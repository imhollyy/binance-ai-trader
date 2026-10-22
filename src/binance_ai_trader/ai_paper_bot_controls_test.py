"""BT-016: disposable PostgreSQL-only bot-state compare-and-swap rehearsal.

A server-internal isolated integration fixture, not an authorized HTTP API.
Only two EXPLICIT localhost disposable databases are writable, each with
its own test-only environment sentinel. No worker activation, PAPER account
mutations, execution or exchange calls. Cookie/CSRF trust belongs exclusively
to the guarded staging HTTP entrypoint; this service still accepts only an
operator session hash and MUST NOT be exposed directly.
"""

from __future__ import annotations

import hashlib
import os
import time
from dataclasses import replace

from pydantic import ValidationError
from sqlalchemy import Engine, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from binance_ai_trader.ai_constitution import BotState
from binance_ai_trader.ai_position_protection import (
    BotControlRejected,
    BotControlSnapshot,
    StopBehavior,
    change_paper_bot_state,
)
from binance_ai_trader.models import (
    PaperBotControlEventRow,
    PaperBotControlRow,
    StagingSessionRow,
    TradingSettingsRow,
)
from binance_ai_trader.risk_schemas import RiskSettings


def is_disposable_control_http_test(engine: Engine) -> bool:
    """Hard registration boundary for staging-only HTTPS route tests."""
    return (
        engine.dialect.name == "postgresql"
        and engine.url.host in ("localhost", "127.0.0.1")
        and engine.url.database == "trader_bot_controls_http_test"
        and os.environ.get("BOT_CONTROLS_HTTP_TEST_ISOLATED") == "1"
    )


def _disposable_only(engine: Engine) -> None:
    original_fixture = (
        engine.url.database == "trader_bot_controls_test"
        and os.environ.get("BOT_CONTROLS_TEST_ISOLATED") == "1"
    )
    if (
        engine.dialect.name != "postgresql"
        or engine.url.host not in ("localhost", "127.0.0.1")
        or not (original_fixture or is_disposable_control_http_test(engine))
    ):
        raise BotControlRejected("isolated_bot_control_database_only")


def _clock(now_ms: int | None) -> int:
    now = time.time_ns() // 1_000_000 if now_ms is None else now_ms
    if type(now) is not int or now <= 0:
        raise BotControlRejected("invalid_control_clock")
    return now


def _snapshot(row: PaperBotControlRow) -> BotControlSnapshot:
    try:
        return BotControlSnapshot(
            state=BotState(row.state),
            revision=row.revision,
            settings_version=row.settings_version,
            stop_behavior=StopBehavior(row.stop_behavior),
            operator_confirmed_reduce_only=row.operator_confirmed_reduce_only,
        )
    except ValueError as exc:
        raise BotControlRejected("invalid_persisted_bot_state") from exc


def _settings(session: Session) -> tuple[TradingSettingsRow, RiskSettings]:
    row = session.scalar(
        select(TradingSettingsRow).where(TradingSettingsRow.id == 1).with_for_update()
    )
    if row is None or row.version < 1:
        raise BotControlRejected("persisted_paper_settings_missing")
    try:
        settings = RiskSettings.model_validate(row.data)
    except ValidationError as exc:
        raise BotControlRejected("invalid_persisted_settings") from exc
    if settings.mode != "paper" or settings.market_type != "spot" or settings.max_leverage != 1:
        raise BotControlRejected("spot_paper_only")
    return row, settings


def initialize_disposable_bot_control(
    engine: Engine, *, now_ms: int | None = None
) -> BotControlSnapshot:
    """Seed STOPPED exactly once, with no permission to turn on the worker."""
    _disposable_only(engine)
    now = _clock(now_ms)
    with Session(engine) as session, session.begin():
        settings_row, _ = _settings(session)
        session.execute(
            insert(PaperBotControlRow)
            .values(
                id=1,
                revision=1,
                settings_version=settings_row.version,
                state=BotState.STOPPED.value,
                stop_behavior=StopBehavior.MONITOR_ONLY.value,
                operator_confirmed_reduce_only=False,
                modified_at_ms=now,
            )
            .on_conflict_do_nothing(index_elements=["id"])
        )
        row = session.scalar(
            select(PaperBotControlRow).where(PaperBotControlRow.id == 1).with_for_update()
        )
        if row is None:
            raise BotControlRejected("paper_bot_control_missing")
        return _snapshot(row)


def read_disposable_bot_control(engine: Engine) -> BotControlSnapshot | None:
    """Return actual stored state, never infer RUNNING from bot_enabled."""
    _disposable_only(engine)
    with Session(engine) as session:
        row = session.get(PaperBotControlRow, 1)
        return _snapshot(row) if row is not None else None


def update_disposable_bot_control(
    engine: Engine,
    *,
    actor_session_hash: str,
    expected_revision: int,
    next_state: BotState,
    stop_behavior: StopBehavior,
    confirm_reduce_only_review: bool,
    now_ms: int | None = None,
) -> BotControlSnapshot:
    """One transaction, row locks, revision CAS and append-only audit.

    No API/worker uses this function. The hash must be injected only after
    the future StagingAccess cookie + Origin + CSRF verification; this isolated
    test additionally checks a matching live server-side operator session.
    """
    _disposable_only(engine)
    now = _clock(now_ms)
    if (
        not isinstance(actor_session_hash, str)
        or len(actor_session_hash) != 64
        or any(char not in "0123456789abcdef" for char in actor_session_hash)
        or not isinstance(next_state, BotState)
        or not isinstance(stop_behavior, StopBehavior)
        or type(confirm_reduce_only_review) is not bool
        or (confirm_reduce_only_review and stop_behavior is not StopBehavior.REVIEW_REDUCE_ONLY)
        or type(expected_revision) is not int
        or expected_revision < 1
    ):
        raise BotControlRejected("invalid_operator_control_request")
    with Session(engine) as session, session.begin():
        settings_row, settings = _settings(session)
        current_row = session.scalar(
            select(PaperBotControlRow).where(PaperBotControlRow.id == 1).with_for_update()
        )
        if current_row is None:
            raise BotControlRejected("paper_control_bootstrap_required")
        existing = _snapshot(current_row)
        if (
            existing.revision != expected_revision
            or existing.settings_version != settings_row.version
        ):
            raise BotControlRejected("control_revision_or_settings_stale")
        actor = session.scalar(
            select(StagingSessionRow)
            .where(StagingSessionRow.token_hash == actor_session_hash)
            .with_for_update()
        )
        if (
            actor is None
            or actor.role != "operator"
            or actor.revoked
            or actor.issued_at_ms > now
            or actor.expires_at_ms <= now
        ):
            raise BotControlRejected("authorized_operator_session_required")
        # No no-op audit rows: a persisted change must be meaningful.
        if next_state is existing.state:
            if (
                stop_behavior is existing.stop_behavior
                and confirm_reduce_only_review == existing.operator_confirmed_reduce_only
            ):
                raise BotControlRejected("no_op_transition_requires_new_revision")
            changed = replace(existing, revision=existing.revision + 1)
        else:
            changed = change_paper_bot_state(
                settings,
                existing,
                expected_revision=expected_revision,
                next_state=next_state,
                operator_authorized=True,
            )
        changed = replace(
            changed,
            stop_behavior=stop_behavior,
            operator_confirmed_reduce_only=confirm_reduce_only_review,
        )
        if changed.revision != expected_revision + 1:
            raise BotControlRejected("control_revision_unexpected")
        current_row.revision = changed.revision
        current_row.state = changed.state.value
        current_row.stop_behavior = changed.stop_behavior.value
        current_row.operator_confirmed_reduce_only = changed.operator_confirmed_reduce_only
        current_row.modified_at_ms = now
        session.add(
            PaperBotControlEventRow(
                revision=changed.revision,
                previous_state=existing.state.value,
                new_state=changed.state.value,
                stop_behavior=changed.stop_behavior.value,
                operator_confirmed_reduce_only=changed.operator_confirmed_reduce_only,
                settings_version=existing.settings_version,
                actor_session_hash=hashlib.sha256(actor_session_hash.encode("ascii")).hexdigest(),
                recorded_at_ms=now,
            )
        )
        session.flush()
        return changed
