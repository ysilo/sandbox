"""
src.risk — couche de contrôle déterministe (§11).

Exports principaux :
- `KillSwitch`                         — sentinelle on-disk `data/KILL`.
- `CircuitBreaker`, `CircuitState`     — désactivation par stratégie (§11.3).
- `ichimoku_gate.check`                — règle d'or §2.5 / check C6.
- `RiskGate`                           — orchestrateur des 10 checks C1→C10.
- `GateConfig`, `GateContext`          — seuils + état d'évaluation.

Invariant §8.8.1 : `RiskDecision.checks` contient toujours 10 entrées ordonnées.
Le module ne charge AUCUN YAML — tout vient du `GateConfig` construit en amont.
"""
from __future__ import annotations

from . import ichimoku_gate
from .circuit_breaker import CircuitBreaker, CircuitBreakerResult, CircuitState
from .gate import (
    DataQualityState,
    GateConfig,
    GateContext,
    MacroState,
    PortfolioState,
    PositionSnapshot,
    RiskGate,
    TokenBudgetState,
    empty_context,
)
from .ichimoku_gate import IchimokuGateResult
from .kill_switch import KillSwitch

__all__ = [
    "KillSwitch",
    "CircuitBreaker",
    "CircuitBreakerResult",
    "CircuitState",
    "ichimoku_gate",
    "IchimokuGateResult",
    "RiskGate",
    "GateConfig",
    "GateContext",
    "PortfolioState",
    "PositionSnapshot",
    "MacroState",
    "DataQualityState",
    "TokenBudgetState",
    "empty_context",
]
