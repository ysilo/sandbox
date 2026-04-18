"""
tests/test_scheduler.py — loader + runner APScheduler (§15.1).

Couvre :
- Loader : parse cycles + maintenance + news_watcher
- Compat `sessions` (ancien format) → mappé vers `cycles`
- Cron invalide → ValueError
- Job sans name/pipeline/cron → ValueError
- Duplicates name (cycles + maintenance) → ValueError
- Runner : build_scheduler enregistre les jobs + ids corrects
- Runner : pipeline manquant → KeyError explicite
- Runner : 5 et 6 champs cron supportés
- Runner : shutdown sans erreur
"""
from __future__ import annotations

import pytest
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.schedulers.blocking import BlockingScheduler

from src.scheduler.loader import (
    NewsWatcherConfig,
    ScheduleJob,
    SchedulesConfig,
    load_schedules,
)
from src.scheduler.runner import (
    _cron_to_trigger,
    build_scheduler,
)


# ---------------------------------------------------------------------------
# Loader — parsing
# ---------------------------------------------------------------------------


def test_load_full_config():
    raw = {
        "cycles": [
            {"name": "open_eu", "cron": "0 7 * * 1-5",
             "pipeline": "full_cycle", "markets": ["forex", "equity"]},
        ],
        "maintenance": [
            {"name": "pnl_update", "cron": "*/30 * * * *",
             "pipeline": "update_open_trades"},
        ],
        "news_watcher": {
            "enabled": True,
            "poll_seconds": 30,
            "impact_threshold_adhoc": 0.75,
        },
    }
    cfg = load_schedules(raw)
    assert len(cfg.cycles) == 1
    assert len(cfg.maintenance) == 1
    assert cfg.cycles[0].markets == ["forex", "equity"]
    assert cfg.news_watcher.enabled is True
    assert cfg.news_watcher.poll_seconds == 30
    assert cfg.news_watcher.impact_threshold_adhoc == 0.75


def test_load_legacy_sessions_maps_to_cycles():
    # Ancien format avec `sessions` → doit encore fonctionner
    raw = {
        "sessions": [
            {"name": "pre_europe", "cron": "0 6 * * 1-5",
             "pipeline": "full_analysis"},
        ],
    }
    cfg = load_schedules(raw)
    assert len(cfg.cycles) == 1
    assert cfg.cycles[0].name == "pre_europe"


def test_load_empty_news_watcher_defaults_disabled():
    cfg = load_schedules({})
    assert cfg.news_watcher.enabled is False
    assert cfg.cycles == []
    assert cfg.maintenance == []


def test_invalid_cron_raises():
    raw = {"cycles": [{"name": "bad", "cron": "not a cron", "pipeline": "x"}]}
    with pytest.raises(ValueError, match="CFG_006"):
        load_schedules(raw)


def test_missing_name_raises():
    raw = {"cycles": [{"cron": "0 0 * * *", "pipeline": "p"}]}
    with pytest.raises(ValueError, match="CFG_005"):
        load_schedules(raw)


def test_missing_pipeline_raises():
    raw = {"cycles": [{"name": "x", "cron": "0 0 * * *"}]}
    with pytest.raises(ValueError, match="CFG_005"):
        load_schedules(raw)


def test_duplicate_names_raise():
    raw = {
        "cycles": [{"name": "dup", "cron": "0 0 * * *", "pipeline": "a"}],
        "maintenance": [{"name": "dup", "cron": "0 1 * * *", "pipeline": "b"}],
    }
    with pytest.raises(ValueError, match="CFG_008"):
        load_schedules(raw)


def test_cycles_must_be_list():
    raw = {"cycles": {"oops": "dict"}}
    with pytest.raises(ValueError, match="CFG_007"):
        load_schedules(raw)


def test_loader_loads_real_config_yaml():
    """Lecture du vrai `config/schedules.yaml` du projet."""
    cfg = load_schedules()
    assert len(cfg.cycles) >= 1
    # Chaque nom doit être unique
    names = [j.name for j in cfg.all_jobs]
    assert len(names) == len(set(names))


# ---------------------------------------------------------------------------
# _cron_to_trigger
# ---------------------------------------------------------------------------


def test_cron_5_fields_ok():
    trig = _cron_to_trigger("0 7 * * 1-5")
    assert trig is not None


def test_cron_6_fields_ok():
    trig = _cron_to_trigger("0 0 7 * * *")
    assert trig is not None


def test_cron_invalid_shape_raises():
    with pytest.raises(ValueError, match="CFG_006"):
        _cron_to_trigger("7")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def _cfg_for_runner() -> SchedulesConfig:
    return SchedulesConfig(
        cycles=[
            ScheduleJob(name="open_eu", cron="0 7 * * 1-5",
                        pipeline="full_cycle", markets=["equity"]),
            ScheduleJob(name="crypto_6h", cron="0 */6 * * *",
                        pipeline="crypto_quick_scan", markets=["crypto"]),
        ],
        maintenance=[
            ScheduleJob(name="pnl_update", cron="*/30 * * * *",
                        pipeline="update_open_trades", kind="maintenance"),
        ],
        news_watcher=NewsWatcherConfig(enabled=False),
    )


def test_build_scheduler_registers_all_jobs():
    called: dict[str, int] = {}

    def fp_full():
        called["full"] = called.get("full", 0) + 1

    def fp_crypto():
        called["crypto"] = called.get("crypto", 0) + 1

    def fp_pnl():
        called["pnl"] = called.get("pnl", 0) + 1

    handle = build_scheduler(
        config=_cfg_for_runner(),
        pipelines={
            "full_cycle": fp_full,
            "crypto_quick_scan": fp_crypto,
            "update_open_trades": fp_pnl,
        },
    )
    try:
        assert isinstance(handle.scheduler, BackgroundScheduler)
        ids = handle.job_ids()
        assert set(ids) == {"open_eu", "crypto_6h", "pnl_update"}
    finally:
        handle.shutdown(wait=False)


def test_build_scheduler_missing_pipeline_raises():
    with pytest.raises(KeyError, match="SCHED_001"):
        build_scheduler(
            config=_cfg_for_runner(),
            pipelines={"full_cycle": lambda: None},   # manquants
        )


def test_build_scheduler_blocking_mode():
    handle = build_scheduler(
        config=_cfg_for_runner(),
        pipelines={
            "full_cycle": lambda: None,
            "crypto_quick_scan": lambda: None,
            "update_open_trades": lambda: None,
        },
        blocking=True,
    )
    try:
        assert isinstance(handle.scheduler, BlockingScheduler)
    finally:
        handle.shutdown(wait=False)


def test_scheduler_handle_start_guard_for_blocking():
    handle = build_scheduler(
        config=_cfg_for_runner(),
        pipelines={
            "full_cycle": lambda: None,
            "crypto_quick_scan": lambda: None,
            "update_open_trades": lambda: None,
        },
        blocking=True,
    )
    try:
        # start(blocking=False) sur un BlockingScheduler → RuntimeError
        with pytest.raises(RuntimeError, match="blocking"):
            handle.start(blocking=False)
    finally:
        handle.shutdown(wait=False)


def test_scheduler_shutdown_without_start_is_noop():
    handle = build_scheduler(
        config=_cfg_for_runner(),
        pipelines={
            "full_cycle": lambda: None,
            "crypto_quick_scan": lambda: None,
            "update_open_trades": lambda: None,
        },
    )
    # Doit pouvoir appeler shutdown deux fois sans exception
    handle.shutdown(wait=False)
    handle.shutdown(wait=False)


def test_scheduler_empty_config_no_jobs():
    empty = SchedulesConfig(cycles=[], maintenance=[],
                            news_watcher=NewsWatcherConfig())
    handle = build_scheduler(config=empty, pipelines={})
    try:
        assert handle.job_ids() == []
    finally:
        handle.shutdown(wait=False)
