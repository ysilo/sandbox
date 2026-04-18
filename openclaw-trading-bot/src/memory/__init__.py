"""
src.memory — SQLite WAL, repositories append-only, exporter MEMORY.md,
retrieval FAISS.

Source : TRADING_BOT_ARCHITECTURE.md §10.

Usage typique :
    from src.memory import init_db, TradesRepository, MarkdownExporter, LessonIndex

    con = init_db()  # data/memory.db par défaut, WAL + foreign_keys ON
    trades = TradesRepository(con)
    trades.insert(TradeRecord(...))

    MarkdownExporter().export_to_file(con)         # régénère data/MEMORY.md

    idx = LessonIndex()
    idx.build_from_rows(LessonsRepository(con).all_active())
    hits = idx.query(asset="EURUSD", regime_tags=["risk_on"],
                     strategy="breakout_momentum")
"""
from __future__ import annotations

from .db import DB_PATH, SCHEMA_VERSION, close_db, init_db
from .lesson_index import LessonHit, LessonIndex
from .markdown_exporter import ExporterConfig, MEMORY_MD_PATH, MarkdownExporter
from .repositories import (
    APIUsageRepository,
    CyclesRepository,
    HypothesesRepository,
    HypothesisRecord,
    LessonRecord,
    LessonsRepository,
    LLMUsageRepository,
    ObservationsRepository,
    PatchesRepository,
    PerformanceMetricsRecord,
    PerformanceRepository,
    RegimeSnapshotRecord,
    RegimeSnapshotsRepository,
    RollbacksRepository,
    TradeRecord,
    TradesRepository,
    purge_telemetry,
)

__all__ = [
    # db
    "init_db", "close_db", "DB_PATH", "SCHEMA_VERSION",
    # records
    "TradeRecord", "LessonRecord", "HypothesisRecord",
    "RegimeSnapshotRecord", "PerformanceMetricsRecord",
    # repositories
    "TradesRepository", "LessonsRepository", "HypothesesRepository",
    "RegimeSnapshotsRepository", "PerformanceRepository",
    "LLMUsageRepository", "APIUsageRepository",
    "CyclesRepository", "ObservationsRepository",
    "PatchesRepository", "RollbacksRepository",
    "purge_telemetry",
    # markdown
    "MarkdownExporter", "ExporterConfig", "MEMORY_MD_PATH",
    # retrieval
    "LessonIndex", "LessonHit",
]
