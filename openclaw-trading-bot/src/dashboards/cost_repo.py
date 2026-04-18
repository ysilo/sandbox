"""
src.dashboards.cost_repo — agrégation coûts/API pour le panel §14.3.

Pull-only : `build_panel()` est appelé à la fin de chaque cycle (§8.7.3
étape 7) + exposé via `GET /costs.json` (§14.5.6). Pas de polling.

Requêtes SQL canoniques §14.5.5 — adaptées à notre schéma §14.5.1 où :
- `llm_usage` : colonnes `ts`, `tokens_in`, `tokens_out` (pas de cache_read/
  cache_write dans V1 — on renverra 0 côté CostPanel).
- `api_usage` : colonnes `ts`, `source`, `status` (http code), `cached`,
  `latency_ms`. On dérive `success` par `status BETWEEN 200 AND 299` OR
  `cached = 1`.

Invariants :
- Idempotent, lecture pure (pas d'effet de bord SQL).
- Cohérence : toutes les requêtes utilisent le même `now` snapshot.
- Dégradation : si `llm_usage` est vide (bootstrap) → champs à 0,
  `source_data_lag_seconds = +inf`, alerte `no_llm_data` levée.
- Performance : ~50 ms sur ~100 k lignes (SQLite WAL local).
"""
from __future__ import annotations

import logging
import math
import sqlite3
from calendar import monthrange
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Literal, Optional

from src.dashboards.pricing import LLMLimits, ModelPricing

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# DTOs (contrats Jinja + JSON) — §14.5.3 adaptés au schéma V1
# ---------------------------------------------------------------------------


@dataclass
class AgentCostRow:
    agent: str
    calls_24h: int
    tokens_in_24h: int
    tokens_out_24h: int
    cost_24h_usd: float
    cost_month_usd: float
    model: str
    pct_month_budget: float


@dataclass
class ModelCostRow:
    model: str
    tokens_in: int
    tokens_out: int
    cost_usd: float


@dataclass
class ApiSourceRow:
    source: str
    kind: str
    calls_24h: int
    cache_hit_pct: float
    latency_p50_ms: int
    latency_p95_ms: int
    error_rate_pct: float
    quota_used_pct: Optional[float]
    cost_24h_usd: float
    state: Literal["green", "amber", "red"]


@dataclass
class DailyCostPoint:
    day: str           # "YYYY-MM-DD"
    cost_usd: float
    tokens: int


@dataclass
class ConsumerRow:
    cycle_id: Optional[str]
    agent: str
    model: str
    cost_usd: float
    tokens: int
    request_ref: Optional[str] = None


@dataclass
class CostAlert:
    level: Literal["info", "warning", "critical"]
    code: str          # "tokens_daily_warn", "cost_month_crit", "no_llm_data", "pricing_stale"
    message: str


@dataclass
class CostPanel:
    tokens_today: int
    tokens_daily_budget: int
    cost_month_usd: float
    cost_month_budget_usd: float
    forecast_month_usd: float
    by_agent: list[AgentCostRow]
    by_model: list[ModelCostRow]
    by_api_source: list[ApiSourceRow]
    trend_30d: list[DailyCostPoint]
    top_consumers: list[ConsumerRow]
    pricing_last_updated: str
    alerts: list[CostAlert]
    computed_at: str                   # ISO-8601 UTC
    source_data_lag_seconds: float     # +inf si llm_usage vide

    def to_dict(self) -> dict[str, Any]:
        """Conversion JSON-safe — remplace +inf par None (non sérialisable)."""
        d = asdict(self)
        if math.isinf(d.get("source_data_lag_seconds", 0.0)):
            d["source_data_lag_seconds"] = None
        return d


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------


class CostRepository:
    """Agrège `llm_usage` + `api_usage` sur 24h/mois/30j (§14.5.5)."""

    # Seuils §14.3.4
    _AMBER_ERROR_PCT = 2.0
    _RED_ERROR_PCT = 10.0
    _AMBER_LATENCY_P95_MS = 1500
    _RED_LATENCY_P95_MS = 2000

    def __init__(
        self,
        db: sqlite3.Connection,
        pricing: ModelPricing,
        limits: LLMLimits,
    ) -> None:
        self.db = db
        self.pricing = pricing
        self.limits = limits

    # ------------------------------------------------------------------
    # Entrée publique
    # ------------------------------------------------------------------

    def build_panel(self, *, now: Optional[datetime] = None) -> CostPanel:
        now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        w = self._windows(now)

        tokens_today = self._tokens_today(w)
        cost_mtd = self._cost_mtd(w)
        by_agent = self._breakdown_by_agent(w)
        by_model = self._breakdown_by_model(w)
        by_api = self._api_health(w)
        trend = self._trend_30d(w)
        top = self._top_consumers(w, n=5)
        lag = self._data_lag(now)
        alerts = self._alerts(tokens_today, cost_mtd, lag)

        return CostPanel(
            tokens_today=tokens_today,
            tokens_daily_budget=self.limits.max_daily_tokens,
            cost_month_usd=cost_mtd,
            cost_month_budget_usd=self.limits.max_monthly_cost_usd,
            forecast_month_usd=self._forecast(cost_mtd, now),
            by_agent=by_agent,
            by_model=by_model,
            by_api_source=by_api,
            trend_30d=trend,
            top_consumers=top,
            pricing_last_updated=self.pricing.last_updated.isoformat()
            if self.pricing.last_updated != date.min else "unknown",
            alerts=alerts,
            computed_at=now.isoformat().replace("+00:00", "Z"),
            source_data_lag_seconds=lag,
        )

    # ------------------------------------------------------------------
    # Windows — toutes les bornes sont des str ISO-8601 UTC
    # ------------------------------------------------------------------

    def _windows(self, now: datetime) -> dict[str, str]:
        today_utc = now.replace(hour=0, minute=0, second=0, microsecond=0)
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        return {
            "today_utc_start": today_utc.isoformat(),
            "tomorrow_utc_start": (today_utc + timedelta(days=1)).isoformat(),
            "month_start": month_start.isoformat(),
            "d30_start": (now - timedelta(days=30)).isoformat(),
            "window_24h": (now - timedelta(hours=24)).isoformat(),
            "now": now.isoformat(),
        }

    # ------------------------------------------------------------------
    # Q1 — Tokens consommés aujourd'hui
    # ------------------------------------------------------------------

    def _tokens_today(self, w: dict[str, str]) -> int:
        row = self.db.execute(
            """
            SELECT COALESCE(SUM(tokens_in), 0) + COALESCE(SUM(tokens_out), 0)
            FROM llm_usage
            WHERE ts >= :today AND ts < :tomorrow
            """,
            {"today": w["today_utc_start"], "tomorrow": w["tomorrow_utc_start"]},
        ).fetchone()
        return int(row[0] or 0)

    # ------------------------------------------------------------------
    # Q2 — Coût mois courant
    # ------------------------------------------------------------------

    def _cost_mtd(self, w: dict[str, str]) -> float:
        row = self.db.execute(
            """
            SELECT COALESCE(SUM(cost_usd), 0.0)
            FROM llm_usage
            WHERE ts >= :month AND ts < :now
            """,
            {"month": w["month_start"], "now": w["now"]},
        ).fetchone()
        return float(row[0] or 0.0)

    # ------------------------------------------------------------------
    # Q3 — Breakdown par agent (24h + cost mois)
    # ------------------------------------------------------------------

    def _breakdown_by_agent(self, w: dict[str, str]) -> list[AgentCostRow]:
        rows_24h = self.db.execute(
            """
            SELECT agent,
                   COUNT(*)                   AS calls,
                   COALESCE(SUM(tokens_in), 0)  AS tin,
                   COALESCE(SUM(tokens_out), 0) AS tout,
                   COALESCE(SUM(cost_usd), 0)   AS cost,
                   MAX(model)                   AS model
            FROM llm_usage
            WHERE ts >= :w24
            GROUP BY agent
            ORDER BY cost DESC
            """,
            {"w24": w["window_24h"]},
        ).fetchall()

        # cost mois par agent (requête séparée pour éviter join complexe)
        cost_month_by_agent = {
            r[0]: float(r[1] or 0.0)
            for r in self.db.execute(
                """
                SELECT agent, COALESCE(SUM(cost_usd), 0.0) AS cost_month
                FROM llm_usage
                WHERE ts >= :month AND ts < :now
                GROUP BY agent
                """,
                {"month": w["month_start"], "now": w["now"]},
            ).fetchall()
        }

        total_month = sum(cost_month_by_agent.values()) or 1.0  # évite /0
        out: list[AgentCostRow] = []
        for agent, calls, tin, tout, cost_24h, model in rows_24h:
            cost_month = cost_month_by_agent.get(agent, 0.0)
            out.append(
                AgentCostRow(
                    agent=agent,
                    calls_24h=int(calls),
                    tokens_in_24h=int(tin),
                    tokens_out_24h=int(tout),
                    cost_24h_usd=float(cost_24h),
                    cost_month_usd=cost_month,
                    model=model or "unknown",
                    pct_month_budget=round(100.0 * cost_month / total_month, 1),
                )
            )
        return out

    # ------------------------------------------------------------------
    # Q4 — Breakdown par modèle (mois)
    # ------------------------------------------------------------------

    def _breakdown_by_model(self, w: dict[str, str]) -> list[ModelCostRow]:
        rows = self.db.execute(
            """
            SELECT model,
                   COALESCE(SUM(tokens_in), 0),
                   COALESCE(SUM(tokens_out), 0),
                   COALESCE(SUM(cost_usd), 0.0)
            FROM llm_usage
            WHERE ts >= :month AND ts < :now
            GROUP BY model
            ORDER BY 4 DESC
            """,
            {"month": w["month_start"], "now": w["now"]},
        ).fetchall()
        return [
            ModelCostRow(
                model=m or "unknown",
                tokens_in=int(tin),
                tokens_out=int(tout),
                cost_usd=float(cost),
            )
            for m, tin, tout, cost in rows
        ]

    # ------------------------------------------------------------------
    # Q5 — Santé des APIs data sources (24h)
    # ------------------------------------------------------------------

    def _api_health(self, w: dict[str, str]) -> list[ApiSourceRow]:
        rows = self.db.execute(
            """
            SELECT source,
                   COALESCE(MAX(kind), '')                               AS kind,
                   COUNT(*)                                              AS calls,
                   SUM(CASE WHEN cached = 1 THEN 1 ELSE 0 END)           AS cached,
                   SUM(CASE WHEN status BETWEEN 200 AND 299
                             OR cached = 1 THEN 1 ELSE 0 END)            AS ok,
                   COALESCE(SUM(cost_usd), 0.0)                          AS cost_24h
            FROM api_usage
            WHERE ts >= :w24
            GROUP BY source
            ORDER BY source
            """,
            {"w24": w["window_24h"]},
        ).fetchall()
        out: list[ApiSourceRow] = []
        for source, kind, calls, cached, ok, cost in rows:
            calls_i = int(calls or 0)
            err_rate = 100.0 * (calls_i - int(ok or 0)) / calls_i if calls_i else 0.0
            cache_hit = 100.0 * int(cached or 0) / calls_i if calls_i else 0.0
            p50, p95 = self._latency_percentiles(source, w)
            state: Literal["green", "amber", "red"]
            if err_rate > self._RED_ERROR_PCT or p95 > self._RED_LATENCY_P95_MS:
                state = "red"
            elif err_rate > self._AMBER_ERROR_PCT or p95 > self._AMBER_LATENCY_P95_MS:
                state = "amber"
            else:
                state = "green"
            out.append(
                ApiSourceRow(
                    source=source,
                    kind=str(kind or ""),
                    calls_24h=calls_i,
                    cache_hit_pct=round(cache_hit, 1),
                    latency_p50_ms=int(p50),
                    latency_p95_ms=int(p95),
                    error_rate_pct=round(err_rate, 2),
                    quota_used_pct=None,
                    cost_24h_usd=float(cost or 0.0),
                    state=state,
                )
            )
        return out

    def _latency_percentiles(self, source: str, w: dict[str, str]) -> tuple[float, float]:
        """Percentiles p50 / p95 — calcul Python (SQLite n'a pas PERCENTILE_CONT)."""
        vals = [
            int(r[0])
            for r in self.db.execute(
                """
                SELECT latency_ms FROM api_usage
                WHERE source = :s AND ts >= :w24 AND latency_ms IS NOT NULL
                """,
                {"s": source, "w24": w["window_24h"]},
            ).fetchall()
            if r[0] is not None
        ]
        if not vals:
            return 0.0, 0.0
        vals.sort()
        n = len(vals)
        p50 = vals[int(0.50 * (n - 1))]
        p95 = vals[int(0.95 * (n - 1))]
        return float(p50), float(p95)

    # ------------------------------------------------------------------
    # Q6 — Tendance 30j
    # ------------------------------------------------------------------

    def _trend_30d(self, w: dict[str, str]) -> list[DailyCostPoint]:
        rows = self.db.execute(
            """
            SELECT SUBSTR(ts, 1, 10)           AS day,
                   COALESCE(SUM(cost_usd), 0)  AS cost,
                   COALESCE(SUM(tokens_in), 0) + COALESCE(SUM(tokens_out), 0) AS tokens
            FROM llm_usage
            WHERE ts >= :d30
            GROUP BY day
            ORDER BY day
            """,
            {"d30": w["d30_start"]},
        ).fetchall()
        return [DailyCostPoint(day=d, cost_usd=float(c), tokens=int(t)) for d, c, t in rows]

    # ------------------------------------------------------------------
    # Q7 — Top consumers (N=5)
    # ------------------------------------------------------------------

    def _top_consumers(self, w: dict[str, str], *, n: int = 5) -> list[ConsumerRow]:
        rows = self.db.execute(
            """
            SELECT session_id, agent, model,
                   SUM(cost_usd)                        AS cost,
                   SUM(tokens_in + tokens_out)          AS tokens,
                   MAX(request_ref)                     AS request_ref
            FROM llm_usage
            WHERE ts >= :d30
            GROUP BY session_id, agent, model
            ORDER BY cost DESC
            LIMIT :n
            """,
            {"d30": w["d30_start"], "n": n},
        ).fetchall()
        return [
            ConsumerRow(
                cycle_id=sid,
                agent=a,
                model=m,
                cost_usd=float(cost or 0.0),
                tokens=int(tok or 0),
                request_ref=ref,
            )
            for sid, a, m, cost, tok, ref in rows
        ]

    # ------------------------------------------------------------------
    # Data freshness
    # ------------------------------------------------------------------

    def _data_lag(self, now: datetime) -> float:
        row = self.db.execute("SELECT MAX(ts) FROM llm_usage").fetchone()
        if row is None or row[0] is None:
            return float("inf")
        try:
            last = datetime.fromisoformat(row[0])
        except ValueError:
            return float("inf")
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        return (now - last).total_seconds()

    # ------------------------------------------------------------------
    # Forecast (fin de mois)
    # ------------------------------------------------------------------

    def _forecast(self, cost_mtd: float, now: datetime) -> float:
        days_elapsed = now.day
        days_in_month = monthrange(now.year, now.month)[1]
        return cost_mtd / max(days_elapsed, 1) * days_in_month

    # ------------------------------------------------------------------
    # Alertes §14.3.7
    # ------------------------------------------------------------------

    def _alerts(self, tokens_today: int, cost_mtd: float, lag_s: float) -> list[CostAlert]:
        out: list[CostAlert] = []
        daily_budget = self.limits.max_daily_tokens or 1
        month_budget = self.limits.max_monthly_cost_usd or 1.0

        pct_daily = tokens_today / daily_budget
        pct_month = cost_mtd / month_budget

        if pct_daily >= self.limits.daily_tokens_crit_pct:
            out.append(CostAlert(
                level="critical",
                code="tokens_daily_crit",
                message=f"Budget tokens journalier à {pct_daily*100:.0f}% — gate §11.4 imminent.",
            ))
        elif pct_daily >= self.limits.daily_tokens_warn_pct:
            out.append(CostAlert(
                level="warning",
                code="tokens_daily_warn",
                message=f"Budget tokens journalier à {pct_daily*100:.0f}%.",
            ))

        if pct_month >= self.limits.monthly_cost_crit_pct:
            out.append(CostAlert(
                level="critical",
                code="cost_month_crit",
                message=f"Budget coût mensuel à {pct_month*100:.0f}% — kill-switch auto §11.4.",
            ))
        elif pct_month >= self.limits.monthly_cost_warn_pct:
            out.append(CostAlert(
                level="warning",
                code="cost_month_warn",
                message=f"Budget coût mensuel à {pct_month*100:.0f}%.",
            ))

        if math.isinf(lag_s):
            out.append(CostAlert(
                level="info",
                code="no_llm_data",
                message="Aucune donnée dans llm_usage (bootstrap) — panneau à 0.",
            ))
        elif lag_s > 3600:
            out.append(CostAlert(
                level="warning",
                code="data_stale",
                message=f"Dernier point llm_usage il y a {int(lag_s/60)} min.",
            ))

        if self.pricing.is_stale():
            out.append(CostAlert(
                level="warning",
                code="pricing_stale",
                message="config/pricing.yaml > 90 j — vérifier les tarifs Anthropic.",
            ))

        return out


__all__ = [
    "CostRepository",
    "CostPanel",
    "AgentCostRow",
    "ModelCostRow",
    "ApiSourceRow",
    "DailyCostPoint",
    "ConsumerRow",
    "CostAlert",
]
