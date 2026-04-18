"""
src.strategies.divergence_hunter — §6.4 TRADING_BOT_ARCHITECTURE.md.

Detecte les divergences RSI/MACD vs prix. Waiver Ichimoku (§2.5.1) : on
prend la direction de l'oscillateur, pas du Kumo.

Entry :
  - "divergence_rsi" indicator score signe la direction
    (score > 0 → bullish divergence → long)
  - CMF contrarian (dans sens du trade)
  - RSI zone extrême (< -0.2 pour long, > 0.2 pour short) — momentum exhaustion
  - Ichimoku : pas d'alignement opposé strict (waiver)
  - composite_score >= 0.70

Exit : tp_rule = "r_multiple" [2.0, 3.0], atr_stop_mult 1.8, trailing Chikou.

Pour V1, le signal de divergence est supposé pré-calculé par
`signal_crossing` et publié via un IndicatorScore nommé "divergence_rsi".
Le score scalaire ∈ [-1, 1] représente la force + sens de la divergence.
"""
from __future__ import annotations

from typing import Optional

from src.contracts.skills import MarketSnapshot, NewsPulse, SignalOutput, StrategyConfig
from src.contracts.strategy import TradeProposal

from ._common import (
    assemble_proposal,
    compute_stop,
    last_close,
    passes_composite_gate,
    passes_ichimoku_gate,
    scalar,
    score_by_name,
    tp_from_config,
)

NAME = "divergence_hunter"


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

    # Divergence direction — signal obligatoire
    div = score_by_name(signal.momentum, "divergence_rsi")
    if div is None or abs(div.score) < 0.2:
        return None
    side = "long" if div.score > 0 else "short"

    if not passes_ichimoku_gate(signal, config, side):
        return None

    # MACD divergence optionnelle : si présente, doit confirmer
    macd_div = score_by_name(signal.momentum, "divergence_macd")
    if macd_div is not None:
        if side == "long" and macd_div.score < 0:
            return None
        if side == "short" and macd_div.score > 0:
            return None

    # CMF contrarian (flux opposé au mouvement de prix)
    cmf = scalar(signal.volume, "cmf", default=0.0)
    if side == "long" and cmf <= 0:
        return None
    if side == "short" and cmf >= 0:
        return None

    # RSI en zone extrême (momentum exhaustion)
    rsi = scalar(signal.momentum, "rsi_14", default=0.0)
    if side == "long" and rsi > -0.2:
        return None
    if side == "short" and rsi < 0.2:
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

    catalysts = ["divergence_rsi", "cmf_contrarian", "rsi_extreme"]
    if macd_div is not None:
        catalysts.append("divergence_macd_confirm")
    return assemble_proposal(
        strategy_id=NAME, signal=signal, snapshot=snapshot, config=config,
        side=side, entry=entry, stop=stop, tp_list=tp_list,
        catalysts=catalysts,
    )


__all__ = ["NAME", "build_proposal"]
