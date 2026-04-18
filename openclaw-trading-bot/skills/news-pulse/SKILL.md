---
name: news-pulse
description: |
  Ingestion continue de news (RSS + APIs) avec dédoublonnage, NER tickers,
  scoring sentiment (modèle local FinBERT / VADER) et détection de catalyseurs
  (earnings, FOMC, CPI, hack, halving, M&A). Alimente la famille
  `sentiment_macro` de `signal-crossing` et la stratégie `news_driven_momentum`
  (§6.7). Peut déclencher un cycle ad-hoc sur breaking news à fort impact
  (§15.1 — trigger ad-hoc).

  DÉCLENCHE CE SKILL quand l'utilisateur demande l'actualité du marché, un
  catalyseur récent, un résumé macro de la journée, un "quoi de neuf sur X",
  ou quand l'orchestrateur construit le contexte d'une session. Active aussi
  sur les triggers : "news", "actus", "catalyseur", "breaking", "ça parle de
  quoi ce matin".

triggers:
  - "news"
  - "actus"
  - "quoi de neuf"
  - "catalyseur"
  - "breaking"
  - début de chaque session (pre_* et post_us_close)
  - cycle ad-hoc sur breaking news (§15.1)

allowed_tools:
  - web_fetch
  - read
  - write

spec_refs:
  - "§6.7 — news_driven_momentum"
  - "§7 — sources (config/sources.yaml)"
  - "§15.1 — trigger ad-hoc sur breaking news"
  - "§2.3 — budget & modèle : sonnet-4-6 (opus est réservé)"

budget:
  model: claude-sonnet-4-6        # §2.3 — PAS opus (réservé self_improve + archi-review)
  tokens_per_run: ~4000           # résumé + catégorisation des ~20 news retenues
  target_tokens_breaking: 1500    # cycle ad-hoc, compact
  api_calls_budget: ~15           # RSS + NewsAPI + Finnhub + Trading Economics + FRED

code_paths:
  - src/news/fetchers/             # RSS, NewsAPI, Finnhub, Trading Economics
  - src/news/sentiment.py          # FinBERT / VADER fallback
  - src/news/ner.py                # extraction tickers (assets.yaml + heuristiques)
  - src/news/catalysts.py          # détection earnings, FOMC, hack, halving…
---

# news-pulse

## Pourquoi ce skill existe

Le sentiment / macro ne se déduit pas du prix seul. Sans news-pulse,
`signal-crossing` perd sa 5ᵉ famille (sentiment_macro, 15 % du score
composite) et `news_driven_momentum` (§6.7) n'a aucun signal d'entrée.

Ce skill traite 2 régimes :
- **Cycle normal** (début de session, ~4 par jour) : scan complet des feeds,
  résumés des 20 news les plus pertinentes, scoring par actif.
- **Cycle ad-hoc breaking news** (§15.1) : déclenché par un article classé
  `impact=high` sur un actif du portefeuille. Latence cible < 90 s.

## Sources (source de vérité : `config/sources.yaml`)

Ne jamais hardcoder les URLs ici — toutes listées dans sources.yaml :

- **RSS généralistes** : Reuters, CoinDesk, The Block
- **APIs macro** : NewsAPI, Finnhub (news + calendriers earnings),
  Trading Economics (tedata — calendrier macro), FRED (séries macro US),
  World Bank / OECD / Eurostat (macros long terme)
- **Crypto spécialisé** : CoinGecko (trending), CoinMarketCap (dominance)

Politique : `fail-closed` (§7). Si toutes les sources d'une catégorie
échouent, le skill écrit `news-pulse.json` avec `"degraded": true` et
`signal-crossing.sentiment_macro` se met à 0 (neutre).

## Pipeline

### 1. Fetch parallèle
Adapter par source (`src/news/fetchers/*.py`). Chaque adapter gère son rate
limit et remonte `api_usage` en base. Timeout 8 s par source, parallèle.

### 2. Dédoublonnage
Hash normalisé du titre (lowercase, ponctuation strippée, entités résolues).
Garde la version avec le timestamp le plus ancien (l'original).

### 3. NER tickers
- Dictionnaire strict à partir de `config/assets.yaml` (tickers connus)
- Heuristique majuscules pour candidats nouveaux (filtrés ensuite)
- Dictionnaire de mapping (ex : "Bitcoin" → "BTCUSDT", "Fed" → marker macro)

### 4. Sentiment scoring (Python local — pas de LLM)
- **FinBERT** (ProsusAI/finbert, ~400 MB ONNX) si disponible
- Fallback **VADER** (NLTK, léger, rapide)
- Sortie : score ∈ [-1, +1] par news

### 5. Catalyseur détection
Pattern matching + calendriers Finnhub/Trading Economics → `catalyst_type` ∈
`{earnings, fomc, cpi, nfp, hack, halving, merger, listing, regulation, other}`.
Chaque type a un `impact_baseline` tabulé (§6.7) modulé par la proximité
temporelle (t-60min → t+120min).

### 6. Résumé LLM (sonnet-4-6, ~4k tokens)
Une seule passe LLM par cycle, prompt structuré :
- entrée : 20 news candidates (titre + source + ticker + sentiment + catalyst)
- sortie JSON strict : pour chaque news → `summary_2_phrases`, `impact ∈
  {low, medium, high}`, `recommended_action ∈ {ignore, monitor, ad_hoc_cycle}`

**Jamais opus-4-7** — §2.3 le réserve à `self_improve_weekly` et
`architecture_review_monthly`. Utiliser opus ici consommerait ~1.50 $/cycle ×
4 cycles = 6 $/jour, hors budget.

### 7. Trigger ad-hoc (§15.1)
Si ≥ 1 news sort avec `recommended_action = "ad_hoc_cycle"` ET touche un actif
du portefeuille ou de la watchlist → enqueue un cycle `full_analysis` sur
l'actif concerné (latence cible < 90 s).

## Contrat de sortie

`data/analyses/<day>/<session>/news-pulse.json` :

```json
{
  "ts": "2026-04-17T13:00:00Z",
  "session": "pre_us",
  "items": [
    {
      "hash": "a8f2…",
      "title": "Fed hints at holding rates through Q3",
      "source": "reuters",
      "published_at": "2026-04-17T11:42:00Z",
      "url": "https://…",
      "tickers": ["DXY", "SPY", "BTCUSDT"],
      "sentiment": -0.18,
      "catalyst_type": "fomc",
      "impact": "high",
      "summary": "Powell signale une pause prolongée. Marché anticipe déjà…",
      "recommended_action": "ad_hoc_cycle"
    }
  ],
  "by_asset": { "BTCUSDT": {"sentiment_avg": -0.12, "n_items": 3, "has_high_impact": true} },
  "triggered_ad_hoc": ["BTCUSDT"],
  "stats": {"fetched": 187, "after_dedupe": 94, "scored": 20, "duration_sec": 42}
}
```

## Règles

- **Jamais inventer une URL.** Si la source n'a pas fourni de lien canonique,
  `url: null` — le résumé reste utile, mais sans prétention de citation.
- **Filtrer le contenu sponsorisé** (détection par heuristiques de titre +
  domaines en liste noire dans sources.yaml).
- **Cap dur 20 news résumées par cycle** — au-delà, coût LLM explose sans
  gain marginal (les news 21-∞ sont redondantes 90 % du temps).
- **Breaking news < 90 s** : si latence dépasse, abort ad-hoc et log
  `slow_news_pulse`. Le cycle régulier suivant rattrapera.

## Traçabilité SQLite

| Table | Écriture | Usage aval |
|---|---|---|
| `news_items` | 1 ligne / news retenue | backfill, audit |
| `llm_usage` | 1 ligne (étape 6) | budget §14.3 |
| `api_usage` | 1 ligne / source fetch | budget §14.3 |
| `observations` | 1 si ad-hoc déclenché | feed `self-improve` |

## Modes dégradés

- **FinBERT modèle corrompu / indisponible** → fallback VADER. Ajoute tag
  `low_quality_sentiment` dans les items.
- **LLM échoue étape 6** → items passent avec résumé = titre tronqué 120
  chars, `impact` calculé uniquement sur `catalyst_type` + proximité.
  Cycle ad-hoc jamais déclenché dans ce mode.
- **Toutes les sources d'une catégorie down** → `"degraded": true`,
  `by_asset.*.sentiment_avg = 0` (neutre). `signal-crossing` voit la
  composante sentiment/macro à 0 mais continue.

## Commandes manuelles

```bash
# Cycle complet
python -m src.news.pulse --session pre_us

# Cycle ad-hoc sur un ticker
python -m src.news.pulse --ad-hoc BTCUSDT

# Dry-run (ne écrit pas, n'appelle pas le LLM)
python -m src.news.pulse --dry-run --session pre_us
```
