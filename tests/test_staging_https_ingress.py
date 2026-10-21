"""BT-009B: real loopback TLS -> sanitizing test proxy -> staging FastAPI.

This is deliberately a disposable TEST HARNESS, not a deployable ingress:
- Both listeners bind to 127.0.0.1 and need no network/provider account.
- The TLS certificate is ephemeral, self-signed and trusted ONLY by this test.
- The proxy originates upstream traffic from 127.0.0.2; Uvicorn trusts exactly
  that IP for scheme headers. A direct 127.0.0.1 connection is untrusted.
- No browser engine, Cloudflare Access, production TLS or actual Postgres.
"""

from __future__ import annotations

import http.client
import shutil
import socket
import ssl
import subprocess
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
import uvicorn
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from binance_ai_trader.api import create_app
from binance_ai_trader.api_config import ApiConfig
from binance_ai_trader.models import Base, StagingLoginGateRow
from binance_ai_trader.staging_auth import COOKIE, password_hash

HOST = "api.staging.example"
ORIGIN = "https://app.staging.example"
VIEWER_PASSWORD = "fixture-viewer-passphrase-only"
OPERATOR_PASSWORD = "fixture-operator-passphrase-only"
HOP_HEADERS = {
    "connection",
    "content-length",
    "host",
    "keep-alive",
    "proxy-connection",
    "transfer-encoding",
    "te",
    "trailer",
    "upgrade",
}
UNTRUSTED_FORWARD_HEADERS = {
    "forwarded",
    "x-forwarded-for",
    "x-forwarded-host",
    "x-forwarded-port",
    "x-forwarded-proto",
    "x-real-ip",
}


@pytest.fixture
def tls_ingress(tmp_path: Path) -> Iterator[tuple[httpx.Client, int]]:
    """Actual TLS sockets and an isolated SQLite staging auth database."""
    if shutil.which("openssl") is None:
        pytest.skip("OpenSSL CLI required to generate an ephemeral local TLS certificate")
    cert = tmp_path / "fixture.crt"
    private_key = tmp_path / "fixture.key"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-sha256",
            "-subj",
            "/CN=localhost",
            "-addext",
            "subjectAltName=IP:127.0.0.1",
            "-keyout",
            str(private_key),
            "-out",
            str(cert),
        ],
        check=True,
        capture_output=True,
        timeout=15,
    )
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    with Session(engine) as session, session.begin():
        session.add(StagingLoginGateRow(id=1, failures=0, locked_until_ms=0))
    config = ApiConfig(
        database_url="sqlite+pysqlite:///:memory:",
        admin_token=None,
        auth_mode="staging",
        staging_origin=ORIGIN,
        staging_host=HOST,
        viewer_password_hash=password_hash(VIEWER_PASSWORD, salt=b"viewer-tls-test1"),
        operator_password_hash=password_hash(OPERATOR_PASSWORD, salt=b"operat-tls-test1"),
    )
    app = create_app(config=config, engine=engine)

    # Reserve the private API socket before starting Uvicorn, avoiding a port race.
    upstream_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    upstream_socket.bind(("127.0.0.1", 0))
    upstream_socket.listen(128)
    upstream_port = int(upstream_socket.getsockname()[1])
    uvicorn_server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=upstream_port,
            lifespan="off",
            access_log=False,
            log_level="error",
            proxy_headers=True,
            forwarded_allow_ips="127.0.0.2",
        )
    )
    api_thread = threading.Thread(
        target=uvicorn_server.run, kwargs={"sockets": [upstream_socket]}, daemon=True
    )
    api_thread.start()

    class LoopbackTLSProxy(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:
            self._forward()

        def do_POST(self) -> None:
            self._forward()

        def do_PUT(self) -> None:
            self._forward()

        def do_OPTIONS(self) -> None:
            self._forward()

        def _forward(self) -> None:
            # This is the *test* ingress policy. Never treat it as a production
            # Cloudflare Access/WAF policy or proof of direct-origin isolation.
            if self.headers.get("Host") != HOST:
                self.send_error(400, "Untrusted host")
                return
            if not self.path.startswith("/api/v1/"):
                self.send_error(404, "Not a public staging route")
                return
            body_size = int(self.headers.get("Content-Length", "0"))
            if body_size < 0 or body_size > 64_000:
                self.send_error(413, "Invalid request size")
                return
            body = self.rfile.read(body_size)
            headers = {
                key: value
                for key, value in self.headers.items()
                if key.lower() not in HOP_HEADERS | UNTRUSTED_FORWARD_HEADERS
            }
            headers["Host"] = HOST
            # Only THIS test proxy may attest HTTPS to Uvicorn.
            headers["X-Forwarded-Proto"] = "https"
            headers["X-Forwarded-For"] = "127.0.0.1"
            upstream = http.client.HTTPConnection(
                "127.0.0.1",
                upstream_port,
                timeout=5,
                source_address=("127.0.0.2", 0),
            )
            try:
                upstream.request(self.command, self.path, body=body, headers=headers)
                response = upstream.getresponse()
                data = response.read()
                self.send_response(response.status)
                for key, value in response.getheaders():
                    if key.lower() not in HOP_HEADERS:
                        self.send_header(key, value)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            finally:
                upstream.close()

        def log_message(self, format: str, *args: object) -> None:
            # Never print cookie, body, CSRF, passwords or token-bearing URLs.
            return

    proxy = ThreadingHTTPServer(("127.0.0.1", 0), LoopbackTLSProxy)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile=str(cert), keyfile=str(private_key))
    proxy.socket = context.wrap_socket(proxy.socket, server_side=True)
    proxy_port = int(proxy.server_address[1])
    proxy_thread = threading.Thread(target=proxy.serve_forever, daemon=True)
    proxy_thread.start()
    try:
        deadline = time.monotonic() + 5
        while not uvicorn_server.started and time.monotonic() < deadline:
            time.sleep(0.01)
        assert uvicorn_server.started, "Isolated Uvicorn did not start"
        # Unlike verify=False, cert-file trust validates the ephemeral TLS
        # server certificate and its 127.0.0.1 subjectAltName.
        with httpx.Client(
            base_url=f"https://127.0.0.1:{proxy_port}",
            verify=str(cert),
            headers={"Host": HOST, "Origin": ORIGIN},
            timeout=5.0,
            trust_env=False,
        ) as client:
            yield client, upstream_port
    finally:
        proxy.shutdown()
        proxy.server_close()
        proxy_thread.join(timeout=5)
        uvicorn_server.should_exit = True
        api_thread.join(timeout=5)
        upstream_socket.close()
        engine.dispose()


def test_real_tls_cookie_csrf_and_revocation(
    tls_ingress: tuple[httpx.Client, int],
) -> None:
    client, _ = tls_ingress
    assert client.get("/api/v1/settings").status_code == 401
    signed_in = client.post(
        "/api/v1/auth/login", json={"username": "viewer", "password": VIEWER_PASSWORD}
    )
    assert signed_in.status_code == 200
    assert signed_in.json()["role"] == "viewer"
    cookie = signed_in.headers["set-cookie"].lower()
    assert all(flag in cookie for flag in ("__host-", "secure", "httponly", "samesite=strict"))
    csrf = signed_in.json()["csrf_token"]
    assert client.cookies.get(COOKIE) is not None
    assert client.get("/api/v1/auth/session").status_code == 200
    settings = client.get("/api/v1/settings").json()
    assert (
        client.put(
            "/api/v1/settings",
            headers={"X-CSRF-Token": csrf, "X-Paper-Settings-Approval": "PAPER_SETTINGS_ONLY"},
            json={"expected_version": 0, "settings": settings["settings"]},
        ).status_code
        == 403
    )
    assert client.post("/api/v1/auth/logout").status_code == 403
    assert client.post("/api/v1/auth/logout", headers={"X-CSRF-Token": csrf}).status_code == 200
    assert client.get("/api/v1/auth/session").status_code == 401


def test_tls_proxy_strips_spoofed_forwarded_headers_and_rejects_bad_host(
    tls_ingress: tuple[httpx.Client, int],
) -> None:
    client, upstream_port = tls_ingress
    payload = {"username": "viewer", "password": VIEWER_PASSWORD}
    forged = {
        "X-Forwarded-Proto": "http",
        "X-Forwarded-Host": "evil.example",
        "X-Forwarded-For": "198.51.100.77",
        "Forwarded": "proto=http;host=evil.example",
    }
    # Client-supplied forwarding headers cannot alter the verified TLS fact.
    assert client.post("/api/v1/auth/login", headers=forged, json=payload).status_code == 200
    assert (
        client.post(
            "/api/v1/auth/login", headers={"Host": "evil.example"}, json=payload
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/api/v1/auth/login", headers={"Origin": "https://evil.example"}, json=payload
        ).status_code
        == 403
    )
    assert client.get("/health/live").status_code == 404

    # A direct HTTP caller on 127.0.0.1, even with an HTTPS scheme spoof,
    # is not the trusted proxy at 127.0.0.2 and cannot obtain a session.
    with httpx.Client(trust_env=False, timeout=5) as direct:
        denied = direct.post(
            f"http://127.0.0.1:{upstream_port}/api/v1/auth/login",
            headers={"Host": HOST, "Origin": ORIGIN, "X-Forwarded-Proto": "https"},
            json=payload,
        )
        assert denied.status_code == 403


def test_operator_elevation_over_real_loopback_tls(
    tls_ingress: tuple[httpx.Client, int],
) -> None:
    client, _ = tls_ingress
    auth = client.post(
        "/api/v1/auth/login", json={"username": "operator", "password": OPERATOR_PASSWORD}
    )
    assert auth.status_code == 200
    csrf = str(auth.json()["csrf_token"])
    snapshot = client.get("/api/v1/settings").json()
    payload = {"expected_version": 0, "settings": snapshot["settings"]}
    approval = {"X-Paper-Settings-Approval": "PAPER_SETTINGS_ONLY"}
    assert client.put("/api/v1/settings", headers=approval, json=payload).status_code == 403
    assert (
        client.put(
            "/api/v1/settings", headers={**approval, "X-CSRF-Token": "x" * 64}, json=payload
        ).status_code
        == 403
    )
    assert (
        client.put(
            "/api/v1/settings", headers={**approval, "X-CSRF-Token": csrf}, json=payload
        ).status_code
        == 200
    )
