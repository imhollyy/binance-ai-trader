"""Single-shot, PostgreSQL READ ONLY PAPER Spot signal planning.

No Binance network, credentials, order endpoint, worker, database mutation or
promise of execution. The decision is only for the freshest closed candle.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import ValidationError
from sqlalchemy import Engine, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from binance_ai_trader.feed_quality import FeedStatus, evaluate_closed_spot_feed
from binance_ai_trader.market_data import INTERVAL_MS, SUPPORTED_SYMBOLS, Candle, MarketDataError
from binance_ai_trader.models import (
    PaperSpotAccountRow,
    PaperSpotJournalRow,
    PaperSpotPositionRow,
    SpotCandleRow,
    TradingSettingsRow,
)
from binance_ai_trader.paper_backtest import BacktestDecision
from binance_ai_trader.paper_execution import PaperExecutionRejected, _reconcile
from binance_ai_trader.paper_risk import (
    PaperRiskRequest,
    PaperRiskState,
    PaperRiskStatus,
    evaluate_paper_risk,
)
from binance_ai_trader.paper_strategy import (
    ClosedCandleObservation,
    SmaCrossParameters,
    SmaCrossStrategy,
)
from binance_ai_trader.risk_schemas import RiskSettings

STRATEGY_VERSION = "sma-cross-v1"
MAX_JOURNAL_ROWS = 2000
MAX_POSITION_ROWS = 1000
DEFAULT_PARAMS = SmaCrossParameters()
BPS = Decimal("10000")
HUNDRED = Decimal("100")


class SourceState(StrEnum):
    HEALTHY = "healthy"
    RATE_LIMITED = "rate_limited"
    UNAVAILABLE = "unavailable"


class SignalPlanStatus(StrEnum):
    CANDIDATE = "candidate_not_executable"
    NO_SIGNAL = "no_signal"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class PaperSignalPlan:
    status: SignalPlanStatus
    reason: str
    symbol: str
    interval: str
    evaluated_at_ms: int
    latest_close_time_ms: int | None = None
    checked_candles: int = 0
    settings_version: int | None = None
    decision: BacktestDecision | None = None
    idempotency_key: str | None = None
    strategy_version: str = STRATEGY_VERSION


def _block(
    reason: str,
    symbol: str,
    interval: str,
    clock_ms: int,
    *,
    last_close: int | None = None,
    checked: int = 0,
    version: int | None = None,
) -> PaperSignalPlan:
    return PaperSignalPlan(
        SignalPlanStatus.BLOCKED, reason, symbol, interval, clock_ms,
        latest_close_time_ms=last_close,
        checked_candles=checked,
        settings_version=version,
    )


def _stable_key(
    symbol: str, interval: str, close_ms: int, params: SmaCrossParameters
) -> str:
    payload = {
        "symbol": symbol,
        "interval": interval,
        "close_ms": close_ms,
        "strategy_version": STRATEGY_VERSION,
        "fast": params.fast_window,
        "slow": params.slow_window,
        "quantity": format(params.quantity.normalize(), "f"),
        "stop_pct": format(params.stop_distance_pct.normalize(), "f"),
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("ascii")
    ).hexdigest()
    return "paper-signal-" + digest


def _risk_gate(
    *,
    settings: RiskSettings,
    account: PaperSpotAccountRow,
    events: list[PaperSpotJournalRow],
    decision: BacktestDecision,
    close_price: Decimal,
    now_ms: int,
) -> str | None:
    if settings.manual_approval:
        return "manual_approval_required"
    if not settings.bot_enabled:
        return "paper_bot_disabled"
    if decision.action == "sell":
        return None
    quantity = decision.quantity
    stop = decision.stop_price
    if quantity is None or stop is None:
        return "invalid_strategy_entry"
    entry = close_price * (Decimal(1) + Decimal("5") / BPS)
    if not stop < entry:
        return "invalid_stop_at_observed_price"
    notional = entry * quantity
    entry_fee = notional * Decimal("10") / BPS
    stop_fill = stop * (Decimal(1) - Decimal("5") / BPS)
    exit_fee = stop_fill * quantity * Decimal("10") / BPS
    assumed_stop_loss = notional + entry_fee - stop_fill * quantity + exit_fee
    if notional + entry_fee > account.cash_usdt:
        return "insufficient_paper_cash_at_reference"
    if assumed_stop_loss > (
        account.initial_capital_usdt * Decimal(str(settings.risk_per_trade_pct)) / HUNDRED
    ):
        return "estimated_stop_risk_limit"

    today = datetime.fromtimestamp(now_ms // 1000, tz=UTC).date()
    day_start = account.initial_capital_usdt
    running = account.initial_capital_usdt
    peak = running
    for event in events:
        if event.event_type != "spot_sell":
            continue
        if event.created_at.tzinfo is None:
            return "journal_timestamp_untrusted"
        if event.created_at.astimezone(UTC).date() > today:
            return "future_journal_event"
        if event.created_at.astimezone(UTC).date() < today:
            day_start += event.realized_pnl_delta_usdt
        running += event.realized_pnl_delta_usdt
        peak = max(peak, running)
    if running != account.cash_usdt:
        return "unreconciled_realized_pnl"
    risk = evaluate_paper_risk(
        settings,
        PaperRiskRequest(
            proposed_notional_usdt=notional,
            stop_distance_pct=(entry - stop) * HUNDRED / entry,
        ),
        PaperRiskState(
            equity_usdt=account.cash_usdt,
            day_start_equity_usdt=day_start,
            peak_equity_usdt=peak,
            reserved_capital_usdt=Decimal(0),
            open_positions=0,
        ),
    )
    if risk.status is not PaperRiskStatus.ELIGIBLE:
        return "paper_risk_" + risk.reason
    return None


def plan_paper_spot_signal_once(
    engine: Engine | None,
    *,
    symbol: str,
    interval: str,
    source_state: SourceState | None,
    source_checked_at_ms: int | None,
    params: SmaCrossParameters = DEFAULT_PARAMS,
    candles_limit: int = 100,
    source_error: MarketDataError | None = None,
    now_ms: int | None = None,
) -> PaperSignalPlan:
    """Produce a non-executable candidate from a consistent READ ONLY snapshot.

    source_state='healthy' must be explicitly provided by a trusted caller
    after a successful public-feed check; this function CANNOT attest source
    connectivity, and an arbitrary caller's 'healthy' is not proof of it.
    now_ms is injected only for deterministic tests; normal calls use UTC time.
    Repeated/concurrent calls may return the same key. NO durable claim or
    at-most-once execution exists until a later atomic executor is built.
    """

    clock = time.time_ns() // 1_000_000 if now_ms is None else now_ms
    if type(clock) is not int or clock <= 0:
        raise ValueError("invalid_utc_clock")
    if symbol not in SUPPORTED_SYMBOLS or interval not in INTERVAL_MS:
        raise ValueError("unsupported_spot_market")
    if type(candles_limit) is not int or not 50 <= candles_limit <= 1000:
        raise ValueError("invalid_history_limit")
    if not isinstance(params, SmaCrossParameters):
        raise ValueError("invalid_strategy_parameters")
    step = INTERVAL_MS[interval]
    if source_error is not None:
        if not isinstance(source_error, MarketDataError):
            raise ValueError("invalid_source_error")
        return _block("source_error", symbol, interval, clock)
    if (
        not isinstance(source_state, SourceState)
        or source_state is not SourceState.HEALTHY
        or type(source_checked_at_ms) is not int
        or not 0 <= clock - source_checked_at_ms <= step
    ):
        return _block("source_unverified_or_restricted", symbol, interval, clock)
    if engine is None or engine.dialect.name != "postgresql":
        return _block("postgresql_required", symbol, interval, clock)

    try:
        with Session(engine) as session:
            session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
            settings_row = session.get(TradingSettingsRow, 1)
            account = session.get(PaperSpotAccountRow, 1)
            if settings_row is None or account is None:
                return _block("paper_account_or_settings_missing", symbol, interval, clock)
            try:
                settings = RiskSettings.model_validate(settings_row.data)
            except ValidationError:
                return _block("invalid_persisted_settings", symbol, interval, clock)
            if (
                settings.mode != "paper"
                or settings.market_type != "spot"
                or symbol not in settings.allowed_symbols
                or account.mode != "paper"
                or account.market_type != "spot"
                or account.settings_version != settings_row.version
                or account.initial_capital_usdt != Decimal(settings.spot_capital_usdt)
            ):
                return _block("paper_account_settings_mismatch", symbol, interval, clock)

            positions = list(
                session.scalars(
                    select(PaperSpotPositionRow)
                    .where(PaperSpotPositionRow.account_id == 1)
                    .order_by(PaperSpotPositionRow.id)
                    .limit(MAX_POSITION_ROWS + 1)
                )
            )
            events = list(
                session.scalars(
                    select(PaperSpotJournalRow)
                    .where(PaperSpotJournalRow.account_id == 1)
                    .order_by(PaperSpotJournalRow.id)
                    .limit(MAX_JOURNAL_ROWS + 1)
                )
            )
            if len(positions) > MAX_POSITION_ROWS or len(events) > MAX_JOURNAL_ROWS:
                return _block("paper_ledger_unbounded", symbol, interval, clock)
            try:
                _reconcile(account, positions, events)
            except (PaperExecutionRejected, ArithmeticError):
                return _block("paper_ledger_unreconciled", symbol, interval, clock)
            open_positions = [p for p in positions if p.status == "open"]
            if len(open_positions) > 1:
                return _block("paper_position_state_unknown", symbol, interval, clock)
            if open_positions and open_positions[0].symbol != symbol:
                return _block("other_symbol_position_open", symbol, interval, clock)

            stored = list(
                session.scalars(
                    select(SpotCandleRow)
                    .where(SpotCandleRow.symbol == symbol, SpotCandleRow.interval == interval)
                    .order_by(SpotCandleRow.open_time_ms.desc())
                    .limit(candles_limit)
                )
            )
            history = tuple(
                Candle(
                    row.symbol, row.interval, row.open_time_ms, row.close_time_ms,
                    row.open, row.high, row.low, row.close, row.volume,
                )
                for row in reversed(stored)
            )
            quality = evaluate_closed_spot_feed(
                history, symbol=symbol, interval=interval, now_ms=clock, min_history=50
            )
            if quality.status is not FeedStatus.READY:
                return _block(
                    "feed_" + quality.status.value, symbol, interval, clock,
                    last_close=quality.latest_close_time_ms, checked=len(history),
                )
            last = history[-1]
            if clock - last.close_time_ms > step or source_checked_at_ms <= last.close_time_ms:
                return _block(
                    "latest_close_not_currently_actionable", symbol, interval, clock,
                    last_close=last.close_time_ms, checked=len(history),
                )
            decision = SmaCrossStrategy(params).on_closed_candle(
                ClosedCandleObservation(history, position_open=bool(open_positions))
            )
            if decision is None:
                return PaperSignalPlan(
                    SignalPlanStatus.NO_SIGNAL, "no_sma_cross_on_latest_close",
                    symbol, interval, clock, last.close_time_ms, len(history),
                    settings_version=settings_row.version,
                )
            reason = _risk_gate(
                settings=settings, account=account, events=events, decision=decision,
                close_price=last.close, now_ms=clock,
            )
            if reason is not None:
                return _block(
                    reason, symbol, interval, clock, last_close=last.close_time_ms,
                    checked=len(history), version=settings_row.version,
                )
            return PaperSignalPlan(
                SignalPlanStatus.CANDIDATE, "paper_diagnostic_only_next_open_unknown",
                symbol, interval, clock, last.close_time_ms, len(history),
                settings_version=settings_row.version, decision=decision,
                idempotency_key=_stable_key(symbol, interval, last.close_time_ms, params),
            )
    except (SQLAlchemyError, OverflowError, ValueError):
        return _block("database_unavailable_or_invalid", symbol, interval, clock)
