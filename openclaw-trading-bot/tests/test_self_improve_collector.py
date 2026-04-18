"""
tests/test_self_improve_collector.py — étape 1 §13.2.

Vérifie :
- collector filtre uniquement les trades `closed` et dans la fenêtre
- enrichit `duration_hours`, `pnl_bucket`, `catalysts` (JSON → list)
- loser/winner ratio déterministe
- dataset vide → ratio_losers = 0.0 (aucun division by zero)
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.memory.db import init_db
from src.memory.repositories import TradeRecord, TradesRepository
from src.self_improve.collector import CollectedDataset, collect_closed_trades


@pytest.fixture
def con():
    c = init_db(":memory:")
    yield c
    c.close()


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 4, 10, 12, 0, tzinfo=timezone.utc)


def _insert(
    repo: TradesRepository,
    *,
    tid: str,
    strategy: str = "breakout_momentum",
    asset: str = "RUI.PA",
    entry_dt: datetime,
    exit_dt: datetime | None,
    pnl_pct: float | None,
    status: str = "closed",
    conviction: float = 0.8,
    catalysts: list[str] | None = None,
) -> None:
    repo.insert(
        TradeRecord(
            id=tid,
            asset=asset,
            asset_class="equity",
            strategy=strategy,
            side="long",
            entry_price=100.0,
            entry_time=entry_dt.replace(microsecond=0).isoformat(),
            stop_price=95.0,
            tp_prices=[105.0, 110.0],
            size_pct_equity=0.02,
            conviction=conviction,
            rr_estimated=2.0,
            catalysts=catalysts or ["catalyst_a"],
            exit_price=None if exit_dt is None else 100.0 * (1 + (pnl_pct or 0) / 100),
            exit_time=exit_dt.replace(microsecond=0).isoformat() if exit_dt else None,
            pnl_pct=pnl_pct,
            pnl_usd_fictif=None if pnl_pct is None else pnl_pct * 10,
            status=status,
        )
    )


def test_empty_db_returns_empty_dataset(con, now):
    ds = collect_closed_trades(con, since_days=30, now=now)
    assert isinstance(ds, CollectedDataset)
    assert ds.total == 0
    assert ds.trades == []
    assert ds.winners == []
    assert ds.losers == []
    assert ds.ratio_losers == 0.0


def test_only_closed_trades_returned(con, now):
    repo = TradesRepository(con)
    base = now - timedelta(days=3)
    _insert(repo, tid="T-1", entry_dt=base, exit_dt=base + timedelta(hours=4), pnl_pct=2.0)
    _insert(
        repo, tid="T-2", entry_dt=base, exit_dt=None, pnl_pct=None, status="open"
    )
    ds = collect_closed_trades(con, since_days=30, now=now)
    assert ds.total == 1
    assert ds.trades[0]["id"] == "T-1"


def test_window_filters_old_trades(con, now):
    repo = TradesRepository(con)
    old = now - timedelta(days=60)
    recent = now - timedelta(days=5)
    _insert(repo, tid="T-OLD", entry_dt=old, exit_dt=old + timedelta(hours=2), pnl_pct=1.0)
    _insert(repo, tid="T-NEW", entry_dt=recent, exit_dt=recent + timedelta(hours=2), pnl_pct=1.0)
    ds = collect_closed_trades(con, since_days=30, now=now)
    ids = [t["id"] for t in ds.trades]
    assert ids == ["T-NEW"]


def test_classify_winners_and_losers(con, now):
    repo = TradesRepository(con)
    base = now - timedelta(days=2)
    _insert(repo, tid="T-W", entry_dt=base, exit_dt=base + timedelta(hours=3), pnl_pct=2.5)
    _insert(repo, tid="T-L", entry_dt=base, exit_dt=base + timedelta(hours=3), pnl_pct=-1.2)
    _insert(repo, tid="T-SCRATCH", entry_dt=base, exit_dt=base + timedelta(hours=3), pnl_pct=0.1)
    ds = collect_closed_trades(con, since_days=30, now=now)
    # 0.1 % reste un gain nominal (pnl_pct > 0 → winner)
    winner_ids = {w["id"] for w in ds.winners}
    assert {"T-W", "T-SCRATCH"} == winner_ids
    assert len(ds.losers) == 1 and ds.losers[0]["id"] == "T-L"
    assert ds.total == 3
    assert ds.ratio_losers == pytest.approx(1 / 3)


def test_enrichment_adds_duration_and_bucket(con, now):
    repo = TradesRepository(con)
    base = now - timedelta(days=1)
    _insert(
        repo,
        tid="T-Z",
        entry_dt=base,
        exit_dt=base + timedelta(hours=5),
        pnl_pct=1.5,
        catalysts=["earnings"],
    )
    ds = collect_closed_trades(con, since_days=30, now=now)
    t = ds.trades[0]
    assert t["duration_hours"] == 5.0
    assert t["pnl_bucket"] == "win"
    assert t["catalysts"] == ["earnings"]


def test_strategies_unique(con, now):
    repo = TradesRepository(con)
    base = now - timedelta(days=1)
    _insert(repo, tid="T-A", strategy="breakout_momentum",
            entry_dt=base, exit_dt=base + timedelta(hours=1), pnl_pct=1)
    _insert(repo, tid="T-B", strategy="mean_reversion",
            entry_dt=base, exit_dt=base + timedelta(hours=1), pnl_pct=1)
    _insert(repo, tid="T-C", strategy="breakout_momentum",
            entry_dt=base, exit_dt=base + timedelta(hours=1), pnl_pct=1)
    ds = collect_closed_trades(con, since_days=30, now=now)
    assert ds.strategies() == ["breakout_momentum", "mean_reversion"]


def test_custom_loser_threshold(con, now):
    repo = TradesRepository(con)
    base = now - timedelta(days=1)
    _insert(repo, tid="T-X", entry_dt=base, exit_dt=base + timedelta(hours=1), pnl_pct=-0.3)
    # seuil default -0.5 → pas un loser
    ds_default = collect_closed_trades(con, since_days=30, now=now)
    assert ds_default.losers == []
    # seuil -0.2 → devient un loser
    ds_strict = collect_closed_trades(con, since_days=30, loser_threshold_pct=-0.2, now=now)
    assert len(ds_strict.losers) == 1
