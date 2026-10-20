"""Fail-closed runtime configuration for the BT-001 research scaffold."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from os import environ


class RunMode(StrEnum):
    """Only paper mode is legal in this milestone."""

    PAPER = "paper"


@dataclass(frozen=True, slots=True)
class Settings:
    """Immutable application settings with no credential fields."""

    mode: RunMode = RunMode.PAPER

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        """Reject attempts to enable real-money trading, even by accident."""

        values = environ if env is None else env
        requested_mode = values.get("TRADING_MODE", "paper").strip().lower()
        if requested_mode != RunMode.PAPER.value:
            raise ValueError(
                "Only TRADING_MODE=paper is allowed in BT-001; live trading is disabled."
            )
        return cls(mode=RunMode.PAPER)
