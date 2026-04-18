"""
src.strategies — 7 stratégies `build_proposal` §6 TRADING_BOT_ARCHITECTURE.md.

Chaque module expose :
    NAME: str
    build_proposal(*, signal, snapshot, config, news=None,
                   kijun=None, tenkan=None, hvn_levels=None) -> TradeProposal | None

Le registry `STRATEGY_REGISTRY` est consommé par l'orchestrateur (§8.7) pour
dispatcher le bon module en fonction de `StrategyChoice.strategy_id`.

Design :
- 100 % déterministe, 0 token (pas une frontière skill LLM — cf. §8.9).
- Chaque stratégie valide ses propres gates puis délègue l'assemblage à
  `src.strategies._common.assemble_proposal` qui vérifie R/R et construit
  la dataclass `TradeProposal`.
- Les waivers Ichimoku (§2.5.1) sont portés par `StrategyConfig.requires_ichimoku_alignment`
  et honorés par `_common.passes_ichimoku_gate`.
"""
from __future__ import annotations

import inspect
from typing import Callable, Optional

from src.contracts.skills import MarketSnapshot, NewsPulse, SignalOutput, StrategyConfig
from src.contracts.strategy import TradeProposal

from . import (
    breakout_momentum,
    divergence_hunter,
    event_driven_macro,
    ichimoku_trend_following,
    mean_reversion,
    news_driven_momentum,
    volume_profile_scalp,
)

# Signature canonique de `build_proposal`
BuildProposalFn = Callable[..., Optional[TradeProposal]]


STRATEGY_REGISTRY: dict[str, BuildProposalFn] = {
    ichimoku_trend_following.NAME: ichimoku_trend_following.build_proposal,
    breakout_momentum.NAME:        breakout_momentum.build_proposal,
    mean_reversion.NAME:           mean_reversion.build_proposal,
    divergence_hunter.NAME:        divergence_hunter.build_proposal,
    volume_profile_scalp.NAME:     volume_profile_scalp.build_proposal,
    event_driven_macro.NAME:       event_driven_macro.build_proposal,
    news_driven_momentum.NAME:     news_driven_momentum.build_proposal,
}

# Cache des signatures — calculé une fois à l'import pour éviter l'overhead
# `inspect.signature()` sur le hot-path dispatcher §8.7.
_STRATEGY_PARAMS: dict[str, frozenset[str]] = {
    name: frozenset(inspect.signature(fn).parameters.keys())
    for name, fn in STRATEGY_REGISTRY.items()
}


def build_proposal_for(
    strategy_id: str,
    *,
    signal: SignalOutput,
    snapshot: MarketSnapshot,
    config: StrategyConfig,
    news: Optional[NewsPulse] = None,
    **extras: object,
) -> Optional[TradeProposal]:
    """Dispatche vers le module stratégie identifié par `strategy_id`.

    `extras` transporte les paramètres spécifiques à certaines stratégies
    (`kijun=`, `tenkan=`, `hvn_levels=`, `volume_ratio=`, …). Chaque module
    accepte uniquement ses propres kwargs pertinents ; les autres sont
    ignorés — on filtre via `_STRATEGY_PARAMS` (cached à l'import) pour ne
    pas propager un TypeError.

    Raise KeyError si `strategy_id` inconnu (l'orchestrateur doit vérifier
    la config au startup contre `STRATEGY_REGISTRY.keys()`).
    """
    fn = STRATEGY_REGISTRY[strategy_id]
    sig_params = _STRATEGY_PARAMS[strategy_id]
    forwarded = {k: v for k, v in extras.items() if k in sig_params}
    return fn(
        signal=signal, snapshot=snapshot, config=config, news=news,
        **forwarded,
    )


__all__ = [
    "STRATEGY_REGISTRY",
    "build_proposal_for",
    # modules
    "ichimoku_trend_following",
    "breakout_momentum",
    "mean_reversion",
    "divergence_hunter",
    "volume_profile_scalp",
    "event_driven_macro",
    "news_driven_momentum",
]
