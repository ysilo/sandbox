# MEMORY_INDEX.md

Index humain des IDs persistés en SQLite. **Régénéré par
[`memory-consolidate`](skills/memory-consolidate/SKILL.md)** chaque nuit
à 02:00 UTC (§10.6) — ne pas éditer à la main.

Source de vérité : `data/memory.db`. Ce fichier n'est qu'une vue aplatie
pour les recherches rapides côté opérateur humain.

## Conventions d'IDs

| Préfixe | Table SQL | Exemple | Description |
|---|---|---|---|
| `H-xxx` | `hypotheses` | `H-012` | Hypothèse émise par `self-improve` |
| `L-xxxx` | `lessons` | `L-0042` | Leçon apprise (append-only) |
| `T-xxxxxx` | `trades` | `T-000158` | Trade simulé ou réel (append-only) |
| `B-xx` | `observations` (`tag='bias'`) | `B-03` | Biais détecté (revue lundi) |
| `TD-xx` | `observations` (`tag='todo'`) | `TD-07` | TODO d'auto-amélioration |
| `P-xxx` | `patches_pending` | `P-017` | Patch self-improve en attente PR |

## Hypothèses (H-xxx)

_Aucune — `self-improve` en produira après les 50 premiers trades paper._

## Leçons (L-xxxx)

- `L-0000` — cold-start — `system` — Initialisation du bot

## Trades (T-xxxxxx)

_Aucun._

## Biais (B-xx)

_Aucun — enrichi par `self-improve` à partir des divergences
LLM vs déterministe (§13.2 étape 3)._

## TODOs (TD-xx)

_Indexation automatique au prochain passage de `memory-consolidate`._

## Patches (P-xxx)

_Aucun — voir `IMPROVEMENTS_PENDING.md` pour les propositions actives
en attente de merge._
