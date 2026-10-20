# Sprint 01 — tasks and acceptance gates

## BT-001: Bootstrap — implemented on feature branch, pending review

- [x] Create private independent GitHub repository and dev/feature-branch workflow.
- [x] Add Python 3.12 package, entry point and reproducible install instructions.
- [x] Add paper-only fail-closed configuration and unit tests.
- [x] Add Ruff lint/format, Mypy strict, Pytest and GitHub Actions workflow.
- [x] Add .gitignore, safe environment example, security policy and architecture notes.
- [ ] Confirm CI succeeded on the PR or verify equivalent commands locally before merge.
- [ ] Reviewer approves and merges the PR into dev.

## BT-002: API + PostgreSQL + Docker — future package

- [ ] FastAPI health/readiness endpoints.
- [ ] Database schema and migrations.
- [ ] Docker Compose for local services; no production credentials in repo.
- [ ] Startup and failure-mode integration tests.

## BT-003: Binance public REST history — future package

- [ ] Ingest BTCUSDT / ETHUSDT candles for 1m, 5m and 15m.
- [ ] Validate exchange timestamps, symbols, limits, price and quantity precision.
- [ ] Durable idempotent storage and bounded retries.

## BT-004: Public WebSocket — future package

- [ ] Reconnect and recover missing data on interruptions.
- [ ] Detect stale data and expose degraded health status.
- [ ] Respect connection and message limits.

## BT-005–BT-010 — future packages

- Data retention / reconciliation, gap recovery, indicator checks (EMA/RSI/ATR),
  read-only reporting API, resilience/security testing and operator documentation.

## Merge rules

Do not merge on failing/unverified tests. Never enable live trading as part of this sprint.
