"""
src.orchestrator.resilience — helpers §8.7.1 pour timeouts/retry/fallbacks.

Pattern utilisé partout dans `run.py` : chaque étape est enveloppée dans
`run_step()`, qui capture :
- TimeoutError si l'étape dépasse son budget,
- n retries avec backoff exponentiel,
- fallback paresseux si tout échoue.

Le résultat est `StepOutcome(value, used_fallback, error, attempts, flag)` :
- `value` : la sortie OK (résultat de `fn` ou du fallback)
- `used_fallback` : True si on est tombé sur le fallback
- `error` : dernière exception le cas échéant (pour log)
- `attempts` : nombre de tentatives (1 = succès direct)
- `flag` : chaîne ajoutée à `degradation_flags` si dégradé, sinon None

Design :
- Timeout thread-based (ThreadPoolExecutor) : Python ne peut pas préempter
  un thread, donc c'est indicatif pour du I/O bloquant. Pour du CPU pur
  (scanner déterministe, 0 token) le timeout sert d'assertion.
- Si `fn` est parfaitement instrumentée (honore timeout via signaux internes),
  on bénéficie d'un kill réel ; sinon le thread continue mais on abandonne
  son résultat et on passe au fallback.
- `fallback` doit être un callable 0-arg (ou None si pas de fallback prévu).
"""
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutTimeout
from dataclasses import dataclass
from typing import Callable, Generic, Optional, TypeVar

log = logging.getLogger(__name__)


T = TypeVar("T")


@dataclass
class StepOutcome(Generic[T]):
    """Résultat d'un appel `run_step()`."""

    value: T
    used_fallback: bool
    error: Optional[BaseException] = None
    attempts: int = 1
    flag: Optional[str] = None

    @property
    def ok(self) -> bool:
        """Succès sans fallback ET sans erreur."""
        return not self.used_fallback and self.error is None


def run_step(
    fn: Callable[[], T],
    *,
    step_name: str,
    timeout_s: float,
    retries: int = 0,
    backoff_s: float = 1.0,
    backoff_factor: float = 3.0,
    fallback: Optional[Callable[[], T]] = None,
    degradation_flag: Optional[str] = None,
) -> StepOutcome[T]:
    """Exécute `fn()` avec timeout, retries exponentiels et fallback.

    Args:
        fn: l'appel à protéger (0-arg, pour simplifier le typage).
        step_name: nom logique pour les logs (ex "regime_detect").
        timeout_s: budget par tentative en secondes.
        retries: nombre de retries AU-DELÀ de la 1ʳᵉ tentative (0 = pas de retry).
        backoff_s: délai avant la 1ʳᵉ retry en secondes.
        backoff_factor: facteur exponentiel entre retries (3.0 = 1s, 3s, 9s).
        fallback: callable 0-arg appelé si `fn` échoue définitivement. Si None,
            l'exception est rélevée (le caller choisit quoi faire).
        degradation_flag: flag à propager dans `CycleResult.degradation_flags`
            si on est tombé sur le fallback. Si None, pas de flag (panne silencieuse).

    Returns:
        `StepOutcome` — toujours avec `.value` populée (depuis `fn` ou `fallback`).

    Raises:
        Toute exception de `fn` SI `fallback is None` ET toutes les tentatives échouent.
    """
    last_err: Optional[BaseException] = None
    max_attempts = 1 + retries

    for attempt in range(1, max_attempts + 1):
        try:
            # Timeout via ThreadPoolExecutor.submit.result(timeout=...)
            with ThreadPoolExecutor(max_workers=1) as ex:
                fut = ex.submit(fn)
                try:
                    val = fut.result(timeout=timeout_s)
                except FutTimeout:
                    last_err = TimeoutError(
                        f"{step_name} dépassé {timeout_s}s (tentative {attempt}/{max_attempts})"
                    )
                    log.warning(
                        "step_timeout",
                        extra={"step": step_name, "timeout_s": timeout_s, "attempt": attempt},
                    )
                    # Laisser le fut finir en arrière-plan (on ne peut pas kill)
                    _ = fut  # reference kept so executor shuts down after fn returns
                    raise last_err
            return StepOutcome(value=val, used_fallback=False, attempts=attempt)

        except BaseException as e:  # noqa: BLE001 — on log et on retry/fallback
            last_err = e
            log.warning(
                "step_failed",
                extra={"step": step_name, "attempt": attempt, "err": str(e)},
            )
            if attempt < max_attempts:
                # Backoff : backoff_s * backoff_factor^(attempt-1)
                delay = backoff_s * (backoff_factor ** (attempt - 1))
                time.sleep(delay)
                continue
            # Plus de retries — on passe au fallback
            break

    # Tous les essais ont échoué
    if fallback is not None:
        try:
            val = fallback()
            log.warning(
                "step_fallback_used",
                extra={"step": step_name, "flag": degradation_flag},
            )
            return StepOutcome(
                value=val,
                used_fallback=True,
                error=last_err,
                attempts=max_attempts,
                flag=degradation_flag,
            )
        except BaseException as fb_err:  # noqa: BLE001
            log.error(
                "step_fallback_failed",
                extra={"step": step_name, "err": str(fb_err)},
            )
            # Exception originale relevée — le caller gère
            raise last_err if last_err is not None else fb_err

    # Pas de fallback — on propage
    assert last_err is not None  # pour mypy
    raise last_err


__all__ = ["StepOutcome", "run_step"]
