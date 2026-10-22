# BT-027 acceptance status

This branch exposes only verified PAPER records already stored in the application
database. Unknown values remain absent; the dashboard does not infer prices,
unrealized P&L, spread costs, or a live worker state.

## Risk profiles (BT-027A)

`GET /api/v1/risk/profiles/capabilities` is authenticated and returns an explicit
unavailable response. The BT-024 RiskGuard uses a separate isolated research
store and has no application-facing, user-approved profile write contract. The
dashboard therefore keeps levels 1–5 unselectable and does not write PAPER
settings as a substitute. Enabling selection requires a separate versioned
profile service with user approval, optimistic concurrency, immutable audit,
and fail-closed integration with RiskGuard.

## Worker control (BT-027B)

The dashboard shows persisted last-attempt metadata only. It does not treat that
metadata as a heartbeat. No worker identity/lease supervisor or verified
heartbeat API is integrated, so start and stop controls remain disabled and
`process_verified_running` remains false. Enabling controls requires a
restart-safe lease/heartbeat contract, authorized commands, audit, and
concurrency tests.

## AI research API (BT-027C)

Model artifacts and prospective receipts are kept in a dedicated isolated
research database. The application API has no configured, read-only connection
to that database; the application PAPER database is not used as a fallback.
The dashboard accurately labels models `RESEARCH ONLY — NOT APPROVED` and
shows no fabricated predictions or metrics. A future endpoint must use an
explicitly configured read-only research connection, preserve receipt/outcome
provenance, and expose no execution capability.

## Acceptance boundary

The dashboard's stored PAPER ledger views, closed-candle history, cost evidence,
responsive UI, and fail-closed empty states are implemented. BT-027A profile
selection, BT-027B worker controls, and BT-027C research records are deliberately
not enabled because their required backend contracts are absent. This PR is not
ready to merge until those acceptance items and required CI checks are complete.
