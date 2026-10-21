"""Disposable CI-only single-host HTTPS staging preview smoke.

Synthetic credentials are knowingly public fixture values. Never run this
script against actual provider resources, production data or private Binance.
No successful login payload/cookie/CSRF value is printed.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HOST = "preview.staging.example"
BASE = f"https://{HOST}"
VIEWER = "ci-only-viewer-password-not-for-any-service"
OPERATOR = "ci-only-operator-password-not-for-any-service"
POSTGRES_PASSWORD = "ci_only_preview_postgres_password_2026"


def _verifier(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=16384, r=8, p=1, dklen=64)
    return f"scrypt_16384:{salt.hex()}:{digest.hex()}"


def generate(path: Path) -> None:
    if path.exists():
        raise RuntimeError("Refusing to overwrite an existing preview env file")
    content = "\n".join(
        (
            "PRIVATE_PREVIEW_OPT_IN=isolated-paper-only",
            f"PREVIEW_HOST={HOST}",
            f"STAGING_WEB_ORIGIN={BASE}",
            f"STAGING_POSTGRES_PASSWORD={POSTGRES_PASSWORD}",
            "STAGING_DATABASE_URL="
            "postgresql+psycopg://trader_preview:"
            f"{POSTGRES_PASSWORD}@preview-db:5432/trader_preview",
            f"STAGING_VIEWER_PASSWORD_HASH={_verifier(VIEWER)}",
            f"STAGING_OPERATOR_PASSWORD_HASH={_verifier(OPERATOR)}",
            "",
        )
    )
    path.write_text(content, encoding="utf-8")
    path.chmod(0o600)
    print("Synthetic preview-only fixture prepared (no credentials shown)")


def request(
    path: str,
    destination: Path,
    *,
    method: str = "GET",
    origin: str | None = BASE,
    cookie: Path | None = None,
    body: Path | None = None,
    csrf: str | None = None,
    extra_headers: tuple[str, ...] = (),
) -> tuple[int, bytes]:
    target = destination / "body.out"
    cmd = [
        "curl",
        "--silent",
        "--show-error",
        "--insecure",
        "--noproxy",
        "*",
        "--resolve",
        f"{HOST}:443:127.0.0.1",
        "--connect-timeout",
        "3",
        "--max-time",
        "12",
        "--output",
        str(target),
        "--write-out",
        "%{http_code}",
        "--request",
        method,
        f"{BASE}{path}",
    ]
    if origin is not None:
        cmd.extend(["--header", f"Origin: {origin}"])
    if body is not None:
        cmd.extend(["--header", "Content-Type: application/json"])
        cmd.extend(["--data-binary", f"@{body}"])
    if cookie is not None:
        cmd.extend(["--cookie", str(cookie)])
        if method == "POST" and path.endswith("/login"):
            cmd.extend(["--cookie-jar", str(cookie)])
    if csrf is not None:
        cmd.extend(["--header", f"X-CSRF-Token: {csrf}"])
    for header in extra_headers:
        cmd.extend(["--header", header])
    result = subprocess.run(cmd, capture_output=True, timeout=18, check=False)
    if result.returncode != 0:
        return 0, b""
    try:
        return int(result.stdout.decode("ascii").strip()), target.read_bytes()
    except (ValueError, OSError):
        return 0, b""


def smoke() -> None:
    with tempfile.TemporaryDirectory(prefix="preview-ci-") as temp:
        dest = Path(temp)
        cookie = dest / "cookiejar"
        body = dest / "login.json"
        body.write_text(json.dumps({"username": "viewer", "password": VIEWER}))
        body.chmod(0o600)
        deadline = time.monotonic() + 150
        while True:
            code, html = request("/", dest)
            if code == 200 and b"TradePilot" in html:
                break
            if time.monotonic() >= deadline:
                raise RuntimeError("Private preview web is not ready")
            time.sleep(2)
        print("Local single-host TLS web route: PASS (ephemeral internal CA)")

        # Never confuse web 200 with API readiness: require real authentication.
        # Only the numeric HTTP code is diagnostic, never headers/body/cookie.
        deadline = time.monotonic() + 75
        retry = 0
        while True:
            code, response = request(
                "/api/v1/auth/login", dest, method="POST", cookie=cookie, body=body
            )
            if code == 200:
                data = json.loads(response)
                assert data["role"] == "viewer"
                csrf = data["csrf_token"]
                assert isinstance(csrf, str) and len(csrf) == 64
                break
            retry += 1
            if code in (400, 401, 403, 404, 405, 422) and retry >= 3:
                raise RuntimeError(f"Staging login rejected with HTTP {code} (no details logged)")
            if time.monotonic() >= deadline:
                raise RuntimeError(f"Private preview API not ready, HTTP {code}")
            time.sleep(2)
        print("Actual loopback HTTPS -> private Caddy -> FastAPI -> PostgreSQL login: PASS")

        code, summary = request("/api/v1/paper/dashboard", dest, cookie=cookie)
        assert code == 200
        dashboard = json.loads(summary)
        assert dashboard["mode"] == "paper"
        assert dashboard["worker"]["process_verified_running"] is False
        print("Authenticated PAPER read-only dashboard: PASS")

        code, settings_reply = request("/api/v1/settings", dest, cookie=cookie)
        assert code == 200
        current = json.loads(settings_reply)
        put_payload = dest / "settings.json"
        put_payload.write_text(
            json.dumps({"expected_version": current["version"], "settings": current["settings"]})
        )
        code, _ = request(
            "/api/v1/settings",
            dest,
            method="PUT",
            cookie=cookie,
            csrf=csrf,
            body=put_payload,
            extra_headers=("X-Paper-Settings-Approval: PAPER_SETTINGS_ONLY",),
        )
        assert code == 403
        code, _ = request("/health/ready", dest)
        assert code == 404
        code, _ = request("/api/v1/auth/logout", dest, method="POST", cookie=cookie, csrf=csrf)
        assert code == 200
        code, _ = request("/api/v1/auth/session", dest, cookie=cookie)
        assert code == 401
        print("Viewer denied writes; health hidden; logout and revoked cookie: PASS")

        code, _ = request(
            "/api/v1/auth/login",
            dest,
            method="POST",
            cookie=cookie,
            body=body,
            origin="https://wrong.staging.example",
        )
        assert code == 403
        print("Wrong Origin rejected: PASS")
    print("NOTE: Test CA is NOT a trusted public TLS certificate or Cloudflare Access proof.")


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "generate":
        generate(Path(sys.argv[2]))
    elif sys.argv[1:] == ["smoke"]:
        smoke()
    else:
        raise SystemExit("usage: preview_ci_smoke.py generate <env-file> | smoke")
