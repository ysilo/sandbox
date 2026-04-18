"""
src.strategies.volume_profile_scalp — §6.5 TRADING_BOT_ARCHITECTURE.md.

Scalp sur rebonds de niveaux Volume Profile (POC/VAH/VAL/HVN) avec VWAP.
Waiver Ichimoku §2.5.1 : VP prend le pas, mais on rejette l'alignement opposé.

Entry :
  - "volume_profile" IndicatorScore signale rebond (score dans sens trade)
  - VWAP proximity : signal.trend "vwap" dans sens trade
  - RSI neutre (|rsi| < 0.3) — pas de momentum extrême qui casserait la range
  - Ichimoku waiver (pas d'alignment inverse strict)
  - composite_score >= 0.70

Exit : tp_rule = "hvn" [1.5, 2.0], atr_stop_mult 1.0 (scalp serré).
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

NAME = "volume_profile_scalp"


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

    # Volume Profile direction obligatoire
    vp = score_by_name(signal.volume, "volume_profile")
    if vp is None or abs(vp.score) < 0.2:
        return None
    side = "long" if vp.score > 0 else "short"

    if not passes_ichimoku_gate(signal, config, side):
        return None

    # VWAP aligné
    vwap = scalar(signal.volume, "vwap", default=0.0)
    if side == "long" and vwap <= 0:
        return None
    if side == "short" and vwap >= 0:
        return None

    # RSI neutre : pas de momentum extrême
    rsi = scalar(signal.momentum, "rsi_14", default=0.0)
    if abs(rsi) > 0.3:
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

    catalysts = ["volume_profile_level", "vwap_aligned", "rsi_neutral"]
    return assemble_proposal(
        strategy_id=NAME, signal=signal, snapshot=snapshot, config=config,
        side=side, entry=entry, stop=stop, tp_list=tp_list,
        catalysts=catalysts,
    )


__all__ = ["NAME", "build_proposal"]
