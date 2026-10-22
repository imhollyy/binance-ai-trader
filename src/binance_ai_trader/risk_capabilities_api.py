"""Fail-closed public shape for the independent research RiskGuard boundary.

The application database is not the dedicated BT-024 research store. This route
deliberately never reads or mutates research-store state or implies an active profile.
"""

from __future__ import annotations

from collections.abc import Callable

from fastapi import Depends, FastAPI, Response
from pydantic import BaseModel
from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError

from binance_ai_trader.paper_supervisor import read_worker_status
from binance_ai_trader.risk_store import RiskStoreRejected, active_profile


class RiskProfileCapabilities(BaseModel):
    source: str = "application_api"
    profile_source: str = "dedicated_isolated_research_store"
    available: bool = False
    active_profile_version: int | None = None
    active_risk_level: int | None = None
    allowed_levels: tuple[int, ...] = (1, 2, 3, 4, 5)
    selectable: bool = False
    reason: str = "risk_profile_application_contract_not_implemented"


def register_risk_capabilities_route(
    app: FastAPI,
    require_admin: Callable[..., None],
    risk_engine: Engine | None,
    worker_engine: Engine | None,
) -> None:
    """Authenticated capability discovery only; never install/activate profiles."""

    @app.get(
        "/api/v1/risk/profiles/capabilities",
        response_model=RiskProfileCapabilities,
        dependencies=[Depends(require_admin)],
    )
    def risk_profile_capabilities(response: Response) -> RiskProfileCapabilities:
        response.headers["Cache-Control"] = "no-store, private"
        response.headers["Pragma"] = "no-cache"
        if risk_engine is None:
            return RiskProfileCapabilities()
        try:
            active = active_profile(risk_engine)
        except (RiskStoreRejected, SQLAlchemyError):
            return RiskProfileCapabilities(reason="isolated_risk_store_unavailable")
        if active is None:
            try:
                selectable = (
                    worker_engine is not None
                    and read_worker_status(worker_engine).state == "STOPPED"
                )
            except SQLAlchemyError:
                selectable = False
            return RiskProfileCapabilities(
                available=True,
                selectable=selectable,
                reason="no_user_confirmed_profile_installed",
            )
        profile, state, _revision = active
        try:
            worker_stopped = (
                worker_engine is not None and read_worker_status(worker_engine).state == "STOPPED"
            )
        except SQLAlchemyError:
            worker_stopped = False
        selectable = state.value == "STOPPED" and worker_stopped
        return RiskProfileCapabilities(
            available=True,
            active_profile_version=profile.profile_version,
            active_risk_level=profile.risk_level,
            selectable=selectable,
            reason="ready" if selectable else "profile_changes_require_stopped_bot",
        )
