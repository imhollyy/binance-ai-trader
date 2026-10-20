"""Validated, PAPER-ONLY strategy and capital configuration."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class RiskSettings(BaseModel):
    """Editable limits are deliberately bounded; no live execution engine exists."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    mode: Literal["paper"] = "paper"
    market_type: Literal["spot", "futures"] = "spot"
    bot_enabled: bool = False
    spot_capital_usdt: int = Field(default=1000, ge=0, le=1_000_000)
    futures_capital_usdt: int = Field(default=0, ge=0, le=1_000_000)
    risk_per_trade_pct: float = Field(default=0.25, ge=0.05, le=2)
    daily_loss_limit_pct: float = Field(default=1.5, ge=0.1, le=5)
    max_drawdown_pct: float = Field(default=5, ge=1, le=15)
    max_open_positions: int = Field(default=3, ge=1, le=10)
    max_leverage: int = Field(default=1, ge=1, le=5)
    stop_loss_required: Literal[True] = True
    manual_approval: bool = True
    allowed_symbols: list[str] = Field(
        default_factory=lambda: ["BTCUSDT", "ETHUSDT"], min_length=1, max_length=10
    )

    @field_validator("allowed_symbols")
    @classmethod
    def check_symbols(cls, symbols: list[str]) -> list[str]:
        if len(set(symbols)) != len(symbols):
            raise ValueError("Symbols must not repeat")
        if any(not s.isascii() or not s.isalnum() or s != s.upper() for s in symbols):
            raise ValueError("Symbols must be uppercase ASCII alphanumeric")
        if any(len(s) < 5 or len(s) > 20 for s in symbols):
            raise ValueError("Symbol length must be 5..20")
        return symbols

    @model_validator(mode="after")
    def check_mode(self) -> RiskSettings:
        if self.market_type == "spot" and self.max_leverage != 1:
            raise ValueError("Spot mode cannot use leverage")
        if self.bot_enabled and self.market_type == "spot" and self.spot_capital_usdt == 0:
            raise ValueError("Active paper spot simulation needs nonzero spot capital")
        if self.bot_enabled and self.market_type == "futures" and self.futures_capital_usdt == 0:
            raise ValueError("Active paper futures simulation needs nonzero futures capital")
        return self


class SettingsSnapshot(BaseModel):
    version: int = Field(ge=0)
    settings: RiskSettings


class SettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=0)
    settings: RiskSettings
