---
name: dashboard-builder
description: |
  Rend à la fin de chaque session un dashboard HTML statique + summary.md
  Telegram-friendly (≤ 800 chars). Source de vérité du layout : §14 du doc
  d'architecture (14.1 objectifs, 14.2 opportunités, 14.3 coûts, 14.4
  portefeuille, 14.5 data model, 14.6 notifications). Contrat strict :
  l'opérateur doit pouvoir valider ou rejeter les opportunités en < 60 s.

  DÉCLENCHE CE SKILL en fin de pipeline `full_analysis`, à la demande de
  l'opérateur ("génère le dashboard", "montre-moi la photo", "envoie le
  récap Telegram"), ou pour une régénération post-validation d'un patch
  `self-improve`. Active aussi sur : "dashboard", "résumé session",
  "envoie le récap".

triggers:
  - fin de pipeline full_analysis
  - "génère le dashboard"
  - "montre-moi la photo"
  - "envoie le récap"
  - "résumé session"

allowed_tools:
  - read
  - write

spec_refs:
  - "§14.1 — Objectifs, audience, layout global"
  - "§14.2 — Cartes opportunités"
  - "§14.3 — Panel coûts tokens & APIs"
  - "§14.4 — Portefeuille, régime, signaux, stratégies"
  - "§14.5 — Data model & pipeline de build"
  - "§14.6 — Notifications Telegram"

budget:
  tokens_per_run: 0          # 100 % rendu Jinja2, pas de LLM
  wallclock_target_sec: 10

code_paths:
  - src/dashboard/builder.py       # orchestration rendu
  - src/dashboard/templates/       # Jinja2 (base.html + partials)
  - src/dashboard/charts.py        # Plotly pour equity curve, heatmap, donut
  - src/dashboard/summary.py       # rendu summary.md Telegram
---

# dashboard-builder

## Pourquoi ce skill existe

L'opérateur n'a pas le temps de lire 4 JSONs et d'inspecter SQLite à chaque
cycle. Le dashboard est l'**interface unique de décision** : en une page,
comprendre le régime, repérer les opportunités, vérifier le risque,
surveiller le budget. Le summary.md complète avec un micro-résumé
Telegram (≤ 800 chars) pour les situations où l'opérateur est loin de son
écran.

Source de vérité du layout et des composants : **§14 du doc
d'architecture** (sections 14.1 à 14.6). Ce SKILL.md ne redéfinit pas le
design — il décrit uniquement le pipeline de build et les règles
opérationnelles.

## Layout (rappel concis — détails §14)

| # | Section | § |
|---|---|---|
| 1 | Header : session, ts UTC, régime macro/vol, benchmarks (SPX/BTC/DXY/VIX) | 14.1 |
| 2 | Heatmap actifs × familles d'indicateurs (couleur = score signé) | 14.2 |
| 3 | Cartes opportunités (top 5 long / top 5 short) + boutons validate/reject | 14.2 |
| 4 | Table des proposals : entry / stop / TP / R/R / conviction / catalyst | 14.2 |
| 5 | Panel exposition : donut par classe, corrélations, DD, equity 90 j | 14.4 |
| 6 | Calendrier événements 72 h (tag macro / earnings / onchain / geo) | 14.4 |
| 7 | Perf stratégies 30 j : winrate, PF, Sharpe, active/disabled | 14.4 |
| 8 | Panel coûts : tokens LLM du jour, coût cumulé mois, top consommateurs | 14.3 |
| 9 | Notes agent : 3-5 phrases (crois / doute / surveille) — sortie LLM | 14.1 |

Le rendu utilise `jinja2` + `plotly` (tous deux dans requirements.txt). Pas
de CSS framework lourd — Tailwind via CDN pour rester offline-friendly.

## Pipeline de build (§14.5)

### 1. Collecte des inputs
Lit depuis SQLite + JSON du cycle courant :
- `regime_snapshots` (plus récent)
- `market-scan.json`, `signal-crossing.json`, `news-pulse.json`,
  `strategy-selector.json`, `risk-gate.json` — tous écrits dans
  `data/analyses/<day>/<session>/`
- tables `trades`, `performance_metrics`, `llm_usage`, `api_usage`

### 2. Construction des graphiques
- Equity curve 90 j → Plotly line
- Heatmap signaux → Plotly heatmap avec colorscale RdYlGn
- Donut exposition → Plotly pie par classe
- Corrélation intra-portefeuille → matrice seaborn-like

Tous les charts sont **inline dans le HTML** (pas de requêtes externes) —
contrainte de fiabilité réseau du VPS.

### 3. Rendu Jinja2
Template principal : `src/dashboard/templates/session.html.j2`. Partials :
`_header.j2`, `_opportunities.j2`, `_risks.j2`, `_portfolio.j2`,
`_costs.j2`, `_notes.j2`.

### 4. Summary Telegram
`summary.md` : 800 chars max. Format strict :
```
📊 [pre_us] 2026-04-17 · risk_on/mid
💼 Equity 102.4k (+0.3%) · DD 1.2% · 5/8 positions
🎯 3 opportunités · 🚨 1 risque · 📰 2 news high
💸 12k tok ($0.38) · 52% budget mois
🔗 <dashboard_url>
```

### 5. Persistance
- `data/analyses/<day>/<session>/dashboard.html`
- `data/analyses/<day>/<session>/dashboard.png` (screenshot via playwright
  ou headless-chrome si disponible, sinon skip)
- `data/analyses/<day>/<session>/summary.md`

## Règles d'émission

- **> 30 % de l'univers indisponible** → rend le dashboard mais avec un
  **badge rouge "scan dégradé"** en header, et summary.md préfixé de `⚠️ `.
  Ne jamais silencieusement publier un dashboard incomplet.
- **Aucune proposition retenue** → dashboard rendu quand même (avec section
  opportunités vide), summary Telegram émis avec `0 opportunités`. Utile
  pour l'opérateur de savoir que le cycle a bien tourné à vide.
- **Cycle en cours d'exécution** (concurrence) → ne pas écraser le
  dashboard du cycle précédent avant que le nouveau soit prêt ; écrire
  d'abord dans `dashboard.html.tmp` puis `mv` atomique.

## Interaction avec Telegram (§14.6)

Le skill n'envoie pas lui-même le Telegram — il écrit `summary.md`. C'est le
service FastAPI (`src/api/telegram.py`, §14.6 + §17.2) qui lit ce fichier
et push le message à chaque nouveau `summary.md` détecté (inotify ou
polling).

Cette séparation permet de re-pusher manuellement (`POST /telegram/resend`)
sans relancer tout le dashboard.

## Traçabilité SQLite

| Table | Écriture | Usage aval |
|---|---|---|
| `dashboard_renders` | 1 ligne / render | audit, détection des renders manqués |

## Garde-fous

- **Pas de LLM.** Toutes les phrases humaines viennent des autres skills
  (notes_agent produites par les agents en amont). Ce skill agrège et rend,
  point. Évite que le dashboard devienne une 3ᵉ couche de génération LLM
  avec ses propres risques (incohérence avec les données, coût).
- **Pas d'accès réseau.** CDN Tailwind uniquement (loading async, fallback
  si offline) + ressources inline. Le VPS peut avoir une connexion flaky ;
  le dashboard doit se rendre même offline.
- **Déterministe à même inputs.** Deux runs consécutifs sur les mêmes
  données doivent produire le même HTML (modulo le timestamp). Pas de
  `random`, pas d'ordre dict non trié.

## Commandes manuelles

```bash
# Rendu pour le cycle courant
python -m src.dashboard.builder --session pre_us

# Re-render après correction d'une donnée (sans relancer le pipeline)
python -m src.dashboard.builder --session pre_us --force

# Rendu avec données factices (dev / tests visuels)
python -m src.dashboard.builder --fixtures tests/fixtures/minimal.json
```
