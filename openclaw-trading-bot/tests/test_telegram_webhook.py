"""
tests/test_telegram_webhook.py — FastAPI webhook Telegram (§14.6).

Couvre :
- Secret manquant → 403
- Secret faux → 403
- Secret correct + commande inconnue → 200 ignored/unknown
- Commande mutable (/validate) sans admin_user_id → access_denied
- Commande mutable avec admin_user_id → handler appelé
- Commande publique (/status) sans user_id admin → handler appelé
- Parse correct des args multiples
"""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.telegram.notifier import TelegramConfig, TelegramNotifier
from src.telegram.webhook import _parse_command, create_webhook_router


ADMIN_ID = 42


@pytest.fixture
def notifier():
    http = MagicMock(spec=httpx.Client)
    resp = MagicMock(status_code=200, text="ok")
    http.post.return_value = resp
    cfg = TelegramConfig(
        bot_token="tok",
        chat_id="chat",
        webhook_secret="SECRET",
        admin_user_id=ADMIN_ID,
        api_base_url="https://tg.test",
    )
    return TelegramNotifier(config=cfg, http=http)


def _make_client(notifier: TelegramNotifier, handlers: dict):
    app = FastAPI()
    router = create_webhook_router(notifier=notifier, handlers=handlers)
    app.include_router(router)
    return TestClient(app)


def _update(text: str, *, user_id: int = 100) -> dict[str, Any]:
    return {
        "update_id": 1,
        "message": {
            "message_id": 1,
            "date": 0,
            "chat": {"id": int(user_id), "type": "private"},
            "from": {"id": int(user_id), "is_bot": False, "first_name": "u"},
            "text": text,
        },
    }


# ---------------------------------------------------------------------------
# _parse_command
# ---------------------------------------------------------------------------


def test_parse_command_simple():
    cmd, args, uid = _parse_command(_update("/status"))
    assert cmd == "/status"
    assert args == []
    assert uid == 100


def test_parse_command_with_args():
    cmd, args, uid = _parse_command(_update("/validate tp_abc123 urgent"))
    assert cmd == "/validate"
    assert args == ["tp_abc123", "urgent"]


def test_parse_command_strips_bot_suffix():
    cmd, _, _ = _parse_command(_update("/status@openclaw_bot"))
    assert cmd == "/status"


def test_parse_command_no_text():
    cmd, args, uid = _parse_command({"message": {"from": {"id": 5}}})
    assert cmd is None
    assert uid == 5


def test_parse_command_non_command_text():
    cmd, _, _ = _parse_command(_update("bonjour"))
    assert cmd is None


# ---------------------------------------------------------------------------
# Sécurité webhook
# ---------------------------------------------------------------------------


def test_webhook_missing_secret_header_403(notifier):
    client = _make_client(notifier, {})
    r = client.post("/telegram/webhook", json=_update("/status"))
    assert r.status_code == 403


def test_webhook_wrong_secret_403(notifier):
    client = _make_client(notifier, {})
    r = client.post(
        "/telegram/webhook",
        json=_update("/status"),
        headers={"X-Telegram-Bot-Api-Secret-Token": "WRONG"},
    )
    assert r.status_code == 403


def test_webhook_no_secret_configured_403(notifier):
    notifier.config.webhook_secret = None   # type: ignore[misc]
    client = _make_client(notifier, {})
    r = client.post(
        "/telegram/webhook",
        json=_update("/status"),
        headers={"X-Telegram-Bot-Api-Secret-Token": "anything"},
    )
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# Dispatching
# ---------------------------------------------------------------------------


def test_webhook_unknown_command_200(notifier):
    client = _make_client(notifier, {})
    r = client.post(
        "/telegram/webhook",
        json=_update("/nope"),
        headers={"X-Telegram-Bot-Api-Secret-Token": "SECRET"},
    )
    assert r.status_code == 200
    assert r.json().get("unknown_command") == "/nope"


def test_webhook_non_command_message_ignored(notifier):
    client = _make_client(notifier, {})
    r = client.post(
        "/telegram/webhook",
        json=_update("bonjour"),
        headers={"X-Telegram-Bot-Api-Secret-Token": "SECRET"},
    )
    assert r.status_code == 200
    assert r.json().get("ignored") is True


def test_webhook_public_command_dispatched(notifier):
    calls: list[tuple] = []

    def h_status(args, user_id):
        calls.append(("status", args, user_id))
        return "ok"

    client = _make_client(notifier, {"/status": h_status})
    r = client.post(
        "/telegram/webhook",
        json=_update("/status"),
        headers={"X-Telegram-Bot-Api-Secret-Token": "SECRET"},
    )
    assert r.status_code == 200
    assert r.json() == {"ok": True, "command": "/status"}
    assert calls and calls[0][0] == "status"


def test_webhook_admin_command_denied_for_non_admin(notifier):
    calls: list[tuple] = []

    def h_validate(args, user_id):
        calls.append(("validate", args, user_id))
        return "should not happen"

    client = _make_client(notifier, {"/validate": h_validate})
    r = client.post(
        "/telegram/webhook",
        json=_update("/validate tp_abc", user_id=999),
        headers={"X-Telegram-Bot-Api-Secret-Token": "SECRET"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body.get("access_denied") is True
    assert calls == []


def test_webhook_admin_command_allowed_for_admin(notifier):
    calls: list[tuple] = []

    def h_validate(args, user_id):
        calls.append(("validate", args, user_id))
        return "✓ validé"

    client = _make_client(notifier, {"/validate": h_validate})
    r = client.post(
        "/telegram/webhook",
        json=_update("/validate tp_abc", user_id=ADMIN_ID),
        headers={"X-Telegram-Bot-Api-Secret-Token": "SECRET"},
    )
    assert r.status_code == 200
    assert r.json() == {"ok": True, "command": "/validate"}
    assert calls == [("validate", ["tp_abc"], ADMIN_ID)]


def test_webhook_handler_exception_returns_200_with_error_text(notifier):
    def h_crash(args, user_id):
        raise RuntimeError("handler_broken")

    client = _make_client(notifier, {"/status": h_crash})
    r = client.post(
        "/telegram/webhook",
        json=_update("/status"),
        headers={"X-Telegram-Bot-Api-Secret-Token": "SECRET"},
    )
    # Webhook ne doit PAS retourner 5xx (sinon Telegram re-push)
    assert r.status_code == 200
