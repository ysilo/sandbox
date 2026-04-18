"""
src.contracts.skills — frontière Pydantic inter-skills et modules internes.

Source : TRADING_BOT_ARCHITECTURE.md §8.8.1.

Règles d'or (voir §2) :
- Tout ce qui traverse une frontière skill (LLM) OU module Python ↔ orchestrateur
  est typé ici. Aucune structure ad-hoc (dict nu) n'est tolérée côté consommateur.
- Les timestamps sont des strings ISO-8601 UTC suffixés `Z` (regex validée).
  V2 pourra migrer vers `datetime`, mais V1 reste en str pour sérialisation JSON
  simple (queue offline, cache, logs).
- `SignalOutput.is_proposal` est figé à False côté type-system : aucun skill
  LLM ne peut produire un TradeProposal. Seule porte : `build_proposal` (§8.9).
- `RiskDecision.checks` contient TOUJOURS 10 entrées ordonnées C1→C10.
  `_pad_checks()` complète les checks manquants (short-circuit §11.6) avec
  `evaluated=False` pour préserver la traçabilité.

Invariants validés par tests unitaires (tests/contracts/*.py).
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, Field, confloat, conint, conlist, field_validator

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TS_REGEX = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def _utc_now() -> str:
    """Timestamp ISO-8601 UTC avec suffixe `Z`, tronqué à la seconde."""
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _validate_ts(value: str) -> str:
    if not _TS_REGEX.match(value):
        raise ValueError(f"timestamp non conforme ISO-8601 UTC Z : {value!r}")
    return value


# ---------------------------------------------------------------------------
# market-scan
# ---------------------------------------------------------------------------


class Candidate(BaseModel):
    """Sortie de `market-scan` : asset admissible pour shortlist §8.8."""

    asset: str                                          # "RUI.PA", "EURUSD", "BTC/USDT"
    asset_class: Literal["equity", "forex", "crypto"]
    score_scan: confloat(ge=-1.0, le=1.0)               # score rapide déterministe §8.8.2
    liquidity_ok: bool                                  # spread + depth > seuils §7.3
    forced_by: Optional[Literal["news_pulse", "telegram_cmd", "correlated_to"]] = None
    correlated_to: Optional[str] = None                 # rempli si forced_by == "correlated_to"


# ---------------------------------------------------------------------------
# strategy-selector
# ---------------------------------------------------------------------------


class StrategyChoice(BaseModel):
    strategy_id: str                                    # clé dans strategies.yaml
    weight: confloat(ge=0.0, le=1.0)                    # pondération pour ce candidat
    reason: str                                         # "regime=risk_on + trend_up"


class SelectionOutput(BaseModel):
    asset: str
    strategies: conlist(StrategyChoice, min_length=1, max_length=3)


# ---------------------------------------------------------------------------
# signal-crossing (module interne, §5 + §6)
# ---------------------------------------------------------------------------


class IchimokuPayload(BaseModel):
    price_above_kumo: bool
    tenkan_above_kijun: bool
    chikou_above_price_26: bool
    kumo_thickness_pct: confloat(ge=0.0)
    aligned_long: bool
    aligned_short: bool
    distance_to_kumo_pct: float                         # signé : positif = au-dessus


class IndicatorScore(BaseModel):
    name: str                                           # "supertrend", "rsi_14", "macd"...
    score: confloat(ge=-1.0, le=1.0)
    confidence: confloat(ge=0.0, le=1.0)


class SignalOutput(BaseModel):
    """Sortie du module `src/signals/signal_crossing.py`.

    **Diagnostic scalaire** — jamais une proposition de trade. La transformation
    signal → TradeProposal est logée dans `src/strategies/<id>.build_proposal`
    (§8.9), déterministe et 0 token.
    """

    asset: str
    timestamp: str                                      # ISO-8601 UTC Z
    composite_score: confloat(ge=-1.0, le=1.0)          # §5.5
    confidence: confloat(ge=0.0, le=1.0)
    regime_context: Literal["risk_on", "transition", "risk_off"]
    ichimoku: IchimokuPayload                           # recopié tel quel par build_proposal
    trend: list[IndicatorScore]                         # Supertrend, MACD, PSAR, Aroon, ADX, BB
    momentum: list[IndicatorScore]                      # RSI, Stoch, TRIX, CCI, Momentum
    volume: list[IndicatorScore]                        # OBV, VWAP, CMF, VolumeProfile
    is_proposal: Literal[False] = False                 # figé — invariant §8.8.1

    @field_validator("timestamp")
    @classmethod
    def _ts_format(cls, v: str) -> str:
        return _validate_ts(v)


# ---------------------------------------------------------------------------
# news-pulse
# ---------------------------------------------------------------------------


class NewsItem(BaseModel):
    source: str                                         # "reuters_rss" | "finnhub" | ...
    title: str
    url: str
    published: str                                      # ISO-8601 UTC Z
    impact: confloat(ge=0.0, le=1.0)                    # §6.7
    sentiment: confloat(ge=-1.0, le=1.0)
    entities: list[str]                                 # tickers / orgs extraits (NER)

    @field_validator("published")
    @classmethod
    def _ts_format(cls, v: str) -> str:
        return _validate_ts(v)


class NewsPulse(BaseModel):
    asset: str
    window_hours: conint(ge=1, le=72) = 24
    items: list[NewsItem]                               # triés impact desc
    top: Optional[NewsItem] = None                      # items[0] si présent
    aggregate_impact: confloat(ge=0.0, le=1.0)          # max des items pondérés
    aggregate_sentiment: confloat(ge=-1.0, le=1.0)      # moyenne pondérée

    @classmethod
    def empty(cls, asset: str, window_hours: int = 24) -> "NewsPulse":
        """Factory pour fallback news_agent KO (§8.7.1)."""
        return cls(
            asset=asset,
            window_hours=window_hours,
            items=[],
            top=None,
            aggregate_impact=0.0,
            aggregate_sentiment=0.0,
        )


# ---------------------------------------------------------------------------
# build_proposal — entrée : MarketSnapshot, StrategyConfig
# ---------------------------------------------------------------------------


class MarketSnapshot(BaseModel):
    """Snapshot complet par asset à un instant donné, produit par src/data/fetcher.py."""

    asset: str
    asset_class: Literal["equity", "forex", "crypto"]
    ts: str                                             # ISO-8601 UTC Z
    # OHLCV : (ts, open, high, low, close, volume) — N barres
    ohlcv: list[tuple[str, float, float, float, float, float]]
    timeframe: Literal["1h", "4h", "1d"]
    atr_14: confloat(ge=0.0)
    spread_bp: Optional[confloat(ge=0.0)] = None        # spread en bp (forex/crypto)
    adv_usd_20d: Optional[confloat(ge=0.0)] = None      # avg daily volume 20d (equity/crypto)
    fx_rate: Optional[float] = None                     # conversion devise si ≠ base portfolio

    @field_validator("ts")
    @classmethod
    def _ts_format(cls, v: str) -> str:
        return _validate_ts(v)


class StrategyExitConfig(BaseModel):
    atr_stop_mult: confloat(gt=0.0) = 2.0
    tp_rule: Literal["kijun", "tenkan", "hvn", "r_multiple"]
    tp_r_multiples: list[confloat(gt=0.0)] = Field(default_factory=lambda: [1.5, 3.0])
    trailing: Optional[Literal["chikou", "kijun", "atr"]] = None


class StrategyConfig(BaseModel):
    """Sous-schéma de strategies.yaml (§3.1). Chargé et validé au startup."""

    id: str
    enabled: bool = True
    requires_ichimoku_alignment: bool = True            # waiver du check C6 (§11.6)
    max_risk_pct_equity: confloat(ge=0.0, le=0.05) = 0.01
    min_rr: confloat(ge=1.0) = 1.5
    min_composite_score: confloat(ge=0.0, le=1.0) = 0.60
    coef_self_improve: confloat(ge=0.5, le=1.5) = 1.0   # multiplicateur conviction §13
    entry: dict[str, float | bool | str]                # conditions spécifiques par stratégie
    exit: StrategyExitConfig
    timeframes: list[Literal["1h", "4h", "1d"]]


# ---------------------------------------------------------------------------
# risk-gate
# ---------------------------------------------------------------------------


CHECK_IDS: tuple[str, ...] = (
    "C1_kill_switch",
    "C2_daily_loss",
    "C3_max_open_positions",
    "C4_exposure_per_class",
    "C5_circuit_breaker",
    "C6_ichimoku_alignment",
    "C7_token_budget",
    "C8_correlation_cap",
    "C9_macro_volatility",
    "C10_data_quality",
)


class RiskCheckResult(BaseModel):
    check_id: Literal[
        "C1_kill_switch",
        "C2_daily_loss",
        "C3_max_open_positions",
        "C4_exposure_per_class",
        "C5_circuit_breaker",
        "C6_ichimoku_alignment",
        "C7_token_budget",
        "C8_correlation_cap",
        "C9_macro_volatility",
        "C10_data_quality",
    ]
    passed: bool
    severity: Literal["blocking", "warn"]               # warn = pass but flag
    reason: str                                         # message actionnable §2.6
    evaluated: bool = True                              # False si short-circuité §11.6


def _pad_checks(checks: list[RiskCheckResult]) -> list[RiskCheckResult]:
    """Complète la liste pour avoir les 10 checks C1→C10 dans l'ordre.

    Les checks non-évalués (short-circuit §11.6) sont marqués `evaluated=False`.
    Invariant §8.8.1 : `RiskDecision.checks` contient toujours exactement 10 entrées.
    """
    by_id = {c.check_id: c for c in checks}
    padded: list[RiskCheckResult] = []
    for cid in CHECK_IDS:
        if cid in by_id:
            padded.append(by_id[cid])
        else:
            padded.append(
                RiskCheckResult(
                    check_id=cid,  # type: ignore[arg-type]
                    passed=True,
                    severity="warn",
                    reason="short-circuited: une gate précédente a déjà rejeté",
                    evaluated=False,
                )
            )
    return padded


class RiskDecision(BaseModel):
    proposal_id: str
    approved: bool
    reasons: list[str] = Field(default_factory=list)    # raisons de rejet (vide si approved)
    adjusted_size_pct: Optional[confloat(ge=0.0, le=1.0)] = None
    checks: conlist(RiskCheckResult, min_length=10, max_length=10)   # ordre C1→C10
    ts: str

    @field_validator("ts")
    @classmethod
    def _ts_format(cls, v: str) -> str:
        return _validate_ts(v)

    @classmethod
    def reject(
        cls,
        proposal_id: str,
        reasons: list[str],
        checks: list[RiskCheckResult],
    ) -> "RiskDecision":
        """Factory pour rejet. Complète les checks manquants avec `evaluated=False`."""
        return cls(
            proposal_id=proposal_id,
            approved=False,
            reasons=reasons,
            checks=_pad_checks(checks),
            ts=_utc_now(),
        )

    @classmethod
    def approve(
        cls,
        proposal_id: str,
        checks: list[RiskCheckResult],
        adjusted_size_pct: float,
    ) -> "RiskDecision":
        return cls(
            proposal_id=proposal_id,
            approved=True,
            reasons=[],
            adjusted_size_pct=adjusted_size_pct,
            checks=_pad_checks(checks),
            ts=_utc_now(),
        )


# ---------------------------------------------------------------------------
# backtest-quick
# ---------------------------------------------------------------------------


class BacktestReport(BaseModel):
    strategy_id: str
    period_from: str
    period_to: str
    trades_n: conint(ge=0)
    win_rate: confloat(ge=0.0, le=1.0)
    avg_rr: float
    sharpe: float
    max_dd_pct: confloat(ge=0.0, le=1.0)
    monte_carlo_p5: float                               # 5e percentile equity curve
    monte_carlo_p95: float


__all__ = [
    "Candidate",
    "StrategyChoice",
    "SelectionOutput",
    "IchimokuPayload",
    "IndicatorScore",
    "SignalOutput",
    "NewsItem",
    "NewsPulse",
    "MarketSnapshot",
    "StrategyExitConfig",
    "StrategyConfig",
    "CHECK_IDS",
    "RiskCheckResult",
    "RiskDecision",
    "BacktestReport",
    "_pad_checks",
    "_utc_now",
]
