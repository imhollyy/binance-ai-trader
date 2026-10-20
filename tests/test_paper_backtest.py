"""Historical closed-bar PAPER Spot replay: deterministic, no I/O or hidden future fill."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from decimal import Decimal

import pytest

from binance_ai_trader.market_data import Candle, MarketRateLimited
from binance_ai_trader.paper_backtest import (
    BacktestDecision,
    PaperBacktestRejected,
    PaperBacktestResult,
    run_paper_spot_backtest,
)
from binance_ai_trader.paper_spot import PaperCosts
from binance_ai_trader.risk_schemas import RiskSettings

D = Decimal


def settings(**changes: object) -> RiskSettings:
    return RiskSettings.model_validate(
        {
            "mode": "paper",
            "market_type": "spot",
            "spot_capital_usdt": 1000,
            "bot_enabled": True,
            "manual_approval": False,
            **changes,
        }
    )


def candles(count: int = 60, interval: str = "1m") -> list[Candle]:
    step = {"1m": 60_000, "5m": 300_000, "15m": 900_000}[interval]
    return [
        Candle(
            symbol="BTCUSDT",
            interval=interval,
            open_time_ms=index * step,
            close_time_ms=(index + 1) * step - 1,
            open=D("100"),
            high=D("112"),
            low=D("40"),
            close=D("105"),
            volume=D("2.000000000000000001"),
        )
        for index in range(count)
    ]


def clock(history: Sequence[Candle]) -> int:
    return history[-1].close_time_ms + 1


def buy(history: list[Candle], index: int = 49, quantity: str = "1") -> BacktestDecision:
    return BacktestDecision(
        observed_close_time_ms=history[index].close_time_ms,
        action="buy",
        quantity=D(quantity),
        stop_price=D("99"),
    )


def sell(history: list[Candle], index: int = 50) -> BacktestDecision:
    return BacktestDecision(observed_close_time_ms=history[index].close_time_ms, action="sell")


def run(
    history: Sequence[Candle],
    decisions: Sequence[BacktestDecision],
    *,
    costs: PaperCosts | None = None,
    warmup_candles: int = 50,
) -> PaperBacktestResult:
    return run_paper_spot_backtest(
        history,
        symbol="BTCUSDT",
        interval=history[0].interval,
        as_of_ms=clock(history),
        settings=settings(),
        decisions=decisions,
        costs=costs if costs is not None else PaperCosts(),
        warmup_candles=warmup_candles,
    )


def test_next_open_fill_and_costs_are_decimal_and_replay_is_deterministic() -> None:
    data = candles()
    # Decision comes after candle 49 CLOSE. Its fill is next candle 50 OPEN,
    # NEVER the 49th bar's favorable closing price.
    data[49] = replace(data[49], close=D("105"))
    data[50] = replace(data[50], open=D("100"), close=D("105"))
    data[51] = replace(data[51], open=D("110"), high=D("112"))
    plan = [buy(data), sell(data)]
    first = run(data, plan)
    second = run(tuple(data), tuple(plan))
    assert first == second
    assert len(first.fills) == 2
    entry, exit_event = first.fills
    assert entry.trade_id == exit_event.trade_id == "bt_49"
    assert entry.signal_close_time_ms == data[49].close_time_ms
    assert entry.fill_open_time_ms == data[50].open_time_ms
    assert entry.reference_open_price == D("100")
    assert entry.assumed_fill_price == D("100.0500")
    assert exit_event.fill_open_time_ms == data[51].open_time_ms
    assert exit_event.reference_open_price == D("110")
    assert exit_event.assumed_fill_price == D("109.9450")
    assert first.book.cash_usdt == D("1009.6850050")
    assert first.book.realized_pnl_usdt == D("9.6850050")
    assert first.book.paid_fees_usdt == D("0.2099950")
    assert first.closed_trades == 1
    assert first.open_positions == 0
    assert first.feed_quality.usable_for_paper_signals
    assert all(isinstance(fill.fee_usdt, Decimal) for fill in first.fills)
    assert data[50].open == D("100")  # input remains intact


def test_open_trade_never_pretends_to_have_realized_pnl_or_equity() -> None:
    data = candles()
    result = run(data, [buy(data)])
    assert result.open_positions == 1
    assert result.closed_trades == 0
    assert result.book.realized_pnl_usdt == D("0")
    assert result.book.cash_usdt == D("899.8499500")
    assert not hasattr(result, "unrealized_pnl_usdt")
    assert not hasattr(result, "equity_usdt")
    assert len(result.fills) == 1


def test_empty_decisions_are_reproducible_without_fabricated_orders() -> None:
    data = candles()
    report = run(data, [])
    assert report.fills == ()
    assert report.book.cash_usdt == D("1000")
    assert report.book.positions == ()
    assert report.book.closed == ()
    assert report.closed_trades == 0


@pytest.mark.parametrize(
    "bad_data",
    [
        [],
        candles(49),
        [*candles(50)[:49], replace(candles(50)[49], close_time_ms=50 * 60_000)],
        [*candles(50)[:49], replace(candles(50)[49], open_time_ms=49 * 60_000 + 1)],
        [*candles(50)[:49], candles(50)[48]],
        [*candles(50)[:49], replace(candles(50)[49], symbol="ETHUSDT")],
        [*candles(50)[:49], replace(candles(50)[49], high=D("NaN"))],
        [*candles(50)[:49], replace(candles(50)[49], open=D("200"))],
    ],
)
def test_rejects_empty_short_open_duplicate_misaligned_and_invalid_feed(
    bad_data: list[Candle],
) -> None:
    with pytest.raises(PaperBacktestRejected, match="feed_not_ready"):
        run_paper_spot_backtest(
            bad_data,
            symbol="BTCUSDT",
            interval="1m",
            as_of_ms=50 * 60_000,
            settings=settings(),
            decisions=[],
        )


def test_gap_stale_and_future_unclosed_candles_fail_closed() -> None:
    data = candles()
    gap = data[:]
    gap[50] = replace(
        gap[50],
        open_time_ms=gap[50].open_time_ms + 60_000,
        close_time_ms=gap[50].close_time_ms + 60_000,
    )
    with pytest.raises(PaperBacktestRejected, match="feed_not_ready"):
        run(gap, [])
    with pytest.raises(PaperBacktestRejected, match="feed_not_ready:stale"):
        run_paper_spot_backtest(
            data,
            symbol="BTCUSDT",
            interval="1m",
            as_of_ms=clock(data) + 600_000,
            settings=settings(),
            decisions=[],
        )
    with pytest.raises(PaperBacktestRejected, match="feed_not_ready:invalid"):
        run_paper_spot_backtest(
            data,
            symbol="BTCUSDT",
            interval="1m",
            as_of_ms=clock(data) - 1,
            settings=settings(),
            decisions=[],
        )


@pytest.mark.parametrize("status", [418, 429])
def test_upstream_rate_limit_never_becomes_a_backtest_signal(status: int) -> None:
    data = candles()
    with pytest.raises(PaperBacktestRejected, match="market_source_unavailable"):
        run_paper_spot_backtest(
            data,
            symbol="BTCUSDT",
            interval="1m",
            as_of_ms=clock(data),
            settings=settings(),
            decisions=[buy(data)],
            source_error=MarketRateLimited(status, retry_after_seconds=15),
        )


def test_no_same_bar_or_unobserved_late_signals_and_no_duplicate_schedule() -> None:
    data = candles()
    invalid = [
        [buy(data, 48)],  # before the 50-candle warmup
        [buy(data, 59)],  # signal at last close cannot fill
        [buy(data), buy(data)],  # duplicate close
        [sell(data), buy(data)],  # reversed schedule
        [
            BacktestDecision(
                observed_close_time_ms=12, action="buy", quantity=D("1"), stop_price=D("99")
            )
        ],
    ]
    for plan in invalid:
        with pytest.raises(PaperBacktestRejected):
            run(data, plan)


def test_closed_position_is_required_for_sell_and_no_second_open_without_marks() -> None:
    data = candles()
    with pytest.raises(PaperBacktestRejected, match="paper_position_not_open"):
        run(data, [sell(data)])
    with pytest.raises(PaperBacktestRejected, match="unmarked_open_position"):
        run(data, [buy(data), buy(data, 50)])


def test_manual_approval_is_never_silently_bypassed() -> None:
    data = candles()
    with pytest.raises(PaperBacktestRejected, match="manual_approval_required"):
        run_paper_spot_backtest(
            data,
            symbol="BTCUSDT",
            interval="1m",
            as_of_ms=clock(data),
            settings=settings(manual_approval=True),
            decisions=[buy(data)],
        )
    explicit = replace(buy(data), explicit_approval=True)
    result = run_paper_spot_backtest(
        data,
        symbol="BTCUSDT",
        interval="1m",
        as_of_ms=clock(data),
        settings=settings(manual_approval=True),
        decisions=[explicit],
    )
    assert result.open_positions == 1


def test_realized_loss_blocks_next_entry_and_no_partial_return() -> None:
    data = candles()
    data[51] = replace(data[51], open=D("50"))
    plan = [buy(data), sell(data), buy(data, 51)]
    with pytest.raises(PaperBacktestRejected, match="daily_loss_limit"):
        run(data, plan)


def test_cost_and_risk_limits_are_checked_before_opening() -> None:
    data = candles()
    with pytest.raises(PaperBacktestRejected, match="fee_slippage_adjusted_risk_limit"):
        run(data, [buy(data, quantity="2")], costs=PaperCosts(fee_bps=100, slippage_bps=0))
    for costs in [PaperCosts(fee_bps=-1), PaperCosts(slippage_bps=101)]:
        with pytest.raises(PaperBacktestRejected, match="invalid_simulation_costs"):
            run(data, [], costs=costs)
    with pytest.raises(PaperBacktestRejected, match="paper_bot_disabled"):
        run_paper_spot_backtest(
            data,
            symbol="BTCUSDT",
            interval="1m",
            as_of_ms=clock(data),
            settings=settings(bot_enabled=False),
            decisions=[buy(data)],
        )


def test_stop_and_quantity_must_be_valid_before_simulation() -> None:
    data = candles()
    for quantity in [D("NaN"), D("Infinity"), D("-1"), D("0")]:
        with pytest.raises(PaperBacktestRejected, match="invalid_paper_entry_decision"):
            run(data, [replace(buy(data), quantity=quantity)])
    with pytest.raises(PaperBacktestRejected, match="stop_not_below_entry"):
        run(data, [replace(buy(data), stop_price=D("100"))])
    with pytest.raises(PaperBacktestRejected, match="sell_must_not_specify_entry"):
        run(data, [replace(sell(data), quantity=D("1"))])


def test_different_utc_day_rejects_unmarked_overnight_position() -> None:
    data = candles(100, "15m")
    with pytest.raises(PaperBacktestRejected, match="overnight_mark_to_market"):
        run(data, [buy(data, index=94), sell(data, index=95)])


def test_futures_and_invalid_warmup_never_use_spot_backtest() -> None:
    data = candles()
    for warmup in [0, 1, 49, 1001, True]:
        with pytest.raises(PaperBacktestRejected, match="invalid_warmup"):
            run(data, [], warmup_candles=warmup)
    with pytest.raises(PaperBacktestRejected, match="spot_only"):
        run_paper_spot_backtest(
            data,
            symbol="BTCUSDT",
            interval="1m",
            as_of_ms=clock(data),
            settings=RiskSettings(market_type="futures", futures_capital_usdt=100, max_leverage=2),
            decisions=[],
        )
