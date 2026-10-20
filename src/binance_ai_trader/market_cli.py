"""A single manual public-Spot market import; NEVER a trading command.

Usage (inside the local docker compose API container):
  python -m binance_ai_trader.market_cli --symbol BTCUSDT --interval 1m --limit 100
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence

from sqlalchemy import create_engine
from sqlalchemy.exc import SQLAlchemyError

from binance_ai_trader.api_config import ApiConfig
from binance_ai_trader.market_data import (
    MarketDataError,
    MarketRateLimited,
    PublicSpotMarketClient,
)
from binance_ai_trader.market_ingest import ingest_once


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m binance_ai_trader.market_cli",
        description="Import CLOSED public Spot candles once, without Binance account access.",
    )
    parser.add_argument("--symbol", required=True, choices=("BTCUSDT", "ETHUSDT"))
    parser.add_argument("--interval", required=True, choices=("1m", "5m", "15m"))
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args(argv)
    if not 1 <= args.limit <= 1000:
        print("Limit must be an integer in range 1..1000", file=sys.stderr)
        return 2
    try:
        config = ApiConfig.from_env()
    except ValueError as exc:
        print(f"Safe configuration rejected: {exc}", file=sys.stderr)
        return 2
    if not config.database_url:
        print("DATABASE_URL must be set; no import attempted.", file=sys.stderr)
        return 2

    try:
        engine = create_engine(config.database_url, pool_pre_ping=True)
    except SQLAlchemyError:
        print("Database configuration could not be initialized.", file=sys.stderr)
        return 5

    try:
        report = ingest_once(
            engine,
            PublicSpotMarketClient(),
            symbol=args.symbol,
            interval=args.interval,
            limit=args.limit,
        )
    except MarketRateLimited as exc:
        cooldown = exc.retry_after_seconds
        print(
            f"Public Binance rate limit HTTP {exc.status_code}; "
            f"retry_after_seconds={cooldown}; no automatic retry.",
            file=sys.stderr,
        )
        return 3
    except MarketDataError:
        print("Public market data unavailable or invalid; import stopped.", file=sys.stderr)
        return 4
    except SQLAlchemyError:
        print("Database write/read failed; import stopped.", file=sys.stderr)
        return 5
    finally:
        engine.dispose()

    print(
        json.dumps(
            {
                "source": "public_spot_closed_candles",
                "symbol": report.symbol,
                "interval": report.interval,
                "received_closed": report.received_closed,
                "inserted_new": report.inserted_new,
                "missing_count": len(report.missing_open_times_ms),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
