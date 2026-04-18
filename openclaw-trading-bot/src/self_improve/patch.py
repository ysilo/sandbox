"""
src.self_improve.patch — étape 3 du self-improve (§13.2).

Génère un `StrategyPatch` candidat à partir d'un `DiagnosedPattern`. En V1 le
générateur est un stub déterministe : il ne produit pas de diff git mais un
*contrat* décrivant la modification (quel paramètre, quelle valeur, quel
scope) — suffisant pour la validation backtest stubée et la revue humaine.

Un `StrategyPatch` expose :
- `patch_id`   : identifiant stable (P-xxxxxx)
- `target`     : `strategy:<id>` | `risk:<param>` | `regime:<id>` (= scope)
- `kind`       : `param_tuning` | `feature_add` | `bugfix` | `filter_add` | ...
- `description`: texte français court
- `change`     : dict des deltas de paramètres (ex: `{"max_risk_pct": 0.006}`)
- `source_pattern` : le pattern diagnostiqué à l'origine du patch
- `created_at`

Règles stub (mapping diagnostic → patch) :
- `concentration_loser_strategy:<id>` → `kind=param_tuning` sur la stratégie
  (réduit `max_risk_pct` de 20 %).
- `low_conviction_losses` → `kind=param_tuning` sur risk (relève
  `min_conviction_to_propose` de 0.05).
- `quick_loss_scalp` → `kind=param_tuning` sur risk (allonge `stop_atr_mult` de
  10 %).
- `repeat_loss:<asset>:<strat>` → `kind=filter_add` (ajoute l'asset à la
  blacklist temporaire de la stratégie).
- `regime_wide_loss` → `kind=regime_shift` (suggère `risk_off`).

Le stub ne modifie **aucun fichier** : il construit uniquement l'objet
`StrategyPatch`. La mise en PR locale est faite par `pr_generator.py`.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from src.self_improve.diagnostician import DiagnosedPattern
from src.utils.logging_utils import get_logger


log = get_logger("SelfImprove.PatchGenerator")


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------


@dataclass
class StrategyPatch:
    patch_id: str
    target: str                              # scope (strategy:<id> | risk:<param> | ...)
    kind: str                                # param_tuning | feature_add | bugfix | ...
    description: str
    change: dict = field(default_factory=dict)
    source_pattern: Optional[DiagnosedPattern] = None
    created_at: str = ""

    @staticmethod
    def new_id() -> str:
        return f"P-{uuid.uuid4().hex[:6].upper()}"


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).replace(microsecond=0).isoformat()


# ---------------------------------------------------------------------------
# Générateur stub
# ---------------------------------------------------------------------------


def _patch_for_concentration(pat: DiagnosedPattern) -> StrategyPatch:
    # target="strategy:<id>" → réduit max_risk_pct de 20 %
    strat = pat.scope.split(":", 1)[1] if ":" in pat.scope else "unknown"
    return StrategyPatch(
        patch_id=StrategyPatch.new_id(),
        target=pat.scope,
        kind="param_tuning",
        description=(
            f"Réduire `max_risk_pct` de la stratégie `{strat}` de 20 % (concentration "
            f"de pertes détectée : {pat.frequency} trades)."
        ),
        change={"strategy": strat, "param": "max_risk_pct", "delta_pct": -0.20},
        source_pattern=pat,
        created_at=_now_iso(),
    )


def _patch_for_low_conviction(pat: DiagnosedPattern) -> StrategyPatch:
    return StrategyPatch(
        patch_id=StrategyPatch.new_id(),
        target="risk:min_conviction_to_propose",
        kind="param_tuning",
        description=(
            f"Relever `min_conviction_to_propose` de +0.05 "
            f"({pat.frequency} pertes sur conviction faible)."
        ),
        change={"param": "min_conviction_to_propose", "delta_abs": 0.05},
        source_pattern=pat,
        created_at=_now_iso(),
    )


def _patch_for_quick_loss(pat: DiagnosedPattern) -> StrategyPatch:
    return StrategyPatch(
        patch_id=StrategyPatch.new_id(),
        target="risk:stop_atr_mult",
        kind="param_tuning",
        description=(
            f"Allonger `stop_atr_mult` de +10 % ({pat.frequency} pertes rapides < 2h)."
        ),
        change={"param": "stop_atr_mult", "delta_pct": 0.10},
        source_pattern=pat,
        created_at=_now_iso(),
    )


def _patch_for_repeat(pat: DiagnosedPattern) -> StrategyPatch:
    # pattern = "repeat_loss:<asset>:<strat>"
    parts = pat.pattern.split(":")
    asset = parts[1] if len(parts) > 1 else "?"
    strat = parts[2] if len(parts) > 2 else "?"
    return StrategyPatch(
        patch_id=StrategyPatch.new_id(),
        target=pat.scope,
        kind="filter_add",
        description=(
            f"Mettre l'actif `{asset}` en blacklist temporaire (14j) pour la stratégie "
            f"`{strat}` après {pat.frequency} pertes consécutives."
        ),
        change={"strategy": strat, "blacklist_asset": asset, "days": 14},
        source_pattern=pat,
        created_at=_now_iso(),
    )


def _patch_for_regime_wide(pat: DiagnosedPattern) -> StrategyPatch:
    return StrategyPatch(
        patch_id=StrategyPatch.new_id(),
        target="regime:global",
        kind="regime_shift",
        description=(
            f"Passer temporairement en `risk_off` (ratio pertes {pat.frequency} trades)."
        ),
        change={"regime": "risk_off", "duration_days": 7},
        source_pattern=pat,
        created_at=_now_iso(),
    )


def generate_patch(pattern: DiagnosedPattern) -> Optional[StrategyPatch]:
    """Étape 3 du self-improve : 1 pattern → 0/1 patch.

    Retourne `None` si le pattern n'est pas pris en charge par le stub.
    """
    name = pattern.pattern
    if name.startswith("concentration_loser_strategy:"):
        patch = _patch_for_concentration(pattern)
    elif name == "low_conviction_losses":
        patch = _patch_for_low_conviction(pattern)
    elif name == "quick_loss_scalp":
        patch = _patch_for_quick_loss(pattern)
    elif name.startswith("repeat_loss:"):
        patch = _patch_for_repeat(pattern)
    elif name == "regime_wide_loss":
        patch = _patch_for_regime_wide(pattern)
    else:
        log.info("no_patch_for_pattern", pattern=name)
        return None

    log.info(
        "patch_generated",
        patch_id=patch.patch_id,
        target=patch.target,
        kind=patch.kind,
    )
    return patch


__all__ = ["StrategyPatch", "generate_patch"]
