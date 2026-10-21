"""Protected Quick Tunnel env bootstrap: no plaintext persistence or outside host."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.prepare_quick_preview import prepare


def test_quick_bootstrap_preserves_only_hashes_when_host_changes(tmp_path: Path) -> None:
    env_file = tmp_path / ".env.preview.quick"
    first = "https://first-synthetic.trycloudflare.com"
    second = "https://second-synthetic.trycloudflare.com"
    assert prepare(
        first, env_file, viewer="synthetic-viewer-password-2026",
        operator="synthetic-operator-password-2026",
    )
    text = env_file.read_text()
    assert "synthetic-viewer-password" not in text
    assert "synthetic-operator-password" not in text
    assert "STAGING_WEB_ORIGIN=" + first in text
    assert "PREVIEW_INGRESS_BIND=127.0.0.1:8765:80" in text
    original_secret_lines = [
        line for line in text.splitlines()
        if "PASSWORD_HASH=" in line or line.startswith("STAGING_POSTGRES_PASSWORD=")
    ]
    assert len(original_secret_lines) == 3

    assert not prepare(second, env_file)
    changed = env_file.read_text()
    assert "STAGING_WEB_ORIGIN=" + second in changed
    assert first not in changed
    for line in original_secret_lines:
        assert line in changed


@pytest.mark.parametrize(
    "origin",
    [
        "http://bad.trycloudflare.com",
        "https://bad.example.com",
        "https://user@bad.trycloudflare.com",
        "https://bad.trycloudflare.com:8443",
        "https://bad.trycloudflare.com/prefix",
        "https://bad.trycloudflare.com?secret=1",
        "https://bad.trycloudflare.com#frag",
        "https://BAD.trycloudflare.com",
    ],
)
def test_quick_bootstrap_rejects_unknown_hostname(
    tmp_path: Path, origin: str,
) -> None:
    path = tmp_path / ".env.preview.quick"
    with pytest.raises(ValueError, match="exact HTTPS"):
        prepare(
            origin, path, viewer="synthetic-viewer-password-2026",
            operator="synthetic-operator-password-2026",
        )
    assert not path.exists()


def test_quick_bootstrap_rejects_reusing_nonisolated_file(tmp_path: Path) -> None:
    path = tmp_path / ".env.preview.quick"
    path.write_text("ADMIN_TOKEN=UNSAFE\n")
    with pytest.raises(ValueError, match="Unexpected"):
        prepare("https://safe.trycloudflare.com", path)
    assert path.read_text() == "ADMIN_TOKEN=UNSAFE\n"


def test_quick_bootstrap_does_not_accept_same_password(tmp_path: Path) -> None:
    path = tmp_path / ".env.preview.quick"
    with pytest.raises(ValueError, match="distinct"):
        prepare(
            "https://private.trycloudflare.com", path,
            viewer="synthetic-same-password-2026",
            operator="synthetic-same-password-2026",
        )
    assert not path.exists()
