"""BT-013: AI proposals -> independent risk review -> ISOLATED immutable PAPER book.

No API route, PostgreSQL mutation, Binance order client, job activation,
live exchange connection or Futures. This is an offline dry-run integration,
not an autonomous production execution path. Input must come from trusted
server collectors, NEVER from the model's JSON.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal

from binance_ai_trader.ai_constitution import (
    AiSpotProposal,
    BotState,
    ConstitutionAction,
    ConstitutionVerdict,
    IndependentRiskPolicy,
    MarketRegime,
    TrustedSpotEvidence,
    evaluate_constitution_spot_proposal,
)
from binance_ai_trader.ai_market_regime import TimeframeHistory, analyze_spot_timeframes
from binance_ai_trader.paper_spot import (
    PaperCosts,
    PaperSpotBook,
    PaperSpotRejected,
    SpotBuyIntent,
    simulate_spot_buy,
    simulate_spot_sell,
)

BPS = Decimal("10000")
TWO = Decimal("2")
QTY_STEP = Decimal("0.00000001")


@dataclass(frozen=True, slots=True)
class AiPaperDryRun:
    verdict: ConstitutionVerdict
    book: PaperSpotBook
    event: str

    @property
    def simulated(self) -> bool:
        return self.event in ("PAPER_BUY", "PAPER_SELL")


def _deny(proposal: AiSpotProposal, book: PaperSpotBook, reason: str) -> AiPaperDryRun:
    return AiPaperDryRun(
        ConstitutionVerdict(proposal.signal_id, ConstitutionAction.NO_TRADE, reason),
        book,
        "NONE",
    )


def _verified_costs(evidence: TrustedSpotEvidence) -> PaperCosts | None:
    """Existing PAPER fills accept only integral fee/slip basis points.

    Reject unknown fractional precision instead of rounding exchange costs
    downward to make performance appear better.
    """
    fee, slip, spread = (
        evidence.taker_fee_bps,
        evidence.slippage_bps,
        evidence.spread_bps,
    )
    if (
        not all(isinstance(x, Decimal) and x.is_finite() for x in (fee, slip, spread))
        or not Decimal(0) < fee <= 100
        or not Decimal(0) < slip <= 100
        or not Decimal(0) <= spread <= 100
        or fee != int(fee)
        or slip != int(slip)
    ):
        return None
    return PaperCosts(fee_bps=int(fee), slippage_bps=int(slip))


def simulate_reviewed_ai_spot(
    proposal: AiSpotProposal,
    policy: IndependentRiskPolicy,
    evidence: TrustedSpotEvidence,
    histories: Sequence[TimeframeHistory],
    book: PaperSpotBook,
    *,
    context_timeframe: str,
    higher_timeframe: str,
    now_ms: int,
    trade_id_to_close: str | None = None,
) -> AiPaperDryRun:
    """One non-mutating dry-run BUY or risk-reducing SELL, with audited fees.

    The caller supplies server-owned risk and source verification. A successful
    result is a NEW in-memory paper book, not a database transaction or order.
    For exits, no order flow is assumed; a verified fresh mark is required.
    """
    if (
        not isinstance(proposal, AiSpotProposal)
        or not isinstance(policy, IndependentRiskPolicy)
        or not isinstance(evidence, TrustedSpotEvidence)
        or not isinstance(book, PaperSpotBook)
    ):
        raise TypeError("typed_ai_paper_dry_run_required")
    settings = policy.settings
    if (
        settings.mode != "paper"
        or settings.market_type != "spot"
        or settings.max_leverage != 1
        or not settings.stop_loss_required
        or proposal.market != "SPOT"
        or proposal.mode != "PAPER"
        or proposal.symbol not in settings.allowed_symbols
        or book.initial_capital_usdt != Decimal(settings.spot_capital_usdt)
        or policy.settings_version < 1
        or policy.settings_version != policy.account_settings_version
    ):
        return _deny(proposal, book, "USER_HARD_LIMIT")
    try:
        assessment = analyze_spot_timeframes(
            histories,
            symbol=proposal.symbol,
            execution_timeframe=proposal.execution_timeframe,
            context_timeframe=context_timeframe,
            higher_timeframe=higher_timeframe,
            now_ms=now_ms,
        )
    except (ValueError, TypeError):
        return _deny(proposal, book, "DATA_QUALITY_FAILURE")
    if not assessment.data_ok:
        return _deny(proposal, book, assessment.reason_code)
    if (
        assessment.execution_close_at_ms != proposal.observed_close_at_ms
        or assessment.execution_close != evidence.reference_price
        or proposal.market_regime != assessment.regime
    ):
        return _deny(proposal, book, "MTF_CONTEXT_MISMATCH")
    costs = _verified_costs(evidence)
    if costs is None:
        return _deny(proposal, book, "UNVERIFIED_TRANSACTION_COST")
    half_spread = evidence.spread_bps / (BPS * TWO)
    if proposal.decision is ConstitutionAction.BUY:
        if assessment.regime not in (MarketRegime.UPTREND, MarketRegime.STRONG_UPTREND):
            return _deny(proposal, book, "REGIME_STRATEGY_MISMATCH")
        if book.positions or any(p.trade_id == proposal.signal_id for p in book.closed):
            return _deny(proposal, book, "PAPER_EXPOSURE_OR_REPLAY")
        if (
            book.cash_usdt != policy.account_state.equity_usdt
            or policy.account_state.open_positions != 0
            or policy.account_state.reserved_capital_usdt != 0
        ):
            return _deny(proposal, book, "PAPER_RISK_SNAPSHOT_MISMATCH")
        verdict = evaluate_constitution_spot_proposal(proposal, policy, evidence, now_ms=now_ms)
        if not verdict.proposal_eligible:
            return AiPaperDryRun(verdict, book, "NONE")
        entry_reference = evidence.reference_price * (1 + half_spread)
        entry_fill = entry_reference * (1 + Decimal(costs.slippage_bps) / BPS)
        quantity = (verdict.allowed_notional_usdt / entry_fill).quantize(
            QTY_STEP, rounding=ROUND_DOWN
        )
        if quantity <= 0:
            return _deny(proposal, book, "PAPER_QUANTITY_TOO_SMALL")
        try:
            after = simulate_spot_buy(
                book,
                settings,
                policy.account_state,
                SpotBuyIntent(
                    trade_id=proposal.signal_id,
                    symbol=proposal.symbol,
                    reference_price=entry_reference,
                    quantity=quantity,
                    stop_price=proposal.stop_price or Decimal(0),
                ),
                costs=costs,
            )
        except (PaperSpotRejected, ArithmeticError, ValueError):
            return _deny(proposal, book, "PAPER_ENTRY_RISK_REJECTED")
        return AiPaperDryRun(verdict, after, "PAPER_BUY")
    if proposal.decision is ConstitutionAction.SELL:
        # STOPPED's stop_behavior is not specified by current RiskSettings;
        # do not silently invent it. PAUSED and EMERGENCY may reduce risk.
        if policy.bot_state not in (BotState.RUNNING, BotState.PAUSED, BotState.EMERGENCY_FLATTEN):
            return _deny(proposal, book, "BOT_NOT_RUNNING")
        if (
            evidence.source != "verified_public_binance_spot"
            or not evidence.critical_data_ok
            or not evidence.exchange_constraints_ok
            or evidence.symbol != proposal.symbol
            or evidence.interval != proposal.execution_timeframe
            or evidence.last_closed_at_ms != proposal.observed_close_at_ms
            or type(now_ms) is not int
            or now_ms < proposal.created_at_ms
            or now_ms >= proposal.valid_until_ms
            or type(policy.max_market_age_ms) is not int
            or policy.max_market_age_ms <= 0
            or not (evidence.last_closed_at_ms <= evidence.received_at_ms <= proposal.created_at_ms)
            or now_ms - evidence.received_at_ms > policy.max_market_age_ms
            or now_ms - evidence.last_closed_at_ms > policy.max_market_age_ms
        ):
            return _deny(proposal, book, "DATA_QUALITY_FAILURE")
        matches = [
            p
            for p in book.positions
            if p.trade_id == trade_id_to_close and p.symbol == proposal.symbol
        ]
        if len(matches) != 1:
            return _deny(proposal, book, "PAPER_POSITION_NOT_OPEN")
        if len(book.positions) != 1:
            return _deny(proposal, book, "PAPER_EXPOSURE_OR_REPLAY")
        try:
            after = simulate_spot_sell(
                book,
                matches[0].trade_id,
                evidence.reference_price * (1 - half_spread),
                costs=costs,
            )
        except (PaperSpotRejected, ArithmeticError, ValueError):
            return _deny(proposal, book, "PAPER_EXIT_REJECTED")
        return AiPaperDryRun(
            ConstitutionVerdict(
                proposal.signal_id, ConstitutionAction.SELL, "PAPER_EXIT_SIMULATED"
            ),
            after,
            "PAPER_SELL",
        )
    if proposal.decision in (ConstitutionAction.HOLD, ConstitutionAction.NO_TRADE):
        return _deny(proposal, book, "MODEL_NO_TRADE")
    return _deny(proposal, book, "UNSUPPORTED_PROPOSAL")
