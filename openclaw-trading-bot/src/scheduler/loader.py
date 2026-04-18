"""
src.scheduler.loader — chargement + validation de `config/schedules.yaml` (§15.1).

- Chaque job a un `name` unique (dans l'ensemble cycles+maintenance),
  un `cron` validé par `croniter.is_valid()` et un `pipeline`.
- `markets` est optionnel (présent pour les cycles equity/forex/crypto).
- `news_watcher` est une section optionnelle séparée avec ses propres
  paramètres (poll_seconds, seuils d'impact, cooldown, pipeline ad-hoc).

Aucune instanciation d'APScheduler à ce niveau — voir `runner.py`.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from croniter import croniter

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScheduleJob:
    """Un job APScheduler : cycle planifié ou tâche de maintenance."""

    name: str
    cron: str
    pipeline: str
    markets: list[str] = field(default_factory=list)
    kind: str = "cycle"   # "cycle" | "maintenance"


@dataclass(frozen=True)
class NewsWatcherConfig:
    """Paramètres du NewsWatcher (§15.1)."""

    enabled: bool = False
    poll_seconds: int = 60
    impact_threshold_candidate: float = 0.60
    impact_threshold_adhoc: float = 0.80
    adhoc_cooldown_minutes: int = 15
    adhoc_pipeline: str = "focused_cycle"
    max_adhoc_per_day: int = 6


@dataclass(frozen=True)
class SchedulesConfig:
    """Résultat du loader."""

    cycles: list[ScheduleJob]
    maintenance: list[ScheduleJob]
    news_watcher: NewsWatcherConfig

    @property
    def all_jobs(self) -> list[ScheduleJob]:
        return [*self.cycles, *self.maintenance]


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def _parse_job(raw: dict[str, Any], *, kind: str) -> ScheduleJob:
    if not isinstance(raw, dict):
        raise ValueError(f"CFG_005 job invalide (dict attendu) : {raw!r}")
    name = raw.get("name")
    cron = raw.get("cron")
    pipeline = raw.get("pipeline")
    if not name or not isinstance(name, str):
        raise ValueError(f"CFG_005 job sans name : {raw!r}")
    if not cron or not isinstance(cron, str):
        raise ValueError(f"CFG_005 job sans cron : {raw!r}")
    if not pipeline or not isinstance(pipeline, str):
        raise ValueError(f"CFG_005 job sans pipeline : {raw!r}")
    if not croniter.is_valid(cron):
        raise ValueError(f"CFG_006 cron invalide pour {name!r} : {cron!r}")
    markets = raw.get("markets") or []
    if not isinstance(markets, list):
        raise ValueError(f"CFG_005 markets doit être une liste pour {name!r}")
    return ScheduleJob(
        name=name,
        cron=cron,
        pipeline=pipeline,
        markets=[str(m) for m in markets],
        kind=kind,
    )


def _parse_news_watcher(raw: Optional[dict[str, Any]]) -> NewsWatcherConfig:
    if not raw:
        return NewsWatcherConfig(enabled=False)
    return NewsWatcherConfig(
        enabled=bool(raw.get("enabled", False)),
        poll_seconds=int(raw.get("poll_seconds", 60)),
        impact_threshold_candidate=float(raw.get("impact_threshold_candidate", 0.60)),
        impact_threshold_adhoc=float(raw.get("impact_threshold_adhoc", 0.80)),
        adhoc_cooldown_minutes=int(raw.get("adhoc_cooldown_minutes", 15)),
        adhoc_pipeline=str(raw.get("adhoc_pipeline", "focused_cycle")),
        max_adhoc_per_day=int(raw.get("max_adhoc_per_day", 6)),
    )


def load_schedules(raw: Optional[dict[str, Any]] = None) -> SchedulesConfig:
    """Charge (depuis `config/schedules.yaml` si `raw` absent) et valide.

    Les noms de jobs doivent être uniques globalement (cycles+maintenance).
    """
    if raw is None:
        from src.utils.config_loader import load_yaml
        raw = load_yaml("schedules.yaml")

    # La V1 historique utilisait `sessions` → on garde la compat
    cycles_raw = raw.get("cycles") or raw.get("sessions") or []
    maintenance_raw = raw.get("maintenance") or []

    if not isinstance(cycles_raw, list):
        raise ValueError("CFG_007 `cycles` doit être une liste")
    if not isinstance(maintenance_raw, list):
        raise ValueError("CFG_007 `maintenance` doit être une liste")

    cycles = [_parse_job(j, kind="cycle") for j in cycles_raw]
    maintenance = [_parse_job(j, kind="maintenance") for j in maintenance_raw]

    names: set[str] = set()
    for j in [*cycles, *maintenance]:
        if j.name in names:
            raise ValueError(f"CFG_008 nom de job dupliqué : {j.name!r}")
        names.add(j.name)

    nw = _parse_news_watcher(raw.get("news_watcher"))

    log.info(
        "schedules_loaded",
        extra={
            "cycles": len(cycles),
            "maintenance": len(maintenance),
            "news_watcher_enabled": nw.enabled,
        },
    )
    return SchedulesConfig(cycles=cycles, maintenance=maintenance, news_watcher=nw)


__all__ = [
    "ScheduleJob",
    "NewsWatcherConfig",
    "SchedulesConfig",
    "load_schedules",
]
