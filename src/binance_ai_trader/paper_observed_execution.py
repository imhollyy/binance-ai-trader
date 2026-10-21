"""Atomic PAPER settlement using a newly received Binance PUBLIC last-price sample.

No live orders or Binance authenticated endpoints. The reference is a local
REST receive-time observation, NOT a Binance order execution timestamp.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from pydantic import ValidationError
from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session

from binance_ai_trader.market_data import Candle, MarketDataError
from binance_ai_trader.models import (
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
    open_paper_spot,
)
from binance_ai_trader.paper_signal_plan import _stable_key
from binance_ai_trader.paper_spot import PaperCosts, SpotBuyIntent
from binance_ai_trader.paper_strategy import (
    ClosedCandleObservation,
    SmaCrossParameters,
    SmaCrossStrategy,
)
from binance_ai_trader.public_spot_observation import (
    PublicSpotObservation,
    PublicSpotObservationClient,
    validate_public_observation,
)
from binance_ai_trader.risk_schemas import RiskSettings

DEFAULT_PARAMS = SmaCrossParameters()
DEFAULT_COSTS = PaperCosts()


class ObservedPaperStatus(StrEnum):
    NO_SIGNAL = "no_signal"
    RECORDED = "paper_recorded"


@dataclass(frozen=True, slots=True)
class ObservedPaperResult:
    status: ObservedPaperStatus
    reference_price: Decimal
    source_observed_at_ms: int
    signal_close_time_ms: int
    receipt: PaperTradeReceipt | None = None


def _history_rows(session: Session, sample: PublicSpotObservation) -> tuple[Candle, ...]:
    rows = list(
        session.scalars(
            select(SpotCandleRow)
            .where(
                SpotCandleRow.symbol == sample.symbol,
                SpotCandleRow.interval == sample.interval,
            )
            .order_by(SpotCandleRow.open_time_ms.desc())
            .limit(len(sample.closed_candles))
        )
    )
    return tuple(
        Candle(
            symbol=row.symbol,
            interval=row.interval,
            open_time_ms=row.open_time_ms,
            close_time_ms=row.close_time_ms,
            open=row.open,
            high=row.high,
            low=row.low,
            close=row.close,
            volume=row.volume,
        )
        for row in reversed(rows)
    )


def _verified_strategy_settings(settings: RiskSettings, symbol: str) -> None:
    if (
        settings.mode != "paper"
        or settings.market_type != "spot"
        or not settings.bot_enabled
        or settings.manual_approval
        or symbol not in settings.allowed_symbols
    ):
        raise PaperExecutionRejected("paper_settings_or_approval_blocked")


def run_observed_paper_spot_once(
    engine: Engine,
    observer: PublicSpotObservationClient,
    *,
    symbol: str,
    interval: str,
    params: SmaCrossParameters = DEFAULT_PARAMS,
    costs: PaperCosts = DEFAULT_COSTS,
    limit: int = 100,
) -> ObservedPaperResult:
    """Observe public data once, then revalidate inside PAPER ledger transaction.

    There is no caller-supplied HEALTHY flag, quote timestamp or fill price.
    The complete observation is obtained by this function's own trusted public
    adapter; DI MockTransport/clock should be used ONLY for offline tests.
    """

    if engine.dialect.name != "postgresql":
        raise PaperExecutionRejected("postgresql_required")
    if type(observer) is not PublicSpotObservationClient:
        raise PaperExecutionRejected("trusted_observer_required")
    if not isinstance(params, SmaCrossParameters):
        raise PaperExecutionRejected("invalid_strategy_configuration")
    if not isinstance(costs, PaperCosts):
        raise PaperExecutionRejected("invalid_cost_configuration")
    try:
        sample = observer.observe(symbol=symbol, interval=interval, limit=limit)
        validate_public_observation(sample, now_ms=observer.now_ms())
    except (MarketDataError, ValueError) as exc:
        raise PaperExecutionRejected("public_spot_observation_blocked") from exc

    # Read-only preflight chooses the intended atomic operation. Settings,
    # positions, journal and complete stored history MUST be checked again
    # inside the subsequent account-locked transaction before any write.
    with Session(engine) as session:
        session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
        setting = session.get(TradingSettingsRow, 1)
        account = session.get(PaperSpotAccountRow, 1)
        if setting is None or account is None:
            raise PaperExecutionRejected("paper_account_or_settings_missing")
        try:
            settings = RiskSettings.model_validate(setting.data)
        except ValidationError as exc:
            raise PaperExecutionRejected("invalid_persisted_settings") from exc
        _verified_strategy_settings(settings, symbol)
        if (
            account.mode != "paper"
            or account.market_type != "spot"
            or account.settings_version != setting.version
            or account.initial_capital_usdt != Decimal(settings.spot_capital_usdt)
        ):
            raise PaperExecutionRejected("account_settings_mismatch")
        positions = list(
            session.scalars(
                select(PaperSpotPositionRow).where(PaperSpotPositionRow.account_id == 1)
            )
        )
        journals = list(
            session.scalars(select(PaperSpotJournalRow).where(PaperSpotJournalRow.account_id == 1))
        )
        _reconcile(account, positions, journals)
        opening = [position for position in positions if position.status == "open"]
        if len(opening) > 1 or any(p.symbol != symbol for p in opening):
            raise PaperExecutionRejected("uncertain_paper_position")
        if _history_rows(session, sample) != sample.closed_candles:
            raise PaperExecutionRejected("stored_candles_disagree_with_public_feed")
        decision = SmaCrossStrategy(params).on_closed_candle(
            ClosedCandleObservation(sample.closed_candles, position_open=bool(opening))
        )
        version = setting.version
        trade_to_close = opening[0].trade_id if opening else None

    if decision is None:
        return ObservedPaperResult(
            ObservedPaperStatus.NO_SIGNAL,
            sample.reference_price,
            sample.received_at_ms,
            sample.signal_close_time_ms,
        )

    # Shared deterministic signal identity with BT-006B protects against both
    # execution models settling the same historical N-close signal.
    key_seed = _stable_key(symbol, interval, sample.signal_close_time_ms, params, decision.action)
    payload = {
        "v": 1,
        "signal": key_seed,
        "settings_version": version,
        "operation": decision.action,
    }
    key = (
        "signal-exec-"
        + hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("ascii")
        ).hexdigest()
    )
    trade_id = "signal_" + hashlib.sha256(key_seed.encode("ascii")).hexdigest()[:40]

    def locked_verify(
        session: Session,
        stored_settings: RiskSettings,
        account: PaperSpotAccountRow,
        positions: list[PaperSpotPositionRow],
        journals: list[PaperSpotJournalRow],
    ) -> None:
        _verified_strategy_settings(stored_settings, symbol)
        stored_version = session.get(TradingSettingsRow, 1)
        if (
            stored_version is None
            or stored_version.version != version
            or account.settings_version != version
            or account.initial_capital_usdt != Decimal(stored_settings.spot_capital_usdt)
        ):
            raise PaperExecutionRejected("signal_settings_changed_under_lock")
        current_open = [p for p in positions if p.status == "open"]
        if len(current_open) > 1 or any(p.symbol != symbol for p in current_open):
            raise PaperExecutionRejected("uncertain_paper_position")
        if (decision.action == "buy") != (not current_open):
            raise PaperExecutionRejected("paper_position_changed_under_lock")
        if decision.action == "sell" and (
            not current_open or current_open[0].trade_id != trade_to_close
        ):
            raise PaperExecutionRejected("paper_position_changed_under_lock")
        if _history_rows(session, sample) != sample.closed_candles:
            raise PaperExecutionRejected("stored_candles_changed_under_lock")
        try:
            validate_public_observation(sample, now_ms=observer.now_ms())
        except (MarketDataError, ValueError) as exc:
            raise PaperExecutionRejected("stale_public_quote_under_lock") from exc
        candidate = SmaCrossStrategy(params).on_closed_candle(
            ClosedCandleObservation(sample.closed_candles, position_open=bool(current_open))
        )
        if candidate != decision:
            raise PaperExecutionRejected("strategy_changed_under_lock")

    def audit(session: Session, journal: PaperSpotJournalRow) -> None:
        session.add(
            PaperSpotObservedQuoteRow(
                journal_id=journal.id,
                symbol=symbol,
                interval=interval,
                source=sample.source,
                signal_close_time_ms=sample.signal_close_time_ms,
                requested_at_ms=sample.requested_at_ms,
                received_at_ms=sample.received_at_ms,
                reference_price=_money(sample.reference_price),
                fee_bps=costs.fee_bps,
                slippage_bps=costs.slippage_bps,
            )
        )

    if decision.action == "buy":
        if decision.quantity is None or decision.stop_price is None:
            raise PaperExecutionRejected("invalid_strategy_entry")
        with Session(engine) as session:
            receipt = open_paper_spot(
                session,
                SpotBuyIntent(
                    trade_id,
                    symbol,
                    sample.reference_price,
                    decision.quantity,
                    decision.stop_price,
                ),
                key,
                costs=costs,
                _signal_preflight=locked_verify,
                _signal_postwrite=audit,
            )
    else:
        if trade_to_close is None:
            raise PaperExecutionRejected("unknown_sell_position")
        with Session(engine) as session:
            receipt = close_paper_spot(
                session,
                trade_to_close,
                sample.reference_price,
                key,
                costs=costs,
                _signal_preflight=locked_verify,
                _signal_postwrite=audit,
            )
    return ObservedPaperResult(
        ObservedPaperStatus.RECORDED,
        sample.reference_price,
        sample.received_at_ms,
        sample.signal_close_time_ms,
        receipt=receipt,
    )
