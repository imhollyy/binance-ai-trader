"""The AI gate cannot bypass risk, and net PAPER fills deduct real costs."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from binance_ai_trader.ai_constitution import (
    AiSpotProposal, BotState, IndependentRiskPolicy, TrustedSpotEvidence,
)
from binance_ai_trader.ai_market_regime import TimeframeHistory
from binance_ai_trader.ai_paper_dry_run import simulate_reviewed_ai_spot
from binance_ai_trader.market_data import INTERVAL_MS, Candle
from binance_ai_trader.paper_risk import PaperRiskState
from binance_ai_trader.paper_spot import new_paper_spot_book
from binance_ai_trader.risk_schemas import RiskSettings

D = Decimal
NOW = 1_800_000_000_000
CALIBRATED = frozenset({"synthetic-calibrated"})


def feeds() -> tuple[TimeframeHistory, ...]:
    data: list[TimeframeHistory] = []
    for interval in ("1m", "5m", "15m"):
        step = INTERVAL_MS[interval]
        candles: list[Candle] = []
        final_open = ((NOW - 1) // step) * step
        for i in range(50):
            opened = final_open - (49 - i) * step
            close = D("100") + D(i) / 10
            candles.append(Candle(
                "BTCUSDT", interval, opened, opened + step - 1,
                close, close + D("0.1"), close - D("0.1"), close, D("10"),
            ))
        data.append(TimeframeHistory(interval, tuple(candles)))
    return tuple(data)


def settings() -> RiskSettings:
    return RiskSettings(
        mode="paper",
        market_type="spot",
        bot_enabled=True,
        manual_approval=False,
        spot_capital_usdt=1000,
    )


def policy(*, bot_state: BotState = BotState.RUNNING) -> IndependentRiskPolicy:
    return IndependentRiskPolicy(
        settings=settings(),
        account_state=PaperRiskState(D("1000"), D("1000"), D("1000"), D("0"), 0),
        bot_state=bot_state,
        settings_version=7,
        account_settings_version=7,
        calibrated_model_versions=CALIBRATED,
        min_net_edge_bps=D("10"),
        min_edge_to_cost_ratio=D("1.5"),
        max_market_age_ms=120_000,
    )


def evidence(**updates: object) -> TrustedSpotEvidence:
    return replace(TrustedSpotEvidence(
        symbol="BTCUSDT",
        interval="1m",
        last_closed_at_ms=NOW - 1,
        received_at_ms=NOW + 400,
        reference_price=D("104.9"),
        taker_fee_bps=D("10"),
        spread_bps=D("2"),
        slippage_bps=D("5"),
        critical_data_ok=True,
        exchange_constraints_ok=True,
    ), **updates)


def signal(action: str = "BUY", **updates: object) -> AiSpotProposal:
    data: dict[str, object] = {
        "schema_version": "1.0",
        "signal_id": "dry_run_signal_01" if action == "BUY" else "dry_run_exit_01",
        "model_version": "synthetic-calibrated",
        "market": "SPOT", "mode": "PAPER", "symbol": "BTCUSDT",
        "execution_timeframe": "1m",
        "observed_close_at_ms": NOW - 1,
        "created_at_ms": NOW + 1000,
        "valid_until_ms": NOW + 60_000,
        "decision": action,
        "market_regime": "UPTREND",
        "reason_codes": ["SYNTHETIC_TEST"],
        "probability": {"win": "0.6", "loss": "0.3", "flat": "0.1"},
        "expected_win_bps": "130", "expected_loss_bps": "20",
        "suggested_notional_usdt": "100", "stop_price": "103.9",
    }
    data.update(updates)
    return AiSpotProposal.model_validate(data)


def run(
    proposal: AiSpotProposal | None = None,
    *,
    p: IndependentRiskPolicy | None = None,
    e: TrustedSpotEvidence | None = None,
    histories: tuple[TimeframeHistory, ...] | None = None,
    book=None,
    trade_id: str | None = None,
):
    owner = p or policy()
    return simulate_reviewed_ai_spot(
        proposal or signal(),
        owner,
        e or evidence(),
        feeds() if histories is None else histories,
        new_paper_spot_book(owner.settings) if book is None else book,
        context_timeframe="5m",
        higher_timeframe="15m",
        now_ms=NOW + 2_000,
        trade_id_to_close=trade_id,
    )


def test_ai_buy_then_sell_books_net_realized_pnl_after_fee_slip_spread() -> None:
    first = run()
    assert first.simulated and first.event == "PAPER_BUY"
    assert first.verdict.proposal_eligible
    assert first.book.realized_pnl_usdt == 0
    assert first.book.paid_fees_usdt > 0
    assert len(first.book.positions) == 1
    quantity = first.book.positions[0].quantity
    assert quantity > 0
    assert first.book.positions[0].entry_notional_usdt <= D("100")

    closed = run(
        signal("SELL"), book=first.book, e=evidence(reference_price=D("110")),
        trade_id="dry_run_signal_01",
        # Closed candle source must still match the independently verified mark.
    )
    assert not closed.simulated
    assert closed.verdict.reason_code == "MTF_CONTEXT_MISMATCH"
    assert closed.book == first.book

    # A separate immutable CLOSE session at the same observed quote:
    same_quote_close = run(signal("SELL"), book=first.book, trade_id="dry_run_signal_01")
    assert same_quote_close.simulated
    assert same_quote_close.event == "PAPER_SELL"
    assert len(same_quote_close.book.positions) == 0
    assert same_quote_close.book.realized_pnl_usdt < 0  # costs defeat flat gross price
    assert same_quote_close.book.paid_fees_usdt > first.book.paid_fees_usdt
    assert same_quote_close.book.cash_usdt == (
        D("1000") + same_quote_close.book.realized_pnl_usdt
    )
    assert first.book.positions  # immutable original book


@pytest.mark.parametrize(
    ("bad_signal", "bad_policy", "bad_evidence", "expected"),
    [
        ({ "market_regime": "DOWNTREND" }, {}, {}, "MTF_CONTEXT_MISMATCH"),
        ({ "expected_win_bps": "1" }, {}, {}, "INSUFFICIENT_EXPECTED_NET_EDGE"),
        ({}, {"bot_state": BotState.PAUSED}, {}, "BOT_NOT_RUNNING"),
        ({}, {"calibrated_model_versions": frozenset()}, {}, "MODEL_NOT_CALIBRATED"),
        ({}, {"account_settings_version": 1}, {}, "USER_HARD_LIMIT"),
        ({}, {}, {"critical_data_ok": False}, "DATA_QUALITY_FAILURE"),
        ({}, {}, {"taker_fee_bps": D("10.5")}, "UNVERIFIED_TRANSACTION_COST"),
        ({}, {}, {"source": "model-provided"}, "DATA_QUALITY_FAILURE"),
    ],
)
def test_unsafe_ai_entries_never_change_book(
    bad_signal: dict[str, object],
    bad_policy: dict[str, object],
    bad_evidence: dict[str, object],
    expected: str,
) -> None:
    proposal = signal(**bad_signal)
    candidate_policy = replace(policy(), **bad_policy)
    input_book = new_paper_spot_book(candidate_policy.settings)
    outcome = run(
        proposal,
        p=candidate_policy,
        e=evidence(**bad_evidence),
        book=input_book,
    )
    assert not outcome.simulated
    assert outcome.book == input_book
    assert outcome.verdict.reason_code == expected


def test_missing_higher_timeframe_blocks_even_extreme_ev() -> None:
    candidate = signal(expected_win_bps="90000")
    result = run(candidate, histories=feeds()[:2])
    assert result.verdict.reason_code == "DATA_QUALITY_FAILURE"
    assert not result.simulated


@pytest.mark.parametrize("state", [BotState.PAUSED, BotState.EMERGENCY_FLATTEN])
def test_reduce_only_exit_allowed_while_new_entries_paused(state: BotState) -> None:
    opened = run()
    assert opened.simulated
    no_new = run(signal(signal_id="another_trade_0001"), p=policy(bot_state=state))
    assert not no_new.simulated
    done = run(
        signal("SELL"), p=policy(bot_state=state),
        book=opened.book, trade_id="dry_run_signal_01",
    )
    assert done.event == "PAPER_SELL"
    assert not done.book.positions


def test_stopped_exit_and_double_exit_are_rejected() -> None:
    opened = run()
    stopped = run(
        signal("SELL"), p=policy(bot_state=BotState.STOPPED),
        book=opened.book, trade_id="dry_run_signal_01",
    )
    assert stopped.verdict.reason_code == "BOT_NOT_RUNNING"
    closed = run(signal("SELL"), book=opened.book, trade_id="dry_run_signal_01")
    again = run(signal("SELL"), book=closed.book, trade_id="dry_run_signal_01")
    assert not again.simulated
    assert again.verdict.reason_code == "PAPER_POSITION_NOT_OPEN"
    assert again.book == closed.book


def test_same_buy_signal_cannot_double_enter() -> None:
    first = run()
    duplicate = run(book=first.book)
    assert duplicate.verdict.reason_code == "PAPER_EXPOSURE_OR_REPLAY"
    assert duplicate.book == first.book
