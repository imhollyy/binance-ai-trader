"""BT-026 bounded deterministic offline PAPER ledger endurance rehearsal."""

from decimal import Decimal

from binance_ai_trader.paper_risk import PaperRiskState
from binance_ai_trader.paper_spot import (
    PaperCosts,
    SpotBuyIntent,
    new_paper_spot_book,
    simulate_spot_buy,
    simulate_spot_sell,
)
from binance_ai_trader.risk_schemas import RiskSettings

D = Decimal


def test_one_thousand_costed_cycles_are_deterministic_and_reconciled() -> None:
    settings = RiskSettings(
        mode="paper",
        market_type="spot",
        bot_enabled=True,
        manual_approval=False,
        spot_capital_usdt=1000,
    )
    costs = PaperCosts(fee_bps=1, slippage_bps=1)
    book = new_paper_spot_book(settings)
    equity_path = [book.cash_usdt]
    day_start_equity = book.cash_usdt

    for index in range(1_000):
        state = PaperRiskState(
            equity_usdt=book.cash_usdt,
            day_start_equity_usdt=day_start_equity,
            peak_equity_usdt=max(equity_path),
            reserved_capital_usdt=D("0"),
            open_positions=0,
        )
        book = simulate_spot_buy(
            book,
            settings,
            state,
            SpotBuyIntent(f"cycle_{index}", "BTCUSDT", D("100"), D("0.01"), D("99.99")),
            costs=costs,
        )
        assert len(book.positions) == 1
        book = simulate_spot_sell(book, f"cycle_{index}", D("100"), costs=costs)
        assert not book.positions
        assert len(book.closed) == index + 1
        assert book.cash_usdt == book.initial_capital_usdt + book.realized_pnl_usdt
        assert book.closed[-1].realized_pnl_usdt < 0  # fees and adverse slippage included
        assert book.closed[-1].gross_realized_pnl_usdt == (
            book.closed[-1].realized_pnl_usdt
            + book.closed[-1].entry_fee_usdt
            + book.closed[-1].exit_fee_usdt
        )
        equity_path.append(book.cash_usdt)

    assert len(book.closed) == 1_000
    assert book.realized_pnl_usdt == sum((trade.realized_pnl_usdt for trade in book.closed), D("0"))
    assert book.paid_fees_usdt == sum((trade.exit_fee_usdt for trade in book.closed), D("0")) + sum(
        (D("0.00010001") for _ in book.closed), D("0")
    )
    assert equity_path[-1] < equity_path[0]
    peak = equity_path[0]
    maximum_drawdown = D("0")
    for equity in equity_path[1:]:
        peak = max(peak, equity)
        maximum_drawdown = max(maximum_drawdown, (peak - equity) / peak)
    assert maximum_drawdown > 0


def test_higher_hypothetical_cost_stress_reduces_net_result() -> None:
    settings = RiskSettings(
        mode="paper",
        market_type="spot",
        bot_enabled=True,
        manual_approval=False,
        spot_capital_usdt=1000,
    )

    def round_trip(costs: PaperCosts) -> Decimal:
        book = new_paper_spot_book(settings)
        state = PaperRiskState(D("1000"), D("1000"), D("1000"), D("0"), 0)
        book = simulate_spot_buy(
            book,
            settings,
            state,
            SpotBuyIntent("stress", "BTCUSDT", D("100"), D("1"), D("99")),
            costs=costs,
        )
        return simulate_spot_sell(book, "stress", D("101"), costs=costs).realized_pnl_usdt

    base = round_trip(PaperCosts(fee_bps=10, slippage_bps=5, spread_bps=2))
    stressed = round_trip(PaperCosts(fee_bps=20, slippage_bps=10, spread_bps=4))
    assert stressed < base
