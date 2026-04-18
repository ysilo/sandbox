"""
src.regime — détection de régime de marché via HMM 3 états (§12).

Pipeline :
    FRED/CoinGecko → `build_features()` → (N, 5) matrix
      → `RegimeDetector.detect()` → `RegimeState(macro, volatility, probas, ...)`

Le régime est consommé par :
- `strategy-selector` (§8.8.3) pour pondérer les stratégies par régime
- `SignalCrossing` via `regime_context` (risk_on/transition/risk_off)
- `RiskGate.C9_macro_volatility` (§11.6) pour le crash-mode

Model store : `data/models/regime_hmm_v{n}.pkl` + `.meta.json` + symlink.
Last-regime cache : `data/cache/last_regime.json` (fallback si fetch KO).
"""
from __future__ import annotations

from .features import (
    FEATURE_NAMES,
    FeatureFetchError,
    FeatureMatrix,
    FeatureSourceConfig,
    build_features,
)
from .hmm_detector import (
    DetectorConfig,
    InsufficientTrainingDataError,
    RegimeDetector,
    RegimeState,
)
from .persistence import ModelMeta, ModelStore

__all__ = [
    # features
    "FEATURE_NAMES",
    "FeatureFetchError",
    "FeatureMatrix",
    "FeatureSourceConfig",
    "build_features",
    # detector
    "RegimeDetector",
    "RegimeState",
    "DetectorConfig",
    "InsufficientTrainingDataError",
    # persistence
    "ModelMeta",
    "ModelStore",
]
