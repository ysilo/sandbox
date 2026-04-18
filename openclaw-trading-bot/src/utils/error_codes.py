"""
src.utils.error_codes — taxonomie d'erreurs actionnables (§2.6, §7.5.2).

Six familles couvrent tout ce qui peut casser. Chaque code a :
- un préfixe stable (CFG / NET / DATA / LLM / RISK / RUN)
- un numéro à 3 chiffres
- une `default_remediation` française courte et actionnable
- un `severity` par défaut (WARNING / ERROR / CRITICAL)

Utilisation :
    from src.utils.error_codes import EC
    log.error("source_unreachable", error_code=EC.NET_002.code, ...)

ou via helper du logger :
    log.error("source_unreachable", ec=EC.NET_002, asset="RUI.PA", ...)
    # → remediation auto-injectée si l'appelant n'en fournit pas

Règle d'or §7.5.1 : `remediation` est obligatoire à partir du niveau WARNING.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


Severity = str  # "WARNING" | "ERROR" | "CRITICAL"


@dataclass(frozen=True)
class _CodeSpec:
    code: str
    short: str
    default_remediation: str
    severity: Severity = "ERROR"


class EC(Enum):
    # -------------------------------------------------------------------
    # CFG — Configuration (bloquant au démarrage le plus souvent)
    # -------------------------------------------------------------------
    CFG_001 = _CodeSpec(
        code="CFG_001",
        short="env_var_missing",
        default_remediation=(
            "Ajouter la variable manquante dans .env (ANTHROPIC_API_KEY / TELEGRAM_BOT_TOKEN / "
            "TELEGRAM_CHAT_ID) puis relancer. Voir §17.6 du doc d'archi."
        ),
        severity="CRITICAL",
    )
    CFG_002 = _CodeSpec(
        code="CFG_002",
        short="yaml_invalid",
        default_remediation=(
            "Fichier YAML manquant, mal formé, ou qui ne valide pas son schéma Pydantic. "
            "Relire la section §3.1 du doc et vérifier les clés/valeurs."
        ),
        severity="CRITICAL",
    )
    CFG_003 = _CodeSpec(
        code="CFG_003",
        short="value_out_of_range",
        default_remediation=(
            "Valeur hors des bornes autorisées par le schéma (ex: max_risk_per_trade_pct_equity > 5). "
            "Consulter les contraintes Pydantic dans src/utils/config_loader.py."
        ),
        severity="CRITICAL",
    )

    # -------------------------------------------------------------------
    # NET — Réseau
    # -------------------------------------------------------------------
    NET_001 = _CodeSpec(
        code="NET_001",
        short="timeout",
        default_remediation=(
            "Timeout HTTP — retry automatique avec backoff. Si > 5 min, vérifier la connectivité "
            "hôte (curl -I <url>) et l'état des DNS."
        ),
    )
    NET_002 = _CodeSpec(
        code="NET_002",
        short="connection_refused",
        default_remediation=(
            "Connection refused — service distant probablement down. Retry + fallback auto; "
            "si le fallback échoue aussi, cycle fail-closed (§2.2)."
        ),
    )
    NET_003 = _CodeSpec(
        code="NET_003",
        short="dns_fail",
        default_remediation=(
            "Résolution DNS impossible. Vérifier /etc/resolv.conf côté host ou l'état du "
            "réseau Docker (docker network inspect)."
        ),
    )
    NET_004 = _CodeSpec(
        code="NET_004",
        short="tls_error",
        default_remediation=(
            "Erreur TLS — certificat expiré ou chaîne incomplète. Mettre à jour ca-certificates "
            "dans l'image Docker."
        ),
    )

    # -------------------------------------------------------------------
    # DATA — Données (fail-closed sur DATA_005)
    # -------------------------------------------------------------------
    DATA_001 = _CodeSpec(
        code="DATA_001",
        short="stale_data",
        default_remediation=(
            "Données en cache plus vieilles que le seuil (cf. §7.3 data_quality). "
            "Purger data/cache/ ou attendre le prochain fetch."
        ),
        severity="WARNING",
    )
    DATA_002 = _CodeSpec(
        code="DATA_002",
        short="ohlcv_gap",
        default_remediation=(
            "Trou dans la série OHLCV (barre manquante, week-end, halte de trading). "
            "Vérifier via src/utils/data_quality._detect_gaps()."
        ),
        severity="WARNING",
    )
    DATA_003 = _CodeSpec(
        code="DATA_003",
        short="outlier",
        default_remediation=(
            "Variation > 10 % sur une barre — possible spike corrompu. La barre est écartée "
            "du calcul des indicateurs; vérifier la source."
        ),
        severity="WARNING",
    )
    DATA_004 = _CodeSpec(
        code="DATA_004",
        short="source_failed_fallback_ok",
        default_remediation=(
            "Source primaire KO, fallback secondaire a réussi — aucune action immédiate. "
            "Si le taux d'erreur primaire > 20 % sur 1 h, investiguer."
        ),
        severity="WARNING",
    )
    DATA_005 = _CodeSpec(
        code="DATA_005",
        short="all_sources_exhausted",
        default_remediation=(
            "TOUTES les sources pour cet asset ont échoué — cycle fail-closed (§2.2). "
            "1) curl -I sur chaque provider. 2) Vérifier les quotas daily_request_caps dans "
            "config/sources.yaml. 3) Le cycle n'émettra aucune proposition pour cet asset."
        ),
    )
    DATA_006 = _CodeSpec(
        code="DATA_006",
        short="empty_shortlist",
        default_remediation=(
            "Shortlist vide après scoring (§8.8.2) — signal marché sans edge, pas une erreur. "
            "Cycle terminé proprement sans proposition."
        ),
        severity="WARNING",
    )
    DATA_007 = _CodeSpec(
        code="DATA_007",
        short="scrape_layout_changed",
        default_remediation=(
            "Structure HTML du scrape (Boursorama) a changé — parse échoue. "
            "Vérifier src/data/sources/boursorama_scrape.py et ajuster les selecteurs."
        ),
    )
    DATA_008 = _CodeSpec(
        code="DATA_008",
        short="timeframe_unsupported",
        default_remediation=(
            "Timeframe demandé non supporté par la source (ex: intraday sur provider EOD-only). "
            "Utiliser un autre provider ou adapter la stratégie."
        ),
    )

    # -------------------------------------------------------------------
    # LLM — API Anthropic
    # -------------------------------------------------------------------
    LLM_001 = _CodeSpec(
        code="LLM_001",
        short="auth_failed",
        default_remediation=(
            "HTTP 401 — clé ANTHROPIC_API_KEY invalide ou révoquée. Régénérer sur console.anthropic.com "
            "et mettre à jour .env."
        ),
        severity="CRITICAL",
    )
    LLM_002 = _CodeSpec(
        code="LLM_002",
        short="rate_limit",
        default_remediation=(
            "HTTP 429 rate-limit — retry automatique avec backoff (60s). Si récurrent, réduire "
            "la fréquence des cycles ou augmenter le plan Anthropic."
        ),
    )
    LLM_003 = _CodeSpec(
        code="LLM_003",
        short="overloaded",
        default_remediation=(
            "HTTP 529 Anthropic overloaded — retry automatique. Si > 10 min, alerter Telegram et "
            "laisser le cycle suivant réessayer."
        ),
    )
    LLM_004 = _CodeSpec(
        code="LLM_004",
        short="token_budget_exceeded",
        default_remediation=(
            "Budget tokens quotidien dépassé (§11.4). Le risk-gate C7 bloque toute nouvelle "
            "proposition jusqu'à 00:00 UTC. Si récurrent, revoir max_daily_tokens dans risk.yaml."
        ),
    )
    LLM_005 = _CodeSpec(
        code="LLM_005",
        short="invalid_json_response",
        default_remediation=(
            "Réponse LLM non-conforme au schéma (JSON manquant ou invalide). Retry 1× avec prompt "
            "de correction; si ré-échec, log.error + cycle skip l'item."
        ),
    )

    # -------------------------------------------------------------------
    # RISK — Risk management (toutes bloquantes pour la proposition)
    # -------------------------------------------------------------------
    RISK_001 = _CodeSpec(
        code="RISK_001",
        short="kill_switch_on",
        default_remediation=(
            "Fichier data/KILL présent — toute proposition gelée. `rm data/KILL` pour réactiver."
        ),
        severity="WARNING",
    )
    RISK_002 = _CodeSpec(
        code="RISK_002",
        short="circuit_breaker_tripped",
        default_remediation=(
            "Circuit breaker C5 armé (3+ degradation_flags ou risk_gate_failure_rate > 50 %). "
            "Cycle suivant reporté de 1 h (§11.1). Vérifier data/logs/ pour la cause."
        ),
    )
    RISK_003 = _CodeSpec(
        code="RISK_003",
        short="daily_loss_reached",
        default_remediation=(
            "Perte quotidienne max atteinte (C2). Aucune nouvelle proposition jusqu'à 00:00 UTC. "
            "Auto-reset au changement de jour UTC."
        ),
        severity="WARNING",
    )
    RISK_004 = _CodeSpec(
        code="RISK_004",
        short="proposal_rejected",
        default_remediation=(
            "Proposition rejetée par la risk-gate (voir `reasons` du RiskDecision). "
            "Aucune action requise — comportement attendu (§11.6)."
        ),
        severity="WARNING",
    )

    # -------------------------------------------------------------------
    # RUN — Runtime / infra
    # -------------------------------------------------------------------
    RUN_001 = _CodeSpec(
        code="RUN_001",
        short="sqlite_lock",
        default_remediation=(
            "SQLite locked (timeout 5 s dépassé). WAL mode activé : log + retry dans le cycle suivant. "
            "Si persistant, vérifier data/memory.db (-wal, -shm)."
        ),
    )
    RUN_002 = _CodeSpec(
        code="RUN_002",
        short="parquet_corrupt",
        default_remediation=(
            "Fichier parquet corrompu — supprimer data/cache/<asset>/<tf>.parquet et laisser "
            "le prochain fetch le régénérer."
        ),
    )
    RUN_003 = _CodeSpec(
        code="RUN_003",
        short="disk_full",
        default_remediation=(
            "Espace disque < 5 % — purger data/logs/ (>14j) et data/dashboards/ (>30j). "
            "Revoir la politique logrotate dans deploy/logrotate.conf."
        ),
        severity="CRITICAL",
    )
    RUN_004 = _CodeSpec(
        code="RUN_004",
        short="scheduler_missed_fire",
        default_remediation=(
            "Un cycle programmé a sauté (APScheduler missed_fire). Normal si host surchargé; "
            "si > 3 missed/jour, augmenter misfire_grace_time dans le scheduler."
        ),
        severity="WARNING",
    )

    # ------------------------------------------------------------------
    # Accesseurs pratiques
    # ------------------------------------------------------------------

    @property
    def code(self) -> str:
        return self.value.code

    @property
    def short(self) -> str:
        return self.value.short

    @property
    def default_remediation(self) -> str:
        return self.value.default_remediation

    @property
    def severity(self) -> Severity:
        return self.value.severity


def by_code(code: str) -> EC:
    """Lookup inverse : 'NET_002' → EC.NET_002. Lève KeyError si inconnu."""
    return EC[code]


__all__ = ["EC", "Severity", "by_code"]
