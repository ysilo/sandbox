"""
tests/test_contracts.py — invariants Pydantic inter-skills (§8.8.1).

Ces tests sont la plomberie de base : tout changement de `src/contracts/skills.py`
qui casse l'une de ces assertions doit être considéré breaking et documenté
dans TRADING_BOT_ARCHITECTURE.md avant merge.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.contracts import (
    CHECK_IDS,
    CycleResult,
    IchimokuPayload,
    NewsPulse,
    RiskCheckResult,
    RiskDecision,
    SignalOutput,
    TradeProposal,
    _pad_checks,
    _utc_now,
)


# ---------------------------------------------------------------------------
# Timestamps
# ---------------------------------------------------------------------------


def test_utc_now_matches_iso8601_z_format() -> None:
    ts = _utc_now()
    assert len(ts) == 20
    assert ts.endswith("Z")
    assert ts[10] == "T"


def test_signal_output_rejects_bad_timestamp() -> None:
    ichi = _make_ichi()
    with pytest.raises(ValidationError):
        SignalOutput(
            asset="RUI.PA",
            timestamp="not-a-ts",
            composite_score=0.5,
            confidence=0.7,
            regime_context="risk_on",
            ichimoku=ichi,
            trend=[], momentum=[], volume=[],
        )


# ---------------------------------------------------------------------------
# SignalOutput : invariant is_proposal=False
# ---------------------------------------------------------------------------


def test_signal_output_is_proposal_cannot_be_true() -> None:
    """Invariant §8.8.1 : aucun skill LLM ne peut produire un TradeProposal."""
    with pytest.raises(ValidationError):
        SignalOutput(
            asset="RUI.PA",
            timestamp="2026-04-17T10:00:00Z",
            composite_score=0.5,
            confidence=0.7,
            regime_context="risk_on",
            ichimoku=_make_ichi(),
            trend=[], momentum=[], volume=[],
            is_proposal=True,  # type: ignore[arg-type]
        )


# ---------------------------------------------------------------------------
# RiskDecision : invariant `checks` toujours 10 entrées C1→C10 ordonnées
# ---------------------------------------------------------------------------


def test_pad_checks_preserves_order_and_pads() -> None:
    partial = [
        RiskCheckResult(
            check_id="C1_kill_switch",
            passed=False, severity="blocking",
            reason="KILL file present",
        ),
    ]
    padded = _pad_checks(partial)
    assert len(padded) == 10
    assert [c.check_id for c in padded] == list(CHECK_IDS)
    assert padded[0].evaluated is True
    assert padded[1].evaluated is False  # C2..C10 non-évalués (short-circuit)
    assert all(c.passed for c in padded[1:])  # convention §8.8.1


def test_risk_decision_reject_has_ten_checks() -> None:
    rej = RiskDecision.reject(
        proposal_id="tp_abc",
        reasons=["C1 kill_switch"],
        checks=[RiskCheckResult(
            check_id="C1_kill_switch", passed=False, severity="blocking",
            reason="KILL file",
        )],
    )
    assert rej.approved is False
    assert rej.reasons == ["C1 kill_switch"]
    assert len(rej.checks) == 10


def test_risk_decision_approve_has_ten_checks_and_size() -> None:
    all_ok = [
        RiskCheckResult(check_id=cid, passed=True, severity="warn", reason="ok")
        for cid in CHECK_IDS
    ]
    ok = RiskDecision.approve(proposal_id="tp_xyz", checks=all_ok, adjusted_size_pct=0.01)
    assert ok.approved is True
    assert ok.reasons == []
    assert ok.adjusted_size_pct == 0.01
    assert len(ok.checks) == 10


# ---------------------------------------------------------------------------
# NewsPulse.empty() — fallback news_agent KO (§8.7.1)
# ---------------------------------------------------------------------------


def test_news_pulse_empty_factory() -> None:
    np = NewsPulse.empty("RUI.PA", window_hours=12)
    assert np.items == []
    assert np.top is None
    assert np.aggregate_impact == 0.0
    assert np.aggregate_sentiment == 0.0


# ---------------------------------------------------------------------------
# CycleResult factories
# ---------------------------------------------------------------------------


def test_cycle_result_success_no_degradation_is_success() -> None:
    r = CycleResult.success(proposals=2)
    assert r.status == "success"
    assert r.degradation_flags == []


def test_cycle_result_success_with_degradation_is_degraded() -> None:
    r = CycleResult.success(proposals=1, degradation_flags=["regime_stale"])
    assert r.status == "degraded"
    assert r.degradation_flags == ["regime_stale"]


def test_cycle_result_aborted_sets_reason() -> None:
    r = CycleResult.aborted("kill_switch")
    assert r.status == "aborted"
    assert r.reason == "kill_switch"
    assert r.proposals == 0


# ---------------------------------------------------------------------------
# TradeProposal — dataclass, pas Pydantic, ichimoku typé (fix A3)
# ---------------------------------------------------------------------------


def test_trade_proposal_requires_typed_ichimoku() -> None:
    tp = TradeProposal(
        strategy_id="ichimoku_trend_following",
        asset="RUI.PA", asset_class="equity", side="long",
        entry_price=40.0, stop_price=38.0, tp_prices=[43.0, 46.0],
        rr=1.5, conviction=0.65, risk_pct=0.01,
        catalysts=["macd_cross"],
        ichimoku=_make_ichi(),
    )
    assert tp.proposal_id.startswith("tp_")
    assert isinstance(tp.ichimoku, IchimokuPayload)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ichi() -> IchimokuPayload:
    return IchimokuPayload(
        price_above_kumo=True,
        tenkan_above_kijun=True,
        chikou_above_price_26=True,
        kumo_thickness_pct=0.02,
        aligned_long=True,
        aligned_short=False,
        distance_to_kumo_pct=0.5,
    )
