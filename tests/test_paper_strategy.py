"""Offline causal Spot strategy: prefix isolation, next-open fills, and fail-closed risk."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from binance_ai_trader.market_data import Candle, MarketRateLimited
from binance_ai_trader.paper_backtest import BacktestDecision, PaperBacktestRejected
from binance_ai_trader.paper_spot import PaperCosts
from binance_ai_trader.paper_strategy import (
    ClosedCandleObservation,
    SmaCrossParameters,
    SmaCrossStrategy,
    generate_causal_spot_decisions,
    run_causal_paper_spot_backtest,
)
from binance_ai_trader.risk_schemas import RiskSettings

D = Decimal


def history(prices: list[str] | None = None) -> list[Candle]:
    values = prices if prices is not None else ["100"] * 70
    candles: list[Candle] = []
    for i, value in enumerate(values):
        close = D(value)
        candles.append(
            Candle(
                symbol="BTCUSDT",
                interval="1m",
                open_time_ms=i * 60_000,
                close_time_ms=(i + 1) * 60_000 - 1,
                open=close,
                high=close + D("1"),
                low=close - D("1"),
                close=close,
                volume=D("2.000000000000000001"),
            )
        )
    return candles


def trend() -> list[Candle]:
    return history(["100"] * 50 + ["99"] * 4 + ["102"] * 5 + ["98"] * 5 + ["100"] * 6)


def config(**changes: object) -> RiskSettings:
    return RiskSettings.model_validate(
        {
            "market_type": "spot",
            "spot_capital_usdt": 1000,
            "bot_enabled": True,
            "manual_approval": False,
            "risk_per_trade_pct": 2,
            **changes,
        }
    )


def generate(data: list[Candle], strategy: SmaCrossStrategy) -> tuple[BacktestDecision, ...]:
    return generate_causal_spot_decisions(
        data,
        symbol="BTCUSDT",
        interval="1m",
        as_of_ms=data[-1].close_time_ms + 1,
        strategy=strategy,
    )


def run(
    data: list[Candle],
    *,
    settings: RiskSettings | None = None,
    strategy: SmaCrossStrategy | None = None,
    costs: PaperCosts | None = None,
) -> object:
    return run_causal_paper_spot_backtest(
        data,
        symbol="BTCUSDT",
        interval="1m",
        as_of_ms=data[-1].close_time_ms + 1,
        settings=settings if settings is not None else config(),
        strategy=strategy if strategy is not None else SmaCrossStrategy(),
        costs=costs if costs is not None else PaperCosts(),
    )


class PrefixRecorder:
    def __init__(self) -> None:
        self.close_times: list[int] = []
        self.prefix_lengths: list[int] = []

    def on_closed_candle(
        self, observation: ClosedCandleObservation
    ) -> BacktestDecision | None:
        self.close_times.append(observation.close_time_ms)
        self.prefix_lengths.append(len(observation.observed))
        assert isinstance(observation.observed, tuple)
        assert observation.observed[-1].close_time_ms == observation.close_time_ms
        assert observation.position_open is False
        return None


def test_full_history_scanned_without_future_candles_or_cherry_picked_ranges() -> None:
    data = history()
    recorder = PrefixRecorder()
    decisions = generate_causal_spot_decisions(
        data,
        symbol="BTCUSDT",
        interval="1m",
        as_of_ms=data[-1].close_time_ms + 1,
        strategy=recorder,
    )
    assert decisions == ()
    assert recorder.prefix_lengths == list(range(50, 70))
    assert recorder.close_times == [data[i].close_time_ms for i in range(49, 69)]
    assert data[-1].close_time_ms not in recorder.close_times
    assert len(data) == 70


def test_flat_market_generates_no_fabricated_trades() -> None:
    data = history()
    strategy = SmaCrossStrategy(SmaCrossParameters(fast_window=3, slow_window=6))
    report = run_causal_paper_spot_backtest(
        data,
        symbol="BTCUSDT",
        interval="1m",
        as_of_ms=data[-1].close_time_ms + 1,
        settings=config(),
        strategy=strategy,
    )
    assert report.decisions == ()
    assert report.backtest.fills == ()
    assert report.backtest.book.cash_usdt == D("1000")
    assert report.backtest.book.realized_pnl_usdt == D("0")


def test_sma_crossover_generates_next_open_paper_fills_and_replays_identically() -> None:
    data = trend()
    strategy = SmaCrossStrategy(SmaCrossParameters(fast_window=3, slow_window=6))
    first = run_causal_paper_spot_backtest(
        data,
        symbol="BTCUSDT",
        interval="1m",
        as_of_ms=data[-1].close_time_ms + 1,
        settings=config(),
        strategy=strategy,
    )
    replay = run_causal_paper_spot_backtest(
        tuple(data),
        symbol="BTCUSDT",
        interval="1m",
        as_of_ms=data[-1].close_time_ms + 1,
        settings=config(),
        strategy=SmaCrossStrategy(SmaCrossParameters(fast_window=3, slow_window=6)),
    )
    assert first == replay
    assert len(first.decisions) >= 2
    assert first.decisions[0].action == "buy"
    assert first.decisions[1].action == "sell"
    assert first.decisions[0].explicit_approval is False
    assert first.backtest.closed_trades >= 1
    for decision, fill in zip(first.decisions, first.backtest.fills, strict=True):
        assert fill.signal_close_time_ms == decision.observed_close_time_ms
        assert fill.fill_open_time_ms == fill.signal_close_time_ms + 1
        assert fill.reference_open_price == data[fill.fill_open_time_ms // 60_000].open
        assert fill.fee_usdt > 0
    assert isinstance(first.backtest.book.cash_usdt, Decimal)
    assert first.backtest.book.paid_fees_usdt > 0


def test_future_candle_shock_does_not_change_earlier_signals() -> None:
    data = trend()
    strategy = SmaCrossStrategy(SmaCrossParameters(fast_window=3, slow_window=6))
    baseline = generate(data, strategy)
    shocked = list(data)
    last = shocked[-1]
    shocked[-1] = replace(
        last,
        open=D("2000"),
        close=D("2000"),
        high=D("2001"),
        low=D("1999"),
    )
    # No strategy callback receives the final candle, because it has no
    # next open. Thus this future shock cannot rewrite any earlier signal.
    assert generate(shocked, strategy) == baseline


@pytest.mark.parametrize("status", [418, 429])
def test_source_rate_limits_block_all_strategy_invocations(status: int) -> None:
    data = trend()
    recorder = PrefixRecorder()
    with pytest.raises(PaperBacktestRejected, match="market_source_unavailable"):
        generate_causal_spot_decisions(
            data,
            symbol="BTCUSDT",
            interval="1m",
            as_of_ms=data[-1].close_time_ms + 1,
            strategy=recorder,
            source_error=MarketRateLimited(status, retry_after_seconds=15),
        )
    assert recorder.prefix_lengths == []


def test_bad_candles_and_staleness_never_call_strategy() -> None:
    data = history()
    variants: list[tuple[list[Candle], int]] = [
        ([], 70 * 60_000),
        (data[:49], 49 * 60_000),
        (data[:25] + data[26:], 70 * 60_000),
        (data[:30] + [data[29]] + data[30:], 70 * 60_000),
        ([*data[:-1], replace(data[-1], symbol="ETHUSDT")], 70 * 60_000),
        ([*data[:-1], replace(data[-1], close=D("NaN"))], 70 * 60_000),
        (data, 70 * 60_000 - 1),
        (data, 70 * 60_000 + 300_000),
    ]
    for candles, clock in variants:
        recorder = PrefixRecorder()
        with pytest.raises(PaperBacktestRejected, match="feed_not_ready"):
            generate_causal_spot_decisions(
                candles,
                symbol="BTCUSDT",
                interval="1m",
                as_of_ms=clock,
                strategy=recorder,
            )
        assert recorder.prefix_lengths == []


class BadSignalStrategy:
    def __init__(self, kind: str) -> None:
        self.kind = kind
        self.calls = 0

    def on_closed_candle(self, observation: ClosedCandleObservation) -> BacktestDecision:
        self.calls += 1
        ms = observation.close_time_ms
        if self.kind == "future":
            ms += 60_000
        if self.kind == "approval":
            return BacktestDecision(ms, "buy", D("1"), D("99"), explicit_approval=True)
        if self.kind == "bad_exit":
            return BacktestDecision(ms, "sell", quantity=D("1"))
        if self.kind == "sell":
            return BacktestDecision(ms, "sell")
        if self.kind == "bad_decimal":
            return BacktestDecision(ms, "buy", D("NaN"), D("99"))
        return BacktestDecision(ms, "buy", D("1"), D("99"))


@pytest.mark.parametrize(
    ("kind", "message"),
    [
        ("future", "invalid_or_future_strategy_signal"),
        ("approval", "invalid_or_future_strategy_signal"),
        ("bad_exit", "exit_without_entry"),
        ("sell", "exit_without_entry"),
        ("bad_decimal", "invalid_strategy_entry"),
        ("duplicate", "duplicate_unmarked_entry"),
    ],
)
def test_custom_callback_fail_closed_on_invalid_or_unapproved_signals(
    kind: str, message: str
) -> None:
    data = history()
    with pytest.raises(PaperBacktestRejected, match=message):
        generate_causal_spot_decisions(
            data,
            symbol="BTCUSDT",
            interval="1m",
            as_of_ms=data[-1].close_time_ms + 1,
            strategy=BadSignalStrategy(kind),
        )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"fast_window": True},
        {"fast_window": 1},
        {"fast_window": 20, "slow_window": 20},
        {"slow_window": 50},
        {"quantity": D("NaN")},
        {"quantity": D("Infinity")},
        {"quantity": D("0")},
        {"stop_distance_pct": D("0")},
        {"stop_distance_pct": D("11")},
        {"stop_distance_pct": D("NaN")},
    ],
)
def test_precommitted_sma_parameters_are_bounded(kwargs: dict[str, object]) -> None:
    with pytest.raises(PaperBacktestRejected, match="invalid_sma_parameters"):
        SmaCrossParameters(**kwargs)  # type: ignore[arg-type]


def test_manual_approval_is_not_forged_and_loss_and_costs_are_realized() -> None:
    data = trend()
    strategy = SmaCrossStrategy(SmaCrossParameters(fast_window=3, slow_window=6))
    with pytest.raises(PaperBacktestRejected, match="manual_approval_required"):
        run_causal_paper_spot_backtest(
            data,
            symbol="BTCUSDT",
            interval="1m",
            as_of_ms=data[-1].close_time_ms + 1,
            settings=config(manual_approval=True),
            strategy=strategy,
        )
    base = run(data, strategy=strategy)
    costly = run(data, strategy=strategy, costs=PaperCosts(fee_bps=15, slippage_bps=10))
    assert costly.backtest.book.paid_fees_usdt > base.backtest.book.paid_fees_usdt
    assert costly.backtest.book.realized_pnl_usdt < base.backtest.book.realized_pnl_usdt
    with pytest.raises(PaperBacktestRejected, match="simulation_rejected"):
        run(data, strategy=strategy, settings=config(spot_capital_usdt=100))


def test_strategy_cannot_claim_live_prices_equity_or_real_orders() -> None:
    data = trend()
    report = run(
        data,
        strategy=SmaCrossStrategy(SmaCrossParameters(fast_window=3, slow_window=6)),
    )
    assert not hasattr(report.backtest, "equity_usdt")
    assert not hasattr(report.backtest, "unrealized_pnl_usdt")
    assert report.backtest.feed_quality.usable_for_paper_signals
