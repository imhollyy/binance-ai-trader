"""BT-015 PAPER-only state controls and one-shot public position protection.

MockTransport tests do not prove real exchange provenance or actual exits.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from typing import Any

import httpx
import pytest

from binance_ai_trader.ai_constitution import BotState
from binance_ai_trader.ai_position_protection import (
    BotControlRejected,
    BotControlSnapshot,
    ProtectionDecision,
    ProtectionAction,
    StopBehavior,
    change_paper_bot_state,
    evaluate_paper_position_protection,
    observe_public_paper_position_once,
)
from binance_ai_trader.paper_spot import PaperSpotPosition
from binance_ai_trader.public_spot_observation import PublicSpotObservationClient
from binance_ai_trader.risk_schemas import RiskSettings

D = Decimal
START = 1_800_000_180_000
NOW = START + 50


def settings(**changes: Any) -> RiskSettings:
    values: dict[str, object] = {
        "bot_enabled": True,
        "manual_approval": False,
        "spot_capital_usdt": 1000,
    }
    values.update(changes)
    return RiskSettings.model_validate(values)


def control(
    state: BotState = BotState.RUNNING,
    *,
    behavior: StopBehavior = StopBehavior.MONITOR_ONLY,
    authorized: bool = False,
    version: int = 7,
) -> BotControlSnapshot:
    return BotControlSnapshot(state, 3, version, behavior, authorized)


def position(*, stop: str = "99", symbol: str = "BTCUSDT") -> PaperSpotPosition:
    return PaperSpotPosition(
        "bt015_test_trade", symbol, D("1"), D("104"), D("104"), D("0.1"),
        D(stop), D("6"),
    )


def kline(ms: int) -> list[object]:
    return [
        ms, "100", "110", "90", "100", "20", ms + 59_999, "0", 1,
        "0", "0", "0",
    ]


def observer(
    *, price: str = "100.5", status: int = 200, damaged: bool = False,
    clock_skew: int = 0,
) -> tuple[PublicSpotObservationClient, list[str], list[int]]:
    seen: list[str] = []
    clock = [NOW]

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        assert request.url.host == "data-api.binance.vision"
        assert request.method == "GET"
        assert request.url.params["symbol"] == "BTCUSDT"
        assert "X-MBX-APIKEY" not in request.headers
        if request.url.path == "/api/v3/klines":
            rows = [kline(START - (60 - i) * 60_000) for i in range(60)]
            if damaged:
                rows[20][6] = int(rows[20][6]) + 60_000
            return httpx.Response(200, json=rows)
        assert request.url.path == "/api/v3/ticker/price"
        clock[0] += clock_skew
        return httpx.Response(status, json={"symbol": "BTCUSDT", "price": price})

    return (
        PublicSpotObservationClient(
            transport=httpx.MockTransport(handler), clock_ms=lambda: clock[0]
        ),
        seen,
        clock,
    )


def check(
    source: PublicSpotObservationClient,
    *,
    control_state: BotControlSnapshot | None = None,
    held: PaperSpotPosition | None = None,
    target: Decimal | None = None,
    version: int = 7,
    owner: RiskSettings | None = None,
) -> ProtectionDecision:
    return observe_public_paper_position_once(
        source, owner or settings(), control_state or control(), held or position(),
        settings_version=version, take_profit_price=target,
    )


def test_state_machine_is_optimistic_versioned_and_never_starts_by_default() -> None:
    original = control(BotState.STOPPED)
    with pytest.raises(BotControlRejected, match="explicit_paper_start"):
        change_paper_bot_state(
            settings(), original, expected_revision=3,
            next_state=BotState.RUNNING, operator_authorized=True,
        )
    paused = change_paper_bot_state(
        settings(), original, expected_revision=3,
        next_state=BotState.PAUSED, operator_authorized=False,
    )
    assert paused.revision == 4
    assert paused.state is BotState.PAUSED
    assert original.state is BotState.STOPPED
    with pytest.raises(BotControlRejected, match="invalid_or_stale"):
        change_paper_bot_state(
            settings(), paused, expected_revision=3,
            next_state=BotState.RUNNING, operator_authorized=True,
        )
    with pytest.raises(BotControlRejected, match="explicit_paper_start"):
        change_paper_bot_state(
            settings(), paused, expected_revision=4,
            next_state=BotState.RUNNING, operator_authorized=False,
        )
    with pytest.raises(BotControlRejected, match="explicit_paper_start"):
        change_paper_bot_state(
            settings(bot_enabled=False), paused, expected_revision=4,
            next_state=BotState.RUNNING, operator_authorized=True,
        )
    started = change_paper_bot_state(
        settings(), paused, expected_revision=4,
        next_state=BotState.RUNNING, operator_authorized=True,
    )
    assert started.revision == 5
    assert started.state is BotState.RUNNING
    with pytest.raises(BotControlRejected, match="emergency_requires_operator"):
        change_paper_bot_state(
            settings(), started, expected_revision=5,
            next_state=BotState.EMERGENCY_FLATTEN, operator_authorized=False,
        )
    emergency = change_paper_bot_state(
        settings(), started, expected_revision=5,
        next_state=BotState.EMERGENCY_FLATTEN, operator_authorized=True,
    )
    assert emergency.state is BotState.EMERGENCY_FLATTEN
    stopped = change_paper_bot_state(
        settings(), emergency, expected_revision=6,
        next_state=BotState.STOPPED, operator_authorized=False,
    )
    assert stopped.state is BotState.STOPPED


def test_public_ticker_only_alerts_stop_breach_never_closes_position() -> None:
    client, paths, _ = observer(price="98")
    before = position()
    active = control()
    decision = check(client, held=before, control_state=active)
    assert decision.action is ProtectionAction.ALERT_STOP_BREACH
    assert decision.reason_code == "STOP_BREACHED_NOT_EXECUTED"
    assert decision.stop_price == D("99")
    assert decision.observed_price == D("98")
    assert not decision.would_execute
    assert paths == ["/api/v3/klines", "/api/v3/ticker/price"]
    assert before == position()
    assert active == control()


def test_pause_requires_explicit_review_authorization_and_no_execution() -> None:
    client, _, _ = observer(price="98")
    pending = check(client, control_state=control(BotState.PAUSED))
    assert pending.action is ProtectionAction.ALERT_STOP_BREACH
    client, _, _ = observer(price="98")
    accepted = check(
        client,
        control_state=control(
            BotState.PAUSED, behavior=StopBehavior.REVIEW_REDUCE_ONLY, authorized=True
        ),
    )
    assert accepted.action is ProtectionAction.REDUCE_ONLY_REVIEW
    assert accepted.reason_code == "STOP_EXIT_REQUIRES_REVIEW"
    assert not accepted.would_execute
    client, _, _ = observer(price="98")
    not_approved = check(
        client, control_state=control(
            BotState.PAUSED, behavior=StopBehavior.REVIEW_REDUCE_ONLY
        )
    )
    assert not_approved.action is ProtectionAction.ALERT_STOP_BREACH


def test_stopped_and_emergency_exposure_do_not_pretend_exit() -> None:
    client, _, _ = observer(price="98")
    stopped = check(client, control_state=control(BotState.STOPPED))
    assert stopped.action is ProtectionAction.ALERT_STOPPED_POSITION_OPEN
    assert stopped.reason_code == "STOPPED_HAS_UNPROTECTED_EXPOSURE"
    client, _, _ = observer()
    emergency = check(client, control_state=control(BotState.EMERGENCY_FLATTEN))
    assert emergency.action is ProtectionAction.ALERT_EMERGENCY
    assert emergency.reason_code == "EMERGENCY_NOT_EXECUTED"
    client, _, _ = observer()
    authorized = check(
        client,
        control_state=control(
            BotState.EMERGENCY_FLATTEN,
            behavior=StopBehavior.REVIEW_REDUCE_ONLY, authorized=True,
        ),
    )
    assert authorized.action is ProtectionAction.REDUCE_ONLY_REVIEW
    assert authorized.reason_code == "EMERGENCY_EXIT_REQUIRES_REVIEW"


def test_take_profit_is_explicit_alert_not_marketing_pnl() -> None:
    client, _, _ = observer(price="105")
    result = check(client, target=D("105"))
    assert result.action is ProtectionAction.ALERT_TAKE_PROFIT
    assert result.reason_code == "TAKE_PROFIT_NOT_EXECUTED"
    assert not result.would_execute
    client, _, _ = observer(price="100")
    watching = check(client, target=D("106"))
    assert watching.action is ProtectionAction.MONITORING


@pytest.mark.parametrize(
    ("source_kwargs", "reason"),
    [
        ({"status": 429}, "PUBLIC_QUOTE_UNAVAILABLE"),
        ({"status": 503}, "PUBLIC_QUOTE_UNAVAILABLE"),
        ({"price": "NaN"}, "PUBLIC_QUOTE_UNAVAILABLE"),
        ({"damaged": True}, "PUBLIC_QUOTE_UNAVAILABLE"),
        ({"clock_skew": 4000}, "PUBLIC_QUOTE_UNAVAILABLE"),
    ],
)
def test_bad_or_rate_limited_public_source_never_emits_an_exit(
    source_kwargs: dict[str, Any], reason: str,
) -> None:
    client, _, _ = observer(**source_kwargs)
    denied = check(client)
    assert denied.action is ProtectionAction.OBSERVATION_BLOCKED
    assert denied.reason_code == reason


def test_freshness_provenance_and_version_fail_closed() -> None:
    client, _, clock = observer(price="98")
    sample = client.observe(symbol="BTCUSDT", interval="1m", limit=50)
    clock[0] += 4_000
    stale = evaluate_paper_position_protection(
        settings(), control(), position(), sample,
        settings_version=7, now_ms=clock[0],
    )
    assert stale.action is ProtectionAction.OBSERVATION_BLOCKED
    assert stale.reason_code == "PUBLIC_QUOTE_STALE_OR_INVALID"

    for bad in (
        replace(sample, source="model_claimed_provenance"),
        replace(sample, symbol="ETHUSDT"),
        replace(sample, reference_price=D("500")),
    ):
        result = evaluate_paper_position_protection(
            settings(), control(), position(), bad,
            settings_version=7, now_ms=NOW,
        )
        assert result.action is ProtectionAction.OBSERVATION_BLOCKED

    client, _, _ = observer()
    mismatch = check(client, version=8)
    assert mismatch.action is ProtectionAction.OBSERVATION_BLOCKED
    assert mismatch.reason_code == "PAPER_CONTROL_OR_SETTINGS_MISMATCH"
    client, _, _ = observer()
    invalid_target = check(client, target=D("104"))
    assert invalid_target.reason_code == "INVALID_POSITION_OR_TARGET"


def test_no_position_skips_exchange_and_untrusted_adapter_is_rejected() -> None:
    client, paths, _ = observer()
    no_open = observe_public_paper_position_once(
        client, settings(), control(), None, settings_version=7
    )
    assert no_open.action is ProtectionAction.NO_POSITION
    assert paths == []
    denied = observe_public_paper_position_once(
        object(), settings(), control(), position(), settings_version=7  # type: ignore[arg-type]
    )
    assert denied.reason_code == "TRUSTED_PUBLIC_OBSERVER_REQUIRED"
    assert paths == []
