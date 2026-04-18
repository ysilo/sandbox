"""
src.strategies.news_driven_momentum — §6.7 TRADING_BOT_ARCHITECTURE.md.

Trade sur newsflow avec impact_score fort et sentiment marqué. Contrairement
à event_driven_macro qui se déclenche sur macro calendaires, ici on capte
les dépêches ad-hoc (Reuters, TradingEconomics, Binance announces, Finnhub).

Entry :
  - news.aggregate_impact >= config.entry.news_impact_min (0.70)
  - |news.aggregate_sentiment| >= config.entry.news_sentiment_abs (0.50)
  - Ichimoku aligné (golden rule §2.5) dans le sens du sentiment
  - composite_score >= 0.79
  - Volume surge (OBV + CMF dans sens trade)

Exit : tp_rule = "r_multiple" [1.5, 3.0], atr_stop_mult 1.5, trailing ATR.
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
    tp_from_config,
)

NAME = "news_driven_momentum"


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

    # News obligatoires + seuils
    if news is None or not news.items:
        return None
    impact_min = float(config.entry.get("news_impact_min", 0.70))
    sentiment_abs = float(config.entry.get("news_sentiment_abs", 0.50))

    if float(news.aggregate_impact) < impact_min:
        return None
    sentiment = float(news.aggregate_sentiment)
    if abs(sentiment) < sentiment_abs:
        return None

    side = "long" if sentiment > 0 else "short"

    if not passes_ichimoku_gate(signal, config, side):
        return None

    # OBV aligné
    obv = scalar(signal.volume, "obv", default=0.0)
    if side == "long" and obv <= 0:
        return None
    if side == "short" and obv >= 0:
        return None

    # CMF aligné (flux acheteur/vendeur)
    cmf = scalar(signal.volume, "cmf", default=0.0)
    if side == "long" and cmf <= 0:
        return None
    if side == "short" and cmf >= 0:
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

    catalysts = [
        f"news_impact={news.aggregate_impact:.2f}",
        f"news_sentiment={sentiment:+.2f}",
        "obv_surge",
        "cmf_flux",
    ]
    # Attacher la source principale si présente
    if news.top is not None:
        catalysts.append(f"source:{news.top.source}")

    return assemble_proposal(
        strategy_id=NAME, signal=signal, snapshot=snapshot, config=config,
        side=side, entry=entry, stop=stop, tp_list=tp_list,
        catalysts=catalysts,
    )


__all__ = ["NAME", "build_proposal"]
