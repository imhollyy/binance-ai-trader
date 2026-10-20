# BT-005A — Deterministic historical PAPER Spot replay

Pure function: `paper_backtest.run_paper_spot_backtest(...)`

This is an **offline, hypothetical, historical Spot long-only calculation**.
No database reads/writes, existing PAPER account mutation, HTTP endpoint,
worker, Binance authenticated connection, API key, real order, Futures
execution, scheduling or AI strategy is involved.

## Input contract and chronology

- Up to 1,000 ordered `market_data.Candle` records, all for one supported
  `symbol` and `interval` (BTCUSDT/ETHUSDT; 1m/5m/15m).
- Caller's `as_of_ms` is an **explicit historical replay clock** (not the
  wall clock or proof of live freshness). `evaluate_closed_spot_feed` must
  return **READY**: at least 50 fully **closed**, contiguous candles;
  invalid, missing, duplicated, unfinished or stale history fails closed.
  Ingestion errors, including Binance public API **418/429**, must be passed
  as `source_error` and cause rejection. The backtest makes **zero HTTP
  requests**, has no retry loop and cannot independently verify provenance.
- Only externally supplied `BacktestDecision` events are replayed, at
  exact historical candle **close** times. A decision must have a 50-candle
  warmup and at least **one next candle** available. Decisions are strictly
  increasing and unique; at most 100. Signals on the last candle are
  rejected, rather than given imaginary fills.
- A decision at the end of candle **N** hypothetically fills using only the
  **OPEN of candle N+1** with explicit adverse slippage and commission,
  never at the same bar's favorable close. The decision model itself
  receives **no future data from this engine**. An externally supplied
  decision plan can still be contaminated with future knowledge; this
  library does **not** prove a plan/strategy was generated causally.
  A later strategy adapter must only supply prior closed candles.
- Entry: `action="buy"`, positive Decimal `quantity`, positive Decimal
  `stop_price` below next opening reference, unique synthetic trade ID.
  Exit: `action="sell"`, no entry parameters; closes **100%** of the
  current paper long. No auto-stop, trailing stop, target fill or forced
  liquidation at the end of the series is modeled.
- At most one open position, **no overnight position** crossing a UTC
  boundary: reliable mark-to-market/daily equity for overnight positions
  isn't implemented. Risk settings require paper-only Spot mode; disabled
  bot and required manual approval fail closed unless the synthetic entry
  explicitly records approval. Daily loss and peak drawdown use only
  realized, flat cash before a new opening.

## Example

```python
from decimal import Decimal
from binance_ai_trader.paper_backtest import BacktestDecision, run_paper_spot_backtest
from binance_ai_trader.risk_schemas import RiskSettings

# candles: caller-provided, verified, ordered, closed historical Candle objects.
report = run_paper_spot_backtest(
    candles,
    symbol="BTCUSDT",
    interval="1m",
    as_of_ms=candles[-1].close_time_ms + 1,
    settings=RiskSettings(bot_enabled=True, manual_approval=False),
    decisions=[
        BacktestDecision(
            observed_close_time_ms=candles[49].close_time_ms,
            action="buy",
            quantity=Decimal("1"),
            stop_price=Decimal("99"),
        ),
        BacktestDecision(
            observed_close_time_ms=candles[50].close_time_ms,
            action="sell",
        ),
    ],
)
```

`report.fills` contains deterministic synthetic next-open fills with
their explicit costs and decision/fill timestamps. `report.book` is an
**immutable in-memory ledger**, not the persistent PAPER account.
`book.realized_pnl_usdt`, `book.paid_fees_usdt` and `book.cash_usdt`
are hypothetical; with an open position **cash is not equity**. No
unrealized PnL or performance return is fabricated. Stop losses are
**not guaranteed fills**, and historical results do not predict profits.

## Verification and scope

`tests/test_paper_backtest.py` covers next-bar execution, replay
determinism, finite Decimal values, commissions, settings risk/approval,
realized loss, corrupted or stale history, 418/429, duplicate/unfillable
signals, overnight fail-closed behavior and empty plans. CI runs Ruff,
mypy and pytest. This pure package creates **no Alembic migration**,
no strategy/UI API and has no right to modify user data.

Separate later packages must cover a causally generated strategy,
strategy validation, optional backtest presentation and an independently
reviewed paper worker. This does **not** authorize a real trading engine.
