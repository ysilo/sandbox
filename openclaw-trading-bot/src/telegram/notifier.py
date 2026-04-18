"""
src.telegram.notifier — TelegramNotifier (§14.6).

MVP fonctionnel :
- Appels HTTP via `httpx` vers `api.telegram.org` (configurable pour tests).
- Fail-safe : si `TELEGRAM_BOT_TOKEN` est absent, les envois deviennent des
  no-ops loggés en WARNING. La pipeline ne doit JAMAIS crasher parce qu'on
  n'arrive pas à notifier.
- 5 méthodes publiques : `send_cycle_summary`, `send_proposal_card`,
  `send_alert`, `send_daily_recap`, `send_cost_digest`.
- Déduplication : `send_alert` ignore les codes identiques dans la fenêtre
  `alerts.deduplicate_window_s` (§7.5.5).
- Les escapes MarkdownV2 sont appliqués aux champs dynamiques (noms
  d'actifs, raisons, nombres formatés). Cf. `_md_escape`.
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

from src.dashboards.cost_repo import CostPanel

log = logging.getLogger(__name__)


# Caractères à échapper en MarkdownV2 (source : Telegram docs)
_MDV2_SPECIALS = r"_*[]()~`>#+-=|{}.!\\"


def _md_escape(text: Any) -> str:
    """Échappe les caractères réservés MarkdownV2 pour un champ dynamique."""
    s = str(text) if text is not None else ""
    out = []
    for ch in s:
        if ch in _MDV2_SPECIALS:
            out.append("\\" + ch)
        else:
            out.append(ch)
    return "".join(out)


# ---------------------------------------------------------------------------
# Config + état
# ---------------------------------------------------------------------------


@dataclass
class TelegramConfig:
    """Configuration chargée depuis `config/telegram.yaml` + env vars."""

    bot_token: Optional[str]
    chat_id: Optional[str]
    webhook_secret: Optional[str] = None
    admin_user_id: int = 0
    api_base_url: str = "https://api.telegram.org"
    parse_mode: str = "MarkdownV2"
    disable_web_page_preview: bool = True
    silent_alerts: bool = False
    max_msg_per_sec: int = 10
    retry_on_429_s: int = 5
    alert_min_level: str = "ERROR"
    alert_dedup_window_s: int = 600
    proposal_card_expiration_min: int = 120
    proposal_card_enable_details: bool = True

    @property
    def is_enabled(self) -> bool:
        return bool(self.bot_token) and bool(self.chat_id)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "TelegramConfig":
        env_map = raw.get("env", {}) or {}
        env_token = env_map.get("bot_token", "TELEGRAM_BOT_TOKEN")
        env_chat = env_map.get("chat_id", "TELEGRAM_CHAT_ID")
        env_secret = env_map.get("webhook_secret", "TELEGRAM_WEBHOOK_SECRET")

        rate = raw.get("rate_limit", {}) or {}
        alerts = raw.get("alerts", {}) or {}
        card = raw.get("proposal_card", {}) or {}

        return cls(
            bot_token=os.environ.get(env_token),
            chat_id=os.environ.get(env_chat),
            webhook_secret=os.environ.get(env_secret),
            admin_user_id=int(raw.get("admin_user_id", 0) or 0),
            api_base_url=str(raw.get("api_base_url", "https://api.telegram.org")),
            parse_mode=str(raw.get("parse_mode", "MarkdownV2")),
            disable_web_page_preview=bool(raw.get("disable_web_page_preview", True)),
            silent_alerts=bool(raw.get("silent_alerts", False)),
            max_msg_per_sec=int(rate.get("max_messages_per_second", 10)),
            retry_on_429_s=int(rate.get("retry_on_429_seconds", 5)),
            alert_min_level=str(alerts.get("min_level", "ERROR")).upper(),
            alert_dedup_window_s=int(alerts.get("deduplicate_window_s", 600)),
            proposal_card_expiration_min=int(card.get("expiration_minutes", 120)),
            proposal_card_enable_details=bool(card.get("enable_details_button", True)),
        )


# ---------------------------------------------------------------------------
# Notifier
# ---------------------------------------------------------------------------


_LEVEL_ORDER = {"DEBUG": 0, "INFO": 1, "WARNING": 2, "ERROR": 3, "CRITICAL": 4}


@dataclass
class TelegramNotifier:
    """Envoie des messages sur Telegram en fail-safe."""

    config: TelegramConfig
    http: Optional[httpx.Client] = None            # injectable pour tests
    _last_alert: dict[str, float] = field(default_factory=dict)

    # ------------------------------------------------------------------
    # Factories
    # ------------------------------------------------------------------

    @classmethod
    def from_yaml(cls, *, http: Optional[httpx.Client] = None) -> "TelegramNotifier":
        """Charge `config/telegram.yaml` + env et instancie le notifier."""
        from src.utils.config_loader import load_yaml
        cfg = TelegramConfig.from_dict(load_yaml("telegram.yaml"))
        return cls(config=cfg, http=http)

    # ------------------------------------------------------------------
    # API publique §14.6
    # ------------------------------------------------------------------

    def send_cycle_summary(self, proposals: list[Any], regime: Any) -> bool:
        """Message court (≤ 800 chars) envoyé à chaque fin de cycle."""
        n = len(proposals)
        head = f"🔁 *Cycle terminé* — {_md_escape(n)} proposition\\(s\\)"
        body_lines = [
            f"• Régime : {_md_escape(regime.macro)} · vol {_md_escape(regime.volatility)}",
        ]
        for p in proposals[:5]:
            side_emoji = "📈" if getattr(p, "side", "") == "long" else "📉"
            body_lines.append(
                f"{side_emoji} {_md_escape(p.asset)} — "
                f"{_md_escape(p.strategy_id)} — "
                f"R/R {_md_escape(f'{p.rr:.1f}')} · "
                f"conv {_md_escape(f'{p.conviction:.2f}')}"
            )
        if n > 5:
            body_lines.append(f"\\.\\.\\. et {_md_escape(n - 5)} autres")
        text = head + "\n" + "\n".join(body_lines)
        return self._send_message(text[:800])

    def send_proposal_card(self, proposal: Any) -> bool:
        """Carte avec boutons inline [Valider] [Rejeter] [Détails]."""
        asset = _md_escape(proposal.asset)
        side = _md_escape(proposal.side.upper())
        text = (
            f"*{asset}* · {side} · `{_md_escape(proposal.strategy_id)}`\n"
            f"Entrée : `{_md_escape(f'{proposal.entry_price:.4f}')}` · "
            f"Stop : `{_md_escape(f'{proposal.stop_price:.4f}')}`\n"
            f"TP : {_md_escape(' / '.join(f'{x:.4f}' for x in proposal.tp_prices))}\n"
            f"R/R : {_md_escape(f'{proposal.rr:.1f}')} · "
            f"Conviction : {_md_escape(f'{proposal.conviction:.2f}')} · "
            f"Size : {_md_escape(f'{proposal.risk_pct*100:.1f}')} %"
        )
        buttons = [
            [
                {"text": "✅ Valider", "callback_data": f"validate:{proposal.proposal_id}"},
                {"text": "❌ Rejeter", "callback_data": f"reject:{proposal.proposal_id}"},
            ]
        ]
        if self.config.proposal_card_enable_details:
            buttons[0].append(
                {"text": "ℹ️ Détails", "callback_data": f"details:{proposal.proposal_id}"}
            )
        return self._send_message(text, reply_markup={"inline_keyboard": buttons})

    def send_alert(self, message: str, *, level: str = "warning",
                   code: Optional[str] = None) -> bool:
        """Alerte — dédupliquée par `code` dans `alert_dedup_window_s`.

        `level` est insensible à la casse. Les niveaux en dessous de
        `alert_min_level` sont ignorés (§7.5.5).
        """
        lvl = level.upper()
        if _LEVEL_ORDER.get(lvl, 0) < _LEVEL_ORDER.get(self.config.alert_min_level, 3):
            log.debug("telegram_alert_below_min", extra={"level": lvl})
            return False
        if code:
            last = self._last_alert.get(code)
            now = time.monotonic()
            if last is not None and (now - last) < self.config.alert_dedup_window_s:
                log.debug("telegram_alert_deduped", extra={"code": code})
                return False
            self._last_alert[code] = now
        icon = {"CRITICAL": "🚨", "ERROR": "❗", "WARNING": "⚠️", "INFO": "ℹ️"}.get(lvl, "•")
        text = f"{icon} *{_md_escape(lvl)}* — {_md_escape(message)}"
        return self._send_message(text, disable_notification=self.config.silent_alerts)

    def send_daily_recap(self, pnl: Any) -> bool:
        """Récap P&L du jour + coûts LLM (§14.6)."""
        lines = ["🗓️ *Recap jour*"]
        for attr, label in [
            ("total_pnl_pct", "P&L total"),
            ("total_pnl_usd", "P&L USD"),
            ("n_trades", "Trades"),
            ("winrate", "Winrate"),
            ("llm_cost_usd", "LLM"),
        ]:
            if hasattr(pnl, attr):
                v = getattr(pnl, attr)
                lines.append(f"• {_md_escape(label)} : `{_md_escape(_fmt_num(v))}`")
        return self._send_message("\n".join(lines))

    def send_cost_digest(self, panel: CostPanel) -> bool:
        """Digest coûts — tokens jour, coût mois, top 3, APIs dégradées."""
        lines = [
            "💰 *Digest coûts*",
            f"• Tokens jour : `{_md_escape(panel.tokens_today)}` "
            f"/ `{_md_escape(panel.tokens_daily_budget)}`",
            f"• Coût mois : `${_md_escape(f'{panel.cost_month_usd:.2f}')}` "
            f"/ `${_md_escape(f'{panel.cost_month_budget_usd:.2f}')}`",
            f"• Forecast : `${_md_escape(f'{panel.forecast_month_usd:.2f}')}`",
        ]
        if panel.by_agent:
            top3 = panel.by_agent[:3]
            lines.append("*Top agents 24h* :")
            for r in top3:
                lines.append(
                    f"• `{_md_escape(r.agent)}` — `${_md_escape(f'{r.cost_24h_usd:.4f}')}`"
                )
        bad_apis = [a for a in panel.by_api_source if a.state != "green"]
        if bad_apis:
            lines.append("*APIs dégradées* :")
            for a in bad_apis:
                emoji = "🟠" if a.state == "amber" else "🔴"
                lines.append(
                    f"{emoji} `{_md_escape(a.source)}` — err "
                    f"`{_md_escape(f'{a.error_rate_pct:.1f}')}%` · "
                    f"p95 `{_md_escape(a.latency_p95_ms)}ms`"
                )
        return self._send_message("\n".join(lines))

    # ------------------------------------------------------------------
    # Plomberie interne
    # ------------------------------------------------------------------

    def _send_message(self, text: str, *, reply_markup: Optional[dict[str, Any]] = None,
                      disable_notification: bool = False) -> bool:
        """POST /sendMessage — renvoie True si succès, False si fail-safe."""
        if not self.config.is_enabled:
            log.warning("telegram_disabled_noop")
            return False
        url = f"{self.config.api_base_url}/bot{self.config.bot_token}/sendMessage"
        payload: dict[str, Any] = {
            "chat_id": self.config.chat_id,
            "text": text,
            "parse_mode": self.config.parse_mode,
            "disable_web_page_preview": self.config.disable_web_page_preview,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        if disable_notification:
            payload["disable_notification"] = True
        try:
            client = self.http or httpx.Client(timeout=10.0)
            response = client.post(url, json=payload)
            if response.status_code == 200:
                return True
            log.warning(
                "telegram_send_failed",
                extra={"status": response.status_code, "body": response.text[:200]},
            )
            return False
        except Exception as exc:
            log.warning("telegram_send_exception", extra={"error": str(exc)})
            return False


def _fmt_num(v: Any) -> str:
    """Formate un nombre simplement pour Telegram."""
    try:
        if isinstance(v, float):
            return f"{v:.2f}"
        return str(v)
    except Exception:
        return "?"


__all__ = ["TelegramNotifier", "TelegramConfig"]
