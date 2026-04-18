---
name: backtest-quick
description: |
  Backtest walk-forward + Monte-Carlo bootstrap sur 3 ans de données, avec
  Deflated Sharpe Ratio pour corriger le biais multi-testing. Appelé avant
  d'activer une stratégie en paper-trading et par `self-improve` (§13.2
  étape 5) pour valider chaque patch candidat. Produit un HTML + JSON +
  décision pass/fail basée sur un gate strict.

  DÉCLENCHE CE SKILL quand l'utilisateur demande de backtester une
  stratégie, de valider un patch, de vérifier qu'une idée tient sur le
  long terme, ou avant toute mise en paper/prod. Active aussi sur :
  "backtest", "walk forward", "tient la route", "valide ça sur 3 ans".

triggers:
  - "backtest"
  - "backtest <strat>"
  - "walk forward"
  - "tient la route"
  - "valide ça sur N ans"
  - étape 5 du pipeline `self-improve` (§13.2)
  - avant merge d'un patch stratégie

allowed_tools:
  - bash
  - read
  - write

spec_refs:
  - "§9 — Simulation & journal (cadre général)"
  - "§9.4 — Deflated Sharpe Ratio"
  - "§13.2 — Usage par self-improve (étape 5 Validation)"

budget:
  tokens_per_run: 0             # 100 % numpy/pandas, pas de LLM
  wallclock_target_sec: 60      # cible pour 3 ans de data, 1 stratégie

code_paths:
  - src/simulation/backtest.py         # walk-forward + rolling windows
  - src/simulation/monte_carlo.py      # bootstrap sur ordre des trades
  - src/simulation/deflated_sharpe.py  # Bailey & López de Prado
  - src/simulation/gates.py            # pass/fail logic
---

# backtest-quick

## Pourquoi ce skill existe

Un backtest naïf (single-pass, in-sample, sans correction multi-testing)
produit des Sharpes trompeurs. `self-improve` (§13) teste ~3 patches par
run → sans correction, probabilité cumulée de faux positif explose.

Ce skill applique **trois protections** :
- walk-forward : la période de test n'est jamais vue à l'entraînement
- Monte-Carlo bootstrap : teste la robustesse à l'ordre des trades
- Deflated Sharpe (Bailey & López de Prado) : corrige le biais induit par
  le nombre d'essais

C'est le garde-fou statistique de toute la boucle d'auto-amélioration.

## Protocole (§9)

### Walk-forward
- Fenêtre d'entraînement : 252 jours (1 an trading)
- Fenêtre de test : 63 jours (1 trimestre)
- Pas : 63 jours → 12 splits sur 3 ans de données
- Les paramètres de la stratégie (si optimisables) sont fittés sur train
  uniquement, puis appliqués tels quels sur test

### Monte-Carlo
- 1 000 bootstraps sur l'ordre des trades réalisés en test
- Pour chaque bootstrap : recalcul Sharpe, max DD, PF
- Sortie : distribution empirique → `sharpe_p5`, `sharpe_p95`,
  `dd_worst_case` (p95)

### Deflated Sharpe Ratio (§9.4)
Formule Bailey & López de Prado, avec `n_trials = nb de patches testés
dans ce run self-improve`. Intuition : plus on teste de patches, plus un
Sharpe élevé "par chance" devient probable — DSR corrige cette
probabilité.

Seuil : `DSR > 0.95` ≡ probabilité < 5 % que le Sharpe observé soit dû
au bruit + multi-testing.

## Métriques calculées

| Métrique | Formule | Interprétation |
|---|---|---|
| Sharpe | `mean(returns) / std(returns) * sqrt(252)` | risk-adjusted return |
| Sortino | `mean / downside_std * sqrt(252)` | Sharpe qui ignore la vol à la hausse |
| Profit Factor | `gains / |losses|` | $1 perdu = combien gagné |
| Max Drawdown | pic-à-creux max | perte pire cas historique |
| % months positive | mois > 0 / total | consistance |
| Deflated Sharpe | cf. §9.4 | Sharpe corrigé multi-testing |
| Calmar | annual return / max DD | return/risque équilibré |

## Gate de passage

Une stratégie / un patch passe **si et seulement si** :

| Condition | Seuil |
|---|---|
| Sharpe walk-forward | > 1.0 |
| Profit Factor | > 1.3 |
| Max DD | < 15 % equity |
| Deflated Sharpe (p-value) | significatif à 95 % (`DSR > 0.95`) |
| Pente d'equity (dernier tiers) | > 0 |
| Nb de trades | ≥ 50 |

Seuils alignés sur §13.2 (pipeline `self-improve`). Un seul échec →
`reject`. Pas de "bon gré mal gré" — la rigueur statistique est le
garde-fou principal contre le déploiement de stratégies overfittées.

## Contrat de sortie

JSON : `data/backtests/<YYYYMMDD>-<strat_or_patch>.json`

```json
{
  "strategy": "breakout_momentum",
  "patch_id": "H-012",              
  "period": {"start": "2023-04-01", "end": "2026-04-01"},
  "walk_forward_splits": 12,
  "metrics": {
    "sharpe_walk_forward": 1.41,
    "sortino": 1.87,
    "profit_factor": 1.52,
    "max_dd_pct": 8.9,
    "months_positive_pct": 68.2,
    "deflated_sharpe": 0.97,
    "calmar": 1.58,
    "trades_count": 298
  },
  "monte_carlo": {
    "sharpe_p5": 1.08,
    "sharpe_p95": 1.73,
    "dd_worst_case_pct": 12.4
  },
  "gate": {"passed": true, "failing_checks": []},
  "n_trials_this_run": 3,           
  "runtime_sec": 47
}
```

HTML : `data/backtests/<YYYYMMDD>-<strat_or_patch>.html` avec :
- equity curve train + test par split (Plotly)
- distribution Monte-Carlo Sharpe / DD (histogrammes)
- tableau trades (top 20 + 20 bottom par P&L)

## Traçabilité SQLite

| Table | Écriture | Usage aval |
|---|---|---|
| `backtest_runs` | 1 ligne / run | historique, lookup par self-improve |
| `trades_simulated` | N lignes / run | archivage pour audit futur |

## Usage depuis `self-improve`

Appel programmatique (pas via CLI) — cf. §13.2 étape 5 :

```python
from src.simulation.backtest import run_backtest_quick
from src.simulation.deflated_sharpe import deflated_sharpe_ratio

baseline_result = run_backtest_quick(strategy_original, years=3)
patched_result  = run_backtest_quick(strategy_patched,  years=3)
dsr = deflated_sharpe_ratio(
    patched_result.sharpe,
    n_trials=patch.trial_count,   
)
```

## Garde-fous

- **Data leakage zéro.** Les paramètres optimisables (si la stratégie en a)
  ne sont fittés QUE sur la fenêtre train. Toute exception (ex : feature
  engineering qui utilise la moyenne "globale" au lieu d'un rolling)
  doit être documentée et justifiée.
- **Trades < 50** → gate `reject` automatique (pas de significativité
  statistique).
- **Pas d'appel LLM.** 100 % numpy/pandas, déterministe à seed fixée.
  Un run rejoue identique sur même data + même seed.
- **Pas de short-circuit.** Même si une stratégie est "évidemment bonne",
  elle passe par tout le protocole. C'est le protocole qui fait autorité,
  pas l'intuition.

## Commandes manuelles

```bash
# Backtest d'une stratégie active
python -m src.simulation.backtest --strategy breakout_momentum --years 3

# Backtest d'un patch issu de self-improve
python -m src.simulation.backtest --patch-branch self-improve/20260417-fomc-filter

# Mode rapide (1 an, pas de MC — pour itération dev)
python -m src.simulation.backtest --strategy mean_reversion --years 1 --skip-mc

# Comparaison baseline vs patch (comme self-improve)
python -m src.simulation.backtest --compare \
    --baseline breakout_momentum \
    --patched self-improve/20260417-fomc-filter
```
