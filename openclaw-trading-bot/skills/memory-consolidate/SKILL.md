---
name: memory-consolidate
description: |
  Pipeline nocturne de maintenance mémoire. Tourne chaque nuit à 02:00 UTC :
  purge la télémétrie > 180 j, archive les leçons stériles (confidence < 0.40 non
  confirmée depuis 90 j), fusionne les doublons sémantiques (cosinus > 0.92 via
  FAISS + fastembed), promeut les leçons mûres au tag `stable` (confidence > 0.8
  ET âge > 30 j), reconstruit l'index FAISS, et régénère la façade humaine
  MEMORY.md. Source de vérité = SQLite ; MEMORY.md n'est qu'un rendu lisible.

  DÉCLENCHE CE SKILL quand l'utilisateur demande une consolidation mémoire, un
  nettoyage des leçons, une fusion de doublons, un rebuild de l'index
  sémantique, ou une régénération de MEMORY.md — même s'il ne cite pas le nom
  du skill. Déclenche aussi sur les formules : "consolide la mémoire", "fais le
  ménage dans les leçons", "rebuilde l'index".

triggers:
  - cron nocturne
  - "consolide la mémoire"
  - "memory consolidate"
  - "nettoie les leçons"
  - "rebuild index FAISS"

allowed_tools:
  - read
  - write
  - bash

spec_refs:
  - "§10.3 — MEMORY.md façade humaine"
  - "§10.4 — Index FAISS (fastembed + bge-small-en-v1.5)"
  - "§10.5 — Stratification prompt + cache"
  - "§10.6 — Pipeline consolidation (référence canonique)"

schedule:
  cron: "0 2 * * *"             # chaque nuit 02:00 UTC (config/schedules.yaml)
  timezone: UTC

budget:
  tokens_per_run: 0             # 100 % déterministe + embeddings locaux, pas de LLM
  wallclock_target_sec: 30      # < 500 leçons : rebuild FAISS < 1 s

code_paths:
  - src/memory/consolidator.py        # orchestration pipeline (6 étapes)
  - src/memory/lesson_index.py        # FAISS + fastembed
  - src/memory/markdown_exporter.py   # régénération MEMORY.md (façade humaine)

constants:
  similarity_merge_threshold: 0.92       # cosinus pour fusion doublons
  low_confidence_archive: 0.40           # leçons stériles
  stale_low_confidence_days: 90          # non-confirmées depuis N jours
  telemetry_retention_days: 180          # llm_usage, api_usage
  stable_tag_threshold_confidence: 0.80  # promotion L2 (§10.5)
  stable_tag_min_age_days: 30
---

# memory-consolidate

## Pourquoi ce skill existe

Sans maintenance, la mémoire dérive :
- les leçons douteuses restent dans le contexte et ajoutent du bruit (perte de
  signal dans le prompt LLM, §10.5)
- les doublons quasi-identiques coûtent des tokens sans valeur ajoutée
- la télémétrie `llm_usage` / `api_usage` grossit sans borne (coût disque +
  requêtes dashboard plus lentes)
- MEMORY.md, la façade humaine (§10.3), devient illisible

Ce skill tourne une fois par nuit, 100 % déterministe, 0 token LLM. Il utilise
les embeddings locaux `fastembed` + `BAAI/bge-small-en-v1.5` (§10.4) pour la
détection de doublons — aucune dépendance externe au-delà de `faiss-cpu`.

## Pipeline (6 étapes — §10.6)

Chaque étape mutante tourne dans sa propre transaction SQL. Un run sans rien à
consolider (cas du cold-start) n'effectue aucune mutation : pipeline idempotent.

### 1. Purge télémétrie (§14.5.1)
Supprime de `llm_usage` et `api_usage` toutes les lignes avec
`ts < now() - 180 jours`. C'est purement rétentionnel : les métriques
agrégées quotidiennes restent dans `performance_metrics` à granularité jour,
donc on ne perd pas d'info historique utile.

### 2. Archivage des leçons stériles
`UPDATE lessons SET archived = 1 WHERE confidence < 0.40 AND date < now() - 90 j`.
Une leçon à faible confiance qui ne se confirme pas sur 90 j n'aidera
probablement jamais — mieux vaut la sortir du retrieval que la garder comme
bruit. Elle reste en base (pour audit) mais n'apparaît plus dans FAISS ni
dans les prompts L2/L3.

### 3. Fusion des doublons
Recalcule les embeddings de toutes les leçons actives, construit la matrice de
similarité `sims = embs @ embs.T` (cosinus, vecteurs bge L2-normalisés), et
pour chaque paire `(i, j)` avec `sims[i,j] > 0.92` :
- garde la plus confiante
- archive l'autre en ajoutant le tag `merged_into:<winner_id>` (JSON append)

Le tag permet un éventuel audit / rollback et une traçabilité dans le rendu
MEMORY.md ("cette leçon en remplace 2 autres").

### 4. Promotion `stable` (consommé par PromptBuilder §10.5 L2)
`UPDATE lessons ... SET tags = json_insert(tags, '$[#]', 'stable')` pour les
leçons matures : `confidence > 0.8 AND âge > 30 j`. Le guard
`tags NOT LIKE '%"stable"%'` évite le double-tag. Ces leçons composent la
couche L2 du prompt stratifié — mises en cache Anthropic `ephemeral`.

### 5. Rebuild index FAISS (§10.4)
`LessonIndex.rebuild()` :
- lit `lessons WHERE archived = 0`
- recalcule embeddings via fastembed
- reconstruit `IndexFlatIP` (exact cosine, < 1 ms à < 500 leçons)
- persiste `data/lesson_index.faiss` + `data/lesson_index.meta.json`

Hors transaction SQL car c'est un effet de bord fichier.

### 6. Régénération MEMORY.md (§10.3)
Délègue à `MarkdownExporter(self.db).export()`. La façade produite n'est plus
injectée dans les prompts LLM (c'était la dette §10 avant §10.4-10.5) — elle
existe uniquement pour la revue humaine. Donc sa mise à jour est best-effort :
si `MarkdownExporter` n'est pas encore implémenté, le pipeline log un warning
et continue.

## Garde-fous

- **Jamais de `DELETE` sur `lessons` ou `trades`.** Seulement `archived = 1`.
  L'historique doit rester auditable — la purge concerne uniquement
  `llm_usage` / `api_usage` qui sont des tables télémétrie, pas des sources
  de décision.
- **Transaction SQL globale pour les 4 étapes mutantes.** En cas d'échec
  (disque plein, corruption), rollback complet : la mémoire reste dans un
  état cohérent. Le rebuild FAISS et la régénération markdown sont hors
  transaction (fichiers uniquement).
- **Idempotent.** Un run sans nouvelles leçons/doublons/leçons mûres ne
  produit aucune mutation — on peut re-run autant que nécessaire.

## Notification

Silence par défaut (§14.6). Un résumé Telegram est envoyé uniquement si :
- `ConsolidationReport.has_mutations()` est vrai (purge > 0 OU archive > 0 OU
  merge > 0 OU promotion > 0)
- OU si le pipeline lève une exception (alerte ERR)

Format du résumé :
```
🧹 memory-consolidate 2026-04-17 02:00 UTC
  purge=123, archive=4, merge=2, promote=7, index=87
```

## Commandes manuelles

```bash
# Dry-run : n'écrit rien, mais loggue ce qui serait fait
python -m src.memory.consolidator --dry-run

# Run complet (équivalent du cron)
python -m src.memory.consolidator --run

# Force un rebuild FAISS seul (après modif schéma embedder)
python -c "from src.memory.lesson_index import LessonIndex; \
           LessonIndex(db, 'data/lesson_index.faiss').rebuild()"
```

## Interaction avec les autres skills

- **`self-improve`** (§13) : à chaque run hebdo, crée des hypothèses et des
  leçons. `memory-consolidate` nettoie les hypothèses rejetées > 90 j et
  archive les leçons `self_improve` à faible confiance.
- **`market-scan`, `signal-crossing`, `news-pulse`** : écrivent des leçons
  intra-cycle (observations, anomalies). Ce skill gère leur consolidation à
  long terme.
- **`dashboard-builder`** : lit la sortie de la consolidation pour afficher
  les indicateurs "santé mémoire" dans le panel §14.4.

## Modes dégradés

- **Base SQLite verrouillée** (autre transaction en cours) → retry 3× avec
  backoff 5 s, puis abort propre + alerte Telegram. Le run du lendemain
  rattrapera.
- **`fastembed` indisponible / modèle corrompu** → skip étapes 3 + 5, continue
  le reste (purge + archive + promote). L'index FAISS sera reconstruit au
  prochain cycle `full_analysis` qui en aura besoin.
- **Disque plein** → le rebuild FAISS échouera ; l'état SQLite sera déjà
  consolidé (transaction commited). Alerte Telegram mais pas d'inconsistance
  — le `_ensure_loaded()` de `LessonIndex` rebuilde à la volée au prochain
  `query()`.
