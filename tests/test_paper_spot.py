"""Pure paper Spot accounting tests: no API keys, no DB and no HTTP."""

from __future__ import annotations

from decimal import Decimal

import pytest

from binance_ai_trader.paper_risk import PaperRiskState
from binance_ai_trader.paper_spot import (
    PaperCosts,
    PaperSpotRejected,
    SpotBuyIntent,
    new_paper_spot_book,
    simulate_spot_buy,
    simulate_spot_sell,
)
from binance_ai_trader.risk_schemas import RiskSettings

D = Decimal


def config(**values: object) -> RiskSettings:
    return RiskSettings.model_validate(
        {
            "market_type": "spot",
            "spot_capital_usdt": 1000,
            "bot_enabled": True,
            "manual_approval": False,
            **values,
        }
    )


def risk_state(reserved: str = "0", positions: int = 0, equity: str = "1000") -> PaperRiskState:
    return PaperRiskState(
        equity_usdt=D(equity),
        day_start_equity_usdt=D("1000"),
        peak_equity_usdt=D("1000"),
        reserved_capital_usdt=D(reserved),
        open_positions=positions,
    )


def intent(trade_id: str = "trade_1", quantity: str = "1") -> SpotBuyIntent:
    return SpotBuyIntent(trade_id, "BTCUSDT", D("100"), D(quantity), D("99"))


def test_immutable_paper_open_close_with_taker_fee_slippage_and_pnl() -> None:
    settings = config()
    initial = new_paper_spot_book(settings)
    opened = simulate_spot_buy(initial, settings, risk_state(), intent())
    assert initial.cash_usdt == D("1000")
    assert initial.positions == ()
    position = opened.positions[0]
    assert position.entry_fill_price == D("100.0500")
    assert position.entry_notional_usdt == D("100.0500")
    assert position.entry_fee_usdt == D("0.1000500")
    assert opened.cash_usdt == D("899.8499500")
    assert position.estimated_stop_loss_usdt > D("1")
    assert opened.realized_pnl_usdt == 0

    closed = simulate_spot_sell(opened, "trade_1", D("110"))
    assert len(closed.positions) == 0
    assert len(closed.closed) == 1
    assert opened.closed == ()
    assert closed.closed[0].exit_fill_price == D("109.9450")
    assert closed.realized_pnl_usdt == D("9.6850050")
    assert closed.cash_usdt == D("1009.6850050")
    assert closed.paid_fees_usdt == D("0.2099950")
    assert initial.cash_usdt == D("1000")


def test_defaults_fail_closed_for_disabled_bot() -> None:
    settings = RiskSettings()
    initial = new_paper_spot_book(settings)
    with pytest.raises(PaperSpotRejected, match="paper_bot_disabled"):
        simulate_spot_buy(initial, settings, risk_state(), intent())


def test_approval_is_not_automatic() -> None:
    settings = config(manual_approval=True)
    initial = new_paper_spot_book(settings)
    with pytest.raises(PaperSpotRejected, match="manual_approval_required"):
        simulate_spot_buy(initial, settings, risk_state(), intent())
    approved = simulate_spot_buy(
        initial, settings, risk_state(), intent(), explicit_approval=True
    )
    assert len(approved.positions) == 1


def test_duplicate_open_and_close_are_rejected() -> None:
    settings = config()
    initial = new_paper_spot_book(settings)
    opened = simulate_spot_buy(initial, settings, risk_state(), intent())
    with pytest.raises(PaperSpotRejected, match="duplicate_paper_trade_id"):
        simulate_spot_buy(
            opened, settings, risk_state(reserved="100.0500", positions=1), intent()
        )
    closed = simulate_spot_sell(opened, "trade_1", D("110"))
    with pytest.raises(PaperSpotRejected, match="paper_position_not_open"):
        simulate_spot_sell(closed, "trade_1", D("110"))
    with pytest.raises(PaperSpotRejected, match="duplicate_paper_trade_id"):
        simulate_spot_buy(closed, settings, risk_state(equity="1009.6850050"), intent())


def test_stale_risk_snapshot_rejected() -> None:
    settings = config()
    opened = simulate_spot_buy(
        new_paper_spot_book(settings), settings, risk_state(), intent()
    )
    with pytest.raises(PaperSpotRejected, match="risk_snapshot_mismatch"):
        simulate_spot_buy(opened, settings, risk_state(), intent("trade_2"))


def test_fee_and_slippage_can_exceed_original_risk_estimate() -> None:
    settings = config()
    with pytest.raises(PaperSpotRejected, match="fee_slippage_adjusted_risk_limit"):
        simulate_spot_buy(
            new_paper_spot_book(settings),
            settings,
            risk_state(),
            intent(quantity="2"),
            costs=PaperCosts(fee_bps=100, slippage_bps=0),
        )


def test_cash_spend_includes_fees() -> None:
    settings = config(risk_per_trade_pct=2)
    with pytest.raises(PaperSpotRejected):
        simulate_spot_buy(
            new_paper_spot_book(settings),
            settings,
            risk_state(),
            intent(quantity="10"),
            costs=PaperCosts(fee_bps=100, slippage_bps=0),
        )


@pytest.mark.parametrize(
    ("reference", "quantity", "stop"),
    [
        ("0", "1", "99"),
        ("NaN", "1", "99"),
        ("100", "Infinity", "99"),
        ("100", "1", "100"),
        ("100", "1", "-1"),
    ],
)
def test_invalid_inputs_rejected(reference: str, quantity: str, stop: str) -> None:
    settings = config()
    buy = SpotBuyIntent("trade_1", "BTCUSDT", D(reference), D(quantity), D(stop))
    with pytest.raises(PaperSpotRejected):
        simulate_spot_buy(new_paper_spot_book(settings), settings, risk_state(), buy)


def test_futures_never_routes_to_spot_ledger() -> None:
    settings = RiskSettings(
        market_type="futures", futures_capital_usdt=100, max_leverage=3, bot_enabled=True
    )
    with pytest.raises(PaperSpotRejected, match="spot_paper_only"):
        new_paper_spot_book(settings)


def test_unapproved_symbol_rejected() -> None:
    settings = config(allowed_symbols=["ETHUSDT"])
    with pytest.raises(PaperSpotRejected, match="paper_symbol_not_allowed"):
        simulate_spot_buy(new_paper_spot_book(settings), settings, risk_state(), intent())


@pytest.mark.parametrize(
    "costs", [PaperCosts(fee_bps=-1), PaperCosts(slippage_bps=101)]
)
def test_invalid_cost_assumptions_rejected(costs: PaperCosts) -> None:
    settings = config()
    with pytest.raises(PaperSpotRejected, match="invalid_simulation_costs"):
        simulate_spot_buy(
            new_paper_spot_book(settings), settings, risk_state(), intent(), costs=costs
        )


def test_negative_or_nan_exit_price_rejected_without_mutation() -> None:
    settings = config()
    book = simulate_spot_buy(
        new_paper_spot_book(settings), settings, risk_state(), intent()
    )
    for value in (D("-2"), D("NaN"), D("Infinity")):
        with pytest.raises(PaperSpotRejected, match="invalid_exit_reference_price"):
            simulate_spot_sell(book, "trade_1", value)
    assert len(book.positions) == 1
