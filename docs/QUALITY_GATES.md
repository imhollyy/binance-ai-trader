# Quality gates and four-tool integration plan

This document prepares, but **does not install or verify**, the four tools
used in the Tam Kare development workflow: **Context7 MCP, UI Skills,
Playwright, Strix**. The user's existing VS Code/Codex setup will be
reused only after its capabilities are confirmed for this *independent* repo.

| Tool | Current status | Safe first task |
|---|---|---|
| Context7 MCP | Not verified in this repo | Resolve FastAPI, SQLAlchemy, Alembic and Next.js docs |
| UI Skills | Not verified in this repo | Audit dashboard/mobile risk controls and accessible warnings |
| Playwright | Already in `web/e2e` and CI | Run 360/390/768/1280px browser smoke tests |
| Strix | Not verified in this repo | Authorized, non-destructive scan of isolated paper-only localhost stack |

## Codex review workflow

1. Pull `dev`, inspect diff and confirm no secrets/API keys.
2. Make one focused task branch + tests; never make unauthorized live orders.
3. Run Ruff lint, Ruff formatter, mypy strict and pytest; fix failures.
4. For changed migrations, run real PostgreSQL tests and Docker data restart
   test. For changed UI, run Next build and Playwright mobile test.
5. Use Context7 for **actual doc calls** and UI Skills for **actual audit**
   when enabled; distinguish `PASS` from `NOT RUN`.
6. Run Strix only on an explicitly authorized isolated disposable
   environment. This is a security review, not a public penetration test.
7. Review PR diff and CI status; merge to `dev` only when gates are green.
8. Leave `main`, remote deploy, Binance credentials and real trading
   permissions untouched until separate release/security decisions.

## Remaining security checks prior to internet/mobile access

- Passwordless/multi-factor production authentication, robust session and
  CSRF protections; replace the local `X-Admin-Token` approach.
- HTTPS and server-side authorization / rate limits / safe headers.
- Threat model, remote access tests, audit integrity and disaster recovery.
- Separate Spot and Futures simulation tests; bounded market ingestion,
  replay-safe state, realistic fees, slippage, funding/liquidation.
- Independent paper trading period with manual reconciliation.
- Explicit go/no-go approval before any live exchange adapter exists.

No component of this plan implies that an account, order worker, deployed
frontend, public API or live Binance account is already active.
