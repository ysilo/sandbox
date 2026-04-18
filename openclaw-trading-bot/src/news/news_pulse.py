"""
src.news.news_pulse — skill `news-pulse` (§6.7, §7, §15.1).

Pipeline déterministe qui ingère des news brutes (déjà fetchées par les
adapters `src/news/fetchers/*`) et produit un `NewsPulse` Pydantic par asset
(§8.8.1). La seule étape payante (§6 du SKILL.md — résumé LLM) est isolée
derrière un **Protocol `LLMSummarizer`** : en prod l'orchestrateur branchera
sonnet-4-6, ici on fournit un stub par défaut (`PassthroughSummarizer`) pour
rester 0-token et 100 % testable.

Flux §6.7 :

    raw_items ──▶ dedupe ──▶ NER entities ──▶ sentiment (lexique)
                                            │
                                            ▼
                                   catalyst detection
                                            │
                                            ▼
                                   impact = f(catalyst, proximity, sentiment)
                                            │
                                            ▼
                                   LLM summarize  ◀── OPTIONAL (stub)
                                            │
                                            ▼
                                       NewsPulse

Pas d'I/O : aucun fetch HTTP, aucun accès fichier. L'orchestrateur (§8.7)
passe les `RawNewsItem` déjà collectés.

Modes dégradés (§7 fail-closed, conforme au SKILL.md) :
- Pas d'items → `NewsPulse.empty(asset)`.
- `LLMSummarizer` lève une exception → fallback titre tronqué 120 chars,
  pas de `summary` enrichi, l'impact reste calculé par les règles locales.
- Sentiment analyzer erreur → 0.0 neutre.

Traçabilité : les stats `PulseStats` renvoyées sont consommées par
l'orchestrateur qui les persiste dans `news_items` + `llm_usage` + `api_usage`.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import (
    Iterable,
    Literal,
    Optional,
    Protocol,
    Sequence,
)

from src.contracts.skills import NewsItem, NewsPulse


# ---------------------------------------------------------------------------
# Types publics
# ---------------------------------------------------------------------------


CatalystType = Literal[
    "earnings", "fomc", "cpi", "nfp",
    "hack", "halving", "merger", "listing",
    "regulation", "other",
]


@dataclass
class RawNewsItem:
    """News brute produite par un fetcher.

    L'ID n'est pas requis : le pipeline dédupliquera via un hash du titre
    normalisé. Les entités sont extraites par NER ultérieurement si laissées
    vides ici.
    """

    source: str                          # "reuters_rss" | "finnhub" | ...
    title: str
    url: str
    published_at: datetime               # tz-aware UTC
    body: Optional[str] = None           # contenu long optionnel
    entities_hint: list[str] = field(default_factory=list)  # NER pré-calculée éventuelle


@dataclass
class PulseStats:
    """Métriques du run (alimentent le dashboard §14.3)."""

    fetched: int
    after_dedupe: int
    scored: int
    with_catalyst: int
    llm_calls: int
    duration_ms: int = 0


# ---------------------------------------------------------------------------
# Protocol LLMSummarizer (stub par défaut)
# ---------------------------------------------------------------------------


class LLMSummarizer(Protocol):
    """Interface pour la passe LLM étape 6 du SKILL.md.

    En prod : wrapper sonnet-4-6 (§2.3). Ici le stub `PassthroughSummarizer`
    la rend identité pour garder le pipeline 0-token.
    """

    def summarize(self, items: Sequence["EnrichedItem"]) -> list["EnrichedItem"]:  # pragma: no cover
        ...


class PassthroughSummarizer:
    """Stub LLM : renvoie les items sans enrichissement.

    Le consommateur remplacera par une implémentation sonnet-4-6 qui :
    - ajoute `summary_2_phrases` (nouveau champ → on le placera dans NewsItem.title tronqué pour compat)
    - catégorise l'impact sur une échelle plus fine

    En stub, on garantit que :
    - Aucun appel réseau n'est fait.
    - Les champs sentiment/impact/entities restent inchangés.
    """

    def summarize(self, items: Sequence["EnrichedItem"]) -> list["EnrichedItem"]:
        return list(items)


@dataclass
class EnrichedItem:
    """Item au milieu du pipeline : après enrichissement déterministe, avant LLM.

    Exposé publiquement car le `LLMSummarizer` le consomme.
    """

    raw: RawNewsItem
    entities: list[str]
    sentiment: float          # ∈ [-1, 1]
    catalyst_type: CatalystType
    impact: float             # ∈ [0, 1]
    dedupe_hash: str


# ---------------------------------------------------------------------------
# Constantes §6.7 — baselines catalyst (avant modulation proximité/sentiment)
# ---------------------------------------------------------------------------


# Impact de base par type de catalyseur (§6.7)
_CATALYST_BASELINE: dict[CatalystType, float] = {
    "fomc":       0.90,
    "cpi":        0.80,
    "nfp":        0.75,
    "earnings":   0.65,
    "hack":       0.95,
    "merger":     0.70,
    "halving":    0.60,
    "listing":    0.50,
    "regulation": 0.70,
    "other":      0.30,
}


# Patterns regex → catalyst_type (détection déterministe, insensible à la casse)
_CATALYST_PATTERNS: list[tuple[re.Pattern[str], CatalystType]] = [
    (re.compile(r"\b(fomc|fed\s+meeting|rate\s+hike|rate\s+cut|jerome\s+powell|fed\s+funds)\b", re.I), "fomc"),
    (re.compile(r"\b(cpi|inflation\s+data|consumer\s+prices?)\b", re.I), "cpi"),
    (re.compile(r"\b(nfp|non[-\s]farm|jobs\s+report|unemployment\s+rate)\b", re.I), "nfp"),
    (re.compile(r"\b(earnings|quarterly\s+results|q[1-4]\s+report|résultats?\s+trimestriels?)\b", re.I), "earnings"),
    (re.compile(r"\b(hack|exploit|breach|stolen|drain|drained|attack)\b", re.I), "hack"),
    (re.compile(r"\b(halving|block\s+reward|reward\s+halv)\b", re.I), "halving"),
    (re.compile(r"\b(merger|acquisition|acquires?|buyout|takeover|rachat)\b", re.I), "merger"),
    (re.compile(r"\b(listing|listed|delisted|delisting|cotation)\b", re.I), "listing"),
    (re.compile(r"\b(regulation|regulator|sec\b|cftc|lawsuit|banned|fine|penalty)\b", re.I), "regulation"),
]


# Lexique sentiment minimaliste (français + anglais)
# Chaque mot → poids [-1, 1]. Somme pondérée puis tanh → score [-1, 1].
_SENTIMENT_LEXICON: dict[str, float] = {
    # Positif
    "surge": 0.8, "surges": 0.8, "rally": 0.7, "rallies": 0.7, "beat": 0.6,
    "beats": 0.6, "rose": 0.5, "rising": 0.5, "gain": 0.5, "gains": 0.5,
    "strong": 0.5, "record": 0.6, "boost": 0.6, "boosts": 0.6, "upgrade": 0.7,
    "upgraded": 0.7, "approved": 0.5, "bullish": 0.9, "breakthrough": 0.7,
    "hausse": 0.6, "record": 0.6, "positive": 0.4, "positif": 0.4,
    "excellent": 0.7, "solide": 0.5, "dépasse": 0.6, "dépassent": 0.6,
    # Négatif
    "crash": -0.9, "crashes": -0.9, "plunge": -0.8, "plunges": -0.8,
    "fall": -0.5, "falls": -0.5, "drop": -0.5, "drops": -0.5, "miss": -0.6,
    "misses": -0.6, "weak": -0.5, "declined": -0.5, "declines": -0.5,
    "downgrade": -0.7, "downgraded": -0.7, "lawsuit": -0.6, "banned": -0.7,
    "hack": -0.8, "hacked": -0.9, "breach": -0.7, "bearish": -0.9,
    "loss": -0.5, "losses": -0.5, "selloff": -0.8, "panic": -0.9,
    "baisse": -0.5, "chute": -0.8, "recul": -0.4, "négatif": -0.4,
    "inquiétude": -0.5, "faible": -0.4, "manque": -0.5, "rate": -0.5,
}


# ---------------------------------------------------------------------------
# Helpers normalisation
# ---------------------------------------------------------------------------


_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_WS_RE = re.compile(r"\s+")


def _strip_accents(s: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", s)
        if not unicodedata.combining(c)
    )


def _normalize_title(title: str) -> str:
    """Normalise un titre pour la comparaison de duplication.

    - lowercase + strip accents
    - remove ponctuation
    - collapse spaces
    """
    s = _strip_accents(title).lower()
    s = _PUNCT_RE.sub(" ", s)
    s = _WS_RE.sub(" ", s).strip()
    return s


def _hash_title(title: str) -> str:
    """SHA256 tronqué 16 hex chars du titre normalisé — collision négligeable."""
    return hashlib.sha256(_normalize_title(title).encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Étapes du pipeline
# ---------------------------------------------------------------------------


def _dedupe(items: Sequence[RawNewsItem]) -> list[RawNewsItem]:
    """Supprime les doublons : garde l'occurrence la plus ancienne par hash titre.

    Politique §6.7 : priorité au timestamp le plus ancien (l'original).
    """
    seen: dict[str, RawNewsItem] = {}
    for it in items:
        h = _hash_title(it.title)
        existing = seen.get(h)
        if existing is None or it.published_at < existing.published_at:
            seen[h] = it
    # Ordonner chronologiquement descendant pour la suite (récent en tête)
    return sorted(seen.values(), key=lambda x: x.published_at, reverse=True)


def _extract_entities(
    item: RawNewsItem,
    *,
    asset_keywords: dict[str, list[str]],
) -> list[str]:
    """Extrait les tickers mentionnés via dictionnaire + hints préexistants.

    `asset_keywords` : mapping ticker → list[keyword] (case-insensitive).
    Ex : `{"BTCUSDT": ["bitcoin", "btc"], "RUI.PA": ["rubis", "rui"]}`.

    Les `entities_hint` éventuelles (fournies par le fetcher) sont mergées.
    """
    found: set[str] = set(item.entities_hint)
    hay = (item.title + " " + (item.body or "")).lower()

    for ticker, kws in asset_keywords.items():
        for kw in kws:
            if kw.lower() in hay:
                found.add(ticker)
                break

    return sorted(found)


def _score_sentiment(text: str) -> float:
    """Score sentiment ∈ [-1, 1] via lexique + tanh pour saturation douce.

    Implémentation minimaliste (pas de dépendance NLTK/FinBERT ici — cf.
    SKILL.md §4 : FinBERT prioritaire en prod, VADER fallback, ici nous
    sommes en mode "charpente déterministe 0-token").
    """
    if not text:
        return 0.0
    tokens = re.findall(r"[a-zA-Zà-ÿÀ-Ÿ']+", text.lower())
    if not tokens:
        return 0.0
    raw = sum(_SENTIMENT_LEXICON.get(tok, 0.0) for tok in tokens)
    # Normalise par sqrt(len) pour éviter qu'un long article bias vs titre
    import math
    denom = max(1.0, math.sqrt(len(tokens)))
    score = raw / denom
    # Saturation douce
    return max(-1.0, min(1.0, math.tanh(score)))


def _detect_catalyst(title: str, body: Optional[str] = None) -> CatalystType:
    """Détection catalyst via pattern matching §5 du SKILL.md.

    Premier pattern qui matche l'emporte (ordre = priorité). `other` par défaut.
    """
    hay = title + " " + (body or "")
    for pattern, cat in _CATALYST_PATTERNS:
        if pattern.search(hay):
            return cat
    return "other"


def _compute_impact(
    *,
    catalyst: CatalystType,
    published_at: datetime,
    now: datetime,
    sentiment: float,
) -> float:
    """Impact ∈ [0, 1] = baseline × proximité × |sentiment-boost|.

    - Baseline §6.7 par `_CATALYST_BASELINE`.
    - Proximité : décroit linéairement de 1.0 (≤ t+1h) à 0.3 (≥ t+24h).
      Si news future (calendrier), on considère comme t+0.
    - Sentiment boost : +0.10 × |sentiment|, cappé à 1.0. Les news très
      polarisées gagnent un bonus d'impact.
    """
    base = _CATALYST_BASELINE.get(catalyst, 0.30)

    # Proximité — delta en heures
    delta = abs((now - published_at).total_seconds()) / 3600.0
    if delta <= 1.0:
        prox = 1.0
    elif delta >= 24.0:
        prox = 0.30
    else:
        # Décroissance linéaire 1h → 24h : 1.0 → 0.30
        prox = 1.0 - (0.70 * (delta - 1.0) / 23.0)

    raw = base * prox + 0.10 * abs(sentiment)
    return max(0.0, min(1.0, raw))


# ---------------------------------------------------------------------------
# API publique
# ---------------------------------------------------------------------------


def build_pulse(
    asset: str,
    raw_items: Sequence[RawNewsItem],
    *,
    asset_keywords: dict[str, list[str]],
    window_hours: int = 24,
    now: Optional[datetime] = None,
    max_items: int = 20,
    summarizer: Optional[LLMSummarizer] = None,
) -> tuple[NewsPulse, PulseStats]:
    """Construit le `NewsPulse` Pydantic pour un asset donné.

    Args:
        asset: symbole canonique (ex "BTCUSDT", "RUI.PA").
        raw_items: news brutes collectées par les fetchers.
        asset_keywords: mapping ticker → keywords pour NER. Permet au scanner
            de détecter qu'une news parle de l'asset.
        window_hours: fenêtre temporelle (§8.8.1, contraint 1-72).
        now: "instant courant" pour le calcul de proximité (tests).
        max_items: cap §6 SKILL.md (défaut 20).
        summarizer: optionnel — stub `PassthroughSummarizer` si None.

    Returns:
        (NewsPulse, PulseStats). Si raw_items vide ou aucun hit sur asset →
        `NewsPulse.empty(asset, window_hours)`.
    """
    if summarizer is None:
        summarizer = PassthroughSummarizer()

    ref_now = now or datetime.now(timezone.utc)
    window_start = ref_now - timedelta(hours=window_hours)

    # Étape 0 : filtre par fenêtre temporelle
    fetched = len(raw_items)
    in_window = [i for i in raw_items if i.published_at >= window_start]

    # Étape 1 : dedupe
    unique = _dedupe(in_window)

    # Étape 2-5 : enrichissement déterministe
    enriched: list[EnrichedItem] = []
    for it in unique:
        entities = _extract_entities(it, asset_keywords=asset_keywords)
        if asset not in entities:
            # Cette news ne concerne pas l'asset visé
            continue
        try:
            sent = _score_sentiment(it.title + " " + (it.body or ""))
        except Exception:  # pragma: no cover — défense
            sent = 0.0
        catalyst = _detect_catalyst(it.title, it.body)
        impact = _compute_impact(
            catalyst=catalyst,
            published_at=it.published_at,
            now=ref_now,
            sentiment=sent,
        )
        enriched.append(
            EnrichedItem(
                raw=it,
                entities=entities,
                sentiment=sent,
                catalyst_type=catalyst,
                impact=impact,
                dedupe_hash=_hash_title(it.title),
            )
        )

    # Tri par impact desc, cap à max_items
    enriched.sort(key=lambda e: e.impact, reverse=True)
    enriched = enriched[:max_items]
    with_catalyst_count = sum(1 for e in enriched if e.catalyst_type != "other")

    # Étape 6 : LLM (stub par défaut — identity)
    llm_calls = 0
    try:
        enriched = list(summarizer.summarize(enriched))
        if not isinstance(summarizer, PassthroughSummarizer):
            llm_calls = 1
    except Exception:  # pragma: no cover — fallback §7 fail-closed
        # Les items déterministes sont conservés
        pass

    # Étape 7 : construction NewsPulse
    if not enriched:
        return NewsPulse.empty(asset, window_hours), PulseStats(
            fetched=fetched,
            after_dedupe=len(unique),
            scored=0,
            with_catalyst=0,
            llm_calls=llm_calls,
        )

    items = [_to_pydantic(e) for e in enriched]
    aggregate_impact = max((i.impact for i in items), default=0.0)
    # Moyenne pondérée par impact (évite de diluer par des news faibles)
    total_weight = sum(i.impact for i in items) or 1.0
    aggregate_sentiment = sum(i.sentiment * i.impact for i in items) / total_weight
    aggregate_sentiment = max(-1.0, min(1.0, aggregate_sentiment))

    pulse = NewsPulse(
        asset=asset,
        window_hours=window_hours,
        items=items,
        top=items[0] if items else None,
        aggregate_impact=aggregate_impact,
        aggregate_sentiment=aggregate_sentiment,
    )
    stats = PulseStats(
        fetched=fetched,
        after_dedupe=len(unique),
        scored=len(items),
        with_catalyst=with_catalyst_count,
        llm_calls=llm_calls,
    )
    return pulse, stats


def _to_pydantic(e: EnrichedItem) -> NewsItem:
    """EnrichedItem → NewsItem (Pydantic §8.8.1).

    Le `published` est sérialisé en ISO-8601 UTC Z (contrat exige "Z" suffix).
    """
    ts = e.raw.published_at.astimezone(timezone.utc)
    ts_str = ts.strftime("%Y-%m-%dT%H:%M:%SZ")
    return NewsItem(
        source=e.raw.source,
        title=e.raw.title,
        url=e.raw.url,
        published=ts_str,
        impact=float(e.impact),
        sentiment=float(e.sentiment),
        entities=list(e.entities),
    )


def build_pulse_batch(
    assets: Iterable[str],
    raw_items: Sequence[RawNewsItem],
    *,
    asset_keywords: dict[str, list[str]],
    window_hours: int = 24,
    now: Optional[datetime] = None,
    max_items: int = 20,
    summarizer: Optional[LLMSummarizer] = None,
) -> dict[str, tuple[NewsPulse, PulseStats]]:
    """Batch — construit un `NewsPulse` par asset depuis le même pool de news.

    Utilisé par l'orchestrateur §8.7 pour factoriser l'ingestion : les
    fetchers tournent une fois, le pipeline réaffecte les news par asset
    via NER.
    """
    return {
        asset: build_pulse(
            asset, raw_items,
            asset_keywords=asset_keywords,
            window_hours=window_hours,
            now=now,
            max_items=max_items,
            summarizer=summarizer,
        )
        for asset in assets
    }


def triggers_ad_hoc(pulse: NewsPulse, *, impact_threshold: float = 0.75) -> bool:
    """Détecte si le pulse justifie un cycle ad-hoc §15.1.

    Critère §15.1 : ≥ 1 news avec `impact ≥ impact_threshold` sur l'asset.
    L'orchestrateur utilise ce flag pour enqueue un cycle focused.
    """
    return pulse.aggregate_impact >= impact_threshold


__all__ = [
    "RawNewsItem",
    "EnrichedItem",
    "PulseStats",
    "LLMSummarizer",
    "PassthroughSummarizer",
    "CatalystType",
    "build_pulse",
    "build_pulse_batch",
    "triggers_ad_hoc",
]
