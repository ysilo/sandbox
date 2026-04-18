"""src.scheduler — APScheduler wiring depuis config/schedules.yaml (§15.1)."""
from src.scheduler.loader import (
    NewsWatcherConfig,
    ScheduleJob,
    SchedulesConfig,
    load_schedules,
)
from src.scheduler.runner import (
    PipelineCallable,
    SchedulerHandle,
    build_scheduler,
    install_graceful_shutdown,
)

__all__ = [
    "ScheduleJob",
    "NewsWatcherConfig",
    "SchedulesConfig",
    "load_schedules",
    "SchedulerHandle",
    "PipelineCallable",
    "build_scheduler",
    "install_graceful_shutdown",
]
