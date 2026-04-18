---
name: signal-crossing
description: |
  Calcule un score composite [-1, +1] + confidence [0, 1] par actif, en croisant
  5 familles d'indicateurs (tendance, momentum, volatilité, flux, sentiment/macro).
  Opère sur la shortlist issue de `market-scan`. Les pondérations par famille
  sont internes au skill ; les pondérations par stratégie viennent de
  `config/strategies.yaml` (§5.5, `default_weights`) et sont susceptibles d'être
  ré-optimisées par `self-improve`.

  DÉCLENCHE CE SKILL quand l'utilisateur cite un ticker (EURUSD, BTC, NVDA…) et
  demande une analyse technique, un score, un verdict indicateurs, un "qu'est-ce
  que dit le graphe", ou quand l'orchestrateur traite la shortlist. Ne jamais
  emmettre de proposition sans passer ensuite par `risk-gate`.

triggers:
  - "signal"
  - "score"
  - "croiser indicateurs"
  - ticker explicite
  - post-`market-scan` dans le pipeline full_analysis

allowed_tools:
  - read
  - bash

spec_refs:
  - "§5.5 — Score composite & default_weights"
  - "§6 — Stratégies actives (ichimoku, breakout_momentum, etc.)"
  - "§7 — news-pulse pour la famille sentiment/macro"
  - "§11 — risk-gate (appel obligatoire en aval)"

budget:
  tokens_per_run: 0            # 100 % Python, pas de LLM
  wallclock_target_sec: 5      # ~50 ms par actif × 20 actifs shortlist

code_paths:
  - src/signals/indicators/       # MA, ADX, RSI, MACD, OBV, Bollinger…
  - src/signals/composite.py      # calcul du score composite
  - src/strategies/               # 7 stratégies cf. config/strategies.yaml
---

# signal-crossing

## Pourquoi ce skill existe

Un indicateur seul est bruité. Le crossing sert à agréger 5 familles
indépendantes pour obtenir un signal plus robuste — et à exposer les
composantes pour que l'humain puisse comprendre le score et, en cas
d'activation de `self-improve`, identifier quelle famille sous-performe.

## Familles d'indicateurs (pondération interne)

| Famille | Indicateurs | Poids |
|---|---|---|
| Tendance | MA20/50/200, ADX(14), Ichimoku cloud, chikou span | **0.35** |
| Momentum | RSI(14), MACD histogram, ROC(20), stochastique | **0.25** |
| Volatilité | ATR(14), Bollinger width & %B, Keltner | **0.10** |
| Flux | OBV slope, volume ratio 5/20, CVD (si crypto) | **0.15** |
| Sentiment / Macro | score `news-pulse` asset-level, DXY, yields 10y | **0.15** |

Ces poids sont **indépendants** des `default_weights` de `config/strategies.yaml`
(qui, eux, pondèrent les *piliers stratégiques* — ichimoku, trend, momentum,
volume — au niveau du score composite de proposition). Ne pas confondre.

## Contrat de sortie

**Ce skill ne produit PAS de TradeProposal.** Il émet un *diagnostic* scoré
par actif. La transformation diagnostic → TradeProposal (entry, stop, tp, rr,
risk_pct, catalysts) est faite par le module `src/strategies/<strategy_id>.py`
via `build_proposal(signal, market_data, regime, config)` — cf. §8.9 du
TRADING_BOT_ARCHITECTURE.md. Cette séparation garantit la règle d'or §2.1
(« Python décide, LLM propose ») : même la construction du prix d'entrée,
du stop et du R/R reste déterministe, logée dans la stratégie qui connaît
ses propres règles de sizing.

```json
{
  "asset": "NVDA",
  "ts": "2026-04-17T13:05:00Z",
  "score": 0.62,
  "confidence": 0.75,
  "components": {
    "trend": 0.9,
    "momentum": 0.7,
    "volatility": -0.1,
    "flow": 0.5,
    "sentiment_macro": 0.3
  },
  "regime_ok": true,
  "ichimoku": {
    "price_above_kumo": true,
    "tenkan_above_kijun": true,
    "chikou_above_price_26": true,
    "aligned_long": true,
    "aligned_short": false
  },
  "applicable_strategies": ["breakout_momentum", "news_driven_momentum"],
  "rejected_strategies": [
    {"name": "ichimoku_trend_following", "reason": "tenkan_crosses_kijun = false"}
  ]
}
```

Le bloc `ichimoku` est consommé par `risk-gate` (check §11.5 `ichimoku_alignment`)
et permet au gate de trancher sans recalculer les valeurs — il lit ce que
signal-crossing a déjà calculé et compare avec la direction de la proposition.

## Règles d'interprétation

- `score > 0.30` ∧ `confidence ≥ 0.40` → candidat **long**
- `score < -0.30` ∧ `confidence ≥ 0.40` → candidat **short**
- `confidence < 0.40` → `wait` (pas de proposition, pas de passage `risk-gate`)
- `regime_ok = false` → **override wait** : même un score fort est ignoré si
  le régime macro/vol courant incompatible avec toutes les stratégies actives
  (cf. §6, champ `regimes:` par stratégie)

## Calcul de la confidence

`confidence = convergence × data_quality × regime_alignment`

- `convergence` ∈ [0,1] : combien des 5 familles vont dans le même sens
- `data_quality` ∈ [0,1] : 1.0 si toutes les sources OHLCV fraîches,
  dégrade linéairement si timestamps > 5 min ou bougies manquantes
- `regime_alignment` ∈ [0,1] : 1.0 si ≥ 1 stratégie active compatible au
  régime courant (§12), 0.0 sinon

Cette décomposition rend la confidence auditable — on sait pourquoi elle est
basse.

## Interaction avec `strategy-selector` et `src/strategies/<id>.py`

`applicable_strategies` contient les noms présents dans `active:` de
`config/strategies.yaml` ET dont les conditions `entry:` sont satisfaites
(cf. stratégies actuelles : `ichimoku_trend_following`, `breakout_momentum`,
`mean_reversion`, `divergence_hunter`, `volume_profile_scalp`,
`event_driven_macro`, `news_driven_momentum`).

`rejected_strategies[]` liste les stratégies qui seraient éligibles par
régime mais dont une condition d'entrée manque (utile pour le debug et
pour `self-improve`).

Chaîne complète orchestrateur (§8.7) :

```
signal-crossing.score(asset)
    → SignalOutput (ce JSON)
strategy-selector.pick(regime, signals)
    → SelectionOutput.active_strategies
for strategy_id in signal.applicable_strategies ∩ active_strategies:
    src/strategies/<strategy_id>.build_proposal(signal, market_data, regime)
        → TradeProposal | None
risk-gate.evaluate(proposal)
    → approve | reduce_size | reject
```

La couche `src/strategies/<id>.py` est le **seul** endroit où un `SignalOutput`
peut devenir un `TradeProposal`. Aucun skill LLM ne doit fabriquer de
TradeProposal — c'est du calcul déterministe (ATR pour le stop, targets
configurés dans `exit:` de strategies.yaml, R/R dérivé).

## Traçabilité SQLite

| Table | Écriture | Usage aval |
|---|---|---|
| `observations` | 1 ligne / actif / cycle | feed `self-improve` §13 |

Chaque observation contient le score, la confidence, les composants, l'état
du régime — c'est le dataset d'entraînement de `self-improve` (étape 2 featurize).

## Garde-fous

- **Ne jamais sortir `score` sans `confidence`.** Un score numérique seul est
  faussement précis.
- **Ne jamais court-circuiter `risk-gate`.** Même un score 0.95 avec
  confidence 0.95 doit passer par `risk-gate` (§11) avant toute proposition.
- **Pas d'appel LLM.** Ce skill est 100 % déterministe ; toute évolution qui
  voudrait y injecter du LLM doit passer par ADR (risque d'instabilité du
  scoring et dépense de tokens sur un skill appelé 20× par cycle).

## Commandes manuelles

```bash
# Score d'un actif isolé
python -m src.signals.composite --asset BTCUSDT

# Score de la shortlist produite par le dernier market-scan
python -m src.signals.composite --from-last-scan

# Debug verbose (composantes + calcul confidence détaillé)
python -m src.signals.composite --asset EURUSD -vv
```
