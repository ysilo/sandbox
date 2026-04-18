"""
src.strategies.mean_reversion — §6.3 TRADING_BOT_ARCHITECTURE.md.

Stratégie **contrarian** : waiver Ichimoku (§2.5.1). On tolère tout sauf
l'alignement directement opposé à la thèse de rebound.

Entry (LONG — rebound d'oversold) :
  - RSI(14) en survente (score < -0.4 par convention, ou via oscillator_zone)
  - Stochastique cross up en zone basse (momentum.stochastic > 0 après cross)
  - CCI extrême (<-100, normalisé < -0.5)
  - ADX modéré (<30) — pas de tendance forte, sinon on s'oppose à un trend
  - Ichimoku waiver : aligned_short INTERDIT (anti-golden-rule)
  - composite_score >= 0.80 (min_composite_score)

Exit : tp_rule = "tenkan", atr_stop_mult = 1.2.
"""
from __future__ import annotations

from typing import Optional

from src.contracts.skills import MarketSnapshot, NewsPulse, SignalOutput, StrategyConfig
from src.contracts.strategy import Side, TradeProposal

from ._common import (
    assemble_proposal,
    compute_stop,
    last_close,
    passes_composite_gate,
    passes_ichimoku_gate,
    scalar,
    tp_from_config,
)

NAME = "mean_reversion"


def _infer_contrarian_side(signal: SignalOutput) -> Optional[Side]:
    """Direction à l'inverse du momentum : RSI très bas → long, RSI très haut → short."""
    rsi = scalar(signal.momentum, "rsi_14", default=0.0)
    # rsi score est dans [-1, 1] : -1 = survente profonde, +1 = surachat profond
    if rsi <= -0.4:
        return "long"
    if rsi >= 0.4:
        return "short"
    return None


def build_proposal(
    *,
    signal: SignalOutput,
    snapshot: MarketSnapshot,
    config: StrategyConfig,
    news: Optional[NewsPulse] = None,
    kijun: Optional[float] = None,
    tenkan: Optional[float] = None,
    hvn_levels: Optional[list[float]] = None,
) -> Optional[TradeProposal]:
    if not passes_composite_gate(signal, config):
        return None

    side = _infer_contrarian_side(signal)
    if side is None:
        return None
    if not passes_ichimoku_gate(signal, config, side):
        return None

    # CCI extrême dans le sens contrarian
    cci = scalar(signal.momentum, "cci", default=0.0)
    if side == "long" and cci > -0.5:
        return None
    if side == "short" and cci < 0.5:
        return None

    # Stochastique : cross-back en zone
    stoch = scalar(signal.momentum, "stochastic", default=0.0)
    if side == "long" and stoch <= 0:
        return None
    if side == "short" and stoch >= 0:
        return None

    # ADX filter (pas de tendance forte)
    adx = scalar(signal.trend, "adx_14", default=0.0)
    if adx > 0.30:  # 30 / 100
        return None

    entry = last_close(snapshot)
    stop = compute_stop(
        side=side, entry=entry,
        atr=snapshot.atr_14, atr_mult=config.exit.atr_stop_mult,
    )
    tp_list = tp_from_config(
        side=side, entry=entry, stop=stop,
        exit_cfg=config.exit, ichimoku=signal.ichimoku,
        kijun=kijun, tenkan=tenkan, hvn_levels=hvn_levels,
    )

    catalysts = ["rsi_extreme", "stochastic_cross", "cci_extreme", "low_adx"]
    return assemble_proposal(
        strategy_id=NAME, signal=signal, snapshot=snapshot, config=config,
        side=side, entry=entry, stop=stop, tp_list=tp_list,
        catalysts=catalysts,
    )


__all__ = ["NAME", "build_proposal"]
