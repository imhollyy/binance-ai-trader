from __future__ import annotations

import pytest

from binance_ai_trader.paper_entry_gate import require_atomic_riskguard_authorization
from binance_ai_trader.paper_execution import PaperExecutionRejected


def test_separate_riskguard_and_ledger_cannot_authorize_a_persistent_entry() -> None:
    with pytest.raises(PaperExecutionRejected, match="riskguard_paper_atomicity_unavailable"):
        require_atomic_riskguard_authorization()
