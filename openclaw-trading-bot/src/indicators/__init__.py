"""
src.indicators — familles d'indicateurs techniques (TRADING_BOT_ARCHITECTURE.md §5).

Ichimoku est le seul indicateur obligatoire (§5.1) : tout signal Ichimoku neutre
ou contraire à la direction est rejeté par le risk gate (§11, check C3).
"""
from __future__ import annotations

from .ichimoku import IchimokuResult, compute_ichimoku, ichimoku_payload
from .momentum import (
    MomentumScore,
    StochResult,
    TRIXResult,
    compute_cci,
    compute_momentum,
    compute_rsi,
    compute_stochastic,
    compute_trix,
    rsi_signal,
    stoch_signal,
)
from .trend import (
    ADXResult,
    AroonResult,
    BollingerResult,
    MACDResult,
    TrendScore,
    compute_adx,
    compute_aroon,
    compute_bollinger,
    compute_macd,
    compute_parabolic_sar,
    compute_supertrend,
)
from .volatility import (
    VolatilityProfile,
    compute_atr,
    compute_atr_pct,
    compute_bb_bandwidth,
    compute_realized_vol,
    compute_volatility_profile,
)
from .volume import (
    VolumeProfileResult,
    VolumeScore,
    compute_cmf,
    compute_obv,
    compute_volume_profile,
    compute_vwap,
    volume_profile_signal,
)
from .weights import (
    DEFAULT_COMPOSITE,
    DEFAULT_MOMENTUM,
    DEFAULT_TREND,
    DEFAULT_VOLUME,
    CompositeWeights,
    MomentumWeights,
    TrendWeights,
    VolumeWeights,
)

__all__ = [
    # ichimoku
    "IchimokuResult", "compute_ichimoku", "ichimoku_payload",
    # trend
    "MACDResult", "AroonResult", "ADXResult", "BollingerResult", "TrendScore",
    "compute_supertrend", "compute_macd", "compute_parabolic_sar",
    "compute_aroon", "compute_adx", "compute_bollinger",
    # momentum
    "StochResult", "TRIXResult", "MomentumScore",
    "compute_rsi", "compute_stochastic", "compute_trix",
    "compute_cci", "compute_momentum",
    "rsi_signal", "stoch_signal",
    # volume
    "VolumeProfileResult", "VolumeScore",
    "compute_obv", "compute_vwap", "compute_cmf", "compute_volume_profile",
    "volume_profile_signal",
    # volatility
    "VolatilityProfile", "compute_atr", "compute_atr_pct",
    "compute_realized_vol", "compute_bb_bandwidth", "compute_volatility_profile",
    # weights
    "TrendWeights", "MomentumWeights", "VolumeWeights", "CompositeWeights",
    "DEFAULT_TREND", "DEFAULT_MOMENTUM", "DEFAULT_VOLUME", "DEFAULT_COMPOSITE",
]
