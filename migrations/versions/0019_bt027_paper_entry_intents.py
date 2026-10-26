"""Add durable, append-only PAPER entry coordination intents and events.

This is coordination groundwork only. It does not reserve RiskGuard capacity or
write to the PAPER ledger.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0019_bt027_paper_entry_intents"
down_revision: str | None = "0018_bt027_worker_supervisor"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "paper_entry_intents",
        sa.Column("request_id", sa.String(36), primary_key=True),
        sa.Column("idempotency_key", sa.String(128), nullable=False, unique=True),
        sa.Column("intent_sha256", sa.String(64), nullable=False),
        sa.Column("intent_payload", sa.JSON(), nullable=False),
        sa.Column("source_type", sa.String(16), nullable=False),
        sa.Column("source_reference", sa.String(128), nullable=False),
        sa.Column("model_version", sa.String(128)),
        sa.Column("risk_profile_version", sa.Integer(), nullable=False),
        sa.Column(
            "paper_account_id",
            sa.Integer(),
            sa.ForeignKey("paper_spot_accounts.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("paper_settings_version", sa.Integer(), nullable=False),
        sa.Column("symbol", sa.String(20), nullable=False),
        sa.Column("quantity", sa.Numeric(38, 18), nullable=False),
        sa.Column("reference_price", sa.Numeric(38, 18), nullable=False),
        sa.Column("order_notional_usdt", sa.Numeric(38, 18), nullable=False),
        sa.Column("risk_amount_usdt", sa.Numeric(38, 18), nullable=False),
        sa.Column("fee_cost_usdt", sa.Numeric(38, 18), nullable=False),
        sa.Column("spread_cost_usdt", sa.Numeric(38, 18), nullable=False),
        sa.Column("slippage_cost_usdt", sa.Numeric(38, 18), nullable=False),
        sa.Column("expected_net_advantage_usdt", sa.Numeric(38, 18), nullable=False),
        sa.Column("price_observed_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("claimed_ledger_snapshot_sha256", sa.String(64), nullable=False),
        sa.Column("claimed_cost_evidence_sha256", sa.String(64), nullable=False),
        sa.Column("worker_id", sa.String(36)),
        sa.Column("worker_generation", sa.BigInteger()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("clock_timestamp()"),
        ),
        sa.CheckConstraint(
            "idempotency_key ~ '^[A-Za-z0-9_-]{8,128}$'",
            name="ck_paper_entry_intent_idempotency",
        ),
        sa.CheckConstraint(
            "intent_sha256 ~ '^[0-9a-f]{64}$' AND "
            "claimed_ledger_snapshot_sha256 ~ '^[0-9a-f]{64}$' AND "
            "claimed_cost_evidence_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_paper_entry_intent_hashes",
        ),
        sa.CheckConstraint(
            "source_type IN ('MANUAL','AI','OBSERVED','SIGNAL','RESEARCH')",
            name="ck_paper_entry_intent_source",
        ),
        sa.CheckConstraint(
            "length(source_reference) BETWEEN 1 AND 128",
            name="ck_paper_entry_intent_source_reference",
        ),
        sa.CheckConstraint(
            "(source_type = 'AI' AND model_version IS NOT NULL) OR "
            "(source_type <> 'AI' AND model_version IS NULL)",
            name="ck_paper_entry_intent_model_source",
        ),
        sa.CheckConstraint(
            "risk_profile_version >= 1 AND paper_account_id >= 1 AND "
            "paper_settings_version >= 1 AND price_observed_at_ms > 0",
            name="ck_paper_entry_intent_versions",
        ),
        sa.CheckConstraint(
            "symbol IN ('BTCUSDT','ETHUSDT') AND quantity > 0 AND "
            "reference_price > 0 AND order_notional_usdt > 0 AND risk_amount_usdt > 0",
            name="ck_paper_entry_intent_amounts",
        ),
        sa.CheckConstraint(
            "fee_cost_usdt >= 0 AND spread_cost_usdt >= 0 AND slippage_cost_usdt >= 0",
            name="ck_paper_entry_intent_costs",
        ),
        sa.CheckConstraint(
            "(worker_id IS NULL AND worker_generation IS NULL) OR "
            "(worker_id IS NOT NULL AND worker_generation >= 1)",
            name="ck_paper_entry_intent_worker_fence",
        ),
    )
    op.create_table(
        "paper_entry_intent_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("event_id", sa.String(36), nullable=False, unique=True),
        sa.Column(
            "request_id",
            sa.String(36),
            sa.ForeignKey("paper_entry_intents.request_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("from_state", sa.String(32)),
        sa.Column("to_state", sa.String(32), nullable=False),
        sa.Column("reason_code", sa.String(64), nullable=False),
        sa.Column("evidence_reference", sa.String(256)),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("clock_timestamp()"),
        ),
        sa.UniqueConstraint("request_id", "sequence", name="uq_paper_entry_event_sequence"),
        sa.CheckConstraint("sequence >= 1", name="ck_paper_entry_event_sequence"),
        sa.CheckConstraint(
            "reason_code ~ '^[a-z][a-z0-9_]{2,63}$'",
            name="ck_paper_entry_event_reason",
        ),
        sa.CheckConstraint(
            "evidence_reference IS NULL OR length(evidence_reference) BETWEEN 1 AND 256",
            name="ck_paper_entry_event_evidence_reference",
        ),
        sa.CheckConstraint(
            "to_state IN ('PREPARED','RISK_RESERVED','LEDGER_COMMITTED',"
            "'RECONCILIATION_REQUIRED','COMPLETED','REJECTED') AND "
            "(from_state IS NULL OR from_state IN ('PREPARED','RISK_RESERVED',"
            "'LEDGER_COMMITTED','RECONCILIATION_REQUIRED','COMPLETED','REJECTED'))",
            name="ck_paper_entry_event_states",
        ),
        sa.CheckConstraint(
            "(sequence = 1 AND from_state IS NULL AND to_state = 'PREPARED') OR "
            "(sequence > 1 AND from_state IS NOT NULL)",
            name="ck_paper_entry_event_initial_state",
        ),
    )
    op.create_index(
        "ix_paper_entry_intent_events_request_sequence",
        "paper_entry_intent_events",
        ["request_id", "sequence"],
    )
    op.create_index("ix_paper_entry_intents_created_at", "paper_entry_intents", ["created_at"])
    op.execute(
        """
        CREATE FUNCTION bt027_paper_entry_immutable() RETURNS trigger AS $$
        BEGIN RAISE EXCEPTION 'paper entry intent data is immutable'; END;
        $$ LANGUAGE plpgsql;
        CREATE TRIGGER bt027_paper_entry_intents_no_update_delete
          BEFORE UPDATE OR DELETE ON paper_entry_intents
          FOR EACH ROW EXECUTE FUNCTION bt027_paper_entry_immutable();
        CREATE TRIGGER bt027_paper_entry_intents_no_truncate
          BEFORE TRUNCATE ON paper_entry_intents
          FOR EACH STATEMENT EXECUTE FUNCTION bt027_paper_entry_immutable();
        CREATE TRIGGER bt027_paper_entry_events_no_update_delete
          BEFORE UPDATE OR DELETE ON paper_entry_intent_events
          FOR EACH ROW EXECUTE FUNCTION bt027_paper_entry_immutable();
        CREATE TRIGGER bt027_paper_entry_events_no_truncate
          BEFORE TRUNCATE ON paper_entry_intent_events
          FOR EACH STATEMENT EXECUTE FUNCTION bt027_paper_entry_immutable();
        CREATE FUNCTION bt027_paper_entry_event_transition_guard() RETURNS trigger AS $$
        DECLARE previous_state text;
        DECLARE previous_sequence integer;
        BEGIN
          IF NEW.to_state IN ('RISK_RESERVED','LEDGER_COMMITTED','COMPLETED')
            AND NEW.evidence_reference IS NULL THEN
            RAISE EXCEPTION 'paper entry state transition requires evidence reference';
          END IF;
          PERFORM 1 FROM paper_entry_intents
            WHERE request_id = NEW.request_id FOR UPDATE;
          SELECT to_state, sequence INTO previous_state, previous_sequence
            FROM paper_entry_intent_events
            WHERE request_id = NEW.request_id
            ORDER BY sequence DESC LIMIT 1;
          IF previous_sequence IS NULL THEN
            IF NEW.sequence <> 1 OR NEW.from_state IS NOT NULL OR NEW.to_state <> 'PREPARED' THEN
              RAISE EXCEPTION 'paper entry must begin in PREPARED';
            END IF;
          ELSE
            IF NEW.sequence <> previous_sequence + 1
              OR NEW.from_state IS DISTINCT FROM previous_state THEN
              RAISE EXCEPTION 'paper entry event sequence or prior state mismatch';
            END IF;
            IF NOT (
              (previous_state = 'PREPARED' AND NEW.to_state IN
                ('RISK_RESERVED','RECONCILIATION_REQUIRED','REJECTED')) OR
              (previous_state = 'RISK_RESERVED' AND NEW.to_state IN
                ('LEDGER_COMMITTED','RECONCILIATION_REQUIRED')) OR
              (previous_state = 'LEDGER_COMMITTED' AND NEW.to_state IN
                ('COMPLETED','RECONCILIATION_REQUIRED'))
            ) THEN
              RAISE EXCEPTION 'invalid paper entry state transition';
            END IF;
          END IF;
          RETURN NEW;
        END; $$ LANGUAGE plpgsql;
        CREATE TRIGGER bt027_paper_entry_events_transition_guard
          BEFORE INSERT ON paper_entry_intent_events
          FOR EACH ROW EXECUTE FUNCTION bt027_paper_entry_event_transition_guard();
        """
    )


def downgrade() -> None:
    raise RuntimeError("BT-027 coordination intent and audit history must not be dropped")
