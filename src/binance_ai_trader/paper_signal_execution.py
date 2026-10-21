"""Atomic PAPER-ONLY settlement of a previously verified closed-candle signal.

This is a *retrospective synthetic fill* at a stored NEXT-CANDLE open. It is
not a real-market executable price, an automatic strategy worker or an order
endpoint. The source-health assertion is external; no Binance APIs are called.
"""

from __future__ import annotations

import hashlib
import json
import time
from decimal import Decimal

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from binance_ai_trader.feed_quality import FeedStatus, evaluate_closed_spot_feed
from binance_ai_trader.market_data import INTERVAL_MS, Candle
from binance_ai_trader.models import (
    PaperSpotAccountRow,
    PaperSpotJournalRow,
    PaperSpotPositionRow,
    SpotCandleRow,
)
from binance_ai_trader.paper_backtest import BacktestDecision
from binance_ai_trader.paper_execution import (
    PaperExecutionRejected,
    PaperTradeReceipt,
    close_paper_spot,
    open_paper_spot,
)
from binance_ai_trader.paper_signal_plan import (
    PaperSignalPlan,
    SignalPlanStatus,
    SourceState,
    _stable_key,
)
from binance_ai_trader.paper_spot import SpotBuyIntent
from binance_ai_trader.paper_strategy import (
    ClosedCandleObservation,
    SmaCrossParameters,
    SmaCrossStrategy,
)
from binance_ai_trader.risk_schemas import RiskSettings

DEFAULT_PARAMS = SmaCrossParameters()
MAX_CANDLES = 1000


def _identity(plan: PaperSignalPlan) -> tuple[str, str]:
    """Stable key across replay, and one trade ID across settings versions."""
    assert plan.idempotency_key is not None and plan.settings_version is not None
    assert plan.decision is not None
    core = hashlib.sha256(plan.idempotency_key.encode("ascii")).hexdigest()
    payload = {
        "v": 1,
        "signal": plan.idempotency_key,
        "settings_version": plan.settings_version,
        "operation": plan.decision.action,
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("ascii")
    ).hexdigest()
    return "signal-exec-" + digest, "signal_" + core[:40]


def execute_paper_signal_once(
    engine: Engine,
    plan: PaperSignalPlan,
    *,
    source_state: SourceState | None,
    source_checked_at_ms: int | None,
    params: SmaCrossParameters = DEFAULT_PARAMS,
    now_ms: int | None = None,
) -> PaperTradeReceipt:
    """Settle a synthetic signal at the stored following candle's OPEN.

    Both signal recertification and PAPER ledger writes occur in the EXISTING
    locked transactional executor. A duplicate idempotency key replays the
    prior journal receipt without a second position or journal entry.
    """

    clock = time.time_ns() // 1_000_000 if now_ms is None else now_ms
    if type(clock) is not int or clock <= 0:
        raise PaperExecutionRejected("invalid_observation_clock")
    if engine.dialect.name != "postgresql":
        raise PaperExecutionRejected("postgresql_required")
    if not isinstance(params, SmaCrossParameters) or not isinstance(plan, PaperSignalPlan):
        raise PaperExecutionRejected("invalid_signal_plan")
    if (
        plan.status is not SignalPlanStatus.CANDIDATE
        or plan.decision is None
        or plan.idempotency_key is None
        or plan.settings_version is None
        or plan.latest_close_time_ms is None
        or plan.decision.action not in ("buy", "sell")
        or plan.symbol not in ("BTCUSDT", "ETHUSDT")
        or plan.interval not in INTERVAL_MS
    ):
        raise PaperExecutionRejected("unapproved_or_missing_candidate")
    decision = plan.decision
    step = INTERVAL_MS[plan.interval]
    if (
        type(plan.latest_close_time_ms) is not int
        or type(decision.observed_close_time_ms) is not int
        or decision.observed_close_time_ms != plan.latest_close_time_ms
        or type(plan.settings_version) is not int
        or plan.settings_version < 1
        or type(plan.evaluated_at_ms) is not int
        or not plan.latest_close_time_ms < plan.evaluated_at_ms
        or plan.evaluated_at_ms >= plan.latest_close_time_ms + 1 + step
        or decision.explicit_approval is not False
        or plan.idempotency_key
        != _stable_key(
            plan.symbol, plan.interval, plan.latest_close_time_ms, params, decision.action
        )
    ):
        raise PaperExecutionRejected("candidate_identity_or_clock_mismatch")

    # External attestation is REQUIRED, not an independent exchange-health
    # verification. Nothing is automatically fetched, retried, or scheduled.
    if (
        source_state is not SourceState.HEALTHY
        or type(source_checked_at_ms) is not int
        or not 0 <= clock - source_checked_at_ms <= step
    ):
        raise PaperExecutionRejected("source_unverified_or_restricted")

    key, trade_id = _identity(plan)
    price: Decimal | None = None
    stop: Decimal | None = None
    quantity: Decimal | None = None

    # The public reference must come from a persisted, consecutive, CLOSED
    # next candle; never from a caller-supplied or synthetic market price.
    # For the operation we must pass this price to the original service,
    # which revalidates the SAME candle and signal after locking the account.
    with Session(engine) as session:
        if session.in_transaction():
            raise PaperExecutionRejected("unexpected_session_state")
        rows = session.scalars(
            select(SpotCandleRow)
            .where(
                SpotCandleRow.symbol == plan.symbol,
                SpotCandleRow.interval == plan.interval,
                SpotCandleRow.open_time_ms == plan.latest_close_time_ms + 1,
            )
        ).all()
        if len(rows) != 1:
            raise PaperExecutionRejected("next_closed_candle_required")
        row = rows[0]
        price = row.open
        session.rollback()

    def verify_locked(
        session: Session,
        settings: RiskSettings,
        account: PaperSpotAccountRow,
        positions: list[PaperSpotPositionRow],
        events: list[PaperSpotJournalRow],
    ) -> None:
        if (
            settings.mode != "paper"
            or settings.market_type != "spot"
            or not settings.bot_enabled
            or settings.manual_approval
            or plan.symbol not in settings.allowed_symbols
            or account.settings_version != plan.settings_version
            or account.initial_capital_usdt != Decimal(settings.spot_capital_usdt)
        ):
            raise PaperExecutionRejected("signal_settings_or_approval_changed")
        opening = [
            position for position in positions
            if position.status == "open"
        ]
        if len(opening) > 1 or any(p.symbol != plan.symbol for p in opening):
            raise PaperExecutionRejected("uncertain_paper_position")
        if opening and opening[0].opened_at.timestamp() * 1000 > plan.evaluated_at_ms:
            raise PaperExecutionRejected("position_opened_after_signal")
        if (decision.action == "buy") != (not opening):
            raise PaperExecutionRejected("signal_position_state_changed")
        if decision.action == "sell" and opening[0].trade_id != trade_id:
            # Buying manually before the strategy signal is supported, but
            # the plan must use the real trade_id of the open position.
            # This check is performed by the caller's provided plan binding.
            raise PaperExecutionRejected("signal_trade_id_mismatch")
        rows = list(
            session.scalars(
                select(SpotCandleRow)
                .where(
                    SpotCandleRow.symbol == plan.symbol,
                    SpotCandleRow.interval == plan.interval,
                )
                .order_by(SpotCandleRow.open_time_ms.desc())
                .limit(MAX_CANDLES)
            )
        )
        history = tuple(
            Candle(
                symbol=row.symbol, interval=row.interval,
                open_time_ms=row.open_time_ms, close_time_ms=row.close_time_ms,
                open=row.open, high=row.high, low=row.low, close=row.close,
                volume=row.volume,
            )
            for row in reversed(rows)
        )
        quality = evaluate_closed_spot_feed(
            history,
            symbol=plan.symbol,
            interval=plan.interval,
            now_ms=clock,
            min_history=50,
        )
        if quality.status is not FeedStatus.READY:
            raise PaperExecutionRejected("feed_not_ready:" + quality.status.value)
        latest = history[-1]
        previous = history[-2]
        if (
            latest.open_time_ms != plan.latest_close_time_ms + 1
            or previous.close_time_ms != plan.latest_close_time_ms
            or latest.open != price
            or latest.close_time_ms >= clock
            or clock - latest.close_time_ms > step
            or source_checked_at_ms <= latest.close_time_ms
        ):
            raise PaperExecutionRejected("next_candle_or_source_is_stale")
        observed = ClosedCandleObservation(
            history[:-1], position_open=bool(opening)
        )
        recomputed = SmaCrossStrategy(params).on_closed_candle(observed)
        if recomputed != decision:
            raise PaperExecutionRejected("strategy_signal_changed")
        if decision.action == "buy" and (
            quantity is None or stop is None or quantity != decision.quantity
            or stop != decision.stop_price
        ):
            raise PaperExecutionRejected("invalid_buy_payload")
        if decision.action == "sell" and (
            decision.quantity is not None or decision.stop_price is not None
        ):
            raise PaperExecutionRejected("invalid_sell_payload")

    if decision.action == "buy":
        if not isinstance(decision.quantity, Decimal) or not isinstance(
            decision.stop_price, Decimal
        ):
            raise PaperExecutionRejected("invalid_buy_payload")
        quantity = decision.quantity
        stop = decision.stop_price
        with Session(engine) as session:
            return open_paper_spot(
                session,
                SpotBuyIntent(trade_id, plan.symbol, price, quantity, stop),
                key,
                _signal_preflight=verify_locked,
            )
    with Session(engine) as session:
        return close_paper_spot(
            session, trade_id, price, key, _signal_preflight=verify_locked
        )
