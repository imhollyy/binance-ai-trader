from __future__ import annotations

from pathlib import Path

import pytest

from scripts.prepare_review_preview import _read_env, prepare


def test_review_preview_writes_only_verifier_and_preserves_identity_on_origin_change(
    tmp_path: Path,
) -> None:
    password = "local-review-password-with-more-than-16-characters"
    env_path = tmp_path / ".env.preview.review"

    assert prepare("https://first-review.trycloudflare.com", env_path, password)
    initial = _read_env(env_path)
    stored = env_path.read_text(encoding="utf-8")

    assert password not in stored
    assert initial["STAGING_DATABASE_URL"].endswith("@preview-db:5432/trader_preview")
    assert initial["PREVIEW_INGRESS_BIND"] == "127.0.0.1:8877:80"
    assert initial["STAGING_VIEWER_PASSWORD_HASH"].startswith("scrypt_16384:")
    assert initial["STAGING_OPERATOR_PASSWORD_HASH"] != initial["STAGING_VIEWER_PASSWORD_HASH"]

    assert not prepare("https://second-review.trycloudflare.com", env_path)
    rotated = _read_env(env_path)
    assert rotated["STAGING_POSTGRES_PASSWORD"] == initial["STAGING_POSTGRES_PASSWORD"]
    assert rotated["STAGING_VIEWER_PASSWORD_HASH"] == initial["STAGING_VIEWER_PASSWORD_HASH"]
    assert rotated["PREVIEW_HOST"] == "second-review.trycloudflare.com"


@pytest.mark.parametrize(
    "origin",
    (
        "http://not-tls.trycloudflare.com",
        "https://wrong.example",
        "https://user@review.trycloudflare.com",
        "https://review.trycloudflare.com/path",
        "https://review.trycloudflare.com?token=not-allowed",
    ),
)
def test_review_preview_rejects_untrusted_or_non_exact_origin(tmp_path: Path, origin: str) -> None:
    with pytest.raises(ValueError):
        prepare(origin, tmp_path / ".env.preview.review", "local-review-password-with-more-than-16")


def test_review_preview_rejects_tampered_database_target(tmp_path: Path) -> None:
    env_path = tmp_path / ".env.preview.review"
    prepare(
        "https://review.trycloudflare.com",
        env_path,
        "local-review-password-with-more-than-16",
    )
    env_path.write_text(
        env_path.read_text(encoding="utf-8").replace(
            "@preview-db:5432/trader_preview", "@production-db:5432/trader_preview"
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="isolated service"):
        _read_env(env_path)


def test_review_preview_only_accepts_its_own_ignored_environment_filename(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="ignored .env.preview.review"):
        prepare(
            "https://review.trycloudflare.com",
            tmp_path / ".env.preview.quick",
            "local-review-password-with-more-than-16",
        )


def test_review_proxy_trust_matches_isolated_ingress_network() -> None:
    root = Path(__file__).resolve().parents[1]
    base = (root / "compose.preview.yaml").read_text(encoding="utf-8")
    review = (root / "compose.preview.review.yaml").read_text(encoding="utf-8")

    # The review overlay replaces the base command: only its own ingress may
    # assert X-Forwarded-Proto=https for staging auth behind Cloudflare PIN.
    assert "--forwarded-allow-ips 172.30.210.10" in base
    assert "ipv4_address: 172.30.220.10" in review
    assert "--proxy-headers --forwarded-allow-ips 172.30.220.10" in review
    assert "--forwarded-allow-ips 172.30.210.10" not in review


def test_existing_review_resume_requires_isolation_and_https_session() -> None:
    # Static safety check. The Windows owner/PIN/browser roundtrip is a separate gate.
    root = Path(__file__).resolve().parents[1]
    script = (root / "scripts/Resume-TradePilotReviewPreview.ps1").read_text(encoding="utf-8")
    assert "tradepilot_bt027_review" in script
    assert "Compare-Object -ReferenceObject $expectedContainers -DifferenceObject $actual" in script
    assert '$expectedVolume = $project + "_preview_postgres_data"' in script
    assert "--allowed-mail" in script
    assert "up --build --no-deps -d preview-api preview-web preview-ingress" in script
    assert "/api/v1/auth/session" in script
    assert '"401"' in script
    assert "docker compose @compose config --quiet" in script
    for forbidden in (
        "down -v",
        "volume prune",
        "docker rm",
        "docker volume rm",
        "preview_account_bootstrap",
    ):
        assert forbidden not in script
