# Trading Bot V1 — Architecture de référence

> **Objet** : Spécification d'implémentation complète du trading bot V1. Un développeur doit pouvoir coder directement à partir de ce document.
>
> ⚠️ **Avertissement** — Le trading algorithmique comporte un risque de perte totale. En V1, le bot fonctionne **exclusivement en mode simulation** (aucun ordre réel). Le passage au trading réel est réservé à la V2, après validation de la rentabilité sur données simulées.

---

## Table des matières

1. [Vision V1](#1-vision-v1)
2. [Principes architecturaux](#2-principes-architecturaux)
3. [Arborescence cible](#3-arborescence-cible)
4. [Séparation déterministe / LLM](#4-séparation-déterministe--llm)
5. [Indicateurs — Ichimoku comme pilier central](#5-indicateurs--ichimoku-comme-pilier-central)
6. [Stratégies de Trading](#6-stratégies-de-trading)
7. [Sources de données & Data Quality Monitor](#7-sources-de-données--data-quality-monitor)
8. [Agents spécialisés & Skills Cowork](#8-agents-spécialisés--skills-cowork)
9. [Simulation & Suivi P&L](#9-simulation--suivi-pl)
10. [Mémoire SQLite](#10-mémoire-sqlite)
11. [Risk Management](#11-risk-management)
12. [Détection de régime (HMM)](#12-détection-de-régime-hmm)
13. [Self-Improve](#13-self-improve)
14. [Dashboard HTML & Notifications Telegram](#14-dashboard-html--notifications-telegram)
15. [Orchestration & Schedules](#15-orchestration--schedules)
16. [Tests & CI/CD](#16-tests--cicd)
17. [Déploiement](#17-déploiement)
18. [Glossaire](#18-glossaire)

---

## 1. Vision V1

Le bot est un **simulateur de trading assisté par IA avec validation humaine quotidienne**.

- Analyse **3 classes d'actifs** : Forex, Crypto, Actions/ETF.
- Propose des trades sur la base d'une analyse technique multi-indicateurs enrichie par LLM.
- Maintient un **journal de simulation** avec P&L fictif dans un fichier Excel.
- L'utilisateur **valide ou rejette** les propositions chaque jour via Telegram ou le dashboard.
- Le trading **réel** est réservé à la V2, conditionné à la preuve de rentabilité simulée.

### 1.1 Objectifs fonctionnels

- Produire **2 cycles d'analyse par jour pour equity/forex** : ouverture marché (07:00 UTC) et post-clôture (21:00 UTC). **Crypto : 4 cycles/jour** (un toutes les 6 h à 00:00, 06:00, 12:00, 18:00 UTC), les deux cycles equity/forex venant s'y superposer en semaine.
- **Pas de rapports le weekend pour les Actions et le Forex** : les marchés equity (Euronext, US) et forex sont fermés du vendredi soir au dimanche soir. Les cycles `full_cycle` et `full_cycle_with_journal` ne tournent donc **que du lundi au vendredi** (cron `* * * 1-5`). Seul le pipeline crypto continue 24/7 (marché ouvert en permanence). Voir §15.1 pour la configuration cron détaillée.
- **Focus equity — Euronext Paris uniquement** : l'univers actions de V1 est centré sur **Euronext Paris** (CAC 40, SBF 120 + mid-caps liquides). **Rubis (RUI.PA)** fait partie des tickers prioritaires. Les actions US sont **hors scope V1** — ni Alpaca ni yfinance ne sont utilisés. L'univers effectif est défini dans `config/assets.yaml` (classe `equity`, providers Euronext-only `[stooq, boursorama_scrape]` — **100 % gratuits, sans clé API**, conformes à l'esprit MVP du §2.3). Tickers au format `.PA` (ex: `RUI.PA`), convertis en `.fr` pour Stooq et en code Boursorama `1rP<SYMBOL>` pour le scrape. Voir §7.1 pour la config sources.
- Croiser **Ichimoku + au moins 3 indicateurs complémentaires** par actif avant toute proposition.
- Générer des propositions notées : *conviction score*, *risk score*, *R/R estimé*.
- Alimenter une mémoire persistante SQLite capitalisée sur les leçons, hypothèses, régimes.
- Se corriger périodiquement via des boucles de self-improve à 4 échelles de temps.

### 1.2 Ce que V1 ne fait PAS

- Aucun ordre passé sur un broker réel.
- Pas de connexion à un exchange avec clé d'exécution.
- Pas de ML supervisé complexe pour le scoring (hors HMM régime et self-improve).

---

## 2. Principes architecturaux

### 2.1 Règle d'or #1 — Séparation déterministe / LLM

> **Python décide, LLM propose.**

| Couche | Responsabilité | Technologie |
|---|---|---|
| **Quantitatif** | Calcul d'indicateurs, scoring, risk gate, backtest, HMM | Python pur (pandas, numpy, hmmlearn) |
| **Qualitatif** | Interprétation news, design d'hypothèses, self-improve, analyse narrative | Claude via API Anthropic |

Le LLM ne touche jamais au code de scoring ni aux seuils de risk. Il interprète, il propose. Python décide et filtre — aucun bypass possible, même en situation d'urgence.

### 2.2 Fail-closed

En cas de doute, de données manquantes ou périmées, de timeout API : **on ne propose pas de trade**. On log l'incident, on alerte sur Telegram.

### 2.3 Budget tokens maîtrisé

Le but est de gagner de l'argent, pas d'en dépenser en tokens. Les appels LLM sont minimisés : un seul appel qualitatif par cycle, pas d'appel pour les calculs d'indicateurs.

```yaml
# config/risk.yaml
llm:
  max_daily_tokens: 50000
  max_monthly_cost_usd: 15.0
  model: claude-sonnet-4-6    # pas opus pour les cycles automatiques
  opus_reserved_for: [self_improve_weekly, architecture_review_monthly]
```

### 2.4 Humain dans la boucle

- Validation quotidienne des propositions via Telegram ou dashboard.
- Validation obligatoire avant tout merge d'un patch self-improve impactant les stratégies ou l'architecture.

### 2.5 Règle d'or #2 — Contrat de sortie du pipeline

> Une proposition de trade ne sort du pipeline que si elle passe le risk gate **ET** que l'indicateur Ichimoku est cohérent avec la direction proposée.

Complémentaire de §2.1 : §2.1 dit *qui* décide (Python, pas le LLM), §2.5 dit *quoi* doit être vrai pour qu'une sortie soit émise. Les deux gates sont déterministes et non-contournables.

**Implémentation** : le check `ichimoku_alignment` est le contrôle #6 du
risk-gate (§11.5). Il consomme le bloc `ichimoku` déjà calculé par
`signal-crossing` et recopié par `build_proposal` (§8.9) — pas de
recalcul, O(1). Les stratégies contrariennes peuvent waiver ce check via
`requires_ichimoku_alignment: false` dans `config/strategies.yaml`
(mean_reversion, divergence_hunter, volume_profile_scalp) ; le waiver est
toujours explicite et justifié par commentaire. Défaut : `true`.

### 2.6 Règle d'or #3 — Logs d'erreurs actionnables

> Chaque log d'erreur doit permettre à l'opérateur de **diagnostiquer et corriger** le problème sans lire le code. Un message qui dit juste `"failed"` ou `"error"` est considéré comme un bug.

Trois exigences non-négociables pour tout log de niveau `WARNING` ou au-dessus :

1. **Quoi** — nom du composant + nature de l'erreur (`DataFetcher.fetch_ohlcv timeout`, `AnthropicClient.messages.create 401`).
2. **Pourquoi** — cause racine identifiable (`EODHD_API_KEY manquante`, `Stooq rate-limit 429`, `circuit breaker ichimoku_trend_following trip`).
3. **Comment corriger** — action concrète suggérée (`ajouter la clé dans .env et relancer`, `attendre 60 s`, `vérifier data/logs/YYYY-MM-DD.log.jsonl autour de HH:MM`).

Corollaires :

- **Fail-fast au démarrage** — si une clé API obligatoire manque ou un service critique est injoignable, le bot **refuse de démarrer** avec un message explicite plutôt que d'échouer silencieusement au premier cycle (cf. §7.5).
- **Logs structurés JSON** — tout log passe par `src/utils/logging_utils.py` qui émet du JSON line-delimited dans `data/logs/YYYY-MM-DD.log.jsonl` avec un schéma fixe `{ts, level, component, event, asset?, cause, remediation, context}` (cf. §7.5).
- **Corrélation par `cycle_id`** — chaque cycle porte un UUID propagé dans tous les logs associés, ce qui permet de reconstituer la chronologie d'un incident avec `jq '.cycle_id == "..."' data/logs/*.jsonl`.
- **Pas de secrets dans les logs** — clés API, tokens Telegram, URLs complètes avec paramètres sensibles sont masqués (`EODHD_API_KEY=***`). Un test CI grep les patterns sensibles dans les fixtures de logs.
- **Alertes Telegram uniquement sur `ERROR` + `CRITICAL`** — le bruit `WARNING` reste dans les fichiers, pas dans Telegram (cf. §14.6).

---

## 3. Arborescence cible

```
openclaw-trading-bot/
├── CLAUDE.md                          # Instructions permanentes pour l'agent OpenClaw
├── MEMORY.md                          # Façade Markdown de la mémoire (générée depuis SQLite)
├── MEMORY_INDEX.md                    # Index de la mémoire
├── TRADING_BOT_ARCHITECTURE.md        # Ce document
├── README.md
├── pyproject.toml                    # Source de vérité des deps (§17.5.1)
├── uv.lock                           # Lockfile reproductible (engagée dans git)
├── requirements.txt                  # Artefact généré via `uv export` (Dependabot)
├── .env.example
│
├── config/
│   ├── assets.yaml                    # Univers d'actifs par classe
│   ├── strategies.yaml                # Paramètres des stratégies
│   ├── risk.yaml                      # Limites, sizing, budget tokens
│   ├── sources.yaml                   # Feeds données (scraping + APIs)
│   └── schedules.yaml                 # Cron des cycles d'analyse
│
├── src/
│   ├── indicators/
│   │   ├── ichimoku.py                # Ichimoku Kinko Hyo (9, 26, 52)
│   │   ├── trend.py                   # Supertrend, MACD, Parabolic SAR, Aroon, ADX, Bollinger
│   │   ├── momentum.py                # RSI, Stochastique, TRIX, CCI, Momentum
│   │   ├── volume.py                  # OBV, VWAP, CMF, Volume Profile
│   │   └── composite.py              # Score composite [-1, +1]
│   │
│   ├── data/
│   │   ├── fetcher.py                 # Stooq CSV + Boursorama scrape (Euronext), OANDA (FX), ccxt (crypto)
│   │   ├── ticker_map.py              # Conversion ticker canonique → provider-specific (§7.1)
│   │   ├── quality_monitor.py         # Vérification fraîcheur, anomalies, fallback
│   │   ├── cache.py                   # Cache Parquet OHLCV
│   │   └── calendar.py                # Calendrier économique (FOMC, CPI, etc.)
│   │
│   ├── agents/
│   │   ├── technical_analyst.py       # Agent analyseur technique
│   │   ├── news_analyst.py            # Agent analyseur news
│   │   ├── risk_manager.py            # Agent risk manager
│   │   ├── simulator.py               # Agent simulateur (journal P&L)
│   │   ├── reporter.py                # Agent reporter (dashboard + Telegram)
│   │   └── orchestrator.py            # Orchestrateur central
│   │
│   ├── regime/
│   │   └── hmm_detector.py            # HMM 3-4 états (risk-on/off/transition)
│   │
│   ├── simulation/
│   │   ├── journal.py                 # Journal des trades simulés
│   │   └── pnl_excel.py               # Export/mise à jour Excel (.xlsx)
│   │
│   ├── memory/
│   │   ├── db.py                      # Façade SQLite (WAL mode)
│   │   ├── markdown_exporter.py       # Export vers MEMORY.md
│   │   └── models.py                  # Dataclasses des entités mémoire
│   │
│   ├── risk/
│   │   ├── gate.py                    # Risk gate (filtre déterministe)
│   │   ├── kill_switch.py             # Lecture fichier KILL
│   │   └── circuit_breaker.py         # Circuit breaker par stratégie
│   │
│   ├── backtest/
│   │   ├── walk_forward.py
│   │   ├── monte_carlo.py
│   │   └── deflated_sharpe.py
│   │
│   ├── self_improve/
│   │   ├── analyzer.py                # Analyse des trades perdants
│   │   ├── patch_generator.py         # Génération de patches (via LLM)
│   │   └── validator.py               # Validation statistique du patch
│   │
│   ├── dashboard/
│   │   ├── html_builder.py            # Génération dashboard HTML
│   │   └── templates/
│   │       └── daily.html.jinja2
│   │
│   ├── notifications/
│   │   └── telegram.py                # Envoi messages Telegram
│   │
│   └── utils/
│       ├── logging_utils.py           # Logs structurés JSON (schéma §7.5)
│       ├── error_codes.py             # Taxonomie d'erreurs CONFIG/NET/DATA/LLM/RISK/RUN (§7.5)
│       ├── health_checks.py           # Startup validation : clés API + services joignables (§7.5)
│       ├── config_loader.py           # Chargement YAML/env + validation schéma
│       └── json_schema.py             # Validation JSON inter-agents
│
├── skills/                            # Skills OpenClaw sur-mesure — 8 skills exactement (§8.8).
│   ├── market-scan/                   # Note : signal-crossing et build_proposal
│   ├── strategy-selector/             # NE SONT PAS des skills — ce sont des modules
│   ├── news-pulse/                    # internes en src/signals/ et src/strategies/.
│   ├── risk-gate/
│   ├── dashboard-builder/
│   ├── memory-consolidate/
│   ├── backtest-quick/
│   └── self-improve/
│
├── data/
│   ├── KILL                           # Fichier kill-switch (présence = freeze total — §11.1)
│   ├── cache/                         # Prix OHLCV Parquet
│   │   └── YYYY-MM-DD/
│   ├── simulation/
│   │   ├── journal.xlsx               # Journal P&L simulé (fichier principal)
│   │   └── archives/
│   ├── memory.db                      # SQLite (WAL mode)
│   ├── dashboards/                    # Dashboards HTML générés
│   │   └── YYYY-MM-DD/
│   ├── backtests/
│   └── logs/
│       └── YYYY-MM-DD.log.jsonl
│
└── tests/
    ├── unit/
    │   ├── test_ichimoku.py
    │   ├── test_indicators.py
    │   ├── test_composite_score.py
    │   ├── test_risk_gate.py
    │   ├── test_hmm_detector.py
    │   ├── test_logging_schema.py        # Valide schéma JSON + présence remediation (§7.5)
    │   ├── test_health_checks.py         # Valide startup validation (§7.5.3)
    │   └── test_pnl_excel.py
    └── integration/
        ├── test_pipeline_e2e.py
        └── test_data_fetcher.py
```

### 3.1 Schémas complets des fichiers `config/*.yaml`

Les cinq fichiers de config sont validés au startup par `src/utils/config_loader.py` (Pydantic `model_validate`) — une clé manquante, un type invalide ou une valeur hors bornes fait échouer le health check (§7.5.3) avec `error_code: CFG_002`.

**`config/risk.yaml`** — version V1 complète, valeurs par défaut éprouvées.

```yaml
# Limites dures (§11.2)
max_daily_loss_pct_equity:     2.0             # [0.5, 5.0]
max_risk_per_trade_pct_equity: 0.75            # [0.1, 2.0]
max_open_positions_total:      8               # [1, 20]
max_open_positions_per_class:
  equity: 4                                    # [0, 10]
  forex:  3                                    # [0, 10]
  crypto: 3                                    # [0, 10]
max_exposure_pct_per_class:
  equity: 50.0                                 # [0, 100]
  forex:  40.0
  crypto: 30.0
max_correlated_exposure_pct:   20.0            # §11.6 C8 — ρ > 0.7 sur 60j
correlation_window_days:       60              # [20, 120]
correlation_threshold:         0.70            # [0.5, 0.9]

# Protection macro (§11.6 C9)
macro_vol_cap:
  vix:                35.0                     # [20, 80]
  hmm_confidence_min: 0.55                     # [0.5, 0.95] — déclenche si régime risk_off > ce seuil

# Circuit breaker par stratégie (§11.3)
circuit_breaker:
  dd_7d_vs_median_threshold: 2.0               # [1.2, 4.0] — trip si DD 7j > X × DD médian 30j
  min_trades_for_trip:       5                 # [3, 20] — n'évalue pas sous ce seuil
  cooldown_hours:            48                # [12, 168]

# Budget LLM (§11.4)
llm:
  max_daily_tokens:       50000                # [5000, 500000]
  max_monthly_cost_usd:   15.0                 # [0.0, 500.0]
  model:                  claude-sonnet-4-6
  opus_reserved_for:      [self_improve_weekly, architecture_review_monthly]
  kill_switch_on_budget:  true                 # arme KILL si max_monthly_cost atteint

# Dégradation contrôlée (§11.6)
warn_only: []                                  # ex: ["C9_macro_volatility"] pour calibration
```

**`config/assets.yaml`** — univers V1 aligné §8.8.3.

```yaml
enabled_markets: [equity, crypto]              # forex désactivé par défaut V1 (OANDA payant)

equity:
  provider_class: euronext                     # résolu par sources.yaml (§7.1)
  benchmark:      "^FCHI"                      # CAC 40 pour corrélations
  universe:
    - { ticker: "RUI.PA",  name: "Rubis",        tags: [energy, midcap, priority] }
    - { ticker: "MC.PA",   name: "LVMH",         tags: [luxury, cac40] }
    - { ticker: "TTE.PA",  name: "TotalEnergies",tags: [energy, cac40] }
    - { ticker: "SAN.PA",  name: "Sanofi",       tags: [health, cac40] }
    - { ticker: "BNP.PA",  name: "BNP Paribas",  tags: [bank, cac40] }
    # … CAC 40 + SBF 120 mid-caps liquides, ~120 lignes complètes
  cost_overrides: {}                           # override spreads/commissions §9.4

forex:
  enabled: false                               # activer en éditant ce flag + enabled_markets
  universe: []                                 # EUR/USD, USD/JPY, … si activé

crypto:
  provider_class: ccxt
  universe:
    - { ticker: "BTC/USDT", tags: [top10, priority] }
    - { ticker: "ETH/USDT", tags: [top10] }
    # … 10 paires top market cap hors stablecoins
```

**`config/strategies.yaml`** — schéma + défauts pour chaque stratégie.

```yaml
defaults:
  requires_ichimoku_alignment: true
  min_rr:                      1.5             # R/R minimum pour laisser passer build_proposal
  min_composite_score:         0.60            # signal-crossing seuil
  min_confidence:              0.55
  max_risk_pct_equity:         0.75            # clampé par risk.yaml
  coef_self_improve:           1.0             # ajustement §13 (multiplicatif [0.5, 1.2])

active:
  - ichimoku_trend_following
  - breakout_momentum
  - mean_reversion
  - divergence_hunter
  - volume_profile_scalp
  - event_driven_macro
  - news_driven_momentum

strategies:
  ichimoku_trend_following:
    timeframes: { entry: H4, signal: D1 }
    entry:
      price_above_kumo:      true
      tenkan_cross_kijun_up: true
      chikou_above_price_26: true
    exit:
      atr_stop_mult: 2.0                       # stop = entry − 2×ATR14
      tp1:           "R_multiple:2.0"          # R-multiple, tenkan, kijun, HVN
      tp2:           "R_multiple:3.0"
      trailing:      "kijun"                   # trailing stop sur Kijun quand price > tp1
    indicator_weights:                         # §5.5 composite
      trend:    0.50
      momentum: 0.30
      volume:   0.20
    requires_ichimoku_alignment: true
    min_rr:                      2.0           # override du défaut

  breakout_momentum:
    timeframes: { entry: H4 }
    entry:
      price_breaks_20_high:          true
      obv_surge_vs_median_20d:       1.3       # ratio > 1.3
      atr_pct_expansion:             true
    exit:
      atr_stop_mult: 1.5
      tp1: "R_multiple:1.5"
      tp2: "R_multiple:3.0"
    indicator_weights: { trend: 0.40, momentum: 0.40, volume: 0.20 }
    requires_ichimoku_alignment: true

  mean_reversion:
    timeframes: { entry: H1 }
    entry:
      rsi_below: 30
      price_below_bb_lower: true
      regime_in: [transition, risk_off_moderate]
    exit:
      atr_stop_mult: 1.8
      tp1: "bb_middle"
    indicator_weights: { trend: 0.20, momentum: 0.50, volume: 0.30 }
    requires_ichimoku_alignment: false         # contrarien §11.5

  divergence_hunter:
    timeframes: { entry: H4 }
    entry:
      divergence_on: [rsi, macd]
      min_divergence_bars: 5
    exit: { atr_stop_mult: 2.0, tp1: "R_multiple:2.0" }
    indicator_weights: { trend: 0.25, momentum: 0.55, volume: 0.20 }
    requires_ichimoku_alignment: false

  volume_profile_scalp:
    timeframes: { entry: M15 }                 # intraday — résolution fixée §6.5
    entry: { price_at_hvn: true, vwap_bounce: true }
    exit:  { atr_stop_mult: 1.0, tp1: "R_multiple:1.0" }
    indicator_weights: { trend: 0.15, momentum: 0.30, volume: 0.55 }
    requires_ichimoku_alignment: false         # horizon trop court (§11.5)

  event_driven_macro:
    timeframes: { entry: H1 }
    entry: { post_event_window_min: 15, price_move_pct_forex: 0.5, price_move_pct_other: 1.5 }
    exit:  { atr_stop_mult: 2.5, tp1: "R_multiple:2.0" }
    indicator_weights: { trend: 0.40, momentum: 0.40, volume: 0.20 }
    requires_ichimoku_alignment: true

  news_driven_momentum:
    timeframes: { entry: H1 }
    entry:
      min_impact:     0.60
      price_move_pct: { forex: 0.4, equity: 1.0, crypto: 1.5 }
    exit: { atr_stop_mult: 2.0, tp1: "R_multiple:2.0" }
    indicator_weights: { trend: 0.35, momentum: 0.35, volume: 0.30 }
    requires_ichimoku_alignment: true
```

**`config/sources.yaml`** — déjà détaillé §7.1 ; schéma validé par le même `config_loader`.

**`config/schedules.yaml`** — déjà détaillé §15.1 ; schéma valide `cron` via `croniter.is_valid`.

---

## 4. Séparation déterministe / LLM

### 4.1 Ce que Python calcule (jamais délégué au LLM)

```python
# src/indicators/composite.py

def compute_composite_score(
    ohlcv: pd.DataFrame,
    config: StrategyConfig,
) -> CompositeScore:
    """
    Retourne un score composite signé dans [-1, +1] avec confidence.
    Calcul 100% déterministe, reproductible, seedable.
    """
    ichimoku = IchimokuSignal.from_ohlcv(ohlcv, periods=(9, 26, 52))
    trend    = TrendScore.compute(ohlcv, config.trend_weights)
    momentum = MomentumScore.compute(ohlcv, config.momentum_weights)
    volume   = VolumeScore.compute(ohlcv, config.volume_weights)

    raw = (
        config.w_ichimoku  * ichimoku.score  +
        config.w_trend     * trend.score     +
        config.w_momentum  * momentum.score  +
        config.w_volume    * volume.score
    )
    confidence = _compute_confidence(ichimoku, trend, momentum, volume)
    return CompositeScore(value=np.clip(raw, -1, 1), confidence=confidence, components={
        "ichimoku": ichimoku.score,
        "trend": trend.score,
        "momentum": momentum.score,
        "volume": volume.score,
    })
```

**Python gère aussi :**
- Calcul de tous les indicateurs techniques
- Scoring composite et pondérations
- Risk gate (filtres durs)
- HMM régime de marché
- Circuit breaker
- Backtest / walk-forward / Monte-Carlo
- Écriture dans SQLite et Excel

### 4.2 Ce que le LLM fait (uniquement qualitatif)

```python
# src/agents/technical_analyst.py

def llm_interpret_context(
    composite: CompositeScore,
    regime: RegimeState,
    news_summary: str,
    asset: str,
) -> LLMInterpretation:
    """
    Appel LLM unique par actif candidat. Interprétation narrative uniquement.
    Ne modifie PAS le score composite — enrichit uniquement la justification.
    """
    prompt = build_interpretation_prompt(composite, regime, news_summary, asset)
    response = anthropic_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=400,
        messages=[{"role": "user", "content": prompt}],
    )
    return LLMInterpretation.parse(response.content[0].text)
```

**Le LLM gère :**
- Résumé et interprétation des news
- Narration de la proposition de trade (justification textuelle)
- Design de nouvelles hypothèses (self-improve)
- Analyse de causes racines sur trades perdants
- Revue mensuelle d'architecture

### 4.3 Format d'échange inter-agents (JSON strict)

```json
{
  "session_id": "2026-04-17T07:00Z_open",
  "asset": "EURUSD",
  "asset_class": "forex",
  "regime": {
    "macro": "risk_off",
    "volatility": "mid",
    "trend": "down",
    "probabilities": { "risk_off": 0.72, "transition": 0.21, "risk_on": 0.07 }
  },
  "composite_score": {
    "value": -0.63,
    "confidence": 0.74,
    "components": {
      "ichimoku": -0.8,
      "trend": -0.6,
      "momentum": -0.45,
      "volume": -0.4
    }
  },
  "proposal": {
    "id": "P-0042",
    "side": "short",
    "entry": 1.0712,
    "stop": 1.0758,
    "tp": [1.0668, 1.0612],
    "size_pct_equity": 1.0,
    "rr": 2.2,
    "conviction": 0.68,
    "catalysts": ["DXY strength", "CPI US above expectations"],
    "risk_flags": [],
    "narrative": "Ichimoku bearish: prix sous le Kumo, Chikou confirme, Tenkan < Kijun."
  },
  "risk_gate_result": "approve",
  "llm_narrative": "Structure technique cohérente avec régime risk-off. Catalyseur macro présent."
}
```

---

## 5. Indicateurs — Ichimoku comme pilier central

### 5.1 Ichimoku Kinko Hyo (9, 26, 52)

Ichimoku est le seul indicateur **obligatoire**. Toute proposition dont le signal Ichimoku est neutre ou contraire à la direction est rejetée par le risk gate.

```python
# src/indicators/ichimoku.py

from dataclasses import dataclass
import pandas as pd
import numpy as np


@dataclass
class IchimokuResult:
    tenkan: pd.Series       # Moyenne (haut+bas)/2 sur 9 périodes
    kijun: pd.Series        # Moyenne (haut+bas)/2 sur 26 périodes
    senkou_a: pd.Series     # Projection future (tenkan+kijun)/2, décalée +26
    senkou_b: pd.Series     # Projection future sur 52 périodes, décalée +26
    chikou: pd.Series       # Prix actuel décalé -26 périodes
    kumo_bullish: pd.Series # True si senkou_a > senkou_b
    score: float            # Score signé [-1, +1] du dernier point


def compute_ichimoku(df: pd.DataFrame, fast=9, mid=26, slow=52) -> IchimokuResult:
    high, low, close = df["high"], df["low"], df["close"]

    tenkan  = (high.rolling(fast).max() + low.rolling(fast).min()) / 2
    kijun   = (high.rolling(mid).max()  + low.rolling(mid).min())  / 2
    senkou_a = ((tenkan + kijun) / 2).shift(mid)
    senkou_b = ((high.rolling(slow).max() + low.rolling(slow).min()) / 2).shift(mid)
    chikou  = close.shift(-mid)

    score = _ichimoku_score(close, tenkan, kijun, senkou_a, senkou_b, chikou)
    return IchimokuResult(tenkan, kijun, senkou_a, senkou_b, chikou,
                          senkou_a > senkou_b, score)


def _ichimoku_score(close, tenkan, kijun, span_a, span_b, chikou) -> float:
    """
    Score sur 5 conditions binaires, pondérées :
      1. Prix au-dessus/en-dessous du Kumo (poids 0.35)
      2. Tenkan > Kijun (poids 0.20)
      3. Kumo bullish / bearish (poids 0.20)
      4. Chikou au-dessus / en-dessous du prix il y a 26 périodes (poids 0.15)
      5. Prix au-dessus / en-dessous de la Kijun (poids 0.10)
    Résultat dans [-1, +1].
    """
    c = close.iloc[-1]
    k_top    = max(span_a.iloc[-1], span_b.iloc[-1])
    k_bot    = min(span_a.iloc[-1], span_b.iloc[-1])
    in_kumo  = k_bot <= c <= k_top

    if in_kumo:
        return 0.0  # Signal neutre → filtre du risk gate

    signals = [
        (1 if c > k_top else -1,  0.35),
        (1 if tenkan.iloc[-1] > kijun.iloc[-1] else -1, 0.20),
        (1 if span_a.iloc[-1] > span_b.iloc[-1] else -1, 0.20),
        (1 if not pd.isna(chikou.iloc[-26]) and close.iloc[-1] > close.iloc[-27] else -1, 0.15),
        (1 if c > kijun.iloc[-1] else -1, 0.10),
    ]
    return sum(s * w for s, w in signals)
```

**Règle d'invalidation** : si `in_kumo is True` (prix dans le nuage), le score est 0 et la proposition est bloquée par le risk gate — le marché est indécis.

### 5.2 Indicateurs de tendance (confirmation Ichimoku)

Vue d'ensemble :

| Indicateur | Paramètres | Composants | Usage principal | Fiabilité |
|---|---|---|---|---|
| **Supertrend** | ATR(10), ×3 | ATR + bande supérieure/inférieure, 1 ligne directionnelle | Suivi tendance, stop-loss dynamique | ⭐⭐⭐⭐⭐ |
| **MACD** | 12, 26, 9 | EMA rapide, EMA lente, ligne signal, histogramme | Divergences prix/momentum, croisements | ⭐⭐⭐⭐ (31% win rate mesuré) |
| **Parabolic SAR** | step=0.02, max=0.2 | Points au-dessus/en-dessous des bougies | Retournements de tendance, stop trailing | ⭐⭐⭐⭐ |
| **Aroon** | 14 | Aroon Up, Aroon Down, oscillateur (Up−Down) | Détection début de tendance, âge du plus haut/bas | ⭐⭐⭐⭐⭐ (32% win rate — le plus haut mesuré) |
| **ADX + DMI** | 14 | +DI, −DI, ADX (force) | Force de tendance (seuil critique : ADX > 25) | ⭐⭐⭐⭐ |
| **Bollinger Bands** | SMA(20), ±2σ | Bande haute, bande basse, largeur, %B | Volatilité relative, zones de surachat/survente | ⭐⭐⭐⭐ |

---

#### 5.2.1 Supertrend — ⭐⭐⭐⭐⭐

**Paramètres** : `ATR(period=10)`, `multiplier=3.0`

**Composants** :
- Calcule l'ATR sur N périodes
- Bande supérieure : `(high + low) / 2 + multiplier × ATR`
- Bande inférieure : `(high + low) / 2 − multiplier × ATR`
- La ligne Supertrend bascule entre les deux bandes selon la direction du prix

**Signaux** :
- Ligne verte sous le prix → tendance haussière → signal `+1`
- Ligne rouge au-dessus du prix → tendance baissière → signal `−1`
- Croisement du prix → signal de retournement (fort quand confirmé par Ichimoku)

**Usage spécifique** : Excellent stop-loss dynamique. Suit la tendance serré sans false positives excessifs. Particulièrement fiable sur crypto et marchés en tendance forte. Moins efficace en marché choppy (ADX < 20).

**Combinaison Ichimoku** : Si Supertrend et Ichimoku sont alignés dans la même direction, la conviction augmente de +0.15 dans le score.

---

#### 5.2.2 MACD (12, 26, 9) — ⭐⭐⭐⭐ · 31% win rate mesuré

**Paramètres** : `fast=12`, `slow=26`, `signal=9`

**Composants** :
- **MACD line** : `EMA(12) − EMA(26)`
- **Signal line** : `EMA(9)` appliquée sur la MACD line
- **Histogramme** : `MACD line − signal line` → accélération du momentum

**Signaux** :
- Histogramme positif et croissant → momentum haussier → `+1`
- Histogramme négatif et décroissant → momentum baissier → `−1`
- **Divergence haussière** : prix fait un plus bas, histogramme fait un moins bas → signal retournement haussier potentiel
- **Divergence baissière** : prix fait un plus haut, histogramme fait un moins haut → faiblesse latente

**Nota bene** : Le win rate de 31% est mesuré en signal seul. En confirmation Ichimoku, monte à ~44%.

---

#### 5.2.3 Parabolic SAR — ⭐⭐⭐⭐

**Paramètres** : `step=0.02` (facteur d'accélération initial), `max_step=0.2`

**Composants** :
- Points SAR calculés itérativement : `SAR(t) = SAR(t-1) + AF × (EP − SAR(t-1))`
  - `AF` : facteur d'accélération, part à 0.02, augmente de 0.02 à chaque nouveau EP
  - `EP` : extreme point (plus haut en tendance haussière, plus bas en baissière)
- Placés **sous** les bougies en tendance haussière, **au-dessus** en baissière

**Signaux** :
- Prix croise SAR vers le haut → retournement haussier → `+1`
- Prix croise SAR vers le bas → retournement baissier → `−1`
- **Stop trailing** : utiliser la valeur SAR courante comme niveau de stop dynamique

**Usage spécifique** : Moins précis en entrée (faux retournements en marché latéral), mais excellent pour le suivi de position et la gestion du stop. Combiné avec ADX > 25, élimine ~60% des faux signaux en range.

---

#### 5.2.4 Aroon (14) — ⭐⭐⭐⭐⭐ · 32% win rate mesuré (le plus haut)

**Paramètres** : `period=14`

**Composants** :
- **Aroon Up** : `((period − barres depuis dernier plus haut) / period) × 100`
- **Aroon Down** : `((period − barres depuis dernier plus bas) / period) × 100`
- **Oscillateur Aroon** : `Aroon Up − Aroon Down` → plage `[−100, +100]`

**Signaux** :
- Aroon Up > 70 ET Aroon Down < 30 → tendance haussière forte → `+1`
- Aroon Down > 70 ET Aroon Up < 30 → tendance baissière forte → `−1`
- Les deux autour de 50 → marché sans direction claire → signal neutralisé
- Croisement Up/Down → début de nouvelle tendance (signal précoce)

**Raison de la fiabilité maximale** : Aroon mesure le *temps* depuis le dernier extrême, non la *magnitude*. Il détecte les nouvelles tendances avant que le prix ait fait un grand mouvement, ce qui donne un rapport R/R plus favorable.

---

#### 5.2.5 ADX + DMI — ⭐⭐⭐⭐

**Paramètres** : `period=14`

**Composants** :
- **+DI** (Positive Directional Indicator) : pression acheteuse normalisée par ATR
- **−DI** (Negative Directional Indicator) : pression vendeuse normalisée par ATR
- **ADX** : lissage de `|+DI − −DI| / (+DI + −DI)` → force de tendance, `0–100`

**Signaux** :
- `ADX > 25` → tendance en place (seuil critique, filtre indispensable)
- `ADX > 40` → tendance forte
- `ADX < 20` → marché sans tendance → désactivation des stratégies momentum
- `+DI > −DI` → direction haussière
- `−DI > +DI` → direction baissière
- Croisement +DI / −DI avec `ADX > 25` → signal d'entrée en tendance

**Rôle critique dans le pipeline** : ADX est utilisé comme **pré-filtre** avant tout calcul de score trend. Si `ADX < 20`, le score trend est forcé à 0 (marché trop choppé pour les stratégies momentum).

---

#### 5.2.6 Bollinger Bands — ⭐⭐⭐⭐

**Paramètres** : `SMA(20)`, `std_dev=2.0`

**Composants** :
- **Bande médiane** : `SMA(20)` du prix de clôture
- **Bande haute** : `SMA(20) + 2 × σ(20)`
- **Bande basse** : `SMA(20) − 2 × σ(20)`
- **Largeur** (`%BW`) : `(bande haute − bande basse) / bande médiane` → mesure la volatilité relative
- **%B** : position du prix dans les bandes → `(close − bande basse) / (bande haute − bande basse)`

**Signaux** :
- Prix touche/dépasse bande haute avec volume fort → cassure haussière (confirmée par Ichimoku) → `+0.7`
- Prix touche/dépasse bande basse avec volume fort → cassure baissière → `−0.7`
- %B > 1.0 → surachat extrême → signal de prudence (réduit la conviction de −0.2)
- %B < 0.0 → survente extrême → idem
- **Compression** (BW < percentile 20 sur 1 an) → marché compressé, breakout imminent → signal d'attente

**Usage spécifique** : Les Bollinger Bands sont utilisées en *contexte*, pas comme signal directionnel isolé. Elles renforcent ou pondèrent les autres signaux trend selon la position du prix dans les bandes et l'état de la volatilité.

---

#### 5.2.7 Code — `src/indicators/trend.py`

```python
# src/indicators/trend.py

from dataclasses import dataclass
import pandas as pd
import numpy as np


@dataclass
class MACDResult:
    line: pd.Series
    signal: pd.Series
    histogram: pd.Series


@dataclass
class AroonResult:
    up: pd.Series
    down: pd.Series
    oscillator: pd.Series


@dataclass
class ADXResult:
    adx: pd.Series
    plus_di: pd.Series
    minus_di: pd.Series


@dataclass
class BollingerResult:
    upper: pd.Series
    middle: pd.Series
    lower: pd.Series
    pct_b: pd.Series
    bandwidth: pd.Series


def compute_supertrend(df: pd.DataFrame, atr_period=10, multiplier=3.0) -> pd.Series:
    """Retourne +1 (haussier) ou -1 (baissier) pour chaque bougie."""
    atr = _compute_atr(df, atr_period)
    hl2 = (df["high"] + df["low"]) / 2
    upper = hl2 + multiplier * atr
    lower = hl2 - multiplier * atr

    supertrend = pd.Series(index=df.index, dtype=float)
    direction  = pd.Series(index=df.index, dtype=float)
    for i in range(1, len(df)):
        if df["close"].iloc[i] > upper.iloc[i - 1]:
            direction.iloc[i] = 1
        elif df["close"].iloc[i] < lower.iloc[i - 1]:
            direction.iloc[i] = -1
        else:
            direction.iloc[i] = direction.iloc[i - 1]
        supertrend.iloc[i] = lower.iloc[i] if direction.iloc[i] == 1 else upper.iloc[i]
    return direction


def compute_macd(df: pd.DataFrame, fast=12, slow=26, signal=9) -> MACDResult:
    close = df["close"]
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    line     = ema_fast - ema_slow
    sig      = line.ewm(span=signal, adjust=False).mean()
    return MACDResult(line=line, signal=sig, histogram=line - sig)


def compute_parabolic_sar(df: pd.DataFrame, step=0.02, max_step=0.2) -> pd.Series:
    """
    Retourne +1 (prix > SAR, haussier) ou -1 (prix < SAR, baissier).
    Implémentation itérative standard.
    """
    high, low, close = df["high"].values, df["low"].values, df["close"].values
    n = len(close)
    sar = np.full(n, np.nan)
    direction = np.ones(n)  # +1 haussier, -1 baissier
    af = step
    ep = low[0]

    for i in range(1, n):
        if direction[i - 1] == 1:
            sar[i] = sar[i - 1] + af * (ep - sar[i - 1]) if i > 1 else low[0]
            sar[i] = min(sar[i], low[i - 1], low[max(0, i - 2)])
            if low[i] < sar[i]:
                direction[i] = -1
                sar[i] = ep
                ep = low[i]
                af = step
            else:
                direction[i] = 1
                if high[i] > ep:
                    ep = high[i]
                    af = min(af + step, max_step)
        else:
            sar[i] = sar[i - 1] - af * (sar[i - 1] - ep) if i > 1 else high[0]
            sar[i] = max(sar[i], high[i - 1], high[max(0, i - 2)])
            if high[i] > sar[i]:
                direction[i] = 1
                sar[i] = ep
                ep = high[i]
                af = step
            else:
                direction[i] = -1
                if low[i] < ep:
                    ep = low[i]
                    af = min(af + step, max_step)

    return pd.Series(direction, index=df.index)


def compute_aroon(df: pd.DataFrame, period=14) -> AroonResult:
    high, low = df["high"], df["low"]
    aroon_up   = high.rolling(period + 1).apply(
        lambda x: ((period - (period - np.argmax(x))) / period) * 100, raw=True
    )
    aroon_down = low.rolling(period + 1).apply(
        lambda x: ((period - (period - np.argmin(x))) / period) * 100, raw=True
    )
    return AroonResult(up=aroon_up, down=aroon_down, oscillator=aroon_up - aroon_down)


def compute_adx(df: pd.DataFrame, period=14) -> ADXResult:
    high, low, close = df["high"], df["low"], df["close"]
    tr   = pd.concat([high - low,
                      (high - close.shift()).abs(),
                      (low  - close.shift()).abs()], axis=1).max(axis=1)
    atr  = tr.ewm(alpha=1 / period, adjust=False).mean()
    dm_p = ((high - high.shift()).clip(lower=0)
            .where((high - high.shift()) > (low.shift() - low), 0))
    dm_n = ((low.shift() - low).clip(lower=0)
            .where((low.shift() - low) > (high - high.shift()), 0))
    di_p = 100 * dm_p.ewm(alpha=1 / period, adjust=False).mean() / atr
    di_n = 100 * dm_n.ewm(alpha=1 / period, adjust=False).mean() / atr
    dx   = 100 * (di_p - di_n).abs() / (di_p + di_n).replace(0, np.nan)
    adx  = dx.ewm(alpha=1 / period, adjust=False).mean()
    return ADXResult(adx=adx, plus_di=di_p, minus_di=di_n)


def compute_bollinger(df: pd.DataFrame, period=20, std_dev=2.0) -> BollingerResult:
    close  = df["close"]
    middle = close.rolling(period).mean()
    sigma  = close.rolling(period).std()
    upper  = middle + std_dev * sigma
    lower  = middle - std_dev * sigma
    pct_b  = (close - lower) / (upper - lower).replace(0, np.nan)
    bw     = (upper - lower) / middle.replace(0, np.nan)
    return BollingerResult(upper=upper, middle=middle, lower=lower,
                           pct_b=pct_b, bandwidth=bw)


def _bollinger_signal(close_val: float, bb: BollingerResult) -> float:
    """
    Score basé sur la position du prix dans les bandes.
    Compression (BW faible) → 0. Extrêmes → signal de retournement.
    """
    pct_b = bb.pct_b.iloc[-1]
    if pd.isna(pct_b):
        return 0.0
    if pct_b > 1.0:  return -0.7   # surachat
    if pct_b < 0.0:  return  0.7   # survente
    return np.clip((0.5 - pct_b) * 2, -0.5, 0.5)   # position centrale


def _compute_atr(df: pd.DataFrame, period: int) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    tr = pd.concat([high - low,
                    (high - close.shift()).abs(),
                    (low  - close.shift()).abs()], axis=1).max(axis=1)
    return tr.rolling(period).mean()


@dataclass
class TrendScore:
    score: float        # Signé [-1, +1]
    adx_strength: float # Force de la tendance (0–100), utilisée comme filtre
    components: dict

    @classmethod
    def compute(cls, df: pd.DataFrame, weights: "TrendWeights") -> "TrendScore":
        supertrend = compute_supertrend(df)
        macd       = compute_macd(df)
        psar       = compute_parabolic_sar(df)
        aroon      = compute_aroon(df)
        adx        = compute_adx(df)
        bollinger  = compute_bollinger(df)

        # Pré-filtre ADX : marché sans tendance → score trend forcé à 0
        adx_val = adx.adx.iloc[-1]
        if adx_val < 20:
            return cls(score=0.0, adx_strength=adx_val,
                       components={"filtered": "ADX < 20, marché sans tendance"})

        signals = {
            "supertrend": float(supertrend.iloc[-1]),
            "macd":       1.0 if macd.histogram.iloc[-1] > 0 else -1.0,
            "psar":       float(psar.iloc[-1]),
            "aroon":      np.clip(aroon.oscillator.iloc[-1] / 100, -1, 1),
            "adx_dir":    1.0 if adx.plus_di.iloc[-1] > adx.minus_di.iloc[-1] else -1.0,
            "bollinger":  _bollinger_signal(df["close"].iloc[-1], bollinger),
        }
        score = sum(signals[k] * getattr(weights, k) for k in signals)
        return cls(score=np.clip(score, -1, 1), adx_strength=adx_val,
                   components=signals)
```

**Poids par défaut des indicateurs de tendance dans `config/strategies.yaml`** :

```yaml
trend_weights:
  supertrend: 0.25   # Fiabilité ⭐⭐⭐⭐⭐ — poids le plus fort
  aroon:      0.22   # 32% win rate mesuré — second poids
  macd:       0.18   # 31% win rate — divergences précieuses
  adx_dir:    0.18   # Direction DMI, filtré par ADX
  psar:       0.10   # Stop trailing — signal moins anticipateur
  bollinger:  0.07   # Contexte volatilité — poids faible
```

---

### 5.3 Oscillateurs de momentum

Vue d'ensemble :

| Indicateur | Paramètres | Plage | Composants | Signal clé | Fiabilité |
|---|---|---|---|---|---|
| **RSI** | 14 | 0–100 | EMA gains / EMA pertes | Surachat >70, survente <30, divergences | ⭐⭐⭐⭐⭐ |
| **Stochastique** | K=14, D=3, smooth=5 | 0–100 | %K (rapide), %D (signal lissé) | Croisements %K/%D, zones 20/80 | ⭐⭐⭐ (25% win rate) |
| **TRIX** | period=15, signal=9 | Oscillateur centré 0 | EMA triple lissée, signal EMA(9) | Croisement ligne/signal, divergences | ⭐⭐⭐⭐ (30% win rate) |
| **CCI** | 20 | Centré 0, typiquement ±200 | Écart prix / SMA / MAD | Extrêmes ±100, retournements | ⭐⭐⭐⭐ |
| **Momentum** | 12 | Centré 0 | `close(t) − close(t−N)` | Signe et accélération | ⭐⭐⭐ (26% win rate) |

---

#### 5.3.1 RSI (14) — ⭐⭐⭐⭐⭐

**Paramètres** : `period=14`

**Composants** :
- Moyenne des gains sur N périodes / Moyenne des pertes sur N périodes = `RS`
- `RSI = 100 − (100 / (1 + RS))`
- Lissage Wilder (EMA avec `α = 1/14`)

**Signaux** :
- RSI < 30 → zone de survente → rebond probable si confirmé par Ichimoku → signal `+0.8`
- RSI > 70 → zone de surachat → repli probable → signal `−0.8`
- RSI entre 45–55 → zone neutre → signal `0.0`
- **Divergence haussière** : prix fait un plus bas, RSI fait un moins bas → retournement fort
- **Divergence baissière** : prix fait un plus haut, RSI fait un moins haut → faiblesse imminente
- En tendance forte (ADX > 40) : RSI peut rester en zone de surachat/survente longtemps → ne pas contra-trader

**Usage combiné** : Le RSI est l'oscillateur de référence pour valider les niveaux d'entrée proposés par Ichimoku. Un signal Ichimoku long avec RSI < 30 est la configuration la plus favorable.

---

#### 5.3.2 Stochastique (14, 3, 5) — ⭐⭐⭐ · 25% win rate mesuré

**Paramètres** : `K=14`, `D=3` (signal), `smooth=5` (lissage %K)

**Composants** :
- **%K brut** : `(close − lowest_low_14) / (highest_high_14 − lowest_low_14) × 100`
- **%K lissé** : SMA(%K brut, 5) — version utilisée en pratique
- **%D** : SMA(%K lissé, 3) — ligne de signal

**Signaux** :
- %K croise %D vers le haut en zone < 20 → signal haussier → `+0.6`
- %K croise %D vers le bas en zone > 80 → signal baissier → `−0.6`
- Les deux lignes en zone médiane → signal ignoré (trop de faux positifs)
- Divergence Stoch/prix en zones extrêmes → signal de retournement moyen

**Nota bene** : Le win rate de 25% en standalone est le plus bas du groupe. Ce signal est utilisé **uniquement en confirmation** d'un signal Ichimoku existant, jamais seul. Son poids est réduit en conséquence.

---

#### 5.3.3 TRIX (15, 9) — ⭐⭐⭐⭐ · 30% win rate mesuré

**Paramètres** : `period=15`, `signal_period=9`

**Composants** :
- **EMA1** : `EMA(close, 15)`
- **EMA2** : `EMA(EMA1, 15)` — double lissage
- **EMA3** : `EMA(EMA2, 15)` — triple lissage
- **TRIX line** : variation en % de EMA3 : `(EMA3(t) − EMA3(t−1)) / EMA3(t−1) × 100`
- **Signal line** : `EMA(TRIX line, 9)`
- **Histogramme** : `TRIX line − signal line`

**Signaux** :
- TRIX line > 0 et croissant → momentum haussier → `+0.7`
- TRIX line < 0 et décroissant → momentum baissier → `−0.7`
- Croisement TRIX / signal → point d'inflexion du momentum
- Divergence TRIX / prix → signal de retournement similaire à MACD mais plus filtré

**Avantage clé** : Le triple lissage élimine une grande partie du bruit haute fréquence. TRIX donne moins de faux signaux que MACD en marché volatile. Particulièrement utile sur crypto où le bruit intraday est élevé.

---

#### 5.3.4 CCI (20) — ⭐⭐⭐⭐

**Paramètres** : `period=20`

**Composants** :
- **Prix typique (TP)** : `(high + low + close) / 3`
- **SMA(TP, 20)** : moyenne mobile du prix typique
- **MAD** : déviation absolue moyenne de TP par rapport à SMA(TP)
- **CCI** : `(TP − SMA(TP)) / (0.015 × MAD)`

**Signaux** :
- CCI > +100 → condition de surachat ou début de tendance forte → selon contexte
- CCI < −100 → condition de survente ou début de tendance baissière forte
- CCI croise 0 vers le haut depuis <−100 → rebond confirmé → `+0.8`
- CCI croise 0 vers le bas depuis >+100 → repli confirmé → `−0.8`
- Extrêmes > ±200 → retournement très probable → signal fort de contre-tendance

**Usage spécifique** : Le CCI réagit rapidement aux retournements de prix car il utilise le prix typique (high+low+close). Utile pour détecter les points de retournement intrajournaliers sur forex et crypto. Combiné à Ichimoku, filtre les faux signaux en zone neutre.

---

#### 5.3.5 Momentum (12) — ⭐⭐⭐ · 26% win rate mesuré

**Paramètres** : `period=12`

**Composant** :
- `Momentum(t) = close(t) − close(t−12)` — différence absolue de prix sur N périodes
- Variante normalisée : `Momentum(t) = (close(t) / close(t−12)) × 100`

**Signaux** :
- Momentum > 0 et croissant → accélération haussière → `+0.5`
- Momentum < 0 et décroissant → accélération baissière → `−0.5`
- Momentum croise zéro → changement de direction du momentum
- Divergence Momentum / prix → signal précoce de faiblesse/force

**Nota bene** : Win rate de 26% seul, utilisé comme signal d'accélération en renforcement des autres oscillateurs. Son signal confirme que le mouvement en cours accélère (ou ralentit), ce qui affecte la taille de position recommandée.

---

#### 5.3.6 Code — `src/indicators/momentum.py`

```python
# src/indicators/momentum.py

from dataclasses import dataclass
import pandas as pd
import numpy as np


@dataclass
class StochResult:
    k: pd.Series    # %K lissé
    d: pd.Series    # %D (signal)


@dataclass
class TRIXResult:
    line: pd.Series
    signal: pd.Series
    histogram: pd.Series


def compute_rsi(df: pd.DataFrame, period=14) -> pd.Series:
    delta  = df["close"].diff()
    gain   = delta.clip(lower=0)
    loss   = (-delta).clip(lower=0)
    avg_g  = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_l  = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs     = avg_g / avg_l.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def compute_stochastic(df: pd.DataFrame, k=14, d=3, smooth=5) -> StochResult:
    low_k  = df["low"].rolling(k).min()
    high_k = df["high"].rolling(k).max()
    pct_k_raw = 100 * (df["close"] - low_k) / (high_k - low_k).replace(0, np.nan)
    pct_k  = pct_k_raw.rolling(smooth).mean()
    pct_d  = pct_k.rolling(d).mean()
    return StochResult(k=pct_k, d=pct_d)


def compute_trix(df: pd.DataFrame, period=15, signal=9) -> TRIXResult:
    ema1  = df["close"].ewm(span=period, adjust=False).mean()
    ema2  = ema1.ewm(span=period, adjust=False).mean()
    ema3  = ema2.ewm(span=period, adjust=False).mean()
    line  = ema3.pct_change() * 100
    sig   = line.ewm(span=signal, adjust=False).mean()
    return TRIXResult(line=line, signal=sig, histogram=line - sig)


def compute_cci(df: pd.DataFrame, period=20) -> pd.Series:
    tp  = (df["high"] + df["low"] + df["close"]) / 3
    sma = tp.rolling(period).mean()
    mad = tp.rolling(period).apply(lambda x: np.mean(np.abs(x - x.mean())), raw=True)
    return (tp - sma) / (0.015 * mad.replace(0, np.nan))


def compute_momentum(df: pd.DataFrame, period=12) -> pd.Series:
    return df["close"] - df["close"].shift(period)


def _rsi_signal(rsi_val: float) -> float:
    """
    Zones RSI → score signé.
    Survente (<30) et surachat (>70) donnent le signal le plus fort.
    Zone neutre (45–55) → 0.
    """
    if rsi_val < 30:   return  0.8
    if rsi_val < 40:   return  0.4
    if rsi_val < 45:   return  0.2
    if rsi_val > 70:   return -0.8
    if rsi_val > 60:   return -0.4
    if rsi_val > 55:   return -0.2
    return 0.0


def _stoch_signal(stoch: StochResult) -> float:
    """Croisement K/D en zones extrêmes uniquement."""
    k, d = stoch.k.iloc[-1], stoch.d.iloc[-1]
    k_prev, d_prev = stoch.k.iloc[-2], stoch.d.iloc[-2]
    if k < 20 and k > d and k_prev <= d_prev:  return  0.6  # croisement haussier zone survente
    if k > 80 and k < d and k_prev >= d_prev:  return -0.6  # croisement baissier zone surachat
    if k < 20:   return  0.3
    if k > 80:   return -0.3
    return 0.0


@dataclass
class MomentumScore:
    score: float
    rsi: float        # Valeur brute RSI — exposée pour le dashboard
    components: dict

    @classmethod
    def compute(cls, df: pd.DataFrame, weights: "MomentumWeights") -> "MomentumScore":
        rsi   = compute_rsi(df, period=14)
        stoch = compute_stochastic(df, k=14, d=3, smooth=5)
        trix  = compute_trix(df, period=15, signal=9)
        cci   = compute_cci(df, period=20)
        mom   = compute_momentum(df, period=12)

        signals = {
            "rsi":   _rsi_signal(rsi.iloc[-1]),
            "stoch": _stoch_signal(stoch),
            "trix":  np.clip(trix.line.iloc[-1] * 10, -1, 1),   # normalisation
            "cci":   np.clip(cci.iloc[-1] / 150, -1, 1),
            "mom":   np.sign(mom.iloc[-1]) * min(abs(mom.iloc[-1]) / df["close"].iloc[-1] * 20, 1.0),
        }
        score = sum(signals[k] * getattr(weights, k) for k in signals)
        return cls(score=np.clip(score, -1, 1), rsi=rsi.iloc[-1], components=signals)
```

**Poids par défaut des oscillateurs de momentum dans `config/strategies.yaml`** :

```yaml
momentum_weights:
  rsi:   0.35   # ⭐⭐⭐⭐⭐ — référence absolue
  trix:  0.25   # 30% win rate — meilleur filtre bruit
  cci:   0.20   # Réactivité aux retournements
  stoch: 0.12   # 25% win rate — confirmation uniquement
  mom:   0.08   # 26% win rate — accélération secondaire
```

---

### 5.4 Indicateurs de volume

Vue d'ensemble :

| Indicateur | Paramètres | Composants | Usage principal | Compatibilité Ichimoku |
|---|---|---|---|---|
| **OBV** | Aucun (cumulatif) | Volume cumulé directionnel | Confirme ou invalide les breakouts | OBV croissant + prix sortant du Kumo → signal fort |
| **VWAP** | Session (reset quotidien) | Somme(PV) / Somme(V) | Référence institutionnelle intraday | Prix > VWAP + au-dessus Kumo → double confirmation |
| **CMF** | 20 | Distribution / volume 20 barres | Divergences flux/prix | CMF positif + Kumo bullish → entrée long validée |
| **Volume Profile** | N barres | POC, VAH, VAL, HVN, LVN | Niveaux S/R par concentration de volume | Zones HVN souvent alignées avec bords du Kumo |

---

#### 5.4.1 OBV (On-Balance Volume)

**Paramètres** : Aucun (cumulatif depuis le début de la série)

**Composants** :
- Si `close(t) > close(t−1)` : `OBV(t) = OBV(t−1) + volume(t)`
- Si `close(t) < close(t−1)` : `OBV(t) = OBV(t−1) − volume(t)`
- Si `close(t) = close(t−1)` : `OBV(t) = OBV(t−1)`

**Signaux** :
- OBV en hausse avec prix en hausse → tendance haussière confirmée par le volume
- OBV en baisse avec prix en hausse → **divergence baissière** → attention breakout fake
- Pente OBV sur 5 barres → `np.sign(obv[-1] − obv[-5])` → signal `+1 / −1`
- OBV fait un nouveau plus haut avant le prix → signal anticipateur haussier

**Usage Ichimoku** : Confirmation critique des breakouts. Un prix sortant du Kumo vers le haut **sans** hausse de l'OBV est classé comme breakout non confirmé → conviction réduite de 30%.

---

#### 5.4.2 VWAP (Volume Weighted Average Price)

**Paramètres** : Reset quotidien (session). Fenêtre roulante de N barres pour usage multi-day.

**Composants** :
- **Prix typique** : `TP = (high + low + close) / 3`
- `VWAP = Σ(TP × volume) / Σ(volume)` depuis le début de la session

**Signaux** :
- Prix au-dessus du VWAP → pression acheteuse dominante → signal `+0.5`
- Prix en-dessous du VWAP → pression vendeuse dominante → signal `−0.5`
- Retour au VWAP après un mouvement → niveau de support/résistance intraday
- Écart prix/VWAP > 1.5% → extension → risque de retour à la moyenne

**Usage institutionnel** : Les desk institutionnels benchmarkent leurs exécutions au VWAP. Un prix bien au-dessus signifie que les acheteurs sont agressifs; en-dessous, les vendeurs. Donne un contexte de "flow" que les indicateurs techniques classiques ne voient pas.

---

#### 5.4.3 CMF — Chaikin Money Flow (20)

**Paramètres** : `period=20`

**Composants** :
- **Money Flow Multiplier** : `((close − low) − (high − close)) / (high − low)`
- **Money Flow Volume** : `MFM × volume`
- **CMF** : `Σ(MFV, 20) / Σ(volume, 20)` → plage `[−1, +1]`

**Signaux** :
- CMF > +0.1 → flux acheteur net → signal `+0.6`
- CMF < −0.1 → flux vendeur net → signal `−0.6`
- CMF proche de 0 → marché indécis → signal ignoré
- **Divergence** : prix fait un nouveau plus bas, CMF remonte → capitulation partielle, rebond probable
- CMF positif avec Kumo bullish → entrée long fortement validée par le flux

---

#### 5.4.4 Volume Profile

**Paramètres** : `lookback_bars=200` (configurable), `bins=50`

**Composants** :
- **POC** (Point of Control) : niveau de prix avec le plus grand volume échangé
- **VAH** (Value Area High) : borne haute de la zone où ~70% du volume s'est tradé
- **VAL** (Value Area Low) : borne basse de la value area
- **HVN** (High Volume Node) : zone de fort volume → support/résistance forte
- **LVN** (Low Volume Node) : zone de faible volume → prix traverse rapidement

**Signaux** :
- Prix approche d'un HVN → résistance/support probable → réduire le TP au niveau HVN
- Prix traverse un LVN → accélération probable du mouvement → augmenter le TP
- POC proche du Kijun-sen ou bord du Kumo → double niveau de confluence → stop plus serré
- Breakout au-dessus d'un HVN + volume fort → signal de continuation fort

**Compatibilité Kumo** : Les HVN s'alignent fréquemment avec les bords du Kumo (Senkou Span A/B). Cette confluence renforce la validité des niveaux comme supports/résistances.

---

#### 5.4.5 Code — `src/indicators/volume.py`

```python
# src/indicators/volume.py

from dataclasses import dataclass
import pandas as pd
import numpy as np


@dataclass
class VolumeProfileResult:
    poc: float          # Point of Control
    vah: float          # Value Area High
    val: float          # Value Area Low
    hvn_levels: list[float]  # High Volume Nodes
    lvn_levels: list[float]  # Low Volume Nodes


def compute_obv(df: pd.DataFrame) -> pd.Series:
    direction = np.sign(df["close"].diff()).fillna(0)
    return (direction * df["volume"]).cumsum()


def compute_vwap(df: pd.DataFrame) -> pd.Series:
    """VWAP roulant sur toute la série (adapté pour timeframes daily+)."""
    tp     = (df["high"] + df["low"] + df["close"]) / 3
    cumvol = df["volume"].cumsum()
    cumtpv = (tp * df["volume"]).cumsum()
    return cumtpv / cumvol.replace(0, np.nan)


def compute_cmf(df: pd.DataFrame, period=20) -> pd.Series:
    hl_range = (df["high"] - df["low"]).replace(0, np.nan)
    mfm = ((df["close"] - df["low"]) - (df["high"] - df["close"])) / hl_range
    mfv = mfm * df["volume"]
    return mfv.rolling(period).sum() / df["volume"].rolling(period).sum().replace(0, np.nan)


def compute_volume_profile(df: pd.DataFrame, lookback=200, bins=50) -> VolumeProfileResult:
    """
    Calcule la distribution de volume par niveau de prix sur les N dernières barres.
    Identifie POC, VAH, VAL, HVN, LVN.
    """
    data = df.tail(lookback)
    price_min = data["low"].min()
    price_max = data["high"].max()
    price_bins = np.linspace(price_min, price_max, bins + 1)
    volume_per_bin = np.zeros(bins)

    for _, row in data.iterrows():
        bar_range = row["high"] - row["low"]
        if bar_range == 0:
            continue
        for i in range(bins):
            overlap = min(row["high"], price_bins[i + 1]) - max(row["low"], price_bins[i])
            if overlap > 0:
                volume_per_bin[i] += row["volume"] * (overlap / bar_range)

    poc_idx = int(np.argmax(volume_per_bin))
    poc = (price_bins[poc_idx] + price_bins[poc_idx + 1]) / 2

    total_vol = volume_per_bin.sum()
    value_area_target = total_vol * 0.70
    va_vol = volume_per_bin[poc_idx]
    lo, hi = poc_idx, poc_idx
    while va_vol < value_area_target and (lo > 0 or hi < bins - 1):
        extend_up   = volume_per_bin[hi + 1] if hi + 1 < bins else 0
        extend_down = volume_per_bin[lo - 1] if lo > 0 else 0
        if extend_up >= extend_down:
            hi += 1; va_vol += extend_up
        else:
            lo -= 1; va_vol += extend_down

    mean_vol = volume_per_bin.mean()
    hvn = [(price_bins[i] + price_bins[i+1]) / 2
           for i in range(bins) if volume_per_bin[i] > mean_vol * 1.5]
    lvn = [(price_bins[i] + price_bins[i+1]) / 2
           for i in range(bins) if volume_per_bin[i] < mean_vol * 0.5]

    return VolumeProfileResult(
        poc=poc,
        vah=(price_bins[hi] + price_bins[hi + 1]) / 2,
        val=(price_bins[lo] + price_bins[lo + 1]) / 2,
        hvn_levels=hvn,
        lvn_levels=lvn,
    )


@dataclass
class VolumeScore:
    score: float
    components: dict
    vp: VolumeProfileResult | None = None

    @classmethod
    def compute(cls, df: pd.DataFrame, weights: "VolumeWeights") -> "VolumeScore":
        obv  = compute_obv(df)
        vwap = compute_vwap(df)
        cmf  = compute_cmf(df, period=20)
        vp   = compute_volume_profile(df, lookback=200)

        obv_slope     = np.sign(obv.iloc[-1] - obv.iloc[-5])
        price_vs_vwap = np.sign(df["close"].iloc[-1] - vwap.iloc[-1])
        cmf_signal    = np.clip(cmf.iloc[-1] * 5, -1, 1)    # CMF plage ±0.2 typique → amplifié
        vp_signal     = _volume_profile_signal(df["close"].iloc[-1], vp)

        signals = {
            "obv":  obv_slope,
            "vwap": price_vs_vwap,
            "cmf":  cmf_signal,
            "vp":   vp_signal,
        }
        score = sum(signals[k] * getattr(weights, k) for k in signals)
        return cls(score=np.clip(score, -1, 1), components=signals, vp=vp)


def _volume_profile_signal(close: float, vp: VolumeProfileResult) -> float:
    """
    +0.5 si le prix est au-dessus du POC (pression haussière structurelle).
    −0.5 si en-dessous.
    Réduit à 0 si le prix est entre VAL et VAH (zone de valeur — indécis).
    """
    if vp.val <= close <= vp.vah:
        return 0.0      # Dans la value area → signal neutre
    return 0.5 if close > vp.poc else -0.5
```

**Poids par défaut des indicateurs de volume dans `config/strategies.yaml`** :

```yaml
volume_weights:
  obv:  0.40   # Confirmation directionnelle primaire
  cmf:  0.30   # Flux de capital net
  vwap: 0.20   # Contexte institutionnel
  vp:   0.10   # Contexte structurel (POC/VAH/VAL)
```

### 5.5 Score composite final

```
Score = w_ichimoku × S_ichimoku
      + w_trend    × S_trend
      + w_momentum × S_momentum
      + w_volume   × S_volume
```

Poids par défaut dans `config/strategies.yaml` :

```yaml
default_weights:
  ichimoku:  0.40   # Pilier central
  trend:     0.25
  momentum:  0.20
  volume:    0.15
```

**Seuils de qualification** :

| Score | Qualification | Action |
|---|---|---|
| `abs(score) < 0.4` | Signal faible | Ignoré |
| `0.4 ≤ abs(score) < 0.6` | Signal modéré | Watchlist uniquement |
| `abs(score) ≥ 0.6` | Signal fort | Proposition de trade |

---

## 6. Stratégies de Trading

Six stratégies couvrent l'ensemble des régimes de marché. Chacune a ses propres poids d'indicateurs, sa logique d'entrée/sortie, son circuit breaker individuel et son calcul de score de confiance.

**Règle commune à toutes les stratégies** : Ichimoku doit être aligné avec la direction proposée (score ≠ 0). Tout signal Ichimoku neutre (prix dans le Kumo) bloque la stratégie, quelle qu'elle soit.

### 6.1 Ichimoku Trend Following — Stratégie principale

**Style** : Suivi de tendance multi-timeframe | **Horizon** : 3–15 jours

**Marchés** : Forex (majors), Crypto (top 10), Actions/ETF

**R/R minimum** : 1:2

**Régimes favorables** : `risk_on` fort (prob > 0.65) ou `risk_off` fort (prob > 0.65) — tendance directionnelle claire. Évitée en régime `transition`.

---

#### Conditions d'entrée LONG

Toutes les conditions suivantes doivent être vraies :

| # | Condition | Indicateur | Seuil |
|---|---|---|---|
| 1 | Prix **au-dessus** du Kumo | Ichimoku (Senkou A & B) | `close > max(senkou_a, senkou_b)` |
| 2 | **Tenkan-sen croise Kijun-sen à la hausse** | Ichimoku | `tenkan[-1] > kijun[-1]` ET `tenkan[-2] <= kijun[-2]` |
| 3 | **Chikou Span confirme** | Ichimoku | `chikou[-1] > close[-27]` (prix actuel > prix il y a 26 barres) |
| 4 | **Supertrend haussier** | Supertrend(10, 3) | `supertrend_direction = +1` |
| 5 | **Tendance en place** | ADX(14) | `adx > 25` |

**Confirmation optionnelle** (+0.10 conviction si présente) : Aroon Up > 70 ET Aroon Down < 30.

#### Conditions d'entrée SHORT

Inverse exact des conditions LONG :

| # | Condition | Seuil |
|---|---|---|
| 1 | Prix **sous** le Kumo | `close < min(senkou_a, senkou_b)` |
| 2 | **Tenkan croise Kijun à la baisse** | `tenkan[-1] < kijun[-1]` ET `tenkan[-2] >= kijun[-2]` |
| 3 | **Chikou Span sous le prix** | `chikou[-1] < close[-27]` |
| 4 | **Supertrend baissier** | `supertrend_direction = -1` |
| 5 | **ADX > 25** | tendance baissière en place |

#### Conditions de sortie

| Déclencheur | Description |
|---|---|
| **Sortie principale** | Tenkan-sen recroise Kijun-sen en sens inverse |
| **Sortie d'urgence** | Prix rentre dans le Kumo (zone d'indécision) |
| **Stop-loss** | 1.5 × ATR(14) sous le dernier creux (long) / au-dessus du dernier pic (short) |
| **TP partiel 50%** | À 1R (distance stop × ratio_rr_1) |
| **TP final** | Trailing stop = Parabolic SAR |

#### Timeframe

- **Daily** : identification de la tendance, validation Ichimoku et régime
- **H4** : timing précis du croisement Tenkan/Kijun, entrée

#### Score de confiance

```
confirmations = [
    ichimoku_aligned,    # Obligatoire — weight: 2 pts
    tenkan_kijun_cross,  # Obligatoire — weight: 2 pts
    chikou_confirms,     # Obligatoire — weight: 2 pts
    supertrend_aligned,  # Obligatoire — weight: 2 pts
    adx_above_25,        # Obligatoire — weight: 1 pt
    aroon_confirms,      # Optionnel  — weight: 1 pt
    macd_aligned,        # Optionnel  — weight: 0.5 pt
    volume_confirms,     # Optionnel (OBV slope) — weight: 0.5 pt
]
max_points = 11
confiance = sum(w for cond, w in confirmations if cond) / max_points
```

Proposition envoyée uniquement si `confiance >= 0.82` (au moins les 4 conditions obligatoires).

#### Poids d'indicateurs spécifiques à cette stratégie

```yaml
# config/strategies.yaml — ichimoku_trend_following
strategy_weights:
  ichimoku:  0.50   # Pilier unique — poids renforcé vs default 0.40
  trend:     0.30   # Supertrend + ADX critiques ici
  momentum:  0.10   # RSI secondaire uniquement
  volume:    0.10
```

#### Pseudo-code Python

```python
# src/strategies/ichimoku_trend_following.py

class IchimokuTrendFollowing:
    NAME = "ichimoku_trend_following"

    def generate_signals(
        self, universe: list[str], context: CycleContext
    ) -> list[Signal]:
        signals = []
        for asset in universe:
            df_daily = context.ohlcv(asset, "1d", bars=300)
            df_h4    = context.ohlcv(asset, "4h", bars=200)

            # Ichimoku sur Daily (tendance)
            ichi_d = compute_ichimoku(df_daily)
            if ichi_d.score == 0.0:
                continue  # prix dans le Kumo — pas de signal

            # ADX sur Daily
            adx_d = compute_adx(df_daily)
            if adx_d.adx.iloc[-1] < 25:
                continue  # pas de tendance établie

            # Supertrend sur H4 (timing)
            st_h4 = compute_supertrend(df_h4)
            direction = 1 if ichi_d.score > 0 else -1
            if float(st_h4.iloc[-1]) != direction:
                continue  # Supertrend non aligné

            # Croisement Tenkan/Kijun sur H4
            ichi_h4 = compute_ichimoku(df_h4)
            cross = _detect_tenkan_kijun_cross(ichi_h4)
            if cross != direction:
                continue  # pas de croisement dans la bonne direction

            # Chikou Span sur H4
            if not _chikou_confirms(ichi_h4, direction):
                continue

            # Calcul entrée / stop / TP
            entry = df_h4["close"].iloc[-1]
            atr   = _compute_atr(df_h4, 14).iloc[-1]
            if direction == 1:
                stop = entry - 1.5 * atr
                tp1  = entry + (entry - stop)        # 1R
                tp2  = entry + 2.0 * (entry - stop)  # 2R
            else:
                stop = entry + 1.5 * atr
                tp1  = entry - (stop - entry)
                tp2  = entry - 2.0 * (stop - entry)

            rr = abs(tp2 - entry) / abs(stop - entry)
            if rr < 2.0:
                continue  # R/R insuffisant

            confiance = _compute_confidence_itf(ichi_d, ichi_h4, adx_d, st_h4, context)
            if confiance < 0.82:
                continue

            signals.append(Signal(
                asset=asset, strategy=self.NAME,
                side="long" if direction == 1 else "short",
                entry=entry, stop=stop, tp=[tp1, tp2],
                confidence=confiance, timeframe="4h",
                indicators_used=["ichimoku_daily", "ichimoku_h4", "supertrend_h4",
                                  "adx_daily", "tenkan_kijun_cross_h4"],
            ))
        return signals


def _detect_tenkan_kijun_cross(ichi: IchimokuResult) -> int:
    """Retourne +1 (cross haussier), -1 (cross baissier), 0 (pas de cross)."""
    t, k = ichi.tenkan, ichi.kijun
    if t.iloc[-1] > k.iloc[-1] and t.iloc[-2] <= k.iloc[-2]:  return  1
    if t.iloc[-1] < k.iloc[-1] and t.iloc[-2] >= k.iloc[-2]:  return -1
    return 0


def _chikou_confirms(ichi: IchimokuResult, direction: int) -> bool:
    chikou = ichi.chikou
    if pd.isna(chikou.iloc[-1]):
        return False
    ref_price = chikou.index.get_loc(chikou.index[-1]) - 26
    return (chikou.iloc[-1] > 0) == (direction == 1)
```

#### Circuit breaker individuel

```yaml
circuit_breaker:
  strategy: ichimoku_trend_following
  dd_7d_vs_median_threshold: 2.0   # Désactivation si DD 7j > 2× DD médian historique
  min_history_days: 30              # Ne s'active qu'après 30 jours de données
  cooldown_days: 7                  # Réactivation manuelle après 7 jours minimum
  auto_alert: telegram              # Notification immédiate
```

---

### 6.2 Breakout Momentum

**Style** : Breakout sur volatilité expansive | **Horizon** : 1–5 jours

**Marchés** : Crypto (forte volatilité), Actions (breakouts post-earnings)

**R/R minimum** : 1:2

**Régimes favorables** : `risk_on` (prob > 0.55). Désactivée en `risk_off` extrême ou volatilité `extreme`.

---

#### Conditions d'entrée

| # | Condition | Indicateur | Seuil |
|---|---|---|---|
| 1 | Prix **casse la Bollinger Band haute** | Bollinger(20, ±2σ) | `close > upper_band` |
| 2 | **Volume anormal** | OBV | Pente OBV 3 barres > 0 ET volume barre > 1.5 × volume médian 20j |
| 3 | **Tendance naissante** | Aroon(14) | `aroon_up > 70` |
| 4 | **Kumo haussier** | Ichimoku | `senkou_a > senkou_b` (Kumo vert) ET prix **au-dessus** du Kumo |
| 5 | **Momentum positif** | MACD | `histogram > 0` et croissant (2 dernières barres) |

**Pour le SHORT** : cassure sous la Bollinger Band basse, OBV pente négative, Aroon Down > 70, Kumo baissier.

#### Conditions de sortie

| Déclencheur | Description |
|---|---|
| **TP** | Prochain niveau HVN (Volume Profile) — ou +3R si pas de HVN identifié |
| **Sortie technique** | RSI > 70 (long) ou RSI < 30 (short) → prise de profit partielle |
| **Stop-loss** | Parabolic SAR retourne (croisement de direction) |
| **Stop fixe** | Sous le dernier creux significatif (avant la cassure) |

#### Score de confiance

```
points = {
    bollinger_break:    2,   # Obligatoire
    obv_surge:          2,   # Obligatoire
    aroon_above_70:     2,   # Obligatoire
    ichimoku_kumo_ok:   1,   # Obligatoire
    macd_histogram_pos: 1,   # Obligatoire
    cci_above_100:      1,   # Optionnel (+1 si CCI > 100 pour long)
    volume_profile_lvn: 1,   # Optionnel (cassure d'un LVN → accélération)
}
# Seuil minimum : 8/10 → confiance 80%
```

#### Poids d'indicateurs spécifiques

```yaml
strategy_weights:  # breakout_momentum
  ichimoku:  0.20   # Contexte de fond, poids réduit
  trend:     0.35   # Aroon + Bollinger critiques
  momentum:  0.25   # MACD histogramme
  volume:    0.20   # OBV surge — critère clé
```

#### Pseudo-code Python

```python
# src/strategies/breakout_momentum.py

class BreakoutMomentum:
    NAME = "breakout_momentum"

    def generate_signals(
        self, universe: list[str], context: CycleContext
    ) -> list[Signal]:
        signals = []
        for asset in universe:
            df = context.ohlcv(asset, "4h", bars=150)

            bb    = compute_bollinger(df)
            obv   = compute_obv(df)
            aroon = compute_aroon(df)
            ichi  = compute_ichimoku(df)
            macd  = compute_macd(df)
            vp    = compute_volume_profile(df)

            close = df["close"].iloc[-1]
            vol_20_med = df["volume"].rolling(20).median().iloc[-1]

            # Direction
            long_break  = close > bb.upper.iloc[-1]
            short_break = close < bb.lower.iloc[-1]
            if not (long_break or short_break):
                continue

            direction = 1 if long_break else -1

            # OBV surge
            obv_slope = obv.iloc[-1] - obv.iloc[-3]
            vol_surge = df["volume"].iloc[-1] > vol_20_med * 1.5
            if direction * obv_slope <= 0 or not vol_surge:
                continue

            # Aroon
            aroon_ok = (aroon.up.iloc[-1] > 70 if direction == 1
                        else aroon.down.iloc[-1] > 70)
            if not aroon_ok:
                continue

            # Ichimoku — Kumo aligné
            kumo_bullish = ichi.senkou_a.iloc[-1] > ichi.senkou_b.iloc[-1]
            if direction == 1 and (not kumo_bullish or close < ichi.senkou_a.iloc[-1]):
                continue
            if direction == -1 and (kumo_bullish or close > ichi.senkou_b.iloc[-1]):
                continue

            # MACD histogramme croissant
            hist_growing = (direction * (macd.histogram.iloc[-1] - macd.histogram.iloc[-2]) > 0)
            if not hist_growing:
                continue

            # Entry / Stop / TP
            entry = close
            atr   = _compute_atr(df, 14).iloc[-1]
            if direction == 1:
                stop = df["low"].iloc[-5:-1].min()   # dernier creux avant cassure
                next_hvn = _next_hvn_above(close, vp)
                tp1 = next_hvn if next_hvn else entry + 2 * (entry - stop)
                tp2 = entry + 3 * (entry - stop)
            else:
                stop = df["high"].iloc[-5:-1].max()
                next_hvn = _next_hvn_below(close, vp)
                tp1 = next_hvn if next_hvn else entry - 2 * (stop - entry)
                tp2 = entry - 3 * (stop - entry)

            rr = abs(tp2 - entry) / abs(stop - entry)
            if rr < 2.0:
                continue

            confiance = _compute_confidence_bm(bb, obv, aroon, ichi, macd, vp, direction)
            if confiance < 0.80:
                continue

            signals.append(Signal(
                asset=asset, strategy=self.NAME,
                side="long" if direction == 1 else "short",
                entry=entry, stop=stop, tp=[tp1, tp2],
                confidence=confiance, timeframe="4h",
                indicators_used=["bollinger", "obv", "aroon", "ichimoku", "macd", "volume_profile"],
            ))
        return signals
```

#### Circuit breaker individuel

```yaml
circuit_breaker:
  strategy: breakout_momentum
  dd_7d_vs_median_threshold: 2.0
  min_history_days: 30
  cooldown_days: 5
  extra_condition: "disable_if_regime == extreme_volatility"
```

---

### 6.3 Mean Reversion

**Style** : Retour à la moyenne sur surventes/surachats | **Horizon** : 1–3 jours

**Marchés** : Forex (ranges fréquents), Actions blue chips (grande liquidité)

**R/R minimum** : 1:1.5

**Régimes favorables** : `transition` ou `risk_off` modéré. **Désactivée** en tendance forte (ADX > 35) ou `risk_off` extrême.

---

#### Conditions d'entrée LONG (rebond sur survente)

| # | Condition | Indicateur | Seuil |
|---|---|---|---|
| 1 | **RSI en survente** | RSI(14) | `rsi < 30` |
| 2 | **Prix sous ou dans le Kumo** | Ichimoku | `close <= max(senkou_a, senkou_b)` |
| 3 | **CCI extrême négatif** | CCI(20) | `cci < -100` |
| 4 | **Prix sous la Kijun-sen** | Ichimoku | `close < kijun` |
| 5 | **Confirmation de retournement** | Stochastique(14,3,5) | `%K[-1] > %D[-1]` ET `%K[-2] <= %D[-2]` (croisement haussier en zone < 20) |

**Pour le SHORT** (rebond sur surachat) : RSI > 70, prix au-dessus ou dans le Kumo, CCI > +100, close > kijun, Stochastique croise à la baisse en zone > 80.

#### Conditions de sortie

| Déclencheur | Description |
|---|---|
| **TP1** | Retour à la Kijun-sen |
| **TP2** | Retour à la Tenkan-sen (si plus favorable) |
| **Stop-loss** | 1.0 × ATR(14) sous l'entrée (long) / au-dessus (short) |
| **Sortie de protection** | Si RSI repasse sous 25 (long) → renforcement de la survente, sortir |

#### Score de confiance

```
points = {
    rsi_oversold:       2,   # Obligatoire
    price_at_kumo:      2,   # Obligatoire
    cci_extreme:        1,   # Obligatoire
    below_kijun:        1,   # Obligatoire
    stoch_cross:        2,   # Obligatoire (timing du retournement)
    cmf_diverging:      1,   # Optionnel (CMF remonte alors que prix baisse)
    volume_capitulation: 1,  # Optionnel (volume anormalement élevé = capitulation)
}
# Seuil : 8/10 → confiance 80%
```

#### Poids d'indicateurs spécifiques

```yaml
strategy_weights:  # mean_reversion
  ichimoku:  0.35   # Kijun-sen comme target, Kumo comme contexte
  trend:     0.10   # Poids réduit — stratégie contre-tendance
  momentum:  0.45   # RSI + Stoch + CCI critiques
  volume:    0.10
```

#### Pseudo-code Python

```python
# src/strategies/mean_reversion.py

class MeanReversion:
    NAME = "mean_reversion"

    def generate_signals(
        self, universe: list[str], context: CycleContext
    ) -> list[Signal]:
        # Désactivation en tendance forte
        if context.regime.macro == "risk_off" and context.regime.volatility == "extreme":
            return []

        signals = []
        for asset in universe:
            df = context.ohlcv(asset, "1h", bars=200)

            rsi   = compute_rsi(df, 14)
            ichi  = compute_ichimoku(df)
            cci   = compute_cci(df, 20)
            stoch = compute_stochastic(df, 14, 3, 5)
            adx   = compute_adx(df, 14)
            cmf   = compute_cmf(df, 20)

            # Filtre ADX — pas de mean reversion en tendance forte
            if adx.adx.iloc[-1] > 35:
                continue

            close   = df["close"].iloc[-1]
            kijun   = ichi.kijun.iloc[-1]
            kumo_top = max(ichi.senkou_a.iloc[-1], ichi.senkou_b.iloc[-1])

            # Direction
            long_setup  = (rsi.iloc[-1] < 30 and close <= kumo_top
                           and cci.iloc[-1] < -100 and close < kijun)
            short_setup = (rsi.iloc[-1] > 70 and close >= kumo_top
                           and cci.iloc[-1] > 100  and close > kijun)

            if not (long_setup or short_setup):
                continue

            direction = 1 if long_setup else -1

            # Confirmation Stochastique (timing retournement)
            stoch_cross_ok = _stoch_reversal_cross(stoch, direction)
            if not stoch_cross_ok:
                continue  # Attendre la confirmation

            # Entry / Stop / TP
            entry = close
            atr   = _compute_atr(df, 14).iloc[-1]
            stop  = entry - direction * atr   # 1 ATR dans la direction opposée

            if direction == 1:
                tp1 = kijun                          # TP1 = Kijun-sen
                tp2 = ichi.tenkan.iloc[-1]           # TP2 = Tenkan-sen
            else:
                tp1 = kijun
                tp2 = ichi.tenkan.iloc[-1]

            rr = abs(tp1 - entry) / abs(stop - entry)
            if rr < 1.5:
                continue

            confiance = _compute_confidence_mr(rsi, ichi, cci, stoch, cmf, direction)
            if confiance < 0.75:
                continue

            signals.append(Signal(
                asset=asset, strategy=self.NAME,
                side="long" if direction == 1 else "short",
                entry=entry, stop=stop, tp=[tp1, tp2],
                confidence=confiance, timeframe="1h",
                indicators_used=["rsi", "ichimoku", "cci", "stochastic", "cmf"],
            ))
        return signals


def _stoch_reversal_cross(stoch: StochResult, direction: int) -> bool:
    """Croisement %K/%D dans la zone extrême appropriée."""
    k, d = stoch.k, stoch.d
    if direction == 1:
        return k.iloc[-2] <= d.iloc[-2] and k.iloc[-1] > d.iloc[-1] and k.iloc[-1] < 25
    return k.iloc[-2] >= d.iloc[-2] and k.iloc[-1] < d.iloc[-1] and k.iloc[-1] > 75
```

#### Circuit breaker individuel

```yaml
circuit_breaker:
  strategy: mean_reversion
  dd_7d_vs_median_threshold: 2.0
  min_history_days: 30
  cooldown_days: 5
  extra_condition: "disable_if_adx_mean_14d > 30"  # marché trop directionnel
```

---

### 6.4 Divergence Hunter

**Style** : Divergences RSI/MACD vs prix | **Horizon** : 2–7 jours

**Marchés** : Forex, Crypto, Actions — les trois classes

**R/R minimum** : 1:2

**Régimes favorables** : Tous régimes. Particulièrement efficace en `transition`. Signal fort en `risk_off` pour les divergences haussières (rebonds bear market).

---

#### Principe

Une **divergence haussière** : le prix fait un nouveau plus bas mais l'oscillateur (RSI ou MACD) fait un plus bas **moins bas** → momentum vendeur qui s'affaiblit → retournement probable.

Une **divergence baissière** : le prix fait un nouveau plus haut mais l'oscillateur fait un plus haut **moins haut** → momentum acheteur qui s'épuise.

#### Conditions d'entrée

| # | Condition | Indicateur | Description |
|---|---|---|---|
| 1 | **Divergence détectée** | RSI(14) OU MACD | Divergence haussière ou baissière sur ≥ 2 pivots consécutifs |
| 2 | **Confirmation flux** | CMF(20) | Flux contraire au mouvement de prix (CMF > 0 sur divergence haussière alors que prix baisse) |
| 3 | **Contexte Ichimoku** | Ichimoku | Prix proche du Kumo (±5% de distance) ou rebondissant sur Kijun/Tenkan |
| 4 | **RSI en zone extrême** | RSI(14) | RSI < 40 (divergence haussière) OU RSI > 60 (divergence baissière) |
| 5 | **Momentum TRIX** | TRIX(15,9) | Histogramme TRIX diverge dans le même sens que RSI |

#### Algorithme de détection de divergence

```python
# src/indicators/divergence.py

def detect_divergence(
    price: pd.Series,
    oscillator: pd.Series,
    lookback: int = 50,
    min_pivot_distance: int = 5,
) -> DivergenceResult:
    """
    Détecte divergences haussières et baissières sur les N dernières barres.
    Retourne le type (+1 haussier, -1 baissier, 0 aucune) et la confiance.
    """
    price_pivots_low  = _find_pivot_lows(price,  lookback, min_pivot_distance)
    price_pivots_high = _find_pivot_highs(price, lookback, min_pivot_distance)
    osc_pivots_low    = _find_pivot_lows(oscillator, lookback, min_pivot_distance)
    osc_pivots_high   = _find_pivot_highs(oscillator, lookback, min_pivot_distance)

    # Divergence haussière : prix new low, oscillateur higher low
    bull_div = _check_bullish_divergence(
        price_pivots_low, osc_pivots_low, min_pivots=2
    )
    # Divergence baissière : prix new high, oscillateur lower high
    bear_div = _check_bearish_divergence(
        price_pivots_high, osc_pivots_high, min_pivots=2
    )

    if bull_div.found and not bear_div.found:
        return DivergenceResult(type=1,  confidence=bull_div.strength)
    if bear_div.found and not bull_div.found:
        return DivergenceResult(type=-1, confidence=bear_div.strength)
    return DivergenceResult(type=0, confidence=0.0)


def _check_bullish_divergence(
    price_lows: list[Pivot], osc_lows: list[Pivot], min_pivots: int
) -> DivergenceCheck:
    """
    Aligne les pivots bas price/oscillateur par timestamp.
    Vérifie que price_low2 < price_low1 ET osc_low2 > osc_low1.
    Force = amplitude relative de la divergence.
    """
    ...
```

#### Conditions de sortie

| Déclencheur | Description |
|---|---|
| **Disparition de la divergence** | L'oscillateur confirme le nouveau pivot → sortir |
| **TP1** | Bord du Kumo (Senkou A ou B selon direction) |
| **TP2** | Kijun-sen ou Tenkan-sen |
| **Stop-loss** | Sous le dernier pivot de prix (divergence haussière) / au-dessus (baissière) |

#### Score de confiance

```
points = {
    rsi_divergence:    3,   # Obligatoire (divergence RSI)
    macd_divergence:   2,   # Optionnel (double confirmation)
    cmf_contrarian:    2,   # Obligatoire
    ichimoku_context:  1,   # Obligatoire (prix près du Kumo)
    rsi_extreme_zone:  1,   # Obligatoire
    trix_diverges:     1,   # Optionnel
}
# Seuil : 7/10 → confiance 70% (seuil plus bas, signal plus rare et précieux)
```

#### Poids d'indicateurs spécifiques

```yaml
strategy_weights:  # divergence_hunter
  ichimoku:  0.30   # Contexte et targets
  trend:     0.10   # Secondaire
  momentum:  0.50   # RSI + MACD + TRIX — cœur de la stratégie
  volume:    0.10   # CMF uniquement
```

#### Pseudo-code Python

```python
# src/strategies/divergence_hunter.py

class DivergenceHunter:
    NAME = "divergence_hunter"

    def generate_signals(
        self, universe: list[str], context: CycleContext
    ) -> list[Signal]:
        signals = []
        for asset in universe:
            df = context.ohlcv(asset, "4h", bars=200)

            rsi  = compute_rsi(df, 14)
            macd = compute_macd(df)
            cmf  = compute_cmf(df, 20)
            ichi = compute_ichimoku(df)
            trix = compute_trix(df, 15, 9)

            # Détection divergences
            rsi_div  = detect_divergence(df["close"], rsi,          lookback=60)
            macd_div = detect_divergence(df["close"], macd.histogram, lookback=60)

            if rsi_div.type == 0:
                continue  # Pas de divergence RSI — critère obligatoire

            direction = rsi_div.type

            # Confirmation CMF contrarian
            cmf_ok = (direction == 1 and cmf.iloc[-1] > 0.05) or \
                     (direction == -1 and cmf.iloc[-1] < -0.05)
            if not cmf_ok:
                continue

            # Contexte Ichimoku (prix proche du Kumo ou rebond sur Kijun)
            close    = df["close"].iloc[-1]
            kumo_top = max(ichi.senkou_a.iloc[-1], ichi.senkou_b.iloc[-1])
            kumo_bot = min(ichi.senkou_a.iloc[-1], ichi.senkou_b.iloc[-1])
            dist_pct = min(abs(close - kumo_top), abs(close - kumo_bot)) / close
            near_kumo = dist_pct < 0.05
            on_kijun  = abs(close - ichi.kijun.iloc[-1]) / close < 0.02
            if not (near_kumo or on_kijun):
                continue

            # RSI en zone extrême
            rsi_extreme = (direction == 1 and rsi.iloc[-1] < 40) or \
                          (direction == -1 and rsi.iloc[-1] > 60)
            if not rsi_extreme:
                continue

            # Entry / Stop / TP
            entry = close
            atr   = _compute_atr(df, 14).iloc[-1]
            if direction == 1:
                stop = df["low"].rolling(10).min().iloc[-1] - 0.5 * atr
                tp1  = kumo_bot   # Bord bas du Kumo
                tp2  = ichi.kijun.iloc[-1]
            else:
                stop = df["high"].rolling(10).max().iloc[-1] + 0.5 * atr
                tp1  = kumo_top
                tp2  = ichi.kijun.iloc[-1]

            rr = abs(tp2 - entry) / abs(stop - entry)
            if rr < 2.0:
                continue

            confiance = _compute_confidence_dh(rsi_div, macd_div, cmf, ichi, trix, direction)
            if confiance < 0.70:
                continue

            signals.append(Signal(
                asset=asset, strategy=self.NAME,
                side="long" if direction == 1 else "short",
                entry=entry, stop=stop, tp=[tp1, tp2],
                confidence=confiance, timeframe="4h",
                indicators_used=["rsi_divergence", "macd_divergence", "cmf", "ichimoku"],
            ))
        return signals
```

#### Circuit breaker individuel

```yaml
circuit_breaker:
  strategy: divergence_hunter
  dd_7d_vs_median_threshold: 2.0
  min_history_days: 45   # Nécessite plus d'historique — les divergences sont rares
  cooldown_days: 7
```

---

### 6.5 Volume Profile Scalp

**Style** : Rebond sur niveaux structurels de volume | **Horizon** : 4–24 heures

**Marchés** : Crypto (liquidité élevée), Indices (DAX, SP500 via ETF)

**R/R minimum** : 1:1.5

**Régimes favorables** : Tous régimes avec volatilité `mid`. **Désactivée** si volatilité `extreme` (le Volume Profile perd sa validité en mouvement parabolique).

---

#### Conditions d'entrée

| # | Condition | Indicateur | Seuil |
|---|---|---|---|
| 1 | **Prix rebondit sur POC, VAH ou VAL** | Volume Profile | Distance au niveau ≤ 0.3% |
| 2 | **VWAP comme filtre directionnel** | VWAP | Long si `close > vwap`, Short si `close < vwap` |
| 3 | **Momentum dans la direction** | RSI(14) | RSI entre 40–60 (pas de surachat/survente — rebond propre) |
| 4 | **Ichimoku non contraire** | Ichimoku | Score Ichimoku ≠ oppose direction (accepte score = 0 contrairement aux autres) |
| 5 | **Pas de LVN entre entrée et TP** | Volume Profile | TP au prochain HVN, pas de LVN immédiat sur le chemin |

#### Conditions de sortie

| Déclencheur | Description |
|---|---|
| **TP rapide** | Prochain niveau HVN (High Volume Node) |
| **TP2 conservateur** | POC si prix était sur VAH/VAL |
| **Stop-loss** | 0.5 × ATR(14) — stop serré (scalp) |
| **Sortie temporelle** | Si la position est toujours ouverte après 4 barres H1 sans progression → sortir |

#### Score de confiance

```
points = {
    vp_level_hit:       3,   # Obligatoire (précision de la touche du niveau)
    vwap_aligned:       2,   # Obligatoire
    rsi_neutral:        1,   # Obligatoire (40–60)
    ichimoku_not_oppos: 1,   # Obligatoire
    cmf_confirms:       1,   # Optionnel
    momentum_pos:       1,   # Optionnel (Momentum(12) dans la direction)
    no_lvn_in_path:     1,   # Optionnel (chemin propre jusqu'au TP)
}
# Seuil : 7/10 → confiance 70%
```

#### Poids d'indicateurs spécifiques

```yaml
strategy_weights:  # volume_profile_scalp
  ichimoku:  0.15   # Contexte minimal
  trend:     0.10
  momentum:  0.25   # RSI + Momentum
  volume:    0.50   # Volume Profile + VWAP + CMF — cœur de la stratégie
```

#### Pseudo-code Python

```python
# src/strategies/volume_profile_scalp.py

class VolumeProfileScalp:
    NAME = "volume_profile_scalp"

    # Actif uniquement sur actifs haute liquidité
    ALLOWED_ASSET_CLASSES = ["crypto", "index"]

    def generate_signals(
        self, universe: list[str], context: CycleContext
    ) -> list[Signal]:
        if context.regime.volatility == "extreme":
            return []

        signals = []
        for asset in universe:
            if context.asset_class(asset) not in self.ALLOWED_ASSET_CLASSES:
                continue

            df = context.ohlcv(asset, "1h", bars=200)

            vp   = compute_volume_profile(df, lookback=200, bins=50)
            vwap = compute_vwap(df)
            rsi  = compute_rsi(df, 14)
            ichi = compute_ichimoku(df)
            cmf  = compute_cmf(df, 20)
            atr  = _compute_atr(df, 14)

            close = df["close"].iloc[-1]
            vwap_val = vwap.iloc[-1]

            # Trouver le niveau VP le plus proche
            vp_levels = [vp.poc, vp.vah, vp.val] + vp.hvn_levels
            nearest   = min(vp_levels, key=lambda lvl: abs(close - lvl))
            dist_pct  = abs(close - nearest) / close

            if dist_pct > 0.003:   # plus de 0.3% d'écart — pas assez précis
                continue

            # Direction basée sur VWAP
            direction = 1 if close > vwap_val else -1

            # Ichimoku non contraire (accepte neutre)
            ichi_score = compute_ichimoku(df).score
            if direction == 1 and ichi_score < -0.3:
                continue   # Ichimoku fortement bearish — éviter
            if direction == -1 and ichi_score > 0.3:
                continue

            # RSI neutre (40–60) — rebond propre, pas contre-tendance extrême
            if not (40 <= rsi.iloc[-1] <= 60):
                continue

            # TP = prochain HVN dans la direction
            tp = _next_hvn(close, direction, vp)
            if tp is None:
                continue
            stop = close - direction * 0.5 * atr.iloc[-1]

            rr = abs(tp - close) / abs(stop - close)
            if rr < 1.5:
                continue

            confiance = _compute_confidence_vps(dist_pct, vwap, rsi, ichi, cmf, direction)
            if confiance < 0.70:
                continue

            signals.append(Signal(
                asset=asset, strategy=self.NAME,
                side="long" if direction == 1 else "short",
                entry=close, stop=stop, tp=[tp],
                confidence=confiance, timeframe="1h",
                indicators_used=["volume_profile", "vwap", "rsi", "ichimoku"],
            ))
        return signals
```

#### Circuit breaker individuel

```yaml
circuit_breaker:
  strategy: volume_profile_scalp
  dd_7d_vs_median_threshold: 1.5   # Plus sensible — scalp à faible marge
  min_history_days: 20
  cooldown_days: 3
  extra_condition: "disable_if_regime.volatility == extreme"
```

---

### 6.6 Event-Driven Macro

**Style** : Capture des mouvements post-annonce | **Horizon** : 4–24 heures

**Marchés** : Forex (NFP, décisions taux BCE/Fed), Actions (earnings surprises)

**R/R minimum** : 1:2

**Régimes favorables** : Tous. **Règle absolue** : aucune position prise AVANT l'annonce. On attend la confirmation du mouvement.

---

#### Déclencheurs économiques surveillés

| Événement | Impact attendu | Fenêtre d'entrée |
|---|---|---|
| NFP (Non-Farm Payrolls) | USD, DXY | +15 min après l'annonce |
| Décision taux Fed (FOMC) | USD, indices, or | +15 min après la conférence |
| Décision taux BCE | EUR, DAX | +15 min après la conférence |
| CPI US | USD, or, BTC | +10 min après l'annonce |
| Earnings surprise >5% | Action individuelle | +15 min après ouverture |
| GDP surprise | Devise nationale | +15 min après l'annonce |

**Source des événements** : calendrier économique `data/calendar.py` (voir §7.1 — mapping source → consommateur).

#### Conditions d'entrée

| # | Condition | Description |
|---|---|---|
| 1 | **Événement macro vient de se produire** | `calendar.event_just_occurred(within_minutes=30)` |
| 2 | **Mouvement de prix significatif post-event** | Variation > 0.5% en 15 min (Forex) ou > 1.5% (Crypto/Actions) |
| 3 | **Volume anormal post-event** | Volume barre > 2× médian 20j |
| 4 | **Ichimoku aligne** | Score Ichimoku dans la direction du mouvement post-event |
| 5 | **Pas de retournement immédiat** | Prix ne revient pas à son niveau pré-annonce dans les 15 min |
| 6 | **CMF confirme le flux** | CMF > 0.1 (mouvement haussier) ou < −0.1 (baissier) |

#### Conditions de sortie

| Déclencheur | Description |
|---|---|
| **TP fin de journée** | Clôture de la session courante (H17 Paris, H20 New York) |
| **TP technique** | Prochain niveau Ichimoku (Kijun-sen ou bord Kumo) |
| **Stop-loss** | Retour au niveau pré-annonce (invalidation de la thèse) |
| **Sortie d'urgence** | Si un deuxième événement contradictoire arrive dans la fenêtre |

#### Score de confiance

```
points = {
    event_confirmed:     3,   # Obligatoire (événement dans calendrier)
    price_moved_clearly: 2,   # Obligatoire (seuil de mouvement respecté)
    volume_surge:        2,   # Obligatoire
    ichimoku_aligned:    2,   # Obligatoire
    no_reversal_15min:   1,   # Obligatoire
    cmf_confirms:        1,   # Optionnel
    news_pulse_aligned:  1,   # Optionnel (sentiment news = même direction)
    surprise_magnitude:  1,   # Optionnel (magnitude de la surprise > consensus)
}
# Seuil : 10/13 → confiance 77%
```

#### Poids d'indicateurs spécifiques

```yaml
strategy_weights:  # event_driven_macro
  ichimoku:  0.30
  trend:     0.20   # Supertrend post-event
  momentum:  0.20   # MACD pour confirmer l'impulsion
  volume:    0.30   # OBV + CMF — validation du flux post-event
```

#### Pseudo-code Python

```python
# src/strategies/event_driven_macro.py

class EventDrivenMacro:
    NAME = "event_driven_macro"

    def generate_signals(
        self, universe: list[str], context: CycleContext
    ) -> list[Signal]:
        # Vérifier si un événement macro vient de se produire
        recent_events = context.calendar.events_in_window(minutes_ago=30)
        if not recent_events:
            return []

        signals = []
        for event in recent_events:
            affected_assets = event.affected_assets  # ex: ["EURUSD", "GBPUSD"] pour BCE

            for asset in [a for a in affected_assets if a in universe]:
                df = context.ohlcv(asset, "1h", bars=50)

                # Prix pré-event vs post-event
                pre_event_price  = _price_at(df, event.timestamp - pd.Timedelta(minutes=5))
                post_event_price = df["close"].iloc[-1]
                if pre_event_price is None:
                    continue

                move_pct = (post_event_price - pre_event_price) / pre_event_price
                threshold = 0.005 if context.asset_class(asset) == "forex" else 0.015

                if abs(move_pct) < threshold:
                    continue  # mouvement insuffisant

                direction = 1 if move_pct > 0 else -1

                # Volume surge
                vol_20_med = df["volume"].rolling(20).median().iloc[-1]
                if df["volume"].iloc[-1] < vol_20_med * 2.0:
                    continue

                # Ichimoku aligné
                ichi = compute_ichimoku(df)
                if direction * ichi.score < 0:
                    continue   # Ichimoku contre le mouvement

                # Pas de retournement dans les 15 min post-event
                if _price_reverted(df, event.timestamp, pre_event_price, direction):
                    continue

                # CMF confirme
                cmf = compute_cmf(df, 20)
                if direction * cmf.iloc[-1] < 0.05:
                    continue

                # Entry / Stop / TP
                entry = post_event_price
                stop  = pre_event_price * (1 - direction * 0.001)  # retour pré-event = invalidation
                atr   = _compute_atr(df, 14).iloc[-1]
                tp1   = entry + direction * abs(entry - stop) * 1.5   # 1.5R
                tp2   = entry + direction * abs(entry - stop) * 2.5   # 2.5R

                rr = abs(tp2 - entry) / abs(stop - entry)
                if rr < 2.0:
                    continue

                confiance = _compute_confidence_edm(move_pct, df, ichi, cmf, event)
                if confiance < 0.77:
                    continue

                signals.append(Signal(
                    asset=asset, strategy=self.NAME,
                    side="long" if direction == 1 else "short",
                    entry=entry, stop=stop, tp=[tp1, tp2],
                    confidence=confiance, timeframe="1h",
                    catalysts=[f"{event.name} — {event.actual} vs {event.forecast}"],
                    indicators_used=["ichimoku", "cmf", "obv", "macd"],
                ))
        return signals
```

#### Circuit breaker individuel

```yaml
circuit_breaker:
  strategy: event_driven_macro
  dd_7d_vs_median_threshold: 2.0
  min_history_days: 30
  cooldown_days: 14   # Long cooldown — événements rares, pas de sur-ajustement
  extra_condition: "max_1_trade_per_event"  # Pas de pyramiding post-event
```

---

### 6.7 News-Driven Momentum

**Style** : Capture des mouvements provoqués par une actualité non planifiée au calendrier économique | **Horizon** : 2–24 heures.

**Marchés** : les trois classes, avec biais crypto (24/7 + annonces de listing/delisting très mouvantes).

**R/R minimum** : 1:2.

**Régimes favorables** : tous. Activée uniquement lorsque `NewsAnalystAgent` produit un **impact score** au-dessus du seuil. La stratégie peut **déclencher un cycle ad-hoc hors schedule** (cf. §15.1) si l'impact est élevé et qu'aucun cycle n'est imminent.

Différence avec `event_driven_macro` :

| Axe | event_driven_macro (§6.6) | news_driven_momentum (§6.7) |
|---|---|---|
| Déclencheur | Événement planifié du calendrier (NFP, FOMC, …) | Dépêche non planifiée (breaking news, listing crypto, sanction, défaut) |
| Source | `calendar.events_in_window` | `NewsAnalystAgent` + `binance.announcements` + `tradingeconomics.news` |
| Timing | +15 min après l'annonce chiffrée | ≤ 10 min après publication de la dépêche |
| Univers | Actifs directement liés à l'événement | Univers complet + actifs **tagués** (cf. `config/assets.yaml` champ `tags`) |

---

#### Sources news consommées

| Source | Type | Latence typique | Score d'impact par défaut |
|---|---|---|---|
| Reuters RSS | macro général | 5–15 min | 0.8 |
| Investing.com RSS | macro / equity | 5–15 min | 0.6 |
| Coindesk + TheBlock RSS | crypto | 5–10 min | 0.7 |
| Trading Economics news API | macro / analyst commentary | 2–10 min | 0.8 |
| Binance announcements | crypto listing/delisting/maintenance | < 1 min après pub | 0.9 (listing) / 1.0 (delisting) |
| Binance funding rate spike | signal dérivés | 5 min | 0.6 |
| Binance L/S ratio shift | signal dérivés | 15 min | 0.5 |
| Finnhub news (tickers US) | equity | 1–5 min | 0.7 |

Les pondérations sont configurables dans `config/sources.yaml` (champ `weight`). Une dépêche reprise par plusieurs sources voit son score agrégé (max + bonus par convergence).

#### Impact scoring

`NewsAnalystAgent` calcule un **impact score ∈ [0, 1]** par actif tagué à partir de :

```python
impact = 0.35 * source_weight           # crédibilité (RSS vs API officielle)
       + 0.20 * novelty                 # 1 si < 15 min, décroît linéairement jusqu'à 0 à 60 min
       + 0.15 * convergence             # bonus si N sources ≥ 2 dans la fenêtre
       + 0.15 * sentiment_magnitude     # |sentiment LLM| si articles assez nombreux
       + 0.15 * asset_specificity       # 1 si le ticker est nommé, 0.4 si thématique sectorielle seule
```

Un **seuil d'impact** déclenche la stratégie :

- `impact ≥ 0.60` → ajoute l'actif aux candidats du **cycle courant**
- `impact ≥ 0.80` → déclenche un **cycle ad-hoc** (hors schedule) limité à cet actif + actifs corrélés (cf. §15.1)

#### Conditions d'entrée

| # | Condition | Description |
|---|---|---|
| 1 | **Impact score** | `impact_score ≥ 0.60` agrégé sur la fenêtre 30 min |
| 2 | **Direction news cohérente** | Sentiment LLM signé et non nul (`abs(sentiment) ≥ 0.3`) |
| 3 | **Mouvement de prix** | Variation ≥ 0.4 % (forex), ≥ 1.0 % (equity), ≥ 1.5 % (crypto) dans les 15 min post-news |
| 4 | **Volume anormal** | Volume bougie ≥ 1.8 × médiane 20 j |
| 5 | **Ichimoku aligne** (règle d'or §2.5) | Score Ichimoku dans la direction du sentiment — sinon rejet |
| 6 | **Pas déjà tradé sur le même catalyseur** | `catalyst_fingerprint` absent des trades ouverts |
| 7 | **Hors avoid_window** | Pas dans la fenêtre ±45 min d'un FOMC/CPI/NFP (§11.2) |

#### Conditions de sortie

| Déclencheur | Description |
|---|---|
| **TP1** | 1.5 R atteint OU retour au niveau pré-news (prise de bénéfice rapide) |
| **TP2** | 2.5 R ou résistance technique Kijun/Kumo |
| **Stop** | Retour au niveau pré-news avec buffer 0.15 % |
| **Fade de la news** | Si une dépêche contradictoire arrive dans la fenêtre (`impact ≥ 0.5` direction inverse) |
| **Timeout** | Clôture au bout de 24 h si ni TP ni stop touché — la news est digérée |

#### Score de confiance

```
points = {
    impact_score_high:   3,   # Obligatoire (≥ 0.60)
    sentiment_clear:     2,   # Obligatoire (|sentiment| ≥ 0.3)
    price_moved_clearly: 2,   # Obligatoire
    volume_surge:        2,   # Obligatoire
    ichimoku_aligned:    2,   # Obligatoire (règle d'or)
    convergence_sources: 1,   # Bonus : au moins 2 sources distinctes
    tag_match:           1,   # Bonus : asset tag correspond au thème de la news (ai_narrative, oil_sensitive, …)
    onchain_confirms:    1,   # Bonus crypto : flux exchanges cohérent avec sentiment
}
# Seuil : 11/14 → confiance 79 %
```

#### Poids d'indicateurs spécifiques

```yaml
# config/strategies.yaml — news_driven_momentum
strategy_weights:
  ichimoku:  0.25
  trend:     0.20
  momentum:  0.25   # RSI + MACD — valider la pulsation post-news
  volume:    0.30   # OBV + CMF — confirmer que le flux suit la news
```

#### Pseudo-code Python

```python
# src/strategies/news_driven_momentum.py

class NewsDrivenMomentum:
    NAME = "news_driven_momentum"

    IMPACT_MIN         = 0.60
    IMPACT_ADHOC       = 0.80
    PRICE_MOVE = {"forex": 0.004, "equity": 0.010, "crypto": 0.015}

    def generate_signals(
        self, universe: list[str], context: CycleContext
    ) -> list[Signal]:
        pulse: NewsPulse = context.news_pulse
        if not pulse.impactful_assets:
            return []

        signals = []
        for hit in pulse.impactful_assets:        # list[NewsImpact]
            if hit.impact_score < self.IMPACT_MIN:
                continue
            asset = hit.asset
            if asset not in universe:
                continue

            df = context.ohlcv(asset, "1h", bars=80)
            if df is None or df.empty:
                continue

            # 1. Direction news
            direction = 1 if hit.sentiment > 0 else -1
            if abs(hit.sentiment) < 0.3:
                continue

            # 2. Mouvement de prix depuis la publication
            t_news = hit.published_at
            price_before = _price_at(df, t_news - pd.Timedelta(minutes=15))
            price_now    = df["close"].iloc[-1]
            if price_before is None:
                continue
            move_pct = (price_now - price_before) / price_before
            threshold = self.PRICE_MOVE[context.asset_class(asset)]
            if direction * move_pct < threshold:
                continue

            # 3. Volume anormal
            vol_med = df["volume"].rolling(20).median().iloc[-1]
            if df["volume"].iloc[-1] < 1.8 * vol_med:
                continue

            # 4. Ichimoku aligne (règle d'or §2.5)
            ichi = compute_ichimoku(df)
            if direction * ichi.score <= 0:
                continue

            # 5. Fingerprint anti-doublon
            fp = _catalyst_fingerprint(hit)
            if context.portfolio.has_open_trade_with(fp):
                continue

            # 6. Entry / Stop / TP
            entry = price_now
            stop  = price_before * (1 - direction * 0.0015)
            risk  = abs(entry - stop)
            tp1   = entry + direction * 1.5 * risk
            tp2   = entry + direction * 2.5 * risk
            rr    = abs(tp2 - entry) / risk
            if rr < 2.0:
                continue

            confiance = _compute_confidence_ndm(hit, df, ichi, pulse)
            if confiance < 0.79:
                continue

            signals.append(Signal(
                asset=asset, strategy=self.NAME,
                side="long" if direction == 1 else "short",
                entry=entry, stop=stop, tp=[tp1, tp2],
                confidence=confiance, timeframe="1h",
                catalysts=[hit.headline[:120]],
                catalyst_fingerprint=fp,
                indicators_used=["ichimoku", "macd", "obv", "cmf"],
            ))

            # 7. Déclenche un cycle ad-hoc si impact très élevé
            if hit.impact_score >= self.IMPACT_ADHOC:
                context.trigger_adhoc_cycle(reason=f"news_impact={hit.impact_score:.2f}",
                                            focus_asset=asset)
        return signals
```

#### Intégration avec le flux orchestrateur

Dans `Orchestrator.run_cycle` (§8.7), l'étape 3 actuelle (`news_agent.analyze`) est enrichie : elle produit désormais aussi une liste `impactful_assets` (cf. dataclass `NewsImpact` en §8.3). Ces actifs sont **injectés dans `candidates` en plus** du top 20 technique — même s'ils n'avaient pas été retenus par le scanner. Un cycle peut donc scanner 22–25 actifs si une news à fort impact arrive (budget tokens reste sous contrôle : la narrative technique n'est appelée que sur les scores forts, §2.3).

#### Circuit breaker individuel

```yaml
circuit_breaker:
  strategy: news_driven_momentum
  dd_7d_vs_median_threshold: 2.0
  min_history_days: 30
  cooldown_days: 7
  extra_conditions:
    max_trades_per_news_cluster: 1     # 1 seul trade par catalyst_fingerprint
    max_daily_trades: 3                # cap même si les news s'enchaînent
    max_concurrent_adhoc_cycles: 1     # évite une rafale de cycles sur la même heure
```

---

### 6.8 Interface commune — `Signal` et intégration dans l'orchestrateur

Toutes les stratégies partagent la même interface de sortie :

```python
# src/strategies/base.py

from dataclasses import dataclass, field
from abc import ABC, abstractmethod


@dataclass
class Signal:
    asset: str
    strategy: str
    side: str                        # "long" | "short"
    entry: float
    stop: float
    tp: list[float]
    confidence: float                # [0, 1]
    timeframe: str
    indicators_used: list[str]
    catalysts: list[str]  = field(default_factory=list)
    narrative: str        = ""       # Rempli par LLM si confidence >= 0.6

    @property
    def rr(self) -> float:
        return abs(self.tp[-1] - self.entry) / abs(self.stop - self.entry)

    @property
    def risk_distance(self) -> float:
        return abs(self.stop - self.entry)


class BaseStrategy(ABC):
    NAME: str

    @abstractmethod
    def generate_signals(
        self, universe: list[str], context: "CycleContext"
    ) -> list[Signal]: ...

    def is_regime_compatible(self, regime: "RegimeState") -> bool:
        return True  # Surchargé par chaque stratégie si nécessaire
```

**Activation dans `config/strategies.yaml`** :

```yaml
active_strategies:
  - name: ichimoku_trend_following
    enabled: true
    allocation_pct: 40      # % de l'équité fictive allouée à cette stratégie
    max_concurrent_trades: 3

  - name: breakout_momentum
    enabled: true
    allocation_pct: 25
    max_concurrent_trades: 2

  - name: mean_reversion
    enabled: true
    allocation_pct: 15
    max_concurrent_trades: 2

  - name: divergence_hunter
    enabled: true
    allocation_pct: 10
    max_concurrent_trades: 2

  - name: volume_profile_scalp
    enabled: true
    allocation_pct: 5
    max_concurrent_trades: 2

  - name: event_driven_macro
    enabled: true
    allocation_pct: 5
    max_concurrent_trades: 1

# Règle globale : max 3 stratégies actives simultanément
# La sélection quotidienne est faite par strategy_selector basé sur le régime
max_active_strategies: 3
```

---

### 6.9 Tableau récapitulatif

| Stratégie | Style | Marchés | Timeframe entrée | R/R min | Seuil confiance | Régimes favorables |
|---|---|---|---|---|---|---|
| **Ichimoku Trend Following** | Suivi tendance | Forex, Crypto, Actions | H4 (signal Daily) | 1:2 | 82% | risk_on fort, risk_off fort |
| **Breakout Momentum** | Breakout volatilité | Crypto, Actions | H4 | 1:2 | 80% | risk_on (prob >0.55) |
| **Mean Reversion** | Retour à la moyenne | Forex, Actions | H1 | 1:1.5 | 80% | Transition, risk_off modéré |
| **Divergence Hunter** | Divergences oscillateurs | Forex, Crypto, Actions | H4 | 1:2 | 70% | Tous (esp. Transition) |
| **Volume Profile Scalp** | Rebond niveaux VP | Crypto, Indices | H1 | 1:1.5 | 70% | Tous sauf vol. extrême |
| **Event-Driven Macro** | Post-annonce macro | Forex, Actions | H1 post-event | 1:2 | 77% | Tous (post-event, calendrier) |
| **News-Driven Momentum** | Post breaking news | Forex, Crypto, Actions | H1 post-news | 1:2 | 79% | Tous (opportuniste, hors-calendrier) |

**Règles de coexistence** :
- Max 3 stratégies actives simultanément (sélectionnées par `strategy_selector` selon le régime)
- `ichimoku_trend_following` est toujours prioritaire si le régime est directionnel
- `mean_reversion` et `breakout_momentum` ne sont **jamais actives simultanément** (directions opposées du marché)
- `event_driven_macro` et `news_driven_momentum` sont tous deux opportunistes : ils ne comptent pas dans le plafond de 3 stratégies actives et peuvent se superposer aux 3 stratégies de tendance sélectionnées par le `strategy_selector`.
- Une même dépêche ne doit pas produire à la fois un trade `event_driven_macro` et `news_driven_momentum` : le fingerprint de catalyseur est partagé entre les deux stratégies.

---

## 7. Sources de données & Data Quality Monitor

### 7.1 Sources primaires

Le fichier canonique est `config/sources.yaml`. Quatre catégories sont distinguées : prix OHLCV, news (RSS + APIs + Binance), macro (calendrier + indicateurs), on-chain. L'ordre d'énumération est l'ordre de priorité — le `DataFetcher` bascule sur le suivant si la source courante dépasse `on_error_threshold` ou timeout.

```yaml
# config/sources.yaml — vue synthétique (fichier complet versionné dans le repo)

prices:
  crypto:
    provider: ccxt
    exchanges: [binance, kraken, bybit]
    fallbacks: [kraken_rest, coingecko_api, coinmarketcap_api]
  forex:    { provider: oanda, fallback: exchangerate_host }
  equities:
    # Euronext Paris uniquement — hors scope V1 pour les marchés US (cf. §1.1).
    # Stack MVP 100 % gratuit, sans clé API (cf. §2.3 budget maîtrisé).
    # Exclus volontairement :
    #   - alpaca   (pas de couverture Euronext — broker US only)
    #   - yfinance (dépréciations fréquentes + rate limits Yahoo non documentés)
    #   - EODHD / Xignite / autres payants (différés en V2 si rentabilité prouvée)
    provider: stooq                                  # CSV gratuit, stable, EOD + intraday 5m
    fallbacks: [boursorama_scrape]                   # scrape pages cours broker FR pour intraday

equity_providers:
  stooq:
    base_url:           "https://stooq.com/q/d/l"    # téléchargement CSV direct
    api_key:            null                         # pas de clé
    ticker_suffix:      ".fr"                        # ex : rui.fr → Rubis
    endpoints:          [daily_csv, intraday_5m_csv]
    rate_limit_per_sec: 1                            # soft, politesse
    notes:              "primary — gratuit, EOD fiable, intraday 5m agrégeable en H1/H4/D1"
  boursorama_scrape:
    base_url:           "https://www.boursorama.com/cours"
    api_key:            null
    ticker_prefix:      "1rP"                        # ex : 1rPRUI pour Rubis
    endpoints:          [cours_page, historique_page]
    rate_limit_per_sec: 1
    notes:              "fallback — scrape intraday via pages publiques broker FR"

# Mapping ticker Euronext — fonction `resolve_ticker(symbol, provider)` dans
# src/data/ticker_map.py (seul point de conversion, testé unitairement).
#   Format canonique (config/assets.yaml) : <SYMBOL>.PA      ex: RUI.PA
#   stooq                                 : <symbol>.fr      ex: rui.fr
#   boursorama_scrape                     : 1rP<SYMBOL>      ex: 1rPRUI
# Toute nouvelle source equity doit fournir sa fonction de conversion ici —
# jamais d'ad-hoc ailleurs dans le code.

crypto_providers:
  coingecko_api:     { key: ${COINGECKO_DEMO_KEY}, endpoints: [ohlc, market_chart, markets, global, trending] }
  kraken_rest:       { endpoints: [OHLC, Ticker, Depth, Spread, AssetPairs], rate_limit_per_sec: 1 }
  coinmarketcap_api: { key: ${COINMARKETCAP_KEY}, endpoints: [global_metrics, listings, fear_greed, quotes] }

news:
  rss:
    - { url: "https://feeds.reuters.com/reuters/businessNews", weight: 1.0, tags: [macro, general] }
    - { url: "https://www.coindesk.com/arc/outboundfeeds/rss/", weight: 1.0, tags: [crypto] }
    - { url: "https://www.theblock.co/rss.xml",                 weight: 0.9, tags: [crypto] }
    - { url: "https://www.investing.com/rss/news_1.rss",        weight: 0.8, tags: [macro, equity] }
  apis:
    newsapi: ${NEWSAPI_KEY}        # 100 req/j — enrichissement
    finnhub: ${FINNHUB_KEY}        # 60 req/min — news tagguées par ticker
  binance:                         # endpoints publics, pas de clé
    announcements:    { poll: 60s,  catalogs: [48, 49, 161] }   # listing / delisting / notices
    funding_rate:     { poll: 300s, spike_abs: 0.01 }
    liquidations_24h: { poll: 900s, spike_pct: 0.30 }

macro:
  fred: ${FRED_API_KEY}
  tradingeconomics:
    api_key: ${TRADINGECONOMICS_KEY}       # 500 req/j
    endpoints: [calendar, news, indicators]
    tedata_rss: true                        # flux public sans clé, fallback
  forex_factory_calendar: scrape
  # APIs institutionnelles — pollées 1×/jour pour alimenter régime HMM (§12)
  world_bank:
    indicators: [gdp_growth, inflation_cpi, unemployment, gov_debt_gdp]
    countries:  [USA, EMU, CHN, JPN, GBR, DEU, FRA]
    refresh:    "daily 05:00 UTC"
  oecd:
    datasets:   [cli, business_confidence, consumer_confidence, unemployment_rate]
    refresh:    "daily 05:15 UTC"
  eurostat:
    datasets:   [hicp_inflation, industrial_prod, unemployment, economic_sentiment, retail_trade]
    refresh:    "daily 05:30 UTC"

onchain:
  glassnode_free: true
  dune_public:    true

fallback_policy:
  on_timeout_ms:      3000
  on_error_threshold: 0.10    # bascule si > 10 % erreur sur 1 h
  quota_soft_pct:     0.80    # degraded orange
  quota_hard_pct:     1.00    # cut-off rouge

daily_request_caps:
  newsapi:            90
  finnhub:            70000
  tradingeconomics:   450
  coingecko_api:      2500
  kraken_rest:        60000
  coinmarketcap_api:  300
  world_bank:         500
  oecd:               200
  eurostat:           200
  stooq:              2000       # scraping soft, CSV — primary equity
  boursorama_scrape:  5000       # scraping soft, pages cours — fallback equity
```

**Mapping source → consommateur** :

| Catégorie | Source | Consommé par | Rôle dans le bot |
|---|---|---|---|
| Prix | ccxt (binance/kraken/bybit) | `DataFetcher.fetch_ohlcv` | OHLCV crypto temps réel — primary |
| Prix | **kraken_rest** | `DataFetcher.fetch_ohlcv` + `LiquidityProbe` | Fallback ccxt + profondeur carnet (spread/depth) |
| Prix | **coingecko_api** | `DataFetcher.backfill`, `TechnicalAnalystAgent.dominance` | Backfill long terme + dominance BTC/ETH + trending |
| Prix | **coinmarketcap_api** | `RegimeDetector.sentiment`, `DashboardBuilder` | Fear & Greed, mcap totale — signal régime (pas OHLCV) |
| Prix | oanda / exchangerate_host | `DataFetcher.fetch_ohlcv` | OHLCV forex |
| Prix | **stooq** | `DataFetcher.fetch_ohlcv` | CSV gratuit Euronext `.fr` — primary (EOD + 5m) |
| Prix | **boursorama_scrape** | `DataFetcher.fetch_ohlcv` | Scrape pages cours broker FR — fallback intraday |
| News | RSS (Reuters, Coindesk, TheBlock, Investing) | `NewsAnalystAgent.scraper` | Flux principal news_driven_momentum (§6.7) |
| News | newsapi / finnhub | `NewsAnalystAgent.scraper` | Enrichissement + tags par ticker |
| News | Binance announcements | `NewsAnalystAgent.binance_watcher` | Catalyseurs crypto (listing/delisting) — score d'impact 0.9–1.0 |
| News | Binance funding_rate, L/S ratio | `NewsAnalystAgent.binance_signals` | Signaux dérivés (spike ≠ news mais alerte) |
| Macro | FRED | `EventWatcher` / régime HMM | Indicateurs macro US (taux, emploi) — quasi temps réel |
| Macro | Trading Economics API + tedata_rss | `EventWatcher.calendar`, `NewsAnalystAgent` | Calendrier + commentaires analystes |
| Macro | Forex Factory | `EventWatcher.calendar` (fallback) | Calendrier économique |
| Macro | **world_bank** | `RegimeDetector.long_term_context`, `DashboardBuilder` | GDP / inflation / chômage / dette annuels — input HMM longue vue |
| Macro | **oecd** | `RegimeDetector.leading_indicators` | CLI / BCI / CCI mensuels — anticipation bascule de régime |
| Macro | **eurostat** | `RegimeDetector.eu_context`, `EventWatcher.eu` | HICP / production / sentiment EA19 — volet européen |
| On-chain | Glassnode free, Dune public | `TechnicalAnalystAgent.onchain_confluence` | Confluence sur BTC/ETH |

**Fail-closed** — si toutes les sources d'une catégorie échouent (tous les fallbacks épuisés), `DataFetcher` lève `DataUnavailableError` et le cycle n'émet aucune proposition (cf. §2.2).

**Fréquence & coût** — les 3 APIs institutionnelles (World Bank / OECD / Eurostat) ne sont pas interrogées dans la boucle de cycle : elles sont *pull 1×/jour* à 05:00 UTC (cf. `refresh_cron` dans `config/sources.yaml`) et leurs séries sont cachées en Parquet. Elles n'ajoutent aucun coût tokens (données numériques pures, consommées par le HMM et non par le LLM).

### 7.2 Couche de scraping

```python
# src/data/fetcher.py

class DataFetcher:
    """
    Essaie les sources dans l'ordre de priorité config/sources.yaml.
    En cas d'échec ou de données périmées, passe à la source suivante.
    Lève DataUnavailableError si toutes les sources échouent → fail-closed.
    """

    def fetch_ohlcv(
        self,
        asset: str,
        timeframe: str,
        bars: int = 200,
        max_age_minutes: int = 60,
    ) -> pd.DataFrame:
        for source in self._priority_list("prix"):
            try:
                df = source.fetch(asset, timeframe, bars)
                if self.quality_monitor.is_fresh(df, max_age_minutes):
                    return df
            except Exception as e:
                log.warning(f"Source {source.name} failed for {asset}: {e}")
        raise DataUnavailableError(asset, timeframe)
```

#### 7.2.1 Spécifications de parsing par source

Chaque source a un adaptateur dédié dans `src/data/sources/<name>.py` qui produit un `pd.DataFrame` OHLCV normalisé avec colonnes figées `[open, high, low, close, volume]`, index `DatetimeIndex` UTC, `df.attrs["source"] = "<name>"`.

**Stooq CSV** (`src/data/sources/stooq.py`)

URL EOD daily : `https://stooq.com/q/d/l/?s={ticker_fr}&i=d` (ex: `rui.fr`).
URL intraday 5m : `https://stooq.com/q/d/l/?s={ticker_fr}&i=5`.

Format CSV retourné :

```
Date,Open,High,Low,Close,Volume
2026-04-17,28.50,28.72,28.41,28.65,142300
```

- Délimiteur : virgule. Encodage : UTF-8. Header présent.
- Parsing : `pd.read_csv(url, parse_dates=["Date"])`, puis rename en lowercase, set index sur `Date` localisé UTC (`tz_localize("Europe/Paris").tz_convert("UTC")` pour Euronext).
- Cas « aucune donnée » : Stooq renvoie littéralement le texte `"No data"` en HTTP 200 ; le parser détecte ce cas et lève `DataUnavailableError` (sans réessayer).
- Jours fériés / trading suspendu : lignes absentes → gap naturel, géré par `quality_monitor.has_gaps` (§7.3).
- Rate-limit : pas officiel mais soft-limit observé ~1 req/s — respecté par `rate_limit_per_sec: 1` (§7.1).

**Boursorama scrape** (`src/data/sources/boursorama.py`)

URL cours : `https://www.boursorama.com/cours/{code}/` (ex: `1rPRUI` pour Rubis).
URL historique : `https://www.boursorama.com/cours/{code}/historique/`.

Parsing HTML via `selectolax` (plus rapide que BeautifulSoup) — sélecteurs stables à ce jour :

```python
# src/data/sources/boursorama.py
SELECTORS = {
    "last_price":   "span.c-instrument.c-instrument--last",
    "history_rows": "table.c-table--generic tbody tr",
    "row_cells":    "td",   # ordre : date, open, high, low, close, volume, var
}
DATE_FMT = "%d/%m/%Y"       # français
```

- Décimal : virgule → convertie en point avant `float()`.
- Volume : suffixes `K`/`M` (milliers/millions) → multiplié avant cast.
- Détection de changement de layout : fonction `_validate_html_structure()` vérifie la présence des 7 colonnes attendues ; si la structure diffère, lève `DATA_SCRAPE_LAYOUT_CHANGED` (`error_code: DATA_007`) pour alerter l'opérateur.
- Fixture de test : `tests/fixtures/boursorama_rui_2026-04-17.html` capturée via `deploy/refresh_boursorama_fixture.sh`, rafraîchie mensuellement en CI.

**ccxt** (`src/data/sources/ccxt_source.py`)

Exchange de référence : `binance`. Fallbacks en ordre : `kraken`, `bybit` (déclarés §7.1).

Logique de matching de paire :

```python
# Paires demandées en format canonique "BTC/USDT". Si l'exchange ne supporte pas
# la quote USDT (Kraken peut exposer BTC/USD), fallback ordonné :
QUOTE_FALLBACK = ["USDT", "USD", "USDC", "EUR"]

def resolve_pair(exchange, canonical: str) -> str:
    base, quote = canonical.split("/")
    for q in [quote] + [x for x in QUOTE_FALLBACK if x != quote]:
        candidate = f"{base}/{q}"
        if candidate in exchange.markets:
            return candidate
    raise DataUnavailableError(canonical, "ccxt_no_pair_match")
```

- Timeframes : mapping `H1 → "1h"`, `H4 → "4h"`, `D1 → "1d"`, `M15 → "15m"`.
- Limite de bars : ccxt cap à 1000 bougies par appel — si `bars > 1000`, pagination par `since` timestamps.
- Rate-limit : respecté via `exchange.enableRateLimit = True` (ccxt natif).

**OANDA** (`src/data/sources/oanda.py`) — *optionnel, forex uniquement*

Endpoint v20 : `GET https://api-fxtrade.oanda.com/v3/instruments/{instrument}/candles?granularity={g}&count={n}`.
Granularités : mapping `H1 → "H1"`, `H4 → "H4"`, `D1 → "D"`, `M15 → "M15"`.

- Authentification : header `Authorization: Bearer {OANDA_API_KEY}` (§17.6).
- Format de réponse : JSON `{"candles": [{"time": "...", "mid": {"o","h","l","c"}, "volume": n, "complete": bool}, ...]}`.
- Filtrage : ne garder que `complete: true` (sinon bougie en cours → données partielles).
- Fallback si clé absente : `exchangerate_host` (§7.5.3 + §7.1) — précision EOD seulement.

**exchangerate_host** (`src/data/sources/exchangerate.py`) — *fallback forex gratuit*

Endpoint : `https://api.exchangerate.host/timeseries?start_date=...&end_date=...&base={b}&symbols={s}`.

- Pas de clé, pas de rate-limit documenté.
- Résolution EOD uniquement — si timeframe demandé < 1d, lève `DATA_008 timeframe_unsupported` immédiatement (pas de retry).

**Anthropic `messages.create`** (`src/llm/client.py`)

- SDK : `anthropic>=0.40`.
- Modèle (§2.3) : `claude-sonnet-4-6` (cycles auto) / `claude-opus-4-7` (self-improve, archi review).
- Prompt caching : structure en 5 couches §10.5 avec `cache_control: {"type": "ephemeral"}` aux 4 premières.
- Comptage tokens : utiliser `response.usage` (`input_tokens`, `output_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens`) — tracé dans `llm_usage` (§14.5.1).
- Retries : `anthropic.APIStatusError 429/529` → backoff exponentiel (2s, 4s, 8s max), 3 tentatives. Au-delà, `error_code: LLM_002` ou `LLM_003`.
- Format de réponse attendu (contract LLM → Python) : JSON strict dans un bloc ```json ... ``` dans `response.content[0].text`. Parsing via `pydantic.TypeAdapter(LLMInterpretation).validate_json(...)` — si échec, `error_code: LLM_005` + retry 1× avec prompt de correction.

**Fixtures de tests** — chaque source a une fixture capturée dans `tests/fixtures/<source>/` et un test `test_<source>_parser.py` qui valide le round-trip fixture → DataFrame normalisé.

### 7.3 Data Quality Monitor

```python
# src/data/quality_monitor.py

@dataclass
class QualityReport:
    asset: str
    is_fresh: bool
    has_gaps: bool
    has_outliers: bool
    source_used: str
    warnings: list[str]

class DataQualityMonitor:

    def check(self, df: pd.DataFrame, asset: str, max_age_minutes=60) -> QualityReport:
        warnings = []

        # 1. Fraîcheur
        age = (pd.Timestamp.utcnow() - df.index[-1]).total_seconds() / 60
        is_fresh = age <= max_age_minutes
        if not is_fresh:
            warnings.append(f"Données périmées : {age:.0f} min")

        # 2. Gaps
        expected_freq = pd.infer_freq(df.index)
        has_gaps = df.index.to_series().diff().max() > pd.Timedelta(expected_freq) * 3

        # 3. Outliers (variation >10% sur 1 bougie)
        returns = df["close"].pct_change().abs()
        has_outliers = (returns > 0.10).any()
        if has_outliers:
            warnings.append(f"Outlier détecté : variation > 10%")

        return QualityReport(asset, is_fresh, has_gaps, has_outliers,
                             df.attrs.get("source", "unknown"), warnings)

    def validate_or_raise(self, df: pd.DataFrame, asset: str) -> None:
        report = self.check(df, asset)
        if not report.is_fresh or report.has_outliers:
            raise DataQualityError(asset, report)
```

### 7.4 Cache Parquet

Les OHLCV sont mis en cache localement en Parquet, partitionnés par `YYYY-MM-DD/asset/timeframe.parquet`. TTL configurable par timeframe.

```python
# src/data/cache.py

class OHLCVCache:
    def get(self, asset: str, timeframe: str, date: str) -> pd.DataFrame | None: ...
    def put(self, df: pd.DataFrame, asset: str, timeframe: str, date: str) -> None: ...
    def is_valid(self, asset: str, timeframe: str, max_age_minutes: int) -> bool: ...
```

### 7.5 Logs d'erreurs actionnables & startup health checks

Implémentation de la règle d'or §2.6. Deux volets : **startup validation** (on refuse de démarrer si l'environnement est cassé) et **runtime logs structurés** (chaque incident est autosuffisant pour le debug).

#### 7.5.1 Schéma des logs structurés

Tout log passe par un logger unique qui émet du JSON line-delimited vers `data/logs/YYYY-MM-DD.log.jsonl`. Le schéma est fixe — toute contribution qui enrichit un log doit respecter ces champs.

```json
{
  "ts":          "2026-04-17T07:03:12.418Z",
  "level":       "ERROR",
  "component":   "DataFetcher",
  "event":       "source_unreachable",
  "error_code":  "NET_002",
  "cycle_id":    "c-7f1e-04a2",
  "asset":       "RUI.PA",
  "source":      "stooq",
  "cause":       "ConnectionError: HTTPSConnectionPool(host='stooq.com', port=443): Max retries exceeded",
  "remediation": "Vérifier la connectivité réseau (curl -I https://stooq.com). Fallback boursorama_scrape tenté.",
  "context":     { "timeout_ms": 3000, "retries": 3, "fallback_used": "boursorama_scrape" }
}
```

Règles :

- `cause` est toujours une string humaine — pas de stack trace brut ; la stack complète va dans `context.traceback` uniquement en `DEBUG`.
- `remediation` est **obligatoire** à partir du niveau `WARNING`. Un log qui n'en a pas fait échouer un test unitaire `tests/unit/test_logging_schema.py`.
- `error_code` renvoie à la taxonomie §7.5.2 — permet de grouper les incidents dans le dashboard (§14) et d'écrire des filtres `jq` stables.
- `cycle_id` est propagé via `contextvars` : `with logging_utils.cycle_scope(uuid): ...`.

#### 7.5.2 Taxonomie des codes d'erreur

Six familles, couvrant tout ce qui peut casser. Chaque code a un préfixe stable + numéro à 3 chiffres.

| Préfixe | Famille | Exemples |
|---|---|---|
| `CFG` | Configuration | `CFG_001` clé API manquante, `CFG_002` YAML invalide, `CFG_003` valeur hors bornes |
| `NET` | Réseau | `NET_001` timeout, `NET_002` connection refused, `NET_003` DNS fail, `NET_004` TLS error |
| `DATA` | Données | `DATA_001` données périmées, `DATA_002` gap OHLCV, `DATA_003` outlier > 10 %, `DATA_004` source KO → fallback, `DATA_005` tous fallbacks épuisés (fail-closed §2.2) |
| `LLM` | Anthropic API | `LLM_001` 401 auth, `LLM_002` 429 rate-limit, `LLM_003` 529 overloaded, `LLM_004` budget tokens dépassé (§11.4), `LLM_005` réponse JSON invalide |
| `RISK` | Risk management | `RISK_001` kill-switch armé, `RISK_002` circuit breaker trip stratégie, `RISK_003` max_daily_loss atteint, `RISK_004` proposition rejetée par risk-gate |
| `RUN` | Runtime / infra | `RUN_001` SQLite lock, `RUN_002` Parquet corrompu, `RUN_003` disque plein, `RUN_004` scheduler missed-fire |

Les codes sont définis dans `src/utils/error_codes.py` sous forme d'`Enum` avec docstring et `default_remediation` — le logger utilise le code pour pré-remplir `remediation` si l'appelant n'en fournit pas.

#### 7.5.3 Startup health checks (fail-fast)

Avant que le scheduler ne prenne la main, `src/utils/health_checks.py` exécute une batterie de vérifications. **Un seul échec bloque le démarrage** avec un exit code non-zéro et un log `CRITICAL` explicite.

```python
# src/utils/health_checks.py
@dataclass
class CheckResult:
    name: str
    passed: bool
    cause: str | None = None
    remediation: str | None = None

def run_startup_checks() -> list[CheckResult]:
    enabled_markets = _load_enabled_markets("config/assets.yaml")   # {"forex","equity","crypto"}

    checks = [
        # --- Core (toujours obligatoire) -----------------------------
        _check_env_var("ANTHROPIC_API_KEY", required=True),
        _check_env_var("TELEGRAM_BOT_TOKEN", required=True),
        _check_env_var("TELEGRAM_CHAT_ID",   required=True),
        _check_http_reachable("https://api.anthropic.com/v1/messages", timeout=5, expect=[401, 405]),
        _check_http_reachable("https://api.telegram.org",              timeout=5),
        # Filesystem
        _check_writable(DATA_DIR / "logs"),
        _check_writable(DATA_DIR / "cache"),
        _check_writable(DATA_DIR / "simulation"),
        _check_sqlite_openable(DATA_DIR / "memory.db"),
        # Config
        _check_yaml_loadable("config/risk.yaml"),
        _check_yaml_loadable("config/strategies.yaml"),
        _check_yaml_loadable("config/assets.yaml"),
        _check_yaml_loadable("config/sources.yaml"),
        _check_yaml_loadable("config/schedules.yaml"),
    ]

    # --- Conditionnel : selon marchés activés (cf. §1.1) -------------
    if "equity" in enabled_markets:
        # Providers gratuits, pas de clé — on valide juste la joignabilité.
        checks += [
            _check_http_reachable("https://stooq.com",          timeout=5),
            _check_http_reachable("https://www.boursorama.com", timeout=5),
        ]
    if "forex" in enabled_markets:
        # OANDA payant → optionnel. Si absent, DataFetcher bascule sur
        # exchangerate_host (gratuit, EOD seulement, cf. §7.1 fallback).
        oanda_key = _check_env_var("OANDA_API_KEY", required=False)
        checks.append(oanda_key)
        if not oanda_key.passed:
            checks.append(_check_http_reachable("https://api.exchangerate.host", timeout=5))
    if "crypto" in enabled_markets:
        checks.append(_check_http_reachable("https://api.binance.com/api/v3/ping", timeout=5))

    return checks

def main():
    results = run_startup_checks()
    failures = [r for r in results if not r.passed]
    for r in results:
        (log.info if r.passed else log.critical)(
            "startup_check", name=r.name, passed=r.passed,
            cause=r.cause, remediation=r.remediation,
        )
    if failures:
        log.critical("startup_aborted", error_code="CFG_001",
                     cause=f"{len(failures)} check(s) KO",
                     remediation="Corriger les erreurs CRITICAL ci-dessus puis relancer.")
        sys.exit(78)  # EX_CONFIG
```

#### 7.5.4 Exemples de logs — bonne VS mauvaise pratique

**Cas 1 — clé API Anthropic manquante**

```jsonc
// ❌ Mauvais (inactionnable)
{"level":"ERROR","msg":"auth failed"}

// ✅ Bon (schéma §7.5.1)
{
  "ts":"2026-04-17T06:59:58Z","level":"CRITICAL","component":"HealthCheck",
  "event":"env_var_missing","error_code":"CFG_001",
  "cause":"Variable ANTHROPIC_API_KEY absente de l'environnement (.env non chargé ou clé vide).",
  "remediation":"Ajouter ANTHROPIC_API_KEY=sk-ant-... dans .env puis relancer. Voir §17.6.",
  "context":{"env_file_found":true,"env_file_path":"/app/.env","keys_detected":["TELEGRAM_BOT_TOKEN","TELEGRAM_CHAT_ID"]}
}
```

**Cas 2 — service data KO, fallback réussi**

```jsonc
{
  "ts":"2026-04-17T07:03:12Z","level":"WARNING","component":"DataFetcher",
  "event":"source_failed_fallback_ok","error_code":"DATA_004",
  "cycle_id":"c-7f1e-04a2","asset":"RUI.PA","source":"stooq",
  "cause":"HTTP 503 sur https://stooq.com/q/d/l?s=rui.fr (retries 3/3 épuisés).",
  "remediation":"Aucune action requise — bascule automatique sur boursorama_scrape. Si erreur persiste > 1 h, inspecter data/logs/ pour taux d'erreur stooq.",
  "context":{"http_status":503,"retries":3,"latency_ms":6100,"fallback_used":"boursorama_scrape","fallback_ok":true}
}
```

**Cas 3 — tous les fallbacks épuisés (fail-closed §2.2)**

```jsonc
{
  "ts":"2026-04-17T07:04:47Z","level":"ERROR","component":"DataFetcher",
  "event":"all_sources_exhausted","error_code":"DATA_005",
  "cycle_id":"c-7f1e-04a2","asset":"RUI.PA",
  "cause":"Toutes les sources equity ont échoué : stooq (503), boursorama_scrape (ConnectionError).",
  "remediation":"1) curl -I https://stooq.com et https://www.boursorama.com pour identifier l'indisponibilité. 2) Le cycle n'émettra aucune proposition pour RUI.PA (fail-closed §2.2). 3) Si > 30 min, vérifier le quota daily_request_caps dans config/sources.yaml.",
  "context":{"sources_tried":["stooq","boursorama_scrape"],"cycle_will_skip_asset":true}
}
```

**Cas 4 — rate-limit LLM Anthropic**

```jsonc
{
  "ts":"2026-04-17T07:05:03Z","level":"ERROR","component":"AnthropicClient",
  "event":"rate_limit_hit","error_code":"LLM_002",
  "cycle_id":"c-7f1e-04a2","agent":"NewsAnalystAgent",
  "cause":"HTTP 429 — rate_limit_error (60 req/min dépassé pour l'organisation).",
  "remediation":"Attendre 60 s (retry automatique avec backoff exponentiel). Si récurrent, réduire la fréquence des cycles crypto_6h dans config/schedules.yaml ou augmenter le plan Anthropic.",
  "context":{"retry_after_s":47,"tokens_today":34210,"budget_today":50000,"attempt":1,"max_attempts":3}
}
```

#### 7.5.5 Intégration Telegram & dashboard

- Les logs `ERROR` et `CRITICAL` déclenchent une notification Telegram (`NotificationAgent.send_alert`, §14.6) avec le champ `remediation` en tête du message — l'opérateur sait quoi faire sans ouvrir les logs.
- Le tableau « sources de données » du panel Coûts (§14.3.4) et la commande Telegram `/apis` (§14.6) agrègent les logs par `error_code` sur 24 h ; le clic sur une ligne du dashboard ouvre les 10 dernières occurrences avec la `remediation` pré-affichée.
- Un check CI `tests/unit/test_logging_schema.py` parse les fixtures de logs dans `tests/fixtures/logs/` et valide : (1) JSON parseable, (2) tous les champs obligatoires présents, (3) `remediation` non vide pour `WARNING+`, (4) aucun secret détecté par regex (`sk-ant-`, `bot[0-9]+:AA`, `EAA[A-Za-z0-9]+`).

---

## 8. Agents spécialisés & Skills Cowork

### 8.1 Architecture globale des agents

```
                     ┌─────────────────────┐
                     │    ORCHESTRATEUR    │
                     │  (orchestrator.py)  │
                     └─────────┬───────────┘
                               │ JSON
          ┌────────────────────┼────────────────────┐
          │                    │                    │
          ▼                    ▼                    ▼
  ┌───────────────┐   ┌────────────────┐   ┌──────────────┐
  │ TechnicalAna  │   │  NewsAnalyst   │   │  RiskManager │
  │ lystAgent     │   │  (LLM)         │   │  (Python pur)│
  └───────┬───────┘   └───────┬────────┘   └──────┬───────┘
          │ CompositeScore     │ NewsSummary        │ RiskDecision
          └────────────────────┴────────────────────┘
                               │
                    ┌──────────▼──────────┐
                    │   SimulatorAgent    │
                    │  (journal + Excel)  │
                    └──────────┬──────────┘
                               │ TradeSimulated
                    ┌──────────▼──────────┐
                    │   ReporterAgent     │
                    │ (dashboard+Telegram)│
                    └─────────────────────┘
```

### 8.2 TechnicalAnalystAgent

**Responsabilité unique** : calcul des indicateurs, score composite, interprétation narrative via LLM.

```python
# src/agents/technical_analyst.py

class TechnicalAnalystAgent:

    def analyze(self, asset: str, regime: RegimeState) -> TechnicalAnalysis:
        # 1. Récupération données (déterministe)
        df = self.fetcher.fetch_ohlcv(asset, timeframe="1d", bars=200)
        self.quality_monitor.validate_or_raise(df, asset)

        # 2. Calcul indicateurs (déterministe)
        ichimoku  = compute_ichimoku(df)
        trend     = TrendScore.compute(df, self.config.trend_weights)
        momentum  = MomentumScore.compute(df, self.config.momentum_weights)
        volume    = VolumeScore.compute(df, self.config.volume_weights)
        composite = compute_composite_score(df, self.config)

        # 3. Interprétation narrative (LLM, 1 appel, max 400 tokens)
        if abs(composite.value) >= 0.6:  # uniquement si signal fort
            narrative = self.llm_interpret_context(composite, regime,
                                                    asset=asset, news="")
        else:
            narrative = LLMInterpretation(text="Signal insuffisant.", confidence=0.0)

        return TechnicalAnalysis(
            asset=asset,
            composite=composite,
            ichimoku=ichimoku,
            trend=trend,
            momentum=momentum,
            volume=volume,
            narrative=narrative,
        )
```

### 8.3 NewsAnalystAgent

**Responsabilité unique** : scraping news (RSS + APIs + Binance announcements + Trading Economics news), résumé LLM, score sentiment, détection catalyseurs, **calcul de l'impact score** par actif (consommé par la stratégie `news_driven_momentum` §6.7).

#### Dataclasses partagées

```python
# src/agents/news_types.py

from dataclasses import dataclass, field
from datetime import datetime

@dataclass
class Article:
    url: str
    headline: str
    body: str
    source: str                  # "reuters_rss", "binance_announcement", "tradingeconomics_api", ...
    source_weight: float         # 0.0–1.0, repris depuis config/sources.yaml
    published_at: datetime
    tags: list[str]              # ["crypto", "listing", "ai_narrative", ...]

@dataclass
class Catalyst:
    """Catalyseur planifié détecté par pattern matching (FOMC, CPI, earnings, ...)."""
    kind: str                    # "FOMC" | "CPI" | "earnings" | "listing" | "sanction" | ...
    asset: str | None            # ticker affecté ou None si macro global
    scheduled_at: datetime | None
    fingerprint: str             # hash stable pour déduplication inter-stratégies

@dataclass
class NewsImpact:
    """Impact news non planifié — consommé par news_driven_momentum (§6.7)."""
    asset: str
    headline: str
    sentiment: float             # [-1, +1], signé
    impact_score: float          # [0, 1], cf. formule §6.7
    published_at: datetime
    sources: list[str]           # sources concordantes (convergence bonus)
    tags: list[str]
    catalyst_fingerprint: str    # identique entre event_driven_macro & news_driven_momentum

@dataclass
class NewsSummary:
    text: str
    sentiment: dict[str, float]  # asset → [-1, +1]

@dataclass
class NewsPulse:
    articles: list[Article]
    summary: NewsSummary
    catalysts: list[Catalyst]            # événements planifiés (calendrier)
    impactful_assets: list[NewsImpact]   # news non planifiées — ajouté V1.1
    fetched_at: datetime = field(default_factory=datetime.utcnow)
```

#### Agent

```python
# src/agents/news_analyst.py

class NewsAnalystAgent:

    def analyze(self, assets: list[str], horizon_hours: int = 72) -> NewsPulse:
        # 1. Scraping multi-sources (déterministe, parallèle)
        raw_articles = self.scraper.fetch_recent(assets, hours=horizon_hours)
        # inclut : RSS config/sources.yaml, newsapi, finnhub, binance.announcements,
        #         tradingeconomics.news, tedata_rss

        # 2. Filtrage par ticker/entité (NER simple + tags config/assets.yaml)
        relevant = self.ner_filter.filter(raw_articles, assets)

        # 3. Résumé LLM (1 appel groupé, max 800 tokens)
        if relevant:
            summary = self._llm_summarize(relevant, assets)
        else:
            summary = NewsSummary(text="Aucune news significative.", sentiment={})

        # 4. Catalyseurs planifiés (déterministe — pattern matching)
        catalysts = self.catalyst_detector.detect(relevant)

        # 5. Impact scoring pour les news non planifiées (cf. formule §6.7)
        impactful = self.impact_scorer.score(relevant, summary, assets)

        return NewsPulse(
            articles=relevant,
            summary=summary,
            catalysts=catalysts,
            impactful_assets=impactful,
        )


    def _llm_summarize(self, articles: list[Article], assets: list[str]) -> NewsSummary:
        """
        Prompt structuré : retourne JSON avec sentiment par asset [-1,+1]
        et liste de catalyseurs détectés.
        """
        ...
```

#### News Watcher (poll 60s, hors cycle)

Un composant léger `NewsWatcher` (cf. §15.1 `news_watcher`) tourne en tâche de fond et appelle `NewsAnalystAgent.impact_scorer` sur les dépêches les plus récentes. Lorsqu'un `NewsImpact.impact_score ≥ 0.80`, il déclenche un **cycle ad-hoc** via `Orchestrator.run_focused_cycle(focus_asset=...)`. Pour `0.60 ≤ score < 0.80`, le `NewsImpact` est simplement poussé dans le prochain `NewsPulse` du cycle régulier.

### 8.4 RiskManagerAgent

**Responsabilité unique** : validation déterministe de chaque proposition. Aucun appel LLM.

```python
# src/agents/risk_manager.py

class RiskManagerAgent:

    def evaluate(
        self,
        proposal: TradeProposal,
        portfolio: PortfolioState,
        regime: RegimeState,
    ) -> RiskDecision:

        checks = [
            self._check_kill_switch(),
            self._check_ichimoku_in_kumo(proposal),
            self._check_exposure_limits(proposal, portfolio),
            self._check_rr_minimum(proposal),
            self._check_correlation(proposal, portfolio),
            self._check_avoid_windows(proposal),
            self._check_circuit_breaker(proposal.strategy),
            self._check_token_budget(),
        ]

        failures = [c for c in checks if not c.passed]
        if failures:
            return RiskDecision.reject(
                proposal_id=proposal.id,
                reasons=[f.reason for f in failures],
                checks=checks,            # _pad_checks() complète à 10 si short-circuit
            )

        size = self._compute_position_size(proposal, portfolio)
        return RiskDecision.approve(
            proposal_id=proposal.id,
            checks=checks,
            adjusted_size_pct=size,
        )

    def _check_ichimoku_in_kumo(self, proposal: TradeProposal) -> Check:
        """Bloque si score Ichimoku == 0 (prix dans le Kumo)."""
        ...

    def _compute_position_size(
        self, proposal: TradeProposal, portfolio: PortfolioState
    ) -> float:
        """
        Kelly fractionnel + limites dures risk.yaml.
        Taille = min(kelly_frac, max_risk_per_trade_pct_equity).
        """
        ...
```

### 8.5 SimulatorAgent

**Responsabilité unique** : enregistrement des trades simulés, mise à jour P&L, Excel.

```python
# src/agents/simulator.py

class SimulatorAgent:

    def record_proposal(self, proposal: TradeProposal, validated: bool) -> SimulatedTrade:
        """Enregistre la proposition (validée ou rejetée) dans SQLite et Excel."""
        trade = SimulatedTrade.from_proposal(proposal, validated_by_human=validated)
        self.db.insert_trade(trade)
        self.excel_writer.append_trade(trade)
        return trade

    def update_open_trades(self, prices: dict[str, float]) -> list[SimulatedTrade]:
        """Met à jour le P&L fictif de tous les trades ouverts aux prix courants."""
        open_trades = self.db.get_open_trades()
        closed = []
        for trade in open_trades:
            current_price = prices.get(trade.asset)
            if current_price is None:
                continue
            trade.update_pnl(current_price)
            if trade.should_close(current_price):  # TP ou stop touché
                trade.close(current_price)
                closed.append(trade)
        self.db.update_trades(open_trades)
        self.excel_writer.refresh_summary()
        return closed
```

### 8.6 ReporterAgent

**Responsabilité unique** : génération dashboard HTML, envoi notifications Telegram.

```python
# src/agents/reporter.py

class ReporterAgent:

    def generate_daily_report(
        self,
        analyses: list[TechnicalAnalysis],
        proposals: list[TradeProposal],
        portfolio: PortfolioState,
        regime: RegimeState,
    ) -> DailyReport:
        # 1. Dashboard HTML
        html_path = self.html_builder.build(analyses, proposals, portfolio, regime)

        # 2. Résumé Telegram (≤ 800 chars)
        telegram_msg = self._build_telegram_summary(proposals, portfolio, regime)
        self.telegram.send(telegram_msg)

        # 3. Notification de validation (boutons inline Telegram)
        for proposal in proposals:
            self.telegram.send_proposal_card(proposal)

        return DailyReport(html_path=html_path, proposals_sent=len(proposals))
```

### 8.7 Orchestrateur

#### 8.7.1 Politique de résilience (timeouts, retry, fallback)

L'orchestrateur ne doit **jamais** crasher un cycle complet à cause d'une panne partielle d'un agent. Chaque étape est isolée dans un try/except avec politique explicite :

| Étape | Timeout | Retry | Comportement si KO | Cycle continue ? |
|---|---|---|---|---|
| Kill-switch check | 100 ms | 0 | log.critical, abort cycle | Non (fail-closed) |
| `regime_detector.detect()` | 10 s | 2 (backoff 1/3/9s) | Fallback `last_regime.json` (§12.2.4) | **Oui** (en mode dégradé) |
| `market_scanner.scan()` | 60 s | 1 (backoff 5s) | Univers réduit aux positions ouvertes uniquement | Oui |
| `news_agent.analyze()` | 45 s | 1 (backoff 3s) | `news_pulse = NewsPulse.empty()` (pas d'injection, `impactful_assets=[]`) | Oui |
| `signal_crossing.score()` | 5 s/asset | 1 | Skip l'asset (loggé `DATA_SIGNAL_SKIP`) | Oui (l'asset seulement) |
| `strategy_selector.pick()` | 2 s | 0 | Fallback `active_strategies = ["ichimoku_trend_following"]` (stratégie par défaut conservative) | Oui |
| `build_proposal()` (par stratégie) | 3 s | 0 | Skip la stratégie pour cet asset | Oui |
| `risk_manager.evaluate()` | 500 ms | 0 | **fail-closed** : proposal rejetée via `RiskDecision.reject(proposal_id, reasons=["gate_error"], checks=[])` — `_pad_checks()` complète les 10 avec `evaluated=False` | Oui |
| `simulator.record_proposal()` | 2 s | 2 | Retry synchrone puis queue offline (`data/queue/pending_records.jsonl`) | Oui |
| `reporter.generate_daily_report()` | 30 s | 1 | Rapport minimal text-only + log.error | Oui |
| `db.save_cycle_observations()` | 5 s | 2 | Queue offline `data/queue/pending_observations.jsonl` | Oui |

**Score d'injection news (§8.7.2 et §6.7 clarification)** :
- Seuil dur : `impact_score >= 0.60` pour être candidat
- Rang : tri desc par `impact_score`, cap à **3 injections max** par cycle (éviter dilution du scan)
- Dé-doublonnage : si l'actif est déjà dans `candidates`, on **upgrade son score** au max(`score_scan`, `impact_score * 0.8`) et on marque `forced_by="news_pulse"` (cf. §8.8.1 contrat `Candidate`)
- Si `news_pulse == NewsPulse.empty()` (agent KO) : aucune injection, on log `log.warning("news_injection_skipped", reason="news_agent_degraded")`

#### 8.7.2 État du cycle et observabilité

Chaque `CycleResult` contient un champ `degradation_flags: list[str]` (ex: `["regime_stale", "news_agent_down"]`) exposé dans le dashboard (§14.3.4 panneau APIs) et dans le message Telegram de fin de cycle.

Si `len(degradation_flags) >= 3` OU `risk_gate_failure_rate > 50%` → **circuit breaker** déclenché (cf. §11.1 C5), cycle suivant reporté de 1h.

#### 8.7.3 Code de référence

```python
# src/orchestrator/run.py
#
# Module d'entrée canonique (lancé par le Dockerfile :
#   CMD ["python", "-m", "src.orchestrator.run", "--serve", "--port", "8080"]).
# Les "agents" spécialisés (news, tech, risk) vivent dans leurs sous-packages
# respectifs (src/news/, src/signals/, src/risk/) — l'Orchestrator ne fait
# que les séquencer.

import importlib

class Orchestrator:
    """Séquence les agents pour un cycle complet d'analyse."""

    def run_cycle(self, session_name: str) -> CycleResult:
        # 0. Kill-switch
        if kill_switch.is_active():
            log.critical("Kill-switch actif — cycle annulé")
            return CycleResult.aborted("kill_switch")

        # 1. Régime de marché (déterministe, HMM)
        regime = self.regime_detector.detect()
        self.db.save_regime_snapshot(regime)

        # 2. Scan univers — score rapide
        candidates = self.market_scanner.scan(regime, top_n=20)

        # 3. News (LLM) — produit aussi NewsPulse.impactful_assets (§8.3)
        news_pulse = self.news_agent.analyze([c.asset for c in candidates])

        # 3b. Injection des actifs à fort impact news (§6.7)
        for hit in news_pulse.impactful_assets:
            if hit.impact_score >= 0.60 and hit.asset not in [c.asset for c in candidates]:
                candidates.append(Candidate(asset=hit.asset, score=0.0, forced_by="news"))

        # 4a. Signal-crossing (100 % Python, déterministe) — diagnostic scoré.
        # Attention : SignalOutput N'EST PAS un TradeProposal (cf. §8.9).
        signals = [self.signal_crossing.score(c.asset, regime) for c in candidates]
        strong  = [s for s in signals if abs(s.score) >= 0.6 and s.confidence >= 0.4]

        # 4b. Strategy-selector — choisit 1 à 3 stratégies actives pour ce cycle.
        selection = self.strategy_selector.pick(regime, strong)

        # 4c. build_proposal par stratégie (déterministe, 0 token) — SEUL endroit
        # où un signal devient un TradeProposal (§8.9, règle d'or §2.1).
        proposals: list[TradeProposal] = []
        for signal in strong:
            eligible = set(signal.applicable_strategies) & set(selection.active_strategies)
            for sid in eligible:
                strategy_mod = importlib.import_module(f"src.strategies.{sid}")
                market_data  = self.data_fetcher.snapshot(signal.asset)
                proposal     = strategy_mod.build_proposal(
                    signal, market_data, regime,
                    config=self.strategies_cfg[sid],
                    news_pulse=news_pulse,
                )
                if proposal is not None:
                    proposals.append(proposal)

        # 5. Risk gate (déterministe) — 10 contrôles §11, dont Ichimoku alignment.
        portfolio = self.db.get_portfolio_state()
        approved  = []
        for proposal in proposals:
            decision = self.risk_manager.evaluate(proposal, portfolio, regime)
            if decision.approved:
                approved.append(proposal.with_size(decision.adjusted_size_pct))

        # 6. Simulation + Excel
        for proposal in approved:
            self.simulator.record_proposal(proposal, validated=False)

        # 7. Dashboard + Telegram (avec boutons de validation)
        report = self.reporter.generate_daily_report(analyses, approved, portfolio, regime)

        # 8. Mémoire
        self.db.save_cycle_observations(session_name, analyses, approved)
        self.memory_exporter.refresh_markdown()

        return CycleResult.success(proposals=len(approved), report=report)


    # ------------------------------------------------------------------
    # Cycle ad-hoc déclenché par le NewsWatcher (§15.1 news_watcher)
    # ------------------------------------------------------------------
    def run_focused_cycle(
        self,
        focus_asset: str,
        trigger: NewsImpact,
        correlated_assets: list[str] | None = None,
    ) -> CycleResult:
        """
        Cycle ciblé : skip le market scan, utilise directement l'actif focus
        (+ actifs corrélés) et la stratégie news_driven_momentum en priorité.
        Respecte le cooldown et le max_adhoc_per_day définis en §15.1.
        """
        if kill_switch.is_active():
            return CycleResult.aborted("kill_switch")
        if not self._adhoc_allowed(trigger):
            return CycleResult.aborted("adhoc_cooldown_or_cap")

        regime     = self.regime_detector.detect()
        universe   = [focus_asset, *(correlated_assets or [])]
        news_pulse = self.news_agent.analyze(universe)        # re-score impact
        signals    = [self.signal_crossing.score(a, regime) for a in universe]
        portfolio  = self.db.get_portfolio_state()

        # Cycle focused : on privilégie news_driven_momentum (§6.7) mais on
        # passe quand même par build_proposal pour rester déterministe.
        proposals = []
        for signal in signals:
            for sid in signal.applicable_strategies:
                strategy_mod = importlib.import_module(f"src.strategies.{sid}")
                market_data  = self.data_fetcher.snapshot(signal.asset)
                proposal     = strategy_mod.build_proposal(
                    signal, market_data, regime,
                    config=self.strategies_cfg[sid],
                    news_pulse=news_pulse,
                )
                if proposal is not None:
                    proposals.append(proposal)

        approved = []
        for proposal in proposals:
            decision = self.risk_manager.evaluate(proposal, portfolio, regime)
            if decision.approved:
                approved.append(proposal.with_size(decision.adjusted_size_pct))

        for p in approved:
            self.simulator.record_proposal(p, validated=False)

        self.reporter.generate_daily_report(analyses, approved, portfolio, regime)
        self.db.log_adhoc_cycle(trigger, approved)
        return CycleResult.success(proposals=len(approved), kind="adhoc")
```

### 8.8 Skills Cowork

Chaque skill est un dossier `skills/<name>/SKILL.md` + scripts Python appelables depuis l'agent OpenClaw.

> **Note de périmètre** : seuls les 7 skills listés ci-dessous sont **des vrais skills Cowork**. Les modules purement déterministes (`signal-crossing`, `build_proposal`) vivent en `src/` et sont appelés directement par l'orchestrateur — pas via la machinerie de skills. Cette distinction est importante car elle détermine où logge le budget tokens (skill = potentiellement LLM, module = jamais LLM).

| Skill | Déclencheur | Responsabilité | Sortie |
|---|---|---|---|
| `market-scan` | Début de cycle | Scan déterministe de l'univers, shortlist top candidats | `list[Candidate]` |
| `strategy-selector` | Après market-scan | Sélection 1-3 stratégies selon régime HMM (§12) | `SelectionOutput` |
| `news-pulse` | Par candidat + triggers | Agrégation RSS + sentiment LLM (sonnet-4-6) | `NewsPulse` |
| `risk-gate` | Avant chaque proposition | **10 contrôles** déterministes §11 (Ichimoku alignment inclus), pas de bypass | `RiskDecision` |
| `dashboard-builder` | Fin de cycle | Génération HTML + summary.md Telegram | HTML + md |
| `memory-consolidate` | Nuit 02:00 UTC | Fusion/archivage SQLite + regénération MEMORY.md | MEMORY.md |
| `backtest-quick` | Self-improve / ad-hoc | Walk-forward + Monte-Carlo sur historique | `BacktestReport` |
| `self-improve` | Dimanche 22:00 UTC | Analyse perfs + patches (opus-4-7) → PR locale | PR locale |

**Modules internes (pas des skills)** — appelés directement par `src/orchestrator/run.py` :

| Module | Localisation | Responsabilité | Sortie |
|---|---|---|---|
| `signal_crossing.score()` | `src/signals/signal_crossing.py` | Fusion multi-signaux (Ichimoku + indicateurs §6), 100 % Python déterministe | `SignalOutput` (**diagnostic**, PAS une proposition) |
| `<strategy_id>.build_proposal()` | `src/strategies/<id>.py` | Pricing déterministe → construction TradeProposal (§8.9) | `TradeProposal \| None` |

> **Important** : `build_proposal` n'est **pas** un skill Cowork — c'est un
> module Python pur (`src/strategies/<id>.py`). L'absence de skill à cet
> endroit est volontaire : la transformation signal → proposition (entry,
> stop, tp, rr, sizing) est 100 % déterministe et doit rester auditable
> ligne par ligne dans Git, sans couche LLM. Cf. §8.9 pour le contrat.

#### 8.8.1 Contrats Pydantic inter-skills (schémas figés)

Tous les échanges entre skills passent par ces dataclasses Pydantic, versionnées dans `src/contracts/` et validées aux deux bouts. Un skill qui émet un objet non-conforme échoue en `STRICT` mode côté consommateur.

```python
# src/contracts/skills.py
from pydantic import BaseModel, Field, conlist, confloat, conint
from typing import Literal, Optional

# ---------- market-scan ---------------------------------------------
class Candidate(BaseModel):
    asset:         str                                    # "RUI.PA", "EURUSD", "BTC/USDT"
    asset_class:   Literal["equity", "forex", "crypto"]
    score_scan:    confloat(ge=-1.0, le=1.0)              # score rapide déterministe (§8.8.2)
    liquidity_ok:  bool                                   # spread + depth > seuils §7.3
    forced_by:     Optional[Literal["news_pulse", "telegram_cmd", "correlated_to"]] = None
    correlated_to: Optional[str] = None                   # rempli si forced_by == "correlated_to"

# ---------- strategy-selector ---------------------------------------
class StrategyChoice(BaseModel):
    strategy_id:  str                                     # clé dans strategies.yaml
    weight:       confloat(ge=0.0, le=1.0)                # pondération pour ce candidat
    reason:       str                                     # "regime=risk_on + trend_up"

class SelectionOutput(BaseModel):
    asset:        str
    strategies:   conlist(StrategyChoice, min_length=1, max_length=3)

# ---------- signal-crossing -----------------------------------------
class IchimokuPayload(BaseModel):
    price_above_kumo:        bool
    tenkan_above_kijun:      bool
    chikou_above_price_26:   bool
    kumo_thickness_pct:      confloat(ge=0.0)
    aligned_long:            bool
    aligned_short:           bool
    distance_to_kumo_pct:    float                        # signé : positif = au-dessus

class IndicatorScore(BaseModel):
    name:       str                                       # "supertrend", "rsi_14", "macd"...
    score:      confloat(ge=-1.0, le=1.0)
    confidence: confloat(ge=0.0, le=1.0)

class SignalOutput(BaseModel):
    asset:             str
    timestamp:         str                                # ISO8601 UTC
    composite_score:   confloat(ge=-1.0, le=1.0)          # §5.5
    confidence:        confloat(ge=0.0, le=1.0)
    regime_context:    Literal["risk_on", "transition", "risk_off"]
    ichimoku:          IchimokuPayload                    # recopié tel quel par build_proposal
    trend:             list[IndicatorScore]               # Supertrend, MACD, PSAR, Aroon, ADX, BB
    momentum:          list[IndicatorScore]               # RSI, Stoch, TRIX, CCI, Momentum
    volume:            list[IndicatorScore]               # OBV, VWAP, CMF, VolumeProfile
    is_proposal:       Literal[False] = False             # diagnostic, pas une proposition

# ---------- news-pulse ----------------------------------------------
class NewsItem(BaseModel):
    source:     str                                       # "reuters_rss" | "finnhub" | ...
    title:      str
    url:        str
    published:  str                                       # ISO8601 UTC
    impact:     confloat(ge=0.0, le=1.0)                  # §6.7
    sentiment:  confloat(ge=-1.0, le=1.0)
    entities:   list[str]                                 # tickers/orgs extraits (NER)

class NewsPulse(BaseModel):
    asset:        str
    window_hours: conint(ge=1, le=72) = 24
    items:        list[NewsItem]                          # triés impact desc
    top:          Optional[NewsItem] = None               # items[0] si présent
    aggregate_impact:    confloat(ge=0.0, le=1.0)         # max des items pondérés
    aggregate_sentiment: confloat(ge=-1.0, le=1.0)        # moyenne pondérée

# ---------- build_proposal (modules de stratégie) -------------------
class MarketSnapshot(BaseModel):
    """Snapshot complet par asset à un instant donné. Produit par src/data/fetcher.py."""
    asset:         str
    asset_class:   Literal["equity", "forex", "crypto"]
    ts:            str                                    # ISO-8601 UTC Z
    ohlcv:         list[tuple[str, float, float, float, float, float]]  # (ts, o, h, l, c, v), N barres
    timeframe:     Literal["1h", "4h", "1d"]
    atr_14:        confloat(ge=0.0)
    spread_bp:     Optional[confloat(ge=0.0)] = None      # spread en bp (forex/crypto)
    adv_usd_20d:   Optional[confloat(ge=0.0)] = None      # average daily volume 20d (equity/crypto)
    fx_rate:       Optional[float] = None                 # conversion devise si ≠ base portfolio

class StrategyExitConfig(BaseModel):
    atr_stop_mult:           confloat(gt=0.0) = 2.0
    tp_rule:                 Literal["kijun", "tenkan", "hvn", "r_multiple"]
    tp_r_multiples:          list[confloat(gt=0.0)] = [1.5, 3.0]
    trailing:                Optional[Literal["chikou", "kijun", "atr"]] = None

class StrategyConfig(BaseModel):
    """Sous-schéma de strategies.yaml (§3.1). Chargé et validé au startup."""
    id:                      str
    enabled:                 bool = True
    requires_ichimoku_alignment: bool = True              # waiver du check C6 (§11.6)
    max_risk_pct_equity:     confloat(ge=0.0, le=0.05) = 0.01
    min_rr:                  confloat(ge=1.0) = 1.5
    min_composite_score:     confloat(ge=0.0, le=1.0) = 0.60
    coef_self_improve:       confloat(ge=0.5, le=1.5) = 1.0     # multiplicateur conviction (§13)
    entry:                   dict[str, float | bool | str]       # conditions spécifiques par stratégie
    exit:                    StrategyExitConfig
    timeframes:              list[Literal["1h", "4h", "1d"]]

# ---------- risk-gate -----------------------------------------------
class RiskCheckResult(BaseModel):
    check_id:    Literal[
        "C1_kill_switch", "C2_daily_loss", "C3_max_open_positions",
        "C4_exposure_per_class", "C5_circuit_breaker", "C6_ichimoku_alignment",
        "C7_token_budget", "C8_correlation_cap", "C9_macro_volatility",
        "C10_data_quality",
    ]
    passed:      bool
    severity:    Literal["blocking", "warn"]              # warn = pass but flag
    reason:      str                                      # message actionnable (cf. §2.6)
    evaluated:   bool = True                              # False si short-circuité (cf. §11.6)

class RiskDecision(BaseModel):
    proposal_id: str
    approved:    bool
    reasons:     list[str] = Field(default_factory=list)  # raisons de rejet (vide si approved=True)
    adjusted_size_pct: Optional[confloat(ge=0.0, le=1.0)] = None  # taille ajustée par la gate
    checks:      conlist(RiskCheckResult, min_length=10, max_length=10)   # toujours 10, ordre C1→C10
    ts:          str

    @classmethod
    def reject(cls, proposal_id: str, reasons: list[str], checks: list[RiskCheckResult]) -> "RiskDecision":
        """Factory pour rejet. Complète les checks manquants avec `evaluated=False`."""
        return cls(proposal_id=proposal_id, approved=False, reasons=reasons,
                   checks=_pad_checks(checks), ts=_utc_now())

    @classmethod
    def approve(cls, proposal_id: str, checks: list[RiskCheckResult],
                adjusted_size_pct: float) -> "RiskDecision":
        return cls(proposal_id=proposal_id, approved=True, reasons=[],
                   adjusted_size_pct=adjusted_size_pct,
                   checks=_pad_checks(checks), ts=_utc_now())


def _pad_checks(checks: list[RiskCheckResult]) -> list[RiskCheckResult]:
    """Complète la liste pour avoir les 10 checks C1→C10 dans l'ordre.
    Les checks non-évalués (short-circuit §11.6) sont marqués evaluated=False."""
    by_id = {c.check_id: c for c in checks}
    CHECK_IDS = [
        "C1_kill_switch", "C2_daily_loss", "C3_max_open_positions",
        "C4_exposure_per_class", "C5_circuit_breaker", "C6_ichimoku_alignment",
        "C7_token_budget", "C8_correlation_cap", "C9_macro_volatility",
        "C10_data_quality",
    ]
    padded = []
    for cid in CHECK_IDS:
        if cid in by_id:
            padded.append(by_id[cid])
        else:
            padded.append(RiskCheckResult(
                check_id=cid, passed=True, severity="warn",
                reason="short-circuited: une gate précédente a déjà rejeté",
                evaluated=False,
            ))
    return padded

# ---------- backtest-quick ------------------------------------------
class BacktestReport(BaseModel):
    strategy_id:     str
    period_from:     str
    period_to:       str
    trades_n:        conint(ge=0)
    win_rate:        confloat(ge=0.0, le=1.0)
    avg_rr:          float
    sharpe:          float
    max_dd_pct:      confloat(ge=0.0, le=1.0)
    monte_carlo_p5:  float                                # 5e percentile equity curve
    monte_carlo_p95: float
```

**Invariants cross-skills** :

- `Candidate.asset` est **toujours** présent dans `config/assets.yaml` — sinon rejet en amont.
- `SignalOutput.is_proposal` est figé à `False` côté type-system ; aucune stratégie ne peut produire un `TradeProposal` via `signal-crossing`. La seule porte est `build_proposal` (§8.9).
- `RiskDecision.checks` contient **toujours exactement 10 entrées**, dans l'ordre `C1` → `C10` de §11.6. Les checks non-évalués (short-circuit après un rejet bloquant) portent `evaluated=False` + `passed=True` par convention. La factory `_pad_checks()` complète automatiquement la liste avant validation Pydantic.
- Tous les `timestamp` / `ts` sont en UTC ISO-8601 avec suffixe `Z`. Les contrats les typent en `datetime` dans V2 ; en V1 on reste en `str` + validateur regex `^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$`.

#### 8.8.2 Scoring rapide de `market-scan`

Pour rester en O(nb_assets) sans appel LLM, `market-scan` calcule un score scalaire léger :

```python
score_scan = 0.35 * trend_slope_50              # pente SMA50 normalisée
           + 0.25 * relative_volume_20          # vol / moyenne 20j
           + 0.20 * abs(atr_pct_20)             # volatilité annualisée
           + 0.20 * momentum_roc_20             # ROC sur 20 barres
# clip [-1, 1]
```

Seuil de shortlist : `abs(score_scan) ≥ 0.25` **ET** `liquidity_ok`. Taille cible de la shortlist : **20 candidats max** (voir §8.8.3 pour le fallback si l'univers filtré est plus petit).

#### 8.8.3 Taille d'univers & shortlist

| Classe | Univers total V1 | Shortlist max/cycle | Source |
|---|---:|---:|---|
| Equity Euronext | ~120 (CAC 40 + SBF 120 mid-caps) | 10 | `config/assets.yaml` |
| Forex majors | 7 (EUR/USD, USD/JPY, GBP/USD, USD/CHF, AUD/USD, USD/CAD, NZD/USD) | 5 | idem |
| Crypto | 10 (top 10 market cap hors stablecoins) | 5 | idem |

**Total shortlist = 20/cycle max.** Si la shortlist filtrée est vide après scoring, le cycle log un `WARNING` `DATA_006 empty_shortlist` et s'arrête proprement — c'est un signal marché sans edge, pas une erreur.

### 8.9 Contrat `build_proposal` — signal → proposition

C'est **le** point où un diagnostic scalaire produit par `signal-crossing`
devient une proposition concrète (prix, stop, tp, R/R, sizing). Sans ce
contrat explicite, la règle d'or §2.1 « Python décide, LLM propose » ne
serait pas tenue : un skill LLM pourrait être tenté de fabriquer un
TradeProposal. Ici, c'est impossible — la transformation est logée dans le
module Python de la stratégie concernée, appelé depuis l'orchestrateur.

**Signature commune** (chaque `src/strategies/<id>.py` doit l'implémenter) :

```python
from dataclasses import dataclass

@dataclass
class TradeProposal:
    strategy_id:     str
    asset:           str
    asset_class:     str
    side:            str                 # "long" | "short"
    entry_price:     float
    stop_price:      float
    tp_prices:       list[float]         # tp1, tp2 (tp3 si défini)
    rr:              float               # (tp1 - entry) / (entry - stop), absolu
    conviction:      float               # [0, 1] — calqué sur signal.confidence
    risk_pct:        float               # [0, max_risk_per_trade_pct_equity]
    catalysts:       list[str]
    ichimoku:        IchimokuPayload     # typé, PAS un dict (cf. §8.8.1 A3)

def build_proposal(
    signal:       "SignalOutput",
    market_data:  "MarketSnapshot",
    regime:       "RegimeState",
    config:       "StrategyConfig",
    news_pulse:   "NewsPulse | None" = None,
) -> TradeProposal | None:
    """100 % déterministe, 0 token. Contrats StrategyConfig + MarketSnapshot en §8.8.1."""
```

**Pricing — règles déterministes** :

| Champ | Calcul | Source |
|---|---|---|
| `entry_price` | mid courant du plus récent tick / bougie close | `market_data.ohlcv` |
| `stop_price` | entry ± `config.exit.atr_stop_mult` × ATR(14) | `market_data.atr` + strategies.yaml |
| `tp_prices` | selon `config.exit` (tenkan, kijun, HVN, R-multiple) | `market_data` + strategies.yaml |
| `rr` | `abs((tp1 - entry) / (entry - stop))` | dérivé |
| `conviction` | `signal.confidence` × `config.coef_self_improve` (clippé [0, 1]) | signal-crossing + `StrategyConfig` (§8.8.1) |
| `risk_pct` | `min(config.max_risk_pct_equity, risk.max_risk_per_trade_pct_equity)` | strategies.yaml + risk.yaml |
| `catalysts` | `[news_pulse.top.title]` si `impact ≥ 0.5`, sinon `[]` | news-pulse |
| `ichimoku` | copie de `signal.ichimoku` sans recalcul | signal-crossing |

**Retourne `None` si** (et c'est la seule raison légitime) :
- Une condition `entry:` de strategies.yaml échoue malgré un score
  `signal-crossing` positif (ex: `obv_surge_vs_median_20d` pour breakout,
  qui n'est pas dans les 5 familles du signal composite)
- Le R/R calculé est inférieur à 1.0 (pas la peine de passer au risk-gate
  pour un trade manifestement mauvais — le gate rejetterait de toute façon
  sur `min_rr` mais on économise un appel)
- `market_data` insuffisant (bougies manquantes > 2, ATR indéfini)

**Ne retourne jamais `None` pour** :
- Une raison de risque macro (c'est le rôle de `risk-gate`, check §11.3)
- Une raison de kill-switch / drawdown (c'est le rôle de `risk-gate`, §11.1-2)
- Une raison d'alignement Ichimoku (c'est le rôle de `risk-gate`, §11.5 —
  `build_proposal` se contente de recopier `signal.ichimoku`, le gate tranche)

**Un module = une stratégie.** Pas de duck-typing, pas de factory. La liste
`active:` de `strategies.yaml` détermine quels modules sont chargés ; les
modules non listés sont ignorés. L'orchestrateur (§8.7) fait :

```python
from importlib import import_module
strategy_mod = import_module(f"src.strategies.{strategy_id}")
proposal     = strategy_mod.build_proposal(signal, market_data, regime, cfg)
```

**Tests unitaires obligatoires** (`tests/strategies/test_<id>.py`) :
1. Signal valide + market data propres → proposition cohérente (stop en
   dessous de l'entrée pour un long, R/R ≥ `min_rr`, etc.)
2. ATR nul → `None` (data insuffisante)
3. R/R < 1 avec TP trop proche → `None`
4. Signal short avec prix sous le kumo → proposition `side="short"` avec
   `ichimoku.aligned_short = true` copié
5. Idempotence : deux appels consécutifs avec mêmes inputs → même sortie

---

## 9. Simulation & Suivi P&L

### 9.1 Journal des trades simulés

**Principe** : les trades simulés sont enregistrés au **prix de marché au moment de la proposition**. Le P&L fictif est calculé lors de la mise à jour des prix.

```python
# src/simulation/journal.py

@dataclass
class SimulatedTrade:
    id: str                  # "T-001789"
    asset: str
    asset_class: str         # "forex", "crypto", "equity"
    strategy: str
    side: str                # "long" | "short"
    entry_price: float
    entry_time: datetime
    stop_price: float
    tp_prices: list[float]
    size_pct_equity: float   # % de l'équité fictive engagée
    conviction_score: float
    rr_estimated: float
    catalysts: list[str]

    # Champs mis à jour dynamiquement
    current_price: float     = 0.0
    exit_price: float        = 0.0
    exit_time: datetime      = None
    pnl_pct: float           = 0.0   # en % de l'entrée
    pnl_usd_fictif: float    = 0.0   # sur base 10 000 USD fictifs
    status: str              = "open" # "open" | "closed_tp" | "closed_sl" | "closed_manual"
    validated_by_human: bool = False
    llm_narrative: str       = ""

    def update_pnl(self, current_price: float) -> None:
        direction = 1 if self.side == "long" else -1
        self.current_price = current_price
        self.pnl_pct = direction * (current_price - self.entry_price) / self.entry_price

    def should_close(self, price: float) -> bool:
        if self.side == "long":
            return price <= self.stop_price or price >= self.tp_prices[-1]
        return price >= self.stop_price or price <= self.tp_prices[-1]
```

### 9.2 Format Excel (.xlsx)

```python
# src/simulation/pnl_excel.py

EXCEL_PATH = "data/simulation/journal.xlsx"

SHEETS = {
    "Journal":     "Tous les trades simulés, un par ligne",
    "Ouvertes":    "Positions ouvertes avec P&L mark-to-market",
    "Métriques":   "KPIs agrégés par stratégie, par marché, par période",
    "Équité":      "Courbe d'équité fictive (10 000 USD de départ)",
    "Calendrier":  "Trades par date — vue calendrier",
}

class PnLExcelWriter:

    def append_trade(self, trade: SimulatedTrade) -> None:
        """Ajoute une ligne dans la feuille Journal."""
        ...

    def refresh_summary(self) -> None:
        """
        Recalcule feuilles Métriques + Équité à partir du Journal.
        Appelé après chaque mise à jour de P&L.
        """
        ...

    def _compute_metrics_by_strategy(self) -> pd.DataFrame:
        """Retourne : trades, winrate, PF, Sharpe 30j, Sharpe 90j, max_dd."""
        ...
```

**Métriques calculées par feuille Métriques** :

| Métrique | Formule | Scope |
|---|---|---|
| Winrate | trades gagnants / total | Par stratégie, par marché, total |
| Profit Factor | gains bruts / pertes brutes | Par stratégie |
| Sharpe (fictif) | mean(r) / std(r) × √252 | 30j, 90j, total |
| Max Drawdown | (peak - trough) / peak | Total, par stratégie |
| Avg R réalisé | mean(pnl / risque initial) | Par stratégie |
| Trades/mois | count par mois | Par classe d'actif |

### 9.3 Équité fictive initiale

```yaml
# config/risk.yaml
simulation:
  initial_equity_usd: 10000
  base_currency: USD
```

### 9.4 Coûts d'exécution simulée

L'objectif de la simulation est d'approcher la réalité du live, donc chaque
trade simulé se voit appliquer un slippage et des frais forfaitaires (pas de
microstructure fine en V1) :

```yaml
# config/risk.yaml
slippage_bps_default: 3   # 3 bp = 0.03 % d'écart entrée/sortie vs mid
fee_bps_default:      7   # 7 bp = commission + financement consolidés
```

Justification des ordres de grandeur :

| Classe | Slippage typique | Fees typiques | Total |
|---|---|---|---|
| FX major (EURUSD, GBPUSD) | 0.2–0.5 bp | 0.1–0.3 bp | ~1 bp |
| Crypto BTC/ETH spot | 2–5 bp | 5–10 bp | 7–15 bp |
| Equity US liquides | 1–2 bp | 0.1–0.5 bp | ~2 bp |
| Equity mid-cap / ETF obscurs | 5–10 bp | 1–3 bp | 6–13 bp |

Les valeurs `slippage_bps_default=3` et `fee_bps_default=7` sont les
paramètres conservateurs qui s'appliquent quand l'asset n'a pas de surcharge
explicite dans `config/assets.yaml`. Ils sont volontairement pessimistes
(côté crypto) pour que la simulation reste une borne basse de la performance
live plausible. Une surcharge par asset peut être ajoutée dans `assets.yaml`
(champ `slippage_bps` / `fee_bps` au niveau de l'entrée) — `SimulatorAgent`
lit d'abord la surcharge puis retombe sur le default.

---

## 10. Mémoire SQLite

### 10.1 Initialisation

```python
# src/memory/db.py

DB_PATH = "data/memory.db"

def init_db(path: str = DB_PATH) -> sqlite3.Connection:
    con = sqlite3.connect(path, check_same_thread=False)
    con.execute("PRAGMA journal_mode=WAL")      # concurrence lecture/écriture
    con.execute("PRAGMA foreign_keys=ON")
    _create_tables(con)
    return con
```

### 10.2 Schémas des tables

```sql
-- Trades simulés (source de vérité)
CREATE TABLE trades (
    id              TEXT PRIMARY KEY,           -- "T-001789"
    asset           TEXT NOT NULL,
    asset_class     TEXT NOT NULL,
    strategy        TEXT NOT NULL,
    side            TEXT NOT NULL,
    entry_price     REAL NOT NULL,
    entry_time      TEXT NOT NULL,              -- ISO 8601
    stop_price      REAL NOT NULL,
    tp_prices       TEXT NOT NULL,              -- JSON array
    size_pct_equity REAL NOT NULL,
    conviction      REAL,
    rr_estimated    REAL,
    catalysts       TEXT,                       -- JSON array
    exit_price      REAL,
    exit_time       TEXT,
    pnl_pct         REAL,
    pnl_usd_fictif  REAL,
    status          TEXT NOT NULL DEFAULT 'open',
    validated       INTEGER NOT NULL DEFAULT 0,
    llm_narrative   TEXT,
    session_id      TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Leçons apprises (append-only)
CREATE TABLE lessons (
    id          TEXT PRIMARY KEY,               -- "L-0042"
    date        TEXT NOT NULL,
    content     TEXT NOT NULL,
    trade_ref   TEXT REFERENCES trades(id),
    tags        TEXT,                           -- JSON array
    confidence  REAL DEFAULT 1.0,
    archived    INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Hypothèses actives
CREATE TABLE hypotheses (
    id              TEXT PRIMARY KEY,           -- "H-007"
    content         TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'testing', -- testing|confirmed|rejected
    bayesian_score  REAL DEFAULT 0.5,
    started_at      TEXT NOT NULL,
    last_updated    TEXT,
    evidence        TEXT,                       -- JSON array {date, result, delta_score}
    archived        INTEGER NOT NULL DEFAULT 0
);

-- Snapshots de régime (1/jour)
CREATE TABLE regime_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    date            TEXT NOT NULL UNIQUE,
    macro           TEXT NOT NULL,              -- risk_on|risk_off|neutral
    volatility      TEXT NOT NULL,              -- low|mid|high|extreme
    trend_equity    TEXT,
    trend_forex     TEXT,
    trend_crypto    TEXT,
    prob_risk_off   REAL,
    prob_transition REAL,
    prob_risk_on    REAL,
    hmm_state       INTEGER,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Métriques de performance par stratégie (mise à jour quotidienne)
CREATE TABLE performance_metrics (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy        TEXT NOT NULL,
    date            TEXT NOT NULL,
    trades_total    INTEGER,
    trades_30d      INTEGER,
    winrate_total   REAL,
    winrate_30d     REAL,
    profit_factor   REAL,
    sharpe_30d      REAL,
    sharpe_90d      REAL,
    max_drawdown    REAL,
    active          INTEGER NOT NULL DEFAULT 1,
    UNIQUE(strategy, date)
);

-- Index pour les requêtes fréquentes
CREATE INDEX idx_trades_asset      ON trades(asset);
CREATE INDEX idx_trades_status     ON trades(status);
CREATE INDEX idx_trades_strategy   ON trades(strategy);
CREATE INDEX idx_trades_entry_time ON trades(entry_time);
CREATE INDEX idx_lessons_date      ON lessons(date);
```

> **Télémétrie** — Deux tables supplémentaires dédiées au panel coûts (`llm_usage`, `api_usage`) sont définies en §14.5.1. Elles sont créées par le même `init_db` mais regroupées avec le dashboard pour garder la cohérence fonctionnelle.

### 10.3 Façade Markdown humaine (MEMORY.md)

`MEMORY.md` n'est **plus** le contexte injecté au LLM — c'est une façade humaine
pour lire l'état mémoire d'un œil, diffable dans Git, archivable. Le contexte
LLM est assemblé dynamiquement par le `PromptBuilder` (§10.5), qui consomme
directement SQLite.

```python
# src/memory/markdown_exporter.py

class MarkdownExporter:
    """
    Génère MEMORY.md depuis SQLite pour consultation humaine. Limite par section
    pour garder un fichier < 200 KB — au-delà, `memory-consolidate` (§10.6)
    fusionne et archive.
    """

    MAX_LESSONS   = 30   # Les 30 leçons les plus récentes non archivées
    MAX_HYPOTHESES = 15
    MAX_TRADES_OPEN = 20

    def export(self, db: sqlite3.Connection) -> str:
        regime    = self._latest_regime(db)
        lessons   = self._recent_lessons(db, self.MAX_LESSONS)
        hypotheses = self._active_hypotheses(db, self.MAX_HYPOTHESES)
        perfs     = self._performance_table(db)
        open_trades = self._open_trades(db, self.MAX_TRADES_OPEN)

        return MEMORY_TEMPLATE.format(
            date=datetime.utcnow().isoformat(),
            regime=regime,
            lessons=lessons,
            hypotheses=hypotheses,
            perfs=perfs,
            open_trades=open_trades,
        )
```

### 10.4 Retrieval sémantique des leçons (FAISS)

**Problème** : envoyer au LLM les 30 leçons récentes à chaque appel injecte du
bruit (une leçon sur le FOMC n'est pas pertinente quand on analyse XAUUSD en
plein range). Solution : indexer chaque leçon à l'écriture et récupérer au
moment du cycle les top-K pertinentes au contexte (actif, régime, stratégie).

**Choix techniques** :

- **Embedder** : `fastembed` avec le modèle ONNX `BAAI/bge-small-en-v1.5`
  (~130 MB au téléchargement, embeddings 384 dim, déjà L2-normalisés). Pas de
  `torch` requis — ONNX runtime uniquement, cohérent avec la contrainte VPS slim.
- **Index** : `faiss.IndexFlatIP` (produit scalaire = cosinus sur vecteurs
  normalisés). À <500 leçons, pas besoin d'IVF ni de HNSW — recherche exacte
  en <1 ms.
- **Persistance** : `data/lesson_index.faiss` (binaire FAISS) + 
  `data/lesson_index.meta.json` (ids, contenus, tags, confiances).
- **Rebuild** : au démarrage si le meta est absent ou désynchronisé (pas
  d'heuristique fine en V1 — reconstruction complète < 1 s). Rebuildé aussi
  par `memory-consolidate` après archivage/fusion (§10.6).

**Schéma de requête** :

```python
# src/memory/lesson_index.py

@dataclass(frozen=True)
class LessonHit:
    lesson_id: str
    content: str
    tags: list[str]
    confidence: float
    score: float   # similarité cosinus (0..1)

class LessonIndex:
    def query(
        self,
        *,
        asset: str,                    # ex. "EURUSD"
        regime_tags: Sequence[str],    # ex. ["risk_on", "volatility_mid"]
        strategy: str,                 # ex. "breakout_momentum"
        free_text: str | None = None,  # narrative libre optionnelle
        k: int = 6,
    ) -> list[LessonHit]: ...
```

Le document indexé pour une leçon = `"{tags_space_separated}. {content}"`. La
requête = concaténation `"{asset} {strategy} {regime_tags} {free_text?}"`.
Simple, mais suffisant à l'échelle visée (quelques centaines de leçons).

**Budget runtime** : l'embedder se charge **lazy** — premier appel ~2 s (load
ONNX), appels suivants ~5-10 ms par requête. Au cold-start, le bot n'embeddi
pas. Mémoire résidente : +~200 MB (ONNX + modèle chargé).

### 10.5 Stratification du prompt & prompt caching Anthropic

**Problème** : `MEMORY.md` monolithique à maturité (~50k tokens) envoyé à chaque
appel LLM ferait exploser le budget ($63/mois en input tokens seuls contre un
budget total de $15/mois). Solution : découper le prompt en **4 couches de
volatilité décroissante** et exploiter le `cache_control` d'Anthropic, qui
stocke les préfixes de prompt pour un coût de lecture de 0.1× l'input normal.

**Les 4 couches + 1** :

| Couche | Contenu | Stable pendant | Cache |
|---|---|---|---|
| L1 | `CLAUDE.md` + règles permanentes | semaines | ephemeral |
| L2 | Leçons consolidées (`confidence > 0.8`, âge > 30j, tag `stable`) | jours | ephemeral |
| L3 | Top-K leçons pertinentes (§10.4) + hypothèses actives + régime du jour | ~24h (regénéré par `memory-consolidate` nocturne) | ephemeral |
| L4 | Trades ouverts + perf 30j par stratégie | ~1 cycle (6-12h) | ephemeral |
| L5 | Snapshot marché + indicateurs + news pour l'actif analysé | jamais | non caché |

L5 est le `user message`. L1 à L4 sont des `system` blocks avec
`cache_control: {"type": "ephemeral"}` aux frontières (plafond Anthropic : 4
breakpoints — on l'utilise intégralement).

**Gain financier estimé à maturité** (MEMORY.md à 50k tokens, 14 appels/jour) :

- Sans cache : 14 × 50k × $3/Mtok = **$63/mois** (inputs seuls)
- Avec cache (1 écriture + ~5 lectures par cycle, 2 cycles/jour) :
  - Écritures : 2 × 50k × $3.75/Mtok = $0.38/jour
  - Lectures : 12 × 50k × $0.30/Mtok = $0.18/jour
  - Total : $0.56/jour ≈ **$17/mois** (inputs) — acceptable dans le budget
- **Gain ≈ 73%** sur les inputs MEMORY.md. Combiné aux outputs (inchangés)
  et aux cycles d'ad-hoc/self-improve, le total projeté reste sous $15/mois.

**TTL** : ephemeral par défaut (5 min). Suffisant pour 6 appels tech dans un
cycle de quelques minutes. Le cache inter-cycles (6-12h) n'est pas visé — le
gain viendrait du beta `extended-cache-ttl-2025-04-11` (1h, coût d'écriture
2×) et ne vaut pas la complexité en V1.

**Implémentation** :

```python
# src/memory/prompt_builder.py

@dataclass
class PromptBundle:
    system_blocks: list[dict]   # [{"type": "text", "text": "...", "cache_control": {...}}, ...]
    user_content: str
    estimated_tokens: int

class PromptBuilder:
    def build(
        self, *, asset, regime, strategy_candidate,
        market_snapshot, news_context, k_lessons=6,
    ) -> PromptBundle:
        l1 = self._load_claude_md()                              # stable semaines
        l2 = self._render_stable_lessons()                       # stable jours
        l3 = self._render_dynamic_context(                       # stable ~24h
            self.index.query(asset=asset, regime_tags=regime.tags(),
                             strategy=strategy_candidate, k=k_lessons),
            regime,
        )
        l4 = self._render_portfolio_state()                      # stable ~1 cycle
        l5 = self._render_market_block(                          # jamais caché
            asset=asset, strategy_candidate=strategy_candidate,
            market_snapshot=market_snapshot, news_context=news_context,
        )
        # Les 4 premiers blocs portent cache_control : ephemeral
        return PromptBundle(
            system_blocks=self._finalize_system_blocks([
                ("L1", l1, True), ("L2", l2, True),
                ("L3", l3, True), ("L4", l4, True),
            ]),
            user_content=l5,
            estimated_tokens=...,
        )
```

**Appel Anthropic** :

```python
response = client.messages.create(
    model="claude-sonnet-4-6",
    system=bundle.system_blocks,           # liste de blocs avec cache_control
    messages=[{"role": "user", "content": bundle.user_content}],
    max_tokens=800,
)
# response.usage.cache_creation_input_tokens + cache_read_input_tokens → tracés
# dans llm_usage (§14.5.1) pour monitoring du taux de hit cache
```

**Télémétrie** : le tracker LLM (§14.5.2) stocke `cache_read_tokens` et
`cache_creation_tokens` en plus de `input_tokens` / `output_tokens`, ce qui
permet au dashboard (§14.3) d'afficher le taux de hit cache (cible > 80 %
dans un cycle).

### 10.6 Consolidation mémoire (`memory-consolidate`)

Tâche nocturne planifiée par APScheduler (cron `30 2 * * *`), implémentée par
`src/memory/consolidator.py`. Objectif : empêcher la croissance unbounded de
la table `lessons` et maintenir l'index FAISS propre.

**Pipeline (idempotent)** :

1. **Purge télémétrie** — supprime `llm_usage` et `api_usage` > 180 jours
   (cohérent avec §14.5.1).
2. **Archivage leçons stériles** — `archived = 1` pour toute leçon avec
   `confidence < 0.4` et non confirmée depuis 90 jours. Les leçons archivées
   restent en base (audit) mais sortent de l'index FAISS.
3. **Fusion de doublons** — pour chaque paire de leçons à similarité cosinus
   > 0.92, garde celle à `confidence` max et archive l'autre avec un tag
   `merged_into:<winner_id>` pour traçabilité.
4. **Promotion stable** — tag `stable` pour toute leçon avec
   `confidence > 0.8` ET `age > 30j`. Consommé par la couche L2 du
   `PromptBuilder` (§10.5).
5. **Rebuild FAISS** — reconstruit l'index depuis les leçons non archivées.
6. **Regénération `MEMORY.md`** — délègue à `MarkdownExporter` (§10.3).

**Rapport de run** :

```python
@dataclass
class ConsolidationReport:
    purged_telemetry_rows: int
    archived_stale_lessons: int
    merged_duplicate_pairs: int
    promoted_to_stable: int
    index_size_after: int
```

Loggé en `INFO` et envoyé en résumé Telegram seulement si mutations > 0 ou
si le pipeline a échoué — silence par défaut (§14.6).

**Seuils configurables** (en tête de `consolidator.py`, pas dans YAML — peu
susceptibles de changer hors réglages fins) :

```python
SIMILARITY_MERGE_THRESHOLD = 0.92    # fusion doublons
LOW_CONFIDENCE_ARCHIVE     = 0.40    # seuil archivage
STALE_LOW_CONFIDENCE_DAYS  = 90      # délai de grâce
TELEMETRY_RETENTION_DAYS   = 180     # cohérent avec §14.5.1
```

---

## 11. Risk Management

### 11.1 Kill Switch

```python
# src/risk/kill_switch.py

# Chemin résolu depuis l'env KILL_FILE_PATH (défaut: data/KILL). Le bot tourne
# sous un filesystem racine read-only en prod (§17.3) ; seul data/ est writable,
# donc le kill-switch vit là. Le Dockerfile fixe KILL_FILE_PATH=/app/data/KILL.
KILL_FILE = Path(os.environ.get("KILL_FILE_PATH", "data/KILL"))

class KillSwitch:
    def is_active(self) -> bool:
        return KILL_FILE.exists()

    def arm(self, reason: str) -> None:
        KILL_FILE.parent.mkdir(parents=True, exist_ok=True)
        KILL_FILE.write_text(f"{datetime.utcnow().isoformat()} — {reason}\n")
        log.critical(f"Kill-switch armé : {reason}")
        telegram.send_alert(f"🛑 KILL SWITCH ARMÉ : {reason}")

    def disarm(self) -> None:
        if KILL_FILE.exists():
            KILL_FILE.unlink()
            log.warning("Kill-switch désarmé manuellement")
```

Le fichier `data/KILL` (racine `/app/data/KILL` dans le container) est créé par :
- L'utilisateur (manuellement, via Telegram `/kill` ou `touch data/KILL`)
- Le circuit breaker (drawdown stratégie dépassé)
- 3 erreurs consécutives de fetch de données

### 11.2 Limites dures (`config/risk.yaml`)

```yaml
risk:
  # Position
  max_risk_per_trade_pct_equity: 0.75  # 0.75 % équité fictive max par trade
  min_rr: 1.8                           # R/R minimum accepté
  max_open_positions: 8
  max_open_per_class:
    forex:  3
    crypto: 3
    equity: 4

  # Portefeuille
  max_exposure_per_asset_class_pct: 40     # % équité max par classe
  max_correlated_positions: 3              # positions avec corrélation > seuil
  max_correlation_threshold: 0.70

  # Drawdown
  max_daily_loss_pct_equity: 2.0
  max_total_dd_pct: 15.0

  # Ichimoku — l'alignement est géré **par stratégie** dans
  # config/strategies.yaml (flag `requires_ichimoku_alignment`), pas ici.
  # Voir §11.5 pour le détail du check. On ne duplique PAS le défaut ici,
  # pour éviter deux sources de vérité contradictoires.

  # Circuit breaker
  circuit_breaker:
    dd_7d_threshold_vs_median: 2.0      # Désactivation si DD 7j > 2× médian historique
    min_dd_history_days: 30

  # Fenêtres d'évitement macro (offset_min = [avant, après] en minutes
  # autour de l'événement). Valeurs conservatrices — toute modif doit être
  # répercutée dans config/risk.yaml et les backtests de §11 régressés.
  avoid_windows:
    - name: fomc
      offset_min: [-45, +60]
    - name: cpi_us
      offset_min: [-30, +45]
    - name: nfp
      offset_min: [-30, +45]
    - name: ecb_meeting
      offset_min: [-30, +45]
    - name: weekly_close_crypto_sunday    # clôture hebdo crypto dim 00:00 UTC
      offset_min: [-60, +60]

  # Budget LLM
  llm:
    max_daily_tokens: 50000
    max_monthly_cost_usd: 15.0
```

### 11.3 Circuit Breaker par stratégie

```python
# src/risk/circuit_breaker.py

class CircuitBreaker:
    """
    Désactive automatiquement une stratégie si son drawdown sur 7 jours
    dépasse 2× son drawdown médian historique.
    """

    def check_strategy(self, strategy: str, db: sqlite3.Connection) -> CircuitState:
        dd_7d = self._compute_dd_7d(strategy, db)
        dd_median = self._compute_historical_dd_median(strategy, db)

        if dd_median is None or len(self._get_history(strategy, db)) < 30:
            return CircuitState.INSUFFICIENT_DATA

        if dd_7d > dd_median * 2.0:
            self._disable_strategy(strategy)
            kill_switch.arm(f"Circuit breaker : {strategy} DD 7j = {dd_7d:.1%} > 2× médian ({dd_median:.1%})")
            return CircuitState.TRIPPED

        return CircuitState.CLOSED
```

### 11.4 Budget Tokens

```python
# src/risk/gate.py

class TokenBudgetGate:

    def check(self, estimated_tokens: int) -> bool:
        today_used = self.db.get_tokens_used_today()
        monthly_cost = self.db.get_monthly_llm_cost()

        if today_used + estimated_tokens > self.config.max_daily_tokens:
            log.warning(f"Budget tokens journalier atteint : {today_used}")
            return False

        if monthly_cost >= self.config.max_monthly_cost_usd:
            log.error(f"Budget mensuel LLM atteint : ${monthly_cost:.2f}")
            kill_switch.arm("Budget mensuel LLM dépassé")
            return False

        return True
```

### 11.5 Gate Ichimoku (implémentation de la règle d'or §2.5)

§2.5 énonce : « une proposition ne sort du pipeline que si le risk-gate
passe ET Ichimoku est cohérent avec la direction proposée ». Sans
implémentation, cette règle resterait déclarative. La §8.9 `build_proposal`
recopie l'état Ichimoku depuis `signal-crossing` dans chaque TradeProposal.
Le risk-gate tranche ensuite via le check #6 :

```python
# src/risk/ichimoku_gate.py

@dataclass
class IchimokuGateResult:
    ok: bool
    waived: bool
    reason: str

def check(proposal: TradeProposal, strategies_cfg: dict) -> IchimokuGateResult:
    cfg = strategies_cfg["strategies"][proposal.strategy_id]
    requires = cfg.get(
        "requires_ichimoku_alignment",
        strategies_cfg["defaults"]["requires_ichimoku_alignment"],  # true
    )

    if not requires:
        return IchimokuGateResult(
            ok=True, waived=True,
            reason=f"waiver strategies.yaml[{proposal.strategy_id}]",
        )

    ich = proposal.ichimoku
    aligned = ich["aligned_long"] if proposal.side == "long" else ich["aligned_short"]
    if aligned:
        return IchimokuGateResult(ok=True, waived=False, reason="aligned")
    return IchimokuGateResult(
        ok=False, waived=False,
        reason=(
            f"ichimoku contrarien : side={proposal.side}, "
            f"price_above_kumo={ich['price_above_kumo']}, "
            f"tenkan_above_kijun={ich['tenkan_above_kijun']}, "
            f"chikou_above_price_26={ich['chikou_above_price_26']}"
        ),
    )
```

**Waiver** : déclaré par stratégie dans `config/strategies.yaml`. Les trois
stratégies waivered en V1 et leur justification :

| Stratégie | Waiver | Raison |
|---|---|---|
| `mean_reversion` | `false` | Contrarienne par construction (long sous kumo = setup) |
| `divergence_hunter` | `false` | Cherche les retournements, `distance_to_kumo_pct` intégré à `entry:` |
| `volume_profile_scalp` | `false` | Horizon intraday trop court pour le nuage 26/52 ; `ichimoku_not_opposed` suffit dans `entry:` |

Les quatre autres stratégies (`ichimoku_trend_following`,
`breakout_momentum`, `event_driven_macro`, `news_driven_momentum`)
déclarent `requires_ichimoku_alignment: true` explicitement. Défaut global :
`true` dans le bloc `defaults:` de strategies.yaml, pour que toute nouvelle
stratégie oubliant le flag hérite du comportement conservateur.

**Fail-safe** : si `proposal.ichimoku` est absent (bug `build_proposal`
qui a oublié de recopier depuis le signal), le check retourne `ok=False`
avec `reason="ichimoku payload missing"`. Le défaut en cas d'ambiguïté est
le rejet — jamais l'approbation.

**Pas de recalcul** : le check n'appelle aucun indicateur. Il compare des
booléens déjà produits par `signal-crossing` (§5.1). Cela rend l'évaluation
en O(1) et évite toute divergence d'implémentation entre le score et le
gate.

### 11.6 Inventaire des 10 contrôles du risk-gate

Le skill `risk-gate` (§8.8) annonce « 10 contrôles déterministes ». Voici la liste ordonnée. Chaque check retourne un `RiskCheckResult` (§8.8.1). **Ordre d'exécution = ordre du tableau** — dès qu'un `blocking` échoue, la proposition est rejetée sans évaluer les checks suivants (short-circuit, gain de temps).

| # | ID | Famille | Source du seuil | Que vérifie |
|---|---|---|---|---|
| C1 | `C1_kill_switch` | RUN | `data/KILL` présent → fail | §11.1 — freeze total |
| C2 | `C2_daily_loss` | RISK | `risk.yaml.max_daily_loss_pct_equity` | §11.2 — perte cumulée J > seuil → fail |
| C3 | `C3_max_open_positions` | RISK | `risk.yaml.max_open_positions_total` + per-class | §11.2 — saturation du portefeuille |
| C4 | `C4_exposure_per_class` | RISK | `risk.yaml.max_exposure_pct_per_class` | §11.2 — équité engagée par classe |
| C5 | `C5_circuit_breaker` | RISK | `risk.yaml.circuit_breaker.dd_7d_vs_median_threshold` | §11.3 — stratégie trip → fail si `strategy_id` matché |
| C6 | `C6_ichimoku_alignment` | RISK | `strategies.yaml[id].requires_ichimoku_alignment` | §11.5 — règle d'or §2.5 |
| C7 | `C7_token_budget` | LLM | `risk.yaml.llm.max_daily_tokens` + `max_monthly_cost_usd` | §11.4 — budget atteint → fail |
| C8 | `C8_correlation_cap` | RISK | `risk.yaml.max_correlated_exposure_pct` (défaut 20 %) | Refuse si ajouter cette position porte l'exposition corrélée (ρ > 0.7 sur 60j) au-delà du seuil |
| C9 | `C9_macro_volatility` | DATA | `risk.yaml.macro_vol_cap.vix` (défaut 35) + `hmm_confidence_min` (0.55) | Fail si VIX > cap **ET** HMM régime `risk_off` avec confiance > 0.80 — protection crash |
| C10 | `C10_data_quality` | DATA | `DataQualityReport` (§7.3) | Fail si `is_fresh=False` **ou** `has_outliers=True` **ou** source utilisée = dernier fallback |

**Sévérité** — tous les checks sont `blocking` par défaut. Trois peuvent être dégradés en `warn` via `risk.yaml.warn_only: [...]` pour une phase de calibration :

```yaml
# config/risk.yaml — extrait §11.6
warn_only: []          # V1 : aucun — sécurité max. Ex: ["C9_macro_volatility"] pour désactiver en calibration.
```

**Ordre de court-circuit** — C1 (kill-switch) est toujours évalué en premier, C10 (data quality) en dernier. Les checks C2-C9 s'exécutent dans l'ordre de coût croissant (lecture DB, puis calculs de corrélation).

**Contrat de sortie** — `RiskDecision.approved = all(c.passed for c in checks if c.severity == "blocking")`. Le champ `checks` expose les 10 résultats pour l'audit/dashboard, même en cas de court-circuit (les checks non évalués portent `passed=True, reason="short-circuited by Cn"`).

**Tests d'acceptance** (`tests/unit/test_risk_gate.py`) — un test nommé par check :

```python
def test_C1_kill_switch_blocks(): ...
def test_C2_daily_loss_blocks_at_threshold(): ...
def test_C3_max_positions_blocks_on_overflow(): ...
def test_C4_exposure_per_class_blocks(): ...
def test_C5_circuit_breaker_blocks_tripped_strategy(): ...
def test_C6_ichimoku_blocks_contrarian_non_waived(): ...
def test_C7_token_budget_blocks_over_daily_cap(): ...
def test_C8_correlation_cap_blocks_overlap(): ...
def test_C9_macro_volatility_blocks_in_crash(): ...
def test_C10_data_quality_blocks_stale_or_outlier(): ...
def test_all_ten_checks_present_in_decision(): ...  # invariant §8.8.1
```

---

## 12. Détection de régime (HMM)

### 12.1 Principe

Un HMM (Hidden Markov Model) à **3 états** est entraîné sur les rendements et la volatilité pour classer le régime de marché. Il produit des **probabilités** (pas des seuils binaires), ce qui réduit le flip-flop aux frontières.

**États** :
- **risk_on** : marchés haussiers, volatilité basse/moderate, corrélations normales
- **risk_off** : repli vers les valeurs sûres, vol élevée, corrélations extrêmes
- **transition** : régime intermédiaire ou indécis

### 12.2 Implémentation

#### 12.2.1 Data provenance des features (source unique : FRED + CoinGecko)

Les 5 features du HMM sont calculées depuis des séries **publiques et gratuites** (MVP, §7.1) :

| Feature | Série source | Provider | Endpoint / Série ID | Fréquence | Transformation |
|---|---|---|---|---|---|
| `spx_return` | S&P 500 close | FRED | `SP500` | Daily | log-return `ln(C_t / C_{t-1})` |
| `vix` | VIX close | FRED | `VIXCLS` | Daily | niveau brut (pas de transformation) |
| `dxy_change` | DXY (USD Index) | FRED | `DTWEXBGS` (Broad USD Index) | Daily | variation pct `(C_t - C_{t-1}) / C_{t-1}` |
| `yield_10y_change` | 10-Year Treasury | FRED | `DGS10` | Daily | diff absolue `C_t - C_{t-1}` (en points de base) |
| `crypto_vol` | BTC/USD close | CoinGecko (free, rate-limited) | `/api/v3/coins/bitcoin/market_chart?days=60` | Daily | **std des log-returns sur 20j glissants** |

**Formule explicite `crypto_vol`** :
```
returns_t = [ln(P_{t-i+1} / P_{t-i}) for i in range(1, 21)]   # 20 rendements
crypto_vol_t = std(returns_t)  # écart-type non annualisé
```

**Remarque** : toutes les séries sont synchronisées sur le calendrier **NYSE business days** (FRED est par construction aligné). BTC étant 24/7, on échantillonne le close UTC 23:59 des jours ouvrés NYSE pour alignement.

**Fallback si FRED indisponible** : les features `spx_return`, `vix`, `dxy_change`, `yield_10y_change` peuvent être scrapées depuis Stooq (`^spx`, `^vix`, etc. en CSV) — même politique de fallback que §7.1. Si **les deux** tombent, le HMM utilise le dernier `RegimeState` persisté (`data/cache/last_regime.json`) et le log `log.error("regime_stale_features", fallback="last_known")`.

#### 12.2.2 Training et re-training

| Paramètre | Valeur | Justification |
|---|---|---|
| **Fenêtre d'entraînement** | 5 ans de daily (~1260 observations) | couvre 1 cycle économique complet, incluant vol COVID mars 2020 pour le bootstrap régimes |
| **Minimum pour bootstrap** | 3 ans (~750 obs) | rejeté si insuffisant → log.critical + blocage démarrage |
| **Fréquence re-training** | Mensuelle (1er du mois, 02:00 UTC) | via cron `hmm-retrain` ; stratégie walk-forward (refit sur 5 dernières années glissantes) |
| **Trigger re-training manuel** | Telegram `/regime retrain` | admin-only, avec dry-run par défaut |
| **Re-training auto self-improve** | Si accuracy back-test < 70% sur 30 derniers jours | détecté par self-improve hebdo (§13.2) → patch proposé |

#### 12.2.3 Model versioning et persistance

```
data/models/
  ├── regime_hmm_v1.pkl       # version active (symlink → v{n})
  ├── regime_hmm_v{n}.pkl     # modèles archivés (6 derniers conservés)
  ├── regime_hmm_v{n}.meta.json  # métadonnées (training window, features stats, accuracy)
  └── regime_hmm.lock         # lock file pendant retraining
```

Format `meta.json` :
```json
{
  "version": 3,
  "trained_at": "2026-04-01T02:00:00Z",
  "training_window": {"start": "2021-04-01", "end": "2026-03-31"},
  "n_observations": 1260,
  "feature_means": [0.0004, 18.2, 0.0001, 0.05, 0.024],
  "feature_stds":  [0.012,   7.1, 0.008,  0.12, 0.011],
  "state_map": {"0": "risk_on", "1": "transition", "2": "risk_off"},
  "accuracy_backtest_30d": 0.78,
  "hmm_params": {"n_components": 3, "covariance_type": "diag", "n_iter": 200, "random_state": 42}
}
```

Rollback : si la v{n+1} dégrade l'accuracy de plus de 5 points vs v{n}, self-improve ré-active automatiquement le symlink sur v{n} et log `log.critical("regime_model_rollback", from_version=n+1, to_version=n)`.

#### 12.2.4 Code de référence

```python
# src/regime/hmm_detector.py

from hmmlearn import hmm
import numpy as np
import joblib
import json
from pathlib import Path
from datetime import datetime

MODELS_DIR = Path("data/models")
MODEL_LINK = MODELS_DIR / "regime_hmm_v1.pkl"  # symlink vers version active
CACHE_LAST = Path("data/cache/last_regime.json")
WINDOW_DAYS = 60  # fenêtre glissante pour detect()

@dataclass
class RegimeState:
    macro: str              # "risk_on" | "risk_off" | "transition"
    volatility: str         # "low" | "mid" | "high" | "extreme"
    probabilities: dict[str, float]   # {"risk_on": 0.72, ...}
    hmm_state: int
    date: str


class RegimeDetector:
    """
    SPX / VIX / DXY / yields 10y = features de contexte macro pour le HMM,
    PAS des actifs tradables en V1 (cf. §1.1 — univers de trading = Euronext Paris).
    Ces séries sont consommées en lecture seule par le détecteur de régime.
    """

    def __init__(self):
        self.model, self.meta = self._load_or_train()
        self._state_map = {int(k): v for k, v in self.meta["state_map"].items()}

    def detect(self) -> RegimeState:
        try:
            features = self._build_features(window_days=WINDOW_DAYS)
        except DataUnavailableError as e:
            log.error("regime_stale_features", fallback="last_known", err=str(e))
            return self._load_last_regime()

        state_seq = self.model.predict(features)
        proba_seq = self.model.predict_proba(features)

        current_state = int(state_seq[-1])
        current_proba = proba_seq[-1]

        regime = RegimeState(
            macro=self._state_map[current_state],
            volatility=self._classify_volatility(features[-1]),
            probabilities={self._state_map[i]: float(p) for i, p in enumerate(current_proba)},
            hmm_state=current_state,
            date=datetime.utcnow().date().isoformat(),
        )
        self._persist_last_regime(regime)
        return regime

    def _build_features(self, window_days: int = WINDOW_DAYS) -> np.ndarray:
        """
        Returns: np.ndarray shape (window_days, 5)
        Columns: [spx_return, vix, dxy_change, yield_10y_change, crypto_vol]
        Source: FRED (primary) | Stooq (fallback) | CoinGecko (BTC)
        Détails cf. §12.2.1.
        """
        spx   = fetch_fred_series("SP500",    days=window_days + 25)
        vix   = fetch_fred_series("VIXCLS",   days=window_days + 1)
        dxy   = fetch_fred_series("DTWEXBGS", days=window_days + 1)
        y10y  = fetch_fred_series("DGS10",    days=window_days + 1)
        btc   = fetch_coingecko_ohlc("bitcoin", days=window_days + 25)

        # Transformations (cf. §12.2.1)
        spx_ret       = np.diff(np.log(spx))[-window_days:]
        vix_level     = vix[-window_days:]
        dxy_pct       = (np.diff(dxy) / dxy[:-1])[-window_days:]
        y10y_diff     = np.diff(y10y)[-window_days:]
        btc_logret    = np.diff(np.log(btc))
        crypto_vol    = np.array([btc_logret[i-20:i].std()
                                   for i in range(20, 20 + window_days)])

        return np.column_stack([spx_ret, vix_level, dxy_pct, y10y_diff, crypto_vol])

    def _load_or_train(self) -> tuple[hmm.GaussianHMM, dict]:
        if MODEL_LINK.exists():
            model = joblib.load(MODEL_LINK)
            meta  = json.loads(MODEL_LINK.with_suffix(".meta.json").read_text())
            return model, meta
        return self._train(years=5)

    def _train(self, years: int = 5) -> tuple[hmm.GaussianHMM, dict]:
        features = self._load_historical_features(years=years)
        if len(features) < 750:  # ~3 ans minimum
            raise InsufficientTrainingDataError(f"got {len(features)} obs, need >=750")

        model = hmm.GaussianHMM(n_components=3, covariance_type="diag",
                                n_iter=200, random_state=42)
        model.fit(features)
        version = self._next_version()
        path = MODELS_DIR / f"regime_hmm_v{version}.pkl"
        joblib.dump(model, path)
        meta = self._build_meta(version, features, model)
        path.with_suffix(".meta.json").write_text(json.dumps(meta, indent=2))
        MODEL_LINK.unlink(missing_ok=True)
        MODEL_LINK.symlink_to(path.name)
        log.info("regime_model_trained", version=version, n_obs=len(features))
        return model, meta

    def _load_historical_features(self, years: int) -> np.ndarray:
        """
        Charge 5 ans de daily history pour toutes les features.
        Source primaire = FRED (via data/cache/fred/*.csv) | fallback Stooq CSV.
        Gère les jours fériés NYSE via forward-fill (max 3j).
        """
        end   = datetime.utcnow().date()
        start = end - timedelta(days=int(years * 365.25))
        df = self._fetch_multi_source(start, end)
        df = df.ffill(limit=3).dropna()
        return self._compute_features_matrix(df)

    def _classify_volatility(self, feature_row: np.ndarray) -> str:
        vix = feature_row[1]
        if vix < 15:  return "low"
        if vix < 25:  return "mid"
        if vix < 40:  return "high"
        return "extreme"

    def _persist_last_regime(self, regime: RegimeState) -> None:
        CACHE_LAST.parent.mkdir(parents=True, exist_ok=True)
        CACHE_LAST.write_text(json.dumps(asdict(regime)))

    def _load_last_regime(self) -> RegimeState:
        if not CACHE_LAST.exists():
            # cold-start dégradé : régime "transition" par défaut
            return RegimeState(macro="transition", volatility="mid",
                               probabilities={"risk_on": 0.33, "transition": 0.34, "risk_off": 0.33},
                               hmm_state=1, date=datetime.utcnow().date().isoformat())
        return RegimeState(**json.loads(CACHE_LAST.read_text()))
```

**Erreurs explicites loguées** (cf. §7.5 taxonomie) :

| Condition | Code | Niveau | Action |
|---|---|---|---|
| FRED API key manquante | `CFG_MISSING_FRED_KEY` | CRITICAL | bloque démarrage |
| FRED 503 / timeout | `NET_FRED_UNAVAILABLE` | ERROR | fallback Stooq |
| Stooq fallback KO aussi | `DATA_REGIME_STALE` | ERROR | utilise `last_regime.json` |
| Historique < 3 ans | `DATA_INSUFFICIENT_HISTORY` | CRITICAL | bloque training, alerte Telegram |
| Model pickle corrompu | `DATA_MODEL_CORRUPT` | CRITICAL | retrain forcé, version+1 |

### 12.3 Usage dans la stratégie

```python
# Exemple de filtre régime — les stratégies réelles sont listées §6
def is_regime_compatible(regime: RegimeState, strategy: str) -> bool:
    if strategy == "ichimoku_trend_following":
        # Nécessite régime directionnel clair (§6.1)
        return regime.probabilities["risk_on"] > 0.6 or regime.probabilities["risk_off"] > 0.6
    if strategy == "mean_reversion":
        # Fonctionne mieux en transition / volatilité mid (§6.3)
        return regime.macro == "transition" and regime.volatility in ("low", "mid")
    return True
```

---

## 13. Self-Improve

### 13.1 Boucles à 4 échelles de temps

| Boucle | Fréquence | Pilote | Sortie | Gate humaine |
|---|---|---|---|---|
| **Intra-cycle** | Chaque analyse | Logs + SQL | Observations dans SQLite | Non |
| **Quotidienne** | 23:30 UTC | `memory-consolidate` | MEMORY.md actualisé | Non |
| **Hebdomadaire** | Dimanche 22:00 UTC | `self-improve` | Patch + PR locale | **Oui** |
| **Mensuelle** | 1er du mois 18:00 UTC | Revue architecture | ADR dans `docs/adr/` | **Oui** |

### 13.2 Processus hebdomadaire self-improve

```
1. COLLECTE (Python pur)
   - Lit les 30 derniers trades depuis SQLite
   - Calcule 20 features par trade (régime, score composite, heure, ADX, RSI...)
   - Identifie les trades perdants (pnl_pct < -0.5%)

2. DIAGNOSTIC (LLM — 1 appel, max 1000 tokens)
   - Prompt : "Voici 30 trades avec leurs features. Identifie 3 patterns dominants
     dans les trades perdants. Réponds en JSON."
   - Sortie attendue : [{pattern, frequency, suggested_fix}, ...]

3. IMPLÉMENTATION (LLM — 1 appel par patch, max 800 tokens)
   - Génère le diff Python dans une branche git locale
   - Format : patch_YYYYMMDD_slug.diff

4. VALIDATION (Python pur)
   - backtest walk-forward sur 3 ans
   - Monte-Carlo 1000 bootstraps
   - Deflated Sharpe Ratio

5. SÉLECTION
   - Garde uniquement les patches avec :
     * t-stat vs baseline > 2.0
     * Sharpe walk-forward > baseline
     * DD non aggravé
     * > 50 trades sur période

6. PR LOCALE
   - Crée IMPROVEMENTS_PENDING.md avec diff, métriques, risques, rollback

7. NOTIFICATION Telegram
   - Envoie résumé + lien vers IMPROVEMENTS_PENDING.md
   - Demande validation humaine

8. APRÈS VALIDATION
   - Merge dans main
   - Paper-trading 14 jours
   - Activation si stable
```

```python
# src/self_improve/validator.py

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
    recommendation: str

def validate_patch(patch: StrategyPatch, db: sqlite3.Connection) -> PatchValidationResult:
    baseline = run_walk_forward(patch.strategy_original, years=3)
    patched  = run_walk_forward(patch.strategy_patched,  years=3)
    t_stat   = compute_t_stat(baseline.returns, patched.returns)
    dsr      = deflated_sharpe_ratio(patched.sharpe, n_trials=patch.trial_count)

    passed = (
        t_stat > 2.0 and
        patched.sharpe > baseline.sharpe and
        patched.max_dd <= baseline.max_dd * 1.1 and
        patched.trade_count >= 50 and
        dsr > 0.95
    )
    return PatchValidationResult(
        patch_id=patch.id, passed=passed,
        sharpe_baseline=baseline.sharpe, sharpe_patch=patched.sharpe,
        t_stat=t_stat, dd_baseline=baseline.max_dd, dd_patch=patched.max_dd,
        trade_count=patched.trade_count,
        recommendation="MERGE" if passed else "REJECT",
    )
```

### 13.3 Garde-fous self-improve

#### 13.3.1 Règles générales

- **Maximum 1 patch mergé par semaine** — pour conserver l'attribution des performances.
- Les patches impactant l'architecture, le risk gate (§11) ou les contrats Pydantic (§8.8.1) nécessitent une **ADR** (obligatoire, pas de merge sans ADR validé).
- **Scope interdit** : un patch auto-généré NE PEUT PAS modifier `src/risk/`, `src/orchestrator/kill_switch.py`, `config/risk.yaml` (§3.1), ni le contrat `RiskDecision`. Ces fichiers sont dans une blacklist `.self-improve-ignore` vérifiée avant PR.

#### 13.3.2 Critères de validation concrets

Un patch ne peut passer en **paper-trading** que si **tous** les critères ci-dessous sont remplis (cf. `PatchValidationResult.passed` en §13.2) :

| Critère | Seuil | Rationale |
|---|---|---|
| t-stat (patch vs baseline) | > 2.0 | significativité statistique |
| Sharpe walk-forward patch | > Sharpe baseline | amélioration stricte |
| Max drawdown patch | ≤ max_dd baseline × 1.10 | pas d'aggravation significative |
| Trade count sur la fenêtre | ≥ 50 | échantillon statistiquement exploitable |
| Deflated Sharpe Ratio (DSR) | > 0.95 | contrôle p-hacking multi-essais |
| Scope du patch | hors blacklist §13.3.1 | aucun fichier risk-critique modifié |

#### 13.3.3 Processus de validation humaine (qui approuve, comment)

1. **Génération automatique** du `IMPROVEMENTS_PENDING.md` contenant : diff, metrics, risks, rollback command, patch_id.
2. **Notification Telegram** (§14.6) : message avec boutons `/approve <patch_id>`, `/reject <patch_id>`, `/defer <patch_id>`.
3. **Approbateur unique** : l'opérateur humain listé dans `config/telegram.yaml:admin_user_id`. Aucune auto-approbation possible même si tous les critères sont verts.
4. **Délai d'expiration** : 7 jours. Sans réponse → auto-expire, patch archivé dans `data/patches/expired/`, log `log.warning("patch_expired", patch_id=...)`.
5. **Votes multiples** (cas de patches concurrents la même semaine) : si 2+ patches passent la validation automatique, ils sont **sérialisés** — un seul mergeable par semaine (§13.3.1). Le conflit est résolu par score combiné `rank = 0.5 * t_stat_normalized + 0.3 * sharpe_delta + 0.2 * dsr`, le reste est reporté à la semaine suivante (file FIFO dans `data/patches/queue/`).

#### 13.3.4 Phase paper-trading (14 jours)

Après merge, le patch entre en phase "canary" paper-trading **avant activation full**. Critères de stabilité sur 14 jours :

| Check | Seuil pour passer "stable" |
|---|---|
| Nombre de trades live-simulés | ≥ 20 |
| Sharpe live vs Sharpe backtest | écart ≤ 30 % (live_sharpe >= 0.7 × backtest_sharpe) |
| Max drawdown live | ≤ max_dd backtest × 1.25 |
| Erreurs exceptions runtime | 0 (toute exception non-captée = fail) |
| Divergence distribution returns (KS-test live vs backtest) | p-value > 0.05 |

Si **tous** OK après 14j → patch activé définitivement (flag `active=true` dans `data/patches/history.db`).
Si **un seul KO** → rollback automatique (§13.3.5).

#### 13.3.5 Mécanisme de rollback

**Rollback automatique** déclenché par :
- Échec d'un critère §13.3.4 pendant les 14 jours canary
- Kill-switch activé pendant la phase canary (patch suspect par défaut)
- Self-improve detecte une régression sur la semaine suivante (Sharpe glissant 7j < baseline × 0.8)

**Procédure** :
```bash
# src/self_improve/rollback.py
def rollback(patch_id: str, reason: str) -> RollbackResult:
    commit = db.query("SELECT merge_commit FROM patches WHERE id = ?", patch_id)
    subprocess.run(["git", "revert", "--no-edit", commit], check=True)
    subprocess.run(["git", "push", "origin", "main"], check=True)
    db.execute("UPDATE patches SET status='rolled_back', rollback_reason=? WHERE id=?",
               (reason, patch_id))
    telegram.send(f"🔄 Patch {patch_id} rolled back. Reason: {reason}")
    log.critical("self_improve_rollback", patch_id=patch_id, reason=reason)
```

**Rollback manuel** (Telegram admin-only) : `/rollback <patch_id>` → même procédure + note `reason="manual"`.

**Journalisation** : chaque rollback est persisté dans `data/patches/history.db` (table `rollbacks`) avec colonnes `(patch_id, triggered_at, reason, triggered_by, metrics_snapshot_json)`. Visible dans le dashboard (§14) dans l'onglet "Patches" (à prévoir dans une itération V2, hors MVP).

---

## 14. Dashboard HTML & Notifications Telegram

Le dashboard est l'interface principale de décision. Il doit permettre à l'opérateur (humain unique dans la boucle, cf. §2.4) de **valider ou rejeter les opportunités en moins de 60 secondes**, tout en gardant en permanence un œil sur la santé du portefeuille simulé et sur la consommation de tokens/APIs. Il est régénéré à chaque cycle (`data/dashboards/YYYY-MM-DD/{session}.html`) et servi par un endpoint FastAPI léger qui expose aussi les actions `POST /validate/{id}` et `POST /reject/{id}` (cf. §17.2).

**Mini-sommaire**

- 14.1 Vision, audience & layout global
- 14.2 Opportunités — cartes détaillées
- 14.3 Panel Coûts — tokens LLM & APIs de données
- 14.4 Portefeuille, régime, signaux & stratégies
- 14.5 Data model & pipeline de build
- 14.6 Notifications Telegram

### 14.1 Vision, audience & layout global

**Trois questions auxquelles le dashboard répond, dans cet ordre** :

1. **Quelles opportunités sont proposées et dois-je en valider ?** — bloc central (14.2).
2. **Où en est le portefeuille simulé, le régime et le risque ?** — haut et colonne de droite (14.4).
3. **Combien je dépense en tokens et en appels APIs, et y a-t-il une dérive ?** — panel dédié (14.3).

**Contraintes techniques** :

- Un seul fichier HTML statique par cycle ; pas de SPA. Tailwind via Play CDN pour le style, Chart.js via CDN pour les graphes, Heroicons inline SVG pour les icônes. Aucun framework JS.
- Balise `<meta http-equiv="refresh" content="300">` pour un refresh auto toutes les 5 minutes, complétée par un bouton "↻ Actualiser" qui relit le dernier fichier produit pour la session en cours.
- Mode sombre par défaut (bg `slate-950`, accents `emerald-500` / `rose-500`).
- Responsive 1 colonne < 768 px / 2 colonnes 768–1280 px / 3 colonnes ≥ 1280 px.
- Audience : opérateur unique (propriétaire du bot). Pas d'auth applicative — Caddy impose un `basic_auth` (cf. §17.4).

**Grille cible (desktop ≥ 1280 px)** :

```
┌─── En-tête : session, régime, marchés-clés, kill-switch, horodatage ───────────────┐
├─────────────────────────────────────────┬──────────────────────────────────────────┤
│                                         │  BLOC 2 — PORTEFEUILLE (14.4.1)          │
│  BLOC 1 — OPPORTUNITÉS (14.2)           │  Équité, sparkline 30j, exposition donut │
│  Cartes empilées, triées par            ├──────────────────────────────────────────┤
│  conviction décroissante                │  BLOC 3 — COÛTS & APIs (14.3)            │
│                                         │  Tokens jour/mois, coût $, breakdowns    │
│                                         ├──────────────────────────────────────────┤
│                                         │  BLOC 4 — Risque (14.4.2)                │
│                                         │  DD courant, corrélations, circuit BRK   │
├─────────────────────────────────────────┴──────────────────────────────────────────┤
│  BLOC 5 — Heatmap : actifs × composants score                        (14.4.3)      │
├───────────────────────────────────────────────────┬────────────────────────────────┤
│  BLOC 6 — News & catalyseurs (timeline 72h)       │  BLOC 7 — Perf stratégies 30j │
├───────────────────────────────────────────────────┴────────────────────────────────┤
│  BLOC 8 — Notes de l'agent : ce que j'observe, ce dont je doute       (14.4.4)    │
└────────────────────────────────────────────────────────────────────────────────────┘
```

**En-tête** : session (`pre_europe · 2026-04-17 07:00 UTC`), régime HMM avec probabilités (`risk_off 72 %`), VIX/DXY/BTC, pastille kill-switch (vert `armé=off` / rouge `armé=on`), bouton "↻", heure de génération.

---

### 14.2 Opportunités — cartes détaillées

Chaque proposition validée par le `RiskManagerAgent` est rendue comme une **carte autonome** empilée verticalement. Une carte concentre toute l'information nécessaire à la décision sans scroll horizontal, sans drill-down ni pop-up. Les cartes sont triées par `conviction` décroissante, puis par `|composite_score|`.

#### 14.2.1 Anatomie de la carte

Dans l'ordre vertical :

1. **Bandeau d'en-tête** (hauteur 48 px)
   - À gauche : badge coloré `LONG` (emerald-500) ou `SHORT` (rose-500), ticker en gras, chip classe d'actifs (`forex` / `crypto` / `equity`), chip stratégie (`ichimoku_trend_following`, `breakout_momentum`, …).
   - À droite : ID proposition (`P-0042`), âge (`il y a 7 min`), pastille d'alignement au régime (✓ aligné / ⚠ contre-tendance).

2. **Prix & niveaux** (ligne horizontale, monospace)
   - `Entry 1.0712 · Stop 1.0758 · TP1 1.0668 · TP2 1.0612 · R/R 2.2 · Size 1.0 %`
   - Mini-règle graphique SVG positionnant stop/entry/TP sur un axe horizontal, avec distance relative affichée en %. Permet d'évaluer d'un coup d'œil si le stop est serré ou large.

3. **Conviction & composite score** (bloc breakdown)
   - Jauge circulaire `conviction` (0 → 1) au centre avec la valeur `composite_score` signée au-dessus.
   - 4 mini-barres horizontales pour les composants : `ichimoku`, `trend`, `momentum`, `volume` — couleur selon signe, longueur selon `|valeur|`.
   - Tooltip au survol : indicateurs individuels (Supertrend, MACD, RSI, …) avec leur valeur brute.

4. **Thèse narrative** (2–4 lignes)
   - Texte généré par `TechnicalAnalystAgent` (champ `llm_narrative`).
   - Italique, couleur atténuée. Max 280 caractères. Tronquée avec "… voir détails" au-delà.

5. **Catalyseurs & risk flags**
   - Puces `emerald-400` pour les catalyseurs positifs (`"DXY strength"`, `"CPI US above expectations"`).
   - Puces `amber-400` pour les risk flags (`"FOMC dans 24h"`, `"corrélation BTC > 0.7 avec trade existant"`).
   - Vide si aucun (affiche un discret "—").

6. **Contexte régime** (1 ligne)
   - `Régime : risk_off (72 %) · Volatilité : mid · Trend forex : down`.
   - Icône ✓ si la direction proposée est cohérente avec le régime, ⚠ sinon (le trade peut quand même passer le risk gate via une autre confluence).

7. **Actions** (pied de carte, hauteur 56 px)
   - Boutons : `[✓ Valider]` (emerald, primaire), `[✗ Rejeter]` (rose, secondaire), `[ℹ Détails]` (ouvre un drawer lateral avec le JSON complet d'analyse §4.3 et le news pulse associé).
   - Les boutons effectuent `fetch POST /validate/{id}` ou `/reject/{id}` ; confirmation inline par toast (pas de modal). Après validation, le bouton devient `✓ Validé il y a 2 min · trade T-001789` et l'autre bouton est désactivé.
   - **Synchronisation Telegram** : si l'opérateur valide côté Telegram (§14.6), la carte bascule automatiquement au prochain refresh — lecture de `trades.validated` dans SQLite.

#### 14.2.2 Maquette ASCII d'une carte

```
┌────────────────────────────────────────────────────────────────────────────┐
│ [LONG] EURUSD  forex · ichimoku_trend_following      P-0042 · 7 min · ✓    │
├────────────────────────────────────────────────────────────────────────────┤
│ Entry 1.0712   Stop 1.0758   TP1 1.0668   TP2 1.0612   R/R 2.2   Size 1.0% │
│ ├──┤─────●────────────●───────────●──────┤      stop -0.43 %  TP2 +0.93 %  │
├────────────────────────────────────────────────────────────────────────────┤
│  Composite -0.63 · Confidence 0.74                                         │
│  ichi ████████▌ -0.80   trend ██████▏ -0.60   mom ████▌ -0.45   vol ████ -0.40 │
├────────────────────────────────────────────────────────────────────────────┤
│  « Ichimoku bearish : prix sous le Kumo, Chikou confirme, Tenkan < Kijun. » │
├────────────────────────────────────────────────────────────────────────────┤
│  ● DXY strength    ● CPI US above expectations                             │
│  ⚠ FOMC dans 24h                                                           │
├────────────────────────────────────────────────────────────────────────────┤
│  Régime : risk_off (72 %) · Volatilité : mid · Trend forex : down   ✓      │
├────────────────────────────────────────────────────────────────────────────┤
│  [ ✓ Valider ]    [ ✗ Rejeter ]    [ ℹ Détails ]                            │
└────────────────────────────────────────────────────────────────────────────┘
```

#### 14.2.3 État vide

Si aucune proposition n'a passé le risk gate, afficher un encart centré :

> **Aucune opportunité ce cycle — rester en cash est aussi une décision.**
>
> Top 3 raisons de rejet agrégées sur la fenêtre 24 h :
> • `ichimoku_in_kumo` — 7 rejets
> • `rr_below_min` (< 1.8) — 4 rejets
> • `avoid_window:fomc` — 3 rejets

Les raisons proviennent de `RiskDecision.reasons` (§8.4) consolidées sur 24 h.

#### 14.2.4 Responsive mobile

- Cartes en 1 colonne pleine largeur.
- Les 4 mini-barres du composite deviennent verticales (sparkbars).
- La règle graphique stop/entry/TP passe sous les niveaux texte.
- Boutons d'action collants en bas de carte (`sticky bottom-0` dans le drawer détails).

---

### 14.3 Panel Coûts — Tokens LLM & APIs de données

Ce panel a un double objectif : (a) faire respecter la règle d'or **« gagner de l'argent, pas en dépenser »** (§2.3) et (b) détecter les sources de données qui coûtent cher en quota, en latence ou en erreurs.

#### 14.3.1 KPIs de tête

4 tuiles compactes en haut du panel. Chaque tuile a un sparkline 30 jours en arrière-plan très atténué.

| Tuile | Valeur | Sous-texte | Seuil d'alerte |
|---|---|---|---|
| **Tokens aujourd'hui** | `18 420 / 50 000` | `in : 12 100 · out : 6 320` | orange ≥ 80 %, rouge ≥ 95 % |
| **Coût mensuel LLM** | `$ 4.82 / $ 15.00` | `32 % du budget · J+17/30` | orange ≥ 70 %, rouge ≥ 90 % |
| **Appels APIs 24 h** | `1 247` | `cache-hit : 68 %` | rouge si taux d'erreur > 5 % |
| **Latence p95 data** | `412 ms` | `max : 2.1 s (finnhub)` | rouge > 2 s |

#### 14.3.2 Décomposition par agent LLM

Tableau + barres empilées horizontales (Chart.js `horizontalBar` empilé, proportions mensuelles).

| Agent | Appels 24 h | Tokens (in / out) | Coût 24 h | Coût mois | Modèle | % budget mois |
|---|---:|---:|---:|---:|---|---:|
| TechnicalAnalystAgent | 12 | 3 200 / 2 400 | $ 0.046 | $ 1.32 | `sonnet-4-6` | 28 % |
| NewsAnalystAgent | 4 | 4 800 / 1 200 | $ 0.032 | $ 0.88 | `sonnet-4-6` | 19 % |
| SelfImprove weekly | 0 | — | — | $ 2.10 | `opus-4-7` | 45 % |
| Architecture review | 0 | — | — | $ 0.52 | `opus-4-7` | 11 % |
| **Total** | **16** | **8 000 / 3 600** | **$ 0.078** | **$ 4.82** | — | **100 %** |

Note sous le tableau : « Les cycles automatiques utilisent exclusivement `sonnet-4-6`. `opus-4-7` est réservé à self-improve hebdomadaire et à la revue d'architecture mensuelle (cf. §2.3). »

#### 14.3.3 Tarifs de référence (versionnés)

Les tarifs ne sont pas codés en dur. Ils vivent dans un fichier YAML versionné, relu au démarrage et invalidé au-delà de 90 jours.

```yaml
# config/pricing.yaml — relu au démarrage ; warning si périmé > 90 j
models:
  claude-sonnet-4-6:
    input_per_mtok_usd:  3.00
    output_per_mtok_usd: 15.00
  claude-opus-4-7:
    input_per_mtok_usd:  15.00
    output_per_mtok_usd: 75.00
  claude-haiku-4-5:
    input_per_mtok_usd:  1.00
    output_per_mtok_usd: 5.00

apis:
  finnhub:
    free_quota_per_min: 60
    cost_per_call_usd:  0.0            # plan gratuit
  stooq:             { cost_per_call_usd: 0.0, notes: "CSV gratuit, pas de clé — primary equity Euronext" }
  boursorama_scrape: { cost_per_call_usd: 0.0, notes: "scraping pages cours Euronext — fallback intraday, coût = infra" }

last_updated: "2026-04-17"
source: "https://docs.claude.com/en/docs/about-claude/models"
```

Au démarrage, un log `WARN` est émis si `last_updated > today - 90 j`, et un badge « tarif à vérifier » apparaît dans le dashboard.

#### 14.3.4 Décomposition par source de données

Tableau de santé des APIs. Une ligne par source configurée dans `config/sources.yaml` (§7.1).

| Source | Type | Appels 24 h | Cache-hit | Latence p50 / p95 | Erreurs | Quota | Coût $ | État |
|---|---|---:|---:|---:|---:|---:|---:|:---:|
| stooq (CSV) | prix | 324 | 85 % | 410 / 1 200 ms | 0.6 % | — | — | 🟢 |
| boursorama (scrap.) | prix | 482 | 71 % | 340 / 980 ms | 0.4 % | — | — | 🟢 |
| finnhub | prix | 45 | 0 % | 620 / 2 100 ms | 0 % | 45 / 60 min | — | 🟠 |
| investing_com_calendar | macro | 12 | 95 % | 410 / 890 ms | 0 % | — | — | 🟢 |
| reuters_rss | news | 26 | 100 % | 90 / 210 ms | 0 % | — | — | 🟢 |
| coindesk_rss | news | 18 | 100 % | 110 / 240 ms | 0 % | — | — | 🟢 |

Règles d'état :

- 🟢 **Nominal** : erreurs ≤ 2 %, latence p95 ≤ 1.5 s, quota ≤ 70 %.
- 🟠 **Dégradé** : un seuil franchi (erreurs > 2 %, p95 > 1.5 s, quota > 70 %). La source reste utilisée mais le `DataFetcher` privilégie la suivante si disponible (§7.2).
- 🔴 **Coupée** : erreurs > 10 % sur 1 h **ou** quota atteint. `DataFetcher` bascule automatiquement sur le fallback. Le circuit ré-essaie au bout de 15 min.

#### 14.3.5 Tendance 30 jours

Graphe en lignes superposées (Chart.js, 2 axes Y) :

- ligne 1 : tokens/jour (axe gauche).
- ligne 2 : coût cumulatif du mois glissant (axe droit, $ USD).
- ligne 3 : appels API/jour (axe gauche).
- barres verticales translucides pour les jours où une alerte budget a été levée.
- marqueurs ponctuels pour les runs `self_improve_weekly` et `architecture_review_monthly`.

Objectif : repérer les dérives (self-improve mal configuré qui triple la conso, RSS qui boucle, agent qui génère des prompts trop longs).

#### 14.3.6 Top consommateurs du mois

Liste courte (5 lignes max), extraite de `llm_usage` (§14.5.1) triée par `cost_usd` décroissant.

```
1.  P-0041 · EURUSD       · 2 400 tok · $ 0.036   — narrative longue, contexte FOMC
2.  news_pulse 2026-04-14 · 3 100 tok · $ 0.042   — 28 articles filtrés
3.  self_improve 2026-04-13 · 42 000 tok · $ 1.55 — revue 90 trades
4.  P-0039 · BTCUSD       · 1 900 tok · $ 0.028
5.  news_pulse 2026-04-12 · 2 700 tok · $ 0.037
```

Permet de repérer les requêtes aberrantes (prompts trop longs, sorties non tronquées, boucles LLM).

#### 14.3.7 Alertes & garde-fous

Le panel réutilise exactement la logique de §11.4 (`TokenBudgetGate`) avec deux ajouts côté affichage :

- **Soft cap à 80 %** du budget journalier → bannière ambrée : « Prochaines requêtes LLM suspendues jusqu'à 00:00 UTC si le seuil journalier est atteint. »
- **Forecast fin de mois** : `forecast = cost_month_to_date / days_elapsed × days_in_month`. Affiché en rouge si `forecast > max_monthly_cost_usd`.
- **Kill-switch** armé automatiquement si `max_monthly_cost_usd` dépassé (déjà dans §11.4). Le bandeau d'en-tête du dashboard reflète ce statut en temps réel.

---

### 14.4 Portefeuille, régime, signaux & stratégies

Les autres blocs existent déjà conceptuellement dans la V0 de cette section ; on formalise ici leurs sources de données.

#### 14.4.1 Bloc Portefeuille

- Équité fictive, variation 24 h / 7 j / 30 j (source : `trades`, `PortfolioState`).
- Sparkline équité 30 j (Chart.js, 1 trait, pas d'axes).
- Positions ouvertes : 1 ligne par position avec P&L non réalisé, distance au stop, au TP.
- Donut exposition par classe (forex / crypto / equity).

#### 14.4.2 Bloc Risque

- Drawdown courant (`current_dd_pct`) vs `max_total_dd_pct` (§11.2).
- Perte journalière vs `max_daily_loss_pct_equity`.
- Table de corrélations des positions ouvertes (`max_correlated_positions`).
- État des circuit breakers par stratégie (ouvert / fermé / données insuffisantes).

#### 14.4.3 Heatmap signaux

Grille actifs (lignes) × composants score (colonnes : `composite`, `ichimoku`, `trend`, `momentum`, `volume`). Couleur divergente rouge ↔ vert centrée sur 0. Permet de repérer les confluences en un coup d'œil. Données : `TechnicalAnalysis.composite.components` pour chaque actif du scan.

#### 14.4.4 News & catalyseurs, perf stratégies, notes

- **Timeline 72 h** alimentée par `NewsPulse.catalysts` (§8.3). Chaque évènement a un type (FOMC, CPI, earnings, …) et un asset tagué.
- **Perf stratégies** : lecture de `performance_metrics` (§10.2).
- **Notes de l'agent** : champ libre de 2–5 lignes synthétisant « ce que j'observe, ce dont je doute, ce que je surveille ». Généré en fin de cycle par `TechnicalAnalystAgent` (coût déjà comptabilisé dans 14.3.2).

---

### 14.5 Data model & pipeline de build

#### 14.5.1 Nouvelles tables SQLite (complément §10.2)

Deux tables analytiques, indépendantes du flux de décision. Écriture fail-open : si la télémétrie échoue, on log et on continue ; le bot ne doit jamais s'arrêter à cause d'un souci de mesure.

```sql
-- Utilisation LLM (une ligne par appel API Anthropic)
CREATE TABLE llm_usage (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,                  -- ISO 8601 UTC
    agent       TEXT NOT NULL,                  -- technical_analyst | news_analyst | self_improve | ...
    model       TEXT NOT NULL,                  -- claude-sonnet-4-6 | claude-opus-4-7 | ...
    tokens_in   INTEGER NOT NULL,
    tokens_out  INTEGER NOT NULL,
    cost_usd    REAL NOT NULL,                  -- calculé via config/pricing.yaml à l'insertion
    session_id  TEXT,                           -- lien avec un cycle
    request_ref TEXT,                           -- ex: "P-0042", "news_pulse:2026-04-17"
    duration_ms INTEGER,
    error       TEXT                            -- NULL si succès
);
CREATE INDEX idx_llm_usage_ts    ON llm_usage(ts);
CREATE INDEX idx_llm_usage_agent ON llm_usage(agent);

-- Utilisation des APIs de données (une ligne par appel réel — pas par cache-hit)
CREATE TABLE api_usage (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    source      TEXT NOT NULL,                  -- stooq | boursorama_scrape | finnhub | oanda | ccxt | ...
    kind        TEXT NOT NULL,                  -- prix | macro | news
    asset       TEXT,                           -- optionnel
    endpoint    TEXT,
    status      INTEGER,                        -- 200, 429, 500
    latency_ms  INTEGER,
    cached      INTEGER NOT NULL DEFAULT 0,
    cost_usd    REAL DEFAULT 0.0                -- 0 pour RSS / scraping, > 0 pour APIs payantes
);
CREATE INDEX idx_api_usage_ts     ON api_usage(ts);
CREATE INDEX idx_api_usage_source ON api_usage(source);
```

**Rétention** : 180 jours ; `scripts/purge_telemetry.py` est appelé par la tâche de consolidation nocturne (§15.1).

#### 14.5.2 Traceurs automatiques (decorators)

```python
# src/telemetry/llm_tracker.py

def track_llm(agent: str, request_ref_fn=lambda *a, **kw: None):
    """
    Décorateur appliqué à toute méthode qui appelle l'API Anthropic.
    Écrit une ligne dans llm_usage. Fail-open : n'interrompt jamais l'appelant.
    """
    def decorator(fn):
        @wraps(fn)
        def wrapper(self, *args, **kwargs):
            t0 = time.monotonic()
            resp, error = None, None
            try:
                resp = fn(self, *args, **kwargs)
                return resp
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                raise
            finally:
                try:
                    usage = getattr(resp, "usage", None)
                    tokens_in  = getattr(usage, "input_tokens", 0)
                    tokens_out = getattr(usage, "output_tokens", 0)
                    model      = getattr(resp, "model", self.model)
                    cost       = pricing.cost_usd(model, tokens_in, tokens_out)
                    db.insert_llm_usage(
                        ts=utcnow_iso(), agent=agent, model=model,
                        tokens_in=tokens_in, tokens_out=tokens_out,
                        cost_usd=cost, session_id=self.session_id,
                        request_ref=request_ref_fn(self, *args, **kwargs),
                        duration_ms=int((time.monotonic() - t0) * 1000),
                        error=error,
                    )
                except Exception as telemetry_err:
                    log.warning(f"llm telemetry failed: {telemetry_err}")
        return wrapper
    return decorator
```

```python
# src/telemetry/api_tracker.py — équivalent pour DataFetcher
def track_api(source: str, kind: str): ...
```

Ces décorateurs sont posés sur les méthodes LLM des agents (`_llm_summarize`, `llm_interpret_context`, …) et sur les clients de `src/data/fetcher.py`. **Aucune logique métier ne dépend de leur écriture**.

#### 14.5.3 Contexte Jinja rendu au template

```python
# src/dashboards/context.py

@dataclass
class OpportunityCard:
    proposal:       TradeProposal
    analysis:       TechnicalAnalysis
    regime_aligned: bool
    validation_url: str      # "/validate/P-0042"
    rejection_url: str
    validated_at:   datetime | None   # None tant que non validé
    trade_id:       str | None        # rempli après validation

@dataclass
class AgentCostRow:
    agent: str
    calls_24h: int
    tokens_in_24h: int
    tokens_out_24h: int
    cost_24h_usd: float
    cost_month_usd: float
    model: str
    pct_month_budget: float

@dataclass
class ApiSourceRow:
    source: str
    kind: str
    calls_24h: int
    cache_hit_pct: float
    latency_p50_ms: int
    latency_p95_ms: int
    error_rate_pct: float
    quota_used_pct: float | None
    cost_24h_usd: float
    state: Literal["green", "amber", "red"]

@dataclass
class CostPanel:
    tokens_today: int
    tokens_daily_budget: int
    cost_month_usd: float
    cost_month_budget_usd: float
    forecast_month_usd: float
    by_agent: list[AgentCostRow]
    by_model: list[ModelCostRow]
    by_api_source: list[ApiSourceRow]
    trend_30d: list[DailyCostPoint]          # alimente Chart.js
    top_consumers: list[ConsumerRow]
    pricing_last_updated: date
    alerts: list[CostAlert]
    computed_at: str                         # ISO-8601 UTC, cf. §14.5.5 freshness
    source_data_lag_seconds: float           # +inf si llm_usage vide (bootstrap)

@dataclass
class DashboardContext:
    session: str
    timestamp: datetime
    regime: RegimeState
    portfolio: PortfolioState
    opportunities: list[OpportunityCard]
    cost_panel: CostPanel
    heatmap: HeatmapData
    news_timeline: list[NewsEvent]
    strategy_perf: list[StrategyPerfRow]
    agent_notes: str
    kill_switch_active: bool
```

#### 14.5.4 Builder — `HTMLDashboardBuilder` mis à jour

```python
# src/dashboards/html_builder.py

class HTMLDashboardBuilder:

    def __init__(
        self,
        db: sqlite3.Connection,
        env: Environment,
        pricing: ModelPricing,
        cost_repo: "CostRepository",
    ):
        self.db = db
        self.jinja_env = env
        self.pricing = pricing
        self.cost_repo = cost_repo

    def build(
        self,
        analyses:  list[TechnicalAnalysis],
        proposals: list[TradeProposal],
        portfolio: PortfolioState,
        regime:    RegimeState,
        session:   str,
    ) -> Path:
        ctx = DashboardContext(
            session=session,
            timestamp=datetime.utcnow(),
            regime=regime,
            portfolio=portfolio,
            opportunities=[self._to_card(p, analyses, regime) for p in proposals],
            cost_panel=self.cost_repo.build_panel(),
            heatmap=self._build_heatmap(analyses),
            news_timeline=self._news_timeline(),
            strategy_perf=self._strategy_perf(),
            agent_notes=self._agent_notes(session),
            kill_switch_active=kill_switch.is_active(),
        )
        html = self.jinja_env.get_template("daily.html.jinja2").render(ctx=ctx)
        out = DASHBOARDS_DIR / ctx.timestamp.strftime("%Y-%m-%d") / f"{session}.html"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(html, encoding="utf-8")
        return out
```

#### 14.5.5 `CostRepository` — agrégats sur `llm_usage` et `api_usage`

**Schéma d'entrée attendu** (rappel §14.5.1) :
- `llm_usage(timestamp, agent, model, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, cost_usd, cycle_id, session_id)`
- `api_usage(timestamp, source, endpoint, success, latency_ms, http_status, error_code, cycle_id)`

**Mode d'exécution** : `build_panel()` est appelé **en pull** à la génération du dashboard (fin de cycle, cf. §8.7.3 étape 7) et exposé aussi via `GET /costs.json` (§14.5.6). Pas de polling ni d'événements : le dashboard est régénéré à chaque cycle (§14 intro) donc la fraîcheur max = durée d'un cycle (≈ 5 min typique, ≤ 1h garanti).

**Freshness garantie** : chaque valeur de `CostPanel` est étiquetée `computed_at` (UTC ISO-8601) + `source_data_lag_seconds = now - max(timestamp) in table`. Le template affiche un badge "stale" si lag > 3600s.

##### Requêtes SQL canoniques

Toutes les requêtes utilisent des **paramètres nommés** (protection contre injection, même si la DB est locale) et des **index** définis en §10.2 (`idx_llm_usage_ts`, `idx_api_usage_ts_source`).

```sql
-- Q1. Tokens consommés aujourd'hui (UTC)
SELECT
  SUM(input_tokens)        AS input,
  SUM(output_tokens)       AS output,
  SUM(cache_read_tokens)   AS cache_read,
  SUM(cache_write_tokens)  AS cache_write
FROM llm_usage
WHERE timestamp >= :today_utc_start
  AND timestamp <  :tomorrow_utc_start;

-- Q2. Coût mois courant (MTD)
SELECT COALESCE(SUM(cost_usd), 0.0) AS cost_mtd
FROM llm_usage
WHERE timestamp >= :month_start
  AND timestamp <  :now;

-- Q3. Breakdown par agent (pour pie chart + top consumers)
SELECT
  agent,
  SUM(input_tokens + output_tokens) AS tokens_total,
  SUM(cost_usd)                     AS cost_usd,
  COUNT(DISTINCT cycle_id)          AS cycles
FROM llm_usage
WHERE timestamp >= :window_start
GROUP BY agent
ORDER BY cost_usd DESC;

-- Q4. Breakdown par modèle
SELECT model,
       SUM(input_tokens)       AS input,
       SUM(output_tokens)      AS output,
       SUM(cache_read_tokens)  AS cache_read,
       SUM(cost_usd)           AS cost_usd
FROM llm_usage
WHERE timestamp >= :window_start
GROUP BY model
ORDER BY cost_usd DESC;

-- Q5. Santé des APIs data sources (panel §14.3.4)
SELECT
  source,
  COUNT(*)                                          AS calls,
  100.0 * SUM(success) / NULLIF(COUNT(*), 0)        AS success_rate_pct,
  AVG(latency_ms)                                   AS avg_latency_ms,
  MAX(timestamp)                                    AS last_call,
  SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END)      AS errors
FROM api_usage
WHERE timestamp >= :window_24h
GROUP BY source
ORDER BY source;

-- Q6. Tendance 30j (sparkline) — 1 ligne par jour UTC
SELECT
  DATE(timestamp)      AS day,
  SUM(cost_usd)        AS cost_usd,
  SUM(input_tokens + output_tokens) AS tokens
FROM llm_usage
WHERE timestamp >= :d30_start
GROUP BY DATE(timestamp)
ORDER BY day;

-- Q7. Top consumers — jointure llm_usage + cycles pour contexte
SELECT
  l.cycle_id,
  l.agent,
  l.model,
  SUM(l.cost_usd)                          AS cost_usd,
  SUM(l.input_tokens + l.output_tokens)    AS tokens,
  c.session_name,
  c.started_at
FROM llm_usage l
LEFT JOIN cycles c ON c.id = l.cycle_id
WHERE l.timestamp >= :window_start
GROUP BY l.cycle_id, l.agent, l.model
ORDER BY cost_usd DESC
LIMIT :n;
```

##### Code de référence

```python
# src/dashboards/cost_repo.py
from datetime import datetime, timedelta, timezone
from calendar import monthrange

class CostRepository:
    """
    Agrège llm_usage et api_usage sur les fenêtres (24 h, mois, 30 j) et
    applique le pricing versionné de config/pricing.yaml.

    Mode : pull-only, appelé à chaque génération de dashboard (fin de cycle,
    §8.7.3) + endpoint HTTP /costs.json (§14.5.6).
    Fraîcheur : max = durée d'un cycle ; badge "stale" dans le template si
    source_data_lag_seconds > 3600.
    """

    def __init__(self, db: sqlite3.Connection, pricing: ModelPricing, limits: LLMLimits):
        self.db, self.pricing, self.limits = db, pricing, limits

    def build_panel(self) -> CostPanel:
        now = datetime.now(timezone.utc)
        windows = self._windows(now)

        tokens_today = self._sum_tokens_today(windows)
        cost_month   = self._sum_cost_month(windows)
        by_agent     = self._breakdown_by_agent(windows)
        by_model     = self._breakdown_by_model(windows)
        by_api       = self._api_health(windows)
        trend        = self._trend_30d(windows)
        top          = self._top_consumers(windows, n=5)
        lag_seconds  = self._data_lag(now)
        alerts       = self._check_alerts(tokens_today, cost_month)

        return CostPanel(
            tokens_today=tokens_today,
            tokens_daily_budget=self.limits.max_daily_tokens,
            cost_month_usd=cost_month,
            cost_month_budget_usd=self.limits.max_monthly_cost_usd,
            forecast_month_usd=self._forecast(cost_month, now),
            by_agent=by_agent,
            by_model=by_model,
            by_api_source=by_api,
            trend_30d=trend,
            top_consumers=top,
            pricing_last_updated=self.pricing.last_updated,
            alerts=alerts,
            computed_at=now.isoformat(),
            source_data_lag_seconds=lag_seconds,
        )

    def _windows(self, now: datetime) -> dict:
        today_utc = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return {
            "today_utc_start":   today_utc.isoformat(),
            "tomorrow_utc_start": (today_utc + timedelta(days=1)).isoformat(),
            "month_start":       now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat(),
            "d30_start":         (now - timedelta(days=30)).isoformat(),
            "window_24h":        (now - timedelta(hours=24)).isoformat(),
            "window_start":      (now - timedelta(days=30)).isoformat(),
            "now":               now.isoformat(),
        }

    def _data_lag(self, now: datetime) -> float:
        row = self.db.execute(
            "SELECT MAX(timestamp) FROM llm_usage"
        ).fetchone()
        if row is None or row[0] is None:
            return float("inf")
        last = datetime.fromisoformat(row[0]).replace(tzinfo=timezone.utc)
        return (now - last).total_seconds()

    # Q1 -> Q7 = méthodes internes mappant 1-1 aux requêtes SQL ci-dessus.
    # Aucune sont exécutées en boucle serrée : 1 appel par cycle = coût
    # négligeable (SQLite local, <50ms pour ~100k lignes).

    def _forecast(self, cost_mtd: float, now: datetime) -> float:
        days_elapsed  = now.day
        days_in_month = monthrange(now.year, now.month)[1]
        return cost_mtd / max(days_elapsed, 1) * days_in_month
```

##### Invariants & garanties

- **Idempotent** : `build_panel()` est une lecture pure, pas d'effet de bord SQL.
- **Cohérence** : toutes les requêtes utilisent le même `now` snapshot (pas de drift entre Q1 et Q7).
- **Dégradation** : si `llm_usage` est vide (bootstrap), les champs numériques = 0, `source_data_lag_seconds = +inf`, badge "no_data" affiché.
- **Performance** : max ~100 k lignes/an typique ; toutes les queries < 50 ms sur SQLite WAL (§10.1). Pas de pagination nécessaire.

#### 14.5.6 Endpoints de validation (FastAPI minimal)

```python
# src/dashboards/api.py — monté derrière Caddy + basic_auth (§17.4)

app = FastAPI()

@app.post("/validate/{proposal_id}")
def validate(proposal_id: str, user: str = Depends(basic_auth)) -> dict:
    trade = simulator.confirm_validation(proposal_id, user=user)
    telegram.send_alert(f"✓ {proposal_id} validé → trade {trade.id}", level="info")
    return {"status": "validated", "trade_id": trade.id}

@app.post("/reject/{proposal_id}")
def reject(proposal_id: str, user: str = Depends(basic_auth)) -> dict:
    simulator.reject(proposal_id, user=user)
    return {"status": "rejected"}

@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True, "db": db_ok(), "kill_switch": kill_switch.state()}

@app.get("/costs.json")
def costs() -> dict:
    """Export JSON des agrégats du panel — consommé aussi par /costs Telegram."""
    return asdict(cost_repo.build_panel())
```

#### 14.5.7 Assets front-end

- **Tailwind** via Play CDN pour un rendu single-file (option build-time purge si on empaquette).
- **Chart.js** pour les sparklines, les courbes de tendance 30 j et la heatmap.
- **Heroicons** inline SVG pour les badges et icônes — zéro dépendance runtime.
- Pas de framework JS. Les boutons d'action utilisent `fetch` natif plus un helper `static/actions.js` ≤ 40 lignes, servi depuis le même conteneur FastAPI.

---

### 14.6 Notifications Telegram

Le dashboard HTML et Telegram sont **deux surfaces synchronisées** sur la même source de vérité (SQLite). Toute validation effectuée dans une surface est reflétée dans l'autre au prochain refresh.

```python
# src/notifications/telegram.py

class TelegramNotifier:

    def send_cycle_summary(self, proposals: list[TradeProposal], regime: RegimeState) -> None:
        """Message court (≤ 800 chars) envoyé à chaque cycle."""
        ...

    def send_proposal_card(self, proposal: TradeProposal) -> None:
        """
        Carte inline avec boutons :
        [✅ Valider] [❌ Rejeter] [ℹ️ Détails]
        La validation met à jour SimulatedTrade.validated_by_human = True
        """
        ...

    def send_alert(self, message: str, level: str = "warning") -> None:
        """Alerte kill-switch, circuit breaker, anomalie données, dépassement budget."""
        ...

    def send_daily_recap(self, pnl: PnLSummary) -> None:
        """Récap P&L simulé du jour, top/flop, coûts LLM du jour."""
        ...

    def send_cost_digest(self, panel: CostPanel) -> None:
        """Envoyé par /costs ou déclenché automatiquement si alerte budget."""
        ...
```

**Commandes Telegram disponibles** :

| Commande | Action |
|---|---|
| `/status` | Statut portefeuille simulé, positions ouvertes |
| `/proposals` | Liste les propositions en attente de validation |
| `/validate P-0042` | Valide manuellement une proposition |
| `/reject P-0042` | Rejette une proposition |
| `/kill` | Arme le kill-switch |
| `/resume` | Désarme le kill-switch |
| `/report` | Génère et envoie le dashboard du jour |
| `/metrics` | Métriques de performance simulées |
| `/costs` | Digest coûts : tokens jour/mois, coût $, top 3 agents, APIs dégradées |
| `/budget` | Budget restant du mois, forecast fin de mois, délai avant reset |
| `/apis` | État des sources de données (🟢 / 🟠 / 🔴) avec dernière erreur observée |

---

## 15. Orchestration & Schedules

### 15.1 Schedules

Trois types de pipelines coexistent :

- **Cycles planifiés** (cron) : analyse complète de l'univers aux horaires fixes.
- **Cycles ad-hoc** : déclenchés à la volée par un pic d'impact news (§6.7) ou par une commande Telegram.
- **Tâches de maintenance** : P&L update, mémoire, self-improve, revue d'archi.

```yaml
# config/schedules.yaml
#
# Règle — pas de rapports le weekend pour les actions (Euronext / US) ni
# pour le Forex : ces marchés sont fermés du vendredi ~22:00 UTC au
# dimanche ~22:00 UTC. Les cycles `full_cycle` / `full_cycle_with_journal`
# sont donc contraints à `* * * 1-5` (lundi → vendredi). Seul le pipeline
# crypto tourne 24/7 (cron sans restriction jour de semaine). Cf. §1.1.
cycles:
  # --- Actions + Forex — jours ouvrés UNIQUEMENT (lun-ven) --------------
  - name: open_europe
    cron: "0 7 * * 1-5"                  # pas de cycle samedi/dimanche
    pipeline: full_cycle
    markets: [forex, equity]             # Euronext Paris en priorité (cf. §1.1)

  - name: close_us
    cron: "0 21 * * 1-5"                 # pas de cycle samedi/dimanche
    pipeline: full_cycle_with_journal
    markets: [forex, equity]

  # --- Crypto — 24/7 (marché toujours ouvert) --------------------------
  - name: crypto_6h
    cron: "0 */6 * * *"                  # tourne aussi le weekend
    pipeline: crypto_quick_scan
    markets: [crypto]

news_watcher:
  enabled: true
  poll_seconds: 60                     # scan des flux RSS/APIs toutes les 60 s
  impact_threshold_candidate: 0.60     # ajoute aux candidats du prochain cycle
  impact_threshold_adhoc:     0.80     # déclenche un cycle ad-hoc immédiat
  adhoc_cooldown_minutes:     15       # anti-rafale : 1 cycle ad-hoc max / 15 min
  adhoc_pipeline: focused_cycle        # pipeline léger ciblé sur l'actif concerné
  max_adhoc_per_day:          6        # plafond quotidien

maintenance:
  - name: pnl_update
    cron: "*/30 * * * *"               # Mise à jour P&L toutes les 30 min
    pipeline: update_open_trades

  - name: memory_consolidate
    cron: "30 2 * * *"
    pipeline: memory_consolidate

  - name: self_improve
    cron: "0 22 * * 0"
    pipeline: self_improve_weekly

  - name: architecture_review
    cron: "0 18 1 * *"
    pipeline: architecture_review_monthly

  - name: purge_telemetry
    cron: "15 2 * * *"                 # purge llm_usage / api_usage > 180j
    pipeline: purge_telemetry
```

Le pipeline `focused_cycle` saute le `MarketScanner` et n'analyse que l'actif fourni + ses corrélés connus (ex : si BTC/USDT → ajouter ETH/USDT ; si EURUSD → ajouter DXY comme contexte uniquement). Cela divise par 10 le coût tokens d'un cycle ad-hoc.

### 15.2 Diagramme de flux — `full_cycle` + trigger ad-hoc news

```mermaid
flowchart TD
    subgraph NW[News Watcher (poll 60s)]
        direction TB
        NA[Scrape RSS + Binance + TE news] --> NB[NER filter + NewsImpact.score]
        NB --> NC{impact ≥ 0.80?}
        NC -- Oui --> ND[Trigger focused_cycle adhoc<br/>si cooldown OK]
        NC -- Non --> NE{impact ≥ 0.60?}
        NE -- Oui --> NF[Push NewsImpact dans pulse<br/>→ injecté au prochain cycle]
        NE -- Non --> NG[Log seul]
    end

    A[Démarrage cycle] --> B{Kill-switch?}
    B -- Oui --> Z[Abort + log + Telegram]
    B -- Non --> C[RegimeDetector.detect]
    C --> D[DataFetcher.fetch_universe]
    D --> E{Qualité données OK?}
    E -- Non --> Z
    E -- Oui --> F[MarketScanner.scan top 20]
    F --> F2[+ NewsImpact.assets avec impact ≥ 0.60]
    F2 --> G[NewsAnalystAgent.analyze]
    G --> H[TechnicalAnalystAgent.analyze × N]
    H --> I{Score ≥ 0.6?}
    I -- Non --> M[Filtrage / ajout watchlist]
    I -- Oui --> J[RiskManagerAgent.evaluate]
    J --> K{Approved?}
    K -- Non --> M
    K -- Oui --> L[SimulatorAgent.record_proposal]
    L --> N[ReporterAgent.generate_daily_report]
    N --> O[Telegram — envoi propositions]
    O --> P[MemoryDB.save_observations]
    P --> Q[MarkdownExporter.refresh]
    Q --> R[Fin cycle — log JSON]

    ND -.déclenche.-> A
```

---

## 16. Tests & CI/CD

### 16.1 Tests unitaires

```python
# tests/unit/test_ichimoku.py

def test_ichimoku_bullish_above_kumo():
    df = load_fixture("eurusd_bullish_200bars.parquet")
    result = compute_ichimoku(df)
    assert result.score > 0.5
    assert not (result.senkou_a.iloc[-27] == result.senkou_b.iloc[-27])  # Kumo non plat

def test_ichimoku_returns_zero_in_kumo():
    df = load_fixture("spx_sideways_kumo_entry.parquet")
    result = compute_ichimoku(df)
    assert result.score == 0.0

def test_composite_score_bounded():
    df = load_fixture("btcusd_volatile.parquet")
    score = compute_composite_score(df, default_config())
    assert -1.0 <= score.value <= 1.0
    assert 0.0 <= score.confidence <= 1.0
```

```python
# tests/unit/test_risk_gate.py

def test_risk_gate_rejects_in_kumo():
    proposal = make_proposal(ichimoku_score=0.0)  # prix dans le Kumo
    decision = RiskManagerAgent().evaluate(proposal, empty_portfolio(), neutral_regime())
    assert decision.approved is False
    assert "ichimoku" in decision.reasons[0].lower()

def test_risk_gate_rejects_on_kill_switch(tmp_path, monkeypatch):
    monkeypatch.setenv("KILL_FILE_PATH", str(tmp_path / "KILL"))
    (tmp_path / "KILL").write_text("test")
    proposal = make_strong_proposal()
    decision = RiskManagerAgent().evaluate(proposal, empty_portfolio(), neutral_regime())
    assert decision.approved is False

def test_circuit_breaker_trips_on_excess_dd():
    db = build_db_with_strategy_dd(strategy="ichimoku_trend_following", dd_7d=0.08, dd_median=0.03)
    state = CircuitBreaker().check_strategy("ichimoku_trend_following", db)
    assert state == CircuitState.TRIPPED
```

### 16.2 Tests d'intégration

```python
# tests/integration/test_pipeline_e2e.py

def test_full_cycle_produces_dashboard(mock_fetcher, mock_telegram, tmp_path):
    """Test end-to-end : cycle complet avec données mockées → dashboard généré."""
    orchestrator = Orchestrator(config=test_config(), output_dir=tmp_path)
    result = orchestrator.run_cycle("test_session")

    assert result.status == "success"
    assert (tmp_path / "dashboards").exists()
    html_files = list((tmp_path / "dashboards").glob("**/*.html"))
    assert len(html_files) >= 1

def test_data_fetcher_fallback(mock_stooq_down, mock_boursorama_up):
    """Si Stooq échoue sur une equity Euronext, fallback sur Boursorama (scrape)."""
    df = DataFetcher().fetch_ohlcv("RUI.PA", "1d", 200)
    assert df is not None
    assert df.attrs["source"] == "boursorama_scrape"
```

### 16.3 GitHub Actions CI

```yaml
# .github/workflows/ci.yml

name: CI

on:
  push:
    branches: [main, "self-improve/**"]
  pull_request:
    branches: [main]

jobs:
  lint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v3
      - run: uv sync
      - run: uv run ruff check src/ tests/
      - run: uv run black --check src/ tests/
      - run: uv run mypy src/

  security:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v3
      - run: uv sync
      - run: uv run bandit -r src/ -ll
      - name: Secrets scan
        uses: trufflesecurity/trufflehog@main
        with:
          path: ./
          base: ${{ github.event.repository.default_branch }}

  test:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        python-version: ["3.11", "3.12"]
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v3
      - run: uv sync
      - run: uv run pytest tests/unit/ -v --cov=src --cov-report=xml
      - run: uv run pytest tests/integration/ -v -m "not slow"

  regression:
    runs-on: ubuntu-latest
    if: contains(github.ref, 'self-improve/')
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v3
      - run: uv sync
      - name: Regression test — walk-forward baseline
        run: uv run python -m src.backtest.regression_check --branch=${{ github.ref }}
```

---

## 17. Déploiement

### 17.1 Infrastructure en place

Le VPS héberge déjà **Caddy sur l'hôte** (pas dans un container) avec des
blocs reverse_proxy vers différents services Docker :

```caddy
openclaw.easy-flow.site {
    reverse_proxy 172.18.0.3:18789   # exemple existant — autre projet
}
```

Le trading bot réutilise ce Caddy existant en ajoutant un bloc dédié. Aucun
container Caddy n'est créé dans `docker-compose.yml` du bot — séparation
claire : **Caddy = infrastructure hôte partagée**, **compose = un service
applicatif**.

### 17.2 Architecture des services

```
Internet
    │  HTTPS (443)
    ▼
┌─────────────────────────────────────────────┐
│     Caddy (sur l'HÔTE, systemd service)     │
│     TLS termination automatique (Let's E.)  │
│  openclaw-bot.easy-flow.site → 127.0.0.1:8080
└──────────────────────┬──────────────────────┘
                       │  HTTP loopback
                       ▼
┌─────────────────────────────────────────────┐
│   docker compose (projet trading-bot)        │
│   ┌───────────────────────────────────────┐  │
│   │ trading-bot  — scheduler + FastAPI    │  │
│   │   ports: 127.0.0.1:8080:8080          │  │
│   │   image: openclaw-trading-bot:<GIT>   │  │
│   └───────────────────────────────────────┘  │
└─────────────────────────────────────────────┘
```

**Rationale 127.0.0.1:8080** (pas IP container `172.x.y.z`) : l'IP container
change à chaque recréation et peut entrer en collision avec d'autres bridges
Docker. Le bind loopback est stable, auditable (`ss -tlnp`), et Caddy y
accède sans dépendre du réseau Docker.

### 17.3 docker-compose

La source de vérité est `docker-compose.yml` à la racine. Résumé des choix
opérationnels :

- **Compose V2** : pas de clé `version:` (obsolète) ; projet nommé via
  `name: openclaw-trading-bot` pour `docker compose ps / inspect`.
- **Un seul service** `trading-bot` — scheduler + FastAPI (dashboard) + webhook
  Telegram dans le même container (cf. §17.2). Pas de service `telegram-webhook`
  séparé (mutualisé pour éviter un second port et un second process).
- **Build local uniquement** : `pull_policy: never` et tag par `GIT_SHA` —
  rollback sur image précédente trivial via `deploy.sh --rollback`.
- **Port** : `127.0.0.1:8080:8080` — bind loopback, Caddy termine TLS.
- **Volumes** : `./data:/app/data` (persistant — SQLite, Excel, dashboards,
  `KILL`) et `./config:/app/config:ro` (lecture seule). Plus de bind-mount
  fichier `KILL` séparé (simplifié, cf. §11.1).
- **Filesystem racine read-only** + `no-new-privileges` + tmpfs pour `/tmp`,
  `/app/.cache`, `/app/.local`, `/app/.config` (libs tierces qui écrivent dans
  HOME, ex. platformdirs).
- **Ressources** : `mem_limit: 1024m`, `cpus: 1.5`, `stop_grace_period: 30s`
  (APScheduler + SQLite WAL ont besoin de temps pour fermer proprement).
- **Logs** : driver `json-file` avec rotation `max-size=10m`, `max-file=5` —
  complété par `deploy/logrotate.conf` pour les logs fichier applicatifs sous
  `data/logs/*.log`.
- **Réseau** : `networks.trading_net.external: false` pour la V1 (le premier
  `deploy.sh` crée le réseau). Passer à `external: true` une fois qu'OpenClaw
  le partage.
- **Healthcheck** : défini dans le `Dockerfile` (`curl /healthz`), pas
  redondamment dans `compose` — une seule source de vérité.

### 17.4 Caddyfile (ajout au Caddyfile existant de l'hôte)

La source de vérité est `deploy/Caddyfile.snippet`. Le bloc est
**délibérément minimal** pour s'aligner sur le style des autres blocs du
Caddyfile existant :

```caddy
openclaw-bot.easy-flow.site {
    reverse_proxy 127.0.0.1:8080
}
```

**Décisions explicites** :

- **Auth côté app, pas côté Caddy.** L'opérateur humain (§2.4) s'authentifie
  via `DASHBOARD_BASIC_AUTH_USER` / `DASHBOARD_BASIC_AUTH_PASS` dans `.env`
  (vérifié par `src/api/auth.py`). Un seul endroit pour changer le mot de
  passe, et le webhook Telegram (qui ne peut pas faire du basic auth) reste
  géré uniformément avec le reste de l'app.
- **Cible `127.0.0.1:8080`** (pas IP container). Stable, loopback, Caddy
  n'a pas besoin d'être sur le réseau Docker.
- **Pas de `handle /healthz` séparé.** L'endpoint reste public côté app —
  le reverse_proxy passe tout, Uptime Kuma tape `GET /healthz` directement.
- **Pas de logs applicatifs dans Caddy.** Les logs d'accès vont dans le
  journal systemd de Caddy (global) ; les logs applicatifs sont dans
  `data/logs/*.log` avec rotation `deploy/logrotate.conf`.

Pour ajouter headers de sécurité, rate-limit, ou logs JSON dédiés, voir
commentaires dans `deploy/Caddyfile.snippet` — ces ajouts sont optionnels
et alourdissent le bloc sans bénéfice immédiat sur un projet à un seul
utilisateur opérateur.

**Webhook Telegram** : protégé côté app via header
`X-Telegram-Bot-Api-Secret-Token` comparé à `TELEGRAM_WEBHOOK_SECRET`
(§14.6) ; aucune action requise côté Caddy.

### 17.5 Dockerfile

La source de vérité est le `Dockerfile` à la racine du projet — ce qui suit en
résume les choix clés. Tout changement ici doit être répercuté dans le fichier
réel (et inversement).

Points d'ancrage :

- **Base & stages** : `python:3.12-slim`, multi-stage `builder` (résout +
  installe les deps via `uv sync --frozen` dans `/opt/venv`, cf. §17.7) +
  `runtime` (user non-root, image slim).
- **Paramètres de build** : `OPENCLAW_VERSION` et `GIT_SHA` via `--build-arg`,
  embarqués en `LABEL` + `ENV` pour traçabilité (`OPENCLAW_VERSION`,
  `OPENCLAW_GIT_SHA`).
- **User non-root** : `bot` avec UID/GID fixes `10001:10001` (alignés avec
  `deploy.sh` pour le `chown` du volume `data/`).
- **PID 1 propre** : `tini` assure la propagation des signaux vers APScheduler
  et FastAPI au shutdown (`stop_grace_period: 30s`).
- **Dossiers persistants pré-créés** : `cache/`, `logs/`, `analyses/`,
  `simulation/`, `dashboards/` sous `/app/data/` — évite les crashes d'init
  SQLite/Excel au premier démarrage.
- **Kill-switch** : `KILL_FILE_PATH=/app/data/KILL` (dans le volume persistant,
  pas de bind-mount fichier ; cf. §11.1).
- **Healthcheck** : `curl --fail http://localhost:8080/healthz` toutes les 30 s,
  `start-period=20s`, 3 essais.
- **Entrée** : `ENTRYPOINT ["/usr/bin/tini", "--"]` +
  `CMD ["python", "-m", "src.orchestrator.run", "--serve", "--port", "8080"]`.

```dockerfile
# Extrait (voir Dockerfile racine pour la version complète) :
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "src.orchestrator.run", "--serve", "--port", "8080"]
```

#### 17.5.1 Choix de `uv` comme package manager

Le projet utilise **[uv](https://github.com/astral-sh/uv)** (Astral) en remplacement de `pip + venv + pip-tools`. Rationale :

| Besoin | `pip + requirements.txt` | `uv + pyproject.toml + uv.lock` |
|---|---|---|
| Résolution reproductible | `pip-compile` manuel | `uv.lock` natif, engagé dans git |
| Vitesse install | 45-90 s (MVP stack) | **3-10 s** (10-100× plus rapide) |
| Build Docker cache | bon | excellent (layers séparés deps/project) |
| Groupes de deps (dev/prod) | `requirements-dev.txt` séparé | `[dependency-groups]` PEP 735 natif |
| Gestion Python version | externe (pyenv) | `uv python install 3.12` si besoin |
| Compat `requirements.txt` | — | `uv export` pour Dependabot / SBOM |

**Invariants** :
- `pyproject.toml` = **source de vérité unique** pour les deps (voir §17.5.2).
- `uv.lock` = **lockfile engagée dans git**, garantit reproductibilité CI / prod.
- `requirements.txt` = **artefact généré** via `uv export`, conservé pour Dependabot et scanners SBOM.
- Pas de `pip install` dans les scripts / Docker — **tout passe par `uv`**.

#### 17.5.2 Workflow de développement local avec `uv`

**Installation de `uv` (une fois par machine)** :

```bash
# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh
# Windows
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
# Ou via pipx / brew / nix si préféré
```

**Commandes courantes** :

| Intention | Commande | Effet |
|---|---|---|
| Setup initial après clone | `uv sync` | Crée `.venv/`, installe deps runtime **et** `dev` group |
| Setup CI / prod (sans dev) | `uv sync --frozen --no-dev` | Respecte `uv.lock`, pas de résolution, pas de tests deps |
| Lancer un cycle en local | `uv run python -m src.orchestrator.run --once` | Exécute dans le venv géré, sans activer manuellement |
| Lancer les tests | `uv run pytest` | idem |
| Ajouter une lib runtime | `uv add <pkg>` | Met à jour `pyproject.toml` + `uv.lock` |
| Ajouter une lib dev | `uv add --group dev <pkg>` | idem dans `[dependency-groups.dev]` |
| Retirer une lib | `uv remove <pkg>` | idem |
| Upgrade une lib | `uv lock --upgrade-package <pkg>` | Relock sans toucher les autres |
| Resync après `git pull` | `uv sync` | Idempotent, instant si rien n'a changé |
| Export pour Dependabot | `uv export --format requirements-txt --no-dev --no-hashes -o requirements.txt` | Régénère l'artefact |
| Vérifier la cohérence | `uv lock --check` | Échoue si `pyproject.toml` a divergé de `uv.lock` |

**Hook Git pré-commit recommandé** (`.pre-commit-config.yaml`) :

```yaml
repos:
  - repo: https://github.com/astral-sh/uv-pre-commit
    rev: 0.4.0
    hooks:
      - id: uv-lock           # refuse les commits si uv.lock est out-of-sync
      - id: uv-export         # régénère requirements.txt automatiquement
```

**CI / GitHub Actions** :

```yaml
- uses: astral-sh/setup-uv@v3
  with:
    version: "0.4.x"
    enable-cache: true
- run: uv sync --frozen
- run: uv run pytest
- run: uv run ruff check .
- run: uv run mypy src/
```

**Erreurs loguées côté bot** (taxonomie §7.5) :

| Condition | Code | Niveau | Action |
|---|---|---|---|
| `uv.lock` absent au build Docker | `CFG_MISSING_LOCKFILE` | CRITICAL | build échoue explicitement |
| Divergence `pyproject.toml` ↔ `uv.lock` en CI | `CFG_LOCK_DRIFT` | CRITICAL | job échoue, commit bloqué |
| Import runtime d'un package non listé dans `pyproject.toml` | `RUN_UNDECLARED_IMPORT` | ERROR | via `mypy --strict` périodique |

### 17.6 Variables d'environnement (.env.example)

```bash
# API Anthropic
ANTHROPIC_API_KEY=sk-ant-...

# Telegram
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...

# Sources de données equity — Euronext Paris (cf. §7.1)
# MVP : Stooq (CSV) + Boursorama (scrape) — 100 % gratuit, aucune clé API requise.
# Les rate limits sont gérés côté code via config/sources.yaml (rate_limit_per_sec).

# Simulation
SIMULATION_INITIAL_EQUITY=10000
SIMULATION_BASE_CURRENCY=USD

# Chemins
DATA_DIR=/app/data
MEMORY_DB_PATH=/app/data/memory.db
JOURNAL_XLSX_PATH=/app/data/simulation/journal.xlsx

# Mode
BOT_MODE=simulation            # "simulation" en V1, jamais "live"
LOG_LEVEL=INFO
```

### 17.7 Procédure de premier déploiement

```bash
# Sur le VPS, en user `deploy` (membre du groupe docker)
cd /opt/openclaw-trading-bot          # chemin conseillé

# 1. Copier et remplir les variables d'environnement
cp .env.example .env && nano .env
#    Remplir : ANTHROPIC_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
#    TELEGRAM_WEBHOOK_SECRET, DASHBOARD_BASIC_AUTH_USER/PASS.

# 2. Build + up (deploy.sh gère réseau, build, healthcheck, retention)
./deploy.sh

# 3. Ajouter le bloc Caddy à l'hôte
sudo bash -c 'cat deploy/Caddyfile.snippet >> /etc/caddy/Caddyfile'
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl reload caddy

# 4. (Optionnel) Activer l'auto-update systemd (§17.9)
sudo cp deploy/systemd/openclaw-auto-update.service /etc/systemd/system/
sudo cp deploy/systemd/openclaw-auto-update.timer   /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now openclaw-auto-update.timer

# 5. Vérifier logs + santé
docker compose logs -f trading-bot
curl -s http://127.0.0.1:8080/healthz
```

### 17.8 Procédure de mise à jour manuelle

```bash
# Pull + rebuild + up + healthcheck + rotation images
git pull --ff-only origin main
./deploy.sh

# Vérifier santé
docker compose ps
curl -s http://127.0.0.1:8080/healthz
```

### 17.9 Auto-update (systemd timer)

Ancien déploiement : l'image devait être re-tagguée et `docker compose pull`
lancé à la main — d'où oublis et versions figées. Le nouveau mécanisme est
un **timer systemd quotidien (04:00 UTC)** qui invoque `deploy/auto-update.sh`.

Logique de `deploy/auto-update.sh` (entièrement idempotent) :

1. `git fetch --prune origin main`
2. Compare `HEAD` local vs `origin/main`
3. Si identique → exit 0 silencieux
4. Si différent ET working tree propre → `git merge --ff-only` puis
   `./deploy.sh`
5. Ping Telegram sur succès (`🟢 déployé SHA1 → SHA2`) ou échec
   (`🔴 deploy KO, voir logs`)

Garde-fous (aucun auto-merge destructif) :

- Working tree sale → abort avec WARN (l'opérateur bosse dessus)
- Branche ≠ `main` → abort avec WARN
- `git fetch` KO (réseau) → abort avec ERR, retry au prochain tick
- `ff-merge` impossible (history divergence) → abort + Telegram, fix manuel

Commandes utiles :

```bash
# Check seulement (exit 10 si MAJ dispo, 0 si à jour)
./deploy/auto-update.sh --check

# Force un redeploy même sans nouveau commit (ex: MAJ image de base)
./deploy/auto-update.sh --force

# État du timer
systemctl list-timers openclaw-auto-update.timer
journalctl -u openclaw-auto-update.service -n 100
tail -f data/logs/auto-update.log
```

Fréquence quotidienne (04:00 UTC) justifiée : fenêtre calme entre la clôture
Asie et l'ouverture pre-US, où aucun cycle d'analyse ne tourne. On échange
un peu de réactivité post-push contre zéro interférence avec les cycles et
un journal systemd clair (1 ligne / jour). Pour déployer un hotfix sans
attendre 04:00 UTC : `sudo systemctl start openclaw-auto-update.service`
(ou `./deploy.sh` directement). Si un cycle tourne au moment du tick,
`deploy.sh` + healthcheck gèrent le recreate proprement
(`stop_grace_period: 30s`, WAL SQLite).

### 17.10 Procédure de rollback

```bash
# Rollback sur l'image précédente (tag GIT_SHA gardé via IMAGE_RETENTION)
./deploy.sh --rollback

# OU rollback sur un commit spécifique
git checkout <commit_hash>
./deploy.sh
```

`deploy.sh --rollback` identifie la dernière image SHA précédente
(tri par date de création) et relance `docker compose up -d` dessus —
pas de rebuild nécessaire tant que l'image retention n'a pas pruné.

### 17.11 Kill-switch depuis le VPS

```bash
# Armer — gèle toute nouvelle proposition immédiatement (risk-gate §11.1)
touch /opt/openclaw-trading-bot/data/KILL

# Désarmer
rm /opt/openclaw-trading-bot/data/KILL
```

Le kill-switch est **détecté par le risk-gate à chaque proposition**, pas
par un polling séparé — pas de délai au-delà du temps de cycle courant.
Pour une coupure totale immédiate : `docker compose stop trading-bot`.

---

## 18. Glossaire

| Terme | Définition |
|---|---|
| **ADX** | Average Directional Index — mesure la force d'une tendance (0–100, seuil 25) |
| **Aroon** | Indicateur mesurant le temps écoulé depuis le plus haut/bas récent |
| **ATR** | Average True Range — mesure de volatilité absolue |
| **Chikou Span** | Composant Ichimoku : prix actuel décalé 26 périodes en arrière |
| **CMF** | Chaikin Money Flow — flux de capital entrant/sortant |
| **DD / Drawdown** | Baisse depuis le plus haut d'une courbe de performance |
| **DSR** | Deflated Sharpe Ratio — Sharpe corrigé du data snooping |
| **HMM** | Hidden Markov Model — modèle probabiliste pour la détection de régimes |
| **Kijun-sen** | Composant Ichimoku : moyenne (haut+bas)/2 sur 26 périodes |
| **Kill-switch** | Fichier KILL dont la présence gèle toute proposition de trade |
| **Kumo** | Nuage Ichimoku formé par Senkou Span A et B |
| **OBV** | On-Balance Volume — pression acheteuse/vendeuse cumulative |
| **P&L** | Profit and Loss — gains et pertes |
| **PF** | Profit Factor = gains bruts / pertes brutes |
| **R/R** | Reward/Risk ratio — rapport gain potentiel / perte potentielle |
| **Régime** | Classification du marché (risk-on, risk-off, transition) |
| **RSI** | Relative Strength Index — oscillateur 0–100 |
| **Senkou Span A/B** | Projections futures du Kumo Ichimoku |
| **Supertrend** | Indicateur de suivi de tendance basé sur ATR |
| **Tenkan-sen** | Composant Ichimoku : moyenne (haut+bas)/2 sur 9 périodes |
| **TRIX** | Triple EMA oscillator — filtre les mouvements de bruit |
| **VWAP** | Volume Weighted Average Price — prix moyen pondéré par volume |
| **Walk-forward** | Validation backtest sur fenêtres glissantes (évite l'overfitting) |
| **WAL** | Write-Ahead Logging — mode SQLite permettant la concurrence |

---

*Document vivant — mis à jour par le processus de self-improve mensuel. Toute modification impactant l'architecture ou le risk gate doit être consignée dans `docs/adr/`.*
