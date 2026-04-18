"""
src.risk.gate — 10 contrôles déterministes C1→C10 (§11.6).

Contrat (§8.8.1) :
- Entrée  : `TradeProposal` + `GateContext` (état portefeuille + signaux macro + DQ).
- Sortie  : `RiskDecision` (Pydantic) avec `checks` **toujours** 10 entrées ordonnées.
- Short-circuit : dès qu'un check `blocking` échoue, on interrompt. Les checks
  non évalués sont ajoutés par `_pad_checks()` avec `evaluated=False`.
- Fail-closed : toute exception dans un check devient un rejet (severity=blocking,
  reason="exception: ...").

Le gate ne lit aucun YAML — il prend un `GateConfig` résolu par l'orchestrateur
à partir de `config/risk.yaml`. Cela rend les tests simples (on construit
exactement le contexte qu'on veut vérifier).
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional

from src.contracts.skills import (
    CHECK_IDS,
    RiskCheckResult,
    RiskDecision,
    _pad_checks,
)
from src.contracts.strategy import TradeProposal

from src.risk import ichimoku_gate
from src.risk.circuit_breaker import CircuitBreaker, CircuitState
from src.risk.kill_switch import KillSwitch

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Contexte d'évaluation
# ---------------------------------------------------------------------------


def _estimate_notional_pct(p: TradeProposal) -> float:
    """Notionnel estimé en % équité à partir de risk_pct et de la distance au stop.

    Formule standard de position sizing :
        notional_pct = (risk_pct / stop_distance) × 100

    - `risk_pct` est une proportion (0.0075 = 0.75 % d'équité au risque).
    - `stop_distance` est la fraction d'écart entre entry et stop (0.02 = 2 %).

    Edge cases :
    - Si `stop` est collé à `entry` (stop_distance ≤ 0) : on retombe sur le
      proxy `risk_pct × 100`. C'est ultra-conservateur et n'arrivera jamais
      avec un build_proposal valide (qui refuse ce cas en amont).
    - Si `entry <= 0` : retourne 0 (données corrompues).
    """
    entry = float(p.entry_price)
    stop = float(p.stop_price)
    if entry <= 0:
        return 0.0
    stop_distance = abs(entry - stop) / entry
    if stop_distance <= 0:
        return float(p.risk_pct) * 100.0
    return (float(p.risk_pct) / stop_distance) * 100.0


@dataclass
class PositionSnapshot:
    """Position ouverte — représentation minimale pour les contrôles C3/C4/C8."""

    asset: str
    asset_class: str                      # "equity" | "forex" | "crypto"
    side: str                             # "long" | "short"
    risk_pct: float                       # % équité engagée au risque
    notional_pct: float = 0.0             # % équité de notionnel (pour C4)
    strategy_id: str = ""


@dataclass
class PortfolioState:
    equity: float
    daily_pnl_pct: float                  # signé : -1.5 = -1.5 %
    open_positions: list[PositionSnapshot] = field(default_factory=list)


@dataclass
class MacroState:
    vix: Optional[float] = None
    hmm_regime: str = "transition"
    hmm_confidence: float = 0.0


@dataclass
class DataQualityState:
    is_fresh: bool = True
    has_outliers: bool = False
    used_fallback_source: bool = False    # True si la source utilisée = dernier fallback


@dataclass
class TokenBudgetState:
    tokens_used_today: int = 0
    monthly_cost_usd: float = 0.0
    estimated_tokens: int = 0             # tokens nécessaires pour ce cycle


@dataclass
class GateConfig:
    """Seuils du risk-gate — résolus depuis `config/risk.yaml` par l'orchestrateur."""

    max_daily_loss_pct_equity: float = 2.0
    max_open_positions_total: int = 8
    max_open_per_class: dict[str, int] = field(
        default_factory=lambda: {"forex": 3, "crypto": 3, "equity": 4},
    )
    max_exposure_pct_per_class: float = 40.0         # en %
    circuit_breaker_threshold_ratio: float = 2.0
    circuit_breaker_min_history_days: int = 30
    max_correlated_exposure_pct: float = 20.0        # %
    correlation_rho_threshold: float = 0.7
    max_daily_tokens: int = 50000
    max_monthly_cost_usd: float = 15.0
    macro_vix_cap: float = 35.0
    macro_hmm_confidence_min: float = 0.80
    warn_only: list[str] = field(default_factory=list)


@dataclass
class GateContext:
    """Tout ce dont le gate a besoin pour évaluer les 10 checks."""

    portfolio: PortfolioState
    tokens: TokenBudgetState
    macro: MacroState
    data_quality: DataQualityState
    strategies_cfg: dict                           # dict[id→StrategyConfig] ou yaml brut
    # Précomputé par l'orchestrateur : max |ρ| asset ↔ positions ouvertes sur 60j.
    correlation_to_portfolio: dict[str, float] = field(default_factory=dict)
    # Callable optionnelle pour C5 (plus simple à mocker en test que sqlite).
    circuit_breaker_lookup: Optional[Callable[[str], CircuitState]] = None


# ---------------------------------------------------------------------------
# RiskGate
# ---------------------------------------------------------------------------


class RiskGate:
    """Exécute les 10 contrôles dans l'ordre et produit un `RiskDecision`.

    Usage :
        gate = RiskGate(
            config=GateConfig(),
            kill_switch=KillSwitch(),
            circuit_breaker=CircuitBreaker(),
            db_conn=conn,
        )
        decision = gate.evaluate(proposal, ctx)
    """

    def __init__(
        self,
        *,
        config: GateConfig,
        kill_switch: Optional[KillSwitch] = None,
        circuit_breaker: Optional[CircuitBreaker] = None,
        db_conn: Optional[sqlite3.Connection] = None,
    ) -> None:
        self.config = config
        self.kill_switch = kill_switch or KillSwitch()
        self.circuit_breaker = circuit_breaker or CircuitBreaker(
            threshold_ratio=config.circuit_breaker_threshold_ratio,
            min_history_days=config.circuit_breaker_min_history_days,
        )
        self.db_conn = db_conn

    # ------------------------------------------------------------------
    # Entrée principale
    # ------------------------------------------------------------------

    def evaluate(self, proposal: TradeProposal, ctx: GateContext) -> RiskDecision:
        checks: list[RiskCheckResult] = []
        reasons: list[str] = []

        for cid, fn in self._check_pipeline():
            result = self._run_check(cid, fn, proposal, ctx)
            checks.append(result)
            if not result.passed and self._is_blocking(cid, result):
                reasons.append(f"{cid}: {result.reason}")
                # Short-circuit : on stoppe ici ; _pad_checks remplira le reste.
                return RiskDecision.reject(
                    proposal_id=proposal.proposal_id,
                    reasons=reasons,
                    checks=checks,
                )
            if not result.passed:
                # warn_only : on log mais on continue.
                reasons.append(f"{cid} (warn): {result.reason}")

        # Tous les checks évalués, aucun blocking failed.
        adjusted = min(max(float(proposal.risk_pct), 0.0), 1.0)
        approved = all(
            c.passed for c in checks
            if c.severity == "blocking" and c.evaluated
        )
        if approved:
            return RiskDecision.approve(
                proposal_id=proposal.proposal_id,
                checks=checks,
                adjusted_size_pct=adjusted,
            )
        # En théorie impossible (on aurait short-circuit), mais on reste fail-closed.
        return RiskDecision.reject(
            proposal_id=proposal.proposal_id,
            reasons=reasons or ["unknown"],
            checks=checks,
        )

    # ------------------------------------------------------------------
    # Pipeline des checks
    # ------------------------------------------------------------------

    def _check_pipeline(self) -> Iterable[tuple[str, Callable]]:
        return (
            ("C1_kill_switch",        self._c1_kill_switch),
            ("C2_daily_loss",         self._c2_daily_loss),
            ("C3_max_open_positions", self._c3_max_open_positions),
            ("C4_exposure_per_class", self._c4_exposure_per_class),
            ("C5_circuit_breaker",    self._c5_circuit_breaker),
            ("C6_ichimoku_alignment", self._c6_ichimoku_alignment),
            ("C7_token_budget",       self._c7_token_budget),
            ("C8_correlation_cap",    self._c8_correlation_cap),
            ("C9_macro_volatility",   self._c9_macro_volatility),
            ("C10_data_quality",      self._c10_data_quality),
        )

    def _run_check(
        self,
        cid: str,
        fn: Callable,
        proposal: TradeProposal,
        ctx: GateContext,
    ) -> RiskCheckResult:
        try:
            return fn(proposal, ctx)
        except Exception as exc:  # noqa: BLE001 — fail-closed wrapper
            log.exception("Exception dans %s : %s", cid, exc)
            return RiskCheckResult(
                check_id=cid,  # type: ignore[arg-type]
                passed=False,
                severity="blocking",
                reason=f"exception: {type(exc).__name__}: {exc}",
            )

    def _is_blocking(self, cid: str, result: RiskCheckResult) -> bool:
        if cid in self.config.warn_only:
            return False
        return result.severity == "blocking"

    # ------------------------------------------------------------------
    # Checks individuels
    # ------------------------------------------------------------------

    def _severity_for(self, cid: str) -> str:
        return "warn" if cid in self.config.warn_only else "blocking"

    def _c1_kill_switch(self, _p: TradeProposal, _ctx: GateContext) -> RiskCheckResult:
        active = self.kill_switch.is_active()
        # Lire la reason une seule fois (évite race si le fichier disparaît
        # entre is_active et reason).
        why = self.kill_switch.reason().strip() if active else ""
        return RiskCheckResult(
            check_id="C1_kill_switch",
            passed=not active,
            severity=self._severity_for("C1_kill_switch"),
            reason=(
                f"kill-switch actif : {why or 'no reason logged'}"
                if active else "kill-switch inactif"
            ),
        )

    def _c2_daily_loss(self, _p: TradeProposal, ctx: GateContext) -> RiskCheckResult:
        # max_daily_loss_pct_equity est un seuil positif (e.g. 2.0 %). La perte
        # quotidienne est signée négative ; on compare |pnl| au seuil si pnl < 0.
        threshold = self.config.max_daily_loss_pct_equity
        pnl = ctx.portfolio.daily_pnl_pct
        breached = pnl <= -abs(threshold)
        return RiskCheckResult(
            check_id="C2_daily_loss",
            passed=not breached,
            severity=self._severity_for("C2_daily_loss"),
            reason=(
                f"perte J {pnl:.2f} % ≤ -{threshold:.2f} % : limite atteinte"
                if breached else f"perte J {pnl:.2f} % > -{threshold:.2f} % : OK"
            ),
        )

    def _c3_max_open_positions(self, p: TradeProposal, ctx: GateContext) -> RiskCheckResult:
        total_open = len(ctx.portfolio.open_positions)
        total_cap = self.config.max_open_positions_total
        per_class_cap = self.config.max_open_per_class.get(p.asset_class, 99)
        per_class_open = sum(
            1 for pos in ctx.portfolio.open_positions if pos.asset_class == p.asset_class
        )
        if total_open >= total_cap:
            return RiskCheckResult(
                check_id="C3_max_open_positions",
                passed=False,
                severity=self._severity_for("C3_max_open_positions"),
                reason=f"portefeuille saturé : {total_open}/{total_cap} positions ouvertes",
            )
        if per_class_open >= per_class_cap:
            return RiskCheckResult(
                check_id="C3_max_open_positions",
                passed=False,
                severity=self._severity_for("C3_max_open_positions"),
                reason=(
                    f"classe {p.asset_class} saturée : "
                    f"{per_class_open}/{per_class_cap} ouvertes"
                ),
            )
        return RiskCheckResult(
            check_id="C3_max_open_positions",
            passed=True,
            severity=self._severity_for("C3_max_open_positions"),
            reason=(
                f"OK : {total_open}/{total_cap} total, "
                f"{per_class_open}/{per_class_cap} {p.asset_class}"
            ),
        )

    def _c4_exposure_per_class(self, p: TradeProposal, ctx: GateContext) -> RiskCheckResult:
        cap = self.config.max_exposure_pct_per_class
        # Notionnel actuel de la classe.
        current = sum(
            pos.notional_pct for pos in ctx.portfolio.open_positions
            if pos.asset_class == p.asset_class
        )
        # Notionnel de la proposition : si build_proposal a rempli le champ
        # `notional_pct`, on l'utilise ; sinon on le reconstruit depuis risk_pct
        # et la distance au stop (formule standard §9).
        proposal_notional = getattr(p, "notional_pct", None)
        if proposal_notional is None or proposal_notional <= 0:
            proposal_notional = _estimate_notional_pct(p)
        projected = current + float(proposal_notional)
        breached = projected > cap
        return RiskCheckResult(
            check_id="C4_exposure_per_class",
            passed=not breached,
            severity=self._severity_for("C4_exposure_per_class"),
            reason=(
                f"exposition {p.asset_class} projetée {projected:.1f} % > cap {cap:.1f} %"
                if breached else
                f"exposition {p.asset_class} projetée {projected:.1f} % ≤ cap {cap:.1f} %"
            ),
        )

    def _c5_circuit_breaker(self, p: TradeProposal, ctx: GateContext) -> RiskCheckResult:
        # Priorité : lookup injecté > DB connection
        state: CircuitState
        if ctx.circuit_breaker_lookup is not None:
            state = ctx.circuit_breaker_lookup(p.strategy_id)
        elif self.db_conn is not None:
            result = self.circuit_breaker.check_strategy(p.strategy_id, self.db_conn)
            state = result.state
        else:
            # Pas de source → on considère CLOSED (fail-open pragmatique pour C5).
            state = CircuitState.INSUFFICIENT_DATA

        if state is CircuitState.TRIPPED:
            return RiskCheckResult(
                check_id="C5_circuit_breaker",
                passed=False,
                severity=self._severity_for("C5_circuit_breaker"),
                reason=f"circuit breaker TRIPPED pour stratégie {p.strategy_id}",
            )
        return RiskCheckResult(
            check_id="C5_circuit_breaker",
            passed=True,
            severity=self._severity_for("C5_circuit_breaker"),
            reason=f"circuit {state.value} pour {p.strategy_id}",
        )

    def _c6_ichimoku_alignment(self, p: TradeProposal, ctx: GateContext) -> RiskCheckResult:
        res = ichimoku_gate.check(p, ctx.strategies_cfg)
        return RiskCheckResult(
            check_id="C6_ichimoku_alignment",
            passed=res.ok,
            severity=self._severity_for("C6_ichimoku_alignment"),
            reason=res.reason,
        )

    def _c7_token_budget(self, _p: TradeProposal, ctx: GateContext) -> RiskCheckResult:
        used = ctx.tokens.tokens_used_today + ctx.tokens.estimated_tokens
        if used > self.config.max_daily_tokens:
            return RiskCheckResult(
                check_id="C7_token_budget",
                passed=False,
                severity=self._severity_for("C7_token_budget"),
                reason=(
                    f"budget journalier dépassé : {used} > "
                    f"{self.config.max_daily_tokens} tokens"
                ),
            )
        if ctx.tokens.monthly_cost_usd >= self.config.max_monthly_cost_usd:
            return RiskCheckResult(
                check_id="C7_token_budget",
                passed=False,
                severity=self._severity_for("C7_token_budget"),
                reason=(
                    f"budget mensuel dépassé : ${ctx.tokens.monthly_cost_usd:.2f} ≥ "
                    f"${self.config.max_monthly_cost_usd:.2f}"
                ),
            )
        return RiskCheckResult(
            check_id="C7_token_budget",
            passed=True,
            severity=self._severity_for("C7_token_budget"),
            reason=(
                f"OK : {used}/{self.config.max_daily_tokens} tokens, "
                f"${ctx.tokens.monthly_cost_usd:.2f}/${self.config.max_monthly_cost_usd:.2f}"
            ),
        )

    def _c8_correlation_cap(self, p: TradeProposal, ctx: GateContext) -> RiskCheckResult:
        rho_threshold = self.config.correlation_rho_threshold
        cap_pct = self.config.max_correlated_exposure_pct
        # Somme du notionnel des positions corrélées (|ρ| > seuil) + proposition.
        correlated_notional = 0.0
        asset_rho = ctx.correlation_to_portfolio.get(p.asset, 0.0)
        # Identifier les positions corrélées via la matrice fournie (clé=asset).
        for pos in ctx.portfolio.open_positions:
            rho = ctx.correlation_to_portfolio.get(pos.asset, 0.0)
            if abs(rho) > rho_threshold:
                correlated_notional += pos.notional_pct

        projected = correlated_notional
        if abs(asset_rho) > rho_threshold:
            proposal_notional = getattr(p, "notional_pct", None)
            if proposal_notional is None or proposal_notional <= 0:
                proposal_notional = _estimate_notional_pct(p)
            projected += float(proposal_notional)

        breached = projected > cap_pct
        return RiskCheckResult(
            check_id="C8_correlation_cap",
            passed=not breached,
            severity=self._severity_for("C8_correlation_cap"),
            reason=(
                f"exposition corrélée (|ρ|>{rho_threshold}) projetée "
                f"{projected:.1f} % > cap {cap_pct:.1f} %"
                if breached else
                f"exposition corrélée projetée {projected:.1f} % ≤ cap {cap_pct:.1f} %"
            ),
        )

    def _c9_macro_volatility(self, _p: TradeProposal, ctx: GateContext) -> RiskCheckResult:
        vix = ctx.macro.vix
        vix_cap = self.config.macro_vix_cap
        hmm_conf_min = self.config.macro_hmm_confidence_min
        # Fail si VIX > cap ET HMM risk_off ET confidence > seuil.
        crash_mode = (
            vix is not None
            and vix > vix_cap
            and ctx.macro.hmm_regime == "risk_off"
            and ctx.macro.hmm_confidence > hmm_conf_min
        )
        return RiskCheckResult(
            check_id="C9_macro_volatility",
            passed=not crash_mode,
            severity=self._severity_for("C9_macro_volatility"),
            reason=(
                f"crash mode : VIX={vix} > {vix_cap} + HMM=risk_off "
                f"conf={ctx.macro.hmm_confidence:.2f} > {hmm_conf_min}"
                if crash_mode else
                f"macro OK (VIX={vix}, HMM={ctx.macro.hmm_regime}, "
                f"conf={ctx.macro.hmm_confidence:.2f})"
            ),
        )

    def _c10_data_quality(self, _p: TradeProposal, ctx: GateContext) -> RiskCheckResult:
        dq = ctx.data_quality
        problems: list[str] = []
        if not dq.is_fresh:
            problems.append("is_fresh=False")
        if dq.has_outliers:
            problems.append("has_outliers=True")
        if dq.used_fallback_source:
            problems.append("source=dernier_fallback")
        return RiskCheckResult(
            check_id="C10_data_quality",
            passed=not problems,
            severity=self._severity_for("C10_data_quality"),
            reason=(
                "data quality dégradée : " + ", ".join(problems)
                if problems else "data quality OK"
            ),
        )


# ---------------------------------------------------------------------------
# Factory utilitaire — construit un GateContext minimal pour tests/orchestrator
# ---------------------------------------------------------------------------


def empty_context(strategies_cfg: Optional[dict] = None) -> GateContext:
    """Contexte 'tout va bien' utile pour tests et prototypage orchestrator."""
    return GateContext(
        portfolio=PortfolioState(equity=100_000.0, daily_pnl_pct=0.0, open_positions=[]),
        tokens=TokenBudgetState(tokens_used_today=0, monthly_cost_usd=0.0, estimated_tokens=0),
        macro=MacroState(vix=18.0, hmm_regime="risk_on", hmm_confidence=0.5),
        data_quality=DataQualityState(is_fresh=True, has_outliers=False, used_fallback_source=False),
        strategies_cfg=strategies_cfg or {"defaults": {"requires_ichimoku_alignment": True}, "strategies": {}},
        correlation_to_portfolio={},
        circuit_breaker_lookup=lambda _sid: CircuitState.CLOSED,
    )


__all__ = [
    "GateConfig",
    "GateContext",
    "PortfolioState",
    "PositionSnapshot",
    "MacroState",
    "DataQualityState",
    "TokenBudgetState",
    "RiskGate",
    "empty_context",
    "CHECK_IDS",
    "_pad_checks",
]
