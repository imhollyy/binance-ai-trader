"""Deterministic, in-memory PAPER Spot backtesting over validated closed candles.

No database/session, Binance authenticated client, HTTP endpoint, scheduler, worker
or actual order. A decision at candle close may fill ONLY at the NEXT candle open.
Decisions are externally supplied, not computed from future market observations.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from binance_ai_trader.feed_quality import FeedQuality, FeedStatus, evaluate_closed_spot_feed
from binance_ai_trader.market_data import Candle, MarketDataError
from binance_ai_trader.paper_risk import PaperRiskState
from binance_ai_trader.paper_spot import (
    PaperCosts,
    PaperSpotBook,
    PaperSpotRejected,
    SpotBuyIntent,
    new_paper_spot_book,
    simulate_spot_buy,
    simulate_spot_sell,
)
from binance_ai_trader.risk_schemas import RiskSettings

DAY_MS = 86_400_000
DEFAULT_COSTS = PaperCosts()  # Frozen dataclass; safe shared default.
BacktestAction = Literal["buy", "sell"]


class PaperBacktestRejected(ValueError):
    """The historical simulation is unsafe or has an invalid decision schedule."""


@dataclass(frozen=True, slots=True)
class BacktestDecision:
    """An EXTERNAL synthetic decision, made at the CLOSE of an observed candle.

    An entry requires both a hypothetical quantity and a stop below the next
    opening reference price. A sell always closes the full single Spot long.
    The caller is responsible for ensuring decisions were not derived from
    future candles; this engine guarantees execution never uses same-bar close.
    """

    observed_close_time_ms: int
    action: BacktestAction
    quantity: Decimal | None = None
    stop_price: Decimal | None = None
    explicit_approval: bool = False


@dataclass(frozen=True, slots=True)
class BacktestFill:
    """Fictional fill priced from the NEXT bar's open plus explicit costs."""

    trade_id: str
    action: BacktestAction
    signal_close_time_ms: int
    fill_open_time_ms: int
    reference_open_price: Decimal
    assumed_fill_price: Decimal
    fee_usdt: Decimal
    realized_pnl_delta_usdt: Decimal
    cash_after_usdt: Decimal


@dataclass(frozen=True, slots=True)
class PaperBacktestResult:
    """Realized-only report; cash is NOT total equity with an open position."""

    symbol: str
    interval: str
    first_open_time_ms: int
    last_close_time_ms: int
    as_of_ms: int
    feed_quality: FeedQuality
    costs: PaperCosts
    book: PaperSpotBook
    fills: tuple[BacktestFill, ...]

    @property
    def closed_trades(self) -> int:
        return len(self.book.closed)

    @property
    def open_positions(self) -> int:
        return len(self.book.positions)


def _positive(value: Decimal | None) -> bool:
    return isinstance(value, Decimal) and value.is_finite() and value > 0


def _validate_decisions(
    candles: tuple[Candle, ...],
    decisions: tuple[BacktestDecision, ...],
    warmup_candles: int,
) -> dict[int, BacktestDecision]:
    if len(decisions) > 100:
        raise PaperBacktestRejected("too_many_synthetic_decisions")
    by_close = {c.close_time_ms: i for i, c in enumerate(candles)}
    mapping: dict[int, BacktestDecision] = {}
    last_index = -1
    for decision in decisions:
        if (
            not isinstance(decision, BacktestDecision)
            or type(decision.observed_close_time_ms) is not int
            or decision.action not in ("buy", "sell")
            or type(decision.explicit_approval) is not bool
        ):
            raise PaperBacktestRejected("invalid_synthetic_decision")
        index = by_close.get(decision.observed_close_time_ms)
        if index is None or index < warmup_candles - 1 or index >= len(candles) - 1:
            raise PaperBacktestRejected("signal_unobserved_unwarmed_or_unfillable")
        if index <= last_index:
            raise PaperBacktestRejected("unordered_or_duplicate_signals")
        if decision.action == "buy":
            if not _positive(decision.quantity) or not _positive(decision.stop_price):
                raise PaperBacktestRejected("invalid_paper_entry_decision")
        elif decision.quantity is not None or decision.stop_price is not None:
            raise PaperBacktestRejected("sell_must_not_specify_entry_parameters")
        mapping[index] = decision
        last_index = index
    return mapping


def run_paper_spot_backtest(
    candles: Sequence[Candle],
    *,
    symbol: str,
    interval: str,
    as_of_ms: int,
    settings: RiskSettings,
    decisions: Sequence[BacktestDecision],
    costs: PaperCosts = DEFAULT_COSTS,
    warmup_candles: int = 50,
    source_error: MarketDataError | None = None,
) -> PaperBacktestResult:
    """Replay historical synthetic decisions without any persistent side effect.

    Full candle history must pass the existing fail-closed READY quality gate
    as of the caller's explicit historical clock. This clock is a historical
    replay watermark, NOT proof the candles are fresh at real-world wall time.

    No trading occurs unless an externally supplied decision was available
    at a closed candle; its simulated fill uses the NEXT bar's opening price.
    No automatic stops or forced end-of-series liquidation are fabricated.

    For safety, one open long at a time and no overnight open position:
    marked equity across a UTC day boundary is not modeled. Daily loss and
    peak drawdown gates use realized cash ONLY while the book is flat.
    """

    if source_error is not None:
        if not isinstance(source_error, MarketDataError):
            raise PaperBacktestRejected("invalid_market_source_error")
        raise PaperBacktestRejected("market_source_unavailable") from source_error
    if not isinstance(settings, RiskSettings) or settings.mode != "paper":
        raise PaperBacktestRejected("paper_only")
    if settings.market_type != "spot":
        raise PaperBacktestRejected("spot_only")
    if (
        not isinstance(costs, PaperCosts)
        or type(costs.fee_bps) is not int
        or type(costs.slippage_bps) is not int
        or not 0 <= costs.fee_bps <= 100
        or not 0 <= costs.slippage_bps <= 100
    ):
        raise PaperBacktestRejected("invalid_simulation_costs")
    if type(warmup_candles) is not int or not 50 <= warmup_candles <= 1000:
        raise PaperBacktestRejected("invalid_warmup")
    history = tuple(candles)
    if any(not isinstance(candle, Candle) for candle in history):
        raise PaperBacktestRejected("invalid_history_type")
    try:
        quality = evaluate_closed_spot_feed(
            history,
            symbol=symbol,
            interval=interval,
            now_ms=as_of_ms,
            min_history=warmup_candles,
        )
    except ValueError as exc:
        raise PaperBacktestRejected("invalid_history_config") from exc
    if quality.status is not FeedStatus.READY:
        raise PaperBacktestRejected(f"feed_not_ready:{quality.status.value}")

    schedule = _validate_decisions(history, tuple(decisions), warmup_candles)
    try:
        book = new_paper_spot_book(settings)
    except PaperSpotRejected as exc:
        raise PaperBacktestRejected(str(exc)) from exc

    peak_cash = book.cash_usdt
    day_start_cash = book.cash_usdt
    current_day: int | None = None
    fills: list[BacktestFill] = []
    for signal_index, decision in schedule.items():
        next_open = history[signal_index + 1]
        fill_day = next_open.open_time_ms // DAY_MS
        if fill_day != current_day:
            if book.positions:
                raise PaperBacktestRejected("overnight_mark_to_market_not_supported")
            day_start_cash = book.cash_usdt
            current_day = fill_day

        reference = next_open.open
        before = book
        try:
            if decision.action == "buy":
                if book.positions:
                    raise PaperBacktestRejected("unmarked_open_position_not_supported")
                assert decision.quantity is not None and decision.stop_price is not None
                state = PaperRiskState(
                    equity_usdt=book.cash_usdt,
                    day_start_equity_usdt=day_start_cash,
                    peak_equity_usdt=peak_cash,
                    reserved_capital_usdt=Decimal("0"),
                    open_positions=0,
                )
                trade_id = f"bt_{signal_index}"
                book = simulate_spot_buy(
                    book,
                    settings,
                    state,
                    SpotBuyIntent(
                        trade_id, symbol, reference, decision.quantity, decision.stop_price
                    ),
                    explicit_approval=decision.explicit_approval,
                    costs=costs,
                )
                position = book.positions[-1]
                fill_price = position.entry_fill_price
                fee = position.entry_fee_usdt
                realized = Decimal("0")
            else:
                if len(book.positions) != 1:
                    raise PaperBacktestRejected("paper_position_not_open")
                trade_id = book.positions[0].trade_id
                book = simulate_spot_sell(book, trade_id, reference, costs=costs)
                closed = book.closed[-1]
                fill_price = closed.exit_fill_price
                fee = closed.exit_fee_usdt
                realized = closed.realized_pnl_usdt
                peak_cash = max(peak_cash, book.cash_usdt)
        except PaperSpotRejected as exc:
            raise PaperBacktestRejected(f"simulation_rejected:{exc}") from exc
        assert book is not before
        fills.append(
            BacktestFill(
                trade_id=trade_id,
                action=decision.action,
                signal_close_time_ms=history[signal_index].close_time_ms,
                fill_open_time_ms=next_open.open_time_ms,
                reference_open_price=reference,
                assumed_fill_price=fill_price,
                fee_usdt=fee,
                realized_pnl_delta_usdt=realized,
                cash_after_usdt=book.cash_usdt,
            )
        )

    return PaperBacktestResult(
        symbol=symbol,
        interval=interval,
        first_open_time_ms=history[0].open_time_ms,
        last_close_time_ms=history[-1].close_time_ms,
        as_of_ms=as_of_ms,
        feed_quality=quality,
        costs=costs,
        book=book,
        fills=tuple(fills),
    )
