"""Disposable DB E2E: AI proposal, locked PAPER ledger, net cost and replay.

No external requests, credentials, LIVE/Futures or persistent user account.
CI creates a fresh localhost trader_ai_atomic_test database explicitly.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from decimal import Decimal
from threading import Barrier

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session

from binance_ai_trader.ai_constitution import AiSpotProposal, BotState, TrustedSpotEvidence
from binance_ai_trader.ai_paper_atomic_test import (
    IsolatedAiPolicy,
    execute_disposable_reviewed_ai_once,
)
from binance_ai_trader.market_data import INTERVAL_MS
from binance_ai_trader.models import (
    PaperSpotAccountRow,
    PaperSpotJournalRow,
    PaperSpotPositionRow,
    SpotCandleRow,
    TradingSettingsRow,
)
from binance_ai_trader.paper_execution import PaperExecutionRejected, PaperTradeReceipt
from binance_ai_trader.paper_ledger import ensure_paper_spot_account
from binance_ai_trader.risk_schemas import RiskSettings

D = Decimal
URL = os.environ.get("TEST_POSTGRES_URL")
ISOLATED = os.environ.get("AI_ATOMIC_TEST_ISOLATED") == "1"
VALID = bool(
    URL
    and make_url(URL).get_backend_name() == "postgresql"
    and make_url(URL).host in ("127.0.0.1", "localhost")
    and make_url(URL).database == "trader_ai_atomic_test"
)
pytestmark = pytest.mark.skipif(
    not (ISOLATED and VALID),
    reason="Requires dedicated disposable localhost trader_ai_atomic_test",
)


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    assert URL is not None and ISOLATED and VALID
    db = create_engine(URL, pool_pre_ping=True)
    with Session(db) as session:
        assert session.scalar(select(func.count()).select_from(PaperSpotAccountRow)) == 0
        assert session.scalar(select(func.count()).select_from(PaperSpotJournalRow)) == 0
        assert session.scalar(select(func.count()).select_from(SpotCandleRow)) == 0
    yield db
    db.dispose()


def _seed(db: Engine) -> int:
    now_ms = time.time_ns() // 1_000_000
    with Session(db) as session, session.begin():
        session.add(
            TradingSettingsRow(
                id=1,
                version=4,
                data=RiskSettings(
                    bot_enabled=True,
                    manual_approval=False,
                    spot_capital_usdt=1000,
                ).model_dump(mode="json"),
            )
        )
        for interval in ("1m", "5m", "15m"):
            step = INTERVAL_MS[interval]
            latest_open = (now_ms // step) * step - step
            for i in range(50):
                opened = latest_open - (49 - i) * step
                close = D("100") + D(i) / 10
                session.add(
                    SpotCandleRow(
                        symbol="BTCUSDT",
                        interval=interval,
                        open_time_ms=opened,
                        close_time_ms=opened + step - 1,
                        open=close,
                        high=close + D("0.1"),
                        low=close - D("0.1"),
                        close=close,
                        volume=D("10"),
                    )
                )
    with Session(db) as session:
        ensure_paper_spot_account(session)
    return now_ms


def _proposal(now_ms: int, action: str = "BUY") -> AiSpotProposal:
    last_close = (now_ms // 60_000) * 60_000 - 1
    return AiSpotProposal.model_validate(
        {
            "schema_version": "1.0",
            "signal_id": "isolated_buy_0001" if action == "BUY" else "isolated_sell_0001",
            "model_version": "synthetic-test-only",
            "market": "SPOT",
            "mode": "PAPER",
            "symbol": "BTCUSDT",
            "execution_timeframe": "1m",
            "observed_close_at_ms": last_close,
            "created_at_ms": now_ms,
            "valid_until_ms": now_ms + 90_000,
            "decision": action,
            "market_regime": "UPTREND",
            "reason_codes": ["SYNTHETIC_TEST"],
            "probability": {"win": "0.6", "loss": "0.3", "flat": "0.1"},
            "expected_win_bps": "130",
            "expected_loss_bps": "20",
            "suggested_notional_usdt": "100",
            "stop_price": "103.9",
        }
    )


def _evidence(now_ms: int) -> TrustedSpotEvidence:
    return TrustedSpotEvidence(
        symbol="BTCUSDT",
        interval="1m",
        last_closed_at_ms=(now_ms // 60_000) * 60_000 - 1,
        received_at_ms=now_ms - 1,
        reference_price=D("104.9"),
        taker_fee_bps=D("10"),
        spread_bps=D("2"),
        slippage_bps=D("5"),
        critical_data_ok=True,
        exchange_constraints_ok=True,
    )


def _policy() -> IsolatedAiPolicy:
    return IsolatedAiPolicy(
        expected_settings_version=4,
        bot_state=BotState.RUNNING,
        calibrated_model_versions=frozenset({"synthetic-test-only"}),
        min_net_edge_bps=D("10"),
        min_edge_to_cost_ratio=D("1.5"),
        max_market_age_ms=120_000,
    )


def _execute(
    db: Engine,
    signal: AiSpotProposal,
    sample: TrustedSpotEvidence,
    rules: IsolatedAiPolicy,
    *,
    closing: str | None = None,
) -> PaperTradeReceipt:
    return execute_disposable_reviewed_ai_once(
        db,
        signal,
        sample,
        rules,
        context_timeframe="5m",
        higher_timeframe="15m",
        trade_id_to_close=closing,
    )


def _counts(db: Engine) -> tuple[int, int]:
    with Session(db) as session:
        return (
            session.scalar(select(func.count()).select_from(PaperSpotPositionRow)) or 0,
            session.scalar(select(func.count()).select_from(PaperSpotJournalRow)) or 0,
        )


def test_isolated_locked_ai_buy_sell_replay_risk_and_net_pnl(engine: Engine) -> None:
    clock = _seed(engine)
    proposal = _proposal(clock)
    sample = _evidence(clock)
    rules = _policy()
    assert _counts(engine) == (0, 1)

    for invalid, context, expected in (
        (proposal, replace(rules, calibrated_model_versions=frozenset()), "MODEL_NOT_CALIBRATED"),
        (proposal, replace(rules, bot_state=BotState.PAUSED), "BOT_NOT_RUNNING"),
        (
            proposal,
            replace(rules, expected_settings_version=2),
            "USER_HARD_LIMIT_OR_SETTINGS_VERSION",
        ),
        (proposal, replace(rules, min_net_edge_bps=D("200")), "INSUFFICIENT_EXPECTED_NET_EDGE"),
        (
            proposal.model_copy(update={"market_regime": "DOWNTREND"}),
            rules,
            "MTF_CONTEXT_MISMATCH",
        ),
    ):
        with pytest.raises(PaperExecutionRejected, match="ai_gate:" + expected):
            _execute(engine, invalid, sample, context)
    with pytest.raises(PaperExecutionRejected, match="ai_gate:UNVERIFIED_TRANSACTION_COST"):
        _execute(engine, proposal, replace(sample, taker_fee_bps=D("10.1")), rules)
    with pytest.raises(PaperExecutionRejected, match="ai_gate:DATA_OR_POLICY_UNVERIFIED"):
        _execute(engine, proposal, replace(sample, critical_data_ok=False), rules)
    assert _counts(engine) == (0, 1)

    barrier = Barrier(3)

    def parallel_buy(_: int) -> PaperTradeReceipt:
        barrier.wait(timeout=15)
        return _execute(engine, proposal, sample, rules)

    with ThreadPoolExecutor(max_workers=3) as pool:
        purchases = list(pool.map(parallel_buy, range(3)))
    assert purchases[0] == purchases[1] == purchases[2]
    bought = purchases[0]
    assert bought.event_type == "spot_buy"
    assert bought.cash_after_usdt < D("1000")
    assert bought.fee_usdt > 0
    assert _counts(engine) == (1, 2)
    with Session(engine) as session:
        opened = session.scalar(select(PaperSpotPositionRow))
        assert opened is not None
        assert opened.status == "open"
        assert opened.entry_notional_usdt <= D("100")
        assert opened.quantity > 0

    assert _execute(engine, proposal, sample, rules) == bought
    with pytest.raises(PaperExecutionRejected, match="paper_idempotency_conflict"):
        _execute(engine, proposal, replace(sample, spread_bps=D("6")), rules)
    assert _counts(engine) == (1, 2)

    sell = _proposal(clock, "SELL")
    with pytest.raises(PaperExecutionRejected, match="ai_gate:BOT_NOT_RUNNING"):
        _execute(
            engine,
            sell,
            sample,
            replace(rules, bot_state=BotState.STOPPED),
            closing=bought.trade_id,
        )
    with pytest.raises(PaperExecutionRejected, match="ai_gate:POSITION_NOT_OPEN_OR_MISMATCH"):
        _execute(engine, sell, sample, rules, closing="not_a_position")
    with pytest.raises(PaperExecutionRejected, match="ai_gate:DATA_OR_POLICY_UNVERIFIED"):
        _execute(
            engine,
            sell,
            replace(sample, source="model_claimed_verified"),
            rules,
            closing=bought.trade_id,
        )

    sold = _execute(
        engine,
        sell,
        sample,
        replace(rules, bot_state=BotState.PAUSED),
        closing=bought.trade_id,
    )
    assert sold.event_type == "spot_sell"
    assert sold.realized_pnl_delta_usdt < 0  # flat gross mark, net negative after costs
    assert sold.cash_after_usdt == D("1000") + sold.realized_pnl_delta_usdt
    assert _counts(engine) == (1, 3)
    with Session(engine) as session:
        position = session.scalar(select(PaperSpotPositionRow))
        account = session.get(PaperSpotAccountRow, 1)
        assert position is not None and account is not None
        assert position.status == "closed"
        assert account.paid_fees_usdt == position.entry_fee_usdt + position.exit_fee_usdt
        assert account.realized_pnl_usdt == position.realized_pnl_usdt
        assert account.cash_usdt == sold.cash_after_usdt
    assert (
        _execute(
            engine,
            sell,
            sample,
            replace(rules, bot_state=BotState.PAUSED),
            closing=bought.trade_id,
        )
        == sold
    )

    with pytest.raises(PaperExecutionRejected, match="ai_gate:POSITION_NOT_OPEN_OR_MISMATCH"):
        _execute(
            engine,
            _proposal(clock, "SELL").model_copy(update={"signal_id": "isolated_sell_0002"}),
            sample,
            rules,
            closing=bought.trade_id,
        )
    assert _counts(engine) == (1, 3)


def test_atomic_ai_adapter_denies_non_disposable_targets() -> None:
    other_db = create_engine("postgresql+psycopg://localhost/trader_live", pool_pre_ping=True)
    try:
        with pytest.raises(PaperExecutionRejected, match="disposable_ai_postgres_only"):
            execute_disposable_reviewed_ai_once(
                other_db,
                _proposal(time.time_ns() // 1_000_000),
                _evidence(time.time_ns() // 1_000_000),
                _policy(),
                context_timeframe="5m",
                higher_timeframe="15m",
            )
    finally:
        other_db.dispose()
