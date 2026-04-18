---
name: self-improve
description: |
  Boucle d'auto-amélioration hebdomadaire. Tous les dimanches à 22:00 UTC, analyse
  les ~30 derniers trades clos, identifie via LLM les patterns dominants des pertes,
  génère 1 à 3 patches candidats (filtres, pondérations, exclusions), les valide par
  backtest walk-forward + Monte-Carlo + Deflated Sharpe, et produit une PR locale
  (IMPROVEMENTS_PENDING.md) soumise à validation humaine avant merge puis
  14 jours de paper-trading avant activation réelle.

  DÉCLENCHE CE SKILL quand l'utilisateur demande une analyse rétrospective des
  trades, une revue de performance, une proposition de patch stratégie, ou une
  itération d'amélioration — même s'il ne dit pas explicitement "self-improve".
  Déclenche aussi sur les triggers : "améliore-toi", "que peux-tu apprendre de la
  semaine", "patch candidat", "revue hebdo".

triggers:
  - cron hebdomadaire
  - "auto-amélioration"
  - "self-improve"
  - "revue hebdo"

allowed_tools:
  - bash
  - read
  - write

spec_refs:
  - "§13.1 — 4 échelles de self-improve"
  - "§13.2 — Pipeline hebdomadaire (8 étapes)"
  - "§13.3 — Garde-fous"
  - "§2.3 — opus-4-7 réservé pour ce skill"
  - "§14.3.6 — Budget tokens par run"

schedule:
  cron: "0 22 * * 0"            # dimanche 22:00 UTC (cf. config/schedules.yaml)
  timezone: UTC

budget:
  model_preferred: claude-opus-4-7   # §2.3 — réservé aux tâches créatives lentes
  model_fallback: claude-sonnet-4-6  # bascule si budget mensuel > 80 %
  target_tokens_per_run: 45000       # §14.3.6 — ~42k observés sur 90 trades
  hard_cap_tokens: 80000             # au-delà : abort + alerte Telegram

code_paths:
  - src/self_improve/analyzer.py          # collecte + featurize + diagnostic
  - src/self_improve/patch_generator.py   # synthèse LLM des patches
  - src/self_improve/validator.py         # walk-forward + Monte-Carlo + DSR
  - src/self_improve/pr_builder.py        # rendu IMPROVEMENTS_PENDING.md
---

# self-improve

## Pourquoi ce skill existe

Le bot apprend à trois niveaux (§13.1) : `intra-cycle` (observations en base),
`memory-consolidate` (fusion nocturne des leçons), et `self-improve` (corrections
du code stratégie). Ce skill couvre le **3ᵉ étage** : passer des observations à
des modifications concrètes du code, sous garde-fous stricts pour ne pas casser
l'attribution de performance ni introduire d'overfitting.

La règle d'or §2.1 reste : le LLM **propose**, Python **valide**, l'humain
**décide**. Ici, le LLM n'écrit jamais directement dans `main` — il ouvre une
PR locale que l'opérateur doit valider via Telegram ou dashboard.

## Pipeline (8 étapes — §13.2)

Chaque étape est idempotente : un re-run le même dimanche ne produit pas de
doublons grâce à l'idempotency key `self_improve_<YYYY-WW>`.

### 1. Collecte (Python pur, 0 token)
Source : tables SQLite `trades`, `performance_metrics`, `regime_snapshots`.
Sélectionne les 30 derniers trades clos (`closed_at IS NOT NULL`) + leur
contexte régime au moment de l'entrée. Identifie les perdants
(`pnl_pct < -0.5%`).

### 2. Featurize (Python pur, 0 token)
Pour chaque trade, calcule **~20 features** : régime macro + vol, score
composite d'entrée, heure UTC, ADX, RSI, distance aux bandes de Bollinger,
news_density à t-60min, slippage simulé, corrélation intra-panier, taille
relative, catégorie de stratégie, etc. Résultat : DataFrame
`data/self_improve/YYYY-WW/features.parquet`.

### 3. Diagnostic (LLM — 1 appel, ≤ 1 000 tokens)
Prompt compact : features agrégées + distribution gagnants/perdants.
Demande au LLM (opus-4-7) d'identifier **3 patterns dominants** dans les
perdants, en JSON strict :

```json
[
  {
    "pattern": "entrée pendant volatilité_high + régime risk_off",
    "frequency": 0.42,
    "loss_contribution_pct": 0.61,
    "suggested_fix": "exclure strategies momentum si vix > 25 ET regime = risk_off"
  }
]
```

### 4. Génération patches (LLM — N appels, ≤ 800 tokens chacun)
Pour chaque pattern retenu, demande un patch Python concret au format diff
unifié, ciblant `src/signals/`, `src/strategies/`, ou `config/strategies.yaml`.
Ne jamais toucher `config/risk.yaml` sans ADR signée (§13.3).

Branche git locale : `self-improve/YYYYMMDD-<slug>` (crée une branche par
patch, pas une par run — permet de rejeter sélectivement).

### 5. Validation (Python pur, 0 token — délègue à `backtest-quick`)
Pour chaque patch, appelle `skills/backtest-quick` :
- walk-forward 3 ans, rolling 12 mois
- Monte-Carlo 1 000 bootstraps
- **Deflated Sharpe Ratio** (Bailey & López de Prado) avec `n_trials = nb de patches testés ce run`

Sortie : `PatchValidationResult` (cf. §13.2 extrait code) persisté dans la
table `llm_usage` avec `agent='self_improve'` pour traçabilité budget.

### 6. Sélection (§13.2)
Un patch passe si **toutes** ces conditions sont remplies :

| Critère | Seuil | Raison |
|---|---|---|
| t-stat(returns_patch − returns_baseline) | > 2.0 | significativité statistique |
| Sharpe(patch) | > Sharpe(baseline) | amélioration réelle |
| max_dd(patch) | ≤ max_dd(baseline) × 1.1 | tolérance DD 10 % |
| trade_count(patch) | ≥ 50 | échantillon suffisant |
| DSR | > 0.95 | anti-overfitting (corrige biais multi-testing) |

Si plusieurs patches passent : garde uniquement celui avec le meilleur DSR
(max 1 merge par semaine — §13.3).

### 7. PR locale
Produit `IMPROVEMENTS_PENDING.md` à la racine du repo selon le gabarit
ci-dessous. Crée aussi une entrée dans la table `hypotheses` avec id `H-<NNN>`,
status `testing`, `bayesian_score=0.5`, `evidence=[]`.

### 8. Notification + activation
- Envoie résumé Telegram avec lien dashboard vers `IMPROVEMENTS_PENDING.md`.
- Attend validation humaine (bouton `/approve H-NNN` ou `/reject H-NNN`).
- Si approuvé : merge dans `main` → **14 jours de paper-trading obligatoires**
  avec la nouvelle stratégie listée dans `active:` de `strategies.yaml` avant
  toute éventuelle promotion. V1 reste `BOT_MODE=simulation` en dur —
  la bascule live est V2, après ADR signée (§1 du doc d'archi, CLAUDE.md règle #1).
- Si rejeté : update `hypotheses.status = 'rejected'`, archive la branche, crée
  une leçon `lessons` taggée `rejected_patch`.

## Format IMPROVEMENTS_PENDING.md

Gabarit strict — `pr_builder.py` doit produire exactement cette structure pour
que le dashboard sache le parser :

```markdown
# Patch candidat H-NNN — YYYY-MM-DD

## Contexte
30 derniers trades (YYYY-MM-DD → YYYY-MM-DD), N perdants (XX %).

## Pattern identifié
<phrase issue de l'étape 3, 1-2 lignes>

## Hypothèse
Si on applique <fix>, on réduit <loss_contribution_pct> des pertes de ce pattern.

## Diff résumé
```diff
<diff unifié, max 50 lignes ; tronquer avec '...' si plus long>
```

## Métriques — avant / après (backtest 3 ans walk-forward)
| Métrique     | Baseline | Patch   | Δ       |
|--------------|---------:|--------:|--------:|
| Sharpe       | 1.22     | 1.41    | +0.19   |
| Max DD       | 8.4 %    | 8.9 %   | +0.5 pt |
| Trades       | 312      | 298     | −14     |
| t-stat       | —        | 2.37    | —       |
| DSR          | —        | 0.97    | —       |

## Risques identifiés
- <1 à 3 bullets>

## Rollback
`git revert <sha>` puis `docker compose restart bot`.
La table `hypotheses` sera mise à jour avec `status='rejected'`.

## Validation attendue
Répondre `/approve H-NNN` ou `/reject H-NNN` via Telegram.
```

## Garde-fous (§13.3)

Règles non négociables — si un de ces garde-fous saute, le run avorte avec
alerte Telegram :

1. **Max 1 patch mergé par semaine.** Conserve l'attribution de performance :
   si deux patches s'activent la même semaine, impossible d'isoler l'effet de
   chacun. C'est pour cela que §6 ne garde qu'un seul gagnant même si
   plusieurs patches passent.
2. **Jamais de modification de `config/risk.yaml` sans ADR signée** dans
   `docs/adr/`. Les limites de risque sont le dernier rempart ; elles méritent
   un document formel, pas un patch automatique.
3. **Budget hard cap : 80 000 tokens/run.** Au-delà, abort + alerte. Un run
   typique fait 42k (§14.3.6) ; dépasser 80k signale soit un bug prompt, soit
   une dérive de complexité qu'il faut investiguer avant de laisser courir.
4. **14 jours de paper-trading post-merge avant activation réelle.** Un
   backtest passe rarement un bug de data leakage ; le paper trading en
   conditions réelles est le garde-fou final contre cette classe d'erreurs.
5. **Idempotency key `self_improve_<YYYY-WW>`.** Un re-run la même semaine
   (crash scheduler, run manuel) ne recrée pas de PR doublon.

## Traçabilité (SQLite)

À chaque run, les écritures suivantes sont obligatoires :

| Table | Ligne ajoutée | Champ-clé |
|---|---|---|
| `llm_usage` | 1 par appel LLM | `agent='self_improve'`, `model`, `tokens_in`, `tokens_out` |
| `hypotheses` | 1 par patch retenu | `id='H-NNN'`, `status='testing'`, `started_at=now()` |
| `lessons` | 1 par patch rejeté/activé | `tags=['self_improve','<accepted|rejected>']` |
| `performance_metrics` | mise à jour 14 jours post-activation | snapshot Sharpe/DD avant vs après |

Ces écritures permettent au dashboard (§14.3.6 "Top consommateurs") de voir
exactement ce qu'a coûté le run et au skill `memory-consolidate` (§10.6) de
nettoyer les hypothèses rejetées > 90 j.

## Commandes manuelles (utile en dev)

```bash
# Dry-run : tout le pipeline sauf écriture PR + notification
python -m src.self_improve.analyzer --dry-run --window-days 30

# Run complet (équivalent du cron)
python -m src.self_improve.analyzer --run

# Rejouer un patch spécifique (après modif de la logique)
python -m src.self_improve.validator --hypothesis H-007
```

## Interaction avec les autres skills

- **`backtest-quick`** : appelé à l'étape 5. Source de vérité pour le calcul
  Sharpe / DD / DSR.
- **`memory-consolidate`** (§10.6) : nettoie les hypothèses rejetées à 90 j +
  archive les leçons taggées `self_improve` avec `confidence < 0.4`.
- **`risk-gate`** : JAMAIS modifié par ce skill — garde-fou #2.

## Modes dégradés

- **Budget mensuel > 80 %** → bascule automatique opus-4-7 → sonnet-4-6.
  Ajoute tag `low_budget_mode` à la leçon finale pour détecter a posteriori
  si la qualité des patches en a souffert.
- **< 30 trades clos dans la fenêtre** → skip propre du run (log + entrée
  télémétrie `skipped_reason='insufficient_data'`). Réessaie la semaine
  suivante. Pas d'alerte Telegram (silence par défaut, §14.6).
- **Tous les patches échouent la sélection** → pas de PR, mais écrit une
  leçon dans `lessons` résumant les patterns trouvés et pourquoi aucun patch
  n'est passé. Utile pour le `memory-consolidate` et pour orienter la revue
  d'architecture mensuelle.
