"""
src.strategies.ichimoku_trend_following — §6.1 TRADING_BOT_ARCHITECTURE.md.

Stratégie principale, suiveur de tendance Ichimoku.

Entry (LONG) — conditions obligatoires :
  - ichimoku.aligned_long (price>Kumo, Tenkan>Kijun, Chikou>price[-27], Senkou_A>Senkou_B)
  - Supertrend bullish (score > 0)
  - ADX(14) >= config.entry.adx_min (défaut 25)
  - composite_score >= config.min_composite_score (0.82)

Exit :
  - tp_rule = "kijun", atr_stop_mult = 1.5, tp_r_multiples = [1.5, 3.0]
  - trailing = "kijun"

Rejette dès qu'une condition obligatoire tombe.
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

NAME = "ichimoku_trend_following"


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
    # Gate 1 : composite score
    if not passes_composite_gate(signal, config):
        return None

    # Gate 2 : direction claire
    side = infer_side(signal)
    if side is None:
        return None

    # Gate 3 : Ichimoku (obligatoire pour trend-follower)
    if not passes_ichimoku_gate(signal, config, side):
        return None

    # Gate 4 : Supertrend aligné
    st = scalar(signal.trend, "supertrend")
    if side == "long" and st <= 0:
        return None
    if side == "short" and st >= 0:
        return None

    # Gate 5 : ADX >= seuil (trend établi)
    adx_min = float(config.entry.get("adx_min", 25))
    adx = scalar(signal.trend, "adx_14")
    # ADX est normalisé [0, 1] dans IndicatorScore : on compare au seuil rescalé
    # (seuil 25 sur échelle 0-100 → 0.25 sur échelle 0-1)
    if adx < (adx_min / 100.0):
        return None

    # Prices
    entry = last_close(snapshot)
    stop = compute_stop(
        side=side,
        entry=entry,
        atr=snapshot.atr_14,
        atr_mult=config.exit.atr_stop_mult,
    )
    tp_list = tp_from_config(
        side=side, entry=entry, stop=stop,
        exit_cfg=config.exit, ichimoku=signal.ichimoku,
        kijun=kijun, tenkan=tenkan, hvn_levels=hvn_levels,
    )

    catalysts = ["ichimoku_aligned", "supertrend_aligned", "adx_trend"]
    return assemble_proposal(
        strategy_id=NAME, signal=signal, snapshot=snapshot, config=config,
        side=side, entry=entry, stop=stop, tp_list=tp_list,
        catalysts=catalysts,
    )


__all__ = ["NAME", "build_proposal"]
