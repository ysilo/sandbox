"""
src.memory.db — SQLite WAL + migrations idempotentes.

Source : TRADING_BOT_ARCHITECTURE.md §10.1, §10.2 et §14.5.1.

Tables créées par `init_db` :
- §10.2  : trades, lessons, hypotheses, regime_snapshots, performance_metrics
- §14.5.1 : llm_usage, api_usage (télémétrie, rétention 180j)
- support orchestrator : cycles, observations, patches, rollbacks

Notes :
- WAL + foreign_keys=ON (cohérence + concurrence lecture/écriture).
- Migrations : toutes les instructions sont `IF NOT EXISTS`, donc idempotentes.
  Versioning trivial via `PRAGMA user_version` pour futures migrations
  non-compatibles ; en V1 on se contente du niveau 1.
- `init_db(":memory:")` est accepté pour les tests (même schéma).
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Final


DB_PATH: Final[str] = "data/memory.db"
SCHEMA_VERSION: Final[int] = 1


_SCHEMA_STATEMENTS: list[str] = [
    # --- §10.2 Trades ------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS trades (
        id              TEXT PRIMARY KEY,
        asset           TEXT NOT NULL,
        asset_class     TEXT NOT NULL,
        strategy        TEXT NOT NULL,
        side            TEXT NOT NULL,
        entry_price     REAL NOT NULL,
        entry_time      TEXT NOT NULL,
        stop_price      REAL NOT NULL,
        tp_prices       TEXT NOT NULL,
        size_pct_equity REAL NOT NULL,
        conviction      REAL,
        rr_estimated    REAL,
        catalysts       TEXT,
        exit_price      REAL,
        exit_time       TEXT,
        pnl_pct         REAL,
        pnl_usd_fictif  REAL,
        status          TEXT NOT NULL DEFAULT 'open',
        validated       INTEGER NOT NULL DEFAULT 0,
        llm_narrative   TEXT,
        session_id      TEXT,
        created_at      TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_trades_asset       ON trades(asset)",
    "CREATE INDEX IF NOT EXISTS idx_trades_status      ON trades(status)",
    "CREATE INDEX IF NOT EXISTS idx_trades_strategy    ON trades(strategy)",
    "CREATE INDEX IF NOT EXISTS idx_trades_entry_time  ON trades(entry_time)",
    # --- §10.2 Lessons (append-only) --------------------------------------
    """
    CREATE TABLE IF NOT EXISTS lessons (
        id          TEXT PRIMARY KEY,
        date        TEXT NOT NULL,
        content     TEXT NOT NULL,
        trade_ref   TEXT REFERENCES trades(id),
        tags        TEXT,
        confidence  REAL DEFAULT 1.0,
        archived    INTEGER NOT NULL DEFAULT 0,
        created_at  TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_lessons_date ON lessons(date)",
    # --- §10.2 Hypotheses --------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS hypotheses (
        id              TEXT PRIMARY KEY,
        content         TEXT NOT NULL,
        status          TEXT NOT NULL DEFAULT 'testing',
        bayesian_score  REAL DEFAULT 0.5,
        started_at      TEXT NOT NULL,
        last_updated    TEXT,
        evidence        TEXT,
        archived        INTEGER NOT NULL DEFAULT 0
    )
    """,
    # --- §10.2 Regime snapshots (1/jour) ----------------------------------
    """
    CREATE TABLE IF NOT EXISTS regime_snapshots (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        date            TEXT NOT NULL UNIQUE,
        macro           TEXT NOT NULL,
        volatility      TEXT NOT NULL,
        trend_equity    TEXT,
        trend_forex     TEXT,
        trend_crypto    TEXT,
        prob_risk_off   REAL,
        prob_transition REAL,
        prob_risk_on    REAL,
        hmm_state       INTEGER,
        created_at      TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,
    # --- §10.2 Performance metrics -----------------------------------------
    """
    CREATE TABLE IF NOT EXISTS performance_metrics (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        strategy        TEXT NOT NULL,
        date            TEXT NOT NULL,
        trades_total    INTEGER,
        trades_30d      INTEGER,
        winrate_total   REAL,
        winrate_30d     REAL,
        profit_factor   REAL,
        sharpe_30d      REAL,
        sharpe_90d      REAL,
        max_drawdown    REAL,
        active          INTEGER NOT NULL DEFAULT 1,
        UNIQUE(strategy, date)
    )
    """,
    # --- §14.5.1 Télémétrie LLM -------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS llm_usage (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        ts          TEXT NOT NULL,
        agent       TEXT NOT NULL,
        model       TEXT NOT NULL,
        tokens_in   INTEGER NOT NULL,
        tokens_out  INTEGER NOT NULL,
        cost_usd    REAL NOT NULL,
        session_id  TEXT,
        request_ref TEXT,
        duration_ms INTEGER,
        error       TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_llm_usage_ts    ON llm_usage(ts)",
    "CREATE INDEX IF NOT EXISTS idx_llm_usage_agent ON llm_usage(agent)",
    # --- §14.5.1 Télémétrie API -------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS api_usage (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        ts          TEXT NOT NULL,
        source      TEXT NOT NULL,
        kind        TEXT NOT NULL,
        asset       TEXT,
        endpoint    TEXT,
        status      INTEGER,
        latency_ms  INTEGER,
        cached      INTEGER NOT NULL DEFAULT 0,
        cost_usd    REAL DEFAULT 0.0
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_api_usage_ts     ON api_usage(ts)",
    "CREATE INDEX IF NOT EXISTS idx_api_usage_source ON api_usage(source)",
    # --- Orchestrator : audit cycles --------------------------------------
    """
    CREATE TABLE IF NOT EXISTS cycles (
        id              TEXT PRIMARY KEY,
        kind            TEXT NOT NULL,
        started_at      TEXT NOT NULL,
        finished_at     TEXT,
        status          TEXT NOT NULL DEFAULT 'running',
        proposals_count INTEGER DEFAULT 0,
        approved_count  INTEGER DEFAULT 0,
        degradation     TEXT,
        risk_gate_failure_rate REAL,
        report_path     TEXT,
        error           TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_cycles_started_at ON cycles(started_at)",
    # Observations : analyses par actif pour un cycle (append-only)
    """
    CREATE TABLE IF NOT EXISTS observations (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        cycle_id    TEXT NOT NULL REFERENCES cycles(id),
        asset       TEXT NOT NULL,
        strategy    TEXT,
        approved    INTEGER NOT NULL DEFAULT 0,
        payload     TEXT NOT NULL,                      -- JSON sérialisé
        created_at  TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_observations_cycle ON observations(cycle_id)",
    "CREATE INDEX IF NOT EXISTS idx_observations_asset ON observations(asset)",
    # --- Self-improve : patches & rollbacks -------------------------------
    """
    CREATE TABLE IF NOT EXISTS patches (
        id              TEXT PRIMARY KEY,
        proposed_at     TEXT NOT NULL,
        target          TEXT NOT NULL,                -- strategy:breakout_momentum, risk:max_risk_pct...
        kind            TEXT NOT NULL,                -- param_tuning | feature_add | bugfix | ...
        status          TEXT NOT NULL DEFAULT 'proposed', -- proposed|approved|merged|active|rolled_back|expired
        merge_commit    TEXT,
        score           REAL,
        t_stat          REAL,
        sharpe_delta    REAL,
        dsr             REAL,
        metrics_json    TEXT,
        rollback_reason TEXT,
        active          INTEGER NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_patches_status ON patches(status)",
    """
    CREATE TABLE IF NOT EXISTS rollbacks (
        id                      INTEGER PRIMARY KEY AUTOINCREMENT,
        patch_id                TEXT NOT NULL REFERENCES patches(id),
        triggered_at            TEXT NOT NULL,
        reason                  TEXT NOT NULL,
        triggered_by            TEXT,
        metrics_snapshot_json   TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_rollbacks_patch_id ON rollbacks(patch_id)",
]


def init_db(path: str = DB_PATH) -> sqlite3.Connection:
    """Ouvre (ou crée) la DB, applique les migrations idempotentes, renvoie la connexion.

    Idempotent : appeler `init_db` plusieurs fois ne modifie pas le schéma si déjà à jour.
    """
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)

    con = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
    # WAL n'a pas de sens sur :memory: mais SQLite l'accepte silencieusement.
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA synchronous=NORMAL")  # compromis perf / durabilité
    _migrate(con)
    return con


def _migrate(con: sqlite3.Connection) -> None:
    current = con.execute("PRAGMA user_version").fetchone()[0]
    # En V1, toutes les migrations sont CREATE IF NOT EXISTS → on peut toujours les rejouer.
    for stmt in _SCHEMA_STATEMENTS:
        con.execute(stmt)
    if current < SCHEMA_VERSION:
        con.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def close_db(con: sqlite3.Connection) -> None:
    try:
        con.commit()
    except sqlite3.Error:
        pass
    con.close()


__all__ = ["DB_PATH", "SCHEMA_VERSION", "init_db", "close_db"]
