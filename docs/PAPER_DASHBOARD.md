# BT-008A — PAPER Spot dashboard (READ ONLY, LOCAL ONLY)

This feature connects the existing TradePilot Next.js dashboard to a **single stored PostgreSQL snapshot** via `GET /api/v1/paper/dashboard?limit=10`. It has the **same local `X-Admin-Token` security requirement, localhost TrustedHost restrictions and CORS policy** as the other development APIs. It is NOT a safe public-internet authentication mechanism. Cloudflare publication and secure session auth remain a separate phase.

## What the numbers really mean

- `initial_capital_usdt`: initialized **simulated** PAPER starting balance.
- `cash_usdt`: persisted **simulated** cash, not a portfolio market value.
- `realized_pnl_usdt`: only **realized** PAPER trades. An open position does not contribute an invented unrealized PnL or equity amount.
- `paid_fees_usdt`: ledger's cumulative **hypothetical** fees.
- Position count and bounded latest positions/events are stored accounting facts; `limit` is 1–30, with total counts preserved.
- If a simulated trade has a one-to-one `paper_spot_observed_quotes` record, its source/symbol/interval/reference price/fee-slippage and **LOCAL REST receive time in UTC** appear beside the journal event. This is not exchange execution time, a current tick or a fill guarantee. Older historical or account-opened events have no quote: the panel explicitly says so.

## Worker status is NOT live process telemetry

`paper_worker_guard` persists only last status, last attempt and next allowed time. `trading_settings.bot_enabled` is only a **saved user preference**, not proof that a background worker is running. This API deliberately returns `process_verified_running=false` and the UI says **"Çalışan servis doğrulanmadı"**. It must not infer a running process from recent timestamps, polling, settings or a database row. A future authenticated heartbeat/uptime subsystem can implement verified process status, separately.

## Security and deployment

The new endpoint never initializes a PAPER account, requests Binance data or writes any table. Its PostgreSQL transaction is `REPEATABLE READ READ ONLY`; all fields are bounded and serialized without private credentials, event idempotency keys or arbitrary network URLs. When disconnected, failed or empty, the panel shows no fabricated cash/PnL. Refresh is explicit; no automatic Binance polling or scheduler activation. Neither `main` nor production hosting changed.

Tests cover API auth/no-store/untrusted Origin/Host, database absent, empty and initialized snapshots, exact Decimal values, stored quote event association and read-only behaviour. Connected Playwright tests at 360/390/768 px validate truthful empty states, refreshed API and no horizontal overflow. Existing Python PostgreSQL, Docker Persistence, TypeScript/Next build and Full Stack Paper E2E must pass on final PR head and merged `dev`. Strix active scan and manual Playwright MCP visual inspection are not performed by these CI tests.
