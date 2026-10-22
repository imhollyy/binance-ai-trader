"""Fail-closed public shape for the independent research RiskGuard boundary.

The application database is not the dedicated BT-024 research store. This route
deliberately never reads or mutates research-store state or implies an active profile.
"""

from __future__ import annotations

from collections.abc import Callable

from fastapi import Depends, FastAPI, Response
from pydantic import BaseModel


class RiskProfileCapabilities(BaseModel):
    source: str = "application_api"
    profile_source: str = "dedicated_isolated_research_store"
    available: bool = False
    active_profile_version: int | None = None
    active_risk_level: int | None = None
    allowed_levels: tuple[int, ...] = (1, 2, 3, 4, 5)
    selectable: bool = False
    reason: str = "risk_profile_application_contract_not_implemented"


def register_risk_capabilities_route(app: FastAPI, require_admin: Callable[..., None]) -> None:
    """Authenticated capability discovery only; never install/activate profiles."""

    @app.get(
        "/api/v1/risk/profiles/capabilities",
        response_model=RiskProfileCapabilities,
        dependencies=[Depends(require_admin)],
    )
    def risk_profile_capabilities(response: Response) -> RiskProfileCapabilities:
        response.headers["Cache-Control"] = "no-store, private"
        response.headers["Pragma"] = "no-cache"
        return RiskProfileCapabilities()
