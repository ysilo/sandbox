"""src.self_improve — pipeline hebdomadaire §13.

Stubs V1 — le module est conçu pour être exercé end-to-end sans infra réelle :
- aucun appel LLM (diagnostic & patch sont des heuristiques pures),
- aucun backtest walk-forward réel (`StubBacktestRunner` par défaut),
- aucun git revert réel (`NoOpGitBackend` par défaut).

Les interfaces (`BacktestRunner`, `GitBackend`) sont prêtes à accueillir des
implémentations réelles en V2 sans changer le code applicatif.
"""
from src.self_improve.collector import (
    CollectedDataset,
    DEFAULT_LOSER_PNL_PCT,
    collect_closed_trades,
)
from src.self_improve.diagnostician import (
    DiagnosedPattern,
    DiagnosisReport,
    diagnose,
)
from src.self_improve.patch import StrategyPatch, generate_patch
from src.self_improve.pipeline import PipelineResult, run_self_improve
from src.self_improve.pr_generator import (
    DEFAULT_PATH as DEFAULT_PENDING_PATH,
    write_improvements_pending,
)
from src.self_improve.rollback import (
    GitBackend,
    NoOpGitBackend,
    RollbackOutcome,
    VALID_REASONS,
    rollback_patch,
)
from src.self_improve.selector import (
    BLACKLIST_EXACT,
    BLACKLIST_PREFIXES,
    ScoredPatch,
    SelectionResult,
    select_patches,
)
from src.self_improve.validator import (
    BacktestResult,
    BacktestRunner,
    DEFAULT_THRESHOLDS,
    PatchValidationResult,
    StubBacktestRunner,
    ValidationThresholds,
    validate_patch,
)


__all__ = [
    # collector
    "CollectedDataset", "collect_closed_trades", "DEFAULT_LOSER_PNL_PCT",
    # diagnostician
    "DiagnosedPattern", "DiagnosisReport", "diagnose",
    # patch
    "StrategyPatch", "generate_patch",
    # validator
    "BacktestResult", "BacktestRunner", "StubBacktestRunner",
    "PatchValidationResult", "ValidationThresholds", "DEFAULT_THRESHOLDS",
    "validate_patch",
    # selector
    "BLACKLIST_EXACT", "BLACKLIST_PREFIXES", "ScoredPatch",
    "SelectionResult", "select_patches",
    # pr generator
    "write_improvements_pending", "DEFAULT_PENDING_PATH",
    # rollback
    "GitBackend", "NoOpGitBackend", "RollbackOutcome", "VALID_REASONS",
    "rollback_patch",
    # pipeline
    "PipelineResult", "run_self_improve",
]
