"""
src.contracts.cycle — CycleResult (sortie de `src/orchestrator/run.py`).

Source : TRADING_BOT_ARCHITECTURE.md §8.7.1 + §10 (flags de dégradation).

Chaque exécution de cycle (equity/forex scheduled, crypto scheduled, ad-hoc
news-driven) retourne un CycleResult :
- `success` : au moins un cycle complet terminé, 0 ou n proposals approuvées
- `aborted` : cycle coupé tôt (kill-switch, cooldown ad-hoc, circuit breaker…)
- `degraded` : cycle terminé avec ≥1 `degradation_flags` mais pas encore critique

Le champ `degradation_flags` est exposé dans le dashboard (§14.3.4) et le
message Telegram de fin de cycle. Si `len(degradation_flags) >= 3` OU
`risk_gate_failure_rate > 50%`, le circuit breaker C5 se déclenche (§11.1)
et le cycle suivant est reporté de 1h.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field, confloat, conint

from .skills import _utc_now

CycleStatus = Literal["success", "aborted", "degraded"]
CycleKind = Literal["scheduled", "adhoc"]


class CycleResult(BaseModel):
    """Résultat d'un cycle orchestrateur — log structuré + payload Telegram."""

    status: CycleStatus
    kind: CycleKind = "scheduled"
    session_name: str = ""                              # "eu_morning", "crypto_18utc", "adhoc_news_*"
    proposals: conint(ge=0) = 0                         # proposals approuvées (post risk-gate)
    proposals_rejected: conint(ge=0) = 0                # proposals rejetées par la risk-gate
    reason: Optional[str] = None                        # message d'arrêt si aborted/degraded
    report: Optional[str] = None                        # chemin vers le HTML dashboard du cycle
    degradation_flags: list[str] = Field(default_factory=list)
    risk_gate_failure_rate: confloat(ge=0.0, le=1.0) = 0.0
    duration_s: confloat(ge=0.0) = 0.0
    ts: str = Field(default_factory=_utc_now)

    # ------------------------------------------------------------------
    # Factories
    # ------------------------------------------------------------------

    @classmethod
    def aborted(cls, reason: str, *, session_name: str = "", kind: CycleKind = "scheduled",
                degradation_flags: list[str] | None = None) -> "CycleResult":
        return cls(
            status="aborted",
            kind=kind,
            session_name=session_name,
            reason=reason,
            degradation_flags=degradation_flags or [],
        )

    @classmethod
    def success(
        cls,
        *,
        proposals: int,
        proposals_rejected: int = 0,
        report: Optional[str] = None,
        kind: CycleKind = "scheduled",
        session_name: str = "",
        degradation_flags: list[str] | None = None,
        risk_gate_failure_rate: float = 0.0,
        duration_s: float = 0.0,
    ) -> "CycleResult":
        status: CycleStatus = "degraded" if degradation_flags else "success"
        return cls(
            status=status,
            kind=kind,
            session_name=session_name,
            proposals=proposals,
            proposals_rejected=proposals_rejected,
            report=report,
            degradation_flags=degradation_flags or [],
            risk_gate_failure_rate=risk_gate_failure_rate,
            duration_s=duration_s,
        )


__all__ = ["CycleResult", "CycleStatus", "CycleKind"]
