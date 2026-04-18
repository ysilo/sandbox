"""
src.telegram.webhook — FastAPI sub-app pour les commandes Telegram (§14.6).

Sécurité :
- Header `X-Telegram-Bot-Api-Secret-Token` comparé à `config.webhook_secret`.
  Sans secret OU mismatch → 403.
- Dispatcher : seules les commandes explicitement enregistrées sont
  traitées. Tout le reste retourne `{ok: true, ignored: true}` pour
  préserver le contrat de Telegram (qui re-pousse en cas de 5xx).
- Commandes mutables (`/validate`, `/reject`, `/kill`, `/resume`,
  `/rollback`) restent réservées à l'`admin_user_id`. Les autres user_ids
  reçoivent `access_denied`.

API :
- `create_webhook_router(notifier, handlers)` retourne un `APIRouter`
  à monter sur l'app principale (ex : `app.include_router(router)`).
- Chaque `handler` est une callable `(args: list[str], user_id: int) -> str`
  qui retourne le texte à renvoyer à Telegram en réponse.
"""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Optional, Union

from fastapi import APIRouter, Header, HTTPException, Request

from src.telegram.notifier import TelegramNotifier

log = logging.getLogger(__name__)


CommandHandler = Callable[[list[str], int], Union[str, Awaitable[str]]]


# ---------------------------------------------------------------------------
# Commandes mutables — requièrent admin_user_id
# ---------------------------------------------------------------------------
_ADMIN_ONLY_COMMANDS: set[str] = {
    "/validate", "/reject", "/kill", "/resume", "/rollback", "/regime",
}


def create_webhook_router(
    *,
    notifier: TelegramNotifier,
    handlers: Optional[dict[str, CommandHandler]] = None,
    path: str = "/telegram/webhook",
) -> APIRouter:
    """Construit un `APIRouter` pour le webhook Telegram.

    Args:
        notifier: `TelegramNotifier` dont la config porte `webhook_secret`
            et `admin_user_id`.
        handlers: mapping `"/commande" -> callable(args, user_id) -> str`.
            Si absent, le webhook répond `{ok: true, ignored: true}`.
        path: chemin du webhook (défaut : `/telegram/webhook`).
    """
    router = APIRouter()
    handlers = handlers or {}

    @router.post(path)
    async def telegram_webhook(
        request: Request,
        x_telegram_bot_api_secret_token: Optional[str] = Header(default=None),
    ) -> dict[str, Any]:
        # 1. Vérification secret
        expected = notifier.config.webhook_secret
        if not expected:
            log.warning("telegram_webhook_no_secret_configured")
            raise HTTPException(status_code=403, detail="webhook_secret_not_configured")
        if x_telegram_bot_api_secret_token != expected:
            log.warning("telegram_webhook_bad_secret")
            raise HTTPException(status_code=403, detail="bad_secret")

        # 2. Parse update
        try:
            update = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="invalid_json")

        command, args, user_id = _parse_command(update)
        if command is None:
            return {"ok": True, "ignored": True}

        # 3. Contrôle admin pour les commandes mutables
        if command in _ADMIN_ONLY_COMMANDS:
            admin_id = notifier.config.admin_user_id
            if admin_id and user_id != admin_id:
                log.warning(
                    "telegram_admin_access_denied",
                    extra={"user_id": user_id, "command": command},
                )
                notifier.send_alert(
                    f"access_denied user_id={user_id} cmd={command}",
                    level="WARNING",
                    code=f"tg_access_denied:{user_id}",
                )
                return {"ok": True, "access_denied": True}

        # 4. Dispatch
        handler = handlers.get(command)
        if handler is None:
            return {"ok": True, "unknown_command": command}

        try:
            result = handler(args, user_id)
            if hasattr(result, "__await__"):  # support async handlers
                result = await result  # type: ignore[misc]
            reply = str(result) if result is not None else ""
        except Exception as exc:
            log.exception("telegram_handler_failed", extra={"command": command})
            reply = f"❌ Erreur commande {command} : {exc}"

        if reply:
            notifier._send_message(reply[:4000])
        return {"ok": True, "command": command}

    return router


# ---------------------------------------------------------------------------
# Parsing helper
# ---------------------------------------------------------------------------


def _parse_command(update: dict[str, Any]) -> tuple[Optional[str], list[str], int]:
    """Extrait `(command, args, user_id)` depuis un Telegram Update.

    Retourne `(None, [], 0)` si l'update ne contient pas de commande
    /slash — Telegram pousse aussi les messages texte, callbacks, etc.
    """
    msg = update.get("message") or update.get("edited_message") or {}
    text = (msg.get("text") or "").strip()
    user_id = int(((msg.get("from") or {}).get("id")) or 0)
    if not text or not text.startswith("/"):
        return None, [], user_id
    parts = text.split()
    command = parts[0].split("@")[0]  # gère "/status@mybot"
    args = parts[1:]
    return command, args, user_id


__all__ = ["create_webhook_router", "CommandHandler"]
