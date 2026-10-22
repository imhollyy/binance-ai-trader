"""BT-018 disposable PAPER protective-close settlement rehearsal.

Strictly no worker, API, Binance private client or live/owner PAPER account.
The public observation below MUST use an httpx MockTransport; all observed
quotes, price checks and fills are synthetic. An explicit test authorization
is mandatory even when an operator configured REVIEW_REDUCE_ONLY previously.
"""

from __future__ import annotations

import hashlib
import os
from decimal import Decimal

import httpx
from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session

from binance_ai_trader.ai_constitution import BotState
from binance_ai_trader.ai_position_protection import (
    BotControlSnapshot,
    ProtectionAction,
    StopBehavior,
    evaluate_paper_position_protection,
)
from binance_ai_trader.market_data import MarketDataError
from binance_ai_trader.models import (
    PaperBotControlRow,
    PaperSpotAccountRow,
    PaperSpotJournalRow,
    PaperSpotObservedQuoteRow,
    PaperSpotPositionRow,
    SpotCandleRow,
    TradingSettingsRow,
)
from binance_ai_trader.paper_execution import (
    PaperExecutionRejected,
    PaperTradeReceipt,
    _money,
    _reconcile,
    close_paper_spot,
)
from binance_ai_trader.paper_observed_execution import _history_rows
from binance_ai_trader.paper_spot import PaperCosts, PaperSpotPosition, _costs
from binance_ai_trader.public_spot_observation import (
    PublicSpotObservation,
    PublicSpotObservationClient,
    validate_public_observation,
)
from binance_ai_trader.risk_schemas import RiskSettings

SIMULATED = "trader_protective_close_test"
ENV = "PROTECTIVE_CLOSE_TEST_ISOLATED"


def _isolated(engine: Engine) -> None:
    if (
        engine.dialect.name != "postgresql"
        or engine.url.host not in ("localhost", "127.0.0.1")
        or engine.url.database != SIMULATED
        or os.environ.get(ENV) != "1"
    ):
        raise PaperExecutionRejected("disposable_protective_paper_only")


def _paper_position(row: PaperSpotPositionRow) -> PaperSpotPosition:
    return PaperSpotPosition(
        trade_id=row.trade_id,
        symbol=row.symbol,
        quantity=row.quantity,
        entry_fill_price=row.entry_fill_price,
        entry_notional_usdt=row.entry_notional_usdt,
        entry_fee_usdt=row.entry_fee_usdt,
        stop_price=row.stop_price,
        estimated_stop_loss_usdt=row.estimated_stop_loss_usdt,
    )


def _control(row: PaperBotControlRow) -> BotControlSnapshot:
    try:
        return BotControlSnapshot(
            state=BotState(row.state),
            revision=row.revision,
            settings_version=row.settings_version,
            stop_behavior=StopBehavior(row.stop_behavior),
            operator_confirmed_reduce_only=row.operator_confirmed_reduce_only,
        )
    except ValueError as exc:
        raise PaperExecutionRejected("corrupt_paper_operator_control") from exc


def _confirmed(
    settings: RiskSettings,
    revision: int,
    snapshot: BotControlSnapshot,
    position: PaperSpotPosition,
    sample: PublicSpotObservation,
    now_ms: int,
    take_profit_price: Decimal | None,
) -> ProtectionAction:
    if (
        snapshot.revision != revision
        or snapshot.stop_behavior is not StopBehavior.REVIEW_REDUCE_ONLY
        or not snapshot.operator_confirmed_reduce_only
        or snapshot.state not in (BotState.PAUSED, BotState.EMERGENCY_FLATTEN)
    ):
        raise PaperExecutionRejected("protective_operator_state_denied")
    verdict = evaluate_paper_position_protection(
        settings,
        snapshot,
        position,
        sample,
        settings_version=snapshot.settings_version,
        now_ms=now_ms,
        take_profit_price=take_profit_price,
    )
    # STOP and emergency require operator-configured reduce-only review;
    # optional TP still needs identical explicit independent authorization.
    if verdict.action is ProtectionAction.OBSERVATION_BLOCKED:
        raise PaperExecutionRejected("protective_source_or_settings_denied")
    if verdict.action not in (
        ProtectionAction.REDUCE_ONLY_REVIEW,
        ProtectionAction.ALERT_TAKE_PROFIT,
    ):
        raise PaperExecutionRejected("protective_trigger_not_present")
    return verdict.action


def simulate_isolated_protective_close_once(
    engine: Engine,
    observer: PublicSpotObservationClient,
    *,
    trade_id: str,
    expected_control_revision: int,
    explicit_test_exit_approval: bool,
    costs: PaperCosts,
    take_profit_price: Decimal | None = None,
) -> PaperTradeReceipt:
    """One on-demand mocked-price PAPER settlement behind locked preflight.

    No unattended trigger. The public ticker has NO exchange tick timestamp and
    does not guarantee a tradable price, fill probability or stop execution.
    PnL is net of synthetic fee and adverse slippage, not a real Binance fill.
    """
    _isolated(engine)
    if (
        type(observer) is not PublicSpotObservationClient
        or type(observer._transport) is not httpx.MockTransport
        or type(explicit_test_exit_approval) is not bool
        or not explicit_test_exit_approval
        or type(expected_control_revision) is not int
        or expected_control_revision < 1
        or not isinstance(trade_id, str)
        or not 1 <= len(trade_id) <= 64
        or not trade_id.isascii()
        or not isinstance(costs, PaperCosts)
    ):
        raise PaperExecutionRejected("protective_test_approval_or_source_required")
    try:
        _costs(costs)
    except ValueError as exc:
        raise PaperExecutionRejected("protective_costs_invalid") from exc

    # Initial read deliberately does not authorize any write. Every gate is
    # recomputed under the shared account's transaction lock below.
    with Session(engine) as session:
        session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
        positions = list(
            session.scalars(
                select(PaperSpotPositionRow).where(PaperSpotPositionRow.status == "open")
            )
        )
        if len(positions) != 1 or positions[0].trade_id != trade_id:
            raise PaperExecutionRejected("protective_position_not_open")
        symbol = positions[0].symbol

    try:
        sample = observer.observe(symbol=symbol, interval="1m", limit=100)
        validate_public_observation(sample, now_ms=observer.now_ms())
    except (MarketDataError, ValueError, TypeError) as exc:
        raise PaperExecutionRejected("protective_public_observation_denied") from exc

    trigger = "EMERGENCY" if take_profit_price is None else "TARGET_CHECK"
    # The close uses a stable key per sample and revision. Disagreeing quote
    # with same key raises an idempotency payload conflict in paper_execution.
    seed = (
        f"v1:{trade_id}:{expected_control_revision}:"
        f"{sample.signal_close_time_ms}:{trigger}"
    )
    key = "protected-" + hashlib.sha256(seed.encode("ascii")).hexdigest()[:48]

    def locked(
        session: Session,
        settings: RiskSettings,
        account: PaperSpotAccountRow,
        positions: list[PaperSpotPositionRow],
        journal: list[PaperSpotJournalRow],
    ) -> None:
        settings_row = session.get(TradingSettingsRow, 1)
        control_row = session.scalar(
            select(PaperBotControlRow).where(PaperBotControlRow.id == 1).with_for_update()
        )
        if (
            settings_row is None
            or control_row is None
            or control_row.settings_version != settings_row.version
            or account.settings_version != settings_row.version
            or account.initial_capital_usdt != Decimal(settings.spot_capital_usdt)
            or settings.mode != "paper"
            or settings.market_type != "spot"
        ):
            raise PaperExecutionRejected("protective_settings_unreconciled")
        opened = [p for p in positions if p.status == "open"]
        if (
            len(opened) != 1
            or opened[0].trade_id != trade_id
            or opened[0].symbol != sample.symbol
        ):
            raise PaperExecutionRejected("protective_position_changed_under_lock")
        if _history_rows(session, sample) != sample.closed_candles:
            raise PaperExecutionRejected("protective_stored_feed_disagrees")
        _reconcile(account, positions, journal)
        try:
            validate_public_observation(sample, now_ms=observer.now_ms())
        except (MarketDataError, ValueError, TypeError) as exc:
            raise PaperExecutionRejected("protective_quote_expired_under_lock") from exc
        _confirmed(
            settings,
            expected_control_revision,
            _control(control_row),
            _paper_position(opened[0]),
            sample,
            observer.now_ms(),
            take_profit_price,
        )

    def audit(session: Session, event: PaperSpotJournalRow) -> None:
        session.add(
            PaperSpotObservedQuoteRow(
                journal_id=event.id,
                symbol=sample.symbol,
                interval=sample.interval,
                source=sample.source,
                signal_close_time_ms=sample.signal_close_time_ms,
                requested_at_ms=sample.requested_at_ms,
                received_at_ms=sample.received_at_ms,
                reference_price=_money(sample.reference_price),
                fee_bps=costs.fee_bps,
                slippage_bps=costs.slippage_bps,
            )
        )

    with Session(engine) as session:
        return close_paper_spot(
            session,
            trade_id,
            sample.reference_price,
            key,
            explicit_approval=True,
            costs=costs,
            _signal_preflight=locked,
            _signal_postwrite=audit,
        )
