"""
src.memory.repositories — Façade d'écriture/lecture sur SQLite.

Source : TRADING_BOT_ARCHITECTURE.md §10.2 (trades, lessons, hypotheses,
regime_snapshots, performance_metrics) et §14.5.1 (llm_usage, api_usage).

Principes :
- **Append-only** pour lessons, llm_usage, api_usage, observations, rollbacks.
- **Mutables** (mais tracés via `last_updated`) : trades (status, exit_*),
  hypotheses (status, bayesian_score, evidence), patches (status).
- Pas d'ORM : requêtes paramétrées SQLite uniquement (protection injection,
  simplicité, pas d'état partagé).
- Les repositories ne déclenchent pas d'erreur remontant l'application pour la
  télémétrie (fail-open §14.5.1) — les repositories de décision (trades, lessons)
  remontent les erreurs au caller.

Usage :
    con = init_db()
    trades = TradesRepository(con)
    trades.insert(TradeRecord(...))
    all_open = trades.list_open()
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Optional, Sequence

from src.utils.logging_utils import get_logger


log = get_logger("Memory")


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


def _utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).replace(microsecond=0).isoformat()


def _json_dumps(obj: Any) -> str:
    """Sérialisation JSON déterministe (tri des clés pour faciliter les diffs)."""
    return json.dumps(obj, separators=(",", ":"), sort_keys=True, ensure_ascii=False)


def _json_loads(s: Optional[str], default: Any) -> Any:
    if not s:
        return default
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        return default


# ---------------------------------------------------------------------------
# Records (DTO légers — les contrats Pydantic sont à la frontière des skills)
# ---------------------------------------------------------------------------


@dataclass
class TradeRecord:
    id: str
    asset: str
    asset_class: str
    strategy: str
    side: str                           # long | short
    entry_price: float
    entry_time: str                     # ISO 8601
    stop_price: float
    tp_prices: list[float]
    size_pct_equity: float
    conviction: Optional[float] = None
    rr_estimated: Optional[float] = None
    catalysts: list[str] = field(default_factory=list)
    exit_price: Optional[float] = None
    exit_time: Optional[str] = None
    pnl_pct: Optional[float] = None
    pnl_usd_fictif: Optional[float] = None
    status: str = "open"                # open | closed | cancelled
    validated: bool = False
    llm_narrative: Optional[str] = None
    session_id: Optional[str] = None

    @staticmethod
    def new_id() -> str:
        return f"T-{uuid.uuid4().hex[:6].upper()}"


@dataclass
class LessonRecord:
    id: str
    date: str
    content: str
    trade_ref: Optional[str] = None
    tags: list[str] = field(default_factory=list)
    confidence: float = 1.0

    @staticmethod
    def new_id() -> str:
        return f"L-{uuid.uuid4().hex[:6].upper()}"


@dataclass
class HypothesisRecord:
    id: str
    content: str
    started_at: str
    status: str = "testing"             # testing | confirmed | rejected
    bayesian_score: float = 0.5
    last_updated: Optional[str] = None
    evidence: list[dict] = field(default_factory=list)

    @staticmethod
    def new_id() -> str:
        return f"H-{uuid.uuid4().hex[:6].upper()}"


@dataclass
class RegimeSnapshotRecord:
    date: str
    macro: str
    volatility: str
    trend_equity: Optional[str] = None
    trend_forex: Optional[str] = None
    trend_crypto: Optional[str] = None
    prob_risk_off: Optional[float] = None
    prob_transition: Optional[float] = None
    prob_risk_on: Optional[float] = None
    hmm_state: Optional[int] = None


@dataclass
class PerformanceMetricsRecord:
    strategy: str
    date: str
    trades_total: Optional[int] = None
    trades_30d: Optional[int] = None
    winrate_total: Optional[float] = None
    winrate_30d: Optional[float] = None
    profit_factor: Optional[float] = None
    sharpe_30d: Optional[float] = None
    sharpe_90d: Optional[float] = None
    max_drawdown: Optional[float] = None
    active: bool = True


# ---------------------------------------------------------------------------
# Base repository
# ---------------------------------------------------------------------------


class _BaseRepository:
    def __init__(self, con: sqlite3.Connection) -> None:
        self.con = con

    def _exec(self, sql: str, params: Sequence | dict | None = None) -> sqlite3.Cursor:
        return self.con.execute(sql, params or ())

    def _fetch_one_as_dict(self, cur: sqlite3.Cursor) -> Optional[dict]:
        row = cur.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, row))

    def _fetch_all_as_dicts(self, cur: sqlite3.Cursor) -> list[dict]:
        rows = cur.fetchall()
        if not rows:
            return []
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in rows]


# ---------------------------------------------------------------------------
# Trades
# ---------------------------------------------------------------------------


class TradesRepository(_BaseRepository):
    """Source de vérité pour les trades simulés (V1 paper-trading)."""

    def insert(self, rec: TradeRecord) -> None:
        self._exec(
            """
            INSERT INTO trades (
                id, asset, asset_class, strategy, side,
                entry_price, entry_time, stop_price, tp_prices, size_pct_equity,
                conviction, rr_estimated, catalysts,
                exit_price, exit_time, pnl_pct, pnl_usd_fictif,
                status, validated, llm_narrative, session_id
            ) VALUES (
                ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?,
                ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?, ?, ?
            )
            """,
            (
                rec.id, rec.asset, rec.asset_class, rec.strategy, rec.side,
                rec.entry_price, rec.entry_time, rec.stop_price, _json_dumps(rec.tp_prices), rec.size_pct_equity,
                rec.conviction, rec.rr_estimated, _json_dumps(rec.catalysts),
                rec.exit_price, rec.exit_time, rec.pnl_pct, rec.pnl_usd_fictif,
                rec.status, 1 if rec.validated else 0, rec.llm_narrative, rec.session_id,
            ),
        )

    def close(
        self,
        trade_id: str,
        *,
        exit_price: float,
        exit_time: str,
        pnl_pct: float,
        pnl_usd_fictif: float,
    ) -> int:
        cur = self._exec(
            """
            UPDATE trades
               SET exit_price = ?, exit_time = ?, pnl_pct = ?, pnl_usd_fictif = ?,
                   status = 'closed'
             WHERE id = ? AND status = 'open'
            """,
            (exit_price, exit_time, pnl_pct, pnl_usd_fictif, trade_id),
        )
        return cur.rowcount

    def list_open(self, *, limit: int = 50) -> list[dict]:
        cur = self._exec(
            "SELECT * FROM trades WHERE status = 'open' ORDER BY entry_time DESC LIMIT ?",
            (limit,),
        )
        return self._fetch_all_as_dicts(cur)

    def get(self, trade_id: str) -> Optional[dict]:
        cur = self._exec("SELECT * FROM trades WHERE id = ?", (trade_id,))
        return self._fetch_one_as_dict(cur)


# ---------------------------------------------------------------------------
# Lessons (append-only)
# ---------------------------------------------------------------------------


class LessonsRepository(_BaseRepository):
    """Leçons apprises : une fois insérées, seule une archive (flag) est
    possible — jamais d'UPDATE destructif du contenu."""

    def insert(self, rec: LessonRecord) -> None:
        self._exec(
            """
            INSERT INTO lessons (id, date, content, trade_ref, tags, confidence)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                rec.id, rec.date, rec.content,
                rec.trade_ref, _json_dumps(rec.tags), rec.confidence,
            ),
        )

    def archive(self, lesson_id: str) -> int:
        cur = self._exec(
            "UPDATE lessons SET archived = 1 WHERE id = ? AND archived = 0",
            (lesson_id,),
        )
        return cur.rowcount

    def recent(self, *, limit: int = 30, include_archived: bool = False) -> list[dict]:
        where = "" if include_archived else " WHERE archived = 0 "
        cur = self._exec(
            f"SELECT * FROM lessons{where} ORDER BY date DESC, created_at DESC LIMIT ?",
            (limit,),
        )
        return self._fetch_all_as_dicts(cur)

    def all_active(self) -> list[dict]:
        """Toutes les leçons non-archivées : utilisé par le rebuild FAISS."""
        cur = self._exec(
            "SELECT * FROM lessons WHERE archived = 0 ORDER BY created_at ASC"
        )
        return self._fetch_all_as_dicts(cur)


# ---------------------------------------------------------------------------
# Hypotheses
# ---------------------------------------------------------------------------


class HypothesesRepository(_BaseRepository):
    def insert(self, rec: HypothesisRecord) -> None:
        self._exec(
            """
            INSERT INTO hypotheses (
                id, content, status, bayesian_score,
                started_at, last_updated, evidence, archived
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 0)
            """,
            (
                rec.id, rec.content, rec.status, rec.bayesian_score,
                rec.started_at, rec.last_updated, _json_dumps(rec.evidence),
            ),
        )

    def add_evidence(
        self,
        hypothesis_id: str,
        *,
        date: str,
        result: str,
        delta_score: float,
    ) -> None:
        row = self._fetch_one_as_dict(
            self._exec("SELECT evidence, bayesian_score FROM hypotheses WHERE id = ?", (hypothesis_id,))
        )
        if not row:
            return
        evidence = _json_loads(row.get("evidence"), [])
        evidence.append({"date": date, "result": result, "delta_score": delta_score})
        new_score = max(0.0, min(1.0, (row["bayesian_score"] or 0.5) + delta_score))
        self._exec(
            """
            UPDATE hypotheses
               SET evidence = ?, bayesian_score = ?, last_updated = ?
             WHERE id = ?
            """,
            (_json_dumps(evidence), new_score, _utc_now_iso(), hypothesis_id),
        )

    def set_status(self, hypothesis_id: str, status: str) -> int:
        cur = self._exec(
            "UPDATE hypotheses SET status = ?, last_updated = ? WHERE id = ?",
            (status, _utc_now_iso(), hypothesis_id),
        )
        return cur.rowcount

    def active(self, *, limit: int = 15) -> list[dict]:
        cur = self._exec(
            """
            SELECT * FROM hypotheses
             WHERE archived = 0 AND status IN ('testing', 'confirmed')
             ORDER BY last_updated DESC, started_at DESC
             LIMIT ?
            """,
            (limit,),
        )
        return self._fetch_all_as_dicts(cur)


# ---------------------------------------------------------------------------
# Regime snapshots + performance metrics (UPSERT)
# ---------------------------------------------------------------------------


class RegimeSnapshotsRepository(_BaseRepository):
    def upsert(self, rec: RegimeSnapshotRecord) -> None:
        self._exec(
            """
            INSERT INTO regime_snapshots (
                date, macro, volatility, trend_equity, trend_forex, trend_crypto,
                prob_risk_off, prob_transition, prob_risk_on, hmm_state
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(date) DO UPDATE SET
                macro = excluded.macro,
                volatility = excluded.volatility,
                trend_equity = excluded.trend_equity,
                trend_forex = excluded.trend_forex,
                trend_crypto = excluded.trend_crypto,
                prob_risk_off = excluded.prob_risk_off,
                prob_transition = excluded.prob_transition,
                prob_risk_on = excluded.prob_risk_on,
                hmm_state = excluded.hmm_state
            """,
            (
                rec.date, rec.macro, rec.volatility,
                rec.trend_equity, rec.trend_forex, rec.trend_crypto,
                rec.prob_risk_off, rec.prob_transition, rec.prob_risk_on,
                rec.hmm_state,
            ),
        )

    def latest(self) -> Optional[dict]:
        cur = self._exec(
            "SELECT * FROM regime_snapshots ORDER BY date DESC LIMIT 1"
        )
        return self._fetch_one_as_dict(cur)


class PerformanceRepository(_BaseRepository):
    def upsert(self, rec: PerformanceMetricsRecord) -> None:
        self._exec(
            """
            INSERT INTO performance_metrics (
                strategy, date, trades_total, trades_30d,
                winrate_total, winrate_30d, profit_factor,
                sharpe_30d, sharpe_90d, max_drawdown, active
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(strategy, date) DO UPDATE SET
                trades_total = excluded.trades_total,
                trades_30d = excluded.trades_30d,
                winrate_total = excluded.winrate_total,
                winrate_30d = excluded.winrate_30d,
                profit_factor = excluded.profit_factor,
                sharpe_30d = excluded.sharpe_30d,
                sharpe_90d = excluded.sharpe_90d,
                max_drawdown = excluded.max_drawdown,
                active = excluded.active
            """,
            (
                rec.strategy, rec.date, rec.trades_total, rec.trades_30d,
                rec.winrate_total, rec.winrate_30d, rec.profit_factor,
                rec.sharpe_30d, rec.sharpe_90d, rec.max_drawdown,
                1 if rec.active else 0,
            ),
        )

    def latest_by_strategy(self) -> list[dict]:
        cur = self._exec(
            """
            SELECT pm.*
              FROM performance_metrics pm
              JOIN (
                   SELECT strategy, MAX(date) AS max_date
                     FROM performance_metrics GROUP BY strategy
              ) last ON last.strategy = pm.strategy AND last.max_date = pm.date
             ORDER BY pm.strategy ASC
            """
        )
        return self._fetch_all_as_dicts(cur)


# ---------------------------------------------------------------------------
# Télémétrie (fail-open — les erreurs sont loggées, jamais remontées)
# ---------------------------------------------------------------------------


class LLMUsageRepository(_BaseRepository):
    def record(
        self,
        *,
        agent: str,
        model: str,
        tokens_in: int,
        tokens_out: int,
        cost_usd: float,
        session_id: Optional[str] = None,
        request_ref: Optional[str] = None,
        duration_ms: Optional[int] = None,
        error: Optional[str] = None,
        ts: Optional[str] = None,
    ) -> None:
        try:
            self._exec(
                """
                INSERT INTO llm_usage (
                    ts, agent, model, tokens_in, tokens_out, cost_usd,
                    session_id, request_ref, duration_ms, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ts or _utc_now_iso(), agent, model,
                    int(tokens_in), int(tokens_out), float(cost_usd),
                    session_id, request_ref, duration_ms, error,
                ),
            )
        except sqlite3.Error as e:
            log.warning("llm_usage_insert_failed", cause=str(e))


class APIUsageRepository(_BaseRepository):
    def record(
        self,
        *,
        source: str,
        kind: str,                       # prix | macro | news
        asset: Optional[str] = None,
        endpoint: Optional[str] = None,
        status: Optional[int] = None,
        latency_ms: Optional[int] = None,
        cached: bool = False,
        cost_usd: float = 0.0,
        ts: Optional[str] = None,
    ) -> None:
        try:
            self._exec(
                """
                INSERT INTO api_usage (
                    ts, source, kind, asset, endpoint,
                    status, latency_ms, cached, cost_usd
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ts or _utc_now_iso(), source, kind, asset, endpoint,
                    status, latency_ms, 1 if cached else 0, cost_usd,
                ),
            )
        except sqlite3.Error as e:
            log.warning("api_usage_insert_failed", cause=str(e))


# ---------------------------------------------------------------------------
# Cycles & observations (support orchestrator §8.7)
# ---------------------------------------------------------------------------


class CyclesRepository(_BaseRepository):
    def start(self, *, cycle_id: str, kind: str, started_at: Optional[str] = None) -> None:
        self._exec(
            """
            INSERT INTO cycles (id, kind, started_at, status)
            VALUES (?, ?, ?, 'running')
            """,
            (cycle_id, kind, started_at or _utc_now_iso()),
        )

    def finish(
        self,
        cycle_id: str,
        *,
        status: str,
        proposals_count: int = 0,
        approved_count: int = 0,
        degradation: Optional[Iterable[str]] = None,
        risk_gate_failure_rate: Optional[float] = None,
        report_path: Optional[str] = None,
        error: Optional[str] = None,
    ) -> None:
        self._exec(
            """
            UPDATE cycles
               SET finished_at = ?, status = ?, proposals_count = ?, approved_count = ?,
                   degradation = ?, risk_gate_failure_rate = ?, report_path = ?, error = ?
             WHERE id = ?
            """,
            (
                _utc_now_iso(), status, proposals_count, approved_count,
                _json_dumps(list(degradation or [])), risk_gate_failure_rate,
                report_path, error, cycle_id,
            ),
        )

    def last_n(self, n: int = 20) -> list[dict]:
        cur = self._exec(
            "SELECT * FROM cycles ORDER BY started_at DESC LIMIT ?", (n,),
        )
        return self._fetch_all_as_dicts(cur)


class ObservationsRepository(_BaseRepository):
    """Append-only : une ligne par analyse d'actif dans un cycle."""

    def record(
        self,
        *,
        cycle_id: str,
        asset: str,
        payload: dict,
        strategy: Optional[str] = None,
        approved: bool = False,
    ) -> None:
        self._exec(
            """
            INSERT INTO observations (cycle_id, asset, strategy, approved, payload)
            VALUES (?, ?, ?, ?, ?)
            """,
            (cycle_id, asset, strategy, 1 if approved else 0, _json_dumps(payload)),
        )

    def for_cycle(self, cycle_id: str) -> list[dict]:
        cur = self._exec(
            "SELECT * FROM observations WHERE cycle_id = ? ORDER BY id ASC",
            (cycle_id,),
        )
        return self._fetch_all_as_dicts(cur)


# ---------------------------------------------------------------------------
# Self-improve : patches + rollbacks (§13)
# ---------------------------------------------------------------------------


class PatchesRepository(_BaseRepository):
    def insert(
        self,
        *,
        patch_id: str,
        target: str,
        kind: str,
        score: Optional[float] = None,
        t_stat: Optional[float] = None,
        sharpe_delta: Optional[float] = None,
        dsr: Optional[float] = None,
        metrics: Optional[dict] = None,
        proposed_at: Optional[str] = None,
    ) -> None:
        self._exec(
            """
            INSERT INTO patches (
                id, proposed_at, target, kind, status,
                score, t_stat, sharpe_delta, dsr, metrics_json
            ) VALUES (?, ?, ?, ?, 'proposed', ?, ?, ?, ?, ?)
            """,
            (
                patch_id, proposed_at or _utc_now_iso(), target, kind,
                score, t_stat, sharpe_delta, dsr,
                _json_dumps(metrics or {}),
            ),
        )

    def set_status(
        self,
        patch_id: str,
        status: str,
        *,
        merge_commit: Optional[str] = None,
        active: Optional[bool] = None,
        rollback_reason: Optional[str] = None,
    ) -> int:
        fields = ["status = ?"]
        params: list[Any] = [status]
        if merge_commit is not None:
            fields.append("merge_commit = ?")
            params.append(merge_commit)
        if active is not None:
            fields.append("active = ?")
            params.append(1 if active else 0)
        if rollback_reason is not None:
            fields.append("rollback_reason = ?")
            params.append(rollback_reason)
        params.append(patch_id)
        cur = self._exec(
            f"UPDATE patches SET {', '.join(fields)} WHERE id = ?", params,
        )
        return cur.rowcount

    def get(self, patch_id: str) -> Optional[dict]:
        cur = self._exec("SELECT * FROM patches WHERE id = ?", (patch_id,))
        return self._fetch_one_as_dict(cur)

    def recent(self, n: int = 20) -> list[dict]:
        cur = self._exec(
            "SELECT * FROM patches ORDER BY proposed_at DESC LIMIT ?", (n,),
        )
        return self._fetch_all_as_dicts(cur)


class RollbacksRepository(_BaseRepository):
    def record(
        self,
        *,
        patch_id: str,
        reason: str,
        triggered_by: Optional[str] = None,
        metrics_snapshot: Optional[dict] = None,
        triggered_at: Optional[str] = None,
    ) -> None:
        self._exec(
            """
            INSERT INTO rollbacks (patch_id, triggered_at, reason, triggered_by, metrics_snapshot_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                patch_id, triggered_at or _utc_now_iso(), reason, triggered_by,
                _json_dumps(metrics_snapshot or {}),
            ),
        )

    def for_patch(self, patch_id: str) -> list[dict]:
        cur = self._exec(
            "SELECT * FROM rollbacks WHERE patch_id = ? ORDER BY triggered_at ASC",
            (patch_id,),
        )
        return self._fetch_all_as_dicts(cur)


# ---------------------------------------------------------------------------
# Purge télémétrie (utilisée par `memory-consolidate` §10.6)
# ---------------------------------------------------------------------------


def purge_telemetry(con: sqlite3.Connection, *, retention_days: int = 180) -> dict[str, int]:
    """Supprime les lignes télémétrie au-delà de `retention_days`.

    Renvoie un dict `{table: rows_deleted}`.
    """
    cutoff = f"datetime('now', '-{int(retention_days)} days')"
    deleted: dict[str, int] = {}
    for table in ("llm_usage", "api_usage"):
        cur = con.execute(f"DELETE FROM {table} WHERE ts < {cutoff}")
        deleted[table] = cur.rowcount
    return deleted


__all__ = [
    # records
    "TradeRecord", "LessonRecord", "HypothesisRecord",
    "RegimeSnapshotRecord", "PerformanceMetricsRecord",
    # repos
    "TradesRepository", "LessonsRepository", "HypothesesRepository",
    "RegimeSnapshotsRepository", "PerformanceRepository",
    "LLMUsageRepository", "APIUsageRepository",
    "CyclesRepository", "ObservationsRepository",
    "PatchesRepository", "RollbacksRepository",
    # fonction purge
    "purge_telemetry",
]
