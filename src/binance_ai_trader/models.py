"""Database tables for paper settings and audit history."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
    text,
)
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


class PaperSpotAccountRow(Base):
    """The single local PAPER Spot account; never an exchange balance."""

    __tablename__ = "paper_spot_accounts"
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_paper_spot_accounts_singleton"),
        CheckConstraint(
            "mode = 'paper' AND market_type = 'spot'", name="ck_paper_spot_accounts_mode"
        ),
        CheckConstraint("settings_version >= 1", name="ck_paper_spot_accounts_version"),
        CheckConstraint(
            "initial_capital_usdt > 0 AND initial_capital_usdt <= 1000000 "
            "AND initial_capital_usdt <> 'NaN'::numeric",
            name="ck_paper_spot_accounts_initial_capital",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "cash_usdt >= 0 AND cash_usdt <> 'NaN'::numeric",
            name="ck_paper_spot_accounts_cash",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "realized_pnl_usdt <> 'NaN'::numeric", name="ck_paper_spot_accounts_pnl"
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "paid_fees_usdt >= 0 AND paid_fees_usdt <> 'NaN'::numeric",
            name="ck_paper_spot_accounts_fees",
        ).ddl_if(dialect="postgresql"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    mode: Mapped[str] = mapped_column(String(10), nullable=False, server_default="paper")
    market_type: Mapped[str] = mapped_column(String(10), nullable=False, server_default="spot")
    settings_version: Mapped[int] = mapped_column(Integer, nullable=False)
    initial_capital_usdt: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    cash_usdt: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    realized_pnl_usdt: Mapped[Decimal] = mapped_column(
        Numeric(38, 18), nullable=False, server_default=text("0")
    )
    paid_fees_usdt: Mapped[Decimal] = mapped_column(
        Numeric(38, 18), nullable=False, server_default=text("0")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class PaperSpotPositionRow(Base):
    """Reserved schema for a future transaction-safe PAPER Spot lifecycle."""

    __tablename__ = "paper_spot_positions"
    __table_args__ = (
        UniqueConstraint("account_id", "trade_id", name="uq_paper_spot_positions_trade"),
        UniqueConstraint(
            "account_id", "idempotency_key", name="uq_paper_spot_positions_idempotency"
        ),
        CheckConstraint(
            "trade_id ~ '^[A-Za-z0-9_-]{1,64}$'", name="ck_paper_spot_positions_trade_id"
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "length(idempotency_key) BETWEEN 1 AND 128",
            name="ck_paper_spot_positions_idempotency",
        ),
        CheckConstraint("symbol IN ('BTCUSDT', 'ETHUSDT')", name="ck_paper_spot_positions_symbol"),
        CheckConstraint("status IN ('open', 'closed')", name="ck_paper_spot_positions_status"),
        CheckConstraint(
            "quantity > 0 AND entry_fill_price > 0 AND entry_notional_usdt > 0 "
            "AND entry_fee_usdt >= 0 AND stop_price > 0 AND stop_price < entry_fill_price "
            "AND estimated_stop_loss_usdt >= 0 "
            "AND quantity <> 'NaN'::numeric AND entry_fill_price <> 'NaN'::numeric "
            "AND entry_notional_usdt <> 'NaN'::numeric AND entry_fee_usdt <> 'NaN'::numeric "
            "AND stop_price <> 'NaN'::numeric AND estimated_stop_loss_usdt <> 'NaN'::numeric",
            name="ck_paper_spot_positions_entry",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "(status = 'open' AND exit_fill_price IS NULL AND exit_fee_usdt IS NULL "
            "AND net_proceeds_usdt IS NULL AND realized_pnl_usdt IS NULL AND closed_at IS NULL) "
            "OR (status = 'closed' AND exit_fill_price IS NOT NULL AND exit_fill_price > 0 "
            "AND exit_fee_usdt IS NOT NULL AND exit_fee_usdt >= 0 "
            "AND net_proceeds_usdt IS NOT NULL AND net_proceeds_usdt >= 0 "
            "AND realized_pnl_usdt IS NOT NULL AND closed_at IS NOT NULL "
            "AND exit_fill_price <> 'NaN'::numeric AND exit_fee_usdt <> 'NaN'::numeric "
            "AND net_proceeds_usdt <> 'NaN'::numeric AND realized_pnl_usdt <> 'NaN'::numeric)",
            name="ck_paper_spot_positions_lifecycle",
        ).ddl_if(dialect="postgresql"),
        Index("ix_paper_spot_positions_account_status", "account_id", "status", "id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("paper_spot_accounts.id", ondelete="RESTRICT"), nullable=False
    )
    trade_id: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(10), nullable=False, server_default="open")
    quantity: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    entry_fill_price: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    entry_notional_usdt: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    entry_fee_usdt: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    stop_price: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    estimated_stop_loss_usdt: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    exit_fill_price: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    exit_fee_usdt: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    net_proceeds_usdt: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    realized_pnl_usdt: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    opened_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    @property
    def gross_realized_pnl_usdt(self) -> Decimal | None:
        """Fill-price PnL before fees, derived without changing ledger history."""
        if self.realized_pnl_usdt is None or self.exit_fee_usdt is None:
            return None
        return self.realized_pnl_usdt + self.entry_fee_usdt + self.exit_fee_usdt


class PaperSpotJournalRow(Base):
    """Append-only accounting facts; UPDATE, DELETE and TRUNCATE fail in PostgreSQL."""

    __tablename__ = "paper_spot_journal"
    __table_args__ = (
        UniqueConstraint("account_id", "event_id", name="uq_paper_spot_journal_event"),
        UniqueConstraint("account_id", "idempotency_key", name="uq_paper_spot_journal_idempotency"),
        CheckConstraint("length(event_id) BETWEEN 1 AND 64", name="ck_paper_spot_journal_event_id"),
        CheckConstraint(
            "length(idempotency_key) BETWEEN 1 AND 128",
            name="ck_paper_spot_journal_idempotency",
        ),
        CheckConstraint(
            "(event_type = 'account_opened' AND position_id IS NULL AND cash_delta_usdt > 0) "
            "OR (event_type = 'spot_buy' AND position_id IS NOT NULL AND cash_delta_usdt < 0) "
            "OR (event_type = 'spot_sell' AND position_id IS NOT NULL AND cash_delta_usdt > 0)",
            name="ck_paper_spot_journal_event_type",
        ),
        CheckConstraint(
            "balance_after_usdt >= 0 AND fee_usdt >= 0 "
            "AND cash_delta_usdt <> 'NaN'::numeric AND balance_after_usdt <> 'NaN'::numeric "
            "AND fee_usdt <> 'NaN'::numeric AND realized_pnl_delta_usdt <> 'NaN'::numeric "
            "AND quantity_delta <> 'NaN'::numeric",
            name="ck_paper_spot_journal_amounts",
        ).ddl_if(dialect="postgresql"),
        Index("ix_paper_spot_journal_account_order", "account_id", "id"),
        Index("ix_paper_spot_journal_position", "position_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("paper_spot_accounts.id", ondelete="RESTRICT"), nullable=False
    )
    position_id: Mapped[int | None] = mapped_column(
        ForeignKey("paper_spot_positions.id", ondelete="RESTRICT")
    )
    event_id: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    event_type: Mapped[str] = mapped_column(String(20), nullable=False)
    cash_delta_usdt: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    balance_after_usdt: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    fee_usdt: Mapped[Decimal] = mapped_column(
        Numeric(38, 18), nullable=False, server_default=text("0")
    )
    realized_pnl_delta_usdt: Mapped[Decimal] = mapped_column(
        Numeric(38, 18), nullable=False, server_default=text("0")
    )
    quantity_delta: Mapped[Decimal] = mapped_column(
        Numeric(38, 18), nullable=False, server_default=text("0")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class PaperSpotObservedQuoteRow(Base):
    """Immutable local public quote proof linked to one synthetic journal event.

    This is NOT exchange fill evidence; received_at_ms is this machine's UTC.
    """

    __tablename__ = "paper_spot_observed_quotes"
    __table_args__ = (
        UniqueConstraint("journal_id", name="uq_paper_spot_observed_quotes_journal"),
        CheckConstraint("symbol IN ('BTCUSDT', 'ETHUSDT')", name="ck_paper_observed_symbol"),
        CheckConstraint("interval IN ('1m', '5m', '15m')", name="ck_paper_observed_interval"),
        CheckConstraint(
            "source = 'binance_public_spot_ticker_price'", name="ck_paper_observed_source"
        ),
        CheckConstraint(
            "signal_close_time_ms >= 0 AND requested_at_ms > signal_close_time_ms "
            "AND received_at_ms >= requested_at_ms",
            name="ck_paper_observed_timestamps",
        ),
        CheckConstraint(
            "reference_price > 0 AND reference_price <> 'NaN'::numeric",
            name="ck_paper_observed_price",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "fee_bps BETWEEN 0 AND 100 AND slippage_bps BETWEEN 0 AND 100",
            name="ck_paper_observed_costs",
        ),
        CheckConstraint(
            "spread_bps IS NULL OR spread_bps BETWEEN 0 AND 100",
            name="ck_paper_observed_spread",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    journal_id: Mapped[int] = mapped_column(
        ForeignKey("paper_spot_journal.id", ondelete="RESTRICT"), nullable=False
    )
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    interval: Mapped[str] = mapped_column(String(5), nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    signal_close_time_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    requested_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    received_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reference_price: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    fee_bps: Mapped[int] = mapped_column(Integer, nullable=False)
    slippage_bps: Mapped[int] = mapped_column(Integer, nullable=False)
    spread_bps: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class PaperWorkerGuardRow(Base):
    """Local PAPER worker cooldown; NO credentials or process authorization."""

    __tablename__ = "paper_worker_guard"
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_paper_worker_singleton"),
        CheckConstraint(
            "next_allowed_at_ms >= 0 AND last_attempt_at_ms >= 0",
            name="ck_paper_worker_clock",
        ),
        CheckConstraint("consecutive_failures BETWEEN 0 AND 10", name="ck_paper_worker_failures"),
        CheckConstraint(
            "last_status IN ('ready', 'no_signal', 'paper_recorded', "
            "'rate_limited', 'source_blocked', 'error')",
            name="ck_paper_worker_status",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    next_allowed_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    last_attempt_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False)
    last_status: Mapped[str] = mapped_column(String(24), nullable=False)


class StagingSessionRow(Base):
    """Revocable random-cookie sessions; never persist bearer/csrf plaintext."""

    __tablename__ = "staging_sessions"
    __table_args__ = (
        CheckConstraint("role IN ('viewer', 'operator')", name="ck_stage_session_role"),
        CheckConstraint("expires_at_ms > issued_at_ms", name="ck_stage_session_expiry"),
        Index("ix_staging_session_expiry", "expires_at_ms"),
    )

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    csrf_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    issued_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    expires_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    revoked: Mapped[bool] = mapped_column(default=False, nullable=False)


class StagingLoginGateRow(Base):
    """Shared PostgreSQL brute-force limit; no request IP or PII stored."""

    __tablename__ = "staging_login_gate"
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_stage_login_singleton"),
        CheckConstraint(
            "failures BETWEEN 0 AND 5 AND locked_until_ms >= 0",
            name="ck_stage_login_throttle",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    failures: Mapped[int] = mapped_column(Integer, nullable=False)
    locked_until_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)


class PaperBotControlRow(Base):
    """Inert, versioned operator state. No worker reads this for authorization."""

    __tablename__ = "paper_bot_controls"
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_paper_bot_control_singleton"),
        CheckConstraint(
            "revision >= 1 AND settings_version >= 1 AND modified_at_ms >= 0",
            name="ck_paper_bot_control_versions",
        ),
        CheckConstraint(
            "state IN ('RUNNING', 'PAUSED', 'STOPPED', 'EMERGENCY_FLATTEN')",
            name="ck_paper_bot_control_state",
        ),
        CheckConstraint(
            "stop_behavior IN ('MONITOR_ONLY', 'REVIEW_REDUCE_ONLY')",
            name="ck_paper_bot_stop_behavior",
        ),
        CheckConstraint(
            "stop_behavior = 'REVIEW_REDUCE_ONLY' OR operator_confirmed_reduce_only = false",
            name="ck_paper_bot_approval_consistency",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    settings_version: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False)
    stop_behavior: Mapped[str] = mapped_column(String(32), nullable=False)
    operator_confirmed_reduce_only: Mapped[bool] = mapped_column(nullable=False)
    modified_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)


class PaperBotControlEventRow(Base):
    """Append-only control changes, not an order journal."""

    __tablename__ = "paper_bot_control_events"
    __table_args__ = (
        UniqueConstraint("revision", name="uq_paper_bot_control_event_revision"),
        CheckConstraint(
            "revision >= 2 AND settings_version >= 1 AND recorded_at_ms >= 0",
            name="ck_paper_bot_event_versions",
        ),
        CheckConstraint("length(actor_session_hash) = 64", name="ck_paper_bot_event_actor_digest"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    previous_state: Mapped[str] = mapped_column(String(24), nullable=False)
    new_state: Mapped[str] = mapped_column(String(24), nullable=False)
    stop_behavior: Mapped[str] = mapped_column(String(32), nullable=False)
    operator_confirmed_reduce_only: Mapped[bool] = mapped_column(nullable=False)
    settings_version: Mapped[int] = mapped_column(Integer, nullable=False)
    actor_session_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    recorded_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)


class AiResearchForecastReceiptRow(Base):
    """Test-captured, DB-clocked and append-only research forecast only.

    An entry does NOT prove exchange data or authorize a trade or calibrated model.
    """

    __tablename__ = "ai_research_forecast_receipts"
    __table_args__ = (
        CheckConstraint(
            "symbol IN ('BTCUSDT', 'ETHUSDT') AND interval IN ('1m', '5m', '15m')",
            name="ck_ai_receipt_spot_scope",
        ),
        CheckConstraint(
            "length(signal_id) BETWEEN 8 AND 128 AND length(model_version) BETWEEN 1 AND 64",
            name="ck_ai_receipt_names",
        ),
        CheckConstraint(
            "observed_close_at_ms > 0 AND observed_close_at_ms < received_at_ms "
            "AND received_at_ms < outcome_due_at_ms",
            name="ck_ai_receipt_causal_order",
        ),
        CheckConstraint(
            "outcome_due_at_ms - observed_close_at_ms BETWEEN 60000 AND 86400000",
            name="ck_ai_receipt_horizon",
        ),
        CheckConstraint(
            "prob_win BETWEEN 0 AND 1 AND prob_loss BETWEEN 0 AND 1 "
            "AND prob_flat BETWEEN 0 AND 1 AND prob_win + prob_loss + prob_flat = 1 "
            "AND prob_win <> 'NaN'::numeric AND prob_loss <> 'NaN'::numeric "
            "AND prob_flat <> 'NaN'::numeric",
            name="ck_ai_receipt_probabilities",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "CASE interval "
            "WHEN '1m' THEN MOD(observed_close_at_ms + 1, 60000) = 0 "
            "AND MOD(outcome_due_at_ms - observed_close_at_ms, 60000) = 0 "
            "WHEN '5m' THEN MOD(observed_close_at_ms + 1, 300000) = 0 "
            "AND MOD(outcome_due_at_ms - observed_close_at_ms, 300000) = 0 "
            "WHEN '15m' THEN MOD(observed_close_at_ms + 1, 900000) = 0 "
            "AND MOD(outcome_due_at_ms - observed_close_at_ms, 900000) = 0 "
            "ELSE FALSE END",
            name="ck_ai_receipt_closed_candle_grid",
        ),
        Index("ix_ai_receipt_symbol_due", "symbol", "outcome_due_at_ms"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    signal_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    model_version: Mapped[str] = mapped_column(String(64), nullable=False)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    interval: Mapped[str] = mapped_column(String(5), nullable=False)
    observed_close_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    outcome_due_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    received_at_ms: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    prob_win: Mapped[Decimal] = mapped_column(Numeric(38, 30), nullable=False)
    prob_loss: Mapped[Decimal] = mapped_column(Numeric(38, 30), nullable=False)
    prob_flat: Mapped[Decimal] = mapped_column(Numeric(38, 30), nullable=False)


class AiResearchForecastOutcomeRow(Base):
    """Immutable result for a forecast evaluated against persisted closed candles."""

    __tablename__ = "ai_research_forecast_outcomes"
    __table_args__ = (
        CheckConstraint("outcome IN ('WIN', 'LOSS', 'FLAT')", name="ck_ai_outcome_label"),
        CheckConstraint("entry_close > 0 AND target_close > 0", name="ck_ai_outcome_prices"),
        CheckConstraint("flat_band_bps BETWEEN 0 AND 200", name="ck_ai_outcome_flat_band"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    receipt_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    outcome: Mapped[str] = mapped_column(String(8), nullable=False)
    entry_close: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    target_close: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    flat_band_bps: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False)
    source_url: Mapped[str] = mapped_column(String(255), nullable=False)
    source_candle_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    source_retrieved_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    evaluated_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
