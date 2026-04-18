"""
src.self_improve.pipeline — orchestration des 8 étapes §13.2.

Assemble les modules collector, diagnostician, patch, validator, selector,
pr_generator et persiste les patchs selectionnés dans SQLite
(`PatchesRepository.insert`). Les étapes 7 (validation humaine) et 8 (canary
14j + rollback) sont en-dehors du cycle hebdomadaire : elles sont gérées par
le webhook Telegram (§14.6) et `rollback.py` respectivement.

Boucle V1 :

    1. COLLECTE          — collector.collect_closed_trades
    2. DIAGNOSTIC        — diagnostician.diagnose
    3. IMPLÉMENTATION    — patch.generate_patch (1 patch / pattern)
    4. VALIDATION        — validator.validate_patch (backtest stub)
    5. SÉLECTION         — selector.select_patches (§13.3.1 blacklist + quota)
    6. PR LOCALE         — pr_generator.write_improvements_pending
    7. NOTIFICATION      — notifier.send_alert (optionnel)
    [8. APRÈS VALIDATION — hors cycle : canary + rollback déclenchés ailleurs]

La pipeline est safe-by-default : toute exception d'un module interne est
capturée et remonte dans `PipelineResult.errors`. Le runner n'interrompt
jamais l'orchestrateur général.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Optional

from src.memory.repositories import PatchesRepository
from src.self_improve.collector import CollectedDataset, collect_closed_trades
from src.self_improve.diagnostician import DiagnosisReport, diagnose
from src.self_improve.patch import StrategyPatch, generate_patch
from src.self_improve.pr_generator import (
    DEFAULT_PATH as DEFAULT_PENDING_PATH,
    write_improvements_pending,
)
from src.self_improve.selector import SelectionResult, select_patches
from src.self_improve.validator import (
    BacktestRunner,
    PatchValidationResult,
    StubBacktestRunner,
    ValidationThresholds,
    validate_patch,
)
from src.utils.logging_utils import get_logger


log = get_logger("SelfImprove.Pipeline")


# ---------------------------------------------------------------------------
# Dataclass résultat
# ---------------------------------------------------------------------------


@dataclass
class PipelineResult:
    dataset: Optional[CollectedDataset] = None
    diagnosis: Optional[DiagnosisReport] = None
    patches: list[StrategyPatch] = field(default_factory=list)
    validations: list[PatchValidationResult] = field(default_factory=list)
    selection: Optional[SelectionResult] = None
    pending_path: Optional[str] = None
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def run_self_improve(
    *,
    con: sqlite3.Connection,
    since_days: int = 30,
    runner: Optional[BacktestRunner] = None,
    thresholds: Optional[ValidationThresholds] = None,
    patches_repo: Optional[PatchesRepository] = None,
    notifier=None,
    max_per_week: int = 1,
    pending_path: str = DEFAULT_PENDING_PATH,
) -> PipelineResult:
    """Exécute les étapes 1→6 du self-improve et persiste les patchs retenus."""
    # Garde-fous inputs (pipeline ne lève jamais — errors dans PipelineResult).
    since_days = max(1, int(since_days))
    max_per_week = max(0, int(max_per_week))

    runner = runner or StubBacktestRunner()
    patches_repo = patches_repo or PatchesRepository(con)
    out = PipelineResult()

    # 1. COLLECTE
    try:
        out.dataset = collect_closed_trades(con, since_days=since_days)
    except Exception as e:
        out.errors.append(f"collect_failed:{e}")
        log.error("self_improve_collect_failed", cause=str(e))
        return out

    # 2. DIAGNOSTIC
    try:
        out.diagnosis = diagnose(out.dataset)
    except Exception as e:
        out.errors.append(f"diagnose_failed:{e}")
        log.error("self_improve_diagnose_failed", cause=str(e))
        return out

    # Pas de patterns → on écrit quand même le fichier vide pour idempotence
    if not out.diagnosis.patterns:
        out.selection = SelectionResult()
        try:
            out.pending_path = write_improvements_pending(
                out.selection, path=pending_path
            )
        except Exception as e:
            out.errors.append(f"pr_generator_failed:{e}")
        log.info("self_improve_no_patterns")
        return out

    # 3. IMPLÉMENTATION (stub)
    patches: list[StrategyPatch] = []
    for pat in out.diagnosis.patterns:
        try:
            p = generate_patch(pat)
            if p:
                patches.append(p)
        except Exception as e:    # pragma: no cover - stub pur
            out.errors.append(f"patch_gen_failed:{e}")
    out.patches = patches

    # 4. VALIDATION — on garde les paires (patch, validation) cohérentes même
    # si un validate_patch lève : le patch est alors exclu de la suite.
    pairs: list[tuple[StrategyPatch, PatchValidationResult]] = []
    for patch in patches:
        try:
            val = validate_patch(patch, runner=runner, thresholds=thresholds)
            pairs.append((patch, val))
        except Exception as e:
            out.errors.append(f"validation_failed:{patch.patch_id}:{e}")
            log.error(
                "self_improve_validation_failed",
                patch_id=patch.patch_id,
                cause=str(e),
            )
    out.validations = [v for _, v in pairs]

    # 5. SÉLECTION
    try:
        out.selection = select_patches(pairs, max_per_week=max_per_week)
    except Exception as e:
        out.errors.append(f"selection_failed:{e}")
        log.error("self_improve_selection_failed", cause=str(e))
        return out

    # 5.b Persister les patchs sélectionnés (status=proposed)
    for sp in out.selection.selected:
        try:
            patches_repo.insert(
                patch_id=sp.patch.patch_id,
                target=sp.patch.target,
                kind=sp.patch.kind,
                score=sp.score,
                t_stat=sp.validation.t_stat,
                sharpe_delta=sp.validation.sharpe_delta,
                dsr=sp.validation.dsr,
                metrics={
                    "sharpe_baseline": sp.validation.sharpe_baseline,
                    "sharpe_patch": sp.validation.sharpe_patch,
                    "dd_baseline": sp.validation.dd_baseline,
                    "dd_patch": sp.validation.dd_patch,
                    "trade_count": sp.validation.trade_count,
                    "description": sp.patch.description,
                },
            )
        except Exception as e:
            out.errors.append(f"patch_persist_failed:{e}")

    # 6. PR LOCALE
    try:
        out.pending_path = write_improvements_pending(
            out.selection, path=pending_path
        )
    except Exception as e:
        out.errors.append(f"pr_generator_failed:{e}")

    # 7. NOTIFICATION (best-effort)
    if notifier is not None and out.selection and out.selection.top:
        top = out.selection.top
        try:
            notifier.send_alert(
                f"Nouveau patch proposé : {top.patch.patch_id} — score {top.score:.2f}",
                level="INFO",
                code=f"SELF_IMPROVE:{top.patch.patch_id}",
            )
        except Exception as e:   # pragma: no cover
            out.errors.append(f"notify_failed:{e}")

    log.info(
        "self_improve_done",
        patches=len(out.patches),
        selected=len(out.selection.selected) if out.selection else 0,
        errors=len(out.errors),
    )
    return out


__all__ = ["PipelineResult", "run_self_improve"]
