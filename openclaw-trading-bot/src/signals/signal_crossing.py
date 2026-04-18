"""
src.signals.signal_crossing — composite scorer → `SignalOutput`.

Source : TRADING_BOT_ARCHITECTURE.md §5.5 et §8.8.1 (contrat SignalOutput).

Produit un **diagnostic scalaire** à partir d'un OHLCV dataframe. Ne propose
jamais de trade : `SignalOutput.is_proposal = False` est figé par le type system.
La transformation `SignalOutput → TradeProposal` est logée dans chaque stratégie
(`src/strategies/<id>.build_proposal`, §8.9).

Pipeline :
1. Ichimoku (pilier) → score signé ∈ [-1, +1] ; si `in_kumo` → 0.
2. Trend (Supertrend/MACD/PSAR/Aroon/ADX/BB) → pré-filtre ADX < 20.
3. Momentum (RSI/Stoch/TRIX/CCI/Mom).
4. Volume (OBV/VWAP/CMF/VP) — neutralisé si volume absent (forex).
5. Composite = w_i × S_i + w_t × S_t + w_m × S_m + w_v × S_v
6. Confidence = mélange de |composite| et de l'adéquation avec le régime.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal, Optional

import numpy as np
import pandas as pd

from src.contracts.skills import (
    IndicatorScore,
    SignalOutput,
)
from src.indicators.ichimoku import (
    IchimokuResult,
    compute_ichimoku,
    ichimoku_payload,
)
from src.indicators.momentum import MomentumScore
from src.indicators.trend import TrendScore
from src.indicators.volume import VolumeScore
from src.indicators.weights import (
    CompositeWeights,
    DEFAULT_COMPOSITE,
    DEFAULT_MOMENTUM,
    DEFAULT_TREND,
    DEFAULT_VOLUME,
    MomentumWeights,
    TrendWeights,
    VolumeWeights,
)


RegimeContext = Literal["risk_on", "transition", "risk_off"]


@dataclass
class SignalComputation:
    """Résultat enrichi — utile côté stratégie pour construire TradeProposal."""

    signal_output: SignalOutput
    ichimoku_result: IchimokuResult
    trend: TrendScore
    momentum: MomentumScore
    volume: VolumeScore
    composite: float
    confidence: float


class SignalCrossing:
    """Calcule le `SignalOutput` à partir d'un OHLCV dataframe et du régime.

    Usage :
        sc = SignalCrossing()
        out = sc.score(df, asset="RUI.PA", regime_context="risk_on")
    """

    def __init__(
        self,
        *,
        composite_weights: CompositeWeights = DEFAULT_COMPOSITE,
        trend_weights: TrendWeights = DEFAULT_TREND,
        momentum_weights: MomentumWeights = DEFAULT_MOMENTUM,
        volume_weights: VolumeWeights = DEFAULT_VOLUME,
    ) -> None:
        self.composite = composite_weights
        self.trend_w = trend_weights
        self.momentum_w = momentum_weights
        self.volume_w = volume_weights

    # ------------------------------------------------------------------
    # API
    # ------------------------------------------------------------------

    def score(
        self,
        df: pd.DataFrame,
        *,
        asset: str,
        regime_context: RegimeContext,
        timestamp: Optional[str] = None,
    ) -> SignalOutput:
        return self.compute(df, asset=asset, regime_context=regime_context,
                            timestamp=timestamp).signal_output

    def compute(
        self,
        df: pd.DataFrame,
        *,
        asset: str,
        regime_context: RegimeContext,
        timestamp: Optional[str] = None,
    ) -> SignalComputation:
        _assert_ohlcv(df)

        ichi = compute_ichimoku(df)
        trend = TrendScore.compute(df, self.trend_w)
        momentum = MomentumScore.compute(df, self.momentum_w)
        volume = VolumeScore.compute(df, self.volume_w)

        composite = float(np.clip(
            self.composite.ichimoku * ichi.score
            + self.composite.trend * trend.score
            + self.composite.momentum * momentum.score
            + self.composite.volume * volume.score,
            -1.0, 1.0,
        ))
        # Si Ichimoku neutre (in_kumo), on force le composite à 0 — §5.1.
        if ichi.in_kumo:
            composite = 0.0

        confidence = self._confidence(
            composite=composite, ichi=ichi, trend=trend,
            regime_context=regime_context,
        )

        payload = ichimoku_payload(df, result=ichi)

        signal_output = SignalOutput(
            asset=asset,
            timestamp=timestamp or _utc_now_z(),
            composite_score=composite,
            confidence=confidence,
            regime_context=regime_context,
            ichimoku=payload,
            trend=_pack_trend_indicators(trend),
            momentum=_pack_momentum_indicators(momentum),
            volume=_pack_volume_indicators(volume),
        )

        return SignalComputation(
            signal_output=signal_output,
            ichimoku_result=ichi,
            trend=trend,
            momentum=momentum,
            volume=volume,
            composite=composite,
            confidence=confidence,
        )

    # ------------------------------------------------------------------
    # Confidence
    # ------------------------------------------------------------------

    @staticmethod
    def _confidence(
        *,
        composite: float,
        ichi: IchimokuResult,
        trend: TrendScore,
        regime_context: RegimeContext,
    ) -> float:
        """Confidence ∈ [0, 1]. Approche : magnitude du signal modulée par la
        cohérence avec le régime et la force de la tendance (ADX).
        """
        if ichi.in_kumo or composite == 0.0:
            return 0.0

        base = min(abs(composite), 1.0)

        # Malus si régime contraire : un signal long en risk_off perd de la confiance.
        regime_factor = 1.0
        if regime_context == "risk_off" and composite > 0:
            regime_factor = 0.7
        elif regime_context == "risk_on" and composite < 0:
            regime_factor = 0.7
        elif regime_context == "transition":
            regime_factor = 0.85

        # Bonus léger si ADX > 25 (tendance en place). Le score trend est déjà à 0 si ADX < 20.
        adx_factor = 1.0
        if trend.adx_strength >= 25:
            adx_factor = 1.05
        if trend.adx_strength >= 40:
            adx_factor = 1.10

        return float(np.clip(base * regime_factor * adx_factor, 0.0, 1.0))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pack_trend_indicators(trend: TrendScore) -> list[IndicatorScore]:
    items: list[IndicatorScore] = []
    for name, val in trend.components.items():
        if name == "filtered":
            continue
        try:
            score = float(np.clip(val, -1.0, 1.0))
        except (TypeError, ValueError):
            continue
        items.append(IndicatorScore(
            name=name, score=score, confidence=min(1.0, trend.adx_strength / 40.0),
        ))
    return items


def _pack_momentum_indicators(mom: MomentumScore) -> list[IndicatorScore]:
    items: list[IndicatorScore] = []
    for name, val in mom.components.items():
        try:
            score = float(np.clip(val, -1.0, 1.0))
        except (TypeError, ValueError):
            continue
        # Pour le RSI, la confidence est modulée par l'éloignement du neutre 50.
        if name == "rsi":
            conf = min(1.0, abs(mom.rsi - 50.0) / 30.0)
        else:
            conf = min(1.0, abs(score))
        items.append(IndicatorScore(name=name, score=score, confidence=conf))
    return items


def _pack_volume_indicators(vol: VolumeScore) -> list[IndicatorScore]:
    items: list[IndicatorScore] = []
    for name, val in vol.components.items():
        if name == "filtered":
            continue
        try:
            score = float(np.clip(val, -1.0, 1.0))
        except (TypeError, ValueError):
            continue
        items.append(IndicatorScore(
            name=name, score=score, confidence=min(1.0, abs(score)),
        ))
    return items


def _assert_ohlcv(df: pd.DataFrame) -> None:
    required = {"open", "high", "low", "close", "volume"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"OHLCV DataFrame incomplet, colonnes manquantes : {missing}")
    if len(df) < 52:
        raise ValueError(f"Au moins 52 barres requises pour Ichimoku ({len(df)} reçues)")


def _utc_now_z() -> str:
    return datetime.now(tz=timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


__all__ = ["SignalCrossing", "SignalComputation", "RegimeContext"]
