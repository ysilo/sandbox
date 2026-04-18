"""
src.signals — diagnostic scalaire (`SignalOutput`, invariant `is_proposal=False`).

Les stratégies (`src/strategies/`) consomment le `SignalOutput` et produisent
les `TradeProposal` via leur propre `build_proposal`, déterministe et 0 token.
"""
from __future__ import annotations

from .signal_crossing import SignalComputation, SignalCrossing

__all__ = ["SignalCrossing", "SignalComputation"]
