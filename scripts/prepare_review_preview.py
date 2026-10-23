"""Prepare the isolated BT-027 browser-review preview environment locally."""

from __future__ import annotations

import argparse
import getpass
import hashlib
import os
import re
import secrets
from pathlib import Path
from urllib.parse import urlsplit

from sqlalchemy.engine import make_url

from binance_ai_trader.preview_preflight import verify_preview_environment

KEYS = {
    "PRIVATE_PREVIEW_OPT_IN",
    "PREVIEW_HOST",
    "STAGING_WEB_ORIGIN",
    "STAGING_POSTGRES_PASSWORD",
    "STAGING_DATABASE_URL",
    "STAGING_VIEWER_PASSWORD_HASH",
    "STAGING_OPERATOR_PASSWORD_HASH",
    "PREVIEW_CADDYFILE",
    "PREVIEW_INGRESS_BIND",
}
HOST_PATTERN = re.compile(r"^[a-z0-9-]+\.trycloudflare\.com$")
ENV_NAME = ".env.preview.review"


def _hash(password: str) -> str:
    if not 16 <= len(password) <= 256:
        raise ValueError("Preview password must contain 16-256 characters")
    salt = secrets.token_bytes(16)
    key = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1, dklen=64)
    return f"scrypt_16384:{salt.hex()}:{key.hex()}"


def _read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        if not separator or key not in KEYS or key in values or not value:
            raise ValueError("Review preview environment has an unexpected shape")
        values[key] = value
    if set(values) != KEYS:
        raise ValueError("Review preview environment is incomplete")
    if (
        values["PRIVATE_PREVIEW_OPT_IN"] != "isolated-paper-only"
        or values["PREVIEW_CADDYFILE"] != "./deploy/preview/Caddyfile.quick"
        or values["PREVIEW_INGRESS_BIND"] != "127.0.0.1:8877:80"
    ):
        raise ValueError("Review preview environment does not match its isolated profile")
    database = make_url(values["STAGING_DATABASE_URL"])
    if (
        database.drivername != "postgresql+psycopg"
        or database.host != "preview-db"
        or database.port != 5432
        or database.username != "trader_preview"
        or database.database != "trader_preview"
        or database.password != values["STAGING_POSTGRES_PASSWORD"]
        or database.query
    ):
        raise ValueError("Review preview database target is not the isolated service")
    for key in ("STAGING_VIEWER_PASSWORD_HASH", "STAGING_OPERATOR_PASSWORD_HASH"):
        if not re.fullmatch(r"scrypt_16384:[0-9a-f]{32}:[0-9a-f]{128}", values[key]):
            raise ValueError("Review preview verifier is invalid")
    return values


def prepare(
    origin: str,
    path: Path,
    viewer_password: str | None = None,
    viewer_password_confirmation: str | None = None,
) -> bool:
    parsed = urlsplit(origin)
    if (
        parsed.scheme != "https"
        or not HOST_PATTERN.fullmatch(parsed.hostname or "")
        or parsed.netloc != parsed.hostname
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Expected the exact HTTPS trycloudflare.com origin")
    if path.name != ENV_NAME:
        raise ValueError(f"Use only the ignored {ENV_NAME} file")

    if path.exists():
        values = _read_env(path)
        created = False
    else:
        if viewer_password is None:
            viewer_password = getpass.getpass("New review viewer password (16-256 chars): ")
            if viewer_password_confirmation is None:
                viewer_password_confirmation = getpass.getpass("Confirm review viewer password: ")
        elif viewer_password_confirmation is None:
            viewer_password_confirmation = viewer_password
        if viewer_password_confirmation is None:
            viewer_password_confirmation = getpass.getpass("Confirm review viewer password: ")
        if viewer_password != viewer_password_confirmation:
            raise ValueError("Viewer password confirmation does not match")
        database_password = secrets.token_urlsafe(36)
        values = {
            "PRIVATE_PREVIEW_OPT_IN": "isolated-paper-only",
            "PREVIEW_HOST": "",
            "STAGING_WEB_ORIGIN": "",
            "STAGING_POSTGRES_PASSWORD": database_password,
            "STAGING_DATABASE_URL": (
                "postgresql+psycopg://trader_preview:"
                f"{database_password}@preview-db:5432/trader_preview"
            ),
            "STAGING_VIEWER_PASSWORD_HASH": _hash(viewer_password),
            # A random, undisclosed operator verifier satisfies API config while
            # making the owner-review UI viewer-only.
            "STAGING_OPERATOR_PASSWORD_HASH": _hash(secrets.token_urlsafe(36)),
            "PREVIEW_CADDYFILE": "./deploy/preview/Caddyfile.quick",
            "PREVIEW_INGRESS_BIND": "127.0.0.1:8877:80",
        }
        created = True

    values["PREVIEW_HOST"] = parsed.hostname or ""
    values["STAGING_WEB_ORIGIN"] = origin
    verify_preview_environment(
        {
            **values,
            "API_AUTH_MODE": "staging",
            "TRADING_MODE": "paper",
            "DATABASE_URL": values["STAGING_DATABASE_URL"],
            "STAGING_API_HOST": values["PREVIEW_HOST"],
        }
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{key}={values[key]}\n" for key in sorted(KEYS)), encoding="utf-8")
    if os.name != "nt":
        path.chmod(0o600)
    return created


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--origin", required=True)
    parser.add_argument("--file", default=ENV_NAME)
    args = parser.parse_args()
    if Path(args.file).name != ENV_NAME:
        raise ValueError(f"Use only the ignored {ENV_NAME} file")
    created = prepare(args.origin, Path(args.file))
    print("Isolated review preview configuration prepared; credentials were not printed")
    if created:
        print("Viewer password exists only as a local scrypt verifier")


if __name__ == "__main__":
    main()
