---
name: strategy-selector
description: |
  Sélectionne 1 à 3 stratégies actives pour le cycle courant en fonction du
  régime HMM (§12), de la performance récente (table SQLite
  `performance_metrics`) et des contraintes définies dans
  `config/strategies.yaml`. Ne modifie jamais plus de 1 stratégie active par
  semaine sans ADR signée (garde-fou `self-improve`).

  DÉCLENCHE CE SKILL en début de chaque pipeline `full_analysis` et chaque
  fois que l'utilisateur demande quelles stratégies sont actives, demande un
  "choix de stratégie pour aujourd'hui", un "quelle approche privilégier
  vu le marché", ou quand le régime change. Active aussi sur : "quelles
  stratégies", "quel mode", "quelle approche".

triggers:
  - début pipeline `full_analysis`
  - "quelles stratégies aujourd'hui"
  - "quel mode"
  - "quelle approche"
  - changement de régime HMM (§12)

allowed_tools:
  - read

spec_refs:
  - "§6 — Catalogue des 7 stratégies (ichimoku, breakout_momentum, etc.)"
  - "§12 — Détection de régime HMM"
  - "§13.3 — Garde-fou 1 changement/semaine"

budget:
  tokens_per_run: 0       # 100 % Python
  wallclock_target_ms: 100

code_paths:
  - src/strategies/selector.py   # logique de sélection
  - src/regime/hmm.py            # input régime courant (§12)
  - config/strategies.yaml       # catalogue source de vérité
---

# strategy-selector

## Pourquoi ce skill existe

Toutes les stratégies ne sont pas compatibles avec tous les régimes.
`ichimoku_trend_following` perd en régime `risk_off_mid_vol` (whipsaws).
`mean_reversion` perd en trending fort. Sans selector, soit on les active
toutes (perf globale noyée dans le bruit), soit on fige un choix humain
(pas de réactivité au régime).

Ce skill fait l'**appariement régime → stratégies** en respectant les
contraintes de diversification et le garde-fou de stabilité.

## Catalogue des 7 stratégies (§6)

Source de vérité : `config/strategies.yaml`. Les noms, classes d'actifs,
horizons et régimes compatibles **viennent de là, pas d'ici**.

| Stratégie | §  | Classes | Horizon | Régimes compatibles |
|---|---|---|---|---|
| `ichimoku_trend_following` | 6.1 | fx, crypto, equities | 3-20 j | risk_on_strong, risk_off_strong (|score|>0.65) |
| `breakout_momentum` | 6.2 | fx, crypto, equities | 1-5 j | vol mid/high, tout régime directionnel |
| `mean_reversion` | 6.3 | fx, equities | intraday | vol low/mid, régime sideways |
| `divergence_hunter` | 6.4 | crypto, equities | 2-10 j | vol mid, changement de régime |
| `volume_profile_scalp` | 6.5 | crypto, equities | minutes | vol mid/high, sessions liquides |
| `event_driven_macro` | 6.6 | fx, equities | jours post-event | hors fenêtres d'évitement |
| `news_driven_momentum` | 6.7 | toutes | 30 min - 4 h | breaking news impact=high |

## Règles de sélection

### Contraintes dures (ne jamais violer)
- **Max 3 stratégies actives** simultanément — au-delà, attribution de
  performance devient ingérable
- **Max 60 % d'allocation sur une seule stratégie** — évite la mono-culture
- **Une stratégie `enabled: false` dans strategies.yaml** reste inéligible
  quoi qu'il arrive (désactivation opérateur > sélection automatique)

### Heuristiques régime (modulables par self-improve)
- Régime `risk_on` + vol `low/mid` → favoriser `ichimoku_trend_following`,
  `breakout_momentum`
- Régime `risk_off` + vol `high` → `event_driven_macro` + réduction taille
  systémique via risk-gate
- Régime `sideways` → `mean_reversion` + `volume_profile_scalp`
- Crypto en dehors des heures de marché equity → ne désactive pas les
  stratégies crypto-compatibles
- News `impact=high` détectée par `news-pulse` dans les 30 dernières
  minutes → `news_driven_momentum` force-activé (s'ajoute ou remplace la
  3ᵉ stratégie la moins confiante)

### Désactivation auto (performance-driven)
Si une stratégie présente sur les 30 derniers jours **toutes** ces
conditions :
- `sharpe_30d < 0`
- `max_dd_30d > 1.5 × hist_max_dd`

→ désactivation automatique du cycle en cours + entrée dans `lessons`
(tag `auto_disable`) + notification Telegram. Reste désactivée jusqu'à
validation humaine OU jusqu'à ce que `self-improve` propose un patch
correctif.

### Garde-fou stabilité (§13.3)
**Max 1 stratégie ajoutée/retirée de `active:` par semaine** sans ADR. Le
selector peut changer les *poids* autant qu'il veut, mais le set de
stratégies actives reste stable — sinon l'attribution de performance
devient du bruit.

## Contrat de sortie

```json
{
  "ts": "2026-04-17T13:00:00Z",
  "regime_context": {"macro": "risk_on", "vol": "mid", "hmm_confidence": 0.78},
  "active_strategies": ["ichimoku_trend_following", "breakout_momentum"],
  "weights": {
    "ichimoku_trend_following": 0.60,
    "breakout_momentum": 0.40
  },
  "rationale": "Régime risk_on soutenu (VIX 14, DXY trending down). Ichimoku prioritaire sur signal trend fort (ADX 28). Breakout complément sur cassures récentes.",
  "disabled_this_cycle": [
    {"name": "mean_reversion", "reason": "regime trending, not sideways"}
  ],
  "auto_disabled": []
}
```

## Traçabilité SQLite

| Table | Écriture | Usage aval |
|---|---|---|
| `strategy_selections` | 1 ligne / cycle | dashboard §14.4, self-improve §13 |
| `lessons` | 1 si `auto_disabled` non vide | feed memory-consolidate, self-improve |

## Garde-fous

- **Jamais modifier `config/strategies.yaml`.** Ce skill lit le fichier,
  ne le modifie pas. Les modifications viennent de :
  - l'opérateur (validation PR locale)
  - `self-improve` via PR dans `IMPROVEMENTS_PENDING.md`
- **Régime `unknown` (HMM cold-start)** → fallback sur défaut conservateur :
  `ichimoku_trend_following` seule, poids 1.0, confidence notée basse. Les
  heuristiques régime sont skippées jusqu'à ce que le HMM soit entraîné.
- **Pas d'appel LLM.** Les heuristiques sont du matching simple sur les
  champs `regimes:` de strategies.yaml.

## Interaction avec les autres skills

- **Lit** : `regime_snapshots` (plus récent), `performance_metrics` (30j),
  `config/strategies.yaml`, sortie du dernier `news-pulse`
- **Écrit** : `strategy_selections` → lu par `signal-crossing` et
  `dashboard-builder`
- **Déclenché par** : `market-scan` (en fin de run, sur la base du régime)
- **Déclencheur de** : rien (il publie un état, les autres le lisent)

## Commandes manuelles

```bash
# Sélection pour le cycle courant
python -m src.strategies.selector --session pre_us

# Forcer l'évaluation du régime sans appeler HMM
python -m src.strategies.selector --regime-override risk_on_mid_vol

# Afficher les stratégies qui seraient compatibles sans les poids
python -m src.strategies.selector --list-eligible
```
