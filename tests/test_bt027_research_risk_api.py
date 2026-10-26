"""BT-027 authenticated API boundaries with both isolated stores absent."""

from fastapi.testclient import TestClient

from binance_ai_trader.api import create_app
from binance_ai_trader.api_config import ApiConfig

TOKEN = "r" * 40


def _client() -> TestClient:
    return TestClient(create_app(config=ApiConfig(database_url=None, admin_token=TOKEN)))


def test_risk_profile_api_requires_auth_and_reports_unconfigured_fail_closed() -> None:
    with _client() as client:
        denied = client.get("/api/v1/risk/profiles")
        available = client.get("/api/v1/risk/profiles", headers={"X-Admin-Token": TOKEN})
        capabilities = client.get(
            "/api/v1/risk/profiles/capabilities", headers={"X-Admin-Token": TOKEN}
        )
    assert denied.status_code == 401
    assert available.status_code == 200
    assert available.json()["available"] is False
    assert available.json()["selectable"] is False
    assert capabilities.json()["available"] is False
    assert capabilities.json()["active_profile_version"] is None


def test_risk_profile_write_requires_explicit_approval_and_isolated_store() -> None:
    payload = {
        "expected_revision": 0,
        "risk_level": 1,
        "max_positions": 1,
        "risk_per_trade_pct": "1",
        "max_open_capital": "100",
        "spot_budget": "100",
        "daily_loss_pct": "2",
        "max_drawdown_pct": "5",
        "max_consecutive_losses": 2,
        "user_hard_limits": {},
        "user_confirmed": True,
    }
    with _client() as client:
        missing = client.put(
            "/api/v1/risk/profiles", json=payload, headers={"X-Admin-Token": TOKEN}
        )
        without_user_confirmation = client.put(
            "/api/v1/risk/profiles",
            json={**payload, "user_confirmed": False},
            headers={
                "X-Admin-Token": TOKEN,
                "X-Risk-Profile-Approval": "I_CONFIRM_RISK_LIMITS",
            },
        )
    assert missing.status_code == 403
    assert without_user_confirmation.status_code == 422


def test_ai_research_api_is_authenticated_and_returns_empty_unavailable_contract() -> None:
    with _client() as client:
        denied = client.get("/api/v1/ai/research/predictions")
        response = client.get("/api/v1/ai/research/predictions", headers={"X-Admin-Token": TOKEN})
    assert denied.status_code == 401
    assert response.status_code == 200
    body = response.json()
    assert body["available"] is False
    assert body["status"] == "unavailable"
    assert body["predictions"] == []
    assert body["metrics_available"] is False
