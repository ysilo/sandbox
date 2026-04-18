"""
src.self_improve.rollback — étape 8 du self-improve (§13.3.5).

Centralise la logique de rollback d'un patch :
1. Marque le patch `rolled_back` dans la table `patches`.
2. Enregistre la trace dans la table `rollbacks`.
3. Notifie via Telegram (optionnel, injectable).
4. Annule le merge commit via une interface `GitBackend` (par défaut
   `NoOpGitBackend` — V1).

`GitBackend` est volontairement minimaliste :

    class GitBackend(Protocol):
        def revert(self, merge_commit: str, reason: str) -> RevertResult: ...

Une implémentation future pourra s'appuyer sur `subprocess.run(["git",
"revert"])`. Le stub V1 ne touche pas au dépôt et retourne toujours succès —
les tests peuvent injecter un fake backend pour simuler un échec.

Triggers (§13.3.5) pris en charge :
- `canary_failed`   : canary KO (sharpe < 0.7 × baseline, trades < 20, ...)
- `kill_switch`     : kill-switch activé pendant le canary
- `sharpe_regression_7d` : sharpe glissant 7j < baseline × 0.8
- `manual`          : rollback déclenché à la main
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Protocol

from src.memory.repositories import PatchesRepository, RollbacksRepository
from src.utils.logging_utils import get_logger


log = get_logger("SelfImprove.Rollback")


VALID_REASONS: set[str] = {
    "canary_failed",
    "kill_switch",
    "sharpe_regression_7d",
    "manual",
}


# ---------------------------------------------------------------------------
# Git backend
# ---------------------------------------------------------------------------


@dataclass
class RevertResult:
    ok: bool
    commit: Optional[str] = None
    message: str = ""


class GitBackend(Protocol):
    def revert(self, merge_commit: str, reason: str) -> RevertResult:  # pragma: no cover
        ...


@dataclass
class NoOpGitBackend:
    """V1 — ne touche pas au dépôt. Peut être configuré pour simuler un échec."""

    succeed: bool = True
    revert_commit: str = "00000000"

    def revert(self, merge_commit: str, reason: str) -> RevertResult:
        log.info(
            "noop_git_revert",
            merge_commit=merge_commit,
            reason=reason,
            succeed=self.succeed,
        )
        if not self.succeed:
            return RevertResult(ok=False, message="noop_backend_failure")
        return RevertResult(ok=True, commit=self.revert_commit, message="noop_backend_ok")


# ---------------------------------------------------------------------------
# Résultat
# ---------------------------------------------------------------------------


@dataclass
class RollbackOutcome:
    patch_id: str
    reason: str
    git_revert_ok: bool
    git_revert_commit: Optional[str] = None
    patch_status_updated: bool = False
    trace_recorded: bool = False
    notified: bool = False
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.patch_status_updated and self.trace_recorded and not self.errors


# ---------------------------------------------------------------------------
# Fonction publique
# ---------------------------------------------------------------------------


def rollback_patch(
    patch_id: str,
    *,
    reason: str,
    patches_repo: PatchesRepository,
    rollbacks_repo: RollbacksRepository,
    git: Optional[GitBackend] = None,
    notifier=None,                         # TelegramNotifier-like (duck-typed)
    triggered_by: Optional[str] = None,
    metrics_snapshot: Optional[dict] = None,
) -> RollbackOutcome:
    """Déroule les 4 étapes du rollback.

    Jamais levée d'exception : chaque étape alimente `outcome.errors`.
    L'appelant peut agir (alerte CRITICAL) sur `outcome.ok == False`.
    """
    if reason not in VALID_REASONS:
        # On n'échoue pas — on log et on continue avec "manual".
        log.warning("rollback_unknown_reason", reason=reason)
        reason = "manual"

    outcome = RollbackOutcome(
        patch_id=patch_id,
        reason=reason,
        git_revert_ok=False,
    )

    patch = patches_repo.get(patch_id)
    if not patch:
        outcome.errors.append("patch_not_found")
        log.error("rollback_patch_not_found", patch_id=patch_id)
        return outcome

    # 1) Git revert si un merge_commit existe
    merge_commit = patch.get("merge_commit")
    if merge_commit:
        backend = git or NoOpGitBackend()
        revert = backend.revert(merge_commit=merge_commit, reason=reason)
        outcome.git_revert_ok = revert.ok
        outcome.git_revert_commit = revert.commit
        if not revert.ok:
            outcome.errors.append(f"git_revert_failed:{revert.message}")
    else:
        # Pas de merge_commit → rien à révert (patch au stade "proposed/approved")
        outcome.git_revert_ok = True

    # 2) Mise à jour statut
    try:
        patches_repo.set_status(
            patch_id,
            status="rolled_back",
            active=False,
            rollback_reason=reason,
        )
        outcome.patch_status_updated = True
    except Exception as e:      # pragma: no cover - repo simple
        outcome.errors.append(f"patch_status_update_failed:{e}")

    # 3) Trace
    try:
        rollbacks_repo.record(
            patch_id=patch_id,
            reason=reason,
            triggered_by=triggered_by,
            metrics_snapshot=metrics_snapshot,
        )
        outcome.trace_recorded = True
    except Exception as e:      # pragma: no cover - repo simple
        outcome.errors.append(f"rollback_trace_failed:{e}")

    # 4) Notification Telegram (best-effort, jamais bloquant)
    if notifier is not None:
        try:
            sent = notifier.send_alert(
                f"ROLLBACK {patch_id} — {reason}",
                level="CRITICAL",
                code=f"ROLLBACK:{patch_id}",
            )
            outcome.notified = bool(sent)
        except Exception as e:  # pragma: no cover - noop backend seulement
            outcome.errors.append(f"notify_failed:{e}")

    log.info(
        "rollback_done",
        patch_id=patch_id,
        reason=reason,
        ok=outcome.ok,
        errors=len(outcome.errors),
    )
    return outcome


__all__ = [
    "VALID_REASONS",
    "RevertResult",
    "GitBackend",
    "NoOpGitBackend",
    "RollbackOutcome",
    "rollback_patch",
]
