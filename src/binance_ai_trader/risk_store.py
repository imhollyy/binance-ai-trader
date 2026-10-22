# ruff: noqa: E501
"""Persistent BT-024 risk controls, reservations and audit; isolated local DB only."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import replace
from decimal import Decimal
from typing import Any, cast

from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from binance_ai_trader.ai_constitution import BotState
from binance_ai_trader.ai_shadow_preflight import ShadowPreflightResult
from binance_ai_trader.ai_trainable_model import ResearchPrediction
from binance_ai_trader.risk_guard import (
    RiskDecision,
    RiskProfile,
    RiskRequest,
    evaluate_risk,
    validate_profile,
)

_ID = re.compile(r"^[A-Za-z0-9_-]{8,100}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_LOCK_ID = 23024024


class RiskStoreRejected(ValueError):
    """Persistent risk operation rejected."""


def _isolated(engine: Engine) -> None:
    test_store = (
        engine.url.database == "trader_ai_bt023_test"
        and os.environ.get("TRADEPILOT_BT023_TEST_DB") == "1"
    )
    research_store = (
        engine.url.database == "trader_ai_research"
        and os.environ.get("TRADEPILOT_RISK_STORE") == "1"
    )
    if (
        engine.dialect.name != "postgresql"
        or engine.url.host not in ("127.0.0.1", "localhost")
        or not (test_store or research_store)
    ):
        raise RiskStoreRejected("dedicated_local_bt023_test_database_required")


def _profile_json(profile: RiskProfile) -> dict[str, object]:
    return {
        "schema_version": profile.schema_version,
        "profile_version": profile.profile_version,
        "risk_level": profile.risk_level,
        "max_positions": profile.max_positions,
        "risk_per_trade_pct": str(profile.risk_per_trade_pct),
        "max_open_capital": str(profile.max_open_capital),
        "spot_budget": str(profile.spot_budget),
        "daily_loss_pct": str(profile.daily_loss_pct),
        "max_drawdown_pct": str(profile.max_drawdown_pct),
        "max_consecutive_losses": profile.max_consecutive_losses,
        "user_hard_limits": [[k, str(v)] for k, v in profile.user_hard_limits],
    }


def _profile(value: Mapping[str, Any]) -> RiskProfile:
    hard = cast(list[list[object]], value["user_hard_limits"])
    return RiskProfile(
        str(value["schema_version"]),
        int(value["profile_version"]),
        int(value["risk_level"]),
        int(value["max_positions"]),
        Decimal(str(value["risk_per_trade_pct"])),
        Decimal(str(value["max_open_capital"])),
        Decimal(str(value["spot_budget"])),
        Decimal(str(value["daily_loss_pct"])),
        Decimal(str(value["max_drawdown_pct"])),
        int(value["max_consecutive_losses"]),
        tuple((str(pair[0]), Decimal(str(pair[1]))) for pair in hard),
    )


def install_profile(
    engine: Engine,
    profile: RiskProfile,
    *,
    user_confirmed: bool,
    expected_runtime_revision: int,
    state: BotState = BotState.STOPPED,
) -> int:
    """Store explicitly confirmed profile without enabling the bot."""
    _isolated(engine)
    if state is not BotState.STOPPED:
        raise RiskStoreRejected("initial_profile_must_start_stopped")
    if type(user_confirmed) is not bool or not user_confirmed or not validate_profile(profile):
        raise RiskStoreRejected("explicitly_confirmed_complete_profile_required")
    with Session(engine) as session, session.begin():
        session.execute(text("SELECT pg_advisory_xact_lock(:id)"), {"id": _LOCK_ID})
        runtime = (
            session.execute(
                text(
                    "SELECT revision,active_profile_version FROM ai_risk_runtime WHERE id=1 FOR UPDATE"
                )
            )
            .mappings()
            .one_or_none()
        )
        if runtime is None:
            if expected_runtime_revision != 0 or profile.profile_version != 1:
                raise RiskStoreRejected("stale_initial_profile_version")
            session.execute(
                text(
                    "INSERT INTO ai_risk_profiles(risk_level,profile_version,profile_json,user_confirmed) VALUES(:level,:version,CAST(:data AS json),true)"
                ),
                {
                    "level": profile.risk_level,
                    "version": profile.profile_version,
                    "data": json.dumps(_profile_json(profile)),
                },
            )
            session.execute(
                text(
                    "INSERT INTO ai_risk_runtime(id,active_profile_version,active_risk_level,bot_state,revision) VALUES(1,:version,:level,:state,1)"
                ),
                {
                    "version": profile.profile_version,
                    "level": profile.risk_level,
                    "state": state.value,
                },
            )
            return 1
        current_revision = int(runtime["revision"])
        active_profile_version = int(runtime["active_profile_version"])
        if (
            current_revision != expected_runtime_revision
            or profile.profile_version != active_profile_version + 1
        ):
            raise RiskStoreRejected("stale_profile_revision")
        session.execute(
            text(
                "INSERT INTO ai_risk_profiles(risk_level,profile_version,profile_json,user_confirmed) VALUES(:level,:version,CAST(:data AS json),true)"
            ),
            {
                "level": profile.risk_level,
                "version": profile.profile_version,
                "data": json.dumps(_profile_json(profile)),
            },
        )
        updated_revision = session.execute(
            text(
                "UPDATE ai_risk_runtime SET active_profile_version=:version,active_risk_level=:level,revision=revision+1 WHERE id=1 AND revision=:expected RETURNING revision"
            ),
            {
                "version": profile.profile_version,
                "level": profile.risk_level,
                "expected": expected_runtime_revision,
            },
        ).scalar_one_or_none()
        if updated_revision is None:
            raise RiskStoreRejected("stale_profile_revision")
        return int(updated_revision)


def set_bot_state(engine: Engine, *, state: BotState, expected_revision: int) -> int:
    _isolated(engine)
    transitions = {
        BotState.STOPPED: {BotState.PAUSED},
        BotState.PAUSED: {BotState.RUNNING, BotState.STOPPED, BotState.EMERGENCY_FLATTEN},
        BotState.RUNNING: {BotState.PAUSED, BotState.STOPPED, BotState.EMERGENCY_FLATTEN},
        BotState.EMERGENCY_FLATTEN: {BotState.STOPPED},
    }
    with Session(engine) as session, session.begin():
        session.execute(text("SELECT pg_advisory_xact_lock(:id)"), {"id": _LOCK_ID})
        row = session.execute(
            text("SELECT bot_state,revision FROM ai_risk_runtime WHERE id=1 FOR UPDATE")
        ).one_or_none()
        if (
            row is None
            or row.revision != expected_revision
            or state not in transitions[BotState(row.bot_state)]
        ):
            raise RiskStoreRejected("invalid_or_stale_bot_transition")
        session.execute(
            text("UPDATE ai_risk_runtime SET bot_state=:state,revision=revision+1 WHERE id=1"),
            {"state": state.value},
        )
        session.execute(
            text(
                "INSERT INTO ai_risk_audit(event_id,signal_id,profile_version,bot_revision,decision_json) "
                "SELECT :event,:signal,active_profile_version,revision,CAST(:payload AS json) FROM ai_risk_runtime WHERE id=1"
            ),
            {
                "event": f"botstate_{expected_revision + 1}",
                "signal": f"botstate_{expected_revision + 1}",
                "payload": json.dumps({"action": "BOT_STATE_TRANSITION", "state": state.value}),
            },
        )
        return expected_revision + 1


def record_account_snapshot(
    engine: Engine,
    *,
    snapshot_id: str,
    equity: Decimal,
    day_start_equity: Decimal,
    peak_equity: Decimal,
    open_capital: Decimal,
    open_positions: int,
    consecutive_losses: int,
    source_sha256: str,
) -> None:
    """Append exact Decimal account state attested to a caller's paper-ledger digest."""
    _isolated(engine)
    values = (equity, day_start_equity, peak_equity)
    if (
        _ID.fullmatch(snapshot_id) is None
        or _HASH.fullmatch(source_sha256) is None
        or any(type(v) is not Decimal or not v.is_finite() or v <= 0 for v in values)
        or type(open_positions) is not int
        or open_positions < 0
        or type(open_capital) is not Decimal
        or not open_capital.is_finite()
        or open_capital < 0
        or type(consecutive_losses) is not int
        or consecutive_losses < 0
    ):
        raise RiskStoreRejected("invalid_account_snapshot")
    with Session(engine) as session, session.begin():
        session.execute(
            text(
                "INSERT INTO ai_risk_account_snapshots(snapshot_id,equity_usdt,day_start_equity_usdt,peak_equity_usdt,open_capital_usdt,open_positions,consecutive_losses,source_sha256) "
                "VALUES(:id,:equity,:day,:peak,:capital,:positions,:losses,:hash)"
            ),
            {
                "id": snapshot_id,
                "equity": equity,
                "day": day_start_equity,
                "peak": peak_equity,
                "capital": open_capital,
                "positions": open_positions,
                "losses": consecutive_losses,
                "hash": source_sha256,
            },
        )


def evaluate_and_reserve(
    engine: Engine, *, request_id: str, signal_id: str, profile_version: int, request: RiskRequest
) -> RiskDecision:
    """Lock config/exposure, audit every result, and reserve atomically if allowed."""
    _isolated(engine)
    if _ID.fullmatch(request_id) is None or _ID.fullmatch(signal_id) is None:
        raise RiskStoreRejected("invalid_request_or_signal_id")
    with Session(engine) as session, session.begin():
        session.execute(text("SELECT pg_advisory_xact_lock(:id)"), {"id": _LOCK_ID})
        runtime = (
            session.execute(text("SELECT * FROM ai_risk_runtime WHERE id=1 FOR UPDATE"))
            .mappings()
            .one_or_none()
        )
        stored = session.execute(
            text(
                "SELECT profile_json FROM ai_risk_profiles WHERE risk_level=:level AND profile_version=:version"
            ),
            {"level": runtime["active_risk_level"] if runtime else -1, "version": profile_version},
        ).scalar_one_or_none()
        profile = _profile(stored) if stored is not None else None
        if runtime is None or runtime["active_profile_version"] != profile_version:
            profile = None
        reserved = cast(
            Decimal,
            session.execute(
                text("""SELECT COALESCE(SUM(CASE event_type WHEN 'RESERVE' THEN amount_usdt ELSE -amount_usdt END),0)
            FROM ai_risk_reservation_events""")
            ).scalar_one(),
        )
        positions = int(
            session.execute(
                text("""SELECT COUNT(*) FROM (
            SELECT reservation_id FROM ai_risk_reservation_events GROUP BY reservation_id
            HAVING SUM(CASE event_type WHEN 'RESERVE' THEN 1 ELSE -1 END)>0) active""")
            ).scalar_one()
        )
        snapshot = (
            session.execute(
                text("SELECT * FROM ai_risk_account_snapshots ORDER BY id DESC LIMIT 1")
            )
            .mappings()
            .one_or_none()
        )
        effective = replace(
            request,
            settings_version=request.settings_version,
            bot_state=BotState.STOPPED if runtime is None else BotState(runtime["bot_state"]),
            current_open_capital=Decimal(reserved)
            + (Decimal(0) if snapshot is None else Decimal(snapshot["open_capital_usdt"])),
            equity=Decimal(0) if snapshot is None else Decimal(snapshot["equity_usdt"]),
            day_start_equity=Decimal(0)
            if snapshot is None
            else Decimal(snapshot["day_start_equity_usdt"]),
            peak_equity=Decimal(0) if snapshot is None else Decimal(snapshot["peak_equity_usdt"]),
            open_positions=int(positions)
            + (0 if snapshot is None else int(snapshot["open_positions"])),
            consecutive_losses=10**9 if snapshot is None else int(snapshot["consecutive_losses"]),
        )
        decision = evaluate_risk(profile, effective)
        if session.execute(
            text("SELECT 1 FROM ai_risk_audit WHERE event_id=:id"), {"id": request_id}
        ).first():
            raise RiskStoreRejected("duplicate_risk_audit_id")
        payload = {
            "allowed": decision.allowed,
            "reason": decision.reason.value,
            "audit_code": decision.audit_code,
            "allowed_notional": str(decision.allowed_notional),
            "net_edge_bps": str(decision.net_edge_bps)
            if decision.net_edge_bps is not None
            else None,
            "request": {
                "settings_version": request.settings_version,
                "proposed_notional": str(request.proposed_notional),
                "stop_loss_price": str(request.stop_loss_price)
                if request.stop_loss_price is not None
                else None,
                "take_profit_price": str(request.take_profit_price)
                if request.take_profit_price is not None
                else None,
                "reference_price": str(request.reference_price),
                "fee_bps_round_trip": str(request.fee_bps_round_trip)
                if request.fee_bps_round_trip is not None
                else None,
                "spread_bps": str(request.spread_bps) if request.spread_bps is not None else None,
                "slippage_bps": str(request.slippage_bps)
                if request.slippage_bps is not None
                else None,
                "expected_gross_edge_bps": str(request.expected_gross_edge_bps)
                if request.expected_gross_edge_bps is not None
                else None,
                "min_net_edge_bps": str(request.min_net_edge_bps)
                if request.min_net_edge_bps is not None
                else None,
                "account_snapshot_id": None if snapshot is None else snapshot["snapshot_id"],
            },
        }
        session.execute(
            text(
                "INSERT INTO ai_risk_audit(event_id,signal_id,profile_version,bot_revision,decision_json) VALUES(:id,:signal,:version,:revision,CAST(:decision AS json))"
            ),
            {
                "id": request_id,
                "signal": signal_id,
                "version": profile_version,
                "revision": 0 if runtime is None else runtime["revision"],
                "decision": json.dumps(payload),
            },
        )
        if decision.allowed:
            session.execute(
                text(
                    "INSERT INTO ai_risk_reservation_events(event_id,reservation_id,event_type,amount_usdt,profile_version) VALUES(:id,:reservation,'RESERVE',:amount,:version)"
                ),
                {
                    "id": request_id + "_reserve",
                    "reservation": request_id,
                    "amount": decision.allowed_notional,
                    "version": profile_version,
                },
            )
        return decision


def release_reservation(
    engine: Engine, *, event_id: str, reservation_id: str, profile_version: int
) -> None:
    _isolated(engine)
    if _ID.fullmatch(event_id) is None or _ID.fullmatch(reservation_id) is None:
        raise RiskStoreRejected("invalid_reservation_id")
    with Session(engine) as session, session.begin():
        session.execute(text("SELECT pg_advisory_xact_lock(:id)"), {"id": _LOCK_ID})
        amount = session.execute(
            text(
                "SELECT amount_usdt FROM ai_risk_reservation_events WHERE reservation_id=:id AND event_type='RESERVE'"
            ),
            {"id": reservation_id},
        ).scalar_one_or_none()
        released = int(
            session.execute(
                text(
                    "SELECT COUNT(*) FROM ai_risk_reservation_events WHERE reservation_id=:id AND event_type='RELEASE'"
                ),
                {"id": reservation_id},
            ).scalar_one()
        )
        if amount is None or released:
            raise RiskStoreRejected("unknown_or_already_released_reservation")
        session.execute(
            text(
                "INSERT INTO ai_risk_reservation_events(event_id,reservation_id,event_type,amount_usdt,profile_version) VALUES(:event,:reservation,'RELEASE',:amount,:version)"
            ),
            {
                "event": event_id,
                "reservation": reservation_id,
                "amount": amount,
                "version": profile_version,
            },
        )


def record_shadow_preflight(
    engine: Engine,
    *,
    event_id: str,
    prediction: ResearchPrediction,
    result: ShadowPreflightResult,
) -> None:
    """Persist suitability/audit result; the row can never represent an order."""
    _isolated(engine)
    if (
        _ID.fullmatch(event_id) is None
        or type(prediction) is not ResearchPrediction
        or type(result) is not ShadowPreflightResult
        or result.order_intent_created
    ):
        raise RiskStoreRejected("invalid_shadow_preflight_audit")
    with Session(engine) as session, session.begin():
        runtime = (
            session.execute(
                text("SELECT active_profile_version,revision FROM ai_risk_runtime WHERE id=1")
            )
            .mappings()
            .one_or_none()
        )
        risk_decision = result.risk_decision
        payload = {
            "event_type": "PAPER_SUITABILITY_ONLY",
            "status": result.status,
            "reason_code": result.reason_code,
            "paper_suitability": result.paper_suitability,
            "order_intent_created": False,
            "model_version": prediction.model_version,
            "artifact_sha256": prediction.artifact_sha256,
            "data_sha256": result.data_sha256,
            "cost_sha256": result.cost_sha256,
            "model_action": prediction.action.value,
            "risk_decision": None if risk_decision is None else risk_decision.audit_code,
        }
        session.execute(
            text(
                "INSERT INTO ai_risk_audit(event_id,signal_id,profile_version,bot_revision,decision_json) "
                "VALUES(:event,:signal,:version,:revision,CAST(:payload AS json))"
            ),
            {
                "event": event_id,
                "signal": prediction.signal_id,
                "version": None if runtime is None else runtime["active_profile_version"],
                "revision": 0 if runtime is None else runtime["revision"],
                "payload": json.dumps(payload),
            },
        )
