"""
src.self_improve.diagnostician — étape 2 du self-improve (§13.2).

Identifie des **patterns récurrents de perte** dans un `CollectedDataset` et
propose des axes d'amélioration. En V1 le diagnostic est un stub déterministe
purement Python (pas d'appel LLM) : c'est suffisant pour exercer le reste de
la pipeline (validation, sélection, PR locale, rollback) et sera remplacé par
un skill `architecture-reviewer` dans une version future.

Contrat :
- Input  : `CollectedDataset` (voir `collector.py`).
- Output : `DiagnosisReport` avec `patterns: list[DiagnosedPattern]`.

Un `DiagnosedPattern` expose :
- `pattern` (str)          : description courte en français
- `frequency` (int)        : nombre de trades qui matchent le pattern
- `sample_trade_ids`       : quelques IDs pour traçabilité
- `suggested_fix` (str)    : direction d'amélioration (paramètre à ajuster…)
- `scope` (str)            : `strategy:<id>` | `risk:<param>` | `regime:<id>`
- `severity` (str)         : `low|mid|high`

Règles stub (heuristiques fiables, pas besoin de LLM) :
1. Si ≥ 3 pertes sur la même stratégie → pattern `concentration_strategy`.
2. Si ≥ 2 pertes avec `conviction < 0.5` → pattern `low_conviction`.
3. Si ≥ 2 pertes d'une durée < 2 h → pattern `quick_loss_scalp`.
4. Si ≥ 2 pertes dont le couple (asset, strategy) se répète → pattern `repeat_loss`.
5. Si le ratio losers global > 40 % → pattern `regime_wide_loss`.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Optional

from src.self_improve.collector import CollectedDataset
from src.utils.logging_utils import get_logger


log = get_logger("SelfImprove.Diagnostician")


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class DiagnosedPattern:
    pattern: str
    frequency: int
    sample_trade_ids: list[str] = field(default_factory=list)
    suggested_fix: str = ""
    scope: str = ""
    severity: str = "low"      # low | mid | high


@dataclass
class DiagnosisReport:
    patterns: list[DiagnosedPattern] = field(default_factory=list)
    total_losers: int = 0
    total_trades: int = 0
    note: Optional[str] = None

    @property
    def has_actionable_patterns(self) -> bool:
        return any(p.severity in {"mid", "high"} for p in self.patterns)


# ---------------------------------------------------------------------------
# Heuristiques
# ---------------------------------------------------------------------------


def _pattern_concentration_strategy(losers: list[dict]) -> Optional[DiagnosedPattern]:
    by_strategy = Counter(t.get("strategy") or "unknown" for t in losers)
    for strat, freq in by_strategy.most_common(1):
        if freq >= 3:
            ids = [t["id"] for t in losers if t.get("strategy") == strat][:5]
            return DiagnosedPattern(
                pattern=f"concentration_loser_strategy:{strat}",
                frequency=freq,
                sample_trade_ids=ids,
                suggested_fix=(
                    f"Diminuer `max_risk_pct` de la stratégie `{strat}` ou resserrer "
                    "`min_composite_score`."
                ),
                scope=f"strategy:{strat}",
                severity="high" if freq >= 5 else "mid",
            )
    return None


def _pattern_low_conviction(losers: list[dict]) -> Optional[DiagnosedPattern]:
    low = [t for t in losers if (t.get("conviction") or 1.0) < 0.5]
    if len(low) >= 2:
        return DiagnosedPattern(
            pattern="low_conviction_losses",
            frequency=len(low),
            sample_trade_ids=[t["id"] for t in low[:5]],
            suggested_fix=(
                "Relever le seuil `min_conviction_to_propose` dans config/risk.yaml."
            ),
            scope="risk:min_conviction",
            severity="mid",
        )
    return None


def _pattern_quick_loss_scalp(losers: list[dict]) -> Optional[DiagnosedPattern]:
    quick = [t for t in losers if (t.get("duration_hours") or 999.0) < 2.0]
    if len(quick) >= 2:
        return DiagnosedPattern(
            pattern="quick_loss_scalp",
            frequency=len(quick),
            sample_trade_ids=[t["id"] for t in quick[:5]],
            suggested_fix=(
                "Allonger le stop initial ou réduire l'exposition aux scalps intraday."
            ),
            scope="risk:stop_distance",
            severity="mid",
        )
    return None


def _pattern_repeat_loss(losers: list[dict]) -> Optional[DiagnosedPattern]:
    pairs = Counter(
        (t.get("asset") or "?", t.get("strategy") or "?") for t in losers
    )
    asset_strat, freq = (pairs.most_common(1) or [(("", ""), 0)])[0]
    if freq >= 2:
        asset, strat = asset_strat
        ids = [
            t["id"]
            for t in losers
            if (t.get("asset"), t.get("strategy")) == asset_strat
        ][:5]
        return DiagnosedPattern(
            pattern=f"repeat_loss:{asset}:{strat}",
            frequency=freq,
            sample_trade_ids=ids,
            suggested_fix=(
                f"Filtrer l'actif `{asset}` pour la stratégie `{strat}` pendant "
                "quelques cycles le temps d'investiguer."
            ),
            scope=f"strategy:{strat}",
            severity="mid" if freq == 2 else "high",
        )
    return None


def _pattern_regime_wide_loss(dataset: CollectedDataset) -> Optional[DiagnosedPattern]:
    if dataset.total >= 10 and dataset.ratio_losers > 0.4:
        return DiagnosedPattern(
            pattern="regime_wide_loss",
            frequency=len(dataset.losers),
            sample_trade_ids=[t["id"] for t in dataset.losers[:5]],
            suggested_fix=(
                "Envisager une pause ou passer en mode `risk_off` via config/risk.yaml."
            ),
            scope="regime:global",
            severity="high",
        )
    return None


# ---------------------------------------------------------------------------
# API publique
# ---------------------------------------------------------------------------


def diagnose(dataset: CollectedDataset) -> DiagnosisReport:
    """Étape 2 du self-improve : patterns récurrents de pertes.

    Stub déterministe — voir module docstring pour les règles appliquées.
    """
    report = DiagnosisReport(
        total_losers=len(dataset.losers),
        total_trades=dataset.total,
    )

    if dataset.total == 0:
        report.note = "no_closed_trades_in_window"
        log.info("diagnose_empty_dataset")
        return report

    if len(dataset.losers) == 0:
        report.note = "no_losers_in_window"
        log.info("diagnose_no_losers", total=dataset.total)
        return report

    for maker in (
        lambda: _pattern_concentration_strategy(dataset.losers),
        lambda: _pattern_low_conviction(dataset.losers),
        lambda: _pattern_quick_loss_scalp(dataset.losers),
        lambda: _pattern_repeat_loss(dataset.losers),
        lambda: _pattern_regime_wide_loss(dataset),
    ):
        pat = maker()
        if pat:
            report.patterns.append(pat)

    log.info(
        "diagnose_done",
        patterns=len(report.patterns),
        losers=report.total_losers,
        total=report.total_trades,
    )
    return report


__all__ = ["DiagnosedPattern", "DiagnosisReport", "diagnose"]
