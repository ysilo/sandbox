"""
tests.test_market_scan — skill `market-scan` §8.8.2, §8.8.3.

Couvre :
- Formule score_scan §8.8.2 (poids, clipping)
- Features individuelles (trend_slope_50, relative_volume_20, atr_pct_20, roc_20)
- Seuil de shortlist (abs(score)≥0.25)
- Cap shortlist §8.8.3 (max 20)
- Fallback barres insuffisantes
- Liquidity filter
- force_candidate (news_pulse / telegram_cmd / correlated_to)
- ScanStats cohérentes
- Tri par |score| desc
"""
from __future__ import annotations

from typing import Sequence

import pytest

from src.contracts.skills import Candidate
from src.signals import market_scan as ms
from src.signals.market_scan import (
    SHORTLIST_MAX_TOTAL,
    OHLCVTuple,
    _UniverseEntry,
    compute_score_scan,
    force_candidate,
    scan,
)


# ---------------------------------------------------------------------------
# Factories
# ---------------------------------------------------------------------------


def _bars_flat(n: int = 70, close: float = 100.0, volume: float = 1000.0) -> list[OHLCVTuple]:
    """N barres plates (aucun mouvement) — score_scan ≈ 0."""
    return [
        (f"2026-01-{i:02d}T00:00:00Z", close, close, close, close, volume)
        for i in range(1, n + 1)
    ]


def _bars_uptrend(n: int = 70, start: float = 100.0, step: float = 0.5,
                   volume: float = 1000.0, vol_spike: float = 1.0) -> list[OHLCVTuple]:
    """Uptrend linéaire — positive score_scan.

    `vol_spike` permet d'opter pour un pic de volume sur la dernière barre
    (simule un breakout typique). La formule §8.8.2 pondère `relative_volume_20`
    à 25 % : sans spike le score reste modeste même pour une tendance marquée,
    donc les tests qui veulent franchir `SCORE_SCAN_THRESHOLD=0.25` passent
    explicitement `vol_spike=3.0`.
    """
    out: list[OHLCVTuple] = []
    for i in range(n):
        c = start + i * step
        v = volume * (vol_spike if i == n - 1 else 1.0)
        out.append((f"2026-01-{(i % 28) + 1:02d}T00:00:00Z",
                    c - 0.1, c + 0.2, c - 0.2, c, v))
    return out


def _bars_downtrend(n: int = 70, start: float = 100.0, step: float = 0.5,
                     volume: float = 1000.0, vol_spike: float = 1.0) -> list[OHLCVTuple]:
    """Downtrend linéaire — negative score_scan.

    Noter : `relative_volume_20` et `abs(atr_pct_20)` sont toujours positifs
    par la formule §8.8.2 ; un gros spike de volume sur un downtrend aboutit
    à un score ambigu (signaux contradictoires → le scanner écarte), ce qui
    est le comportement intentionnel. Les tests qui veulent un score
    franchement négatif laissent donc `vol_spike=1.0` (par défaut).
    """
    out: list[OHLCVTuple] = []
    for i in range(n):
        c = start - i * step
        v = volume * (vol_spike if i == n - 1 else 1.0)
        out.append((f"2026-01-{(i % 28) + 1:02d}T00:00:00Z",
                    c + 0.1, c + 0.2, c - 0.2, c, v))
    return out


def _entry(asset: str, cls: str = "crypto") -> _UniverseEntry:
    return _UniverseEntry(asset=asset, asset_class=cls)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Features unitaires
# ---------------------------------------------------------------------------


class TestFeatures:
    def test_trend_slope_50_flat_is_zero(self):
        closes = [100.0] * 80
        assert ms._trend_slope_50(closes) == 0.0

    def test_trend_slope_50_uptrend_positive(self):
        closes = [100.0 + i * 0.5 for i in range(80)]
        assert ms._trend_slope_50(closes) > 0

    def test_trend_slope_50_downtrend_negative(self):
        closes = [100.0 - i * 0.5 for i in range(80)]
        assert ms._trend_slope_50(closes) < 0

    def test_trend_slope_50_insufficient_data(self):
        # Pas assez pour calculer SMA50 + 20 barres
        closes = [100.0] * 30
        assert ms._trend_slope_50(closes) == 0.0

    def test_relative_volume_20_neutral(self):
        vols = [1000.0] * 25
        # ratio = 1.0, score = tanh(0) = 0
        assert ms._relative_volume_20(vols) == pytest.approx(0.0, abs=1e-9)

    def test_relative_volume_20_surge(self):
        vols = [1000.0] * 20 + [3000.0]  # dernière barre 3x la moyenne
        # ratio = 3.0, score = tanh(2) ≈ 0.964
        score = ms._relative_volume_20(vols)
        assert 0.9 < score < 1.0

    def test_atr_pct_20_zero_range(self):
        bars = _bars_flat(30)
        # high==low==close sur toutes barres → TR=0
        assert ms._atr_pct_20(bars) == 0.0

    def test_atr_pct_20_bounded_in_unit(self):
        bars = _bars_uptrend(30)
        score = ms._atr_pct_20(bars)
        assert -1.0 <= score <= 1.0

    def test_momentum_roc_20_flat(self):
        closes = [100.0] * 30
        assert ms._momentum_roc_20(closes) == 0.0

    def test_momentum_roc_20_positive(self):
        closes = [100.0 + i for i in range(30)]  # +30 après 30 barres
        # ROC_20 = (close[-1] - close[-21]) / close[-21] = (129 - 109) / 109 ≈ 0.18
        score = ms._momentum_roc_20(closes)
        assert 0.15 < score < 0.22


# ---------------------------------------------------------------------------
# compute_score_scan
# ---------------------------------------------------------------------------


class TestScoreScan:
    def test_flat_market_near_zero(self):
        bars = _bars_flat(80)
        assert abs(compute_score_scan(bars)) < 0.05

    def test_strong_uptrend_positive(self):
        bars = _bars_uptrend(80, start=100.0, step=1.0)
        score = compute_score_scan(bars)
        assert score > 0

    def test_strong_downtrend_negative(self):
        bars = _bars_downtrend(80, start=200.0, step=1.0)
        score = compute_score_scan(bars)
        assert score < 0

    def test_score_clipped_unit_range(self):
        # Uptrend agressif
        bars = _bars_uptrend(80, start=10.0, step=5.0)
        score = compute_score_scan(bars)
        assert -1.0 <= score <= 1.0

    def test_insufficient_bars_returns_zero(self):
        bars = _bars_flat(20)  # < _MIN_BARS_REQUIRED=60
        assert compute_score_scan(bars) == 0.0


# ---------------------------------------------------------------------------
# scan()
# ---------------------------------------------------------------------------


class TestScan:
    def _fetch_stub(self, bars_by_asset: dict[str, list[OHLCVTuple]]):
        def _f(asset: str, cls: str) -> Sequence[OHLCVTuple]:
            return bars_by_asset.get(asset, [])
        return _f

    def test_shortlists_strong_scores(self):
        # Breakout typique : uptrend + volume spike → passe le seuil 0.25
        fetch = self._fetch_stub({
            "BTC/USDT": _bars_uptrend(80, start=100.0, step=2.0, vol_spike=3.0),
            "STABLE": _bars_flat(80),
        })
        res = scan(
            universe=[_entry("BTC/USDT"), _entry("STABLE")],
            fetch=fetch,
        )
        ids = [c.asset for c in res.candidates]
        assert "BTC/USDT" in ids
        assert "STABLE" not in ids  # score trop faible

    def test_threshold_excludes_weak_scores(self):
        """Un asset avec |score_scan| < 0.25 est exclu de la shortlist."""
        # Micro-trend : trop faible pour passer le seuil
        fetch = self._fetch_stub({
            "MICRO": _bars_uptrend(80, start=100.0, step=0.005),
        })
        res = scan(universe=[_entry("MICRO")], fetch=fetch)
        assert res.candidates == []

    def test_empty_universe_returns_empty(self):
        res = scan(universe=[], fetch=lambda a, c: [])
        assert res.candidates == []
        assert res.stats.scanned == 0

    def test_insufficient_bars_skipped(self):
        fetch = self._fetch_stub({"SHORT": _bars_flat(10)})  # < 60
        res = scan(universe=[_entry("SHORT")], fetch=fetch)
        assert res.candidates == []
        assert res.stats.skipped_insufficient_bars == 1

    def test_liquidity_filter_excludes(self):
        # Breakout qualifiant (vol spike) mais liquidity False → exclus
        fetch = self._fetch_stub({
            "BTC/USDT": _bars_uptrend(80, start=100.0, step=2.0, vol_spike=3.0),
        })

        def _liq(asset: str, cls: str) -> bool:
            return False

        res = scan(universe=[_entry("BTC/USDT")], fetch=fetch, liquidity_check=_liq)
        assert res.candidates == []
        assert res.stats.skipped_liquidity == 1

    def test_liquidity_filter_accepts(self):
        fetch = self._fetch_stub({
            "BTC/USDT": _bars_uptrend(80, start=100.0, step=2.0, vol_spike=3.0),
        })
        res = scan(
            universe=[_entry("BTC/USDT")], fetch=fetch,
            liquidity_check=lambda a, c: True,
        )
        assert len(res.candidates) == 1

    def test_sort_by_abs_score_desc(self):
        # Trois breakouts d'amplitude différente — STRONG doit arriver en tête
        fetch = self._fetch_stub({
            "WEAK":   _bars_uptrend(80, start=100.0, step=1.0, vol_spike=2.0),
            "STRONG": _bars_uptrend(80, start=100.0, step=3.0, vol_spike=5.0),
            "MED":    _bars_uptrend(80, start=100.0, step=2.0, vol_spike=3.0),
        })
        res = scan(
            universe=[_entry("WEAK"), _entry("STRONG"), _entry("MED")],
            fetch=fetch,
        )
        asset_order = [c.asset for c in res.candidates]
        # STRONG est premier (score le plus fort)
        assert asset_order[0] == "STRONG"

    def test_cap_applied(self):
        """`max_shortlist` doit plafonner la sortie."""
        # 25 assets tous en breakout suffisant (vol_spike)
        bars_map = {
            f"A{i:02d}": _bars_uptrend(80, start=100.0, step=2.0 + i * 0.1, vol_spike=3.0)
            for i in range(25)
        }
        fetch = self._fetch_stub(bars_map)
        res = scan(
            universe=[_entry(f"A{i:02d}") for i in range(25)],
            fetch=fetch,
            max_shortlist=SHORTLIST_MAX_TOTAL,
        )
        assert len(res.candidates) <= SHORTLIST_MAX_TOTAL

    def test_fetch_error_isolated(self):
        """Une source en erreur ne casse pas le scan des autres."""

        def _fetch(asset: str, cls: str) -> Sequence[OHLCVTuple]:
            if asset == "BOOM":
                raise RuntimeError("source offline")
            return _bars_uptrend(80, start=100.0, step=2.0, vol_spike=3.0)

        res = scan(
            universe=[_entry("BOOM"), _entry("BTC/USDT")],
            fetch=_fetch,
        )
        assert res.stats.skipped_fetch_error == 1
        # L'autre asset est scanné normalement
        assert any(c.asset == "BTC/USDT" for c in res.candidates)

    def test_stats_totals(self):
        fetch = self._fetch_stub({
            "BTC/USDT": _bars_uptrend(80, start=100.0, step=2.0, vol_spike=3.0),
            "ETH/USDT": _bars_uptrend(80, start=200.0, step=3.0, vol_spike=3.0),
            "STABLE":   _bars_flat(80),
        })
        res = scan(
            universe=[_entry("BTC/USDT"), _entry("ETH/USDT"), _entry("STABLE")],
            fetch=fetch,
        )
        assert res.stats.scanned == 3
        assert res.stats.shortlisted == len(res.candidates)
        # Stable: score ≈ 0 → pas shortlisté, mais pas skippé pour barres non plus
        assert res.stats.skipped_insufficient_bars == 0

    def test_candidate_fields_populated(self):
        fetch = self._fetch_stub({
            "BTC/USDT": _bars_uptrend(80, start=100.0, step=2.0, vol_spike=3.0),
        })
        res = scan(universe=[_entry("BTC/USDT")], fetch=fetch)
        assert len(res.candidates) == 1
        c = res.candidates[0]
        assert isinstance(c, Candidate)
        assert c.asset == "BTC/USDT"
        assert c.asset_class == "crypto"
        assert -1.0 <= c.score_scan <= 1.0
        assert c.liquidity_ok is True
        assert c.forced_by is None


# ---------------------------------------------------------------------------
# force_candidate
# ---------------------------------------------------------------------------


class TestForceCandidate:
    def test_force_news_pulse(self):
        c = force_candidate("BTC/USDT", "crypto", forced_by="news_pulse", score_scan=0.5)
        assert c.forced_by == "news_pulse"
        assert c.score_scan == 0.5
        assert c.liquidity_ok is True

    def test_force_telegram_cmd(self):
        c = force_candidate("EUR/USD", "forex", forced_by="telegram_cmd")
        assert c.forced_by == "telegram_cmd"
        assert c.score_scan == 0.0

    def test_force_correlated_requires_asset(self):
        with pytest.raises(ValueError):
            force_candidate(
                "BTC/USDT", "crypto",
                forced_by="correlated_to",
                correlated_to=None,
            )

    def test_force_correlated_ok(self):
        c = force_candidate(
            "ETH/USDT", "crypto",
            forced_by="correlated_to",
            correlated_to="BTC/USDT",
        )
        assert c.correlated_to == "BTC/USDT"

    def test_force_candidate_clips_score(self):
        c = force_candidate("BTC/USDT", "crypto", forced_by="news_pulse", score_scan=5.0)
        assert c.score_scan == 1.0
        c2 = force_candidate("BTC/USDT", "crypto", forced_by="news_pulse", score_scan=-5.0)
        assert c2.score_scan == -1.0
