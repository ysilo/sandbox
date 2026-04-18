"""
tests/test_self_improve_patch_validator.py — étapes 3 et 4 §13.2.

Vérifie :
- generate_patch routes chaque pattern vers le bon kind
- patchs inconnus → None
- validate_patch passe avec StubBacktestRunner(passing=True)
- validate_patch échoue avec StubBacktestRunner(passing=False)
- custom thresholds → custom failures
- sharpe_delta property cohérent
"""
from __future__ import annotations

from dataclasses import dataclass

from src.self_improve.diagnostician import DiagnosedPattern
from src.self_improve.patch import StrategyPatch, generate_patch
from src.self_improve.validator import (
    BacktestResult,
    StubBacktestRunner,
    ValidationThresholds,
    validate_patch,
)


def _pat(name: str, scope: str = "strategy:breakout_momentum", freq: int = 3) -> DiagnosedPattern:
    return DiagnosedPattern(
        pattern=name, frequency=freq, sample_trade_ids=["T-1", "T-2"],
        suggested_fix="fix", scope=scope, severity="mid",
    )


# ---------------------------------------------------------------------------
# generate_patch
# ---------------------------------------------------------------------------


def test_generate_patch_concentration():
    p = generate_patch(_pat("concentration_loser_strategy:breakout_momentum"))
    assert p is not None
    assert p.kind == "param_tuning"
    assert p.target == "strategy:breakout_momentum"
    assert p.change["param"] == "max_risk_pct"
    assert p.change["delta_pct"] == -0.20


def test_generate_patch_low_conviction():
    p = generate_patch(_pat("low_conviction_losses", scope="risk:min_conviction"))
    assert p is not None
    assert p.target == "risk:min_conviction_to_propose"
    assert p.change["delta_abs"] == 0.05


def test_generate_patch_quick_loss():
    p = generate_patch(_pat("quick_loss_scalp", scope="risk:stop_distance"))
    assert p is not None
    assert p.target == "risk:stop_atr_mult"
    assert p.change["delta_pct"] == 0.10


def test_generate_patch_repeat_loss():
    p = generate_patch(
        _pat("repeat_loss:AAPL:mean_reversion", scope="strategy:mean_reversion")
    )
    assert p is not None
    assert p.kind == "filter_add"
    assert p.change["blacklist_asset"] == "AAPL"
    assert p.change["strategy"] == "mean_reversion"


def test_generate_patch_regime_wide():
    p = generate_patch(_pat("regime_wide_loss", scope="regime:global"))
    assert p is not None
    assert p.kind == "regime_shift"
    assert p.change["regime"] == "risk_off"


def test_generate_patch_unknown_returns_none():
    p = generate_patch(_pat("unknown_pattern"))
    assert p is None


def test_patch_new_id_stable_shape():
    a, b = StrategyPatch.new_id(), StrategyPatch.new_id()
    assert a.startswith("P-") and len(a) == 8
    assert a != b


# ---------------------------------------------------------------------------
# validate_patch
# ---------------------------------------------------------------------------


def _demo_patch() -> StrategyPatch:
    return StrategyPatch(
        patch_id=StrategyPatch.new_id(),
        target="strategy:breakout_momentum",
        kind="param_tuning",
        description="demo",
    )


def test_validate_patch_passes_with_stub_passing():
    p = _demo_patch()
    v = validate_patch(p, runner=StubBacktestRunner(passing=True))
    assert v.passed is True
    assert v.recommendation == "approve"
    assert v.failures == []
    assert v.sharpe_delta > 0
    assert v.patch_id == p.patch_id


def test_validate_patch_fails_with_stub_failing():
    v = validate_patch(_demo_patch(), runner=StubBacktestRunner(passing=False))
    assert v.passed is False
    assert v.recommendation == "reject"
    # Au moins les 3 critères clés échouent
    assert any("t_stat" in f for f in v.failures)
    assert any("sharpe_delta" in f for f in v.failures)
    assert any("trade_count" in f for f in v.failures)
    assert any("dsr" in f for f in v.failures)


def test_validate_patch_custom_thresholds_stricter():
    # Seuil DSR plus strict que ce que retourne le stub passing (0.97)
    thresholds = ValidationThresholds(min_dsr=0.99, min_t_stat=2.0)
    v = validate_patch(
        _demo_patch(),
        runner=StubBacktestRunner(passing=True),
        thresholds=thresholds,
    )
    assert v.passed is False
    assert any("dsr" in f for f in v.failures)


def test_validate_patch_dd_exceeded_fails():
    @dataclass
    class DDBreachRunner:
        def run(self, patch):
            return BacktestResult(
                sharpe_baseline=1.0, sharpe_patch=1.4,
                t_stat=3.0,
                dd_baseline=0.08, dd_patch=0.10,          # ratio 1.25 > 1.10
                trade_count=100, dsr=0.97,
            )

    v = validate_patch(_demo_patch(), runner=DDBreachRunner())
    assert v.passed is False
    assert any("max_dd_patch" in f for f in v.failures)


def test_stub_runner_extra_carries_patch_id():
    p = _demo_patch()
    r = StubBacktestRunner(passing=True).run(p)
    assert r.extra["stub"] is True
    assert r.extra["patch_id"] == p.patch_id
