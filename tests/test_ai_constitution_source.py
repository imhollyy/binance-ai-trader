"""The original user-approved TradePilot constitution is immutable in v1."""

from __future__ import annotations

from pathlib import Path


def test_original_constitution_is_preserved_without_editorial_rewrites() -> None:
    source = (
        Path(__file__).resolve().parents[1] / "docs" / "AI_TRADING_CONSTITUTION_V1.md"
    ).read_bytes()
    assert len(source) == 23_043
    assert source.count(b"\r\n") == 1_161
    assert source.startswith("SİSTEM ROLÜ\r\n".encode())
    assert source.endswith("altında maksimize etmektir.\r\n".encode())
    full = source.decode("utf-8")
    assert "USER HARD LIMITS" in full
    assert "EMERGENCY_FLATTEN" in full
    assert "EXPECTED VALUE" in full
    assert "NO_TRADE" in full
    assert "FUTURES_USDT_M" in full
    assert "CONTROLLED DRAWDOWN" in full
