"""
tests/test_telegram_notifier.py — TelegramNotifier (§14.6).

Couvre :
- fail-safe : absence de bot_token → no-op + warning, pas d'exception
- send_message envoie le bon endpoint + parse_mode MarkdownV2
- send_proposal_card inclut le reply_markup inline_keyboard
- send_alert déduplique par code dans la fenêtre
- send_alert ignore les niveaux < alert_min_level
- send_cost_digest formate correctement un CostPanel
- MarkdownV2 escape sur les caractères spéciaux
- TelegramConfig.from_dict lit env vars
"""
from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import MagicMock

import httpx

from src.dashboards.cost_repo import (
    AgentCostRow,
    ApiSourceRow,
    CostAlert,
    CostPanel,
)
from src.telegram.notifier import TelegramConfig, TelegramNotifier, _md_escape


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _config(*, enabled: bool = True, **kwargs) -> TelegramConfig:
    return TelegramConfig(
        bot_token="fake_token" if enabled else None,
        chat_id="12345" if enabled else None,
        webhook_secret="secret",
        admin_user_id=42,
        api_base_url="https://tg.test",
        **kwargs,
    )


def _mock_http_ok() -> MagicMock:
    m = MagicMock(spec=httpx.Client)
    resp = MagicMock()
    resp.status_code = 200
    resp.text = "ok"
    m.post.return_value = resp
    return m


def _mock_http_fail(status: int = 500) -> MagicMock:
    m = MagicMock(spec=httpx.Client)
    resp = MagicMock()
    resp.status_code = status
    resp.text = "nope"
    m.post.return_value = resp
    return m


@dataclass
class _FakeRegime:
    macro: str = "risk_on"
    volatility: str = "mid"


@dataclass
class _FakeProposal:
    proposal_id: str = "tp_abc"
    asset: str = "RUI.PA"
    side: str = "long"
    strategy_id: str = "breakout_momentum"
    entry_price: float = 52.1234
    stop_price: float = 50.0
    tp_prices: tuple = (54.0, 56.0)
    rr: float = 2.5
    conviction: float = 0.85
    risk_pct: float = 0.01


# ---------------------------------------------------------------------------
# MarkdownV2 escape
# ---------------------------------------------------------------------------


def test_md_escape_special_chars():
    assert _md_escape("a_b*c") == r"a\_b\*c"
    assert _md_escape("price: $52.12") == r"price: $52\.12"
    assert _md_escape("hello") == "hello"


def test_md_escape_none():
    assert _md_escape(None) == ""


# ---------------------------------------------------------------------------
# Fail-safe (pas de token)
# ---------------------------------------------------------------------------


def test_notifier_disabled_returns_false(caplog):
    n = TelegramNotifier(config=_config(enabled=False))
    assert n.config.is_enabled is False
    assert n.send_cycle_summary([], _FakeRegime()) is False


def test_notifier_disabled_alert_noop():
    n = TelegramNotifier(config=_config(enabled=False))
    assert n.send_alert("boom", level="ERROR") is False


# ---------------------------------------------------------------------------
# send_cycle_summary
# ---------------------------------------------------------------------------


def test_send_cycle_summary_posts_to_api():
    http = _mock_http_ok()
    n = TelegramNotifier(config=_config(), http=http)
    ok = n.send_cycle_summary([_FakeProposal()], _FakeRegime())
    assert ok is True
    assert http.post.call_count == 1
    url = http.post.call_args[0][0]
    payload = http.post.call_args.kwargs["json"]
    assert url == "https://tg.test/botfake_token/sendMessage"
    assert payload["chat_id"] == "12345"
    assert payload["parse_mode"] == "MarkdownV2"
    assert "Cycle termin" in payload["text"]


def test_send_cycle_summary_truncates_to_800():
    http = _mock_http_ok()
    n = TelegramNotifier(config=_config(), http=http)
    many = [_FakeProposal() for _ in range(20)]
    n.send_cycle_summary(many, _FakeRegime())
    text = http.post.call_args.kwargs["json"]["text"]
    assert len(text) <= 800


# ---------------------------------------------------------------------------
# send_proposal_card
# ---------------------------------------------------------------------------


def test_send_proposal_card_inline_keyboard():
    http = _mock_http_ok()
    n = TelegramNotifier(config=_config(), http=http)
    n.send_proposal_card(_FakeProposal())
    payload = http.post.call_args.kwargs["json"]
    buttons = payload["reply_markup"]["inline_keyboard"][0]
    labels = [b["text"] for b in buttons]
    assert "✅ Valider" in labels
    assert "❌ Rejeter" in labels
    assert "ℹ️ Détails" in labels
    # callback_data contient le proposal_id
    cbs = [b["callback_data"] for b in buttons]
    assert any("validate:tp_abc" in c for c in cbs)
    assert any("reject:tp_abc" in c for c in cbs)


def test_send_proposal_card_details_toggle_off():
    http = _mock_http_ok()
    n = TelegramNotifier(
        config=_config(proposal_card_enable_details=False),
        http=http,
    )
    n.send_proposal_card(_FakeProposal())
    buttons = http.post.call_args.kwargs["json"]["reply_markup"]["inline_keyboard"][0]
    assert all("détails" not in b["text"].lower() for b in buttons)


# ---------------------------------------------------------------------------
# send_alert — dedup + min level
# ---------------------------------------------------------------------------


def test_send_alert_below_min_level_skipped():
    http = _mock_http_ok()
    n = TelegramNotifier(config=_config(alert_min_level="ERROR"), http=http)
    assert n.send_alert("warning_only", level="WARNING") is False
    assert http.post.call_count == 0


def test_send_alert_deduplicates_by_code():
    http = _mock_http_ok()
    n = TelegramNotifier(
        config=_config(alert_min_level="INFO", alert_dedup_window_s=60),
        http=http,
    )
    assert n.send_alert("boom 1", level="ERROR", code="CX_01") is True
    # 2e envoi immédiat avec le même code → dédupliqué
    assert n.send_alert("boom 2", level="ERROR", code="CX_01") is False
    assert http.post.call_count == 1


def test_send_alert_different_codes_both_sent():
    http = _mock_http_ok()
    n = TelegramNotifier(config=_config(alert_min_level="INFO"), http=http)
    n.send_alert("a", level="ERROR", code="A")
    n.send_alert("b", level="ERROR", code="B")
    assert http.post.call_count == 2


def test_send_alert_passes_silent_flag():
    http = _mock_http_ok()
    n = TelegramNotifier(config=_config(silent_alerts=True), http=http)
    n.send_alert("shh", level="ERROR")
    assert http.post.call_args.kwargs["json"].get("disable_notification") is True


# ---------------------------------------------------------------------------
# send_cost_digest
# ---------------------------------------------------------------------------


def _cost_panel() -> CostPanel:
    return CostPanel(
        tokens_today=8000,
        tokens_daily_budget=10000,
        cost_month_usd=5.23,
        cost_month_budget_usd=15.00,
        forecast_month_usd=8.10,
        by_agent=[
            AgentCostRow(agent="scan", calls_24h=10, tokens_in_24h=100,
                         tokens_out_24h=50, cost_24h_usd=0.12,
                         cost_month_usd=1.5, model="claude-sonnet-4-6",
                         pct_month_budget=30.0),
        ],
        by_model=[],
        by_api_source=[
            ApiSourceRow(source="stooq", kind="equity", calls_24h=50,
                         cache_hit_pct=80.0, latency_p50_ms=100,
                         latency_p95_ms=3000, error_rate_pct=15.0,
                         quota_used_pct=None, cost_24h_usd=0.0,
                         state="red"),
        ],
        trend_30d=[],
        top_consumers=[],
        pricing_last_updated="2026-01-01",
        alerts=[CostAlert(level="warning", code="x", message="y")],
        computed_at="2026-01-20T12:00:00Z",
        source_data_lag_seconds=30.0,
    )


def test_send_cost_digest_formats_panel():
    http = _mock_http_ok()
    n = TelegramNotifier(config=_config(), http=http)
    assert n.send_cost_digest(_cost_panel()) is True
    text = http.post.call_args.kwargs["json"]["text"]
    assert "Digest co" in text
    assert "scan" in text
    assert "stooq" in text           # API dégradée listée
    assert "8000" in text            # tokens_today


# ---------------------------------------------------------------------------
# send_daily_recap
# ---------------------------------------------------------------------------


@dataclass
class _PnL:
    total_pnl_pct: float = 1.23
    total_pnl_usd: float = 45.67
    n_trades: int = 3
    winrate: float = 0.66
    llm_cost_usd: float = 0.05


def test_send_daily_recap_includes_fields():
    http = _mock_http_ok()
    n = TelegramNotifier(config=_config(), http=http)
    n.send_daily_recap(_PnL())
    text = http.post.call_args.kwargs["json"]["text"]
    assert "Recap jour" in text
    # MarkdownV2 escape les points → "1\.23"
    assert r"1\.23" in text


# ---------------------------------------------------------------------------
# Erreurs réseau / API
# ---------------------------------------------------------------------------


def test_send_message_http_500_returns_false():
    http = _mock_http_fail(500)
    n = TelegramNotifier(config=_config(), http=http)
    assert n.send_cycle_summary([], _FakeRegime()) is False


def test_send_message_exception_returns_false():
    http = MagicMock(spec=httpx.Client)
    http.post.side_effect = httpx.ConnectError("no network")
    n = TelegramNotifier(config=_config(), http=http)
    assert n.send_cycle_summary([], _FakeRegime()) is False


# ---------------------------------------------------------------------------
# TelegramConfig.from_dict
# ---------------------------------------------------------------------------


def test_config_from_dict_reads_env(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok123")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "chat456")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "sec789")
    cfg = TelegramConfig.from_dict({
        "admin_user_id": 99,
        "parse_mode": "HTML",
        "rate_limit": {"max_messages_per_second": 5},
        "alerts": {"min_level": "CRITICAL"},
    })
    assert cfg.bot_token == "tok123"
    assert cfg.chat_id == "chat456"
    assert cfg.webhook_secret == "sec789"
    assert cfg.admin_user_id == 99
    assert cfg.parse_mode == "HTML"
    assert cfg.max_msg_per_sec == 5
    assert cfg.alert_min_level == "CRITICAL"
    assert cfg.is_enabled is True


def test_config_from_dict_missing_env_disables(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    cfg = TelegramConfig.from_dict({})
    assert cfg.is_enabled is False
