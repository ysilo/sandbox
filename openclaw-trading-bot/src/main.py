"""
src.main — point d'entrée runtime du bot (§15.2, Phase 14).

Deux modes exposés via argparse :

- ``--smoke`` : valide le câblage (imports, init SQLite, FastAPI /healthz,
  enumération des jobs schedulés) puis exit 0. Utilisé par le CI / la
  validation Docker post-build pour détecter une régression côté wiring
  avant même qu'un cycle ne tourne.

- ``--serve`` : démarre FastAPI (dashboard + webhook Telegram) avec
  ``uvicorn`` sur ``--port``. Le scheduler APScheduler est démarré en
  mode **background** à l'intérieur du process FastAPI via le lifespan
  handler. SIGTERM déclenche un shutdown gracieux (APScheduler d'abord,
  puis l'app uvicorn).

Les pipelines câblés ici sont des **stubs log-only** pour la Phase 14 MVP :
chaque job logge ``{pipeline_name}_tick``. Le wiring réel (avec
``Orchestrator`` + dépendances DB / HTTP réelles) sera fait au moment de
la mise en prod — toute l'infra est prête, seul le bootstrap concret des
Protocols reste à écrire. Le dashboard et le webhook Telegram sont, eux,
pleinement opérationnels.
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Callable

from src.dashboards.api import create_app
from src.dashboards.cost_repo import CostRepository
from src.dashboards.pricing import LLMLimits, ModelPricing
from src.memory.db import init_db
from src.scheduler.loader import load_schedules
from src.scheduler.runner import build_scheduler, SchedulerHandle
from src.utils.logging_utils import configure as configure_logging

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers — stub pipelines pour MVP Phase 14
# ---------------------------------------------------------------------------


def _stub_pipeline(name: str) -> Callable[[], None]:
    def _tick() -> None:
        log.info("pipeline_tick", extra={"pipeline": name})
    _tick.__name__ = f"pipeline_{name}"
    return _tick


# Couvre tous les pipelines référencés dans ``config/schedules.yaml``.
# Ajouter ici le wiring réel quand l'Orchestrator est prêt à être invoqué
# depuis un job (Phase 15+).
_STUB_PIPELINES: dict[str, Callable[[], None]] = {
    name: _stub_pipeline(name)
    for name in (
        "full_cycle",
        "full_cycle_with_journal",
        "crypto_quick_scan",
        "focused_cycle",
        "update_open_trades",
        "memory_consolidate",
        "self_improve_weekly",
        "architecture_review_monthly",
        "purge_telemetry",
    )
}


# ---------------------------------------------------------------------------
# Bootstrap commun (smoke + serve)
# ---------------------------------------------------------------------------


def _bootstrap_components(data_dir: Path) -> tuple[CostRepository, SchedulerHandle]:
    """Initialise les composants partagés par ``--smoke`` et ``--serve``.

    - ``CostRepository`` pointant sur ``{data_dir}/memory.db`` (créé si absent,
      migrations idempotentes).
    - ``SchedulerHandle`` avec les jobs chargés depuis ``config/schedules.yaml``
      et les pipelines stub. Non démarré — le caller décide.
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    db_path = data_dir / "memory.db"

    # init_db crée toutes les tables (llm_usage, api_usage, cycles, trades,
    # patches, rollbacks, lessons) de manière idempotente.
    db = init_db(str(db_path))

    try:
        pricing = ModelPricing.load()
    except Exception:
        log.warning("pricing_load_failed_fallback_empty")
        from datetime import date
        pricing = ModelPricing(rates={}, last_updated=date.min)

    cost_repo = CostRepository(db, pricing, LLMLimits())

    schedules = load_schedules()  # lit config/schedules.yaml
    handle = build_scheduler(
        config=schedules,
        pipelines=_STUB_PIPELINES,
        blocking=False,
    )
    return cost_repo, handle


# ---------------------------------------------------------------------------
# Mode --smoke : validation offline, exit 0/1
# ---------------------------------------------------------------------------


def run_smoke(*, data_dir: Path) -> int:
    """Valide le wiring de bout en bout, sans réseau ni long-running process.

    Étapes :
    1. Import de tous les modules principaux (via les imports top-level).
    2. Bootstrap ``CostRepository`` (init SQLite) + ``SchedulerHandle``
       (parsing YAML + enumération des jobs).
    3. Construction de l'app FastAPI.
    4. Appel direct de ``/healthz`` via ``TestClient`` en mémoire.
    5. Enumération des job IDs pour vérifier qu'ils correspondent au YAML.

    Exit code 0 si toutes les étapes passent, 1 sinon.
    """
    try:
        log.info("smoke_start", extra={"data_dir": str(data_dir)})
        cost_repo, handle = _bootstrap_components(data_dir)
        log.info("smoke_bootstrap_ok", extra={"jobs": handle.job_ids()})

        app = create_app(cost_repo=cost_repo)

        # TestClient démarre l'app en mémoire sans ouvrir de socket.
        from fastapi.testclient import TestClient

        with TestClient(app) as client:
            resp = client.get("/healthz")
            if resp.status_code != 200:
                log.error(
                    "smoke_healthz_failed",
                    extra={"status": resp.status_code, "body": resp.text},
                )
                return 1
            body = resp.json()
            if body.get("status") != "ok":
                log.error("smoke_healthz_bad_body", extra={"body": body})
                return 1
            log.info("smoke_healthz_ok", extra={"body": body})

        handle.shutdown(wait=False)
        log.info("smoke_success")
        return 0
    except Exception:
        log.exception("smoke_failed")
        return 1


# ---------------------------------------------------------------------------
# Mode --serve : FastAPI + scheduler background, SIGTERM → shutdown propre
# ---------------------------------------------------------------------------


def _build_serve_app(cost_repo: CostRepository, handle: SchedulerHandle) -> Any:
    """Monte FastAPI et branche le scheduler sur le lifespan uvicorn."""
    @asynccontextmanager
    async def lifespan(_app: Any):
        log.info("app_startup", extra={"jobs": handle.job_ids()})
        handle.start(blocking=False)
        try:
            yield
        finally:
            log.info("app_shutdown")
            handle.shutdown(wait=True)

    app = create_app(cost_repo=cost_repo)
    # FastAPI >= 0.110 : router.lifespan_context est la surface officielle.
    app.router.lifespan_context = lifespan
    return app


def run_serve(*, host: str, port: int, data_dir: Path) -> int:
    """Sert dashboard + webhook + scheduler jusqu'à SIGTERM."""
    import uvicorn

    cost_repo, handle = _bootstrap_components(data_dir)
    app = _build_serve_app(cost_repo, handle)

    # SIGTERM → uvicorn gère nativement, mais on s'assure que le handle
    # du scheduler sort proprement même en cas d'exception cours de route.
    def _sigterm(signum: int, _frame: Any) -> None:
        log.info("sigterm_received", extra={"signum": signum})
        # uvicorn installera son propre handler, celui-ci est un filet de
        # sécurité pour le scheduler si uvicorn ne ferme pas le lifespan.
        try:
            handle.shutdown(wait=False)
        except Exception:
            log.exception("sigterm_scheduler_shutdown_failed")

    signal.signal(signal.SIGTERM, _sigterm)

    log.info("serve_start", extra={"host": host, "port": port})
    uvicorn.run(
        app,
        host=host,
        port=port,
        log_config=None,   # on garde notre JSON logger
        access_log=False,
    )
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="openclaw",
        description="Openclaw trading bot — entry point.",
    )
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--smoke",
        action="store_true",
        help="Valide le câblage (imports, SQLite, FastAPI /healthz, jobs) et exit.",
    )
    mode.add_argument(
        "--serve",
        action="store_true",
        help="Démarre FastAPI + APScheduler sur --host:--port (long-running).",
    )
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("PORT", "8080")),
    )
    p.add_argument(
        "--data-dir",
        type=Path,
        default=Path(os.environ.get("DATA_DIR", "data")),
    )
    p.add_argument(
        "--log-dir",
        type=Path,
        default=Path(os.environ.get("LOG_DIR", "data/logs")),
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    args.log_dir.mkdir(parents=True, exist_ok=True)
    configure_logging(log_dir=args.log_dir)

    if args.smoke:
        return run_smoke(data_dir=args.data_dir)
    if args.serve:
        return run_serve(host=args.host, port=args.port, data_dir=args.data_dir)
    return 2   # argparse empêche ce cas, filet de sécurité


if __name__ == "__main__":
    sys.exit(main())
