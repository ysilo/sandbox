"""
src.strategies.event_driven_macro — §6.6 TRADING_BOT_ARCHITECTURE.md.

Trade post-événement macro (NFP, FOMC, CPI, ECB). Règle absolue : jamais de
position AVANT l'événement — on attend la confirmation de la direction.

Entry (invariants) :
  - Un event macro récent est publié via `news.top` avec tag matching
    `config.entry.event_tags` (CSV)
  - Post-event window : `news.top.published + post_event_minutes` ≥ now
    (V1 : le signal est déjà horodaté ; on fait confiance à l'orchestrateur
    pour filtrer en amont — on vérifie juste la présence d'un catalyseur)
  - Ichimoku aligné dans le sens du mouvement post-event (requires_ichimoku)
  - composite_score >= 0.77
  - Volume confirme (volume.obv > 0 côté long, < 0 côté short)
  - MACD post-event impulse (score aligné)

Exit : tp_rule = "r_multiple" [1.5, 3.0], atr_stop_mult 1.8, trailing ATR.
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

NAME = "event_driven_macro"


def _event_tag_matches(news: Optional[NewsPulse], tags_csv: str) -> bool:
    """Vérifie qu'au moins un entity/source/title du news.top matche un tag.

    Fail-closed §2.6 : si `tags_csv` est vide ou non-configuré, on **rejette**
    au lieu de degrader la strat en "accepte toute news". L'opérateur doit
    explicitement configurer les tags admissibles dans strategies.yaml
    (ex: "fomc,ecb_meeting,cpi_us,nfp") — sinon event_driven_macro est
    indistinguable de news_driven_momentum et perd son invariant §6.6.
    """
    if news is None or news.top is None:
        return False
    tags = {t.strip().lower() for t in (tags_csv or "").split(",") if t.strip()}
    if not tags:
        return False  # fail-closed — pas de filtre = pas de trade
    blob_parts = [news.top.source, news.top.title, *news.top.entities]
    blob = " ".join(p or "" for p in blob_parts).lower()
    return any(tag in blob for tag in tags)


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

    # Catalyseur event obligatoire
    tags_csv = str(config.entry.get("event_tags", ""))
    if not _event_tag_matches(news, tags_csv):
        return None
    # news.top est garanti non-None ici
    top = news.top  # type: ignore[union-attr]

    side = infer_side(signal)
    if side is None:
        return None
    if not passes_ichimoku_gate(signal, config, side):
        return None

    # Sentiment news aligné avec la direction
    if side == "long" and top.sentiment <= 0:
        return None
    if side == "short" and top.sentiment >= 0:
        return None

    # OBV / volume surge confirme
    obv = scalar(signal.volume, "obv", default=0.0)
    if side == "long" and obv <= 0:
        return None
    if side == "short" and obv >= 0:
        return None

    # MACD post-event impulse
    macd = scalar(signal.trend, "macd", default=0.0)
    if side == "long" and macd <= 0:
        return None
    if side == "short" and macd >= 0:
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

    catalysts = [f"event:{top.source}", "obv_surge", "macd_impulse"]
    return assemble_proposal(
        strategy_id=NAME, signal=signal, snapshot=snapshot, config=config,
        side=side, entry=entry, stop=stop, tp_list=tp_list,
        catalysts=catalysts,
    )


__all__ = ["NAME", "build_proposal"]
