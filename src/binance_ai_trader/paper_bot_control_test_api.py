"""BT-017 isolated operator-authenticated PAPER bot-control HTTP contract.

NOT a production feature: routes exist only when create_app verifies the
disposable localhost trader_bot_controls_http_test DB + explicit test sentinel.
No private Binance credentials, orders, scheduler, worker or owner DB writes.
"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError

from binance_ai_trader.ai_constitution import BotState
from binance_ai_trader.ai_paper_bot_controls_test import (
    is_disposable_control_http_test,
    read_disposable_bot_control,
    update_disposable_bot_control,
)
from binance_ai_trader.ai_position_protection import (
    BotControlRejected,
    BotControlSnapshot,
    StopBehavior,
)
from binance_ai_trader.staging_auth import StagingAccess

EXPLICIT_TEST_APPROVAL = "ISOLATED_BOT_CONTROL_REVIEW_ONLY"


class ControlSnapshotResponse(BaseModel):
    state: BotState
    revision: int
    settings_version: int
    stop_behavior: StopBehavior
    operator_confirmed_reduce_only: bool

    @classmethod
    def from_snapshot(cls, value: BotControlSnapshot) -> ControlSnapshotResponse:
        return cls(
            state=value.state,
            revision=value.revision,
            settings_version=value.settings_version,
            stop_behavior=value.stop_behavior,
            operator_confirmed_reduce_only=value.operator_confirmed_reduce_only,
        )


class ChangeControlRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    expected_revision: int = Field(ge=1)
    next_state: BotState
    stop_behavior: StopBehavior
    confirm_reduce_only_review: bool


def register_bot_control_test_routes(
    app: FastAPI, access: StagingAccess, engine: Engine
) -> None:
    """Register after *server-side* isolated-db eligibility has been checked.

    _current checks the genuine cookie using the verifier-bound hash,
    HTTPS, exact Origin, and (for writes) CSRF before passing the resulting
    session row's hash to the atomic service. Caller cannot inject a digest.
    """
    if not is_disposable_control_http_test(engine) or access.config.auth_mode != "staging":
        raise ValueError("isolated_staging_bot_control_only")

    @app.get(
        "/api/v1/paper/bot-control",
        response_model=ControlSnapshotResponse,
    )
    def get_bot_control(request: Request, response: Response) -> ControlSnapshotResponse:
        access._current(request)
        response.headers["Cache-Control"] = "no-store, private"
        try:
            result = read_disposable_bot_control(engine)
        except BotControlRejected as exc:
            raise HTTPException(503, "Bot control unavailable") from exc
        except SQLAlchemyError as exc:
            raise HTTPException(503, "Bot control database unavailable") from exc
        if result is None:
            raise HTTPException(409, "Bot control not initialized")
        return ControlSnapshotResponse.from_snapshot(result)

    @app.put(
        "/api/v1/paper/bot-control",
        response_model=ControlSnapshotResponse,
    )
    def put_bot_control(
        request: Request,
        response: Response,
        body: ChangeControlRequest,
    ) -> ControlSnapshotResponse:
        # Authenticate real cookie + strict Origin + CSRF before touching
        # durable bot state; an untrusted actor_session_hash is never accepted.
        actor = access._current(request, require_csrf=True)
        if (
            actor.role != "operator"
            or request.headers.get("x-paper-bot-control-approval") != EXPLICIT_TEST_APPROVAL
        ):
            raise HTTPException(403, "Additional bot-control approval required")
        response.headers["Cache-Control"] = "no-store, private"
        try:
            result = update_disposable_bot_control(
                engine,
                actor_session_hash=actor.token_hash,
                expected_revision=body.expected_revision,
                next_state=body.next_state,
                stop_behavior=body.stop_behavior,
                confirm_reduce_only_review=body.confirm_reduce_only_review,
            )
        except BotControlRejected as exc:
            raise HTTPException(409, "Bot control change rejected") from exc
        except SQLAlchemyError as exc:
            raise HTTPException(503, "Bot control database unavailable") from exc
        return ControlSnapshotResponse.from_snapshot(result)
