"""
src.orchestrator.run — Orchestrator + run_cycle (§8.7, §8.7.1).

Séquence un cycle complet : kill-switch → régime → scan → news → signaux →
sélection stratégies → build_proposal → risk-gate → persistance. Chaque
étape est protégée par `resilience.run_step()` avec timeout/retry/fallback
selon la table §8.7.1 :

| Étape               | Timeout | Retry | Fallback                                  |
|---------------------|---------|-------|-------------------------------------------|
| kill_switch         | 100 ms  | 0     | abort (fail-closed)                       |
| regime.detect()     | 10 s    | 2     | last_regime.json cache (§12.2.4)          |
| market_scanner.scan | 60 s    | 1     | positions ouvertes uniquement             |
| news_agent.analyze  | 45 s    | 1     | NewsPulse.empty (pas d'injection)         |
| signal_crossing     | 5 s     | 1     | skip asset, log DATA_SIGNAL_SKIP          |
| strategy_selector   | 2 s     | 0     | ichimoku seule (conservateur)             |
| build_proposal      | 3 s     | 0     | skip stratégie pour cet asset             |
| risk_gate           | 500 ms  | 0     | fail-closed, proposal rejetée              |
| simulator.record    | 2 s     | 2     | queue offline pending_records.jsonl       |
| db.save_cycle_obs   | 5 s     | 2     | queue offline pending_observations.jsonl  |

Invariants :
- Si `len(degradation_flags) >= 3` OU `risk_gate_failure_rate > 0.5`,
  on renvoie `CycleResult` avec status="degraded" — le scheduler (§12)
  déclenche alors un cooldown 1h (C5).
- `kill_switch` est la seule étape fail-closed totale : elle interrompt
  le cycle sans fallback (par définition).
- Les stratégies sont dispatches via `build_proposal_for()` (§8.9) qui est
  elle-même déterministe 0 token.

Design :
- Dépendances injectées dans `__init__` (Protocols plutôt qu'imports directs)
  pour permettre tests unitaires avec mocks.
- `run_cycle(session_name)` est idempotent : 2 appels successifs produisent
  2 cycle_id distincts (UUID interne), mais les effets de bord (DB inserts)
  sont isolés via transactions.
- `run_focused_cycle(focus_asset, ...)` (§15.1) : skip le scan, traite l'asset
  en mode prioritaire avec stratégies applicables par défaut.

Config extérieure :
- `regime_cache_path` (défaut "data/cache/last_regime.json") — pour fallback §12.2.4
- `strategies_cfg` : mapping strategy_id → StrategyConfig Pydantic
- `asset_universe` : list[_UniverseEntry] pour le scan
- `asset_keywords` : mapping ticker → keywords pour NER news_pulse
"""
from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    Any,
    Callable,
    Iterable,
    Literal,
    Optional,
    Protocol,
    Sequence,
)

from src.contracts.cycle import CycleResult
from src.contracts.regime import RegimeState
from src.contracts.skills import (
    Candidate,
    IchimokuPayload,
    MarketSnapshot,
    NewsPulse,
    SelectionOutput,
    SignalOutput,
    StrategyConfig,
)
from src.contracts.strategy import TradeProposal
from src.news.news_pulse import (
    LLMSummarizer,
    RawNewsItem,
    build_pulse_batch,
    triggers_ad_hoc,
)
from src.orchestrator.resilience import run_step
from src.signals import market_scan
from src.signals import strategy_selector
from src.signals.market_scan import _UniverseEntry
from src.signals.strategy_selector import _RegimeView
from src.strategies import build_proposal_for


log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Protocols (dépendances injectées)
# ---------------------------------------------------------------------------


class _KillSwitchLike(Protocol):
    def is_active(self) -> bool: ...
    def reason(self) -> str: ...


class _RegimeDetectorLike(Protocol):
    def detect(self) -> RegimeState: ...


class _RegimeStoreLike(Protocol):
    def read_last_regime(self, *, cache_path: Any) -> Optional[dict]: ...
    def write_last_regime(self, regime_dict: dict, *, cache_path: Any) -> None: ...


class _DataFetcherLike(Protocol):
    def fetch(self, canonical_symbol: str, *, asset_class: str,
              timeframe: str, lookback_bars: int) -> Any: ...


class _RiskGateLike(Protocol):
    def evaluate(self, proposal: TradeProposal, ctx: Any) -> Any: ...


class _CyclesRepoLike(Protocol):
    def start(self, *, cycle_id: str, kind: str,
              started_at: Optional[str] = None) -> None: ...
    def finish(self, cycle_id: str, *, status: str,
               proposals_count: int = 0, approved_count: int = 0,
               degradation: Optional[Iterable[str]] = None,
               risk_gate_failure_rate: Optional[float] = None,
               report_path: Optional[str] = None,
               error: Optional[str] = None) -> None: ...


class _ObservationsRepoLike(Protocol):
    def record(self, *, cycle_id: str, asset: str, payload: dict,
               strategy: Optional[str] = None, approved: bool = False) -> None: ...


class _GateCtxFactoryLike(Protocol):
    """Factory de `GateContext` pour chaque cycle.

    En prod : lit portfolio, tokens, macro depuis la DB + API stats.
    En tests : `empty_context` renvoie un ctx "tout va bien".
    """

    def __call__(self) -> Any: ...


# ---------------------------------------------------------------------------
# Configuration du cycle
# ---------------------------------------------------------------------------


@dataclass
class CycleConfig:
    """Paramétrage d'un run — résolu depuis config/*.yaml par l'appelant."""

    asset_universe: Sequence[_UniverseEntry]
    asset_keywords: dict[str, list[str]]              # ticker → keywords NER
    strategies_cfg: dict[str, StrategyConfig]
    regime_cache_path: Path = Path("data/cache/last_regime.json")
    timeframe: Literal["1h", "4h", "1d"] = "1h"
    lookback_bars: int = 200                          # §8.8.2 scanner a besoin ≥ 60 barres
    news_impact_inject_threshold: float = 0.60         # §8.7.1 seuil dur
    news_inject_cap: int = 3                           # §8.7.1 cap injections
    signal_abs_score_min: float = 0.60                 # §8.7 "strong"
    signal_confidence_min: float = 0.40                # §8.7 "strong"
    degraded_max_flags: int = 3                        # circuit breaker C5
    degraded_rgfr_cap: float = 0.50                    # risk_gate_failure_rate cap


# ---------------------------------------------------------------------------
# Signal-crossing stub (§8.7 — en attendant src/signals/crossing.py)
# ---------------------------------------------------------------------------


def _neutral_signal(asset: str, regime: RegimeState) -> SignalOutput:
    """SignalOutput neutre — aucune composante déterminante.

    Utilisé quand `src.signals.crossing` n'est pas disponible ou qu'il lève
    (DATA_SIGNAL_SKIP §8.7.1). Le signal neutre sera filtré par le seuil
    |composite_score| >= 0.6, donc aucun proposal ne sera construit pour
    cet asset — conforme au design "pas de trade sans conviction".
    """
    from datetime import datetime, timezone
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return SignalOutput(
        asset=asset,
        timestamp=ts,
        composite_score=0.0,
        confidence=0.0,
        regime_context=regime.macro,
        ichimoku=IchimokuPayload(
            price_above_kumo=False,
            tenkan_above_kijun=False,
            chikou_above_price_26=False,
            kumo_thickness_pct=0.0,
            aligned_long=False,
            aligned_short=False,
            distance_to_kumo_pct=0.0,
        ),
        trend=[],
        momentum=[],
        volume=[],
    )


def _try_import_crossing() -> Optional[Callable[[str, RegimeState], SignalOutput]]:
    """Import paresseux de `src.signals.crossing.score` si le module existe.

    Si le module n'est pas encore implémenté (Phase 9.5), on retourne None
    et le caller utilise le fallback neutre.
    """
    try:
        from src.signals import crossing  # type: ignore[attr-defined]
        return crossing.score  # type: ignore[attr-defined]
    except ImportError:
        return None
    except AttributeError:
        return None


# ---------------------------------------------------------------------------
# Conversions format OHLCV
# ---------------------------------------------------------------------------


def _bars_to_tuples(bars: Any) -> list[market_scan.OHLCVTuple]:
    """OHLCVBar (dataclass DataFetcher) → OHLCVTuple (scanner).

    Accepte aussi déjà des tuples (test ergonomique).
    """
    out: list[market_scan.OHLCVTuple] = []
    for b in bars:
        if isinstance(b, tuple):
            out.append(b)  # déjà au bon format
            continue
        out.append(
            (
                getattr(b, "ts"),
                float(getattr(b, "open")),
                float(getattr(b, "high")),
                float(getattr(b, "low")),
                float(getattr(b, "close")),
                float(getattr(b, "volume")),
            )
        )
    return out


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


@dataclass
class _CycleContext:
    """État interne accumulé au fil du cycle — consommé par la finalisation."""

    cycle_id: str
    session_name: str
    kind: Literal["scheduled", "adhoc"]
    started_at: float
    degradation_flags: list[str] = field(default_factory=list)
    proposals_count: int = 0
    approved_count: int = 0
    rejected_count: int = 0
    risk_gate_attempts: int = 0
    risk_gate_rejections: int = 0


class Orchestrator:
    """Séquence les agents pour un cycle complet d'analyse (§8.7).

    Usage :
        orch = Orchestrator(
            kill_switch=ks,
            regime_detector=rd,
            regime_store=store,
            data_fetcher=df,
            risk_gate=rg,
            gate_ctx_factory=lambda: empty_context(strategies_cfg),
            cycles_repo=cycles,
            observations_repo=obs,
            config=CycleConfig(...),
        )
        result: CycleResult = orch.run_cycle(session_name="eu_morning")

    Les dépendances à la DB sont découplées via Protocols — l'orchestrateur
    ne connaît que des interfaces. Pour un smoke-test on injecte des stubs.
    """

    def __init__(
        self,
        *,
        kill_switch: _KillSwitchLike,
        regime_detector: _RegimeDetectorLike,
        regime_store: _RegimeStoreLike,
        data_fetcher: _DataFetcherLike,
        risk_gate: _RiskGateLike,
        gate_ctx_factory: _GateCtxFactoryLike,
        cycles_repo: _CyclesRepoLike,
        observations_repo: _ObservationsRepoLike,
        config: CycleConfig,
        news_fetch: Optional[Callable[[], Sequence[RawNewsItem]]] = None,
        news_summarizer: Optional[LLMSummarizer] = None,
        signal_crossing: Optional[Callable[[str, RegimeState], SignalOutput]] = None,
    ) -> None:
        self.kill_switch = kill_switch
        self.regime_detector = regime_detector
        self.regime_store = regime_store
        self.data_fetcher = data_fetcher
        self.risk_gate = risk_gate
        self.gate_ctx_factory = gate_ctx_factory
        self.cycles_repo = cycles_repo
        self.observations_repo = observations_repo
        self.config = config
        self.news_fetch = news_fetch or (lambda: [])
        self.news_summarizer = news_summarizer
        # Import paresseux (si déjà fourni via injection, on prend celui-là)
        self.signal_crossing = signal_crossing or _try_import_crossing()

    # ------------------------------------------------------------------
    # Entrée publique
    # ------------------------------------------------------------------

    def run_cycle(self, session_name: str) -> CycleResult:
        """Cycle régulier §8.7. Retourne un `CycleResult` en toutes circonstances."""
        ctx = _CycleContext(
            cycle_id=str(uuid.uuid4()),
            session_name=session_name,
            kind="scheduled",
            started_at=time.monotonic(),
        )
        self.cycles_repo.start(cycle_id=ctx.cycle_id, kind=ctx.kind)

        # 0. Kill-switch (fail-closed)
        if self._kill_switch_active(ctx):
            return self._finalize_aborted(ctx, reason="kill_switch")

        # 1. Régime de marché (avec fallback last_regime.json)
        regime = self._resolve_regime(ctx)
        if regime is None:
            # Ni détection ni cache → cycle impossible
            return self._finalize_aborted(ctx, reason="no_regime")

        # 2. Scan univers
        candidates = self._run_scan(ctx, regime)

        # 3. News pulse par asset scanné + injection des impactful (§8.7.1)
        news_by_asset = self._build_news_pulses(ctx, candidates)
        candidates = self._inject_news_candidates(ctx, candidates, news_by_asset)

        if not candidates:
            # DATA_006 empty_shortlist (§8.8.3)
            log.info("empty_shortlist", extra={"cycle_id": ctx.cycle_id})
            return self._finalize_success(ctx)

        # 4. Signaux + sélection stratégie + build_proposal
        proposals = self._generate_proposals(ctx, regime, candidates, news_by_asset)

        # 5. Risk gate — approve / reject
        approved = self._run_risk_gate(ctx, proposals)

        # 6. Persistance observations (1 ligne par asset proposé)
        self._persist_observations(ctx, approved, rejected=[
            p for p in proposals if p not in approved
        ])

        # 7. Finalisation
        return self._finalize_success(ctx)

    def run_focused_cycle(
        self,
        focus_asset: str,
        *,
        correlated_assets: Optional[list[str]] = None,
        asset_class: str = "crypto",
    ) -> CycleResult:
        """Cycle ad-hoc §15.1 — skip le scan, focus sur `focus_asset`.

        Utilisé par le NewsWatcher quand une breaking news (impact ≥ seuil
        §8.7.1 news_impact_inject_threshold) touche un actif du portefeuille.
        On force un `Candidate(forced_by="news_pulse")` et on court-circuite
        le scanner.
        """
        ctx = _CycleContext(
            cycle_id=str(uuid.uuid4()),
            session_name=f"adhoc_{focus_asset}",
            kind="adhoc",
            started_at=time.monotonic(),
        )
        self.cycles_repo.start(cycle_id=ctx.cycle_id, kind=ctx.kind)

        if self._kill_switch_active(ctx):
            return self._finalize_aborted(ctx, reason="kill_switch")

        regime = self._resolve_regime(ctx)
        if regime is None:
            return self._finalize_aborted(ctx, reason="no_regime")

        # Force candidate(s) directement — pas de scan
        candidates: list[Candidate] = [
            market_scan.force_candidate(
                focus_asset,
                asset_class=asset_class,  # type: ignore[arg-type]
                forced_by="news_pulse",
            )
        ]
        for corr in (correlated_assets or []):
            candidates.append(
                market_scan.force_candidate(
                    corr,
                    asset_class=asset_class,  # type: ignore[arg-type]
                    forced_by="correlated_to",
                    correlated_to=focus_asset,
                )
            )

        news_by_asset = self._build_news_pulses(ctx, candidates)
        proposals = self._generate_proposals(ctx, regime, candidates, news_by_asset)
        approved = self._run_risk_gate(ctx, proposals)
        self._persist_observations(ctx, approved, rejected=[
            p for p in proposals if p not in approved
        ])
        return self._finalize_success(ctx)

    # ==================================================================
    # Étapes internes — chacune wrappée par run_step() avec sa policy
    # ==================================================================

    # ---- Étape 0 : kill-switch ----------------------------------------

    def _kill_switch_active(self, ctx: _CycleContext) -> bool:
        """Retourne True si le kill-switch est actif → abort immediate."""
        outcome = run_step(
            lambda: self.kill_switch.is_active(),
            step_name="kill_switch",
            timeout_s=0.100,
            retries=0,
            fallback=lambda: True,  # en cas de panne, fail-closed
            degradation_flag="kill_switch_check_failed",
        )
        if outcome.flag:
            ctx.degradation_flags.append(outcome.flag)
        return bool(outcome.value)

    # ---- Étape 1 : régime ---------------------------------------------

    def _resolve_regime(self, ctx: _CycleContext) -> Optional[RegimeState]:
        """Détecte le régime, fallback sur cache last_regime.json §12.2.4."""

        def _cache_fallback() -> Optional[RegimeState]:
            cached = self.regime_store.read_last_regime(
                cache_path=self.config.regime_cache_path,
            )
            if not cached:
                return None
            # RegimeState est pydantic BaseModel
            return RegimeState.model_validate(cached)

        outcome = run_step(
            self.regime_detector.detect,
            step_name="regime_detect",
            timeout_s=10.0,
            retries=2,
            backoff_s=1.0,
            backoff_factor=3.0,
            fallback=_cache_fallback,
            degradation_flag="regime_stale",
        )
        if outcome.flag:
            ctx.degradation_flags.append(outcome.flag)
        if outcome.value is not None and not outcome.used_fallback:
            # Persiste le dernier régime pour fallback futur
            try:
                self.regime_store.write_last_regime(
                    outcome.value.model_dump(),
                    cache_path=self.config.regime_cache_path,
                )
            except Exception:  # pragma: no cover — best effort
                log.warning("regime_cache_write_failed")
        return outcome.value

    # ---- Étape 2 : scan -----------------------------------------------

    def _run_scan(
        self, ctx: _CycleContext, regime: RegimeState,
    ) -> list[Candidate]:
        """Scan de l'univers. Fallback : univers réduit aux positions ouvertes."""

        def _fetch(asset: str, asset_class: str) -> Sequence[market_scan.OHLCVTuple]:
            bars = self.data_fetcher.fetch(
                asset,
                asset_class=asset_class,  # type: ignore[arg-type]
                timeframe=self.config.timeframe,
                lookback_bars=self.config.lookback_bars,
            )
            return _bars_to_tuples(bars)

        def _do_scan() -> list[Candidate]:
            res = market_scan.scan(
                universe=self.config.asset_universe,
                fetch=_fetch,
            )
            return list(res.candidates)

        def _fallback_positions_only() -> list[Candidate]:
            # TODO Phase 11 : lire les positions ouvertes depuis le portefeuille.
            # V1 : univers vide (la §8.7.1 dit "positions ouvertes uniquement").
            return []

        outcome = run_step(
            _do_scan,
            step_name="market_scan",
            timeout_s=60.0,
            retries=1,
            backoff_s=5.0,
            fallback=_fallback_positions_only,
            degradation_flag="scan_degraded",
        )
        if outcome.flag:
            ctx.degradation_flags.append(outcome.flag)
        return outcome.value

    # ---- Étape 3 : news pulse -----------------------------------------

    def _build_news_pulses(
        self,
        ctx: _CycleContext,
        candidates: list[Candidate],
    ) -> dict[str, NewsPulse]:
        """Un NewsPulse par asset via build_pulse_batch. Fallback = empty."""
        if not candidates:
            return {}

        assets = [c.asset for c in candidates]

        def _do_news() -> dict[str, NewsPulse]:
            raw = list(self.news_fetch())
            batch = build_pulse_batch(
                assets,
                raw,
                asset_keywords=self.config.asset_keywords,
                summarizer=self.news_summarizer,
            )
            return {asset: pulse for asset, (pulse, _stats) in batch.items()}

        def _fallback_empty() -> dict[str, NewsPulse]:
            return {a: NewsPulse.empty(a) for a in assets}

        outcome = run_step(
            _do_news,
            step_name="news_pulse",
            timeout_s=45.0,
            retries=1,
            backoff_s=3.0,
            fallback=_fallback_empty,
            degradation_flag="news_agent_down",
        )
        if outcome.flag:
            ctx.degradation_flags.append(outcome.flag)
        return outcome.value

    def _inject_news_candidates(
        self,
        ctx: _CycleContext,
        candidates: list[Candidate],
        news_by_asset: dict[str, NewsPulse],
    ) -> list[Candidate]:
        """§8.7.1 : upgrade/inject candidats si impact news >= seuil.

        Règle :
        - seuil dur `news_impact_inject_threshold` (défaut 0.60)
        - cap `news_inject_cap` (défaut 3 injections)
        - si asset déjà présent : `forced_by = "news_pulse"` et
          `score_scan = max(score_scan, impact * 0.8)`
        - sinon : on ajoute un nouveau `Candidate(forced_by="news_pulse")`
        """
        if not news_by_asset:
            return candidates

        # Map pour upgrades
        by_asset = {c.asset: c for c in candidates}
        # Trie les news-hits desc par impact
        hits = sorted(
            [
                (asset, pulse)
                for asset, pulse in news_by_asset.items()
                if pulse.aggregate_impact >= self.config.news_impact_inject_threshold
                and triggers_ad_hoc(pulse, impact_threshold=self.config.news_impact_inject_threshold)
            ],
            key=lambda x: x[1].aggregate_impact,
            reverse=True,
        )
        injected = 0
        new_list = list(candidates)
        for asset, pulse in hits:
            if injected >= self.config.news_inject_cap:
                break
            existing = by_asset.get(asset)
            if existing is not None:
                # Upgrade : force forced_by, on gère max(score_scan, impact*0.8)
                upgraded_score = max(
                    float(existing.score_scan),
                    pulse.aggregate_impact * 0.8,
                )
                # On ne peut pas muter un BaseModel — on recrée
                idx = next(i for i, c in enumerate(new_list) if c.asset == asset)
                new_list[idx] = Candidate(
                    asset=existing.asset,
                    asset_class=existing.asset_class,
                    score_scan=min(max(upgraded_score, -1.0), 1.0),
                    liquidity_ok=existing.liquidity_ok,
                    forced_by="news_pulse",
                )
            else:
                # Inject nouveau candidat — asset_class inconnu ici, fallback crypto
                # (le NewsWatcher §15.1 fournira la classe précise en ad-hoc)
                new_list.append(
                    market_scan.force_candidate(
                        asset,
                        asset_class="crypto",  # best-guess, sera ignoré si build_proposal échoue
                        forced_by="news_pulse",
                        score_scan=pulse.aggregate_impact * 0.8,
                    )
                )
            injected += 1
        if injected == 0 and not candidates:
            log.warning("news_injection_skipped", extra={"reason": "news_agent_degraded"})
        return new_list

    # ---- Étape 4 : signaux + sélection + build_proposal ---------------

    def _compute_signal(
        self, ctx: _CycleContext, asset: str, regime: RegimeState,
    ) -> Optional[SignalOutput]:
        """Signal-crossing §5.5. Fallback = neutral (filtré par seuil aval)."""
        if self.signal_crossing is None:
            # Pas d'implémentation disponible — signal neutre
            return _neutral_signal(asset, regime)

        outcome = run_step(
            lambda: self.signal_crossing(asset, regime),  # type: ignore[misc]
            step_name=f"signal_crossing[{asset}]",
            timeout_s=5.0,
            retries=1,
            backoff_s=0.5,
            fallback=lambda: None,  # None → skip l'asset
            degradation_flag=None,  # skip silencieux par asset, pas de flag global
        )
        if outcome.used_fallback:
            log.info("DATA_SIGNAL_SKIP", extra={"asset": asset})
        return outcome.value

    def _pick_strategies(
        self,
        ctx: _CycleContext,
        regime: RegimeState,
        candidate: Candidate,
    ) -> SelectionOutput:
        """strategy_selector.pick(). Fallback = ichimoku_trend_following seule."""

        def _do_pick() -> SelectionOutput:
            view = _RegimeView(
                macro=regime.macro,
                volatility=regime.volatility,
                confidence=float(regime.probabilities.get(regime.macro, 0.0)),
            )
            return strategy_selector.pick(view, candidate)

        def _fallback_ichimoku() -> SelectionOutput:
            # Fallback conservateur §8.7.1
            from src.contracts.skills import StrategyChoice
            return SelectionOutput(
                asset=candidate.asset,
                strategies=[
                    StrategyChoice(
                        strategy_id="ichimoku_trend_following",
                        weight=1.0,
                        reason="fallback_selector_ko",
                    )
                ],
            )

        outcome = run_step(
            _do_pick,
            step_name=f"strategy_selector[{candidate.asset}]",
            timeout_s=2.0,
            retries=0,
            fallback=_fallback_ichimoku,
            degradation_flag="selector_fallback",
        )
        if outcome.flag and outcome.flag not in ctx.degradation_flags:
            ctx.degradation_flags.append(outcome.flag)
        return outcome.value

    def _snapshot_for(
        self, asset: str, asset_class: str, ts: str,
    ) -> Optional[MarketSnapshot]:
        """Reconstruit un `MarketSnapshot` depuis DataFetcher. None si I/O KO."""
        try:
            bars = self.data_fetcher.fetch(
                asset,
                asset_class=asset_class,  # type: ignore[arg-type]
                timeframe=self.config.timeframe,
                lookback_bars=self.config.lookback_bars,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("snapshot_fetch_failed", extra={"asset": asset, "err": str(e)})
            return None
        ohlcv = _bars_to_tuples(bars)
        if len(ohlcv) < 15:
            return None
        # ATR(14) à la louche via max range sur les 14 dernières barres
        window = ohlcv[-14:]
        atr = sum((b[2] - b[3]) for b in window) / len(window)
        return MarketSnapshot(
            asset=asset,
            asset_class=asset_class,  # type: ignore[arg-type]
            ts=ohlcv[-1][0],
            ohlcv=ohlcv,
            timeframe=self.config.timeframe,
            atr_14=max(atr, 0.0),
        )

    def _build_proposal(
        self,
        ctx: _CycleContext,
        strategy_id: str,
        *,
        signal: SignalOutput,
        snapshot: MarketSnapshot,
        news: NewsPulse,
    ) -> Optional[TradeProposal]:
        """build_proposal §8.9 avec timeout 3s. Skip si KO."""
        cfg = self.config.strategies_cfg.get(strategy_id)
        if cfg is None:
            log.info("strategy_config_missing", extra={"strategy": strategy_id})
            return None

        outcome = run_step(
            lambda: build_proposal_for(
                strategy_id,
                signal=signal,
                snapshot=snapshot,
                config=cfg,
                news=news,
            ),
            step_name=f"build_proposal[{strategy_id}/{signal.asset}]",
            timeout_s=3.0,
            retries=0,
            fallback=lambda: None,  # skip stratégie pour cet asset §8.7.1
        )
        return outcome.value

    def _generate_proposals(
        self,
        ctx: _CycleContext,
        regime: RegimeState,
        candidates: list[Candidate],
        news_by_asset: dict[str, NewsPulse],
    ) -> list[TradeProposal]:
        """Pour chaque candidat : signal → selector → build_proposal (×N strategies)."""
        proposals: list[TradeProposal] = []

        for cand in candidates:
            # Signal
            sig = self._compute_signal(ctx, cand.asset, regime)
            if sig is None:
                continue
            # Filtrage §8.7 "strong"
            if (
                abs(sig.composite_score) < self.config.signal_abs_score_min
                or sig.confidence < self.config.signal_confidence_min
            ):
                # Signal trop faible — skip silencieux
                continue
            # Sélection stratégies
            selection = self._pick_strategies(ctx, regime, cand)
            # Snapshot (données pour build_proposal)
            snap = self._snapshot_for(cand.asset, cand.asset_class, sig.timestamp)
            if snap is None:
                continue
            # News (défaut empty)
            news = news_by_asset.get(cand.asset, NewsPulse.empty(cand.asset))
            # Build proposals
            for choice in selection.strategies:
                prop = self._build_proposal(
                    ctx,
                    choice.strategy_id,
                    signal=sig,
                    snapshot=snap,
                    news=news,
                )
                if prop is not None:
                    proposals.append(prop)

        ctx.proposals_count = len(proposals)
        return proposals

    # ---- Étape 5 : risk gate ------------------------------------------

    def _run_risk_gate(
        self, ctx: _CycleContext, proposals: list[TradeProposal],
    ) -> list[TradeProposal]:
        """Applique risk_gate.evaluate sur chaque proposal. Fail-closed §8.7.1."""
        approved: list[TradeProposal] = []
        for p in proposals:
            ctx.risk_gate_attempts += 1

            def _evaluate() -> Any:
                ctx_eval = self.gate_ctx_factory()
                return self.risk_gate.evaluate(p, ctx_eval)

            outcome = run_step(
                _evaluate,
                step_name=f"risk_gate[{p.proposal_id}]",
                timeout_s=0.500,
                retries=0,
                fallback=None,  # pas de fallback — on laisse remonter
                degradation_flag=None,
            )
            decision = outcome.value
            if decision is not None and getattr(decision, "approved", False):
                approved.append(p)
                ctx.approved_count += 1
            else:
                ctx.rejected_count += 1
                ctx.risk_gate_rejections += 1
        return approved

    # ---- Étape 6 : persistance observations ---------------------------

    def _persist_observations(
        self,
        ctx: _CycleContext,
        approved: list[TradeProposal],
        rejected: list[TradeProposal],
    ) -> None:
        """Une ligne par asset/stratégie — consommée par self-improve §13."""
        for p in approved:
            try:
                self.observations_repo.record(
                    cycle_id=ctx.cycle_id,
                    asset=p.asset,
                    strategy=p.strategy_id,
                    approved=True,
                    payload={
                        "side": p.side,
                        "entry_price": p.entry_price,
                        "stop_price": p.stop_price,
                        "tp_prices": list(p.tp_prices),
                        "rr": p.rr,
                        "conviction": p.conviction,
                        "risk_pct": p.risk_pct,
                    },
                )
            except Exception as e:  # pragma: no cover
                log.warning("obs_persist_failed", extra={"err": str(e)})
        for p in rejected:
            try:
                self.observations_repo.record(
                    cycle_id=ctx.cycle_id,
                    asset=p.asset,
                    strategy=p.strategy_id,
                    approved=False,
                    payload={"reason": "risk_gate_rejected"},
                )
            except Exception:  # pragma: no cover
                pass

    # ------------------------------------------------------------------
    # Finalisation
    # ------------------------------------------------------------------

    def _compute_rgfr(self, ctx: _CycleContext) -> float:
        if ctx.risk_gate_attempts == 0:
            return 0.0
        return ctx.risk_gate_rejections / float(ctx.risk_gate_attempts)

    def _finalize_success(self, ctx: _CycleContext) -> CycleResult:
        """Calcule degradation_flags/status et ferme le cycle en DB."""
        duration = time.monotonic() - ctx.started_at
        rgfr = self._compute_rgfr(ctx)

        # Circuit-breaker C5 : >= 3 flags OU rgfr > 50 %
        if (
            len(ctx.degradation_flags) >= self.config.degraded_max_flags
            or rgfr > self.config.degraded_rgfr_cap
        ):
            # On marque degraded même si pas de flag listé (rgfr élevé seul)
            if not ctx.degradation_flags:
                ctx.degradation_flags.append("risk_gate_failure_rate_high")

        status: str = "degraded" if ctx.degradation_flags else "success"

        self.cycles_repo.finish(
            ctx.cycle_id,
            status=status,
            proposals_count=ctx.proposals_count,
            approved_count=ctx.approved_count,
            degradation=list(ctx.degradation_flags),
            risk_gate_failure_rate=rgfr,
        )

        return CycleResult.success(
            proposals=ctx.approved_count,
            proposals_rejected=ctx.rejected_count,
            session_name=ctx.session_name,
            kind=ctx.kind,
            degradation_flags=list(ctx.degradation_flags),
            risk_gate_failure_rate=rgfr,
            duration_s=duration,
        )

    def _finalize_aborted(self, ctx: _CycleContext, *, reason: str) -> CycleResult:
        self.cycles_repo.finish(
            ctx.cycle_id,
            status="aborted",
            proposals_count=0,
            approved_count=0,
            degradation=list(ctx.degradation_flags),
            error=reason,
        )
        return CycleResult.aborted(
            reason,
            session_name=ctx.session_name,
            kind=ctx.kind,
            degradation_flags=list(ctx.degradation_flags),
        )


__all__ = ["Orchestrator", "CycleConfig"]
