"""Runtime configuration: local token and opt-in, HTTPS-only staging are isolated."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from ipaddress import ip_address
from os import environ
from urllib.parse import urlsplit

from binance_ai_trader.config import Settings


def _exact_dns_name(value: str | None) -> bool:
    """Only an exact, lowercase ASCII DNS hostname; never a wildcard/IP."""
    if value is None or len(value) > 253 or value != value.lower():
        return False
    labels = value.split(".")
    if len(labels) < 2 or not all(
        re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label, flags=re.ASCII)
        for label in labels
    ):
        return False
    try:
        ip_address(value)
    except ValueError:
        return True
    return False


@dataclass(frozen=True, slots=True)
class ApiConfig:
    """Default LOCAL-ONLY. Staging credentials NEVER fall back to local tokens."""

    database_url: str | None
    admin_token: str | None
    allowed_origins: tuple[str, ...] = ("http://localhost:3000", "http://127.0.0.1:3000")
    auth_mode: str = "local"
    staging_origin: str | None = None
    staging_host: str | None = None
    viewer_password_hash: str | None = None
    operator_password_hash: str | None = None

    def __post_init__(self) -> None:
        if self.auth_mode not in ("local", "staging"):
            raise ValueError("Unknown API authentication mode")
        if self.auth_mode == "staging":
            origin = self.staging_origin
            host = self.staging_host
            if self.admin_token or not self.database_url or not origin or not host:
                raise ValueError("Staging requires independent database, origin and hostname")
            parsed = urlsplit(origin)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or not parsed.netloc
                or parsed.username
                or parsed.password
                or parsed.path
                or parsed.query
                or parsed.fragment
                or parsed.port not in (None, 443)
                or origin != f"https://{parsed.hostname}"
                or not _exact_dns_name(parsed.hostname)
                or not _exact_dns_name(host)
            ):
                raise ValueError("Staging origin and API host must be exact HTTPS DNS names")
            from binance_ai_trader.staging_auth import valid_password_hash

            if not (
                valid_password_hash(self.viewer_password_hash)
                and valid_password_hash(self.operator_password_hash)
            ):
                raise ValueError("Staging requires two valid independent password hashes")
            if self.viewer_password_hash == self.operator_password_hash:
                raise ValueError("Staging viewer and operator passwords must be distinct")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> ApiConfig:
        values = environ if env is None else env
        Settings.from_env(values)  # Reject non-paper runtime modes.
        mode = values.get("API_AUTH_MODE", "local")
        token = values.get("ADMIN_TOKEN")
        if mode == "staging":
            if token is not None:
                raise ValueError("ADMIN_TOKEN must be unset in staging")
            return cls(
                database_url=values.get("DATABASE_URL"),
                admin_token=None,
                auth_mode="staging",
                staging_origin=values.get("STAGING_WEB_ORIGIN"),
                staging_host=values.get("STAGING_API_HOST"),
                viewer_password_hash=values.get("STAGING_VIEWER_PASSWORD_HASH"),
                operator_password_hash=values.get("STAGING_OPERATOR_PASSWORD_HASH"),
            )
        if token is not None and len(token) < 32:
            raise ValueError("ADMIN_TOKEN must contain at least 32 characters.")
        return cls(
            database_url=values.get("DATABASE_URL"),
            admin_token=token,
            auth_mode=mode,
        )
