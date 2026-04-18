"""
src.regime.hmm_detector — RegimeDetector GaussianHMM 3 états (§12).

Contrat :
- `detect()` → `RegimeState` (macro ∈ {risk_on, transition, risk_off}, volatility,
  probabilités, hmm_state, date).
- Si le fetch features échoue (primaire + fallback KO), lit `last_regime.json`.
- Si `last_regime.json` absent : cold-start `transition` probas uniformes.
- `train(features)` persiste une nouvelle version via `ModelStore`.
- State-map (cluster → macro) dérivé du rang des moyennes de `spx_return` :
  plus haut = risk_on, plus bas = risk_off, milieu = transition.
- `_classify_volatility(vix)` : <15 low, <25 mid, <40 high, ≥40 extreme (§12.2.4).

Dépendances lazy : `hmmlearn` et `joblib` sont importés au training/load time,
pas au import. Permet d'importer le module en test sans hmmlearn (on patchera
la méthode `_fit`).
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Optional, Protocol

import numpy as np

from src.regime.features import (
    FEATURE_NAMES,
    FeatureFetchError,
    FeatureSourceConfig,
    build_features,
)
from src.regime.persistence import ModelMeta, ModelStore

log = logging.getLogger(__name__)


# Seuils VIX §12.2.4
_VOL_LOW = 15.0
_VOL_MID = 25.0
_VOL_HIGH = 40.0


@dataclass
class RegimeState:
    macro: str                                # "risk_on" | "transition" | "risk_off"
    volatility: str                           # "low" | "mid" | "high" | "extreme"
    probabilities: dict[str, float]           # {"risk_on": 0.7, ...}
    hmm_state: int
    date: str                                 # YYYY-MM-DD

    @property
    def confidence(self) -> float:
        """Probabilité du macro state — utile pour les gates (C9)."""
        return float(self.probabilities.get(self.macro, 0.0))

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def transition_default(cls, today: Optional[str] = None) -> "RegimeState":
        today = today or datetime.now(tz=timezone.utc).date().isoformat()
        return cls(
            macro="transition",
            volatility="mid",
            probabilities={"risk_on": 1 / 3, "transition": 1 / 3, "risk_off": 1 / 3},
            hmm_state=1,
            date=today,
        )


class _MacroLike(Protocol):
    name: str

    def fetch_series(self, series_id: str, *, days: int) -> list:
        ...


# ---------------------------------------------------------------------------
# RegimeDetector
# ---------------------------------------------------------------------------


class InsufficientTrainingDataError(RuntimeError):
    """Pas assez d'observations pour train un HMM stable (< 750 obs)."""


@dataclass
class DetectorConfig:
    window_days: int = 60
    n_components: int = 3
    covariance_type: str = "diag"
    n_iter: int = 200
    random_state: int = 42
    min_training_obs: int = 750                 # ~3 ans de daily
    cache_last_regime_path: str = "data/cache/last_regime.json"


class RegimeDetector:
    """HMM 3 états pour détection de régime macro (§12)."""

    def __init__(
        self,
        *,
        store: ModelStore,
        fred: Optional[_MacroLike] = None,
        coingecko: Optional[_MacroLike] = None,
        stooq_macro: Optional[_MacroLike] = None,
        config: Optional[DetectorConfig] = None,
        feature_cfg: Optional[FeatureSourceConfig] = None,
    ) -> None:
        self.store = store
        self.fred = fred
        self.coingecko = coingecko
        self.stooq_macro = stooq_macro
        self.cfg = config or DetectorConfig()
        self.feature_cfg = feature_cfg or FeatureSourceConfig()
        self._model: Optional[Any] = None
        self._meta: Optional[ModelMeta] = None

    # ------------------------------------------------------------------
    # Lazy load du modèle
    # ------------------------------------------------------------------

    def _ensure_loaded(self) -> None:
        if self._model is not None:
            return
        try:
            self._model, self._meta = self.store.load_active()
            log.info("regime model loaded v%d", self._meta.version)
        except FileNotFoundError:
            log.warning("no regime model on disk — detector will use last_regime fallback")
            self._model = None
            self._meta = None

    # ------------------------------------------------------------------
    # API publique
    # ------------------------------------------------------------------

    def detect(self, *, today: Optional[datetime] = None) -> RegimeState:
        """Détecte le régime courant. Fallback sur last_regime si indispo."""
        today_date = (today or datetime.now(tz=timezone.utc)).date()

        # 1. Fetch features
        try:
            features = build_features(
                fred=self.fred,
                coingecko=self.coingecko,
                stooq_macro=self.stooq_macro,
                window_days=self.cfg.window_days,
                config=self.feature_cfg,
                today=today_date,
            )
        except FeatureFetchError as exc:
            log.error("regime_stale_features : %s — fallback last_regime", exc)
            return self._load_last_regime_or_default(today_date.isoformat())

        # 2. Load model (ou fallback)
        self._ensure_loaded()
        if self._model is None or self._meta is None:
            log.warning("regime model missing — fallback last_regime")
            return self._load_last_regime_or_default(today_date.isoformat())

        # 3. Predict
        state_seq = self._model.predict(features.matrix)
        proba_seq = self._model.predict_proba(features.matrix)

        current_state = int(state_seq[-1])
        current_proba = proba_seq[-1]

        state_map = {int(k): v for k, v in self._meta.state_map.items()}
        probs = {state_map[i]: float(current_proba[i]) for i in range(len(current_proba))}

        regime = RegimeState(
            macro=state_map[current_state],
            volatility=self._classify_volatility(features.matrix[-1, 1]),
            probabilities=probs,
            hmm_state=current_state,
            date=today_date.isoformat(),
        )
        self._persist_last_regime(regime)
        return regime

    def train(
        self,
        features: np.ndarray,
        *,
        training_window: Optional[dict[str, str]] = None,
    ) -> ModelMeta:
        """Fit un nouveau HMM sur `features` et persiste via ModelStore.

        Retourne les métadonnées du modèle entraîné. Le modèle devient actif
        immédiatement (symlink/active.json).
        """
        if len(features) < self.cfg.min_training_obs:
            raise InsufficientTrainingDataError(
                f"got {len(features)} obs, need ≥ {self.cfg.min_training_obs}"
            )
        model = self._fit(features)
        state_map = self._build_state_map(model, features)
        version = self.store.next_version()
        meta = ModelMeta(
            version=version,
            trained_at=datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            training_window=training_window or {
                "start": "unknown",
                "end": "unknown",
            },
            n_observations=int(len(features)),
            feature_means=[float(x) for x in features.mean(axis=0)],
            feature_stds=[float(x) for x in features.std(axis=0)],
            state_map={str(k): v for k, v in state_map.items()},
            hmm_params={
                "n_components": self.cfg.n_components,
                "covariance_type": self.cfg.covariance_type,
                "n_iter": self.cfg.n_iter,
                "random_state": self.cfg.random_state,
            },
        )
        self.store.save(model=model, meta=meta)
        self._model = model
        self._meta = meta
        log.info("regime model trained v%d (n=%d)", version, len(features))
        return meta

    # ------------------------------------------------------------------
    # Helpers — HMM fit + state mapping
    # ------------------------------------------------------------------

    def _fit(self, features: np.ndarray) -> Any:
        from hmmlearn import hmm

        model = hmm.GaussianHMM(
            n_components=self.cfg.n_components,
            covariance_type=self.cfg.covariance_type,
            n_iter=self.cfg.n_iter,
            random_state=self.cfg.random_state,
        )
        model.fit(features)
        return model

    def _build_state_map(self, model: Any, features: np.ndarray) -> dict[int, str]:
        """Attribue {risk_on, transition, risk_off} aux clusters par rang du
        `spx_return` moyen (feature 0) en chaque état.

        - État au `spx_return` le plus élevé → risk_on
        - État au `spx_return` le plus bas   → risk_off
        - État restant                       → transition
        """
        means = model.means_   # shape (n_components, n_features)
        if means.shape[0] != 3:
            # On laisse du mou si l'utilisateur a changé n_components.
            # Map arbitraire : indice → "state_i"
            return {i: f"state_{i}" for i in range(means.shape[0])}

        spx_means = means[:, 0]
        order = np.argsort(spx_means)   # asc : [risk_off, transition, risk_on]
        idx_risk_off    = int(order[0])
        idx_transition  = int(order[1])
        idx_risk_on     = int(order[2])
        return {
            idx_risk_off:   "risk_off",
            idx_transition: "transition",
            idx_risk_on:    "risk_on",
        }

    # ------------------------------------------------------------------
    # Volatility classifier
    # ------------------------------------------------------------------

    @staticmethod
    def _classify_volatility(vix_level: float) -> str:
        if vix_level < _VOL_LOW:
            return "low"
        if vix_level < _VOL_MID:
            return "mid"
        if vix_level < _VOL_HIGH:
            return "high"
        return "extreme"

    # ------------------------------------------------------------------
    # last_regime cache
    # ------------------------------------------------------------------

    def _persist_last_regime(self, regime: RegimeState) -> None:
        try:
            self.store.write_last_regime(
                regime.to_dict(),
                cache_path=self.cfg.cache_last_regime_path,
            )
        except OSError as exc:
            log.error("persist last_regime KO (%s)", exc)

    def _load_last_regime_or_default(self, today_iso: str) -> RegimeState:
        cached = self.store.read_last_regime(cache_path=self.cfg.cache_last_regime_path)
        if cached is None:
            return RegimeState.transition_default(today_iso)
        try:
            return RegimeState(**cached)
        except (TypeError, ValueError) as exc:
            log.error("last_regime.json payload invalide (%s) — défaut transition", exc)
            return RegimeState.transition_default(today_iso)


__all__ = [
    "RegimeDetector",
    "RegimeState",
    "DetectorConfig",
    "InsufficientTrainingDataError",
    "FEATURE_NAMES",
]
