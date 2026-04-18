---
name: market-scan
description: |
  Scan de l'univers d'actifs (FX, crypto, equities) en début de chaque session et
  à la demande. Produit en < 30 s un rapport JSON structuré + snapshot régime
  persisté en SQLite : top opportunités, top risques, actifs ignorés, hint régime
  macro/vol. Consommé par `strategy-selector`, `signal-crossing` et
  `dashboard-builder`.

  DÉCLENCHE CE SKILL quand l'utilisateur demande un scan du marché, un état des
  flux, un top movers, une photo rapide de la journée, un "qu'est-ce qui bouge",
  même sans prononcer le mot "scan". Active aussi sur les triggers : "top
  movers", "panorama marché", "univers aujourd'hui", "ça a l'air calme ou
  agité ?".

triggers:
  - début de session (pre_asia, pre_europe, pre_us, post_us_close)
  - "scan"
  - "market scan"
  - "top movers"
  - "panorama marché"
  - "qu'est-ce qui bouge"

allowed_tools:
  - bash
  - read
  - write

spec_refs:
  - "§5.1 — Collecte OHLCV & normalisation"
  - "§7 — Sources de données (config/sources.yaml)"
  - "§11.2 — Fenêtres d'évitement"
  - "§12 — Détection régime HMM"

budget:
  tokens_per_run: 0            # 100 % Python, pas de LLM
  api_calls_budget: ~30        # 1 appel OHLCV par actif de la shortlist
  wallclock_target_sec: 30

code_paths:
  - src/data/adapters/           # ccxt, kraken_rest, coingecko, oanda, alpaca, yfinance
  - src/signals/market_scan.py   # orchestration du scan
  - src/regime/hmm.py            # hint régime (§12)
---

# market-scan

## Pourquoi ce skill existe

Sans scan préalable, les autres skills travaillent à l'aveugle. `market-scan`
produit la **photo contextuelle** qui nourrit tout le pipeline : régime (risk-on/off,
vol low/mid/high), shortlist des actifs à analyser en détail (pour ne pas
gaspiller du temps ni de l'API budget sur les actifs morts), et contexte
événementiel (fenêtres d'évitement actives).

Résultat : `signal-crossing` ne s'applique plus à tout l'univers mais aux 10-20
actifs qui ont effectivement bougé de façon anormale.

## Contrat de sortie

JSON écrit à `data/analyses/<YYYY-MM-DD>/<session>/market-scan.json` :

```json
{
  "session": "pre_us",
  "ts": "2026-04-17T13:00:00Z",
  "top_opportunities": [
    {"asset":"BTC/USDT", "class":"crypto", "move_pct":4.1, "atr_z":2.1,
     "trend":"up", "note":"breakout range 20j", "vol_ratio_5_20":1.6}
  ],
  "top_risks": [
    {"asset":"EURUSD", "class":"fx", "move_pct":-0.8, "atr_z":2.8,
     "trend":"down", "note":"proche support + FOMC J-1"}
  ],
  "ignored": [
    {"asset":"SPY", "reason":"volume médian 20j < seuil"}
  ],
  "regime_hint": {"macro":"risk_on", "vol":"mid", "hmm_confidence":0.82},
  "avoid_windows_active": ["fomc"],
  "stats": {"scanned":87, "after_filter":23, "duration_sec":18.2}
}
```

## Procédure (5 étapes)

### 1. Chargement univers (§7)
Lit `config/assets.yaml` et `config/sources.yaml`. Filtre sur les classes
demandées (défaut : toutes). Respecte les priorités et budgets API par classe
— si on a déjà consommé 80 % du budget CoinGecko du mois, on bascule sur le
fallback CoinMarketCap.

### 2. Récupération OHLCV
Pour chaque actif : 400 bougies sur le timeframe approprié (H1 pour intra-day,
D1 pour swing). Utilise l'adaptateur en tête de fallbacks (ccxt pour crypto,
oanda pour forex, alpaca pour equities). En cas d'échec : bascule sur le
fallback suivant, log l'incident dans `api_usage` avec `status='fallback'`.

### 3. Calcul métriques (Python pur)
Par actif :
- `move_pct = (close - close_prev) / close_prev`
- `atr_z = (|move_pct| - mean_atr_20) / std_atr_20`
- `vol_ratio_5_20 = vol_ma5 / vol_ma20`
- `trend` (str) : position vs MM20 et MM200 → `up | down | sideways`
- distance aux bandes de Bollinger (20, 2)

### 4. Tri, filtrage, enrichissement
- Top 20 par `|atr_z|`
- Dropper ce qui est dans les `avoid_windows` actives (§11.2)
- Enrichir avec le régime HMM courant (§12)
- Splitter en opportunités (tendance directionnelle claire) et risques (mouvement
  violent contre un support/résistance majeur)

### 5. Persistance
- JSON → `data/analyses/<day>/<session>/market-scan.json`
- Snapshot régime → table SQLite `regime_snapshots` (1 ligne par run, clé
  `ts + session`)
- Entrée log structuré : `slog.event("market_scan_done", n_scanned=..., n_kept=..., regime=...)`

## Critères d'exclusion (§7 fail-closed)

Un actif est mis dans `ignored[]` si :
- Volume médian 20 j sous le seuil défini dans `config/assets.yaml`
- Données manquantes sur les 10 dernières bougies
- Provider primaire ET fallback tous deux en échec → **abort du scan** (pas de
  propositions ce cycle, §2.2 règle d'or)
- Actif dans une fenêtre d'évitement active (§11.2) ET `exclude_during_window: true`
  dans assets.yaml

## Fenêtres d'évitement (§11.2)

Source de vérité : `config/risk.yaml` → `avoid_windows`. Ne jamais hardcoder
les offsets ici — les valeurs actuelles sont fomc [-45,60], cpi_us/nfp/ecb [-30,45],
weekly_close_crypto_sunday [-60,60]. Si une fenêtre est active, elle apparaît
dans `avoid_windows_active[]` et les actifs concernés tombent dans `ignored`.

## Traçabilité SQLite

| Table | Écriture | Usage aval |
|---|---|---|
| `regime_snapshots` | 1 ligne / run | HMM historique, dashboard §14.4 |
| `api_usage` | 1 ligne / appel API | budget dashboard §14.3 |
| `observations` | N lignes si anomalie détectée | feed `self-improve` §13 |

## Modes dégradés

- **> 30 % de l'univers indisponible** → log `ALERT`, mais le scan continue
  sur la shortlist partielle. Le JSON contient `"degraded": true`. Le
  dashboard-builder affiche un badge "scan dégradé".
- **HMM régime pas encore entraîné** (cold-start) → `regime_hint.hmm_confidence`
  à 0.0 et `macro` mis à `"unknown"`. `strategy-selector` retombe sur des
  règles par défaut.
- **Budget API classe crypto dépassé** → skip classe crypto, note dans
  `stats.skipped_classes`. Les autres classes restent scannées.

## Commandes manuelles

```bash
# Scan complet (session par défaut = ad_hoc)
python -m src.signals.market_scan --session ad_hoc

# Scan d'une classe seule
python -m src.signals.market_scan --classes crypto --session ad_hoc

# Dry-run (affiche le JSON sans écrire)
python -m src.signals.market_scan --dry-run
```
