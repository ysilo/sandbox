"""
tests.test_regime — HMM regime detector (§12).

Couvre :
- features : construction matrice, transforms, fallback, gaps
- persistence : versioning, symlink/active.json, round-trip save/load, prune
- detector : train+detect, state_map inversion, volatility classifier,
  fallback last_regime, cold-start default
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest

from src.data.sources.base import MacroPoint, SourceUnavailable
from src.regime.features import (
    FEATURE_NAMES,
    FeatureFetchError,
    build_features,
)
from src.regime.hmm_detector import (
    DetectorConfig,
    InsufficientTrainingDataError,
    RegimeDetector,
    RegimeState,
)
from src.regime.persistence import ModelMeta, ModelStore


# ---------------------------------------------------------------------------
# Fakes : MacroSources déterministes
# ---------------------------------------------------------------------------


class _DummyModel:
    """Classe top-level picklable par joblib (nécessaire pour les tests
    ModelStore — les classes locales-in-def ne peuvent pas être picklées).
    """

    def __init__(self, tag: str = "dummy") -> None:
        self.tag = tag


@dataclass
class FakeMacroSource:
    """MacroSource de test : renvoie des séries numpy fixes par series_id."""

    name: str
    series: dict[str, list[MacroPoint]] = field(default_factory=dict)
    raise_for: set[str] = field(default_factory=set)

    def fetch_series(self, series_id: str, *, days: int) -> list[MacroPoint]:
        if series_id in self.raise_for:
            raise SourceUnavailable(f"fake {self.name} KO for {series_id}")
        pts = self.series.get(series_id)
        if pts is None:
            raise SourceUnavailable(f"fake {self.name}: series {series_id} absente")
        return pts[-days:]


def _make_daily_points(start: date, values: list[float]) -> list[MacroPoint]:
    return [
        MacroPoint(date=(start + timedelta(days=i)).isoformat(), value=v)
        for i, v in enumerate(values)
    ]


def _synthetic_macro_series(n_days: int = 200, seed: int = 1) -> dict[str, list[float]]:
    """Séries macro synthétiques : alterne 3 régimes pour que le HMM trouve."""
    rng = np.random.default_rng(seed)
    regime_len = n_days // 3
    # risk_on : SPX monte, VIX bas
    # risk_off : SPX baisse, VIX haut
    # transition : bruit
    spx = [100.0]
    vix, dxy, y10, btc = [], [], [], []
    for i in range(n_days):
        if i < regime_len:   # risk_on
            drift = 0.003; vol = 0.005; vix_lvl = 12 + rng.normal(0, 1)
        elif i < 2 * regime_len:  # risk_off
            drift = -0.004; vol = 0.015; vix_lvl = 35 + rng.normal(0, 3)
        else:                 # transition
            drift = 0.0; vol = 0.008; vix_lvl = 22 + rng.normal(0, 2)
        spx.append(spx[-1] * (1 + drift + rng.normal(0, vol)))
        vix.append(max(5.0, vix_lvl))
        dxy.append(100.0 + rng.normal(0, 0.3) + (i * 0.01 if i < regime_len else -0.01 * i))
        y10.append(3.5 + rng.normal(0, 0.05))
        btc.append(30_000 * (1 + drift * 2 + rng.normal(0, vol * 2)) ** max(i, 1))
    return {
        "spx": spx[1:],
        "vix": vix,
        "dxy": dxy,
        "y10": y10,
        "btc": btc,
    }


# ---------------------------------------------------------------------------
# Tests : features
# ---------------------------------------------------------------------------


class TestBuildFeatures:
    def _mk_sources(self, days=200, seed=1):
        """Crée deux FakeMacroSources (FRED + CoinGecko) avec séries cohérentes."""
        series = _synthetic_macro_series(days, seed=seed)
        start = date(2025, 1, 1)
        fred = FakeMacroSource(
            name="fred_fake",
            series={
                "SP500":    _make_daily_points(start, series["spx"]),
                "VIXCLS":   _make_daily_points(start, series["vix"]),
                "DTWEXBGS": _make_daily_points(start, series["dxy"]),
                "DGS10":    _make_daily_points(start, series["y10"]),
            },
        )
        coingecko = FakeMacroSource(
            name="coingecko_fake",
            series={"bitcoin": _make_daily_points(start, series["btc"])},
        )
        return fred, coingecko, start

    def test_matrix_shape_and_names(self):
        fred, coingecko, start = self._mk_sources(days=200)
        fm = build_features(
            fred=fred, coingecko=coingecko, window_days=60,
            today=start + timedelta(days=199),
        )
        assert fm.matrix.shape == (60, 5)
        assert len(FEATURE_NAMES) == 5
        assert fm.dates and len(fm.dates) == 60

    def test_spx_return_is_log_return(self):
        fred, coingecko, start = self._mk_sources(days=150)
        fm = build_features(
            fred=fred, coingecko=coingecko, window_days=30,
            today=start + timedelta(days=149),
        )
        # Feature 0 = log-returns, |valeur| typiquement < 0.05 sauf crise.
        assert np.all(np.abs(fm.matrix[:, 0]) < 0.2)

    def test_vix_level_untransformed(self):
        fred, coingecko, start = self._mk_sources(days=150)
        fm = build_features(
            fred=fred, coingecko=coingecko, window_days=30,
            today=start + timedelta(days=149),
        )
        # Feature 1 = VIX niveau brut — doit être ≥ 5 partout.
        assert np.all(fm.matrix[:, 1] >= 5.0)

    def test_fallback_on_primary_failure(self):
        # FRED tombe ; fallback Stooq fourni → doit réussir.
        series = _synthetic_macro_series(200, seed=1)
        start = date(2025, 1, 1)
        fred = FakeMacroSource(name="fred_fake", series={}, raise_for={
            "SP500", "VIXCLS", "DTWEXBGS", "DGS10",
        })
        stooq = FakeMacroSource(
            name="stooq_fake",
            series={
                "^spx":    _make_daily_points(start, series["spx"]),
                "^vix":    _make_daily_points(start, series["vix"]),
                "dx.f":    _make_daily_points(start, series["dxy"]),
                "10usy.b": _make_daily_points(start, series["y10"]),
            },
        )
        coingecko = FakeMacroSource(
            name="coingecko_fake",
            series={"bitcoin": _make_daily_points(start, series["btc"])},
        )
        fm = build_features(
            fred=fred, coingecko=coingecko, stooq_macro=stooq,
            window_days=60, today=start + timedelta(days=199),
        )
        assert fm.matrix.shape == (60, 5)

    def test_both_sources_fail_raises(self):
        coingecko = FakeMacroSource(name="cg", series={})
        fred = FakeMacroSource(name="fred", series={}, raise_for={"SP500"})
        with pytest.raises(FeatureFetchError):
            build_features(fred=fred, coingecko=coingecko, window_days=60)

    def test_insufficient_history_raises(self):
        fred, coingecko, start = self._mk_sources(days=30)   # trop court
        with pytest.raises(FeatureFetchError):
            build_features(
                fred=fred, coingecko=coingecko, window_days=60,
                today=start + timedelta(days=29),
            )


# ---------------------------------------------------------------------------
# Tests : persistence
# ---------------------------------------------------------------------------


class TestModelStore:
    def test_versioning_and_round_trip(self, tmp_path):
        store = ModelStore(tmp_path)
        assert store.list_versions() == []
        assert store.next_version() == 1

        # Fake model (joblib peut sérialiser n'importe quel objet picklable
        # défini au niveau module).
        model = _DummyModel(tag="v1")
        meta = ModelMeta(
            version=1,
            trained_at="2026-04-17T10:00:00Z",
            training_window={"start": "2021-04-17", "end": "2026-04-17"},
            n_observations=1260,
            feature_means=[0.0, 18.0, 0.0, 0.05, 0.02],
            feature_stds=[0.01, 7.0, 0.008, 0.12, 0.01],
            state_map={"0": "risk_off", "1": "transition", "2": "risk_on"},
        )
        path = store.save(model=model, meta=meta)
        assert path.exists()
        assert store.active_version() == 1

        model2, meta2 = store.load_active()
        assert isinstance(model2, _DummyModel)
        assert model2.tag == "v1"
        assert meta2.version == 1
        assert meta2.state_map["2"] == "risk_on"

    def test_prune_keeps_last_n(self, tmp_path):
        store = ModelStore(tmp_path, keep_n=3)

        for v in range(1, 6):
            meta = ModelMeta(
                version=v, trained_at="x", training_window={},
                n_observations=1000, feature_means=[], feature_stds=[],
                state_map={},
            )
            store.save(model=_DummyModel(tag=f"v{v}"), meta=meta)

        vs = store.list_versions()
        assert vs == [3, 4, 5]   # 1 et 2 purgés
        assert store.active_version() == 5

    def test_rollback(self, tmp_path):
        store = ModelStore(tmp_path)

        for v in (1, 2):
            meta = ModelMeta(
                version=v, trained_at="x", training_window={},
                n_observations=1000, feature_means=[], feature_stds=[],
                state_map={},
            )
            store.save(model=_DummyModel(tag=f"v{v}"), meta=meta)
        assert store.active_version() == 2
        store.rollback_to(1)
        assert store.active_version() == 1

    def test_last_regime_round_trip(self, tmp_path):
        store = ModelStore(tmp_path)
        cache = tmp_path / "cache" / "last_regime.json"
        payload = {"macro": "risk_on", "volatility": "low",
                   "probabilities": {"risk_on": 0.8}, "hmm_state": 2,
                   "date": "2026-04-17"}
        store.write_last_regime(payload, cache_path=cache)
        out = store.read_last_regime(cache_path=cache)
        assert out == payload

    def test_last_regime_absent_returns_none(self, tmp_path):
        store = ModelStore(tmp_path)
        assert store.read_last_regime(cache_path=tmp_path / "none.json") is None


# ---------------------------------------------------------------------------
# Tests : RegimeDetector
# ---------------------------------------------------------------------------


class TestRegimeState:
    def test_confidence_is_prob_of_macro(self):
        r = RegimeState(
            macro="risk_off", volatility="high",
            probabilities={"risk_on": 0.1, "transition": 0.2, "risk_off": 0.7},
            hmm_state=0, date="2026-04-17",
        )
        assert r.confidence == pytest.approx(0.7)

    def test_transition_default_uniform(self):
        r = RegimeState.transition_default("2026-04-17")
        assert r.macro == "transition"
        assert r.volatility == "mid"
        # 3 probas sommant à 1.0 ±ε
        assert sum(r.probabilities.values()) == pytest.approx(1.0, abs=1e-6)


class TestVolatilityClassifier:
    @pytest.mark.parametrize("vix,expected", [
        (10.0, "low"), (14.9, "low"),
        (15.0, "mid"), (24.9, "mid"),
        (25.0, "high"), (39.9, "high"),
        (40.0, "extreme"), (65.0, "extreme"),
    ])
    def test_thresholds(self, vix, expected):
        assert RegimeDetector._classify_volatility(vix) == expected


class TestStateMap:
    def test_spx_mean_ranking(self):
        """risk_on doit avoir le plus haut spx_return moyen ; risk_off le plus bas."""
        store = ModelStore(Path("/tmp/unused"))
        det = RegimeDetector(store=store)

        class FakeModel:
            means_ = np.array([
                [ 0.001, 18.0, 0.0, 0.0, 0.01],   # état 0 : milieu
                [-0.003, 30.0, 0.0, 0.0, 0.02],   # état 1 : bas  → risk_off
                [ 0.004, 12.0, 0.0, 0.0, 0.005],  # état 2 : haut → risk_on
            ])

        mapping = det._build_state_map(FakeModel(), np.zeros((10, 5)))
        assert mapping[1] == "risk_off"
        assert mapping[2] == "risk_on"
        assert mapping[0] == "transition"


class TestTrainAndDetect:
    @pytest.fixture
    def store(self, tmp_path):
        return ModelStore(tmp_path / "models")

    def test_insufficient_training_data_raises(self, store, tmp_path):
        det = RegimeDetector(store=store, config=DetectorConfig(min_training_obs=500))
        features = np.random.default_rng(0).normal(size=(100, 5))
        with pytest.raises(InsufficientTrainingDataError):
            det.train(features)

    def test_train_persists_model_and_meta(self, store, tmp_path):
        det = RegimeDetector(store=store, config=DetectorConfig(min_training_obs=300))
        # Features synthétiques avec 3 régimes : risk_on, risk_off, transition.
        rng = np.random.default_rng(42)
        n = 400
        segs = [
            rng.normal(loc=[ 0.005, 12, 0, 0, 0.01],
                       scale=[0.002, 1, 0.005, 0.05, 0.003], size=(n//3, 5)),
            rng.normal(loc=[-0.005, 30, 0, 0, 0.02],
                       scale=[0.003, 3, 0.005, 0.05, 0.005], size=(n//3, 5)),
            rng.normal(loc=[ 0.000, 20, 0, 0, 0.015],
                       scale=[0.002, 2, 0.005, 0.05, 0.004], size=(n - 2*(n//3), 5)),
        ]
        features = np.vstack(segs)
        meta = det.train(features)
        assert meta.version == 1
        assert store.active_version() == 1
        # state_map couvre bien les 3 régimes
        assert set(meta.state_map.values()) == {"risk_on", "risk_off", "transition"}

    def test_detect_end_to_end(self, store, tmp_path):
        """Train + detect sur features synthétiques — pipeline complet."""
        det = RegimeDetector(
            store=store, config=DetectorConfig(
                min_training_obs=300,
                cache_last_regime_path=str(tmp_path / "last.json"),
            ),
        )
        rng = np.random.default_rng(42)
        n = 400
        segs = [
            rng.normal(loc=[ 0.005, 12, 0, 0, 0.01],
                       scale=[0.002, 1, 0.005, 0.05, 0.003], size=(n//3, 5)),
            rng.normal(loc=[-0.005, 30, 0, 0, 0.02],
                       scale=[0.003, 3, 0.005, 0.05, 0.005], size=(n//3, 5)),
            rng.normal(loc=[ 0.000, 20, 0, 0, 0.015],
                       scale=[0.002, 2, 0.005, 0.05, 0.004], size=(n - 2*(n//3), 5)),
        ]
        features = np.vstack(segs)
        det.train(features)

        # Maintenant un detect() sur FakeMacroSources avec séries synthétiques
        series = _synthetic_macro_series(200, seed=1)
        start = date(2025, 1, 1)
        fred = FakeMacroSource(name="fred_fake", series={
            "SP500":    _make_daily_points(start, series["spx"]),
            "VIXCLS":   _make_daily_points(start, series["vix"]),
            "DTWEXBGS": _make_daily_points(start, series["dxy"]),
            "DGS10":    _make_daily_points(start, series["y10"]),
        })
        coingecko = FakeMacroSource(name="cg_fake", series={
            "bitcoin": _make_daily_points(start, series["btc"]),
        })
        det.fred = fred
        det.coingecko = coingecko

        today = datetime(2025, 7, 19, tzinfo=timezone.utc)   # dans la fenêtre
        regime = det.detect(today=today)
        assert regime.macro in ("risk_on", "transition", "risk_off")
        assert regime.volatility in ("low", "mid", "high", "extreme")
        # Probabilités en somme ≈ 1.0
        assert sum(regime.probabilities.values()) == pytest.approx(1.0, abs=1e-3)
        assert regime.date == today.date().isoformat()

        # last_regime.json persisté
        last = json.loads((tmp_path / "last.json").read_text())
        assert last["macro"] == regime.macro

    def test_detect_fallback_to_last_regime_when_fetch_fails(self, store, tmp_path):
        # Pré-enregistre un last_regime.
        cache = tmp_path / "last.json"
        payload = {
            "macro": "risk_off", "volatility": "high",
            "probabilities": {"risk_on": 0.1, "transition": 0.2, "risk_off": 0.7},
            "hmm_state": 0, "date": "2026-04-01",
        }
        cache.write_text(json.dumps(payload))

        det = RegimeDetector(
            store=store, config=DetectorConfig(
                min_training_obs=300, cache_last_regime_path=str(cache),
            ),
        )
        # Sources toutes en échec → fallback.
        det.fred = FakeMacroSource(name="x", series={}, raise_for={"SP500"})
        det.coingecko = FakeMacroSource(name="cg", series={})
        regime = det.detect()
        assert regime.macro == "risk_off"
        assert regime.confidence == pytest.approx(0.7)

    def test_detect_cold_start_default_transition(self, store, tmp_path):
        cache = tmp_path / "nothing.json"
        det = RegimeDetector(
            store=store,
            config=DetectorConfig(cache_last_regime_path=str(cache)),
        )
        det.fred = FakeMacroSource(name="x", series={}, raise_for={"SP500"})
        det.coingecko = FakeMacroSource(name="cg", series={})
        regime = det.detect()
        assert regime.macro == "transition"
        # probas uniformes
        assert all(p == pytest.approx(1/3) for p in regime.probabilities.values())
