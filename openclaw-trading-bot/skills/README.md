# Skills OpenClaw — trading bot

Skills sur-mesure installées localement. Chacune possède un `SKILL.md` avec
front-matter YAML structuré (`name`, `description`, `triggers`,
`allowed_tools`, `spec_refs`, `budget`, `code_paths`) et des instructions
détaillées.

Chaque skill référence explicitement les sections du doc d'architecture
(`TRADING_BOT_ARCHITECTURE.md`) qui font autorité — ces SKILL.md sont des
**vues opérationnelles** du pipeline, pas des sources de vérité
indépendantes. En cas de divergence entre un SKILL et le doc, le doc gagne.

## Catalogue

| Skill | Rôle | Budget / run | Source de vérité |
|---|---|---|---|
| `market-scan` | Scan univers, top movers, hint régime, shortlist | 0 tok | §5.1, §7, §11.2, §12 |
| `signal-crossing` | Score composite [-1,+1] + confidence par actif | 0 tok | §5.5, §6 |
| `news-pulse` | Ingestion news, NER, sentiment, catalyseurs, ad-hoc trigger | ~4k tok (sonnet) | §6.7, §7, §15.1 |
| `risk-gate` | 10 contrôles fail-fast (Ichimoku §11.5 inclus) avant émission | 0 tok | §11 (complet) |
| `strategy-selector` | Appariement régime → stratégies actives (max 3) | 0 tok | §6, §12, §13.3 |
| `dashboard-builder` | HTML session + summary Telegram ≤ 800 chars | 0 tok | §14.1 à §14.6 |
| `memory-consolidate` | Fusion / archivage / index FAISS (nuit 02:00 UTC) | 0 tok | §10.3 à §10.6 |
| `backtest-quick` | Walk-forward + Monte-Carlo + DSR + gate pass/fail | 0 tok | §9, §9.4, §13.2 |
| `self-improve` ⭐ | Analyse perfs hebdo + patches + PR locale (dim 22:00 UTC) | ~45k tok (opus) | §13.1 à §13.3 |

## Conventions

- **Budget tokens = 0** sauf `news-pulse` (sonnet-4-6) et `self-improve` (opus-4-7,
  réservé par §2.3). Tout skill consommant du LLM doit déclarer `budget.model`
  et `budget.tokens_per_run` dans son front-matter.
- **Triggers "pushy"** — chaque description commence en imperatif
  (« DÉCLENCHE CE SKILL quand… ») pour améliorer le matching côté LLM
  orchestrateur.
- **Source SQLite > fichiers** — toutes les sorties persistent à la fois en
  JSON pour audit et en SQLite (tables dédiées) pour requêtage dashboard /
  self-improve. Plus de références à `MEMORY.md §X` — la mémoire est en
  base, MEMORY.md n'est qu'une façade humaine (§10.3).
- **Commandes manuelles** systématiquement documentées en bas de chaque
  SKILL — pour dev, debug, et re-run ad-hoc.

## Dépendances entre skills (pipeline full_analysis)

```
market-scan  →  strategy-selector  →  signal-crossing  →  risk-gate
     │               │                      │                 │
     └──→  news-pulse   ───────────────────→│                 │
                                            └──→ proposition ─┘
                                                      │
                                            dashboard-builder
                                                      │
                                                  Telegram
```

Tâches de maintenance (hors pipeline cycle) :
- `memory-consolidate` : nuit 02:00 UTC (§10.6)
- `self-improve` : dimanche 22:00 UTC (§13.2)
- `backtest-quick` : appelé par `self-improve` étape 5, ou manuellement

## Skills tierces à envisager (après revue sécurité)

- `earnings-calendar` (ClawHub — vérifier la signature et le code)
- `economic-calendar` (macro events)
- `onchain-whales` (flux crypto)
- `portfolio-optimizer` (Markowitz / HRP)

⚠️ **ClawHub a purgé plus de 2400 skills malveillants début 2026.** Toujours :
1. Lire le code source.
2. Exécuter en sandbox sans accès réseau avant activation.
3. Vérifier les permissions `allowed_tools`.
4. Figer la version dans `skills/<name>/VERSION`.
