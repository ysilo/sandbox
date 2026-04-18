"""
src.self_improve.selector — étape 5 du self-improve (§13.2, §13.3.1).

Filtre les patchs validés selon la **blacklist §13.3.1** (on ne touche jamais
au risk layer, kill switch, ni au contrat `RiskDecision`), puis classe les
patchs restants par score composite et en retient **au plus 1 par semaine**
(contrainte §13.3.1).

Formule du score composite :

    score = 0.5 * t_stat_norm + 0.3 * sharpe_delta_norm + 0.2 * dsr

Où :
- `t_stat_norm  = min(t_stat / 5, 1.0)`      (5 = plafond normatif)
- `sharpe_delta_norm = min(sharpe_delta / 0.5, 1.0)` (0.5 = délta optimal)
- `dsr` est déjà dans [0, 1]

Contrat :
- Input  : list[(StrategyPatch, PatchValidationResult)]
- Output : `SelectionResult` avec `selected` (≤ 1 en V1) + `rejected` + scores

Blacklist (§13.3.1) — les patchs qui touchent ces scopes sont rejetés :
- `src/risk/`                            → scope commence par `risk:` (sauf whitelist)
- `src/orchestrator/kill_switch.py`      → scope `orchestrator:kill_switch`
- `config/risk.yaml`                     → scope `risk:*` (tuning OK sauf hard limits)
- Contrat `RiskDecision`                 → scope `contract:RiskDecision`

V1 : on bloque `risk:kill_switch`, `risk:circuit_breaker`, `risk:max_daily_loss`,
`orchestrator:*`, `contract:*`. Les param tunings `risk:min_conviction` et
`risk:stop_atr_mult` sont autorisés (pas des hard limits).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from src.self_improve.patch import StrategyPatch
from src.self_improve.validator import PatchValidationResult
from src.utils.logging_utils import get_logger


log = get_logger("SelfImprove.Selector")


# Scopes interdits (§13.3.1). Les autres scopes passent.
BLACKLIST_EXACT: set[str] = {
    "risk:kill_switch",
    "risk:circuit_breaker",
    "risk:max_daily_loss_pct_equity",
    "risk:max_open_positions",
    "risk:max_risk_per_trade_pct_equity",
}
BLACKLIST_PREFIXES: tuple[str, ...] = (
    "orchestrator:",
    "contract:",
)


# Paramètres de la formule de score
_T_STAT_CAP: float = 5.0
_SHARPE_DELTA_CAP: float = 0.5
_MAX_PATCHES_PER_WEEK: int = 1       # §13.3.1


# ---------------------------------------------------------------------------
# Dataclass résultat
# ---------------------------------------------------------------------------


@dataclass
class ScoredPatch:
    patch: StrategyPatch
    validation: PatchValidationResult
    score: float
    # Raison du rejet (vide si le patch est dans `selected`). L'appartenance
    # à `SelectionResult.rejected` ou `blacklisted` est la source de vérité
    # pour "est-ce bloqué ?".
    block_reason: str = ""


@dataclass
class SelectionResult:
    selected: list[ScoredPatch] = field(default_factory=list)
    rejected: list[ScoredPatch] = field(default_factory=list)
    blacklisted: list[ScoredPatch] = field(default_factory=list)

    @property
    def top(self) -> Optional[ScoredPatch]:
        return self.selected[0] if self.selected else None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_blacklisted(target: str) -> tuple[bool, str]:
    if not target:
        return False, ""
    if target in BLACKLIST_EXACT:
        return True, f"scope `{target}` interdit par la blacklist §13.3.1"
    for prefix in BLACKLIST_PREFIXES:
        if target.startswith(prefix):
            return True, f"scope `{target}` interdit (préfixe `{prefix}`)"
    return False, ""


def _compute_score(val: PatchValidationResult) -> float:
    t_stat_norm = min(max(val.t_stat, 0.0) / _T_STAT_CAP, 1.0)
    sharpe_delta_norm = min(max(val.sharpe_delta, 0.0) / _SHARPE_DELTA_CAP, 1.0)
    dsr = max(0.0, min(val.dsr, 1.0))
    return round(0.5 * t_stat_norm + 0.3 * sharpe_delta_norm + 0.2 * dsr, 4)


# ---------------------------------------------------------------------------
# API publique
# ---------------------------------------------------------------------------


def select_patches(
    candidates: list[tuple[StrategyPatch, PatchValidationResult]],
    *,
    max_per_week: int = _MAX_PATCHES_PER_WEEK,
) -> SelectionResult:
    """Étape 5 du self-improve : blacklist + classement + top-k.

    - Rejette les patchs qui n'ont pas passé la validation.
    - Rejette les patchs dont le scope est blacklisté.
    - Classe les survivants par `score` décroissant et retient top `max_per_week`.
    """
    result = SelectionResult()

    scored: list[ScoredPatch] = []
    for patch, val in candidates:
        score = _compute_score(val)
        sp = ScoredPatch(patch=patch, validation=val, score=score)

        if not val.passed:
            sp.block_reason = "validation_failed"
            result.rejected.append(sp)
            continue

        blocked, reason = _is_blacklisted(patch.target)
        if blocked:
            sp.block_reason = reason
            result.blacklisted.append(sp)
            continue

        scored.append(sp)

    scored.sort(key=lambda s: s.score, reverse=True)
    quota = max(0, int(max_per_week))
    result.selected = scored[:quota]
    # Les au-delà deviennent "rejected" par quota (pas réellement mauvais).
    for sp in scored[quota:]:
        sp.block_reason = "weekly_quota_reached"
        result.rejected.append(sp)

    log.info(
        "selection_done",
        selected=len(result.selected),
        rejected=len(result.rejected),
        blacklisted=len(result.blacklisted),
    )
    return result


__all__ = [
    "BLACKLIST_EXACT",
    "BLACKLIST_PREFIXES",
    "ScoredPatch",
    "SelectionResult",
    "select_patches",
]
