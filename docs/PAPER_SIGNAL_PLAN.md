# BT-006A — PostgreSQL READ ONLY PAPER Spot signal planning

`paper_signal_plan.plan_paper_spot_signal_once()` is a synchronous, one-time, NOT EXECUTABLE market decision diagnostic. It has no scheduled worker, internet access, private Binance client, order routes, trade endpoint, persistent idempotency claim, or mutations to the user PAPER ledger.

## Contract

- Supports BTCUSDT/ETHUSDT and 1m/5m/15m, with 50–1000 latest stored Spot candles from PostgreSQL, one REPEATABLE READ READ ONLY transaction. Read account/settings/positions/journal and candles consistently; never bootstrap, update, or reset an account.
- Requires caller's SourceState.HEALTHY and source_checked_at_ms not older than the candle interval; unknown, unavailable or rate-limited 418/429 source states BLOCK. MarketDataError propagated into source_error also BLOCKS. This is **an explicit external attestation**, not proof that Binance was queried: the caller remains responsible for verifying current source health. Never synthesize prices.
- Normal calls use actual UTC epoch milliseconds. now_ms can be injected solely for deterministic tests; injecting past timestamps into a deployed planner would falsely make old data appear actionable.
- The entire selected stored history must pass feed_quality READY. An open, malformed, missing, duplicate, stale, unordered or wrong-interval candle blocks decision formation. Unlike historical backtesting, a candidate can refer only to the most recently completed candle, with its close no more than one interval old. The source-check time must be after the candle closed.
- The audited SmaCrossStrategy is called on the whole immutable prefix ending at the latest fully closed candle, with position_open from the currently persisted, journal-reconciled account state. No old historical decisions are converted into current trade instructions.
- Results: BLOCKED with specific reason; NO_SIGNAL when no current SMA crossing; or CANDIDATE (`candidate_not_executable`) with a Decimal hypothetical BacktestDecision and a strategy-versioned deterministic SHA256 key. The key includes symbol, interval, last candle close, fixed SMA parameters, and buy/sell action. Same inputs/replays/concurrent reads produce the same key; **nothing reserves the key**. Atomic exactly-once protection requires a future unique database constraint in BT-006B.
- The current PAPER account must exist with matching persisted PAPER Spot settings, verified journal, at most one open position, and no contradictory other-symbol exposure. The planner checks bot enabled, manual-approval policy (a model never grants approval), approximate close-reference fee/slippage, stop risk, available cash, realized daily loss and drawdown before BUY. SELL can only be diagnosed for the existing same-symbol open position. A candidate does NOT guarantee an execution next open, fill, realized return or acceptable price. All requirements must be rechecked atomically in a later execution package.
- To bound a potentially large/corrupted ledger, more than 1000 position rows or 2000 journal rows BLOCK. This is conservative until a production-grade incremental reconciliation strategy exists. A read-only DB error also BLOCKS. There is no HTTP endpoint for the planner.

## Typical internal call

```python
from binance_ai_trader.paper_signal_plan import (
    SourceState, plan_paper_spot_signal_once,
)

# The trusted caller must independently verify public market-feed health
# and supply the true source observation clock:
plan = plan_paper_spot_signal_once(
    engine, symbol='BTCUSDT', interval='1m',
    source_state=SourceState.HEALTHY,
    source_checked_at_ms=verified_public_feed_timestamp_ms,
)
# plan.status is BLOCKED, NO_SIGNAL, or CANDIDATE; never an executed order.
```

## Verification / non-goals

`tests/test_paper_signal_plan.py` tests source restriction, typed input rejection and deterministic keys without DB. `tests/test_paper_signal_plan_postgres.py` runs ONLY against disposable localhost `trader_signal_test` migrated via CI. It checks account missing, empty/gapped/unclosed/stale history, same-id concurrent replay, manual approval, read-only ledger counts, persistent reconciliation failures, duplicate-candle uniqueness and matching open-position sell. No real customer data is touched.

Source-state trust, refresh/ingestion orchestration, durable idempotency and real execution are NOT implemented. The next BT-006B must use transaction-safe PAPER-only atomic reconciliation, re-read/revalidate the signal and enforce uniqueness; real exchange orders remain prohibited.
