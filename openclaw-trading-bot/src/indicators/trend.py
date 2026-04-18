"""
src.indicators.trend — Supertrend, MACD, PSAR, Aroon, ADX+DMI, Bollinger.

Source : TRADING_BOT_ARCHITECTURE.md §5.2.

Tous les indicateurs renvoient des `pd.Series` ou des dataclasses typées. Le
score composite `TrendScore.compute(df, weights)` applique un pré-filtre ADX
< 20 qui force `score=0` en marché sans tendance (§5.2.5).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd

from .volatility import compute_atr

if TYPE_CHECKING:
    from src.utils.config_loader import TrendWeights  # noqa: F401


# ---------------------------------------------------------------------------
# Résultats typés
# ---------------------------------------------------------------------------


@dataclass
class MACDResult:
    line: pd.Series
    signal: pd.Series
    histogram: pd.Series


@dataclass
class AroonResult:
    up: pd.Series
    down: pd.Series
    oscillator: pd.Series


@dataclass
class ADXResult:
    adx: pd.Series
    plus_di: pd.Series
    minus_di: pd.Series


@dataclass
class BollingerResult:
    upper: pd.Series
    middle: pd.Series
    lower: pd.Series
    pct_b: pd.Series
    bandwidth: pd.Series


# ---------------------------------------------------------------------------
# Indicateurs individuels
# ---------------------------------------------------------------------------


def compute_supertrend(
    df: pd.DataFrame,
    *,
    atr_period: int = 10,
    multiplier: float = 3.0,
) -> pd.Series:
    """Direction du Supertrend : +1 haussier, -1 baissier (NaN tant qu'indéfini)."""
    atr = compute_atr(df, period=atr_period)
    hl2 = (df["high"] + df["low"]) / 2
    upper_base = hl2 + multiplier * atr
    lower_base = hl2 - multiplier * atr

    n = len(df)
    direction = np.full(n, np.nan)
    final_upper = upper_base.to_numpy(copy=True)
    final_lower = lower_base.to_numpy(copy=True)
    close = df["close"].to_numpy()

    for i in range(1, n):
        if np.isnan(final_upper[i]) or np.isnan(final_lower[i]):
            continue
        # Bandes ajustées comme dans la plupart des implémentations standard
        if not np.isnan(final_upper[i - 1]):
            if upper_base.iloc[i] < final_upper[i - 1] or close[i - 1] > final_upper[i - 1]:
                pass  # garder upper_base[i]
            else:
                final_upper[i] = final_upper[i - 1]
        if not np.isnan(final_lower[i - 1]):
            if lower_base.iloc[i] > final_lower[i - 1] or close[i - 1] < final_lower[i - 1]:
                pass
            else:
                final_lower[i] = final_lower[i - 1]

        prev_dir = direction[i - 1] if not np.isnan(direction[i - 1]) else 1.0
        if close[i] > final_upper[i - 1] if not np.isnan(final_upper[i - 1]) else False:
            direction[i] = 1.0
        elif close[i] < final_lower[i - 1] if not np.isnan(final_lower[i - 1]) else False:
            direction[i] = -1.0
        else:
            direction[i] = prev_dir

    return pd.Series(direction, index=df.index, name="supertrend_dir")


def compute_macd(
    df: pd.DataFrame,
    *,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> MACDResult:
    close = df["close"]
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    line = ema_fast - ema_slow
    sig = line.ewm(span=signal, adjust=False).mean()
    return MACDResult(line=line, signal=sig, histogram=line - sig)


def compute_parabolic_sar(
    df: pd.DataFrame,
    *,
    step: float = 0.02,
    max_step: float = 0.2,
) -> pd.Series:
    """Retourne la direction : +1 haussier (prix > SAR), -1 baissier.

    Implémentation itérative standard (Wilder) ; les deux premières barres sont
    utilisées pour initialiser EP et la direction.
    """
    high = df["high"].to_numpy()
    low = df["low"].to_numpy()
    n = len(df)
    direction = np.zeros(n)
    sar = np.full(n, np.nan)
    if n < 2:
        return pd.Series(direction, index=df.index)

    # Initialisation : direction = haussière si close(1) >= close(0), sinon baissière.
    close = df["close"].to_numpy()
    direction[0] = 1.0 if close[1] >= close[0] else -1.0
    direction[1] = direction[0]
    af = step
    if direction[1] == 1.0:
        sar[1] = low[0]
        ep = high[1]
    else:
        sar[1] = high[0]
        ep = low[1]

    for i in range(2, n):
        prev_dir = direction[i - 1]
        prev_sar = sar[i - 1]
        new_sar = prev_sar + af * (ep - prev_sar)
        reversed_ = False
        if prev_dir == 1.0:
            new_sar = min(new_sar, low[i - 1], low[i - 2])
            if low[i] < new_sar:
                direction[i] = -1.0
                new_sar = ep
                ep = low[i]
                af = step
                reversed_ = True
            else:
                direction[i] = 1.0
                if high[i] > ep:
                    ep = high[i]
                    af = min(af + step, max_step)
        else:
            new_sar = max(new_sar, high[i - 1], high[i - 2])
            if high[i] > new_sar:
                direction[i] = 1.0
                new_sar = ep
                ep = high[i]
                af = step
                reversed_ = True
            else:
                direction[i] = -1.0
                if low[i] < ep:
                    ep = low[i]
                    af = min(af + step, max_step)
        sar[i] = new_sar
        if reversed_:
            # EP redéfini au point extrême courant, af remis à step (déjà fait ci-dessus)
            pass
    return pd.Series(direction, index=df.index, name="psar_dir")


def compute_aroon(df: pd.DataFrame, *, period: int = 14) -> AroonResult:
    high, low = df["high"], df["low"]
    # argmax/argmin sur une fenêtre de `period+1` barres : le plus récent est à l'index `period`.
    aroon_up = high.rolling(period + 1).apply(
        lambda x: (np.argmax(x) / period) * 100, raw=True
    )
    aroon_down = low.rolling(period + 1).apply(
        lambda x: (np.argmin(x) / period) * 100, raw=True
    )
    return AroonResult(up=aroon_up, down=aroon_down, oscillator=aroon_up - aroon_down)


def compute_adx(df: pd.DataFrame, *, period: int = 14) -> ADXResult:
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift()
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    atr = tr.ewm(alpha=1.0 / period, adjust=False).mean()

    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
    minus_dm = down_move.where((down_move > up_move) & (down_move > 0), 0.0)

    plus_di = 100.0 * plus_dm.ewm(alpha=1.0 / period, adjust=False).mean() / atr.replace(0, np.nan)
    minus_di = 100.0 * minus_dm.ewm(alpha=1.0 / period, adjust=False).mean() / atr.replace(0, np.nan)
    dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx = dx.ewm(alpha=1.0 / period, adjust=False).mean()
    return ADXResult(adx=adx, plus_di=plus_di, minus_di=minus_di)


def compute_bollinger(
    df: pd.DataFrame,
    *,
    period: int = 20,
    std_dev: float = 2.0,
) -> BollingerResult:
    close = df["close"]
    middle = close.rolling(period).mean()
    sigma = close.rolling(period).std()
    upper = middle + std_dev * sigma
    lower = middle - std_dev * sigma
    pct_b = (close - lower) / (upper - lower).replace(0, np.nan)
    bw = (upper - lower) / middle.replace(0, np.nan)
    return BollingerResult(upper=upper, middle=middle, lower=lower, pct_b=pct_b, bandwidth=bw)


# ---------------------------------------------------------------------------
# Score composite trend
# ---------------------------------------------------------------------------


def _bollinger_signal(bb: BollingerResult) -> float:
    pct_b = bb.pct_b.iloc[-1] if len(bb.pct_b) else np.nan
    if pd.isna(pct_b):
        return 0.0
    if pct_b > 1.0:
        return -0.7
    if pct_b < 0.0:
        return 0.7
    return float(np.clip((0.5 - pct_b) * 2, -0.5, 0.5))


@dataclass
class TrendScore:
    score: float
    adx_strength: float
    components: dict

    @classmethod
    def compute(cls, df: pd.DataFrame, weights: "TrendWeights") -> "TrendScore":
        supertrend = compute_supertrend(df)
        macd = compute_macd(df)
        psar = compute_parabolic_sar(df)
        aroon = compute_aroon(df)
        adx = compute_adx(df)
        bollinger = compute_bollinger(df)

        adx_val = float(adx.adx.iloc[-1]) if not pd.isna(adx.adx.iloc[-1]) else 0.0
        # Pré-filtre : marché sans tendance → score trend forcé à 0.
        if adx_val < 20:
            return cls(
                score=0.0, adx_strength=adx_val,
                components={"filtered": "ADX < 20, marché sans tendance",
                            "adx": adx_val},
            )

        def _safe(v: float, default: float = 0.0) -> float:
            return float(v) if (v is not None and not pd.isna(v)) else default

        signals = {
            "supertrend": _safe(supertrend.iloc[-1]),
            "macd":       1.0 if _safe(macd.histogram.iloc[-1]) > 0 else -1.0,
            "psar":       _safe(psar.iloc[-1]),
            "aroon":      float(np.clip(_safe(aroon.oscillator.iloc[-1]) / 100.0, -1, 1)),
            "adx_dir":    1.0 if _safe(adx.plus_di.iloc[-1]) > _safe(adx.minus_di.iloc[-1]) else -1.0,
            "bollinger":  _bollinger_signal(bollinger),
        }
        score = sum(signals[k] * float(getattr(weights, k)) for k in signals)
        return cls(
            score=float(np.clip(score, -1.0, 1.0)),
            adx_strength=adx_val,
            components=signals,
        )


__all__ = [
    "MACDResult", "AroonResult", "ADXResult", "BollingerResult",
    "compute_supertrend", "compute_macd", "compute_parabolic_sar",
    "compute_aroon", "compute_adx", "compute_bollinger",
    "TrendScore",
]
