"""Causal closed-candle strategy adapter for offline PAPER Spot backtests.

Built-in SMA signals are computed ONLY from immutable observed-candle prefixes.
No HTTP, database, exchange credentials, background worker or live execution.
External strategy objects may still capture future data via their own closures:
only the built-in SMA example is audited as causal by this implementation.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from binance_ai_trader.feed_quality import FeedStatus, evaluate_closed_spot_feed
from binance_ai_trader.market_data import Candle, MarketDataError
from binance_ai_trader.paper_backtest import (
    DEFAULT_COSTS,
    BacktestDecision,
    PaperBacktestRejected,
    PaperBacktestResult,
    run_paper_spot_backtest,
)
from binance_ai_trader.paper_spot import PaperCosts
from binance_ai_trader.risk_schemas import RiskSettings

HUNDRED = Decimal("100")
MAX_DECISIONS = 100


@dataclass(frozen=True, slots=True)
class ClosedCandleObservation:
    """No future candle, next-open fill, account equity or market mark is exposed."""

    observed: tuple[Candle, ...]
    position_open: bool

    @property
    def close_time_ms(self) -> int:
        return self.observed[-1].close_time_ms


class CausalSpotStrategy(Protocol):
    """Only historical observations are provided; user implementations are untrusted."""

    def on_closed_candle(self, observation: ClosedCandleObservation) -> BacktestDecision | None: ...


@dataclass(frozen=True, slots=True)
class SmaCrossParameters:
    """Precommitted educational SMA crossover and hypothetical PAPER sizing."""

    fast_window: int = 5
    slow_window: int = 20
    quantity: Decimal = Decimal("1")
    stop_distance_pct: Decimal = Decimal("1")

    def __post_init__(self) -> None:
        if (
            type(self.fast_window) is not int
            or type(self.slow_window) is not int
            or not 2 <= self.fast_window < self.slow_window <= 49
            or not isinstance(self.quantity, Decimal)
            or not self.quantity.is_finite()
            or self.quantity <= 0
            or not isinstance(self.stop_distance_pct, Decimal)
            or not self.stop_distance_pct.is_finite()
            or not Decimal("0") < self.stop_distance_pct <= Decimal("10")
        ):
            raise PaperBacktestRejected("invalid_sma_parameters")


@dataclass(frozen=True, slots=True)
class SmaCrossStrategy:
    """SMA cross from current AND prior *observed* closes only.

    A confirmed upward cross while flat creates a hypothetical buy signal;
    a confirmed downward cross while holding creates a hypothetical full exit.
    No automatic stop execution, AI prediction or forced liquidation exists.
    """

    params: SmaCrossParameters = SmaCrossParameters()

    def on_closed_candle(self, observation: ClosedCandleObservation) -> BacktestDecision | None:
        observed = observation.observed
        fast = self.params.fast_window
        slow = self.params.slow_window
        if len(observed) < slow + 1:
            return None
        closes = tuple(candle.close for candle in observed)
        fast_now = sum(closes[-fast:], Decimal("0")) / Decimal(fast)
        slow_now = sum(closes[-slow:], Decimal("0")) / Decimal(slow)
        fast_before = sum(closes[-fast - 1 : -1], Decimal("0")) / Decimal(fast)
        slow_before = sum(closes[-slow - 1 : -1], Decimal("0")) / Decimal(slow)
        if not observation.position_open and fast_before <= slow_before and fast_now > slow_now:
            stop = closes[-1] * (HUNDRED - self.params.stop_distance_pct) / HUNDRED
            return BacktestDecision(
                observed_close_time_ms=observation.close_time_ms,
                action="buy",
                quantity=self.params.quantity,
                stop_price=stop,
            )
        if observation.position_open and fast_before >= slow_before and fast_now < slow_now:
            return BacktestDecision(
                observed_close_time_ms=observation.close_time_ms,
                action="sell",
            )
        return None


@dataclass(frozen=True, slots=True)
class CausalStrategyResult:
    """Offline, realized-only strategy decisions and PAPER replay result."""

    decisions: tuple[BacktestDecision, ...]
    backtest: PaperBacktestResult


def _checked_history(
    candles: Sequence[Candle],
    *,
    symbol: str,
    interval: str,
    as_of_ms: int,
    warmup_candles: int,
    source_error: MarketDataError | None,
) -> tuple[Candle, ...]:
    if source_error is not None:
        if not isinstance(source_error, MarketDataError):
            raise PaperBacktestRejected("invalid_market_source_error")
        raise PaperBacktestRejected("market_source_unavailable") from source_error
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
    return history


def generate_causal_spot_decisions(
    candles: Sequence[Candle],
    *,
    symbol: str,
    interval: str,
    as_of_ms: int,
    strategy: CausalSpotStrategy,
    warmup_candles: int = 50,
    source_error: MarketDataError | None = None,
) -> tuple[BacktestDecision, ...]:
    """Evaluate every eligible closed candle in order, without a future window.

    A caller-supplied strategy CAN improperly reference external future data;
    passing only a prefix cannot prevent such captured-data leakage.
    Use the built-in deterministic SMA for the audited example.
    """

    history = _checked_history(
        candles,
        symbol=symbol,
        interval=interval,
        as_of_ms=as_of_ms,
        warmup_candles=warmup_candles,
        source_error=source_error,
    )
    if not callable(getattr(strategy, "on_closed_candle", None)):
        raise PaperBacktestRejected("invalid_causal_strategy")
    position_open = False
    decisions: list[BacktestDecision] = []
    # Index len(history)-1 is deliberately never evaluated: there is
    # no subsequent candle open at which its order could be filled.
    for index in range(warmup_candles - 1, len(history) - 1):
        observation = ClosedCandleObservation(
            observed=history[: index + 1],
            position_open=position_open,
        )
        try:
            decision = strategy.on_closed_candle(observation)
        except (ArithmeticError, ValueError) as exc:
            raise PaperBacktestRejected("strategy_evaluation_failed") from exc
        if decision is None:
            continue
        if (
            not isinstance(decision, BacktestDecision)
            or type(decision.observed_close_time_ms) is not int
            or decision.observed_close_time_ms != observation.close_time_ms
            or decision.action not in ("buy", "sell")
            or decision.explicit_approval is not False
        ):
            raise PaperBacktestRejected("invalid_or_future_strategy_signal")
        if decision.action == "buy":
            if position_open:
                raise PaperBacktestRejected("duplicate_unmarked_entry")
            if (
                not isinstance(decision.quantity, Decimal)
                or not decision.quantity.is_finite()
                or decision.quantity <= 0
                or not isinstance(decision.stop_price, Decimal)
                or not decision.stop_price.is_finite()
                or decision.stop_price <= 0
            ):
                raise PaperBacktestRejected("invalid_strategy_entry")
            position_open = True
        else:
            if not position_open:
                raise PaperBacktestRejected("exit_without_entry")
            if decision.quantity is not None or decision.stop_price is not None:
                raise PaperBacktestRejected("invalid_strategy_exit")
            position_open = False
        decisions.append(decision)
        if len(decisions) > MAX_DECISIONS:
            raise PaperBacktestRejected("too_many_synthetic_decisions")
    return tuple(decisions)


def run_causal_paper_spot_backtest(
    candles: Sequence[Candle],
    *,
    symbol: str,
    interval: str,
    as_of_ms: int,
    settings: RiskSettings,
    strategy: CausalSpotStrategy,
    costs: PaperCosts = DEFAULT_COSTS,
    warmup_candles: int = 50,
    source_error: MarketDataError | None = None,
) -> CausalStrategyResult:
    """Generate closed-prefix signals then replay next-open PAPER fills.

    This call never changes the persistent PAPER account. Invalid risk,
    gap-open stop, overnight exposure, disabled bot or missing approval
    rejects the WHOLE hypothetical run rather than omitting losing trades.
    """

    if not isinstance(settings, RiskSettings) or settings.market_type != "spot":
        raise PaperBacktestRejected("spot_paper_only")
    history = _checked_history(
        candles,
        symbol=symbol,
        interval=interval,
        as_of_ms=as_of_ms,
        warmup_candles=warmup_candles,
        source_error=source_error,
    )
    decisions = generate_causal_spot_decisions(
        history,
        symbol=symbol,
        interval=interval,
        as_of_ms=as_of_ms,
        strategy=strategy,
        warmup_candles=warmup_candles,
    )
    report = run_paper_spot_backtest(
        history,
        symbol=symbol,
        interval=interval,
        as_of_ms=as_of_ms,
        settings=settings,
        decisions=decisions,
        costs=costs,
        warmup_candles=warmup_candles,
    )
    return CausalStrategyResult(decisions=decisions, backtest=report)
