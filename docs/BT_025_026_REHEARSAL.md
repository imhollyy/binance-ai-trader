# BT-025 / BT-026 isolated PAPER rehearsal

## BT-025: execution boundary

The existing `paper_execution` transaction remains the sole ledger writer. It
locks settings and the single PAPER account, rebuilds account risk from the
persisted journal/positions, reconciles the projection, and applies Decimal
fees/slippage before recording a BUY or reduce-only SELL. Signal and protective
helpers recompute their data, state, version, position and price checks inside
that transaction. PostgreSQL uniqueness and transaction rollback cover replay,
duplicate keys, conflicting payloads, concurrent workers and failed audit
writes.

`ai_paper_atomic_test.py` is an integration-test adapter, not a product or
worker entry point. It refuses any database other than the dedicated local
`trader_ai_atomic_test` database with its explicit isolation flag. Its
proposal, evidence and calibration allowlist are test fixtures; they are not
real model approval, exchange attestations, or executable quotes. Production
research predictions remain disconnected from that adapter. The independent
shadow preflight now requires an explicit externally configured approved model
version. No current model is on such an allowlist, its probabilities remain
uncalibrated, and no order intent is produced by the shadow preflight.

Public observations and following-candle references are hypothetical PAPER
inputs. The worker uses public unauthenticated market data only, stops on
418/429 without retrying around the restriction, and the scheduler remains
opt-in/off by default. No Binance private client, live order, Futures path, or
user PAPER automation is part of this rehearsal.

Protective logic is explicit and independently authorized. PAUSED/STOPPED do
not imply that a position is protected; STOPPED reports open exposure, and
PAUSED may only request a specifically confirmed reduce-only review. The
protective test harness needs a mocked observation and an explicit test exit
approval. Its simulated sell uses the observed quote with adverse slippage and
fees, so a gap below the stop produces a worse simulated fill. Neither the
trigger nor the recorded fill guarantees a stop price. EMERGENCY_FLATTEN is not
reported successful until the isolated settlement actually records a close.

## BT-026: bounded endurance evidence

`tests/test_bt026_paper_resilience.py` runs 1,000 deterministic offline
round-trips against the immutable in-memory Decimal ledger. It checks every
cycle returns to flat, exact cash-to-realized-PnL reconciliation, cost
deduction, separate gross fill-price and net fee-adjusted PnL, fee totals,
closed-trade count, equity path and drawdown. The ledger charges explicit
assumed spread on both sides, as well as fee and slippage; new quote audit rows
store these assumptions separately. Historical rows from before migration
0017 retain `NULL` spread because their value was not recorded. A separate
stress case increases hypothetical fees, spread and slippage and verifies
lower net PnL. These costs are test assumptions, not observed fills or account
rates.

Existing PAPER backtest and transaction tests cover gross/reference versus
costed fills, realized net P&L, open exposure/cash, daily-loss and drawdown
gates, duplicate requests, concurrent workers, audit rollback, process/engine
restart, stale settings, missing/stale candles, market gaps and rate limits.
The historical backtest explicitly rejects unsupported overnight open
positions; it does not claim a 24/7 soak. Real database restart evidence must
come from the separately created disposable PostgreSQL container and owned
volume documented in the task report. Application engine recreation alone is
not physical PostgreSQL restart evidence.

## Scope and release gates

No migration starts a scheduler. AI artifacts stay
`RESEARCH_ONLY_NOT_APPROVED`; calibration, profitability, realized Binance
fills and user account suitability are not established. This branch supplies
an isolated test rehearsal only. Connecting any PAPER execution path to an
actual account remains blocked pending independent model approval, security
review, account-specific limits, backup/restore acceptance, monitoring, and
BT-025 acceptance in a separately reviewed change. Merging this branch does
not authorize activation.
