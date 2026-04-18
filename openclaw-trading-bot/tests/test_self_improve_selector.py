"""
tests/test_self_improve_selector.py — étape 5 §13.2, blacklist §13.3.1.

Vérifie :
- patch qui n'a pas passé la validation → rejected
- scope blacklisté → blacklisted (risk:kill_switch, orchestrator:*, contract:*)
- scope non blacklisté (risk:min_conviction_to_propose) → selected
- top-k par semaine respecté (max_per_week=1 par défaut)
- score composite calculé correctement
- liste vide → résultat vide, pas de crash
"""
from __future__ import annotations

from src.self_improve.patch import StrategyPatch
from src.self_improve.selector import (
    BLACKLIST_EXACT,
    BLACKLIST_PREFIXES,
    select_patches,
)
from src.self_improve.validator import PatchValidationResult


def _val(
    patch_id: str,
    *,
    passed: bool = True,
    t_stat: float = 2.5,
    sharpe_baseline: float = 1.0,
    sharpe_patch: float = 1.3,
    dsr: float = 0.97,
    trade_count: int = 120,
) -> PatchValidationResult:
    return PatchValidationResult(
        patch_id=patch_id,
        passed=passed,
        sharpe_baseline=sharpe_baseline,
        sharpe_patch=sharpe_patch,
        t_stat=t_stat,
        dd_baseline=0.08, dd_patch=0.07,
        trade_count=trade_count,
        dsr=dsr,
        recommendation="approve" if passed else "reject",
    )


def _patch(pid: str, target: str = "strategy:breakout_momentum",
           kind: str = "param_tuning") -> StrategyPatch:
    return StrategyPatch(
        patch_id=pid, target=target, kind=kind, description="demo",
    )


def test_empty_list_returns_empty_selection():
    res = select_patches([])
    assert res.selected == []
    assert res.rejected == []
    assert res.blacklisted == []
    assert res.top is None


def test_failed_validation_rejected():
    p = _patch("P-1")
    v = _val("P-1", passed=False)
    res = select_patches([(p, v)])
    assert len(res.rejected) == 1
    assert res.rejected[0].block_reason == "validation_failed"
    assert res.selected == []


def test_blacklist_exact_scopes_blocked():
    for scope in BLACKLIST_EXACT:
        p = _patch(f"P-{scope}", target=scope)
        v = _val(f"P-{scope}")
        res = select_patches([(p, v)])
        assert len(res.blacklisted) == 1, f"scope {scope} devrait être blacklisté"
        assert res.selected == []


def test_blacklist_prefix_scopes_blocked():
    for prefix in BLACKLIST_PREFIXES:
        scope = f"{prefix}something"
        p = _patch(f"P-{prefix}", target=scope)
        v = _val(f"P-{prefix}")
        res = select_patches([(p, v)])
        assert len(res.blacklisted) == 1, f"scope {scope} devrait être blacklisté"


def test_allowed_risk_param_tuning_scope_selected():
    # risk:min_conviction_to_propose n'est pas un hard-limit → autorisé
    p = _patch("P-OK", target="risk:min_conviction_to_propose")
    v = _val("P-OK")
    res = select_patches([(p, v)])
    assert len(res.selected) == 1
    assert res.top is not None
    assert res.top.patch.patch_id == "P-OK"


def test_top_k_quota_enforced():
    # 3 patchs valides → seul le meilleur est sélectionné, 2 rejetés pour quota
    patches = [
        (_patch("P-A", target="strategy:A"), _val("P-A", t_stat=2.2, dsr=0.96)),
        (_patch("P-B", target="strategy:B"), _val("P-B", t_stat=3.5, dsr=0.98,
                                                  sharpe_patch=1.5)),
        (_patch("P-C", target="strategy:C"), _val("P-C", t_stat=2.4, dsr=0.96)),
    ]
    res = select_patches(patches, max_per_week=1)
    assert len(res.selected) == 1
    assert res.top.patch.patch_id == "P-B"            # meilleur score
    quota_rejects = [r for r in res.rejected if r.block_reason == "weekly_quota_reached"]
    assert len(quota_rejects) == 2


def test_top_k_higher_quota_keeps_more():
    patches = [
        (_patch("P-A", target="strategy:A"), _val("P-A", t_stat=2.2)),
        (_patch("P-B", target="strategy:B"), _val("P-B", t_stat=3.5)),
    ]
    res = select_patches(patches, max_per_week=2)
    assert len(res.selected) == 2


def test_score_composite_ordering():
    # Deux patchs identiques sauf t_stat — le plus élevé gagne
    patches = [
        (_patch("P-LOW", target="strategy:A"), _val("P-LOW", t_stat=2.1)),
        (_patch("P-HIGH", target="strategy:B"), _val("P-HIGH", t_stat=4.5)),
    ]
    res = select_patches(patches, max_per_week=2)
    ids = [sp.patch.patch_id for sp in res.selected]
    assert ids == ["P-HIGH", "P-LOW"]
    assert res.selected[0].score > res.selected[1].score


def test_score_is_rounded_and_capped():
    p = _patch("P-MAX", target="strategy:X")
    v = _val("P-MAX", t_stat=99, sharpe_patch=10.0, dsr=1.0)
    res = select_patches([(p, v)])
    assert res.selected[0].score <= 1.0
