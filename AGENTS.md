# Agent instructions — Binance AI Trader / TradePilot

## Scope and isolation

- THIS repository is `imhollyy/binance-ai-trader`. Never touch Tam Kare,
  its production Supabase, GitHub repository, domains, secrets, or customers.
- Work on `dev` via focused `feat/*`, `test/*` or `docs/*` branches.
  Protect `main`: no unattended release, deployment or main merge.
- No real Binance orders, authenticated account access, withdraw permissions,
  production API keys, Futures execution or live money. `paper` is the ONLY
  supported execution mode; risk boundaries must fail closed.
- Never write secrets in source, logs, pull requests, tests, public GitHub
  Actions output or generated fixtures.
- Local dev API with `X-Admin-Token` is NOT public-ready authentication.
  Do not expose it via a tunnel or public Cloudflare/other hostname.

## Required development checks

From repository root:

```sh
python -m pip install -e ".[dev]"
python -m ruff check .
python -m ruff format --check .
python -m mypy src tests
python -m pytest -q
```

For UI work:

```sh
cd web
npm install
npm run typecheck
npm run build
npm run test:e2e
```

- PostgreSQL integration, Docker down/up persistence and full-stack
  browser tests are covered by GitHub Actions. Don't mark them passed
  until their actual CI jobs succeed.
- Reject any change that fabricates prices, yields optimistic PnL without
  accounting for trading costs, treats an unfinished candle as final, or
  ignores a `429/418` exchange restriction.
- Maintain Decimal/NUMERIC exactness; never represent ledger money as
  binary floats or claim stop-loss is guaranteed.
- For state-changing code require tests for duplicate IDs, restarts,
  replay/concurrency, rejected operations, bounds and failure recovery.
- Add separate tests for behavior change before merge; include CI links
  and remaining gaps in PR description.

## Four optional review tools

The user plans to connect the Tam Kare review toolchain when at their PC:
Context7 MCP, UI Skills, Playwright and Strix. Only Playwright browser tests
are already part of this repository. Never claim the other tools are active
without a successful capability invocation and recorded evidence.

- **Context7 MCP:** use current official documentation for library versions.
- **UI Skills:** audit mobile usability, contrast, keyboard semantics and
  accessible interaction states.
- **Playwright:** test real Chromium browser behavior on 360/390/768/1280 px
  and API-to-PostgreSQL paper-only flows.
- **Strix:** security checks against LOCAL TEST targets with disposable
  credentials/data, after explicit user authorization. Never attack public
  hosts or run destructive scans. Document findings without secrets.

## Definition of done

No production changes; no real orders; new tests; all applicable CI
workflows green; user-facing docs truthful; review and explicit integration
decision before dev-to-main release. Never silently grant trading permissions.
