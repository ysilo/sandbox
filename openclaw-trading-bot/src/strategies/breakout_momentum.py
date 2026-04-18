"""
src.strategies.breakout_momentum — §6.2 TRADING_BOT_ARCHITECTURE.md.

Entry (LONG) — conditions obligatoires :
  - ichimoku.aligned_long (alignement Kumo bullish)
  - Bollinger breakout (score > 0)
  - OBV surge (score > 0)
  - Aroon nascent (score > 0, i.e. aroon_up > 70)
  - MACD histogram positif et croissant
  - composite_score >= config.min_composite_score (0.80)

Exit : tp_rule = "r_multiple" [2.0, 4.0], atr_stop_mult 2.0, trailing ATR.
"""
from __future__ import annotations

from typing import Optional

from src.contracts.skills import MarketSnapshot, NewsPulse, SignalOutput, StrategyConfig
from src.contracts.strategy import TradeProposal

from ._common import (
    assemble_proposal,
    compute_stop,
    infer_side,
    last_close,
    passes_composite_gate,
    passes_ichimoku_gate,
    scalar,
    tp_from_config,
)

NAME = "breakout_momentum"


def build_proposal(
    *,
    signal: SignalOutput,
    snapshot: MarketSnapshot,
    config: StrategyConfig,
    news: Optional[NewsPulse] = None,
    kijun: Optional[float] = None,
    tenkan: Optional[float] = None,
    hvn_levels: Optional[list[float]] = None,
    volume_ratio: Optional[float] = None,
) -> Optional[TradeProposal]:
    if not passes_composite_gate(signal, config):
        return None
    side = infer_side(signal)
    if side is None:
        return None
    if not passes_ichimoku_gate(signal, config, side):
        return None

    # Bollinger breakout (score direction == side)
    bb = scalar(signal.trend, "bollinger")
    if side == "long" and bb <= 0:
        return None
    if side == "short" and bb >= 0:
        return None

    # OBV surge
    obv = scalar(signal.volume, "obv")
    if side == "long" and obv <= 0:
        return None
    if side == "short" and obv >= 0:
        return None

    # Aroon nascent (aroon_up > 70 → score positif)
    aroon = scalar(signal.trend, "aroon")
    if side == "long" and aroon <= 0:
        return None
    if side == "short" and aroon >= 0:
        return None

    # MACD histogram aligné
    macd = scalar(signal.trend, "macd")
    if side == "long" and macd <= 0:
        return None
    if side == "short" and macd >= 0:
        return None

    # Volume ratio minimum — passé par l'orchestrateur en kwarg (raw ratio,
    # pas un IndicatorScore car bornage [-1, 1] incompatible). None = gate
    # désactivé en V1 quand l'orchestrateur ne pré-calcule pas la statistique.
    if volume_ratio is not None:
        vol_min = float(config.entry.get("volume_ratio_min", 1.0))
        if float(volume_ratio) < vol_min:
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

    catalysts = ["bollinger_breakout", "obv_surge", "aroon_trend", "macd_histogram"]
    return assemble_proposal(
        strategy_id=NAME, signal=signal, snapshot=snapshot, config=config,
        side=side, entry=entry, stop=stop, tp_list=tp_list,
        catalysts=catalysts,
    )


__all__ = ["NAME", "build_proposal"]
