"""
tests/test_self_improve_diagnostician.py — étape 2 §13.2.

Vérifie les heuristiques du stub :
- dataset vide
- pas de losers
- concentration par stratégie (≥ 3 pertes)
- low conviction (≥ 2 pertes < 0.5)
- quick loss (≥ 2 pertes < 2h)
- repeat_loss (asset + stratégie)
- regime_wide_loss (ratio > 40 % sur ≥ 10 trades)
"""
from __future__ import annotations

from src.self_improve.collector import CollectedDataset
from src.self_improve.diagnostician import DiagnosisReport, diagnose


def _loser(
    tid: str,
    strategy: str = "breakout_momentum",
    asset: str = "RUI.PA",
    conviction: float = 0.8,
    duration_hours: float | None = 4.0,
    pnl_pct: float = -1.5,
) -> dict:
    return {
        "id": tid,
        "strategy": strategy,
        "asset": asset,
        "conviction": conviction,
        "duration_hours": duration_hours,
        "pnl_pct": pnl_pct,
    }


def _winner(tid: str) -> dict:
    return {
        "id": tid,
        "strategy": "mean_reversion",
        "asset": "EURUSD",
        "conviction": 0.9,
        "duration_hours": 6.0,
        "pnl_pct": 2.0,
    }


def test_empty_dataset_no_patterns():
    ds = CollectedDataset()
    r = diagnose(ds)
    assert isinstance(r, DiagnosisReport)
    assert r.patterns == []
    assert r.note == "no_closed_trades_in_window"


def test_no_losers_no_patterns():
    ds = CollectedDataset(trades=[_winner("T-1")], total=1, winners=[_winner("T-1")])
    r = diagnose(ds)
    assert r.patterns == []
    assert r.note == "no_losers_in_window"


def test_concentration_strategy_pattern():
    losers = [_loser(f"T-{i}", strategy="breakout_momentum") for i in range(3)]
    ds = CollectedDataset(trades=losers, losers=losers, total=3)
    r = diagnose(ds)
    assert any(
        p.pattern == "concentration_loser_strategy:breakout_momentum"
        for p in r.patterns
    )
    pat = [p for p in r.patterns if p.pattern.startswith("concentration")][0]
    assert pat.frequency == 3
    assert pat.severity in {"mid", "high"}
    assert pat.scope == "strategy:breakout_momentum"


def test_low_conviction_pattern():
    losers = [
        _loser("T-1", conviction=0.3),
        _loser("T-2", conviction=0.4, strategy="mean_reversion"),
    ]
    ds = CollectedDataset(trades=losers, losers=losers, total=2)
    r = diagnose(ds)
    assert any(p.pattern == "low_conviction_losses" for p in r.patterns)


def test_quick_loss_pattern():
    losers = [
        _loser("T-1", duration_hours=1.0),
        _loser("T-2", duration_hours=0.5, strategy="mean_reversion"),
    ]
    ds = CollectedDataset(trades=losers, losers=losers, total=2)
    r = diagnose(ds)
    assert any(p.pattern == "quick_loss_scalp" for p in r.patterns)


def test_repeat_loss_pattern():
    losers = [
        _loser("T-1", strategy="mean_reversion", asset="AAPL", duration_hours=8),
        _loser("T-2", strategy="mean_reversion", asset="AAPL", duration_hours=8),
    ]
    ds = CollectedDataset(trades=losers, losers=losers, total=2)
    r = diagnose(ds)
    assert any(p.pattern == "repeat_loss:AAPL:mean_reversion" for p in r.patterns)


def test_regime_wide_loss_pattern():
    losers = [_loser(f"T-L{i}") for i in range(6)]
    winners = [_winner(f"T-W{i}") for i in range(5)]
    ds = CollectedDataset(
        trades=losers + winners,
        losers=losers,
        winners=winners,
        total=len(losers) + len(winners),
    )
    r = diagnose(ds)
    # ratio losers = 6/11 ≈ 0.54
    assert any(p.pattern == "regime_wide_loss" for p in r.patterns)


def test_has_actionable_patterns_flag():
    losers = [_loser(f"T-{i}", strategy="event_driven_macro") for i in range(5)]
    ds = CollectedDataset(trades=losers, losers=losers, total=5)
    r = diagnose(ds)
    assert r.has_actionable_patterns is True
