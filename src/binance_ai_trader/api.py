"""Local-only FastAPI service for PAPER simulation settings."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from hmac import compare_digest
from os import environ

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import Engine, create_engine, select, text, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.middleware.trustedhost import TrustedHostMiddleware

from binance_ai_trader.ai_paper_bot_controls_test import is_disposable_control_http_test
from binance_ai_trader.api_config import ApiConfig
from binance_ai_trader.manual_paper_api import register_manual_paper_route
from binance_ai_trader.models import (
    AuditRow,
    PaperSpotAccountRow,
    PaperSpotPositionRow,
    SpotCandleRow,
    TradingSettingsRow,
)
from binance_ai_trader.paper_bot_control_test_api import register_bot_control_test_routes
from binance_ai_trader.paper_dashboard_api import register_paper_dashboard_route
from binance_ai_trader.paper_read_api import register_paper_spot_read_route
from binance_ai_trader.preview_preflight import verify_preview_environment
from binance_ai_trader.risk_capabilities_api import register_risk_capabilities_route
from binance_ai_trader.risk_schemas import RiskSettings, SettingsSnapshot, SettingsUpdate
from binance_ai_trader.staging_auth import StagingAccess
from binance_ai_trader.staging_observability import StagingObservabilityMiddleware


class AuditEvent(BaseModel):
    """Safe configuration audit metadata; does not expose secrets or full payloads."""

    version: int
    action: str
    created_at: datetime


class AuditLog(BaseModel):
    events: list[AuditEvent]


class StoredSpotCandle(BaseModel):
    """Closed OHLCV data from storage; decimals serialize as JSON strings."""

    open_time_ms: int
    close_time_ms: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


class StoredCandleHistory(BaseModel):
    source: str = "stored_public_spot"
    symbol: str
    interval: str
    candles: list[StoredSpotCandle]


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

    staging = settings.auth_mode == "staging"
    staging_access = StagingAccess(settings, db_engine) if staging else None
    app = FastAPI(
        title="Binance AI Trader — Paper API",
        version="0.2.0",
        docs_url=None if staging else "/docs",
        redoc_url=None if staging else "/redoc",
        openapi_url=None if staging else "/openapi.json",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=(
            [settings.staging_origin or ""] if staging else list(settings.allowed_origins)
        ),
        allow_credentials=staging,
        allow_methods=["GET", "POST", "PUT"] if staging else ["GET", "PUT"],
        allow_headers=(
            ["Content-Type", "X-CSRF-Token", "X-Paper-Settings-Approval"]
            if staging
            else ["X-Admin-Token", "Content-Type"]
        ),
    )
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=(
            [settings.staging_host or ""] if staging else ["localhost", "127.0.0.1", "testserver"]
        ),
    )

    if staging:
        # All staging responses, including denied requests, must prevent
        # caching; audit only fixed safe route/status buckets, not raw URLs.
        app.add_middleware(StagingObservabilityMiddleware)

        @app.exception_handler(RequestValidationError)
        async def staging_invalid_payload(
            _request: Request, _exc: RequestValidationError
        ) -> JSONResponse:
            # Pydantic errors may echo "input", including attempted passwords.
            return JSONResponse(status_code=422, content={"detail": "Invalid request"})

    def require_admin(
        request: Request,
        x_admin_token: str | None = Header(default=None),
    ) -> None:
        """LOCAL dev token only; STAGING requires a revocable cookie session."""

        if staging_access is not None:
            staging_access.require(request)
            return
        if not settings.admin_token:
            raise HTTPException(status_code=503, detail="Local admin access is not configured")
        origin = request.headers.get("origin")
        if origin and origin not in settings.allowed_origins:
            raise HTTPException(status_code=403, detail="Origin not permitted")
        if not x_admin_token or not compare_digest(x_admin_token, settings.admin_token):
            raise HTTPException(status_code=401, detail="Not authorized")

    def require_write(
        request: Request,
        x_admin_token: str | None = Header(default=None),
    ) -> None:
        """Only elevated HTTPS stage operators may change PAPER settings."""

        if staging_access is not None:
            staging_access.require(request, write=True)
            return
        require_admin(request, x_admin_token)

    if staging_access is not None:
        staging_access.register(app)
        # This route is NOT part of normal staging/local. It is registered
        # only on a disposable localhost DB with a dedicated explicit test flag.
        if db_engine is not None and is_disposable_control_http_test(db_engine):
            register_bot_control_test_routes(app, staging_access, db_engine)

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

    @app.get("/api/v1/market/candles", dependencies=[Depends(require_admin)])
    def stored_spot_candles(
        symbol: str = Query(default="BTCUSDT", pattern=r"^(BTCUSDT|ETHUSDT)$"),
        interval: str = Query(default="1m", pattern=r"^(1m|5m|15m)$"),
        limit: int = Query(default=30, ge=1, le=100),
    ) -> StoredCandleHistory:
        """Read only completed Spot candles already stored in PostgreSQL.

        This endpoint NEVER fetches Binance or claims to serve live prices.
        """

        if db_engine is None:
            raise HTTPException(status_code=503, detail="Database not configured")
        try:
            with Session(db_engine) as session:
                rows = session.scalars(
                    select(SpotCandleRow)
                    .where(
                        SpotCandleRow.symbol == symbol,
                        SpotCandleRow.interval == interval,
                    )
                    .order_by(SpotCandleRow.open_time_ms.desc())
                    .limit(limit)
                ).all()
                return StoredCandleHistory(
                    symbol=symbol,
                    interval=interval,
                    candles=[
                        StoredSpotCandle(
                            open_time_ms=row.open_time_ms,
                            close_time_ms=row.close_time_ms,
                            open=row.open,
                            high=row.high,
                            low=row.low,
                            close=row.close,
                            volume=row.volume,
                        )
                        for row in rows
                    ],
                )
        except SQLAlchemyError as exc:
            raise HTTPException(status_code=503, detail="Database unavailable") from exc

    @app.put("/api/v1/settings", dependencies=[Depends(require_write)])
    def put_settings(payload: SettingsUpdate) -> SettingsSnapshot:
        if db_engine is None:
            raise HTTPException(status_code=503, detail="Database not configured")
        now = datetime.now(UTC)
        with Session(db_engine) as session:
            try:
                with session.begin():
                    data = payload.settings.model_dump(mode="json")
                    account = None
                    # The private staging viewer edits *only* bounded PAPER risk
                    # fields. An initialized ledger must never be invalidated by
                    # a settings version bump while carrying real PAPER history.
                    if staging and environ.get("PRIVATE_PREVIEW_OPT_IN") == "isolated-paper-only":
                        current = session.scalar(
                            select(TradingSettingsRow)
                            .where(TradingSettingsRow.id == 1)
                            .with_for_update()
                        )
                        if current is None or current.version != payload.expected_version:
                            raise HTTPException(
                                status_code=409, detail="Preview settings version changed"
                            )
                        prior = RiskSettings.model_validate(current.data)
                        excluded = {
                            "risk_per_trade_pct",
                            "daily_loss_limit_pct",
                            "max_drawdown_pct",
                        }
                        if (
                            prior.model_dump(exclude=excluded)
                            != payload.settings.model_dump(exclude=excluded)
                            or prior.bot_enabled
                            or not prior.manual_approval
                        ):
                            raise HTTPException(
                                status_code=409,
                                detail="Preview permits bounded manual PAPER risk only",
                            )
                        account = session.scalar(
                            select(PaperSpotAccountRow)
                            .where(PaperSpotAccountRow.id == 1)
                            .with_for_update()
                        )
                        if account is not None:
                            if (
                                account.mode != "paper"
                                or account.market_type != "spot"
                                or account.settings_version != current.version
                                or account.initial_capital_usdt != Decimal(prior.spot_capital_usdt)
                            ):
                                raise HTTPException(
                                    status_code=409, detail="PAPER account settings mismatch"
                                )
                            if (
                                session.scalar(
                                    select(PaperSpotPositionRow.id)
                                    .where(
                                        PaperSpotPositionRow.account_id == account.id,
                                        PaperSpotPositionRow.status == "open",
                                    )
                                    .limit(1)
                                )
                                is not None
                            ):
                                raise HTTPException(
                                    status_code=409,
                                    detail="Close open PAPER position before editing risk",
                                )
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
                    if account is not None:
                        # Same SQL transaction as settings + audit: no cash,
                        # position or immutable journal mutations.
                        account.settings_version = payload.expected_version + 1
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

    register_risk_capabilities_route(app, require_admin)
    register_paper_spot_read_route(app, require_admin, db_engine)
    register_paper_dashboard_route(app, require_admin, db_engine)
    # Never expose a write route in local development or generic staging.
    # Registration requires exact isolated preview ENV *and* matching config;
    # invalid or missing flags fail closed rather than enabling trade routes.
    if (
        staging_access is not None
        and environ.get("PRIVATE_PREVIEW_OPT_IN") == "isolated-paper-only"
    ):
        try:
            verify_preview_environment(environ)
        except ValueError:
            pass
        else:
            if (
                settings.database_url == environ.get("DATABASE_URL")
                and settings.staging_origin == environ.get("STAGING_WEB_ORIGIN")
                and settings.staging_host == environ.get("STAGING_API_HOST")
            ):
                register_manual_paper_route(app, require_write, db_engine)
    return app


app = create_app()
