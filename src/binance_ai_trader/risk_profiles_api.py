"""User-confirmed RiskGuard profile management over its isolated store."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from decimal import Decimal
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError

from binance_ai_trader.ai_constitution import BotState
from binance_ai_trader.paper_supervisor import LOCK_ID, read_worker_status
from binance_ai_trader.risk_guard import RiskProfile
from binance_ai_trader.risk_store import (
    RiskStoreRejected,
    active_profile,
    install_profile,
)


class ProfilePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=0)
    risk_level: int = Field(ge=1, le=5)
    max_positions: int = Field(gt=0)
    risk_per_trade_pct: Decimal = Field(gt=0, le=100)
    max_open_capital: Decimal = Field(gt=0)
    spot_budget: Decimal = Field(gt=0)
    daily_loss_pct: Decimal = Field(gt=0, le=100)
    max_drawdown_pct: Decimal = Field(gt=0, le=100)
    max_consecutive_losses: int = Field(gt=0)
    user_hard_limits: dict[str, Decimal] = Field(default_factory=dict)
    user_confirmed: Literal[True]


class ProfileView(BaseModel):
    available: bool
    selectable: bool
    reason: str
    active_profile_version: int | None
    active_risk_level: int | None
    runtime_revision: int | None
    bot_state: str
    schema_version: str | None = None
    limits: dict[str, object] | None = None


def register_risk_profiles_route(
    app: FastAPI,
    require_admin: Callable[..., None],
    require_write: Callable[..., None],
    risk_engine: Engine | None,
    worker_engine: Engine | None,
) -> None:
    """Expose only profile data; never changes bot state or execution authority."""

    def read_view(response: Response) -> ProfileView:
        response.headers["Cache-Control"] = "no-store, private"
        response.headers["Pragma"] = "no-cache"
        if risk_engine is None:
            return ProfileView(
                available=False,
                selectable=False,
                reason="isolated_risk_store_not_configured",
                active_profile_version=None,
                active_risk_level=None,
                runtime_revision=None,
                bot_state=BotState.STOPPED.value,
            )
        try:
            current = active_profile(risk_engine)
        except (RiskStoreRejected, SQLAlchemyError) as exc:
            raise HTTPException(status_code=503, detail="Risk profile store unavailable") from exc
        if current is None:
            if worker_engine is None:
                selectable = False
                reason = "application_worker_status_unavailable"
            else:
                try:
                    worker = read_worker_status(worker_engine)
                    selectable = worker.state == "STOPPED" and not worker.verified_running
                    reason = (
                        "no_user_confirmed_profile_installed"
                        if selectable
                        else "worker_not_stopped"
                    )
                except SQLAlchemyError:
                    selectable = False
                    reason = "application_worker_status_unavailable"
            return ProfileView(
                available=True,
                selectable=selectable,
                reason=reason,
                active_profile_version=None,
                active_risk_level=None,
                runtime_revision=0,
                bot_state=BotState.STOPPED.value,
            )
        profile, state, revision = current
        try:
            worker_stopped = (
                worker_engine is not None and read_worker_status(worker_engine).state == "STOPPED"
            )
        except SQLAlchemyError:
            worker_stopped = False
        selectable = state is BotState.STOPPED and worker_stopped
        return ProfileView(
            available=True,
            selectable=selectable,
            reason="ready" if selectable else "profile_changes_require_stopped_bot",
            active_profile_version=profile.profile_version,
            active_risk_level=profile.risk_level,
            runtime_revision=revision,
            bot_state=state.value,
            schema_version=profile.schema_version,
            limits={
                "max_positions": profile.max_positions,
                "risk_per_trade_pct": profile.risk_per_trade_pct,
                "max_open_capital": profile.max_open_capital,
                "spot_budget": profile.spot_budget,
                "daily_loss_pct": profile.daily_loss_pct,
                "max_drawdown_pct": profile.max_drawdown_pct,
                "max_consecutive_losses": profile.max_consecutive_losses,
                "user_hard_limits": dict(profile.user_hard_limits),
            },
        )

    @app.get(
        "/api/v1/risk/profiles",
        response_model=ProfileView,
        dependencies=[Depends(require_admin)],
    )
    def get_risk_profile(response: Response) -> ProfileView:
        return read_view(response)

    @app.put(
        "/api/v1/risk/profiles",
        response_model=ProfileView,
        dependencies=[Depends(require_write)],
    )
    def put_risk_profile(
        payload: ProfilePayload,
        response: Response,
        x_risk_profile_approval: str | None = Header(default=None),
        x_admin_token: str | None = Header(default=None),
    ) -> ProfileView:
        if x_risk_profile_approval != "I_CONFIRM_RISK_LIMITS":
            raise HTTPException(status_code=403, detail="Explicit risk profile approval required")
        if risk_engine is None:
            raise HTTPException(status_code=503, detail="Isolated risk profile store unavailable")
        if worker_engine is None:
            raise HTTPException(status_code=503, detail="Worker status unavailable")
        try:
            # Serialize profile edits against worker claims. Both operations take
            # this transaction-scoped lock, so a STOPPED check cannot race a claim.
            with worker_engine.begin() as worker_connection:
                worker_connection.execute(
                    text("SELECT pg_advisory_xact_lock(:id)"), {"id": LOCK_ID}
                )
                worker = read_worker_status(worker_engine)
                if worker.state != "STOPPED" or worker.verified_running:
                    raise HTTPException(
                        status_code=409, detail="Risk profile changes require a stopped worker"
                    )
                current = active_profile(risk_engine)
                version = 1 if current is None else current[0].profile_version + 1
                profile = RiskProfile(
                    schema_version="1.0",
                    profile_version=version,
                    risk_level=payload.risk_level,
                    max_positions=payload.max_positions,
                    risk_per_trade_pct=payload.risk_per_trade_pct,
                    max_open_capital=payload.max_open_capital,
                    spot_budget=payload.spot_budget,
                    daily_loss_pct=payload.daily_loss_pct,
                    max_drawdown_pct=payload.max_drawdown_pct,
                    max_consecutive_losses=payload.max_consecutive_losses,
                    user_hard_limits=tuple(sorted(payload.user_hard_limits.items())),
                )
                install_profile(
                    risk_engine,
                    profile,
                    user_confirmed=payload.user_confirmed,
                    expected_runtime_revision=payload.expected_revision,
                    actor_sha256=(
                        hashlib.sha256(x_admin_token.encode("utf-8")).hexdigest()
                        if x_admin_token is not None
                        else None
                    ),
                    state=BotState.STOPPED,
                )
        except RiskStoreRejected as exc:
            raise HTTPException(
                status_code=409, detail="Risk profile rejected; reload current profile"
            ) from exc
        except SQLAlchemyError as exc:
            raise HTTPException(status_code=503, detail="Risk profile store unavailable") from exc
        return read_view(response)
