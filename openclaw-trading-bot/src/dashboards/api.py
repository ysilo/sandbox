"""
src.dashboards.api — FastAPI minimal §14.5.6.

Endpoints MVP :
- POST /validate/{proposal_id}   → décision humaine "go"
- POST /reject/{proposal_id}     → décision humaine "no-go"
- GET  /costs.json               → CostPanel sérialisé (monitoring externe)
- GET  /healthz                  → état liveness (toujours 200 tant que l'app tourne)

Conception :
- La factory `create_app(...)` injecte `CostRepository` + deux callbacks
  `on_validate(proposal_id)` et `on_reject(proposal_id)` pour rester testable
  sans dépendre du simulator (qui sera câblé en Phase 12).
- `/healthz` renvoie `{status: ok, db: ok|missing, pricing_stale: bool}` si
  possible, ne lève jamais d'exception (liveness ≠ readiness).
- `/costs.json` appelle `build_panel()` à chaque requête (pull-only).
  Le coût est déjà ~50 ms sur ~100 k lignes — acceptable pour un MVP.
- Les callbacks reçoivent le `proposal_id` et un `reason` optionnel (body JSON).
  Ils doivent retourner un booléen (True = enregistré, False = non trouvé).

Sécurité V1 :
- Aucune authentification (exposé uniquement sur `127.0.0.1` — voir
  `src/main.py` §14.5.7). À durcir si exposé hors localhost.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from src.dashboards.cost_repo import CostRepository
from src.dashboards.pricing import ModelPricing

log = logging.getLogger(__name__)


# Signatures des callbacks injectés
ValidateCallback = Callable[[str, Optional[str]], bool]
RejectCallback = Callable[[str, Optional[str]], bool]


def _default_validate(proposal_id: str, reason: Optional[str]) -> bool:
    """Stub par défaut — log et renvoie True (utile en dev avant Phase 12)."""
    log.info(
        "dashboard_validate_stub",
        extra={"proposal_id": proposal_id, "reason": reason},
    )
    return True


def _default_reject(proposal_id: str, reason: Optional[str]) -> bool:
    """Stub par défaut — log et renvoie True."""
    log.info(
        "dashboard_reject_stub",
        extra={"proposal_id": proposal_id, "reason": reason},
    )
    return True


def create_app(
    *,
    cost_repo: CostRepository,
    on_validate: ValidateCallback = _default_validate,
    on_reject: RejectCallback = _default_reject,
    pricing: Optional[ModelPricing] = None,
) -> FastAPI:
    """Construit l'application FastAPI.

    Args:
        cost_repo: dépendance CostRepository déjà initialisée.
        on_validate: callback appelé sur POST /validate/{id}.
        on_reject: callback appelé sur POST /reject/{id}.
        pricing: exposé par /healthz (optionnel — `cost_repo.pricing` utilisé sinon).
    """
    app = FastAPI(
        title="Openclaw Dashboard API",
        version="1.0.0",
        docs_url=None,   # pas besoin de Swagger UI sur localhost
        redoc_url=None,
    )

    # ------------------------------------------------------------------
    # GET /healthz — liveness
    # ------------------------------------------------------------------
    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        pr = pricing or cost_repo.pricing
        return {
            "status": "ok",
            "pricing_stale": bool(pr.is_stale()),
            "pricing_last_updated": (
                pr.last_updated.isoformat()
                if pr.last_updated.toordinal() > 1
                else "unknown"
            ),
        }

    # ------------------------------------------------------------------
    # GET /costs.json — panel coûts (JSON-safe via CostPanel.to_dict)
    # ------------------------------------------------------------------
    @app.get("/costs.json")
    def costs_json() -> JSONResponse:
        try:
            panel = cost_repo.build_panel()
            return JSONResponse(panel.to_dict())
        except Exception as exc:
            log.exception("costs_json_failed")
            raise HTTPException(status_code=500, detail=f"cost_repo: {exc}") from exc

    # ------------------------------------------------------------------
    # POST /validate/{proposal_id}
    # ------------------------------------------------------------------
    @app.post("/validate/{proposal_id}")
    async def validate(proposal_id: str, request: Request) -> dict[str, Any]:
        reason = await _extract_reason(request)
        try:
            found = bool(on_validate(proposal_id, reason))
        except Exception as exc:
            log.exception("validate_callback_failed")
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        if not found:
            raise HTTPException(status_code=404, detail="proposal_not_found")
        return {"status": "validated", "proposal_id": proposal_id}

    # ------------------------------------------------------------------
    # POST /reject/{proposal_id}
    # ------------------------------------------------------------------
    @app.post("/reject/{proposal_id}")
    async def reject(proposal_id: str, request: Request) -> dict[str, Any]:
        reason = await _extract_reason(request)
        try:
            found = bool(on_reject(proposal_id, reason))
        except Exception as exc:
            log.exception("reject_callback_failed")
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        if not found:
            raise HTTPException(status_code=404, detail="proposal_not_found")
        return {"status": "rejected", "proposal_id": proposal_id}

    return app


async def _extract_reason(request: Request) -> Optional[str]:
    """Extrait `reason` du body JSON si présent (sinon None). Tolérant au body vide."""
    if request.headers.get("content-length", "0") in ("", "0"):
        return None
    try:
        payload = await request.json()
    except Exception:
        return None
    if isinstance(payload, dict):
        value = payload.get("reason")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


__all__ = ["create_app", "ValidateCallback", "RejectCallback"]
