"""
src.indicators.weights — poids des indicateurs dans les scores composites.

Source : TRADING_BOT_ARCHITECTURE.md §5.2.7, §5.3.6, §5.4.5, §5.5.

Les valeurs par défaut correspondent aux poids calibrés du document ; elles
peuvent être surchargées depuis `config/strategies.yaml` via la section
`weights` par stratégie (non implémenté en V1 — chaque stratégie peut les
charger depuis son propre block si besoin).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TrendWeights:
    supertrend: float = 0.25
    aroon: float = 0.22
    macd: float = 0.18
    adx_dir: float = 0.18
    psar: float = 0.10
    bollinger: float = 0.07


@dataclass(frozen=True)
class MomentumWeights:
    rsi: float = 0.35
    trix: float = 0.25
    cci: float = 0.20
    stoch: float = 0.12
    mom: float = 0.08


@dataclass(frozen=True)
class VolumeWeights:
    obv: float = 0.40
    cmf: float = 0.30
    vwap: float = 0.20
    vp: float = 0.10


@dataclass(frozen=True)
class CompositeWeights:
    """Pondération finale §5.5 :
    score = w_ichimoku×S_i + w_trend×S_t + w_momentum×S_m + w_volume×S_v.
    """
    ichimoku: float = 0.40
    trend: float = 0.25
    momentum: float = 0.20
    volume: float = 0.15


DEFAULT_TREND = TrendWeights()
DEFAULT_MOMENTUM = MomentumWeights()
DEFAULT_VOLUME = VolumeWeights()
DEFAULT_COMPOSITE = CompositeWeights()


__all__ = [
    "TrendWeights", "MomentumWeights", "VolumeWeights", "CompositeWeights",
    "DEFAULT_TREND", "DEFAULT_MOMENTUM", "DEFAULT_VOLUME", "DEFAULT_COMPOSITE",
]
