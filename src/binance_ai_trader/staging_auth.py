"""Opt-in HTTPS staging: revocable PostgreSQL sessions and strict CSRF/RBAC.

The legacy X-Admin-Token remains LOCAL-ONLY and is NEVER recognized here.
No OAuth, exchange credentials, trade execution, public deploy or scheduler routes.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from typing import TYPE_CHECKING

from fastapi import FastAPI, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import Engine, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from binance_ai_trader.models import StagingLoginGateRow, StagingSessionRow

if TYPE_CHECKING:
    from binance_ai_trader.api_config import ApiConfig

COOKIE = "__Host-tradepilot"
SESSION_LIFETIME_MS = 15 * 60_000
LOCKOUT_MS = 5 * 60_000
PASSWORD_N = 16_384
APPROVAL = "PAPER_SETTINGS_ONLY"


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def password_hash(password: str, *, salt: bytes | None = None) -> str:
    """Setup-time helper: generate an environment HASH, not plaintext secret."""

    if len(password) < 16 or len(password) > 256:
        raise ValueError("Staging passphrase length must be between 16 and 256")
    fresh = secrets.token_bytes(16) if salt is None else salt
    if len(fresh) != 16:
        raise ValueError("Invalid staging password salt")
    raw = hashlib.scrypt(password.encode("utf-8"), salt=fresh, n=PASSWORD_N, r=8, p=1, dklen=64)
    return f"scrypt_{PASSWORD_N}:{fresh.hex()}:{raw.hex()}"


def valid_password_hash(value: str | None) -> bool:
    if not isinstance(value, str):
        return False
    parts = value.split(":")
    if len(parts) != 3 or parts[0] != f"scrypt_{PASSWORD_N}":
        return False
    try:
        return len(bytes.fromhex(parts[1])) == 16 and len(bytes.fromhex(parts[2])) == 64
    except ValueError:
        return False


def _verify_password(candidate: str, stored: str) -> bool:
    if not valid_password_hash(stored):
        return False
    _, salt, correct = stored.split(":")
    raw = hashlib.scrypt(
        candidate.encode("utf-8"),
        salt=bytes.fromhex(salt),
        n=PASSWORD_N,
        r=8,
        p=1,
        dklen=64,
    )
    return hmac.compare_digest(raw, bytes.fromhex(correct))


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=50)
    password: str = Field(min_length=1, max_length=256)


class LoginResult(BaseModel):
    role: str
    expires_at_ms: int
    csrf_token: str


class SessionInfo(BaseModel):
    role: str
    expires_at_ms: int
    process_verified_running: bool = False


class StagingAccess:
    def __init__(self, config: ApiConfig, engine: Engine | None):
        if config.auth_mode != "staging":
            raise ValueError("Only configured staging mode creates session handlers")
        self.config = config
        self.engine = engine

    def _session_key(self, raw: str, role: str) -> str:
        """Bind the bearer digest to the currently configured role verifier.

        Updating that role's salted verifier invalidates *all* older tokens
        without dropping migration 0006 tables or requiring raw token storage.
        Never include the verifier, token or generated key in logs.
        """
        verifier = (
            self.config.viewer_password_hash
            if role == "viewer"
            else self.config.operator_password_hash
            if role == "operator"
            else None
        )
        if verifier is None:
            raise ValueError("Unknown staging session role")
        return hmac.new(verifier.encode("ascii"), raw.encode("ascii"), hashlib.sha256).hexdigest()

    def _origin(self, request: Request, *, required: bool = False) -> None:
        # Host checked independently by TrustedHostMiddleware.
        if request.url.scheme != "https":
            raise HTTPException(403, "HTTPS required")
        origin = request.headers.get("origin")
        if (required and origin != self.config.staging_origin) or (
            origin is not None and origin != self.config.staging_origin
        ):
            raise HTTPException(403, "Origin not permitted")
        if request.headers.get("x-admin-token") is not None:
            raise HTTPException(403, "Local admin headers forbidden in staging")

    def _engine(self) -> Engine:
        if self.engine is None:
            raise HTTPException(503, "Staging database unavailable")
        return self.engine

    def _current(self, request: Request, *, require_csrf: bool = False) -> StagingSessionRow:
        self._origin(request, required=require_csrf)
        raw = request.cookies.get(COOKIE)
        if not raw or len(raw) != 64:
            raise HTTPException(401, "Session required")
        try:
            candidates = (
                self._session_key(raw, "viewer"),
                self._session_key(raw, "operator"),
            )
        except UnicodeEncodeError as exc:
            raise HTTPException(401, "Session required") from exc
        try:
            with Session(self._engine()) as session:
                row = session.scalars(
                    select(StagingSessionRow).where(StagingSessionRow.token_hash.in_(candidates))
                ).one_or_none()
                if (
                    row is None
                    or row.token_hash != self._session_key(raw, row.role)
                    or row.revoked
                    or row.expires_at_ms <= _now_ms()
                ):
                    raise HTTPException(401, "Session expired or revoked")
                if require_csrf:
                    candidate = request.headers.get("x-csrf-token", "")
                    if len(candidate) != 64:
                        raise HTTPException(403, "Invalid CSRF protection")
                    try:
                        hashed = _digest(candidate)
                    except UnicodeEncodeError as exc:
                        raise HTTPException(403, "Invalid CSRF protection") from exc
                    if not hmac.compare_digest(row.csrf_hash, hashed):
                        raise HTTPException(403, "Invalid CSRF protection")
                # detached immutable fields; SQLAlchemy expires session on exit.
                session.expunge(row)
                return row
        except SQLAlchemyError as exc:
            raise HTTPException(503, "Staging database unavailable") from exc

    def require(self, request: Request, *, write: bool = False) -> None:
        row = self._current(request, require_csrf=write)
        if write and (
            row.role != "operator" or request.headers.get("x-paper-settings-approval") != APPROVAL
        ):
            raise HTTPException(403, "Additional settings approval required")

    def register(self, app: FastAPI) -> None:
        @app.post("/api/v1/auth/login", response_model=LoginResult)
        def login(body: LoginRequest, request: Request, response: Response) -> LoginResult:
            self._origin(request, required=True)
            now = _now_ms()
            try:
                with Session(self._engine()) as session, session.begin():
                    gate = session.scalars(
                        select(StagingLoginGateRow)
                        .where(StagingLoginGateRow.id == 1)
                        .with_for_update()
                    ).one_or_none()
                    if gate is None:
                        raise HTTPException(503, "Login protection unavailable")
                    if now < gate.locked_until_ms:
                        raise HTTPException(429, "Try again later")
                    expected = (
                        self.config.operator_password_hash
                        if body.username == "operator"
                        else self.config.viewer_password_hash
                    )
                    assert expected is not None  # required by staging configuration
                    verified = _verify_password(body.password, expected)
                    if body.username not in ("viewer", "operator") or not verified:
                        gate.failures = min(5, gate.failures + 1)
                        if gate.failures == 5:
                            gate.locked_until_ms = now + LOCKOUT_MS
                        # Commit throttle before returning a generic failure.
                        outcome: LoginResult | None = None
                    else:
                        gate.failures = 0
                        gate.locked_until_ms = 0
                        sid = secrets.token_hex(32)
                        csrf = secrets.token_hex(32)
                        expires = now + SESSION_LIFETIME_MS
                        session.add(
                            StagingSessionRow(
                                token_hash=self._session_key(sid, body.username),
                                csrf_hash=_digest(csrf),
                                role=body.username,
                                issued_at_ms=now,
                                expires_at_ms=expires,
                                revoked=False,
                            )
                        )
                        outcome = LoginResult(
                            role=body.username, expires_at_ms=expires, csrf_token=csrf
                        )
                if outcome is None:
                    raise HTTPException(401, "Invalid credentials")
                response.set_cookie(
                    key=COOKIE,
                    value=sid,
                    max_age=SESSION_LIFETIME_MS // 1000,
                    httponly=True,
                    secure=True,
                    samesite="strict",
                    path="/",
                )
                response.headers["Cache-Control"] = "no-store, private"
                return outcome
            except SQLAlchemyError as exc:
                raise HTTPException(503, "Staging database unavailable") from exc

        @app.get("/api/v1/auth/session", response_model=SessionInfo)
        def session_info(request: Request, response: Response) -> SessionInfo:
            row = self._current(request)
            response.headers["Cache-Control"] = "no-store, private"
            return SessionInfo(role=row.role, expires_at_ms=row.expires_at_ms)

        @app.post("/api/v1/auth/logout")
        def logout(request: Request, response: Response) -> dict[str, str]:
            try:
                row = self._current(request, require_csrf=True)
            except HTTPException as exc:
                # An expired/missing/revoked session is already signed out on
                # the server. Clear its stale browser cookie without treating
                # a legitimate sign-out as an error. 403 Origin/CSRF and 503
                # database failures still fail closed and are NOT swallowed.
                if exc.status_code != 401:
                    raise
                response.delete_cookie(
                    COOKIE, path="/", secure=True, httponly=True, samesite="strict"
                )
                response.headers["Cache-Control"] = "no-store, private"
                return {"status": "signed_out"}
            try:
                with Session(self._engine()) as session, session.begin():
                    session.execute(
                        update(StagingSessionRow)
                        .where(StagingSessionRow.token_hash == row.token_hash)
                        .values(revoked=True)
                    )
            except SQLAlchemyError as exc:
                raise HTTPException(503, "Staging database unavailable") from exc
            response.delete_cookie(COOKIE, path="/", secure=True, httponly=True, samesite="strict")
            response.headers["Cache-Control"] = "no-store, private"
            return {"status": "signed_out"}
