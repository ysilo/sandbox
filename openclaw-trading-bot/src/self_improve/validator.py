"""
src.self_improve.validator — étape 4 du self-improve (§13.2, §13.3.2).

Soumet un `StrategyPatch` au backtest walk-forward et compare ses métriques
aux seuils §13.3.2 :

- `t_stat > 2.0`
- `sharpe_patch > sharpe_baseline`
- `max_dd_patch ≤ max_dd_baseline × 1.10`
- `trade_count ≥ 50`
- `dsr > 0.95`

En V1 le backtest est **stubé** : la pipeline injecte un `BacktestRunner` pour
les tests. Le `StubBacktestRunner` fourni par défaut retourne des métriques
déterministes dérivées du `StrategyPatch` (useful pour les tests et pour
l'exécution end-to-end sans infra).

Interfaces :
- `BacktestRunner.run(patch) -> BacktestResult` (Protocol)
- `validate_patch(patch, runner, thresholds=None) -> PatchValidationResult`
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Protocol

from src.self_improve.patch import StrategyPatch
from src.utils.logging_utils import get_logger


log = get_logger("SelfImprove.Validator")


# ---------------------------------------------------------------------------
# Seuils (§13.3.2)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ValidationThresholds:
    min_t_stat: float = 2.0
    min_sharpe_delta: float = 0.0            # sharpe_patch > sharpe_baseline
    max_dd_multiplier: float = 1.10          # dd_patch ≤ dd_baseline × 1.10
    min_trade_count: int = 50
    min_dsr: float = 0.95


DEFAULT_THRESHOLDS = ValidationThresholds()


# ---------------------------------------------------------------------------
# Résultats
# ---------------------------------------------------------------------------


@dataclass
class BacktestResult:
    """Sortie d'un backtest walk-forward (format attendu par le validator)."""

    sharpe_baseline: float
    sharpe_patch: float
    t_stat: float
    dd_baseline: float                       # max drawdown baseline (≥ 0)
    dd_patch: float                          # max drawdown patched   (≥ 0)
    trade_count: int
    dsr: float                               # Deflated Sharpe Ratio (0..1)
    extra: dict = field(default_factory=dict)


@dataclass
class PatchValidationResult:
    patch_id: str
    passed: bool
    sharpe_baseline: float
    sharpe_patch: float
    t_stat: float
    dd_baseline: float
    dd_patch: float
    trade_count: int
    dsr: float
    recommendation: str                      # "approve" | "reject"
    failures: list[str] = field(default_factory=list)

    @property
    def sharpe_delta(self) -> float:
        return self.sharpe_patch - self.sharpe_baseline


# ---------------------------------------------------------------------------
# Interface injectable
# ---------------------------------------------------------------------------


class BacktestRunner(Protocol):
    def run(self, patch: StrategyPatch) -> BacktestResult:  # pragma: no cover - protocol
        ...


@dataclass
class StubBacktestRunner:
    """Runner déterministe pour les tests et l'exécution V1.

    Construit un `BacktestResult` à partir du contenu du patch. Le comportement
    est paramétrable via `passing` (True → métriques qui passent les seuils).
    """

    passing: bool = True
    sharpe_baseline: float = 1.0
    sharpe_delta_ok: float = 0.35
    sharpe_delta_ko: float = -0.15
    dd_baseline: float = 0.08
    t_stat_ok: float = 2.5
    t_stat_ko: float = 1.2
    dsr_ok: float = 0.97
    dsr_ko: float = 0.60
    trade_count_ok: int = 120
    trade_count_ko: int = 20

    def run(self, patch: StrategyPatch) -> BacktestResult:
        if self.passing:
            return BacktestResult(
                sharpe_baseline=self.sharpe_baseline,
                sharpe_patch=self.sharpe_baseline + self.sharpe_delta_ok,
                t_stat=self.t_stat_ok,
                dd_baseline=self.dd_baseline,
                dd_patch=self.dd_baseline * 0.95,
                trade_count=self.trade_count_ok,
                dsr=self.dsr_ok,
                extra={"stub": True, "patch_id": patch.patch_id},
            )
        return BacktestResult(
            sharpe_baseline=self.sharpe_baseline,
            sharpe_patch=self.sharpe_baseline + self.sharpe_delta_ko,
            t_stat=self.t_stat_ko,
            dd_baseline=self.dd_baseline,
            dd_patch=self.dd_baseline * 1.5,
            trade_count=self.trade_count_ko,
            dsr=self.dsr_ko,
            extra={"stub": True, "patch_id": patch.patch_id},
        )


# ---------------------------------------------------------------------------
# Validateur
# ---------------------------------------------------------------------------


def _check_thresholds(
    result: BacktestResult,
    thresholds: ValidationThresholds,
) -> list[str]:
    failures: list[str] = []
    if result.t_stat <= thresholds.min_t_stat:
        failures.append(
            f"t_stat={result.t_stat:.2f} ≤ {thresholds.min_t_stat:.2f}"
        )
    sharpe_delta = result.sharpe_patch - result.sharpe_baseline
    if sharpe_delta <= thresholds.min_sharpe_delta:
        failures.append(
            f"sharpe_delta={sharpe_delta:.2f} ≤ {thresholds.min_sharpe_delta:.2f}"
        )
    if result.dd_patch > result.dd_baseline * thresholds.max_dd_multiplier:
        failures.append(
            f"max_dd_patch={result.dd_patch:.3f} > "
            f"{result.dd_baseline:.3f} × {thresholds.max_dd_multiplier}"
        )
    if result.trade_count < thresholds.min_trade_count:
        failures.append(
            f"trade_count={result.trade_count} < {thresholds.min_trade_count}"
        )
    if result.dsr < thresholds.min_dsr:
        failures.append(f"dsr={result.dsr:.3f} < {thresholds.min_dsr:.3f}")
    return failures


def validate_patch(
    patch: StrategyPatch,
    *,
    runner: Optional[BacktestRunner] = None,
    thresholds: Optional[ValidationThresholds] = None,
) -> PatchValidationResult:
    """Étape 4 du self-improve : backtest + check §13.3.2."""
    runner = runner or StubBacktestRunner()
    thresholds = thresholds or DEFAULT_THRESHOLDS

    result = runner.run(patch)
    failures = _check_thresholds(result, thresholds)
    passed = len(failures) == 0

    val = PatchValidationResult(
        patch_id=patch.patch_id,
        passed=passed,
        sharpe_baseline=result.sharpe_baseline,
        sharpe_patch=result.sharpe_patch,
        t_stat=result.t_stat,
        dd_baseline=result.dd_baseline,
        dd_patch=result.dd_patch,
        trade_count=result.trade_count,
        dsr=result.dsr,
        recommendation="approve" if passed else "reject",
        failures=failures,
    )
    log.info(
        "validation_done",
        patch_id=patch.patch_id,
        passed=passed,
        failures=len(failures),
    )
    return val


__all__ = [
    "ValidationThresholds",
    "DEFAULT_THRESHOLDS",
    "BacktestResult",
    "BacktestRunner",
    "StubBacktestRunner",
    "PatchValidationResult",
    "validate_patch",
]
