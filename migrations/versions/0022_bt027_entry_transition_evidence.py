"""Require PAPER ledger receipts for commit and completion events."""

from __future__ import annotations

from alembic import op

revision: str = "0022_bt027_entry_guard"
down_revision: str | None = "0021_bt027_entry_ledger"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION bt027_paper_entry_event_transition_guard()
        RETURNS trigger AS $$
        DECLARE previous_state text;
        DECLARE previous_sequence integer;
        DECLARE receipt_is_valid boolean;
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

          IF NEW.to_state IN ('LEDGER_COMMITTED','COMPLETED') THEN
            SELECT EXISTS (
              SELECT 1
              FROM paper_entry_ledger_commits c
              JOIN paper_entry_intents i ON i.request_id = c.request_id
              JOIN paper_spot_journal j ON j.id = c.journal_id
              JOIN paper_spot_positions p ON p.id = c.position_id
              JOIN LATERAL (
                SELECT prior.balance_after_usdt
                FROM paper_spot_journal prior
                WHERE prior.account_id = j.account_id AND prior.id < j.id
                ORDER BY prior.id DESC LIMIT 1
              ) prior_balance ON TRUE
              WHERE c.request_id = NEW.request_id
                AND c.intent_sha256 = i.intent_sha256
                AND c.commit_sha256 = NEW.evidence_reference
                AND c.risk_audit_event_id = i.request_id
                AND c.risk_reservation_event_id = i.request_id || '_reserve'
                AND c.ledger_event_id = j.event_id
                AND j.event_type = 'spot_buy'
                AND j.idempotency_key = 'entry-' || i.request_id
                AND j.account_id = i.paper_account_id
                AND j.position_id = p.id
                AND p.account_id = i.paper_account_id
                AND p.trade_id = i.request_id
                AND p.idempotency_key = j.idempotency_key
                AND p.symbol = i.symbol
                AND p.quantity = i.quantity
                AND j.quantity_delta = i.quantity
                AND c.fee_usdt = j.fee_usdt
                AND p.entry_fee_usdt = j.fee_usdt
                AND c.cash_after_usdt = j.balance_after_usdt
                AND j.balance_after_usdt - prior_balance.balance_after_usdt = j.cash_delta_usdt
                AND p.entry_notional_usdt + p.entry_fee_usdt = -j.cash_delta_usdt
                AND j.cash_delta_usdt < 0
                AND j.balance_after_usdt >= 0
            ) INTO receipt_is_valid;
            IF NOT receipt_is_valid THEN
              RAISE EXCEPTION 'paper entry commit receipt does not match ledger evidence';
            END IF;
          END IF;
          RETURN NEW;
        END; $$ LANGUAGE plpgsql;
        """
    )


def downgrade() -> None:
    raise RuntimeError("PAPER entry transition evidence guard must not be weakened")
