"""Localhost-only HTTP ingress contract for authenticated Cloudflare Quick Tunnel.

This deliberately does NOT contact Cloudflare. It checks fixed-host routing
and trusted Caddy-to-FastAPI HTTPS attestation on an isolated CI Compose stack.
The public Cloudflare email PIN and browser TLS remain external/manual tests.
"""

from __future__ import annotations

import http.client
import json

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
    code, body, set_cookie = send("/api/v1/auth/login", method="POST", body=payload)
    assert code == 200
    assert set_cookie is not None
    assert all(flag in set_cookie.lower() for flag in ("secure", "httponly", "samesite=strict"))
    assert json.loads(body)["role"] == "viewer"
    cookie = set_cookie.split(";", maxsplit=1)[0]
    code, dash, _ = send("/api/v1/paper/dashboard", cookie=cookie)
    assert code == 200 and json.loads(dash)["mode"] == "paper"
    print("Trusted ingress HTTPS claim -> FastAPI -> PostgreSQL session: PASS")

    code, _, _ = send("/health/ready")
    assert code == 404
    code, _, _ = send("/api/v1/auth/login", method="POST", body=payload, host="evil.test")
    assert code == 404
    code, _, _ = send(
        "/api/v1/auth/login", method="POST", body=payload, origin="https://evil.test"
    )
    assert code == 403
    print("Internal routes, wrong host and wrong origin denied: PASS")
    print("NOT RUN: Cloudflare Quick Tunnel email PIN, public trusted TLS or browser cookies")


if __name__ == "__main__":
    main()
