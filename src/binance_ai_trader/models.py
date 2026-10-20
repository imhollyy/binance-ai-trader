"""Database tables for paper settings and audit history."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from decimal import Decimal

from sqlalchemy import JSON, BigInteger, CheckConstraint, DateTime, ForeignKey, Integer, Numeric, String, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class TradingSettingsRow(Base):
    __tablename__ = "trading_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    data: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    modified_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )


class AuditRow(Base):
    __tablename__ = "settings_audit"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    settings_id: Mapped[int] = mapped_column(ForeignKey("trading_settings.id"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    action: Mapped[str] = mapped_column(String(40), nullable=False)
    data: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )


class SpotCandleRow(Base):
    """Closed public Spot candles, deduplicated by their opening millisecond."""

    __tablename__ = "spot_candles"
    __table_args__ = (
        UniqueConstraint(
            "symbol", "interval", "open_time_ms", name="uq_spot_candles_symbol_interval_open"
        ),
        CheckConstraint("volume >= 0", name="ck_spot_candles_nonnegative_volume"),
        CheckConstraint("high >= low", name="ck_spot_candles_price_bounds"),
        CheckConstraint("open > 0 AND close > 0", name="ck_spot_candles_positive_prices"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    interval: Mapped[str] = mapped_column(String(5), nullable=False)
    open_time_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    close_time_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    open: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    high: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    low: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    close: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    volume: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
