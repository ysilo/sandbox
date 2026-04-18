"""
src.strategies._common — helpers partagés par les 7 implémentations
`build_proposal` (§8.9 TRADING_BOT_ARCHITECTURE.md).

Fonctions 100 % déterministes, 0 token. Chaque stratégie y appuie son tuyau :

    build_proposal(signal, snapshot, config, news=None) -> TradeProposal | None

Pipeline commun :

    1. Gate min_composite_score (`signal.confidence`)
    2. Gate Ichimoku alignment (si `config.requires_ichimoku_alignment`)
    3. Gate stratégie-spécifique (entry rules §6.x) — à la charge du module
    4. Déterminer `side` (long/short) à partir de l'ichimoku + composite_score
    5. Calcul entry/stop/tp :
        - entry = dernier close (snapshot.ohlcv[-1][4])
        - stop  = entry ± `exit.atr_stop_mult × ATR14`
        - tp    = selon `exit.tp_rule` : {kijun, tenkan, hvn, r_multiple}
    6. RR = |tp1 - entry| / |entry - stop| ; reject si < config.min_rr
    7. Conviction = signal.confidence × config.coef_self_improve (borné [0,1])
    8. risk_pct = config.max_risk_pct_equity (V1 — pas de scaling conviction)
    9. Assemblage TradeProposal (dataclass).

Pas de logs ici, pas d'I/O — les caller loggent l'issue (PROPOSAL_ACCEPTED /
PROPOSAL_REJECTED_REASON) dans l'orchestrateur (§8.7).
"""
from __future__ import annotations

from typing import Iterable, Literal, Optional

from src.contracts.skills import (
    IchimokuPayload,
    IndicatorScore,
    MarketSnapshot,
    SignalOutput,
    StrategyConfig,
    StrategyExitConfig,
)
from src.contracts.strategy import Side, TradeProposal


# ---------------------------------------------------------------------------
# Lookups indicateurs
# ---------------------------------------------------------------------------


def score_by_name(scores: Iterable[IndicatorScore], name: str) -> Optional[IndicatorScore]:
    """Retourne le premier IndicatorScore dont `.name == name`, ou None."""
    for s in scores:
        if s.name == name:
            return s
    return None


def scalar(scores: Iterable[IndicatorScore], name: str, default: float = 0.0) -> float:
    """Valeur scalaire `.score` d'un indicateur, ou `default` si absent."""
    s = score_by_name(scores, name)
    return float(s.score) if s is not None else float(default)


def all_indicators(signal: SignalOutput) -> list[IndicatorScore]:
    """Liste concaténée trend + momentum + volume (Ichimoku à part, typé)."""
    return list(signal.trend) + list(signal.momentum) + list(signal.volume)


# ---------------------------------------------------------------------------
# Direction & entrée / sortie
# ---------------------------------------------------------------------------


def infer_side(signal: SignalOutput) -> Optional[Side]:
    """Déduit long/short à partir de l'Ichimoku + composite_score.

    - ichimoku.aligned_long  ET composite > 0  → long
    - ichimoku.aligned_short ET composite < 0  → short
    - Sinon → None (pas de direction claire, la stratégie doit reject)
    """
    ich = signal.ichimoku
    cs = float(signal.composite_score)
    if ich.aligned_long and cs > 0:
        return "long"
    if ich.aligned_short and cs < 0:
        return "short"
    return None


def last_close(snapshot: MarketSnapshot) -> float:
    """Dernier close OHLCV."""
    if not snapshot.ohlcv:
        raise ValueError("MarketSnapshot.ohlcv vide")
    return float(snapshot.ohlcv[-1][4])


# ---------------------------------------------------------------------------
# Stop & TP
# ---------------------------------------------------------------------------


def compute_stop(
    *,
    side: Side,
    entry: float,
    atr: float,
    atr_mult: float,
) -> float:
    """Stop ATR-based : long → entry - k×ATR, short → entry + k×ATR."""
    delta = float(atr) * float(atr_mult)
    return entry - delta if side == "long" else entry + delta


def compute_tp_r_multiple(
    *,
    side: Side,
    entry: float,
    stop: float,
    r_multiples: list[float],
) -> list[float]:
    """TP par multiples de R (distance entry→stop)."""
    r = abs(entry - stop)
    if r <= 0:
        raise ValueError("stop == entry : R=0")
    out: list[float] = []
    for m in r_multiples:
        if side == "long":
            out.append(entry + m * r)
        else:
            out.append(entry - m * r)
    return out


def compute_tp_from_ichimoku(
    *,
    side: Side,
    entry: float,
    stop: float,
    r_multiples: list[float],
    ichimoku: IchimokuPayload,
    rule: Literal["kijun", "tenkan"],
    kijun: Optional[float] = None,
    tenkan: Optional[float] = None,
) -> list[float]:
    """TP primaire sur Kijun/Tenkan. Fallback r_multiple si niveau introuvable.

    V1 : `ichimoku` ne transporte pas kijun/tenkan absolus ; l'orchestrateur
    fournit explicitement via `kijun=`/`tenkan=` (récupéré depuis IchimokuResult).
    Si None, on retombe sur r_multiple pour garantir TP toujours défini.
    """
    level = kijun if rule == "kijun" else tenkan
    if level is None or (side == "long" and level <= entry) or (side == "short" and level >= entry):
        # niveau non exploitable → fallback
        return compute_tp_r_multiple(
            side=side, entry=entry, stop=stop, r_multiples=r_multiples,
        )
    # TP1 = niveau Ichimoku, TP2 = r_multiple[-1] × R comme stretch.
    # Garde-fou : si r_multiples vide (config dégénérée), on ne retourne
    # que le niveau Ichimoku — évite IndexError.
    if not r_multiples:
        return [float(level)]
    stretch = compute_tp_r_multiple(
        side=side, entry=entry, stop=stop, r_multiples=[r_multiples[-1]],
    )[0]
    return [float(level), stretch]


def compute_tp_hvn(
    *,
    side: Side,
    entry: float,
    stop: float,
    r_multiples: list[float],
    hvn_levels: Optional[list[float]] = None,
) -> list[float]:
    """TP sur le prochain HVN (Volume Profile). Fallback r_multiple."""
    if not hvn_levels:
        return compute_tp_r_multiple(
            side=side, entry=entry, stop=stop, r_multiples=r_multiples,
        )
    # Prochain HVN dans le sens du trade
    if side == "long":
        cands = sorted([x for x in hvn_levels if x > entry])
    else:
        cands = sorted([x for x in hvn_levels if x < entry], reverse=True)
    if not cands:
        return compute_tp_r_multiple(
            side=side, entry=entry, stop=stop, r_multiples=r_multiples,
        )
    tp1 = float(cands[0])
    # Garde-fou identique compute_tp_from_ichimoku : r_multiples vide =
    # on ne renvoie que le HVN trouvé.
    if not r_multiples:
        return [tp1]
    stretch = compute_tp_r_multiple(
        side=side, entry=entry, stop=stop, r_multiples=[r_multiples[-1]],
    )[0]
    return [tp1, stretch]


def tp_from_config(
    *,
    side: Side,
    entry: float,
    stop: float,
    exit_cfg: StrategyExitConfig,
    ichimoku: IchimokuPayload,
    kijun: Optional[float] = None,
    tenkan: Optional[float] = None,
    hvn_levels: Optional[list[float]] = None,
) -> list[float]:
    """Dispatch tp_rule → calcul correspondant."""
    rule = exit_cfg.tp_rule
    r_muls = list(exit_cfg.tp_r_multiples)
    if rule == "r_multiple":
        return compute_tp_r_multiple(
            side=side, entry=entry, stop=stop, r_multiples=r_muls,
        )
    if rule in ("kijun", "tenkan"):
        return compute_tp_from_ichimoku(
            side=side, entry=entry, stop=stop, r_multiples=r_muls,
            ichimoku=ichimoku, rule=rule, kijun=kijun, tenkan=tenkan,
        )
    if rule == "hvn":
        return compute_tp_hvn(
            side=side, entry=entry, stop=stop, r_multiples=r_muls,
            hvn_levels=hvn_levels,
        )
    # défense en profondeur : rule inconnue → r_multiple
    return compute_tp_r_multiple(
        side=side, entry=entry, stop=stop, r_multiples=r_muls,
    )


# ---------------------------------------------------------------------------
# R/R & conviction
# ---------------------------------------------------------------------------


def compute_rr(*, entry: float, stop: float, tp_list: list[float]) -> float:
    """R/R = |tp1 - entry| / |entry - stop|. ValueError si stop==entry."""
    if not tp_list:
        raise ValueError("tp_list vide")
    denom = abs(entry - stop)
    if denom <= 0:
        raise ValueError("stop == entry : R=0")
    return abs(tp_list[0] - entry) / denom


def adjusted_conviction(base_confidence: float, coef_self_improve: float) -> float:
    """Conviction finale = confidence × coef_self_improve, clampée [0, 1]."""
    c = float(base_confidence) * float(coef_self_improve)
    return max(0.0, min(1.0, c))


# ---------------------------------------------------------------------------
# Gates génériques (§8.9 invariants)
# ---------------------------------------------------------------------------


def passes_composite_gate(signal: SignalOutput, config: StrategyConfig) -> bool:
    """Gate sur la qualité signal — invariant §8.9.

    Comparaison faite contre `signal.confidence` (∈ [0, 1]) plutôt que
    `signal.composite_score` (∈ [-1, 1] et signé selon direction). Cela
    permet aux stratégies contrarian (waiver §2.5.1) qui prennent une
    direction opposée au composite signed de quand même filtrer la
    qualité globale du signal.

    Le nom du config field reste `min_composite_score` pour rester en
    phase avec strategies.yaml et l'architecture §6.x — historique.
    """
    return float(signal.confidence) >= float(config.min_composite_score)


def passes_ichimoku_gate(signal: SignalOutput, config: StrategyConfig, side: Side) -> bool:
    """Règle d'or §2.5 : rejet si alignement inverse. Waiver par config.

    - Si `requires_ichimoku_alignment` True : on exige aligned_long (long) /
      aligned_short (short).
    - Si False (waiver §2.5.1, stratégies contrarian) : on tolère n'importe
      quel alignement *sauf* l'alignement inverse strict (anti-golden-rule).
    """
    ich = signal.ichimoku
    if config.requires_ichimoku_alignment:
        return ich.aligned_long if side == "long" else ich.aligned_short
    # Waiver : on rejette quand même l'alignement inverse strict.
    if side == "long" and ich.aligned_short:
        return False
    if side == "short" and ich.aligned_long:
        return False
    return True


# ---------------------------------------------------------------------------
# Assembly TradeProposal
# ---------------------------------------------------------------------------


def assemble_proposal(
    *,
    strategy_id: str,
    signal: SignalOutput,
    snapshot: MarketSnapshot,
    config: StrategyConfig,
    side: Side,
    entry: float,
    stop: float,
    tp_list: list[float],
    catalysts: Optional[list[str]] = None,
) -> Optional[TradeProposal]:
    """Assemble + valide R/R. Retourne None si RR < min_rr."""
    try:
        rr = compute_rr(entry=entry, stop=stop, tp_list=tp_list)
    except ValueError:
        return None
    if rr < float(config.min_rr):
        return None
    conviction = adjusted_conviction(signal.confidence, config.coef_self_improve)
    return TradeProposal(
        strategy_id=strategy_id,
        asset=snapshot.asset,
        asset_class=snapshot.asset_class,
        side=side,
        entry_price=float(entry),
        stop_price=float(stop),
        tp_prices=[float(x) for x in tp_list],
        rr=float(rr),
        conviction=float(conviction),
        risk_pct=float(config.max_risk_pct_equity),
        catalysts=list(catalysts or []),
        ichimoku=signal.ichimoku,
    )


__all__ = [
    # lookups
    "score_by_name",
    "scalar",
    "all_indicators",
    # direction / prices
    "infer_side",
    "last_close",
    # stop/tp
    "compute_stop",
    "compute_tp_r_multiple",
    "compute_tp_from_ichimoku",
    "compute_tp_hvn",
    "tp_from_config",
    # rr / conviction
    "compute_rr",
    "adjusted_conviction",
    # gates
    "passes_composite_gate",
    "passes_ichimoku_gate",
    # assembly
    "assemble_proposal",
]
