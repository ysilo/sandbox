"""
src.contracts.regime — RegimeState (sortie de `src/regime/hmm_detector.py`).

Source : TRADING_BOT_ARCHITECTURE.md §12.2.

RegimeState n'est PAS dans skills.py parce qu'il n'est pas un contrat inter-skill
(pas de frontière LLM), mais un type interne partagé entre le détecteur HMM,
`strategy-selector`, les stratégies et le dashboard. Pydantic BaseModel pour
sérialisation JSON cohérente avec `data/cache/last_regime.json` (§8.7.1 fallback).
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, confloat


MacroState = Literal["risk_on", "transition", "risk_off"]
VolatilityState = Literal["low", "mid", "high", "extreme"]


class RegimeState(BaseModel):
    """Sortie du RegimeDetector pour un cycle donné.

    Cache disque : `data/cache/last_regime.json` — rechargé en mode dégradé
    si `regime_detector` KO (§8.7.1).
    """

    macro: MacroState
    volatility: VolatilityState
    probabilities: dict[MacroState, confloat(ge=0.0, le=1.0)] = Field(
        ..., description="distribution sur les 3 états macro, somme ≈ 1.0"
    )
    hmm_state: int                                      # index brut du HMM [0, n_components-1]
    date: str                                           # YYYY-MM-DD (UTC)


__all__ = ["RegimeState", "MacroState", "VolatilityState"]
