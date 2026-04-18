"""
src.contracts.strategy — TradeProposal (sortie de `build_proposal`).

Source : TRADING_BOT_ARCHITECTURE.md §8.9.

C'est **le** point où un SignalOutput devient une proposition concrète
(entry, stop, tp, R/R, sizing). Dataclass (pas Pydantic) parce que :
- 100 % déterministe, construit côté Python pur (pas de frontière LLM)
- Sérialisation interne (queue, dashboard, journal simulator) se fait via
  `asdict(proposal)` — pas besoin de la machinerie pydantic
- `ichimoku` est un `IchimokuPayload` (Pydantic), conservé typé ; c'est le
  `asdict` qui aplatit si besoin

Invariants :
- `strategy_id` ∈ clés de `config/strategies.yaml`
- `side ∈ {"long", "short"}`
- `rr >= config.min_rr` (sinon `build_proposal` retourne `None`)
- `risk_pct ∈ [0, StrategyConfig.max_risk_pct_equity]`
- `0.0 <= conviction <= 1.0`, calqué sur `signal.confidence × config.coef_self_improve`

Ces invariants sont vérifiés par les tests `tests/strategies/test_build_proposal_*.py`.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Literal

from .skills import IchimokuPayload, _utc_now


Side = Literal["long", "short"]


@dataclass
class TradeProposal:
    """Proposition de trade — produite UNIQUEMENT par `src/strategies/<id>.build_proposal`."""

    strategy_id: str
    asset: str
    asset_class: str                                    # "equity" | "forex" | "crypto"
    side: Side
    entry_price: float
    stop_price: float
    tp_prices: list[float]                              # tp1, tp2 (tp3 si défini)
    rr: float                                           # |tp1 - entry| / |entry - stop|, absolu
    conviction: float                                   # [0, 1] — calqué sur signal.confidence
    risk_pct: float                                     # [0, max_risk_pct_equity]
    catalysts: list[str]
    ichimoku: IchimokuPayload                           # typé, PAS un dict (§8.8.1 fix A3)

    # ID unique pour traçabilité bout-en-bout (log → risk-gate → dashboard → Telegram)
    proposal_id: str = field(default_factory=lambda: f"tp_{uuid.uuid4().hex[:12]}")
    ts: str = field(default_factory=_utc_now)


__all__ = ["TradeProposal", "Side"]
