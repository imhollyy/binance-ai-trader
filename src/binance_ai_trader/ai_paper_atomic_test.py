"""BT-014 isolated PostgreSQL integration rehearsal for AI-gated PAPER Spot.

Test-harness ONLY: refuses every non-localhost, non-disposable database.
No API route, scheduler, real orders, Futures or owner PAPER account writes.
Uses the existing transaction-locked ledger rather than a second money engine.
Caller-provided evidence/calibration are SYNTHETIC in this rehearsal and
are NOT authenticated exchange observations or actual model calibration.
"""

from __future__ import annotations

import hashlib
import os
import time
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from binance_ai_trader.ai_constitution import (
    AiSpotProposal,
    BotState,
    ConstitutionAction,
    IndependentRiskPolicy,
    MarketRegime,
    TrustedSpotEvidence,
    evaluate_constitution_spot_proposal,
)
from binance_ai_trader.ai_market_regime import TimeframeHistory, analyze_spot_timeframes
from binance_ai_trader.ai_paper_dry_run import _verified_costs
from binance_ai_trader.market_data import Candle
from binance_ai_trader.models import (
    PaperSpotAccountRow,
    PaperSpotJournalRow,
    PaperSpotPositionRow,
    SpotCandleRow,
    TradingSettingsRow,
)
from binance_ai_trader.paper_execution import (
    PaperExecutionRejected,
    PaperTradeReceipt,
    _risk_state,
    close_paper_spot,
    open_paper_spot,
)
from binance_ai_trader.paper_spot import SpotBuyIntent
from binance_ai_trader.risk_schemas import RiskSettings

BPS = Decimal("10000")
QTY_STEP = Decimal("0.001")
SOURCE = "verified_public_binance_spot"
LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost"})


@dataclass(frozen=True, slots=True)
class IsolatedAiPolicy:
    """Test-owned simulated policy, NOT a live config API or AI-provided input."""

    expected_settings_version: int
    bot_state: BotState
    calibrated_model_versions: frozenset[str]
    min_net_edge_bps: Decimal
    min_edge_to_cost_ratio: Decimal
    max_market_age_ms: int


def _isolated_target(engine: Engine) -> None:
    url = engine.url
    if (
        engine.dialect.name != "postgresql"
        or url.host not in LOCAL_HOSTS
        or url.database != "trader_ai_atomic_test"
        or os.environ.get("AI_ATOMIC_TEST_ISOLATED") != "1"
    ):
        raise PaperExecutionRejected("disposable_ai_postgres_only")


def _history(session: Session, symbol: str, interval: str) -> TimeframeHistory:
    rows = list(
        session.scalars(
            select(SpotCandleRow)
            .where(SpotCandleRow.symbol == symbol, SpotCandleRow.interval == interval)
            .order_by(SpotCandleRow.open_time_ms.desc())
            .limit(50)
        )
    )
    return TimeframeHistory(
        interval,
        tuple(
            Candle(
                row.symbol,
                row.interval,
                row.open_time_ms,
                row.close_time_ms,
                row.open,
                row.high,
                row.low,
                row.close,
                row.volume,
            )
            for row in reversed(rows)
        ),
    )


def _identity(proposal: AiSpotProposal) -> tuple[str, str]:
    digest = hashlib.sha256(proposal.signal_id.encode("ascii")).hexdigest()
    # Signal ID is globally unique across settings versions. Changing payload
    # for that ID must conflict with the original journal digest.
    return "ai-review-" + digest[:48], "ai_" + digest[:40]


def _fail(reason: str) -> None:
    raise PaperExecutionRejected("ai_gate:" + reason)


def execute_disposable_reviewed_ai_once(
    engine: Engine,
    proposal: AiSpotProposal,
    evidence: TrustedSpotEvidence,
    policy: IsolatedAiPolicy,
    *,
    context_timeframe: str,
    higher_timeframe: str,
    trade_id_to_close: str | None = None,
) -> PaperTradeReceipt:
    """Rehearse atomic BUY/SELL + idempotency in a dedicated disposable DB.

    All key gates are recomputed IN the shared account-locked transaction.
    Price equals latest stored execution-timeframe closed candle; this is a
    synthetic retrospective reference, NOT a real fill or tradable quote.
    """
    _isolated_target(engine)
    if (
        not isinstance(proposal, AiSpotProposal)
        or not isinstance(evidence, TrustedSpotEvidence)
        or not isinstance(policy, IsolatedAiPolicy)
        or proposal.decision not in (ConstitutionAction.BUY, ConstitutionAction.SELL)
    ):
        _fail("INVALID_AI_INPUT")
    if (
        type(policy.expected_settings_version) is not int
        or policy.expected_settings_version < 1
        or not isinstance(policy.bot_state, BotState)
        or evidence.source != SOURCE
        or not evidence.critical_data_ok
        or not evidence.exchange_constraints_ok
        or evidence.symbol != proposal.symbol
        or evidence.interval != proposal.execution_timeframe
        or evidence.last_closed_at_ms != proposal.observed_close_at_ms
    ):
        _fail("DATA_OR_POLICY_UNVERIFIED")
    costs = _verified_costs(evidence)
    if costs is None:
        _fail("UNVERIFIED_TRANSACTION_COST")
    assert costs is not None

    key, own_trade_id = _identity(proposal)
    reference = evidence.reference_price
    half_spread = evidence.spread_bps / (2 * BPS)
    if not isinstance(reference, Decimal) or not reference.is_finite() or reference <= 0:
        _fail("INVALID_REFERENCE_PRICE")
    buy_reference = reference * (1 + half_spread)
    sell_reference = reference * (1 - half_spread)
    if sell_reference <= 0:
        _fail("INVALID_SPREAD")
    if proposal.decision is ConstitutionAction.BUY:
        if proposal.suggested_notional_usdt <= 0 or proposal.stop_price is None:
            _fail("INVALID_BUY_SIZE_OR_STOP")
        predicted_fill = buy_reference * (1 + Decimal(costs.slippage_bps) / BPS)
        quantity = (proposal.suggested_notional_usdt / predicted_fill).quantize(
            QTY_STEP, rounding=ROUND_DOWN
        )
        if quantity <= 0:
            _fail("QUANTITY_TOO_SMALL")

    def locked_review(
        session: Session,
        settings: RiskSettings,
        account: PaperSpotAccountRow,
        positions: list[PaperSpotPositionRow],
        journal: list[PaperSpotJournalRow],
    ) -> None:
        settings_row = session.get(TradingSettingsRow, 1)
        if (
            settings_row is None
            or settings_row.version != policy.expected_settings_version
            or account.settings_version != settings_row.version
            or account.initial_capital_usdt != Decimal(settings.spot_capital_usdt)
            or settings.mode != "paper"
            or settings.market_type != "spot"
            or settings.max_leverage != 1
            or not settings.stop_loss_required
            or proposal.symbol not in settings.allowed_symbols
        ):
            _fail("USER_HARD_LIMIT_OR_SETTINGS_VERSION")
        now_ms = time.time_ns() // 1_000_000
        if (
            now_ms < proposal.created_at_ms
            or now_ms >= proposal.valid_until_ms
            or now_ms - evidence.received_at_ms < 0
            or type(policy.max_market_age_ms) is not int
            or policy.max_market_age_ms <= 0
            or now_ms - evidence.received_at_ms > policy.max_market_age_ms
        ):
            _fail("DATA_QUALITY_FAILURE")
        try:
            assessment = analyze_spot_timeframes(
                (
                    _history(session, proposal.symbol, proposal.execution_timeframe),
                    _history(session, proposal.symbol, context_timeframe),
                    _history(session, proposal.symbol, higher_timeframe),
                ),
                symbol=proposal.symbol,
                execution_timeframe=proposal.execution_timeframe,
                context_timeframe=context_timeframe,
                higher_timeframe=higher_timeframe,
                now_ms=now_ms,
            )
        except (ValueError, TypeError) as exc:
            raise PaperExecutionRejected("ai_gate:INVALID_MTF_SOURCE") from exc
        if not assessment.data_ok:
            _fail(assessment.reason_code)
        if (
            assessment.execution_close_at_ms != proposal.observed_close_at_ms
            or assessment.execution_close != reference
            or assessment.regime != proposal.market_regime
        ):
            _fail("MTF_CONTEXT_MISMATCH")
        opening = [p for p in positions if p.status == "open"]
        if proposal.decision is ConstitutionAction.BUY:
            if opening:
                _fail("PORTFOLIO_EXPOSURE")
            try:
                state = _risk_state(account, journal)
            except PaperExecutionRejected as exc:
                raise PaperExecutionRejected("ai_gate:PORTFOLIO_RISK") from exc
            server_policy = IndependentRiskPolicy(
                settings=settings,
                account_state=state,
                bot_state=policy.bot_state,
                settings_version=settings_row.version,
                account_settings_version=account.settings_version,
                calibrated_model_versions=policy.calibrated_model_versions,
                min_net_edge_bps=policy.min_net_edge_bps,
                min_edge_to_cost_ratio=policy.min_edge_to_cost_ratio,
                max_market_age_ms=policy.max_market_age_ms,
            )
            verdict = evaluate_constitution_spot_proposal(
                proposal, server_policy, evidence, now_ms=now_ms
            )
            if not verdict.proposal_eligible:
                _fail(verdict.reason_code)
            if assessment.regime not in (MarketRegime.UPTREND, MarketRegime.STRONG_UPTREND):
                _fail("REGIME_STRATEGY_MISMATCH")
            if quantity * predicted_fill > verdict.allowed_notional_usdt:
                _fail("NOTIONAL_EXCEEDS_VERDICT")
        else:
            # SELL is strictly reduce-only and never opens new risk.
            # STOPPED behavior is undefined in RiskSettings; no silent close.
            if policy.bot_state not in (
                BotState.RUNNING,
                BotState.PAUSED,
                BotState.EMERGENCY_FLATTEN,
            ):
                _fail("BOT_NOT_RUNNING")
            if (
                len(opening) != 1
                or opening[0].trade_id != trade_id_to_close
                or opening[0].symbol != proposal.symbol
            ):
                _fail("POSITION_NOT_OPEN_OR_MISMATCH")
            # No equity/risk budget is inferred from an unmarked open position.
            # This is an isolated reduction, not an autonomous stop engine.

    if proposal.decision is ConstitutionAction.BUY:
        assert proposal.stop_price is not None
        with Session(engine) as session:
            return open_paper_spot(
                session,
                SpotBuyIntent(
                    own_trade_id, proposal.symbol, buy_reference, quantity, proposal.stop_price
                ),
                key,
                costs=costs,
                _signal_preflight=locked_review,
            )
    if not isinstance(trade_id_to_close, str):
        _fail("MISSING_POSITION_IDENTIFIER")
    with Session(engine) as session:
        return close_paper_spot(
            session,
            trade_id_to_close,
            sell_reference,
            key,
            costs=costs,
            _signal_preflight=locked_review,
        )
