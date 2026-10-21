"""Opt-in, fail-closed validation for the disposable PAPER preview Compose.

Run *before* Alembic migrations. Never echo database URLs or credentials.
This is not an authorization to connect the preview to public infrastructure.
"""

from __future__ import annotations

from collections.abc import Mapping
from os import environ
from urllib.parse import urlsplit

from sqlalchemy.engine import make_url

from binance_ai_trader.api_config import ApiConfig


def verify_preview_environment(values: Mapping[str, str]) -> None:
    if values.get("PRIVATE_PREVIEW_OPT_IN") != "isolated-paper-only":
        raise ValueError("Private preview requires separate explicit opt-in")
    if "ADMIN_TOKEN" in values or values.get("API_AUTH_MODE") != "staging":
        raise ValueError("Private preview must use staging sessions, never a local admin token")
    if values.get("TRADING_MODE") != "paper":
        raise ValueError("Private preview requires PAPER-only trading mode")
    if values.get("PAPER_SCHEDULER_OPT_IN") or values.get("PAPER_WORKER_ENABLED"):
        raise ValueError("Private preview cannot enable autonomous PAPER operations")

    config = ApiConfig.from_env(values)
    origin = config.staging_origin
    if origin is None or config.staging_host != urlsplit(origin).hostname:
        raise ValueError("Private preview requires an exact shared HTTPS web/API hostname")

    raw_url = config.database_url
    if raw_url is None:
        raise ValueError("Private preview requires an isolated database")
    try:
        db = make_url(raw_url)
    except Exception as exc:
        raise ValueError("Invalid private preview database configuration") from exc
    if (
        db.drivername != "postgresql+psycopg"
        or db.host != "preview-db"
        or db.port != 5432
        or db.username != "trader_preview"
        or db.database != "trader_preview"
        or not db.password
        or db.query
    ):
        raise ValueError("Private preview requires the isolated preview-only PostgreSQL service")


def main() -> None:
    verify_preview_environment(environ)


if __name__ == "__main__":
    main()
