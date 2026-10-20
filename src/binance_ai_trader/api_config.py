"""Runtime configuration: no secrets committed, no trading privileges."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from os import environ

from binance_ai_trader.config import Settings


@dataclass(frozen=True, slots=True)
class ApiConfig:
    """Settings for the localhost-only development API."""

    database_url: str | None
    admin_token: str | None
    allowed_origins: tuple[str, ...] = ("http://localhost:3000", "http://127.0.0.1:3000")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> ApiConfig:
        values = environ if env is None else env
        Settings.from_env(values)  # Reject non-paper runtime modes.
        token = values.get("ADMIN_TOKEN")
        if token is not None and len(token) < 32:
            raise ValueError("ADMIN_TOKEN must contain at least 32 characters.")
        return cls(database_url=values.get("DATABASE_URL"), admin_token=token)
