"""Docker smoke test for local paper-only API persistence across compose down/up.

Usage:
  python3 scripts/docker_smoke.py seed
  python3 scripts/docker_smoke.py verify

This test intentionally cannot place any exchange orders.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any
from urllib.request import Request, urlopen

API = "http://127.0.0.1:8000"
TOKEN = os.environ["ADMIN_TOKEN"]


def request(path: str, *, method: str = "GET", data: dict[str, Any] | None = None) -> Any:
    body = json.dumps(data).encode("utf-8") if data is not None else None
    req = Request(
        API + path,
        method=method,
        data=body,
        headers={
            "X-Admin-Token": TOKEN,
            "Content-Type": "application/json",
        },
    )
    with urlopen(req, timeout=8) as response:
        assert response.status == 200
        return json.loads(response.read())


def main() -> None:
    if len(sys.argv) != 2 or sys.argv[1] not in {"seed", "verify"}:
        raise SystemExit("Usage: docker_smoke.py seed|verify")

    assert request("/health/ready")["status"] == "ready"
    status = request("/api/v1/status")
    assert status["execution_mode"] == "paper"
    assert status["live_orders_supported"] is False
    assert status["exchange_connected"] is False

    snapshot = request("/api/v1/settings")
    if sys.argv[1] == "seed":
        assert snapshot["version"] == 0, "Expected empty CI-only volume"
        settings = snapshot["settings"]
        settings["spot_capital_usdt"] = 1234
        result = request(
            "/api/v1/settings",
            method="PUT",
            data={"expected_version": 0, "settings": settings},
        )
        assert result["version"] == 1
        assert result["settings"]["spot_capital_usdt"] == 1234
        print("Paper settings and audit event seeded in disposable Docker DB")
    else:
        assert snapshot["version"] == 1, "Settings version missing after down/up"
        assert snapshot["settings"]["spot_capital_usdt"] == 1234
        audit = request("/api/v1/audit")
        assert len(audit["events"]) == 1
        assert audit["events"][0]["version"] == 1
        assert audit["events"][0]["action"] == "settings_updated"
        print("Verified settings and audit persisted after full Compose down/up")


if __name__ == "__main__":
    main()
