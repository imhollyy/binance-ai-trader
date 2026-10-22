"""Minimal allowlisted staging audit signals; never serialize untrusted request data.

Not a substitute for reverse-proxy / Uvicorn access-log sanitization: the
staging ingress must disable raw upstream access logs before deployment.
"""

from __future__ import annotations

import logging

from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger("binance_ai_trader.staging_events")

# Intentionally exact paths; unknown attacker-controlled paths are "other".
_ROUTES = {
    "/api/v1/auth/login": "login",
    "/api/v1/auth/logout": "logout",
    "/api/v1/auth/session": "session",
    "/api/v1/settings": "settings",
    "/api/v1/status": "status",
    "/api/v1/audit": "audit",
    "/api/v1/paper/dashboard": "paper_dashboard",
    "/api/v1/paper/manual": "manual_paper",
    "/api/v1/paper/spot": "paper_spot",
}
_METHODS = {"GET", "POST", "PUT", "OPTIONS"}
_REPLACED_HEADERS = {
    b"cache-control",
    b"pragma",
    b"x-content-type-options",
    b"referrer-policy",
}
_HEADERS = [
    (b"cache-control", b"no-store, private"),
    (b"pragma", b"no-cache"),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
]


class StagingObservabilityMiddleware:
    """Fail-closed response cache policy + bounded, credential-free event tags."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        route = _ROUTES.get(scope.get("path", ""), "other")
        original_method = scope.get("method", "")
        method = original_method if original_method in _METHODS else "other"
        status = 500

        async def send_guarded(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
                headers = [
                    (key, value)
                    for key, value in message.get("headers", [])
                    if key.lower() not in _REPLACED_HEADERS
                ]
                message["headers"] = headers + _HEADERS
            await send(message)

        try:
            await self.app(scope, receive, send_guarded)
        finally:
            # Deliberately NO host, URL, query, IP, body, exceptions, cookies,
            # user name, role, passwords, CSRF token, session id or raw headers.
            logger.info(
                "staging_http route=%s method=%s status=%sxx",
                route,
                method,
                status // 100,
            )
