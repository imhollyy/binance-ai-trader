"""Local-only Quick Tunnel staging env bootstrap (NO provider mutations).

Prompts once for two preview-only passwords, saves only salted scrypt
verifiers in an ignored .env file. A changing trycloudflare hostname updates
ONLY the public origin and host; no credentials are printed or overwritten.
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import os
import re
import secrets
from pathlib import Path
from urllib.parse import urlsplit

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


def _hash(password: str) -> str:
    if not 16 <= len(password) <= 256:
        raise ValueError("Password must contain 16-256 characters")
    salt = secrets.token_bytes(16)
    key = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1, dklen=64)
    return f"scrypt_16384:{salt.hex()}:{key.hex()}"


def _read_env(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        if not separator or key not in KEYS or key in result or not value:
            raise ValueError("Unexpected or empty key in private preview env")
        result[key] = value
    if set(result) != KEYS:
        raise ValueError("Existing private preview env has an incompatible shape")
    if (
        result["PRIVATE_PREVIEW_OPT_IN"] != "isolated-paper-only"
        or result["PREVIEW_CADDYFILE"] != "./deploy/preview/Caddyfile.quick"
        or result["PREVIEW_INGRESS_BIND"] != "127.0.0.1:8765:80"
    ):
        raise ValueError("Existing preview env is not the isolated quick profile")
    if not result["STAGING_DATABASE_URL"].startswith(
        "postgresql+psycopg://trader_preview:"
    ) or not result["STAGING_DATABASE_URL"].endswith("@preview-db:5432/trader_preview"):
        raise ValueError("Preview database does not match the isolated container")
    for key in ("STAGING_VIEWER_PASSWORD_HASH", "STAGING_OPERATOR_PASSWORD_HASH"):
        if not re.fullmatch(r"scrypt_16384:[0-9a-f]{32}:[0-9a-f]{128}", result[key]):
            raise ValueError("Invalid preview credential verifier")
    return result


def prepare(
    origin: str, path: Path, viewer: str | None = None, operator: str | None = None
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
        raise ValueError("Expected the exact HTTPS trycloudflare.com hostname")
    if path.exists():
        # Existing passwords/DB are preserved; only this ephemeral URL rotates.
        values = _read_env(path)
        created = False
    else:
        if viewer is None:
            viewer = getpass.getpass("Choose a NEW preview viewer password (16-256 characters): ")
        if operator is None:
            operator = getpass.getpass("Choose a DIFFERENT preview operator password: ")
        if viewer == operator:
            raise ValueError("Viewer and operator passwords must be distinct")
        pw = secrets.token_hex(24)
        values = {
            "PRIVATE_PREVIEW_OPT_IN": "isolated-paper-only",
            "PREVIEW_HOST": "",
            "STAGING_WEB_ORIGIN": "",
            "STAGING_POSTGRES_PASSWORD": pw,
            "STAGING_DATABASE_URL": (
                f"postgresql+psycopg://trader_preview:{pw}@preview-db:5432/trader_preview"
            ),
            "STAGING_VIEWER_PASSWORD_HASH": _hash(viewer),
            "STAGING_OPERATOR_PASSWORD_HASH": _hash(operator),
            "PREVIEW_CADDYFILE": "./deploy/preview/Caddyfile.quick",
            "PREVIEW_INGRESS_BIND": "127.0.0.1:8765:80",
        }
        created = True
    values["PREVIEW_HOST"] = parsed.hostname or ""
    values["STAGING_WEB_ORIGIN"] = origin
    # The file contains sensitive hashes and a local DB credential; keep private.
    path.write_text("".join(f"{key}={values[key]}\n" for key in sorted(KEYS)), encoding="utf-8")
    if os.name != "nt":
        path.chmod(0o600)
    return created


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--origin", required=True)
    parser.add_argument("--file", default=".env.preview.quick")
    args = parser.parse_args()
    # Only an exact ignored in-project file may be created or mutated.
    if Path(args.file).name != ".env.preview.quick":
        raise ValueError("Use an ignored .env.preview.quick file")
    created = prepare(args.origin, Path(args.file))
    print("Local preview configuration validated (credentials never printed)")
    if created:
        print("Remember the two passwords you entered; only their hashes were saved")


if __name__ == "__main__":
    main()
