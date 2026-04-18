"""
tests/test_memory.py — SQLite migrations, repositories append-only,
markdown exporter, lesson index fallback.

Pas d'accès réseau ni de fichiers `data/memory.db` : on utilise `:memory:`
(sauf pour tester l'écriture atomique du MEMORY.md → tmp_path).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.memory import (
    ExporterConfig,
    HypothesesRepository,
    HypothesisRecord,
    LessonIndex,
    LessonRecord,
    LessonsRepository,
    MarkdownExporter,
    PerformanceMetricsRecord,
    PerformanceRepository,
    RegimeSnapshotRecord,
    RegimeSnapshotsRepository,
    TradeRecord,
    TradesRepository,
    close_db,
    init_db,
    purge_telemetry,
)
from src.memory.repositories import (
    APIUsageRepository,
    CyclesRepository,
    LLMUsageRepository,
    ObservationsRepository,
    PatchesRepository,
    RollbacksRepository,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def con():
    c = init_db(":memory:")
    yield c
    close_db(c)


# ---------------------------------------------------------------------------
# Migrations
# ---------------------------------------------------------------------------


def test_init_db_creates_all_tables(con) -> None:
    cur = con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    tables = {row[0] for row in cur.fetchall()}
    expected = {
        "trades", "lessons", "hypotheses", "regime_snapshots",
        "performance_metrics", "llm_usage", "api_usage",
        "cycles", "observations", "patches", "rollbacks",
    }
    assert expected.issubset(tables), f"manquant : {expected - tables}"


def test_init_db_is_idempotent(con) -> None:
    # Réappliquer le schéma ne doit pas lever
    from src.memory.db import _migrate
    _migrate(con)
    _migrate(con)
    cur = con.execute("PRAGMA user_version")
    assert cur.fetchone()[0] == 1


def test_user_version_set_to_schema_version(con) -> None:
    from src.memory.db import SCHEMA_VERSION
    cur = con.execute("PRAGMA user_version")
    assert cur.fetchone()[0] == SCHEMA_VERSION


# ---------------------------------------------------------------------------
# Trades
# ---------------------------------------------------------------------------


def test_trade_insert_and_close_round_trip(con) -> None:
    repo = TradesRepository(con)
    t = TradeRecord(
        id=TradeRecord.new_id(), asset="RUI.PA", asset_class="equity",
        strategy="ichimoku_trend_following", side="long",
        entry_price=34.50, entry_time="2026-04-17T08:00:00+00:00",
        stop_price=33.80, tp_prices=[35.40, 36.10], size_pct_equity=0.02,
        conviction=0.72, rr_estimated=1.8, catalysts=["breakout"], validated=True,
    )
    repo.insert(t)

    fetched = repo.get(t.id)
    assert fetched is not None
    assert fetched["asset"] == "RUI.PA"
    assert fetched["validated"] == 1
    assert len(repo.list_open()) == 1

    rows = repo.close(
        t.id, exit_price=35.40, exit_time="2026-04-18T15:00:00+00:00",
        pnl_pct=0.026, pnl_usd_fictif=260.0,
    )
    assert rows == 1
    assert repo.list_open() == []
    closed = repo.get(t.id)
    assert closed["status"] == "closed"
    assert closed["exit_price"] == 35.40


def test_trade_close_is_noop_on_already_closed(con) -> None:
    repo = TradesRepository(con)
    t = TradeRecord(
        id="T-CLOSED", asset="X", asset_class="equity", strategy="s",
        side="long", entry_price=1.0, entry_time="t", stop_price=0.9,
        tp_prices=[1.1], size_pct_equity=0.01,
    )
    repo.insert(t)
    repo.close(t.id, exit_price=1.1, exit_time="t+1", pnl_pct=0.1, pnl_usd_fictif=10)
    rows = repo.close(t.id, exit_price=1.2, exit_time="t+2", pnl_pct=0.2, pnl_usd_fictif=20)
    assert rows == 0  # pas de double-close


# ---------------------------------------------------------------------------
# Lessons — append-only : archivage via flag, contenu jamais muté
# ---------------------------------------------------------------------------


def test_lessons_are_append_only(con) -> None:
    repo = LessonsRepository(con)
    rec = LessonRecord(
        id=LessonRecord.new_id(), date="2026-04-17",
        content="FOMC + VIX spike : éviter les breakouts techniques pendant 2h",
        tags=["macro", "FOMC", "risk_off"], confidence=0.85,
    )
    repo.insert(rec)

    # Pas de méthode update_content : l'API ne l'expose pas
    assert not hasattr(repo, "update_content")

    recent = repo.recent(limit=10)
    assert len(recent) == 1
    assert recent[0]["content"] == rec.content

    # Archiver masque la leçon des résultats par défaut
    repo.archive(rec.id)
    assert repo.recent() == []
    assert len(repo.recent(include_archived=True)) == 1


def test_lessons_all_active_excludes_archived(con) -> None:
    repo = LessonsRepository(con)
    for i in range(3):
        repo.insert(LessonRecord(id=f"L-{i:04d}", date="2026-04-17", content=f"L{i}"))
    repo.archive("L-0001")
    active = repo.all_active()
    assert {r["id"] for r in active} == {"L-0000", "L-0002"}


# ---------------------------------------------------------------------------
# Hypotheses : evidence append-only, score clampé dans [0, 1]
# ---------------------------------------------------------------------------


def test_hypothesis_evidence_clamps_score(con) -> None:
    repo = HypothesesRepository(con)
    rec = HypothesisRecord(
        id="H-001", content="breakouts EUR/USD plus probables pendant Asia session",
        started_at="2026-04-01T00:00:00+00:00", bayesian_score=0.9,
    )
    repo.insert(rec)
    # Un +0.5 devrait saturer à 1.0 (pas 1.4)
    repo.add_evidence("H-001", date="2026-04-17", result="confirmed", delta_score=0.5)
    row = con.execute("SELECT bayesian_score, evidence FROM hypotheses WHERE id='H-001'").fetchone()
    assert row[0] == pytest.approx(1.0, abs=1e-9)
    import json
    assert len(json.loads(row[1])) == 1


# ---------------------------------------------------------------------------
# Télémétrie fail-open
# ---------------------------------------------------------------------------


def test_llm_usage_fail_open_on_bad_types(con) -> None:
    repo = LLMUsageRepository(con)
    # Ne doit jamais raise : fail-open
    repo.record(
        agent="technical_analyst", model="claude-sonnet-4-6",
        tokens_in=1000, tokens_out=500, cost_usd=0.0042,
        request_ref="P-0001",
    )
    count = con.execute("SELECT COUNT(*) FROM llm_usage").fetchone()[0]
    assert count == 1


def test_api_usage_record(con) -> None:
    repo = APIUsageRepository(con)
    repo.record(
        source="stooq", kind="prix", asset="RUI.PA",
        endpoint="/q/d/l?s=rui.fr", status=200, latency_ms=145,
    )
    row = con.execute("SELECT source, kind, status FROM api_usage").fetchone()
    assert row == ("stooq", "prix", 200)


def test_purge_telemetry_deletes_old(con) -> None:
    # Insérer une ligne "vieille" (ts < cutoff) puis purger avec retention=0
    con.execute(
        "INSERT INTO llm_usage (ts, agent, model, tokens_in, tokens_out, cost_usd) "
        "VALUES (?, 'a', 'm', 1, 1, 0.001)",
        ("2020-01-01T00:00:00+00:00",),
    )
    con.execute(
        "INSERT INTO api_usage (ts, source, kind) VALUES (?, 'stooq', 'prix')",
        ("2020-01-01T00:00:00+00:00",),
    )
    result = purge_telemetry(con, retention_days=0)
    assert result["llm_usage"] == 1
    assert result["api_usage"] == 1


# ---------------------------------------------------------------------------
# Regime + performance
# ---------------------------------------------------------------------------


def test_regime_upsert_replaces_on_same_date(con) -> None:
    repo = RegimeSnapshotsRepository(con)
    repo.upsert(RegimeSnapshotRecord(
        date="2026-04-17", macro="risk_on", volatility="low",
        prob_risk_on=0.7, prob_transition=0.2, prob_risk_off=0.1, hmm_state=0,
    ))
    repo.upsert(RegimeSnapshotRecord(
        date="2026-04-17", macro="neutral", volatility="mid",
        prob_risk_on=0.3, prob_transition=0.5, prob_risk_off=0.2, hmm_state=1,
    ))
    latest = repo.latest()
    assert latest["macro"] == "neutral"
    count = con.execute("SELECT COUNT(*) FROM regime_snapshots").fetchone()[0]
    assert count == 1                             # upsert, pas d'append


def test_performance_latest_by_strategy(con) -> None:
    repo = PerformanceRepository(con)
    repo.upsert(PerformanceMetricsRecord(
        strategy="ichimoku_trend_following", date="2026-04-15",
        trades_30d=10, winrate_30d=0.55, sharpe_30d=1.2,
    ))
    repo.upsert(PerformanceMetricsRecord(
        strategy="ichimoku_trend_following", date="2026-04-17",
        trades_30d=12, winrate_30d=0.60, sharpe_30d=1.4,
    ))
    repo.upsert(PerformanceMetricsRecord(
        strategy="breakout_momentum", date="2026-04-17",
        trades_30d=8, winrate_30d=0.40, sharpe_30d=0.5, active=False,
    ))
    rows = repo.latest_by_strategy()
    by_strat = {r["strategy"]: r for r in rows}
    assert by_strat["ichimoku_trend_following"]["date"] == "2026-04-17"
    assert by_strat["ichimoku_trend_following"]["winrate_30d"] == pytest.approx(0.60)
    assert by_strat["breakout_momentum"]["active"] == 0


# ---------------------------------------------------------------------------
# Cycles + observations
# ---------------------------------------------------------------------------


def test_cycle_lifecycle_and_observations(con) -> None:
    cycles = CyclesRepository(con)
    obs = ObservationsRepository(con)
    cycles.start(cycle_id="C-001", kind="full_cycle")
    obs.record(cycle_id="C-001", asset="RUI.PA", strategy="ichimoku_trend_following",
               approved=True, payload={"conviction": 0.72})
    obs.record(cycle_id="C-001", asset="TTE.PA", strategy="breakout_momentum",
               approved=False, payload={"conviction": 0.45})
    cycles.finish("C-001", status="ok", proposals_count=2, approved_count=1,
                  degradation=["news_agent_degraded"], risk_gate_failure_rate=0.1)

    rows = obs.for_cycle("C-001")
    assert len(rows) == 2
    approved_rows = [r for r in rows if r["approved"] == 1]
    assert approved_rows[0]["asset"] == "RUI.PA"

    final = cycles.last_n(1)[0]
    assert final["status"] == "ok"
    assert final["proposals_count"] == 2


# ---------------------------------------------------------------------------
# Patches + rollbacks
# ---------------------------------------------------------------------------


def test_patch_rollback_flow(con) -> None:
    patches = PatchesRepository(con)
    rollbacks = RollbacksRepository(con)
    patches.insert(
        patch_id="P-001", target="strategy:breakout_momentum",
        kind="param_tuning", score=0.72, t_stat=2.4, sharpe_delta=0.18, dsr=0.42,
        metrics={"winrate_before": 0.48, "winrate_after": 0.55},
    )
    patches.set_status("P-001", "merged", merge_commit="abc123", active=True)
    p = patches.get("P-001")
    assert p["status"] == "merged"
    assert p["active"] == 1

    rollbacks.record(
        patch_id="P-001", reason="winrate regressed >10% on live data",
        triggered_by="automated_sharpe_monitor",
        metrics_snapshot={"winrate_live": 0.41},
    )
    patches.set_status("P-001", "rolled_back", active=False,
                       rollback_reason="winrate regressed >10%")
    rbs = rollbacks.for_patch("P-001")
    assert len(rbs) == 1
    assert rbs[0]["triggered_by"] == "automated_sharpe_monitor"
    final = patches.get("P-001")
    assert final["status"] == "rolled_back"
    assert final["active"] == 0


# ---------------------------------------------------------------------------
# MarkdownExporter
# ---------------------------------------------------------------------------


def _seed_for_export(con) -> None:
    TradesRepository(con).insert(TradeRecord(
        id="T-OPEN01", asset="RUI.PA", asset_class="equity",
        strategy="ichimoku_trend_following", side="long",
        entry_price=34.5, entry_time="2026-04-17T08:00:00+00:00",
        stop_price=33.8, tp_prices=[35.4, 36.1], size_pct_equity=0.015,
        rr_estimated=1.8,
    ))
    LessonsRepository(con).insert(LessonRecord(
        id="L-0042", date="2026-04-16",
        content="Kumo twist sous SPX → pause les longs techniques",
        tags=["regime", "ichimoku"], confidence=0.9,
    ))
    HypothesesRepository(con).insert(HypothesisRecord(
        id="H-007", content="breakouts forex plus fiables en session asiatique",
        started_at="2026-04-01T00:00:00+00:00", bayesian_score=0.68,
    ))
    RegimeSnapshotsRepository(con).upsert(RegimeSnapshotRecord(
        date="2026-04-17", macro="risk_on", volatility="mid",
        trend_equity="up", trend_forex="range", trend_crypto="up",
        prob_risk_on=0.62, prob_transition=0.28, prob_risk_off=0.10, hmm_state=0,
    ))
    PerformanceRepository(con).upsert(PerformanceMetricsRecord(
        strategy="ichimoku_trend_following", date="2026-04-17",
        trades_30d=14, winrate_30d=0.57, profit_factor=1.4, sharpe_30d=1.1,
        max_drawdown=0.08, active=True,
    ))


def test_markdown_exporter_renders_all_sections(con) -> None:
    _seed_for_export(con)
    md = MarkdownExporter().render(con)
    assert "# MEMORY.md" in md
    assert "T-OPEN01" in md
    assert "RUI.PA" in md
    assert "L-0042" in md
    assert "Kumo twist" in md
    assert "H-007" in md
    assert "risk_on" in md
    assert "ichimoku_trend_following" in md


def test_markdown_exporter_empty_db_renders_gracefully(con) -> None:
    md = MarkdownExporter().render(con)
    assert "Aucun trade ouvert" in md
    assert "Pas encore de leçons enregistrées" in md
    assert "Aucun snapshot de régime" in md


def test_markdown_exporter_export_to_file(con, tmp_path: Path) -> None:
    _seed_for_export(con)
    out = MarkdownExporter(ExporterConfig(max_lessons=5)).export_to_file(
        con, path=tmp_path / "MEMORY.md"
    )
    assert out.exists()
    content = out.read_text(encoding="utf-8")
    assert "T-OPEN01" in content
    # Pas de .tmp laissé
    assert not (tmp_path / "MEMORY.md.tmp").exists()


# ---------------------------------------------------------------------------
# LessonIndex — fallback BOW (les deps FAISS ne sont pas installées en test)
# ---------------------------------------------------------------------------


def test_lesson_index_empty_returns_nothing(con) -> None:
    idx = LessonIndex(backend_preference="bow")
    idx.build_from_rows([])
    hits = idx.query(asset="RUI.PA", regime_tags=["risk_on"], strategy="ichimoku_trend_following")
    assert hits == []


def test_lesson_index_bow_topk_ranking(con) -> None:
    repo = LessonsRepository(con)
    repo.insert(LessonRecord(
        id="L-A", date="2026-04-01",
        content="breakout EURUSD fiable en session asiatique",
        tags=["forex", "breakout_momentum", "asian_session"], confidence=0.8,
    ))
    repo.insert(LessonRecord(
        id="L-B", date="2026-04-02",
        content="kumo twist sous SPX : éviter les longs techniques",
        tags=["equity", "ichimoku_trend_following", "kumo"], confidence=0.7,
    ))
    repo.insert(LessonRecord(
        id="L-C", date="2026-04-03",
        content="crypto range élargi après halving",
        tags=["crypto", "range"], confidence=0.6,
    ))

    idx = LessonIndex(backend_preference="bow")
    idx.build_from_rows(repo.all_active())
    assert idx.size == 3

    hits = idx.query(
        asset="EURUSD", regime_tags=["risk_on"],
        strategy="breakout_momentum", free_text="asian session",
        k=3,
    )
    assert hits, "au moins une leçon attendue"
    # La plus pertinente doit être L-A (tokens match : breakout + asian + forex-ish)
    assert hits[0].lesson_id == "L-A"
    # Scores triés décroissants
    assert all(hits[i].score >= hits[i + 1].score for i in range(len(hits) - 1))


def test_lesson_index_save_and_load_meta(con, tmp_path: Path) -> None:
    repo = LessonsRepository(con)
    repo.insert(LessonRecord(
        id="L-X", date="2026-04-01", content="content X",
        tags=["tag1"], confidence=0.9,
    ))
    idx = LessonIndex(backend_preference="bow")
    idx.build_from_rows(repo.all_active())
    idx.save_meta(tmp_path / "lesson_index.meta.json")

    idx2 = LessonIndex(backend_preference="bow")
    assert idx2.load_meta(tmp_path / "lesson_index.meta.json") is True
    assert idx2.size == 1

    idx3 = LessonIndex(backend_preference="bow")
    assert idx3.load_meta(tmp_path / "does_not_exist.json") is False
