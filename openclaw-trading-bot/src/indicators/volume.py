"""
src.indicators.volume — OBV, VWAP, CMF, Volume Profile.

Source : TRADING_BOT_ARCHITECTURE.md §5.4.

Volume Profile alloue le volume de chaque barre proportionnellement à son
recouvrement avec chaque bin de prix → distribution plus lisse qu'un simple
placement sur le close.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd

if TYPE_CHECKING:
    from src.utils.config_loader import VolumeWeights  # noqa: F401


@dataclass
class VolumeProfileResult:
    poc: float
    vah: float
    val: float
    hvn_levels: list[float]
    lvn_levels: list[float]


def compute_obv(df: pd.DataFrame) -> pd.Series:
    direction = np.sign(df["close"].diff()).fillna(0.0)
    return (direction * df["volume"]).cumsum()


def compute_vwap(df: pd.DataFrame) -> pd.Series:
    """VWAP cumulatif sur toute la série (adapté pour timeframes ≥ daily)."""
    tp = (df["high"] + df["low"] + df["close"]) / 3.0
    cumvol = df["volume"].cumsum()
    cumtpv = (tp * df["volume"]).cumsum()
    return cumtpv / cumvol.replace(0, np.nan)


def compute_cmf(df: pd.DataFrame, *, period: int = 20) -> pd.Series:
    hl_range = (df["high"] - df["low"]).replace(0, np.nan)
    mfm = ((df["close"] - df["low"]) - (df["high"] - df["close"])) / hl_range
    mfv = mfm * df["volume"]
    return mfv.rolling(period).sum() / df["volume"].rolling(period).sum().replace(0, np.nan)


def compute_volume_profile(
    df: pd.DataFrame,
    *,
    lookback: int = 200,
    bins: int = 50,
) -> VolumeProfileResult:
    data = df.tail(lookback)
    if len(data) == 0:
        return VolumeProfileResult(poc=float("nan"), vah=float("nan"), val=float("nan"),
                                   hvn_levels=[], lvn_levels=[])

    price_min = float(data["low"].min())
    price_max = float(data["high"].max())
    if not np.isfinite(price_min) or not np.isfinite(price_max) or price_min == price_max:
        # Dégénéré : série plate → POC = prix unique, pas de VA
        poc = price_min if np.isfinite(price_min) else float("nan")
        return VolumeProfileResult(poc=poc, vah=poc, val=poc, hvn_levels=[], lvn_levels=[])

    edges = np.linspace(price_min, price_max, bins + 1)
    vol_per_bin = np.zeros(bins)
    highs = data["high"].to_numpy()
    lows = data["low"].to_numpy()
    vols = data["volume"].to_numpy()

    for i in range(len(data)):
        bar_range = highs[i] - lows[i]
        if bar_range <= 0:
            continue
        for j in range(bins):
            overlap = min(highs[i], edges[j + 1]) - max(lows[i], edges[j])
            if overlap > 0:
                vol_per_bin[j] += vols[i] * (overlap / bar_range)

    poc_idx = int(np.argmax(vol_per_bin))
    poc = float((edges[poc_idx] + edges[poc_idx + 1]) / 2.0)

    total = float(vol_per_bin.sum())
    target = total * 0.70
    va_vol = vol_per_bin[poc_idx]
    lo = hi = poc_idx
    while va_vol < target and (lo > 0 or hi < bins - 1):
        up = vol_per_bin[hi + 1] if hi + 1 < bins else 0.0
        dn = vol_per_bin[lo - 1] if lo > 0 else 0.0
        if up >= dn and hi + 1 < bins:
            hi += 1
            va_vol += up
        elif lo > 0:
            lo -= 1
            va_vol += dn
        else:
            break

    mean_vol = float(vol_per_bin.mean()) if total > 0 else 0.0
    hvn = [
        float((edges[i] + edges[i + 1]) / 2.0)
        for i in range(bins) if vol_per_bin[i] > mean_vol * 1.5
    ]
    lvn = [
        float((edges[i] + edges[i + 1]) / 2.0)
        for i in range(bins) if 0 < vol_per_bin[i] < mean_vol * 0.5
    ]

    return VolumeProfileResult(
        poc=poc,
        vah=float((edges[hi] + edges[hi + 1]) / 2.0),
        val=float((edges[lo] + edges[lo + 1]) / 2.0),
        hvn_levels=hvn,
        lvn_levels=lvn,
    )


def volume_profile_signal(close: float, vp: VolumeProfileResult) -> float:
    """Signal structurel : 0 dans la VA, ±0.5 au-dessus/en-dessous du POC."""
    if pd.isna(close) or pd.isna(vp.poc):
        return 0.0
    if not pd.isna(vp.val) and not pd.isna(vp.vah) and vp.val <= close <= vp.vah:
        return 0.0
    return 0.5 if close > vp.poc else -0.5


@dataclass
class VolumeScore:
    score: float
    components: dict
    vp: VolumeProfileResult | None = None

    @classmethod
    def compute(cls, df: pd.DataFrame, weights: "VolumeWeights") -> "VolumeScore":
        if "volume" not in df.columns or df["volume"].fillna(0).sum() == 0:
            # Forex : pas de volume significatif → score neutre, pas d'erreur
            return cls(score=0.0, components={"filtered": "volume absent/zéro"}, vp=None)

        obv = compute_obv(df)
        vwap = compute_vwap(df)
        cmf = compute_cmf(df, period=20)
        vp = compute_volume_profile(df, lookback=min(200, len(df)))

        obv_slope = float(np.sign(obv.iloc[-1] - obv.iloc[-5])) if len(obv) >= 5 else 0.0
        close_last = float(df["close"].iloc[-1])
        vwap_last = float(vwap.iloc[-1]) if not pd.isna(vwap.iloc[-1]) else close_last
        price_vs_vwap = float(np.sign(close_last - vwap_last))
        cmf_last = cmf.iloc[-1]
        cmf_signal = float(np.clip((cmf_last * 5.0) if not pd.isna(cmf_last) else 0.0, -1.0, 1.0))
        vp_signal = volume_profile_signal(close_last, vp)

        signals = {
            "obv": obv_slope,
            "vwap": price_vs_vwap,
            "cmf": cmf_signal,
            "vp": vp_signal,
        }
        score = sum(signals[k] * float(getattr(weights, k)) for k in signals)
        return cls(score=float(np.clip(score, -1.0, 1.0)), components=signals, vp=vp)


__all__ = [
    "VolumeProfileResult",
    "compute_obv", "compute_vwap", "compute_cmf", "compute_volume_profile",
    "volume_profile_signal",
    "VolumeScore",
]
