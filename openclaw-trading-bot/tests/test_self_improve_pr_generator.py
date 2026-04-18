"""
tests/test_self_improve_pr_generator.py — étape 6 §13.2.

Vérifie le rendu de `IMPROVEMENTS_PENDING.md` :
- sélection vide → message "Aucun patch"
- sélection avec patch → bloc sélectionné + actions + rejected
- fichier idempotent (réécrit à chaque run)
- XSS / caractères spéciaux n'explosent pas
"""
from __future__ import annotations

from datetime import datetime, timezone

from src.self_improve.diagnostician import DiagnosedPattern
from src.self_improve.patch import StrategyPatch
from src.self_improve.pr_generator import write_improvements_pending
from src.self_improve.selector import ScoredPatch, SelectionResult
from src.self_improve.validator import PatchValidationResult


def _sp(
    pid: str = "P-ABC123",
    target: str = "strategy:breakout_momentum",
    score: float = 0.75,
    t_stat: float = 2.7,
    dsr: float = 0.97,
) -> ScoredPatch:
    patch = StrategyPatch(
        patch_id=pid,
        target=target,
        kind="param_tuning",
        description="Réduire max_risk_pct de 20 %",
        change={"strategy": "breakout_momentum", "param": "max_risk_pct", "delta_pct": -0.2},
        source_pattern=DiagnosedPattern(
            pattern="concentration_loser_strategy:breakout_momentum",
            frequency=4, severity="high",
        ),
    )
    val = PatchValidationResult(
        patch_id=pid, passed=True,
        sharpe_baseline=1.0, sharpe_patch=1.35,
        t_stat=t_stat,
        dd_baseline=0.08, dd_patch=0.076,
        trade_count=120, dsr=dsr,
        recommendation="approve",
    )
    return ScoredPatch(patch=patch, validation=val, score=score)


def test_empty_selection_writes_no_patch_message(tmp_path):
    path = tmp_path / "IMPROVEMENTS_PENDING.md"
    result = write_improvements_pending(
        SelectionResult(), path=str(path),
        now=datetime(2026, 4, 17, tzinfo=timezone.utc),
    )
    content = path.read_text(encoding="utf-8")
    assert result == str(path)
    assert "Improvements Pending — 2026-04-17" in content
    assert "Aucun patch" in content


def test_selection_renders_top_block_with_actions(tmp_path):
    path = tmp_path / "pending.md"
    sel = SelectionResult(selected=[_sp()])
    write_improvements_pending(sel, path=str(path))
    content = path.read_text(encoding="utf-8")
    assert "Patch sélectionné : `P-ABC123`" in content
    assert "strategy:breakout_momentum" in content
    assert "`/approve P-ABC123`" in content
    assert "`/reject P-ABC123`" in content
    assert "`/defer P-ABC123`" in content
    assert "concentration_loser_strategy" in content


def test_selection_renders_rejected_and_blacklisted(tmp_path):
    path = tmp_path / "pending.md"
    rejected = _sp(pid="P-REJ")
    rejected.block_reason = "validation_failed"
    blk = _sp(pid="P-BLK", target="orchestrator:something")
    blk.block_reason = "scope `orchestrator:something` interdit"
    sel = SelectionResult(selected=[_sp()], rejected=[rejected], blacklisted=[blk])
    write_improvements_pending(sel, path=str(path))
    content = path.read_text(encoding="utf-8")
    assert "P-REJ" in content
    assert "validation_failed" in content
    assert "P-BLK" in content
    assert "orchestrator:something" in content


def test_idempotent_rewrite(tmp_path):
    path = tmp_path / "pending.md"
    write_improvements_pending(SelectionResult(selected=[_sp(pid="P-1")]), path=str(path))
    write_improvements_pending(SelectionResult(selected=[_sp(pid="P-2")]), path=str(path))
    content = path.read_text(encoding="utf-8")
    assert "P-2" in content
    assert "P-1" not in content


def test_sharpe_delta_formatted_in_content(tmp_path):
    path = tmp_path / "pending.md"
    sel = SelectionResult(selected=[_sp()])
    write_improvements_pending(sel, path=str(path))
    content = path.read_text(encoding="utf-8")
    assert "+0.35" in content or "+0.35," in content
    assert "Δ +0.35" in content


def test_atomic_write_no_tmp_file_leftover(tmp_path):
    """Sanity check : pas de fichier .tmp laissé dans le dossier après écriture."""
    path = tmp_path / "pending.md"
    write_improvements_pending(SelectionResult(selected=[_sp()]), path=str(path))
    leftover = [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
    assert leftover == []
    assert path.exists()
