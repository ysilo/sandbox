"""
src.contracts — tous les contrats Pydantic / dataclass de l'application.

Arborescence :
- `skills.py` — frontière skill (Cowork) : Candidate, SelectionOutput, SignalOutput,
  IchimokuPayload, NewsItem/Pulse, MarketSnapshot, StrategyConfig, RiskCheckResult,
  RiskDecision, BacktestReport. Cf. TRADING_BOT_ARCHITECTURE.md §8.8.1.
- `strategy.py` — TradeProposal (sortie de build_proposal §8.9). Dataclass.
- `regime.py` — RegimeState (sortie du HMM §12.2).
- `cycle.py` — CycleResult (sortie de l'orchestrateur §8.7.1).

Les imports cross-module doivent passer par ces modules — JAMAIS redéfinir
un type en local.
"""
from __future__ import annotations

from .cycle import CycleKind, CycleResult, CycleStatus
from .regime import MacroState, RegimeState, VolatilityState
from .skills import (
    CHECK_IDS,
    BacktestReport,
    Candidate,
    IchimokuPayload,
    IndicatorScore,
    MarketSnapshot,
    NewsItem,
    NewsPulse,
    RiskCheckResult,
    RiskDecision,
    SelectionOutput,
    SignalOutput,
    StrategyChoice,
    StrategyConfig,
    StrategyExitConfig,
    _pad_checks,
    _utc_now,
)
from .strategy import Side, TradeProposal

__all__ = [
    # skills.py
    "Candidate",
    "StrategyChoice",
    "SelectionOutput",
    "IchimokuPayload",
    "IndicatorScore",
    "SignalOutput",
    "NewsItem",
    "NewsPulse",
    "MarketSnapshot",
    "StrategyExitConfig",
    "StrategyConfig",
    "CHECK_IDS",
    "RiskCheckResult",
    "RiskDecision",
    "BacktestReport",
    # strategy.py
    "TradeProposal",
    "Side",
    # regime.py
    "RegimeState",
    "MacroState",
    "VolatilityState",
    # cycle.py
    "CycleResult",
    "CycleStatus",
    "CycleKind",
    # helpers
    "_pad_checks",
    "_utc_now",
]
