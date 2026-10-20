"""Local-only FastAPI service for PAPER simulation settings."""

from __future__ import annotations

from datetime import UTC, datetime
from hmac import compare_digest

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import Engine, create_engine, select, text, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.middleware.trustedhost import TrustedHostMiddleware

from binance_ai_trader.api_config import ApiConfig
from binance_ai_trader.models import AuditRow, TradingSettingsRow
from binance_ai_trader.risk_schemas import RiskSettings, SettingsSnapshot, SettingsUpdate


class AuditEvent(BaseModel):
    """Safe configuration audit metadata; does not expose secrets or full payloads."""

    version: int
    action: str
    created_at: datetime


class AuditLog(BaseModel):
    events: list[AuditEvent]


class RuntimeStatus(BaseModel):
    execution_mode: str = "paper"
    exchange_connected: bool = False
    worker_active: bool = False
    live_orders_supported: bool = False


def create_app(
    *,
    config: ApiConfig | None = None,
    engine: Engine | None = None,
) -> FastAPI:
    """Create a localhost-only dev API; never connects to an exchange."""

    settings = config if config is not None else ApiConfig.from_env()
    db_engine = engine
    if db_engine is None and settings.database_url:
        db_engine = create_engine(settings.database_url, pool_pre_ping=True)

    app = FastAPI(title="Binance AI Trader — Paper API", version="0.2.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.allowed_origins),
        allow_methods=["GET", "PUT"],
        allow_headers=["X-Admin-Token", "Content-Type"],
    )
    app.add_middleware(
        TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "testserver"]
    )

    def require_admin(
        request: Request,
        x_admin_token: str | None = Header(default=None),
    ) -> None:
        """Both reads and writes require an explicit local development token."""

        if not settings.admin_token:
            raise HTTPException(status_code=503, detail="Local admin access is not configured")
        origin = request.headers.get("origin")
        if origin and origin not in settings.allowed_origins:
            raise HTTPException(status_code=403, detail="Origin not permitted")
        if not x_admin_token or not compare_digest(x_admin_token, settings.admin_token):
            raise HTTPException(status_code=401, detail="Not authorized")

    @app.get("/health/live")
    def liveness() -> dict[str, str]:
        return {"status": "alive"}

    @app.get("/health/ready", response_model=None)
    def readiness() -> JSONResponse | dict[str, str]:
        if db_engine is None:
            return JSONResponse(status_code=503, content={"status": "db_unconfigured"})
        try:
            with db_engine.connect() as connection:
                connection.execute(text("SELECT 1"))
        except SQLAlchemyError:
            return JSONResponse(status_code=503, content={"status": "db_unavailable"})
        return {"status": "ready"}

    @app.get("/api/v1/status", dependencies=[Depends(require_admin)])
    def runtime_status() -> RuntimeStatus:
        return RuntimeStatus()

    @app.get("/api/v1/settings", dependencies=[Depends(require_admin)])
    def get_settings() -> SettingsSnapshot:
        if db_engine is None:
            raise HTTPException(status_code=503, detail="Database not configured")
        with Session(db_engine) as session:
            try:
                record = session.get(TradingSettingsRow, 1)
            except SQLAlchemyError as exc:
                raise HTTPException(status_code=503, detail="Database unavailable") from exc
            if record is None:
                return SettingsSnapshot(version=0, settings=RiskSettings())
            return SettingsSnapshot(
                version=record.version, settings=RiskSettings.model_validate(record.data)
            )

    @app.get("/api/v1/audit", dependencies=[Depends(require_admin)])
    def audit_events(limit: int = Query(default=30, ge=1, le=100)) -> AuditLog:
        """Read the latest configuration changes without returning risk payloads."""

        if db_engine is None:
            raise HTTPException(status_code=503, detail="Database not configured")
        try:
            with Session(db_engine) as session:
                entries = session.scalars(
                    select(AuditRow).order_by(AuditRow.version.desc()).limit(limit)
                ).all()
                return AuditLog(
                    events=[
                        AuditEvent(
                            version=row.version, action=row.action, created_at=row.created_at
                        )
                        for row in entries
                    ]
                )
        except SQLAlchemyError as exc:
            raise HTTPException(status_code=503, detail="Database unavailable") from exc

    @app.put("/api/v1/settings", dependencies=[Depends(require_admin)])
    def put_settings(payload: SettingsUpdate) -> SettingsSnapshot:
        if db_engine is None:
            raise HTTPException(status_code=503, detail="Database not configured")
        now = datetime.now(UTC)
        with Session(db_engine) as session:
            try:
                with session.begin():
                    data = payload.settings.model_dump(mode="json")
                    if payload.expected_version == 0:
                        session.add(TradingSettingsRow(id=1, version=1, data=data, modified_at=now))
                        session.flush()  # Ensure the FK parent exists before inserting audit.
                    else:
                        result = session.connection().execute(
                            update(TradingSettingsRow)
                            .where(
                                TradingSettingsRow.id == 1,
                                TradingSettingsRow.version == payload.expected_version,
                            )
                            .values(
                                version=payload.expected_version + 1,
                                data=data,
                                modified_at=now,
                            )
                        )
                        if result.rowcount != 1:
                            raise HTTPException(
                                status_code=409, detail="Stale settings version; reload"
                            )
                    session.add(
                        AuditRow(
                            settings_id=1,
                            version=payload.expected_version + 1,
                            action="settings_updated",
                            data=data,
                            created_at=now,
                        )
                    )
            except IntegrityError as exc:
                raise HTTPException(
                    status_code=409, detail="Stale settings version; reload"
                ) from exc
            except SQLAlchemyError as exc:
                raise HTTPException(status_code=503, detail="Database unavailable") from exc
        return SettingsSnapshot(version=payload.expected_version + 1, settings=payload.settings)

    return app


app = create_app()
