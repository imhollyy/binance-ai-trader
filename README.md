# Binance AI Trader

Public, research-only Binance Global market data and paper-trading project.

> **No live orders or Binance private API access exist in BT-002.** All risk settings are for a future **paper-only** bot. Toggling the stored `bot_enabled` flag does not start a worker.

## Development

Python 3.12 is required. CI checks all tests, Ruff and Mypy.

```sh
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
python -m pytest -q
python -m ruff check .
python -m ruff format --check .
python -m mypy src tests
```

## Local Docker API (BT-002)

```sh
cp .env.example .env
# Replace ADMIN_TOKEN and POSTGRES_PASSWORD with strong random local-only values.
docker compose up --build -d
curl http://127.0.0.1:8000/health/live
curl http://127.0.0.1:8000/health/ready
```

API docs: http://127.0.0.1:8000/docs . Settings endpoints under `/api/v1/settings` require `X-Admin-Token` header, matching your local `.env` value. The token is never stored in the repository. The API binds to localhost. This is a **development-only token**, NOT production user authentication; do not publicly expose this service.

Stop without deleting volume: `docker compose down`. Do **not** run `docker compose down -v` unless you intentionally want to erase the local database.

## Safety

- No exchange credentials, no transaction endpoints, no Futures trading engine.
- Mode is always `paper`, with bounded risk settings, mandatory stop-loss and audit logging.
- Browser origins are restricted to localhost dev and requests require a local admin token. Do not deploy this API to the internet; production requires proper authentication, CSRF measures, HTTPS, rate limiting and audited access control.
- All money amounts in this milestone are **configuration budgets**, not actual balances or executions.

## Workflow

Feature PRs -> `dev` -> reviewed releases on `main`. Public history and logs must never include API keys, balances or proprietary strategies.

See [architecture](docs/ARCHITECTURE.md), [security](SECURITY.md) and [Sprint 01](docs/SPRINT_01.md).

## Stored public Spot data (BT-003D)

Authenticated local GET `/api/v1/market/candles?symbol=BTCUSDT&interval=1m&limit=30`
returns only previously imported **closed** public Spot candles. All OHLCV decimal
values are returned as JSON **strings** to avoid floating-point rounding.
If no data were imported the `candles` list is empty. This is **not**
a live ticker: no exchange request or scheduled worker is triggered by this endpoint.

## Populate paper-only historical Spot market view (manual, local)

After starting the local Docker Compose stack, run one bounded **public**
market-data import. No Binance API key is needed:

```bash
docker compose exec api python -m binance_ai_trader.market_cli --symbol BTCUSDT --interval 1m --limit 100
docker compose exec api python -m binance_ai_trader.market_cli --symbol ETHUSDT --interval 5m --limit 100
```

The output reports how many CLOSED candles were returned, how many were newly
stored and the count of missing intervals. Run it again to verify idempotence.
Your Piyasa Verileri page can then display stored prices through the local API.

This command is **manual and one-shot**, not a trading bot or background
scheduler. It never accesses balances, orders, Futures, exchange credentials
or private endpoints. HTTP 418/429 stops without automatically retrying.
Your development .env file must not be committed or shared.

## PC başında hızlı başlangıç

Türkçe, Windows PowerShell/macOS/Linux adımlarını içeren
[yerel kurulum ve panel testi](docs/LOCAL_QUICKSTART.md) rehberini izle.
