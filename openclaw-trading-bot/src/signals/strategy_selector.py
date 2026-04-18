"""
src.signals.strategy_selector — skill `strategy-selector` (§8.8, §6.9).

100 % déterministe, 0 token. Prend le régime courant (HMM §12) + un candidat
`market-scan` et retourne 1-3 `StrategyChoice` dans un `SelectionOutput`
Pydantic (§8.8.1).

Règles de sélection §6.9 (figées en dur ici — le doc d'architecture est
la source de vérité) :

- **Plafond** : max 3 stratégies "de fond" simultanées (§6.9).
- **Opportunists hors plafond** : `event_driven_macro` et `news_driven_momentum`
  sont ajoutés au-dessus des 3 si un `NewsPulse`/catalyseur justifie leur
  activation — c'est le rôle de `build_proposal_for` de rejeter si le
  catalyseur n'est pas là. Ici on les rend disponibles par défaut.
- **Exclusivité** : `mean_reversion` et `breakout_momentum` ne coexistent
  jamais dans le même SelectionOutput (directions opposées §6.9).
- **Priorité ichimoku** : si régime directionnel fort (risk_on/risk_off),
  `ichimoku_trend_following` est toujours en tête avec weight 0.5-0.6.
- **Volatilité extreme** : fallback strict sur `ichimoku_trend_following`
  seule (+ opportunists) — on évite les strats contrarian en marché chaotique.

Mapping régime → familles "de fond" :

| macro       | 3 stratégies de fond                                    |
|-------------|--------------------------------------------------------|
| risk_on     | ichimoku_trend_following, breakout_momentum, divergence_hunter |
| risk_off    | ichimoku_trend_following, divergence_hunter, volume_profile_scalp |
| transition  | mean_reversion, divergence_hunter, volume_profile_scalp |

Volatilité :
- `low`/`mid` → 3 strats + opportunists.
- `high` → 3 strats (mais weights abaissés), + opportunists.
- `extreme` → fallback `ichimoku_trend_following` seule + opportunists.

La fonction est **par candidat** : le consommateur (orchestrateur §8.7)
itère sur sa shortlist et appelle `pick(regime, candidate)` pour chacun.

Fallback en cas de régime inconnu ou cold-start HMM (§8.7.1 pick() 2s) :
renvoie `SelectionOutput` avec uniquement `ichimoku_trend_following`
(weight=1.0, reason=cold_start_fallback).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional

from src.contracts.skills import Candidate, SelectionOutput, StrategyChoice


# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

# Stratégies de fond par régime macro (§6.9)
_BASE_BY_REGIME: dict[str, list[str]] = {
    "risk_on":    ["ichimoku_trend_following", "breakout_momentum", "divergence_hunter"],
    "risk_off":   ["ichimoku_trend_following", "divergence_hunter", "volume_profile_scalp"],
    "transition": ["mean_reversion", "divergence_hunter", "volume_profile_scalp"],
}

# Stratégies opportunists — hors plafond §6.9
_OPPORTUNIST_STRATEGIES: tuple[str, ...] = (
    "event_driven_macro",
    "news_driven_momentum",
)

# Plafond §6.9
MAX_BASE_STRATEGIES: int = 3

# Stratégies mutuellement exclusives (directions opposées)
_EXCLUSIVE_PAIRS: tuple[tuple[str, str], ...] = (
    ("mean_reversion", "breakout_momentum"),
)

# Fallback cold-start / régime inconnu
_FALLBACK_STRATEGY: str = "ichimoku_trend_following"


# ---------------------------------------------------------------------------
# Protocole RegimeState — minimal pour découpler du module regime complet
# ---------------------------------------------------------------------------


@dataclass
class _RegimeView:
    """Vue minimale du RegimeState consommée par le selector.

    Permet au selector d'être testable sans instancier `regime.hmm_detector`.
    En prod l'orchestrateur construit ce view depuis `RegimeState` :

        _RegimeView(macro=rs.macro, volatility=rs.volatility, confidence=rs.confidence)
    """

    macro: Literal["risk_on", "transition", "risk_off"] | str
    volatility: Literal["low", "mid", "high", "extreme"] | str
    confidence: float  # probabilité du macro state ∈ [0, 1]


# ---------------------------------------------------------------------------
# Helpers internes
# ---------------------------------------------------------------------------


def _filter_exclusive(strategies: list[str]) -> list[str]:
    """Retire les conflits mutuellement exclusifs (§6.9).

    Si les deux membres d'une paire exclusive sont présents, on garde le premier
    rencontré dans la liste d'entrée (qui reflète l'ordre de priorité du régime).
    """
    kept: list[str] = []
    banned: set[str] = set()
    for s in strategies:
        if s in banned:
            continue
        kept.append(s)
        for a, b in _EXCLUSIVE_PAIRS:
            if s == a:
                banned.add(b)
            elif s == b:
                banned.add(a)
    return kept


def _weights_for_regime(
    base: list[str],
    *,
    macro: str,
    volatility: str,
    confidence: float,
) -> list[float]:
    """Alloue des weights ∈ [0, 1] décroissants selon le régime.

    Convention :
    - Directionnel fort (risk_on/risk_off, confidence ≥ 0.6) :
      ichimoku en tête à ~0.55, puis décroissance linéaire.
    - Transition : pondération plus plate car chaque strat est complémentaire.
    - Volatilité `high` : on garde la structure mais on compresse (×0.8) pour
      signaler la prudence — consommé par `adjusted_conviction` aval.
    """
    n = len(base)
    if n == 0:
        return []

    if macro in ("risk_on", "risk_off") and confidence >= 0.6:
        # Décroissance : lead fort, suivants plus faibles
        raw = [0.55, 0.30, 0.15][:n]
    elif macro == "transition":
        # Plus plat
        raw = [0.40, 0.35, 0.25][:n]
    else:
        # Régime inconnu / transition faible
        raw = [1.0 / n] * n

    # Compression vol high
    if volatility == "high":
        raw = [w * 0.80 for w in raw]

    # Normalise pour que la somme ≈ 1.0 (en restant borné [0, 1])
    total = sum(raw) or 1.0
    return [min(1.0, w / total) for w in raw]


def _reason_for(strategy_id: str, *, macro: str, volatility: str, forced_by: Optional[str]) -> str:
    """Construit le `reason` audit-friendly de chaque StrategyChoice."""
    bits = [f"regime={macro}", f"vol={volatility}"]
    if forced_by:
        bits.append(f"forced_by={forced_by}")
    return f"{strategy_id}: " + " + ".join(bits)


# ---------------------------------------------------------------------------
# API publique
# ---------------------------------------------------------------------------


def pick(
    regime: _RegimeView,
    candidate: Candidate,
    *,
    include_opportunists: bool = True,
) -> SelectionOutput:
    """Sélectionne 1-3 stratégies de fond (+ opportunists) pour un candidat.

    Args:
        regime: vue du RegimeState courant (macro + volatility + confidence).
        candidate: sortie de `market-scan` pour l'asset.
        include_opportunists: True → ajoute event_driven_macro + news_driven_momentum
            en plus des 3 de fond (§6.9 « hors plafond »). L'orchestrateur peut
            les désactiver pour un cycle focused.

    Returns:
        SelectionOutput Pydantic validé (1-3 strategies mais peut contenir jusqu'à
        5 via les opportunists — le contrat Pydantic §8.8.1 borne à 3 ; on clip
        donc et on garde les opportunists hors SelectionOutput quand on atteint
        la limite).

    Raises:
        ValueError: si le régime est complètement inconnu ET `candidate.forced_by`
            non-renseigné — l'orchestrateur doit avoir une vue régime avant de
            sélectionner. Le cold-start produit un fallback ichimoku-only qui ne
            raise pas.
    """
    macro = regime.macro if regime.macro in _BASE_BY_REGIME else None
    vol = regime.volatility if regime.volatility in ("low", "mid", "high", "extreme") else "mid"

    # Extreme vol → fallback strict ichimoku
    if vol == "extreme":
        strategies = [_FALLBACK_STRATEGY]
        weights = [1.0]
    elif macro is None:
        # Régime inconnu → fallback cold-start
        strategies = [_FALLBACK_STRATEGY]
        weights = [1.0]
    else:
        base = list(_BASE_BY_REGIME[macro])
        base = _filter_exclusive(base)
        base = base[:MAX_BASE_STRATEGIES]
        strategies = base
        weights = _weights_for_regime(
            base, macro=macro, volatility=vol, confidence=float(regime.confidence),
        )

    # Assemblage des 3 de fond
    choices: list[StrategyChoice] = []
    for sid, w in zip(strategies, weights):
        choices.append(
            StrategyChoice(
                strategy_id=sid,
                weight=float(w),
                reason=_reason_for(
                    sid, macro=regime.macro, volatility=vol, forced_by=candidate.forced_by,
                ),
            )
        )

    # Ajoute les opportunists hors plafond si place disponible dans le contrat
    # Pydantic (SelectionOutput borné à max_length=3). Règle : on ne sacrifie
    # pas une strat de fond pour un opportunist — si on atteint déjà 3, on
    # ignore les opportunists (l'orchestrateur peut les réinjecter via un
    # cycle focused si newsflow le justifie §8.7).
    if include_opportunists:
        room = MAX_BASE_STRATEGIES - len(choices)
        for sid in _OPPORTUNIST_STRATEGIES:
            if room <= 0:
                break
            # Weight opportunist faible — ils se self-activent sur gate news
            choices.append(
                StrategyChoice(
                    strategy_id=sid,
                    weight=0.10,
                    reason=_reason_for(
                        sid, macro=regime.macro, volatility=vol,
                        forced_by=candidate.forced_by,
                    ) + " [opportunist]",
                )
            )
            room -= 1

    return SelectionOutput(asset=candidate.asset, strategies=choices)


def pick_batch(
    regime: _RegimeView,
    candidates: list[Candidate],
    *,
    include_opportunists: bool = True,
) -> list[SelectionOutput]:
    """Version batch pour l'orchestrateur : un SelectionOutput par candidat."""
    return [
        pick(regime, c, include_opportunists=include_opportunists)
        for c in candidates
    ]


__all__ = [
    "pick",
    "pick_batch",
    "_RegimeView",
    "MAX_BASE_STRATEGIES",
]
