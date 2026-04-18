"""
src.indicators.ichimoku — pilier central, obligatoire.

Source : TRADING_BOT_ARCHITECTURE.md §5.1.

Règle d'invalidation : si `in_kumo` (prix dans le nuage), le score vaut 0 et
le risk gate rejette toute proposition. Le marché est considéré indécis.

API publique :
- `compute_ichimoku(df, fast=9, mid=26, slow=52) -> IchimokuResult`
- `ichimoku_payload(df, result=None) -> IchimokuPayload`   pour les contrats Pydantic
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass
class IchimokuResult:
    tenkan: pd.Series        # (H9+L9)/2
    kijun: pd.Series         # (H26+L26)/2
    senkou_a: pd.Series      # (tenkan+kijun)/2 décalé +mid
    senkou_b: pd.Series      # (H52+L52)/2 décalé +mid
    chikou: pd.Series        # close décalé -mid
    kumo_bullish: pd.Series  # True si senkou_a > senkou_b (comparé en valeur non-shiftée)
    score: float             # signé [-1, +1]
    in_kumo: bool            # prix dernier ∈ [kumo_bot, kumo_top] → score=0

    @property
    def components(self) -> dict:
        return {
            "tenkan_last":  float(self.tenkan.iloc[-1])   if len(self.tenkan)  else float("nan"),
            "kijun_last":   float(self.kijun.iloc[-1])    if len(self.kijun)   else float("nan"),
            "senkou_a_last": float(self.senkou_a.iloc[-1]) if len(self.senkou_a) else float("nan"),
            "senkou_b_last": float(self.senkou_b.iloc[-1]) if len(self.senkou_b) else float("nan"),
            "in_kumo": self.in_kumo,
            "score": self.score,
        }


def compute_ichimoku(
    df: pd.DataFrame,
    *,
    fast: int = 9,
    mid: int = 26,
    slow: int = 52,
) -> IchimokuResult:
    """Calcule Ichimoku Kinko Hyo. `df` doit contenir les colonnes high/low/close."""
    high, low, close = df["high"], df["low"], df["close"]

    tenkan = (high.rolling(fast).max() + low.rolling(fast).min()) / 2
    kijun  = (high.rolling(mid).max()  + low.rolling(mid).min())  / 2
    senkou_a = ((tenkan + kijun) / 2).shift(mid)
    senkou_b = ((high.rolling(slow).max() + low.rolling(slow).min()) / 2).shift(mid)
    chikou  = close.shift(-mid)

    score, in_kumo = _ichimoku_score(
        close=close, tenkan=tenkan, kijun=kijun,
        span_a=senkou_a, span_b=senkou_b, chikou=chikou, mid=mid,
    )
    return IchimokuResult(
        tenkan=tenkan, kijun=kijun,
        senkou_a=senkou_a, senkou_b=senkou_b,
        chikou=chikou, kumo_bullish=(senkou_a > senkou_b),
        score=score, in_kumo=in_kumo,
    )


def _ichimoku_score(
    *,
    close: pd.Series,
    tenkan: pd.Series,
    kijun: pd.Series,
    span_a: pd.Series,
    span_b: pd.Series,
    chikou: pd.Series,
    mid: int,
) -> tuple[float, bool]:
    """Score pondéré (§5.1) sur 5 conditions binaires ; retourne (score, in_kumo)."""
    if len(close) < 2 or pd.isna(span_a.iloc[-1]) or pd.isna(span_b.iloc[-1]):
        return 0.0, False

    c = float(close.iloc[-1])
    a = float(span_a.iloc[-1])
    b = float(span_b.iloc[-1])
    k_top = max(a, b)
    k_bot = min(a, b)
    in_kumo = k_bot <= c <= k_top
    if in_kumo:
        return 0.0, True

    tenkan_last = tenkan.iloc[-1]
    kijun_last = kijun.iloc[-1]

    # Chikou : compare close(t-mid) avec close(t-1-mid) pour savoir si le chikou
    # porté par close décalé est au-dessus du prix d'il y a `mid` barres.
    chikou_bullish = False
    if len(close) > mid + 1:
        close_at_chikou_ref = close.iloc[-mid - 1]
        if not pd.isna(close_at_chikou_ref):
            chikou_bullish = float(close.iloc[-1]) > float(close_at_chikou_ref)

    signals: list[tuple[int, float]] = [
        ( 1 if c > k_top else -1, 0.35),
        ( 1 if not pd.isna(tenkan_last) and not pd.isna(kijun_last) and tenkan_last > kijun_last else -1, 0.20),
        ( 1 if a > b else -1, 0.20),
        ( 1 if chikou_bullish else -1, 0.15),
        ( 1 if not pd.isna(kijun_last) and c > kijun_last else -1, 0.10),
    ]
    return float(np.clip(sum(s * w for s, w in signals), -1.0, 1.0)), False


# ---------------------------------------------------------------------------
# Adaptateur vers le contrat IchimokuPayload (§8.8.1)
# ---------------------------------------------------------------------------


def ichimoku_payload(df: pd.DataFrame, result: IchimokuResult | None = None):
    """Construit un `IchimokuPayload` à partir d'un IchimokuResult (ou le calcule)."""
    from src.contracts.skills import IchimokuPayload      # import local pour éviter cycle

    r = result or compute_ichimoku(df)
    if len(df) < 2 or pd.isna(r.senkou_a.iloc[-1]) or pd.isna(r.senkou_b.iloc[-1]):
        return IchimokuPayload(
            price_above_kumo=False,
            tenkan_above_kijun=False,
            chikou_above_price_26=False,
            kumo_thickness_pct=0.0,
            aligned_long=False,
            aligned_short=False,
            distance_to_kumo_pct=0.0,
        )

    c = float(df["close"].iloc[-1])
    a = float(r.senkou_a.iloc[-1])
    b = float(r.senkou_b.iloc[-1])
    k_top, k_bot = max(a, b), min(a, b)
    price_above = c > k_top
    price_below = c < k_bot
    tenkan_above_kijun = bool(r.tenkan.iloc[-1] > r.kijun.iloc[-1])
    kumo_bullish = a > b

    # Chikou vs prix d'il y a 26 périodes
    mid = 26
    chikou_above = False
    if len(df) > mid + 1:
        chikou_above = c > float(df["close"].iloc[-mid - 1])

    aligned_long = price_above and tenkan_above_kijun and kumo_bullish and chikou_above
    aligned_short = price_below and (not tenkan_above_kijun) and (not kumo_bullish) and (not chikou_above)

    mid_kumo = (a + b) / 2 or 1.0
    kumo_thickness_pct = max(0.0, abs(a - b) / abs(mid_kumo) if mid_kumo else 0.0)
    # Distance signée : positif = prix au-dessus du bord supérieur du kumo.
    if price_above:
        distance_to_kumo_pct = (c - k_top) / c
    elif price_below:
        distance_to_kumo_pct = (c - k_bot) / c
    else:
        distance_to_kumo_pct = 0.0

    return IchimokuPayload(
        price_above_kumo=price_above,
        tenkan_above_kijun=tenkan_above_kijun,
        chikou_above_price_26=chikou_above,
        kumo_thickness_pct=kumo_thickness_pct,
        aligned_long=aligned_long,
        aligned_short=aligned_short,
        distance_to_kumo_pct=distance_to_kumo_pct,
    )


__all__ = ["IchimokuResult", "compute_ichimoku", "ichimoku_payload"]
