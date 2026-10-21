# BT-006B — Atomic PAPER Spot signal settlement (retrospective, never live)

`paper_signal_execution.execute_paper_signal_once(...)` settles a **previously generated** latest-closed-candle PAPER `CANDIDATE`, using a **stored and fully closed following candle's OPEN** as a retrospective synthetic reference. This cannot be a live/open-price executable quote: the following candle must have closed so its validity can be checked. No actual exchange order, Binance credential, authenticated trading, worker, API endpoint, scheduler, production migration or change to `main` is performed.

## Two-phase contract

1. BT-006A read-only plan: a `PaperSignalPlan` with `status=CANDIDATE`, `decision`, `latest_close_time_ms`, `settings_version`, strategy/version/market identity and deterministic key, produced at previous candle N's close. BLOCKED and NO_SIGNAL cannot execute. This plan alone authorizes NOTHING.
2. Later, after following candle N+1 closes, caller supplies explicit trusted external `SourceState.HEALTHY` and fresh `source_checked_at_ms`. A non-healthy state (including documented 418/429 restrictions) rejects all new settlement. External source-health attestation is NOT independently verified by this service; a worker must verify it in its own future package. Historical `now_ms` injection is for isolated tests ONLY.
3. Service reads the real stored N+1 opening quote from PostgreSQL and calls existing `paper_execution.open_paper_spot` or `close_paper_spot`. Within the **same existing account/settings locked transaction**, `_signal_preflight` validates matching account settings/version/manual approval/bot flag, existing actual open position, complete `feed_quality.READY` market history, precise N/N+1 continuity, freshness and exactly the original SMA decision computed from the **prefix ending at N**. A changed price or source status FAILS CLOSED. Only after this verification are immutable journal and PAPER account/position writes made.
4. Existing `paper_execution.py` performs real transaction locking, ledger reconciliations, Decimal risk/cash/stop-distance checks, hypothetical fee/slippage and **DB UNIQUE** idempotency. An identical key and payload returns the same persisted journal receipt without a second write, including after a process restart. A conflicting reference/stop/quantity under the same key fails closed. New buy trade IDs are stable across settings-version changes. No partial ledger entry survives a failed transaction.
5. `signal-exec-<sha256>` idempotency key binds source signal, intended buy/sell action and settings version; a derived stable synthetic buy trade ID prevents the same historical BUY signal from opening a second position across settings-version changes. SELL binds the currently persisted open trade ID, never a guessed one. Only one open Spot position is accepted.

## Important restrictions

- There is **no forward-trading claim**. The synthetic N+1-open price is known only after N+1 closed. This retrospective model must not be used to promise attainable live fills or to represent a real-money bot.
- The ledger must already be initialized; user data, existing backups and current real accounts are untouched by the new test suite.
- The external `SourceState.HEALTHY` assertion is a required dependency from a future trusted market-feed component. This code never fetches Binance data and cannot prevent a dishonest external caller from lying about health. It has no network access.
- Custom user-defined strategy callbacks are NOT accepted; only the fixed built-in SMA parameters from BT-005B are reverified inside the lock.
- Closing an existing open PAPER position is allowed only when a correctly recomputed sell signal targets that existing position. No autonomous stop trigger or forced liquidation exists. Manual approval is not synthesized.
- The historical receipt is a simulated cash movement, not equity or unrealized PnL. Downside is not guaranteed by hypothetical stop prices. A later worker, public authentication, monitoring, production release or Futures simulation must be handled as separate features.

## Verification

`tests/test_paper_signal_execution.py` rejects incomplete/fabricated decisions and checks stable identity. The independent `trader_signal_exec_test` PostgreSQL database is created from scratch and migrated by GitHub CI. `tests/test_paper_signal_execution_postgres.py` checks no next-bar/same-bar fill, unfinished next bar, market-source restrictions, settings/manual approval mismatch, transactional concurrent single BUY, same-key replay/restart, next genuine SELL after crossover, journal uniqueness and unchanged balances after rejection.

All applicable Ruff, strict mypy, pytest, PostgreSQL, Docker Persistence and Full Stack Paper E2E jobs must PASS on the final PR HEAD before a `dev` merge. Strix active security scan and interactive MCP browser inspection are NOT RUN.
