"""Tests pour src.orchestrator.resilience.run_step (§8.7.1)."""
from __future__ import annotations

import time

import pytest

from src.orchestrator.resilience import StepOutcome, run_step


def test_run_step_success_direct() -> None:
    """Cas nominal : fn réussit du premier coup."""
    outcome = run_step(
        lambda: "ok",
        step_name="noop",
        timeout_s=1.0,
    )
    assert outcome.value == "ok"
    assert outcome.used_fallback is False
    assert outcome.error is None
    assert outcome.attempts == 1
    assert outcome.flag is None
    assert outcome.ok is True


def test_run_step_retries_then_success() -> None:
    """1 échec puis succès → 2 attempts, pas de fallback."""
    calls = {"n": 0}

    def fn() -> str:
        calls["n"] += 1
        if calls["n"] < 2:
            raise RuntimeError("transient")
        return "ok"

    outcome = run_step(
        fn,
        step_name="flaky",
        timeout_s=1.0,
        retries=2,
        backoff_s=0.01,
        backoff_factor=1.0,
    )
    assert outcome.value == "ok"
    assert outcome.used_fallback is False
    assert outcome.attempts == 2
    assert outcome.ok is True


def test_run_step_exhausts_retries_and_uses_fallback() -> None:
    """Toutes les tentatives échouent → fallback appelé, flag propagé."""

    def boom() -> str:
        raise RuntimeError("always")

    outcome = run_step(
        boom,
        step_name="bad",
        timeout_s=1.0,
        retries=1,
        backoff_s=0.01,
        fallback=lambda: "fallback_value",
        degradation_flag="bad_flag",
    )
    assert outcome.value == "fallback_value"
    assert outcome.used_fallback is True
    assert outcome.flag == "bad_flag"
    assert outcome.attempts == 2
    assert outcome.error is not None
    assert outcome.ok is False


def test_run_step_no_fallback_raises() -> None:
    """Sans fallback, l'exception originale est propagée."""

    def boom() -> str:
        raise ValueError("original_error")

    with pytest.raises(ValueError, match="original_error"):
        run_step(
            boom,
            step_name="no_fb",
            timeout_s=1.0,
            retries=0,
        )


def test_run_step_timeout_triggers_fallback() -> None:
    """fn dépasse le budget → TimeoutError → fallback."""

    def slow() -> str:
        time.sleep(0.3)
        return "never_returned"

    outcome = run_step(
        slow,
        step_name="slow",
        timeout_s=0.05,
        retries=0,
        fallback=lambda: "fast_fallback",
        degradation_flag="timeout_flag",
    )
    assert outcome.value == "fast_fallback"
    assert outcome.used_fallback is True
    assert outcome.flag == "timeout_flag"
    assert isinstance(outcome.error, TimeoutError)


def test_run_step_fallback_that_raises_propagates_original() -> None:
    """Si le fallback lève, on propage l'erreur originale (pas celle du fallback)."""

    def boom() -> str:
        raise ValueError("primary")

    def bad_fb() -> str:
        raise RuntimeError("fallback_also_broken")

    with pytest.raises(ValueError, match="primary"):
        run_step(
            boom,
            step_name="both_bad",
            timeout_s=1.0,
            retries=0,
            fallback=bad_fb,
        )


def test_run_step_attempts_count_matches_retries() -> None:
    """attempts = 1 + retries quand tout échoue."""

    def boom() -> str:
        raise RuntimeError("x")

    outcome = run_step(
        boom,
        step_name="count",
        timeout_s=1.0,
        retries=3,
        backoff_s=0.001,
        fallback=lambda: "fb",
    )
    assert outcome.attempts == 4  # 1 + 3


def test_step_outcome_ok_logic() -> None:
    """`.ok` est True uniquement si pas de fallback ET pas d'erreur."""
    ok = StepOutcome(value=1, used_fallback=False, error=None)
    assert ok.ok is True

    fb = StepOutcome(value=1, used_fallback=True, error=Exception())
    assert fb.ok is False

    err = StepOutcome(value=1, used_fallback=False, error=Exception())
    assert err.ok is False
