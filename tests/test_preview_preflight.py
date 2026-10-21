"""Private preview MUST reject local admin, unrelated DB or unsafe host settings."""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from binance_ai_trader.preview_preflight import verify_preview_environment
from binance_ai_trader.staging_auth import password_hash

WEB = "https://preview.staging.example"
DB = "postgresql+psycopg://trader_preview:synthetic@preview-db:5432/trader_preview"


@pytest.fixture(scope="module")
def synthetic_env() -> Mapping[str, str]:
    return {
        "PRIVATE_PREVIEW_OPT_IN": "isolated-paper-only",
        "TRADING_MODE": "paper",
        "API_AUTH_MODE": "staging",
        "DATABASE_URL": DB,
        "STAGING_WEB_ORIGIN": WEB,
        "STAGING_API_HOST": "preview.staging.example",
        "STAGING_VIEWER_PASSWORD_HASH": password_hash(
            "synthetic-viewer-2026-isolated",
            salt=b"preview-view-001",
        ),
        "STAGING_OPERATOR_PASSWORD_HASH": password_hash(
            "synthetic-operator-2026-isolated",
            salt=b"preview-oper-001",
        ),
    }


def test_exact_isolated_private_preview_passes(synthetic_env: Mapping[str, str]) -> None:
    verify_preview_environment(synthetic_env)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("PRIVATE_PREVIEW_OPT_IN", ""),
        ("TRADING_MODE", "live"),
        ("API_AUTH_MODE", "local"),
        ("DATABASE_URL", "postgresql+psycopg://trader_preview:pw@localhost:5432/trader_preview"),
        ("DATABASE_URL", "postgresql+psycopg://trader_preview:pw@preview-db:5432/other"),
        ("DATABASE_URL", "postgresql+psycopg://trader:pw@preview-db:5432/trader_preview"),
        ("STAGING_WEB_ORIGIN", "http://preview.staging.example"),
        ("STAGING_WEB_ORIGIN", "https://different.staging.example"),
        ("STAGING_API_HOST", "*.staging.example"),
        ("PAPER_SCHEDULER_OPT_IN", "enabled"),
        ("PAPER_WORKER_ENABLED", "true"),
    ),
)
def test_preview_denies_any_unsafe_override(
    synthetic_env: Mapping[str, str], field: str, value: str
) -> None:
    env = {**synthetic_env, field: value}
    with pytest.raises(ValueError) as error:
        verify_preview_environment(env)
    assert DB not in str(error.value)
    assert "synthetic" not in str(error.value)


def test_preview_rejects_any_local_token_even_if_blank(
    synthetic_env: Mapping[str, str],
) -> None:
    with pytest.raises(ValueError, match="never a local admin token"):
        verify_preview_environment({**synthetic_env, "ADMIN_TOKEN": ""})


def test_preview_never_prints_database_url_when_rejected(
    synthetic_env: Mapping[str, str],
) -> None:
    url = "postgresql+psycopg://trader_preview:SECRET_SENTINEL@prod-db:5432/trader_preview"
    with pytest.raises(ValueError) as error:
        verify_preview_environment({**synthetic_env, "DATABASE_URL": url})
    assert "SECRET_SENTINEL" not in str(error.value)
