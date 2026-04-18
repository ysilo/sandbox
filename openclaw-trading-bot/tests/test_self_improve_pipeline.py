"""
tests/test_self_improve_pipeline.py — orchestration §13.2.

Vérifie :
- pipeline sur DB vide → no patterns, fichier pending écrit
- pipeline sur données avec pertes → patch sélectionné + persisté
- pipeline avec stub `passing=False` → pas de patch sélectionné
- pipeline notifier appelé quand top patch sélectionné
- pipeline ok=True en chemin heureux
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import pytest

from src.memory.db import init_db
from src.memory.repositories import PatchesRepository, TradeRecord, TradesRepository
from src.self_improve.pipeline import run_self_improve
from src.self_improve.validator import StubBacktestRunner


@dataclass
class _FakeNotifier:
    calls: list[tuple] = field(default_factory=list)

    def send_alert(self, message: str, *, level: str = "ERROR", code: str | None = None) -> bool:
        self.calls.append((message, level, code))
        return True


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 4, 10, tzinfo=timezone.utc)


def _insert_loser(
    repo: TradesRepository,
    tid: str,
    *,
    now: datetime,
    strategy: str = "breakout_momentum",
    asset: str = "RUI.PA",
):
    entry = now - timedelta(days=5)
    exitt = entry + timedelta(hours=3)
    repo.insert(
        TradeRecord(
            id=tid, asset=asset, asset_class="equity",
            strategy=strategy, side="long",
            entry_price=100.0, entry_time=entry.isoformat(),
            stop_price=95.0, tp_prices=[105.0],
            size_pct_equity=0.02, conviction=0.75,
            rr_estimated=2.0, catalysts=["c1"],
            exit_price=98.0, exit_time=exitt.isoformat(),
            pnl_pct=-1.5, pnl_usd_fictif=-15.0,
            status="closed",
        )
    )


def test_pipeline_empty_db_writes_empty_pending(tmp_path):
    con = init_db(":memory:")
    path = tmp_path / "pending.md"
    result = run_self_improve(
        con=con, since_days=30, pending_path=str(path),
    )
    assert result.ok is True
    assert result.dataset is not None
    assert result.dataset.total == 0
    assert result.diagnosis is not None
    assert result.diagnosis.patterns == []
    assert result.pending_path == str(path)
    assert "Aucun patch" in path.read_text(encoding="utf-8")


def test_pipeline_with_losers_selects_and_persists_patch(tmp_path, now):
    con = init_db(":memory:")
    repo = TradesRepository(con)
    for i in range(3):
        _insert_loser(repo, f"T-{i}", now=now, strategy="breakout_momentum")

    path = tmp_path / "pending.md"
    notifier = _FakeNotifier()
    result = run_self_improve(
        con=con, since_days=30, pending_path=str(path),
        runner=StubBacktestRunner(passing=True),
        notifier=notifier,
    )

    assert result.ok is True
    assert result.diagnosis.patterns, "un pattern au moins doit être détecté"
    assert len(result.patches) >= 1
    assert len(result.validations) == len(result.patches)
    assert result.selection is not None
    assert len(result.selection.selected) == 1

    top = result.selection.top
    assert top is not None
    assert top.patch.target.startswith("strategy:") or top.patch.target.startswith("risk:")
    assert top.score > 0

    # Patch persisté
    patches_repo = PatchesRepository(con)
    row = patches_repo.get(top.patch.patch_id)
    assert row is not None
    assert row["status"] == "proposed"

    # Fichier pending contient le patch
    content = path.read_text(encoding="utf-8")
    assert top.patch.patch_id in content

    # Notifier appelé
    assert len(notifier.calls) == 1
    assert top.patch.patch_id in notifier.calls[0][0]


def test_pipeline_failing_runner_no_patch_selected(tmp_path, now):
    con = init_db(":memory:")
    repo = TradesRepository(con)
    for i in range(3):
        _insert_loser(repo, f"T-{i}", now=now)

    path = tmp_path / "pending.md"
    result = run_self_improve(
        con=con, since_days=30, pending_path=str(path),
        runner=StubBacktestRunner(passing=False),
    )

    assert result.ok is True
    assert result.selection is not None
    assert result.selection.selected == []
    # Chaque validation a échoué → rejected
    assert len(result.selection.rejected) >= 1
    assert "Aucun patch" in path.read_text(encoding="utf-8")


def test_pipeline_multiple_patterns_quota_enforced(tmp_path, now):
    """Plusieurs patterns détectés → au plus 1 patch sélectionné."""
    con = init_db(":memory:")
    repo = TradesRepository(con)
    # 3 losers breakout → concentration + 2 losers mean_reversion/AAPL → repeat_loss
    for i in range(3):
        _insert_loser(repo, f"T-BRK-{i}", now=now, strategy="breakout_momentum")
    for i in range(2):
        _insert_loser(
            repo, f"T-MR-{i}", now=now, strategy="mean_reversion", asset="AAPL"
        )

    path = tmp_path / "pending.md"
    result = run_self_improve(
        con=con, since_days=30, pending_path=str(path),
        runner=StubBacktestRunner(passing=True), max_per_week=1,
    )
    assert result.ok is True
    assert len(result.selection.selected) == 1
    quota_rejected = [
        r for r in result.selection.rejected if r.block_reason == "weekly_quota_reached"
    ]
    assert len(quota_rejected) >= 1


def test_pipeline_no_notifier_no_crash(tmp_path, now):
    con = init_db(":memory:")
    repo = TradesRepository(con)
    for i in range(3):
        _insert_loser(repo, f"T-{i}", now=now)
    path = tmp_path / "pending.md"
    result = run_self_improve(
        con=con, since_days=30, pending_path=str(path),
        runner=StubBacktestRunner(passing=True),
        notifier=None,
    )
    assert result.ok is True


def test_pipeline_since_days_clamped_to_one(tmp_path):
    con = init_db(":memory:")
    path = tmp_path / "pending.md"
    # since_days=0 ou négatif → clampé à 1, pas de crash
    for bad in (0, -5):
        res = run_self_improve(
            con=con, since_days=bad, pending_path=str(path),
        )
        assert res.ok is True
        assert res.dataset is not None


def test_pipeline_validation_failure_keeps_pairs_aligned(tmp_path, now):
    """Régression : si validate_patch lève sur UN patch, les autres restent
    correctement corrélés avec leur validation (pas de décalage de zip)."""
    con = init_db(":memory:")
    repo = TradesRepository(con)
    # 3 patterns distincts → 3 patches
    for i in range(3):
        _insert_loser(repo, f"T-BRK-{i}", now=now, strategy="breakout_momentum")
    for i in range(2):
        _insert_loser(
            repo, f"T-MR-{i}", now=now, strategy="mean_reversion", asset="AAPL"
        )

    class _FlakyRunner:
        """Lève sur le 2e appel, réussit les autres."""

        def __init__(self):
            self.calls = 0

        def run(self, patch):
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError("simulated_backtest_crash")
            return StubBacktestRunner(passing=True).run(patch)

    path = tmp_path / "pending.md"
    result = run_self_improve(
        con=con, since_days=30, pending_path=str(path),
        runner=_FlakyRunner(),
    )
    # Le run enregistre l'erreur mais ne lève pas
    assert any("validation_failed" in e for e in result.errors)
    # Et chaque validation restante correspond bien à son patch
    assert len(result.validations) == len(result.patches) - 1
    for sp in result.selection.selected:
        assert sp.validation.patch_id == sp.patch.patch_id
