# Binance AI Trader

A **public, research-only** Python project for Binance Global market-data infrastructure and (later) paper trading. Source code and Git history are visible to everyone.

> **SAFE BY DEFAULT:** BT-001 cannot place orders, access an exchange account, use leverage, or trade with real funds. No Binance API keys or authenticated clients are used.

## Current milestone: BT-001 — Project bootstrap (complete on `dev`)

- Python 3.12 application using a `src/` package layout
- Configuration that **refuses any mode other than `paper`**
- Offline unit tests, static type checks, formatting and lint checks
- GitHub Actions CI with read-only permissions
- Architecture, security and sprint documentation

**Not yet implemented:** Binance REST/WebSocket collection, database, strategies, paper matching, dashboard, production hosting or live trading.

## Quick start

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
binance-ai-trader --status
python -m pytest -q
python -m ruff check .
python -m ruff format --check .
python -m mypy src tests
```

On Windows PowerShell, activate with `.venv\Scripts\Activate.ps1`.

The CLI prints `mode=paper | live_trading=disabled`. A `TRADING_MODE=live` variable fails closed.

## Public-repository security

**Never commit API keys, credentials, private account balances, identifiable trade history, proprietary strategies or production secrets.** Public Actions logs, issues, pull requests and past commits are visible. Any future live-trading engine must undergo a separate security review and can reside in a private repository/service.

## Development workflow

`feat/*` -> PR into `dev` -> reviewed release PR into `main`. Merge only when CI passes, safety checks pass and changes are reviewed.

See [architecture](docs/ARCHITECTURE.md), [security](SECURITY.md), and [sprint checklist](docs/SPRINT_01.md).
