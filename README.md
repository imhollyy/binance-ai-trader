# Binance AI Trader

A private, staged research project for Binance Global public market data and (later) paper trading.

> **SAFE BY DEFAULT:** BT-001 cannot place orders, access an exchange account, use leverage, or trade with real funds. There is no exchange client or API-key configuration in this version.

## Current milestone: BT-001 — Project bootstrap

- Python 3.12 application using a `src/` package layout
- Configuration that **refuses any mode other than `paper`**
- Offline unit tests, static type checks, formatting and lint checks
- GitHub Actions CI with read-only permissions
- Architecture, security and sprint documentation

**Not yet implemented:** Binance REST/WebSocket data collection, a database, trading strategies, paper order matching, dashboard, production hosting, or live trading. These require separate reviewed packages.

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

The CLI prints `mode=paper | live_trading=disabled`. A `TRADING_MODE=live` environment variable fails closed.

## Development workflow

`feat/*` branches -> PR into `dev` -> reviewed release PR into `main`. Do not merge unless CI is green and safety checks are verified locally. CI may be unavailable if Actions usage is exhausted.

See [architecture](docs/ARCHITECTURE.md), [security](SECURITY.md), and [sprint checklist](docs/SPRINT_01.md).
