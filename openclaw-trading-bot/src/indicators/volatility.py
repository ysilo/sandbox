"""
src.indicators.volatility — ATR, std log-returns, BB width, percentile vol.

Ces indicateurs sont utilisés transverse : par les stratégies (sizing, trailing),
par le risk gate (circuit breaker §11), et par le régime detector (HMM features
§12.2 : `realized_vol_20`).
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass
class VolatilityProfile:
    atr: float                   # Average True Range (absolu)
    atr_pct: float               # ATR / close (relatif)
    realized_vol_20: float       # std des log-returns 20j annualisée grossièrement
    bb_bandwidth: float          # (upper - lower) / middle à la dernière barre
    percentile_1y: float         # percentile (0..100) du bandwidth courant sur 252 barres
    components: dict


def compute_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """True Range + lissage Wilder (EMA alpha=1/period)."""
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift()
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1.0 / period, adjust=False).mean()


def compute_atr_pct(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """ATR normalisé par le close → comparable entre actifs/timeframes."""
    atr = compute_atr(df, period=period)
    return atr / df["close"].replace(0, np.nan)


def compute_realized_vol(series: pd.Series, window: int = 20) -> pd.Series:
    """Ecart-type roulant des log-returns."""
    logret = np.log(series / series.shift(1))
    return logret.rolling(window).std()


def compute_bb_bandwidth(df: pd.DataFrame, period: int = 20, std_dev: float = 2.0) -> pd.Series:
    """Bollinger bandwidth = (upper - lower) / middle, adimensionnel."""
    close = df["close"]
    middle = close.rolling(period).mean()
    sigma = close.rolling(period).std()
    upper = middle + std_dev * sigma
    lower = middle - std_dev * sigma
    return (upper - lower) / middle.replace(0, np.nan)


def compute_volatility_profile(
    df: pd.DataFrame,
    *,
    atr_period: int = 14,
    vol_window: int = 20,
    bb_period: int = 20,
    percentile_window: int = 252,
) -> VolatilityProfile:
    """Profil synthétique de volatilité — utilisé pour feature engineering HMM et sizing."""
    atr_series = compute_atr(df, period=atr_period)
    atr_pct_series = compute_atr_pct(df, period=atr_period)
    realized = compute_realized_vol(df["close"], window=vol_window)
    bw_series = compute_bb_bandwidth(df, period=bb_period)

    # Percentile du bandwidth courant dans la fenêtre 252 derniers (= 1 an daily)
    tail = bw_series.dropna().tail(percentile_window)
    last_bw = bw_series.iloc[-1] if len(bw_series) else float("nan")
    if len(tail) and not pd.isna(last_bw):
        percentile = float((tail.values <= last_bw).mean() * 100.0)
    else:
        percentile = float("nan")

    def _last(s: pd.Series) -> float:
        return float(s.iloc[-1]) if len(s) and not pd.isna(s.iloc[-1]) else float("nan")

    return VolatilityProfile(
        atr=_last(atr_series),
        atr_pct=_last(atr_pct_series),
        realized_vol_20=_last(realized),
        bb_bandwidth=_last(bw_series),
        percentile_1y=percentile,
        components={
            "atr_period": atr_period,
            "vol_window": vol_window,
            "bb_period": bb_period,
            "percentile_window": percentile_window,
        },
    )


__all__ = [
    "VolatilityProfile",
    "compute_atr", "compute_atr_pct", "compute_realized_vol",
    "compute_bb_bandwidth", "compute_volatility_profile",
]
