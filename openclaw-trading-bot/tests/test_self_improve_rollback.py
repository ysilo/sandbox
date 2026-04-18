"""
tests/test_self_improve_rollback.py — étape 8 §13.3.5.

Vérifie :
- patch inconnu → outcome.errors contient "patch_not_found"
- raison invalide → fallback sur "manual"
- git backend succeed=True → revert OK, status updated, trace recorded
- git backend succeed=False → revert KO mais status/trace toujours updated
- pas de merge_commit → git revert skipé mais trace quand même écrite
- notifier injecté → notifier.send_alert appelé
- pas de notifier → pas d'erreur silencieuse
"""
from __future__ import annotations

from dataclasses import dataclass, field

from src.memory.db import init_db
from src.memory.repositories import PatchesRepository, RollbacksRepository
from src.self_improve.rollback import (
    NoOpGitBackend,
    RevertResult,
    rollback_patch,
    VALID_REASONS,
)


@dataclass
class _FakeNotifier:
    calls: list[tuple] = field(default_factory=list)

    def send_alert(self, message: str, *, level: str = "ERROR", code: str | None = None) -> bool:
        self.calls.append((message, level, code))
        return True


def _setup(con, *, patch_id: str = "P-1", merge_commit: str | None = "abc123"):
    patches = PatchesRepository(con)
    rollbacks = RollbacksRepository(con)
    patches.insert(patch_id=patch_id, target="strategy:X", kind="param_tuning")
    if merge_commit:
        patches.set_status(patch_id, "active", merge_commit=merge_commit, active=True)
    return patches, rollbacks


def test_unknown_patch_returns_error():
    con = init_db(":memory:")
    patches = PatchesRepository(con)
    rollbacks = RollbacksRepository(con)
    out = rollback_patch(
        "P-DOES-NOT-EXIST", reason="manual",
        patches_repo=patches, rollbacks_repo=rollbacks,
    )
    assert out.ok is False
    assert "patch_not_found" in out.errors


def test_invalid_reason_fallback_manual():
    con = init_db(":memory:")
    patches, rollbacks = _setup(con)
    out = rollback_patch(
        "P-1", reason="bogus", patches_repo=patches, rollbacks_repo=rollbacks,
    )
    assert out.reason == "manual"
    assert out.ok is True


def test_all_valid_reasons_accepted():
    for reason in VALID_REASONS:
        con = init_db(":memory:")
        patches, rollbacks = _setup(con, patch_id=f"P-{reason}")
        out = rollback_patch(
            f"P-{reason}", reason=reason,
            patches_repo=patches, rollbacks_repo=rollbacks,
        )
        assert out.reason == reason
        assert out.ok is True


def test_successful_rollback_flow():
    con = init_db(":memory:")
    patches, rollbacks = _setup(con)
    notifier = _FakeNotifier()
    git = NoOpGitBackend(succeed=True, revert_commit="def456")

    out = rollback_patch(
        "P-1", reason="canary_failed",
        patches_repo=patches, rollbacks_repo=rollbacks,
        git=git, notifier=notifier, triggered_by="canary_watchdog",
        metrics_snapshot={"sharpe_live": 0.3},
    )

    assert out.ok is True
    assert out.git_revert_ok is True
    assert out.git_revert_commit == "def456"
    assert out.patch_status_updated is True
    assert out.trace_recorded is True
    assert out.notified is True

    # DB state
    row = patches.get("P-1")
    assert row["status"] == "rolled_back"
    assert row["rollback_reason"] == "canary_failed"
    assert row["active"] == 0
    traces = rollbacks.for_patch("P-1")
    assert len(traces) == 1
    assert traces[0]["reason"] == "canary_failed"

    # Notifier appelé
    assert len(notifier.calls) == 1
    message, level, code = notifier.calls[0]
    assert "P-1" in message
    assert level == "CRITICAL"
    assert code == "ROLLBACK:P-1"


def test_git_backend_failure_still_updates_db():
    con = init_db(":memory:")
    patches, rollbacks = _setup(con)
    git = NoOpGitBackend(succeed=False)

    out = rollback_patch(
        "P-1", reason="sharpe_regression_7d",
        patches_repo=patches, rollbacks_repo=rollbacks, git=git,
    )

    assert out.git_revert_ok is False
    assert any("git_revert_failed" in e for e in out.errors)
    # Mais le statut a été mis à jour et la trace recorded
    assert out.patch_status_updated is True
    assert out.trace_recorded is True
    # → ok=False à cause des erreurs git
    assert out.ok is False


def test_no_merge_commit_skips_git_revert():
    con = init_db(":memory:")
    # Patch en status "proposed" (pas de merge_commit)
    patches, rollbacks = _setup(con, merge_commit=None)

    out = rollback_patch(
        "P-1", reason="manual",
        patches_repo=patches, rollbacks_repo=rollbacks,
    )
    assert out.git_revert_ok is True      # pas de merge → revert considéré OK
    assert out.git_revert_commit is None
    assert out.ok is True


def test_no_notifier_no_crash():
    con = init_db(":memory:")
    patches, rollbacks = _setup(con)

    out = rollback_patch(
        "P-1", reason="kill_switch",
        patches_repo=patches, rollbacks_repo=rollbacks, notifier=None,
    )
    assert out.notified is False
    assert out.ok is True


def test_noop_git_backend_revert_result_shape():
    res = NoOpGitBackend().revert(merge_commit="xyz", reason="manual")
    assert isinstance(res, RevertResult)
    assert res.ok is True
    assert res.commit == "00000000"
