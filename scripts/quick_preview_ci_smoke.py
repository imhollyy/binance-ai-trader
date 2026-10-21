"""Localhost-only HTTP ingress contract for authenticated Cloudflare Quick Tunnel.

This deliberately does NOT contact Cloudflare. It checks fixed-host routing
and trusted Caddy-to-FastAPI HTTPS attestation on an isolated CI Compose stack.
The public Cloudflare email PIN and browser TLS remain external/manual tests.
"""

from __future__ import annotations

import http.client
import json
import time

HOST = "preview.staging.example"
ORIGIN = f"https://{HOST}"
VIEWER = "ci-only-viewer-password-not-for-any-service"


def send(
    path: str,
    *,
    method: str = "GET",
    origin: str | None = ORIGIN,
    cookie: str | None = None,
    body: bytes | None = None,
    host: str = HOST,
) -> tuple[int, bytes, str | None]:
    headers = {"Host": host}
    if origin is not None:
        headers["Origin"] = origin
    if cookie:
        headers["Cookie"] = cookie
    if body is not None:
        headers["Content-Type"] = "application/json"
    conn = http.client.HTTPConnection("127.0.0.1", port=8765, timeout=6)
    try:
        conn.request(method, path, body=body, headers=headers)
        reply = conn.getresponse()
        status = reply.status
        data = reply.read()
        cookie_value = reply.getheader("set-cookie")
        return status, data, cookie_value
    finally:
        conn.close()


def main() -> None:
    code, html, _ = send("/")
    assert code == 200 and b"TradePilot" in html
    print("Local private quick-tunnel origin web: PASS")

    payload = json.dumps({"username": "viewer", "password": VIEWER}).encode()
    # Web can become ready before the restarted staging API applies migrations.
    for _attempt in range(40):
        try:
            code, body, set_cookie = send("/api/v1/auth/login", method="POST", body=payload)
        except OSError:
            code, body, set_cookie = 0, b"", None
        if code == 200:
            break
        if code in (0, 502, 503, 504):
            time.sleep(2)
            continue
        # Restrict diagnostics to fixed error classes, never print body/headers.
        try:
            reason = json.loads(body).get("detail", "")
        except (ValueError, AttributeError):
            reason = ""
        safe = ("HTTPS required", "Origin not permitted", "Local admin headers forbidden")
        category = reason if reason in safe else "unclassified"
        raise RuntimeError(f"Quick-origin login denied: HTTP {code}, reason={category}")
    else:
        raise RuntimeError("Quick-origin API not ready after safe retries")
    assert code == 200
    assert set_cookie is not None
    assert all(flag in set_cookie.lower() for flag in ("secure", "httponly", "samesite=strict"))
    session = json.loads(body)
    assert session["role"] == "viewer"
    csrf = session["csrf_token"]
    assert isinstance(csrf, str) and len(csrf) == 64
    cookie = set_cookie.split(";", maxsplit=1)[0]
    code, dash, _ = send("/api/v1/paper/dashboard", cookie=cookie)
    assert code == 200 and json.loads(dash)["mode"] == "paper"
    print("Trusted ingress HTTPS claim -> FastAPI -> PostgreSQL session: PASS")

    code, _, _ = send("/health/ready")
    assert code == 404
    code, _, _ = send("/api/v1/auth/login", method="POST", body=payload, host="evil.test")
    assert code == 404
    code, _, _ = send("/api/v1/auth/login", method="POST", body=payload, origin="https://evil.test")
    assert code == 403

    # Exercise the same POST Origin, JSON content-type, cookie and CSRF
    # headers used by the real browser logout UI (no Cloudflare involved).
    logout_headers = {
        "Host": HOST,
        "Origin": ORIGIN,
        "Cookie": cookie,
        "Content-Type": "application/json",
        "X-CSRF-Token": csrf,
    }
    conn = http.client.HTTPConnection("127.0.0.1", port=8765, timeout=6)
    try:
        conn.request("POST", "/api/v1/auth/logout", body=b"{}", headers=logout_headers)
        logout = conn.getresponse()
        logout_status = logout.status
        logout.read()
    finally:
        conn.close()
    assert logout_status == 200
    code, _, _ = send("/api/v1/auth/session", cookie=cookie)
    assert code == 401
    print("Quick-origin browser-style CSRF logout and revoked-cookie denial: PASS")
    print("Internal routes, wrong host and wrong origin denied: PASS")
    print("NOT RUN: Cloudflare Quick Tunnel email PIN, public trusted TLS or browser cookies")


if __name__ == "__main__":
    main()
