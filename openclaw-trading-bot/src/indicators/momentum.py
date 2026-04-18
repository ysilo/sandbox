"""
src.indicators.momentum — RSI, Stochastique, TRIX, CCI, Momentum.

Source : TRADING_BOT_ARCHITECTURE.md §5.3.

Note sur les normalisations appliquées dans `MomentumScore` :
- TRIX : multiplié par 10 puis clippé à [-1, 1] — TRIX est une variation en %
  de l'EMA triple, typiquement 0–0.5 % en daily.
- CCI : divisé par 150 puis clippé — le CCI prend couramment des valeurs
  entre ±100 et ±200 aux extrêmes.
- Momentum : normalisé par le close ; un move d'amplitude ≥ 5 % en 12 barres
  sature à ±1.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd

if TYPE_CHECKING:
    from src.utils.config_loader import MomentumWeights  # noqa: F401


# ---------------------------------------------------------------------------
# Résultats typés
# ---------------------------------------------------------------------------


@dataclass
class StochResult:
    k: pd.Series
    d: pd.Series


@dataclass
class TRIXResult:
    line: pd.Series
    signal: pd.Series
    histogram: pd.Series


# ---------------------------------------------------------------------------
# Indicateurs individuels
# ---------------------------------------------------------------------------


def compute_rsi(df: pd.DataFrame, *, period: int = 14) -> pd.Series:
    """RSI Wilder (EMA alpha=1/period) — plage 0..100.

    Edge cases :
    - Si `avg_loss == 0` (série purement haussière sans la moindre baisse),
      RSI = 100 par convention (pas de NaN).
    - Symétriquement, si `avg_gain == 0` (purement baissière), RSI = 0.
    """
    delta = df["close"].diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_g = gain.ewm(alpha=1.0 / period, adjust=False).mean()
    avg_l = loss.ewm(alpha=1.0 / period, adjust=False).mean()

    rsi = 100.0 - (100.0 / (1.0 + (avg_g / avg_l.replace(0, np.nan))))
    # Résoudre les NaN dus à avg_l = 0 : tout-gain → RSI=100, tout-perte → RSI=0
    rsi = rsi.where(~((avg_l == 0) & (avg_g > 0)), 100.0)
    rsi = rsi.where(~((avg_g == 0) & (avg_l > 0)), 0.0)
    return rsi


def compute_stochastic(
    df: pd.DataFrame,
    *,
    k: int = 14,
    d: int = 3,
    smooth: int = 5,
) -> StochResult:
    low_k = df["low"].rolling(k).min()
    high_k = df["high"].rolling(k).max()
    pct_k_raw = 100.0 * (df["close"] - low_k) / (high_k - low_k).replace(0, np.nan)
    pct_k = pct_k_raw.rolling(smooth).mean()
    pct_d = pct_k.rolling(d).mean()
    return StochResult(k=pct_k, d=pct_d)


def compute_trix(df: pd.DataFrame, *, period: int = 15, signal: int = 9) -> TRIXResult:
    ema1 = df["close"].ewm(span=period, adjust=False).mean()
    ema2 = ema1.ewm(span=period, adjust=False).mean()
    ema3 = ema2.ewm(span=period, adjust=False).mean()
    line = ema3.pct_change() * 100.0
    sig = line.ewm(span=signal, adjust=False).mean()
    return TRIXResult(line=line, signal=sig, histogram=line - sig)


def compute_cci(df: pd.DataFrame, *, period: int = 20) -> pd.Series:
    tp = (df["high"] + df["low"] + df["close"]) / 3.0
    sma = tp.rolling(period).mean()
    mad = tp.rolling(period).apply(
        lambda x: np.mean(np.abs(x - x.mean())), raw=True,
    )
    return (tp - sma) / (0.015 * mad.replace(0, np.nan))


def compute_momentum(df: pd.DataFrame, *, period: int = 12) -> pd.Series:
    return df["close"] - df["close"].shift(period)


# ---------------------------------------------------------------------------
# Signaux scalaires
# ---------------------------------------------------------------------------


def rsi_signal(rsi_val: float) -> float:
    """Zones RSI → score signé (§5.3.1)."""
    if pd.isna(rsi_val):
        return 0.0
    if rsi_val < 30:
        return 0.8
    if rsi_val < 40:
        return 0.4
    if rsi_val < 45:
        return 0.2
    if rsi_val > 70:
        return -0.8
    if rsi_val > 60:
        return -0.4
    if rsi_val > 55:
        return -0.2
    return 0.0


def stoch_signal(stoch: StochResult) -> float:
    """Croisements K/D en zones extrêmes uniquement."""
    if len(stoch.k) < 2 or len(stoch.d) < 2:
        return 0.0
    k = stoch.k.iloc[-1]
    d = stoch.d.iloc[-1]
    k_prev = stoch.k.iloc[-2]
    d_prev = stoch.d.iloc[-2]
    if pd.isna(k) or pd.isna(d) or pd.isna(k_prev) or pd.isna(d_prev):
        return 0.0
    if k < 20 and k > d and k_prev <= d_prev:
        return 0.6
    if k > 80 and k < d and k_prev >= d_prev:
        return -0.6
    if k < 20:
        return 0.3
    if k > 80:
        return -0.3
    return 0.0


# ---------------------------------------------------------------------------
# Score composite momentum
# ---------------------------------------------------------------------------


@dataclass
class MomentumScore:
    score: float
    rsi: float
    components: dict

    @classmethod
    def compute(cls, df: pd.DataFrame, weights: "MomentumWeights") -> "MomentumScore":
        rsi = compute_rsi(df, period=14)
        stoch = compute_stochastic(df, k=14, d=3, smooth=5)
        trix = compute_trix(df, period=15, signal=9)
        cci = compute_cci(df, period=20)
        mom = compute_momentum(df, period=12)

        def _safe_last(s: pd.Series, default: float = 0.0) -> float:
            if not len(s):
                return default
            v = s.iloc[-1]
            return float(v) if not pd.isna(v) else default

        rsi_last = _safe_last(rsi, float("nan"))
        close_last = float(df["close"].iloc[-1])
        mom_last = _safe_last(mom)
        mom_norm = np.sign(mom_last) * min(abs(mom_last) / (abs(close_last) * 0.05), 1.0) \
            if close_last and not pd.isna(close_last) else 0.0

        signals = {
            "rsi":   rsi_signal(rsi_last),
            "stoch": stoch_signal(stoch),
            "trix":  float(np.clip(_safe_last(trix.line) * 10.0, -1.0, 1.0)),
            "cci":   float(np.clip(_safe_last(cci) / 150.0, -1.0, 1.0)),
            "mom":   float(mom_norm),
        }
        score = sum(signals[k] * float(getattr(weights, k)) for k in signals)
        return cls(
            score=float(np.clip(score, -1.0, 1.0)),
            rsi=float(rsi_last) if not pd.isna(rsi_last) else 0.0,
            components=signals,
        )


__all__ = [
    "StochResult", "TRIXResult",
    "compute_rsi", "compute_stochastic", "compute_trix",
    "compute_cci", "compute_momentum",
    "rsi_signal", "stoch_signal",
    "MomentumScore",
]
