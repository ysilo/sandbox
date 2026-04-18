"""
src.utils.logging_utils — logger JSON structuré §7.5.1.

Caractéristiques :
- Sortie JSON line-delimited vers `data/logs/YYYY-MM-DD.log.jsonl`
  (+ réplique dans stdout pour `docker logs`)
- Schéma fixe : ts, level, component, event, error_code, cycle_id, cause,
  remediation, context
- `cycle_id` propagé via `contextvars` : `with logging.cycle_scope(uuid): ...`
- Invariant : tout log de niveau ≥ WARNING doit porter `error_code` + `remediation`.
  En l'absence de l'un des deux, un warning meta-log est émis (mais le log
  original passe quand même pour ne pas rater d'info).
- Si l'appelant fournit `ec=EC.X`, le logger injecte automatiquement code +
  remediation par défaut.

Usage :
    from src.utils.logging_utils import get_logger, cycle_scope, log_event
    from src.utils.error_codes import EC

    log = get_logger("DataFetcher")

    with cycle_scope("c-abc"):
        log.error("source_unreachable", ec=EC.NET_002, asset="RUI.PA", cause="...")

Rationale du « rouler le sien » plutôt que `loguru` : on veut un schéma figé,
testable, et une invariance dure (remediation required). Loguru aurait demandé
un wrapper de toute façon.
"""
from __future__ import annotations

import contextvars
import json
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .error_codes import EC


# ---------------------------------------------------------------------------
# État global
# ---------------------------------------------------------------------------


_CYCLE_ID: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "openclaw_cycle_id", default=None
)

_LOG_LOCK = threading.Lock()
_LOG_PATH: Optional[Path] = None


def configure(log_dir: str | Path = "data/logs") -> None:
    """À appeler une fois au startup. Crée le dossier logs si absent."""
    global _LOG_PATH
    p = Path(log_dir)
    p.mkdir(parents=True, exist_ok=True)
    _LOG_PATH = p


def _log_path_for_today() -> Path:
    d = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    base = _LOG_PATH or Path("data/logs")
    base.mkdir(parents=True, exist_ok=True)
    return base / f"{d}.log.jsonl"


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + (
        f"{datetime.now(tz=timezone.utc).microsecond // 1000:03d}Z"
    )


# ---------------------------------------------------------------------------
# cycle_scope — contextvar pour corréler tous les logs d'un cycle
# ---------------------------------------------------------------------------


class _CycleScope:
    def __init__(self, cycle_id: str) -> None:
        self.cycle_id = cycle_id
        self._token: Optional[contextvars.Token[Optional[str]]] = None

    def __enter__(self) -> "_CycleScope":
        self._token = _CYCLE_ID.set(self.cycle_id)
        return self

    def __exit__(self, *exc: Any) -> None:
        if self._token is not None:
            _CYCLE_ID.reset(self._token)


def cycle_scope(cycle_id: str) -> _CycleScope:
    return _CycleScope(cycle_id)


def current_cycle_id() -> Optional[str]:
    return _CYCLE_ID.get()


# ---------------------------------------------------------------------------
# Emission
# ---------------------------------------------------------------------------


_LEVELS_WITH_REMEDIATION = {"WARNING", "ERROR", "CRITICAL"}


def _emit(record: dict[str, Any]) -> None:
    line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
    with _LOG_LOCK:
        try:
            path = _log_path_for_today()
            with path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            # Disque plein ou perms : on ne veut pas planter, on dégrade sur stderr
            pass
        # Toujours stdout pour docker logs
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


def log_event(
    level: str,
    component: str,
    event: str,
    *,
    ec: EC | None = None,
    error_code: str | None = None,
    cause: str | None = None,
    remediation: str | None = None,
    cycle_id: str | None = None,
    **context: Any,
) -> None:
    """Point d'entrée central. Utilisé par les méthodes du logger par composant.

    Règles (§7.5.1) :
    - Niveau ≥ WARNING → `error_code` + `remediation` obligatoires
    - Si `ec=EC.X` fourni, injecte code + default_remediation (si vides)
    """
    level = level.upper()

    if ec is not None:
        error_code = error_code or ec.code
        remediation = remediation or ec.default_remediation

    # Invariant §7.5.1 — warning meta-log si l'appelant a manqué un champ obligatoire
    if level in _LEVELS_WITH_REMEDIATION:
        if not error_code:
            _emit({
                "ts": _now_iso(),
                "level": "WARNING",
                "component": "LoggingInvariant",
                "event": "missing_error_code",
                "cause": f"log {level} sans error_code: component={component} event={event}",
                "remediation": "Ajouter ec=EC.XXX ou error_code=\"XXX_NNN\" au site d'appel (§7.5.1).",
                "context": {"offender_component": component, "offender_event": event},
            })
        if not remediation:
            _emit({
                "ts": _now_iso(),
                "level": "WARNING",
                "component": "LoggingInvariant",
                "event": "missing_remediation",
                "cause": f"log {level} sans remediation: component={component} event={event}",
                "remediation": "Ajouter `remediation=` ou passer `ec=EC.XXX` (rempli par défaut).",
                "context": {"offender_component": component, "offender_event": event},
            })

    record: dict[str, Any] = {
        "ts": _now_iso(),
        "level": level,
        "component": component,
        "event": event,
    }
    if error_code:
        record["error_code"] = error_code
    cid = cycle_id or current_cycle_id()
    if cid:
        record["cycle_id"] = cid
    if cause is not None:
        record["cause"] = cause
    if remediation is not None:
        record["remediation"] = remediation
    if context:
        record["context"] = context

    _emit(record)


# ---------------------------------------------------------------------------
# Wrapper ergonomique par composant — pattern `log = get_logger("Name")`
# ---------------------------------------------------------------------------


class _ComponentLogger:
    def __init__(self, component: str) -> None:
        self.component = component

    def _log(self, level: str, event: str, **kw: Any) -> None:
        log_event(level, self.component, event, **kw)

    def debug(self, event: str, **kw: Any) -> None:
        self._log("DEBUG", event, **kw)

    def info(self, event: str, **kw: Any) -> None:
        self._log("INFO", event, **kw)

    def warning(self, event: str, **kw: Any) -> None:
        self._log("WARNING", event, **kw)

    def error(self, event: str, **kw: Any) -> None:
        self._log("ERROR", event, **kw)

    def critical(self, event: str, **kw: Any) -> None:
        self._log("CRITICAL", event, **kw)


def get_logger(component: str) -> _ComponentLogger:
    return _ComponentLogger(component)


__all__ = [
    "configure",
    "cycle_scope",
    "current_cycle_id",
    "get_logger",
    "log_event",
]
