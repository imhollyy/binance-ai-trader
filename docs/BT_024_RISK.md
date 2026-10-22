# BT-024 persistent risk implementation

## BT-024

`risk_guard.py` is a pure deterministic gate. Its versioned 1–5 profile
requires explicit critical limits and never obtains them from the AI. Hard
user caps are always applied. Stale settings, daily loss, drawdown, loss
streak, position/capital/risk bounds, missing stops, or unverifiable costs
reject new risk. The audit wrapper synchronously hands each decision to an
independent recorder. PAUSED/STOPPED reject new risk and identify undefined
protection behavior; EMERGENCY_FLATTEN is not described as a successful close
without a tested closure engine. No order path is connected.

`ai_shadow_preflight.py` composes the strict typed prediction, freshness/hash
and explicit cost evidence checks with the independent RiskGuard. It returns
only a PAPER suitability result and calls an audit recorder. SELL is blocked
because there is no Spot exit engine. The current model output is
uncalibrated, so BUY cannot pass suitability even when the standalone risk
check passes. `risk_store.record_shadow_preflight` appends the result with an
explicit `order_intent_created: false` marker.

Risk profiles, account snapshots, bot state, reservation events and audit
records now have append-only/versioned PostgreSQL storage in migration 0015.
The integration tests target only the isolated `trader_ai_bt023_test` database.
Research/risk stores fail closed unless configured for local allowlisted
database names with explicit opt-in variables. Account
snapshots include an external source digest, but this change does not attest
that digest against the production PAPER ledger. A tested closure engine and
any PAPER execution path remain absent. The isolated research database itself
is not deployed or auto-created; operators must provision the separate local
allowlisted database and opt-in environment flags. Automatic scheduling stays
off.
