"""
src.scheduler.runner — wiring APScheduler (§15.1).

- `build_scheduler(config, pipelines)` instancie un `BackgroundScheduler` (ou
  `BlockingScheduler` si `blocking=True`) et enregistre un job pour chaque
  entrée de `config.cycles` + `config.maintenance`.
- `pipelines` est un mapping `pipeline_name -> callable()`. Si un pipeline
  référencé dans le YAML n'a pas de callback, une `KeyError` explicite est
  levée au build (fail-fast).
- `id` du job APScheduler = `name` du job YAML (permet `replace_existing=True`
  en hot-reload futur).
- `misfire_grace_time` et `coalesce` sont configurables globalement (`§15.1`
  évoque `RUN_004 missed-fire` — on log WARNING et on coalesce par défaut).

⚠️ Aucune implémentation de `news_watcher` ici : c'est une boucle `asyncio`
séparée branchée sur le Fetcher RSS, livrée en Phase 13+ (out-of-scope V1).
Le `news_watcher` config est exposé via `config.news_watcher` pour que le
caller puisse l'instancier.
"""
from __future__ import annotations

import logging
import signal
from dataclasses import dataclass
from typing import Callable, Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from src.scheduler.loader import SchedulesConfig

log = logging.getLogger(__name__)


PipelineCallable = Callable[[], None]


@dataclass
class SchedulerHandle:
    """Wrapper minimal autour d'APScheduler pour un shutdown explicite."""

    scheduler: object
    config: SchedulesConfig

    def start(self, *, blocking: bool = False) -> None:
        if isinstance(self.scheduler, BlockingScheduler) and not blocking:
            raise RuntimeError(
                "BlockingScheduler requires blocking=True (méthode bloquante)"
            )
        self.scheduler.start()   # type: ignore[attr-defined]

    def shutdown(self, *, wait: bool = True) -> None:
        # `running` est présent sur tous les schedulers APScheduler ; si le
        # handle a été construit puis jamais start() — cas typique du
        # `--smoke` test — shutdown() lève `SchedulerNotRunningError`. On
        # évite le bruit en court-circuitant.
        running = bool(getattr(self.scheduler, "running", False))
        if not running:
            return
        try:
            self.scheduler.shutdown(wait=wait)   # type: ignore[attr-defined]
        except Exception:
            log.exception("scheduler_shutdown_failed")

    def job_ids(self) -> list[str]:
        return [j.id for j in self.scheduler.get_jobs()]   # type: ignore[attr-defined]


def _cron_to_trigger(cron: str) -> CronTrigger:
    """Convertit une expression cron 5-champs en `CronTrigger`.

    Supporte aussi les crons à 6 champs (seconde en position 0) — détection
    par nombre d'espaces.
    """
    parts = cron.split()
    if len(parts) == 5:
        return CronTrigger.from_crontab(cron)
    if len(parts) == 6:
        second, minute, hour, day, month, dow = parts
        return CronTrigger(
            second=second, minute=minute, hour=hour,
            day=day, month=month, day_of_week=dow,
        )
    raise ValueError(f"CFG_006 cron invalide (5 ou 6 champs attendus) : {cron!r}")


def build_scheduler(
    *,
    config: SchedulesConfig,
    pipelines: dict[str, PipelineCallable],
    blocking: bool = False,
    timezone: str = "UTC",
    misfire_grace_time: int = 300,
    coalesce: bool = True,
) -> SchedulerHandle:
    """Instancie le scheduler et enregistre les jobs.

    Args:
        config: sortie de `load_schedules()`.
        pipelines: mapping `name -> callable`. Tous les pipelines référencés
            dans `config.cycles + config.maintenance` DOIVENT être présents.
        blocking: True → BlockingScheduler (utile pour `python -m src.main`),
            False → BackgroundScheduler (utile intégré à FastAPI).
        timezone: fuseau horaire APScheduler. Par défaut UTC (align §15.1).
        misfire_grace_time: tolérance en secondes pour un missed-fire
            (RUN_004) avant de skip le job.
        coalesce: si plusieurs fires sont manqués, en exécuter un seul.
    """
    missing = sorted({j.pipeline for j in config.all_jobs} - pipelines.keys())
    if missing:
        raise KeyError(f"SCHED_001 pipelines manquants : {missing}")

    cls = BlockingScheduler if blocking else BackgroundScheduler
    scheduler = cls(timezone=timezone)

    for job in config.all_jobs:
        trigger = _cron_to_trigger(job.cron)
        scheduler.add_job(
            pipelines[job.pipeline],
            trigger=trigger,
            id=job.name,
            name=f"{job.kind}:{job.name}",
            replace_existing=True,
            misfire_grace_time=misfire_grace_time,
            coalesce=coalesce,
        )
        log.info(
            "scheduler_job_registered",
            extra={
                "name": job.name,
                "cron": job.cron,
                "pipeline": job.pipeline,
                "kind": job.kind,
            },
        )

    return SchedulerHandle(scheduler=scheduler, config=config)


def install_graceful_shutdown(handle: SchedulerHandle) -> None:
    """Branche SIGTERM/SIGINT → `handle.shutdown(wait=True)` (§17.2).

    À appeler depuis `src/main.py` après `build_scheduler`. Dans les tests
    on s'en passe — c'est un effet de bord globale sur le process.
    """
    def _handler(signum: int, _frame: Optional[object]) -> None:
        log.info("scheduler_signal_shutdown", extra={"signum": signum})
        handle.shutdown(wait=True)

    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)


__all__ = [
    "SchedulerHandle",
    "PipelineCallable",
    "build_scheduler",
    "install_graceful_shutdown",
]
