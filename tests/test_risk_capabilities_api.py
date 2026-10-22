"""BT-027A capability discovery must never silently enable risk profiles."""

from fastapi.testclient import TestClient

from binance_ai_trader.api import create_app
from binance_ai_trader.api_config import ApiConfig


def test_requires_local_admin_token() -> None:
    app = create_app(config=ApiConfig(database_url=None, admin_token="a" * 40))
    with TestClient(app) as client:
        result = client.get("/api/v1/risk/profiles/capabilities")
    assert result.status_code == 401


def test_risk_capabilities_fail_closed_even_without_database() -> None:
    app = create_app(config=ApiConfig(database_url=None, admin_token="a" * 40))
    with TestClient(app) as client:
        result = client.get(
            "/api/v1/risk/profiles/capabilities",
            headers={"X-Admin-Token": "a" * 40},
        )
    assert result.status_code == 200
    assert result.headers["cache-control"] == "no-store, private"
    payload = result.json()
    assert payload["available"] is False
    assert payload["selectable"] is False
    assert payload["active_profile_version"] is None
    assert payload["active_risk_level"] is None
    assert payload["allowed_levels"] == [1, 2, 3, 4, 5]
