#!/usr/bin/env bash
# =============================================================================
# openclaw-trading-bot — déploiement en une commande
# -----------------------------------------------------------------------------
# Usage :
#   ./deploy.sh                              # premier déploiement / mise à jour
#   ./deploy.sh --openclaw-version=1.2.0     # embed un tag logique OpenClaw
#   ./deploy.sh --rollback                   # revient à l'image précédente
#   ./deploy.sh --dry-run                    # affiche les étapes sans rien lancer
#   ./deploy.sh --skip-build                 # reprend avec l'image existante
#   ./deploy.sh --help                       # cette aide
#
# Pré-requis sur le VPS :
#   - docker (engine + plugin compose v2)
#   - git (optionnel, pour tagger l'image par SHA)
#   - un fichier .env renseigné à côté de ce script (voir .env.example)
# =============================================================================

set -Eeuo pipefail
IFS=$'\n\t'

# Trap pour un diagnostic utile en cas d'erreur non interceptée.
# `set -E` fait hériter le trap aux fonctions/subshells, BASH_COMMAND donne
# la commande fautive.
trap 'rc=$?; printf "\n\033[31m[ERR]\033[0m Échec ligne %d (rc=%d) : %s\n" "$LINENO" "$rc" "$BASH_COMMAND" >&2; exit $rc' ERR

# --- Configuration ------------------------------------------------------------
readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
readonly COMPOSE_FILE="${SCRIPT_DIR}/docker-compose.yml"
readonly ENV_FILE="${SCRIPT_DIR}/.env"
readonly ENV_EXAMPLE="${SCRIPT_DIR}/.env.example"
readonly DATA_DIR="${SCRIPT_DIR}/data"
readonly KILL_FILE="${DATA_DIR}/KILL"
readonly LOCK_FILE="${SCRIPT_DIR}/.deployment.lock"
readonly NETWORK_NAME="trading_net"
readonly SERVICE_NAME="trading-bot"
readonly IMAGE_NAME="openclaw-trading-bot"
readonly PROJECT_NAME="openclaw-trading-bot"
readonly BOT_UID=10001
readonly BOT_GID=10001
readonly IMAGE_RETENTION=3   # garde les N dernières images SHA/timestamp lors de --prune

# Sérialise les invocations concurrentes (humain + timer auto-update + reboot
# pendant deploy). Un seul lock à la racine `.deployment.lock` partagé entre
# deploy.sh et auto-update.sh — si auto-update.sh nous appelle, il tient déjà
# le lock et nous passe OPENCLAW_LOCK_HELD=1 pour éviter le deadlock.
if [[ "${OPENCLAW_LOCK_HELD:-0}" != "1" ]]; then
  # Le fd 200 reste ouvert jusqu'à la fin du script → lock libéré à l'exit.
  exec 200>"${LOCK_FILE}"
  if command -v flock >/dev/null 2>&1; then
    flock -n 200 || { echo "Un autre déploiement est en cours (lock: ${LOCK_FILE})" >&2; exit 1; }
  fi
fi

# Couleurs (désactivées si stdout non-tty)
if [[ -t 1 ]]; then
  readonly C_RED=$'\033[31m' C_GREEN=$'\033[32m' C_YELLOW=$'\033[33m' C_BLUE=$'\033[34m' C_RESET=$'\033[0m'
else
  readonly C_RED='' C_GREEN='' C_YELLOW='' C_BLUE='' C_RESET=''
fi

log()  { printf '%s[%s]%s %s\n' "${C_BLUE}"  "$(date -u +'%H:%M:%S')" "${C_RESET}" "$*"; }
ok()   { printf '%s[OK]%s %s\n'  "${C_GREEN}" "${C_RESET}" "$*"; }
warn() { printf '%s[WARN]%s %s\n' "${C_YELLOW}" "${C_RESET}" "$*" >&2; }
die()  { printf '%s[ERR]%s %s\n'  "${C_RED}"   "${C_RESET}" "$*" >&2; exit 1; }

usage() {
  cat <<'USAGE'
openclaw-trading-bot — deploy.sh

Usage:
  ./deploy.sh [--openclaw-version=VERSION] [--skip-build] [--dry-run]
  ./deploy.sh --rollback
  ./deploy.sh --help

Options:
  --openclaw-version=VERSION   Tag logique OpenClaw (défaut: latest).
                               Exporté dans l'image (LABEL + ENV) et utilisé
                               comme tag annexe de l'image Docker.
  --skip-build                 Ne reconstruit pas l'image, repart de l'existante.
  --refresh-base               Force `docker build --pull` (MAJ python:3.12-slim).
                               Sans ce flag, le build utilise l'image de base en cache.
  --prune                      Après le up healthy, supprime les vieilles images
                               (ne garde que les ${IMAGE_RETENTION:-3} dernières) + dangling layers.
  --dry-run                    Affiche les commandes sans les exécuter.
  --rollback                   Redémarre sur l'image précédente (tag SHA/timestamp).
  -h, --help                   Cette aide.

Fichiers attendus à côté du script:
  docker-compose.yml
  Dockerfile
  .env            (copier depuis .env.example et renseigner)
  config/         (monté en read-only dans le container)
  data/           (créé si absent, monté en read-write)

USAGE
}

# --- Parsing des arguments ----------------------------------------------------
DRY_RUN=0
ROLLBACK=0
SKIP_BUILD=0
REFRESH_BASE=0
PRUNE=0
# Résolution OPENCLAW_VERSION : flag > var d'env > defaut "latest"
# (on capture la valeur d'env AVANT tout écrasement)
OPENCLAW_VERSION="${OPENCLAW_VERSION:-latest}"

for arg in "$@"; do
  case "$arg" in
    --dry-run)      DRY_RUN=1 ;;
    --rollback)     ROLLBACK=1 ;;
    --skip-build)   SKIP_BUILD=1 ;;
    --refresh-base) REFRESH_BASE=1 ;;
    --prune)        PRUNE=1 ;;
    --openclaw-version=*) OPENCLAW_VERSION="${arg#*=}" ;;
    --openclaw-version)   die "--openclaw-version requiert une valeur (ex: --openclaw-version=1.2.0)" ;;
    -h|--help)      usage; exit 0 ;;
    *)              usage >&2; die "Argument inconnu : $arg" ;;
  esac
done

# Sanitize : autorise uniquement [A-Za-z0-9._-]
if [[ ! "${OPENCLAW_VERSION}" =~ ^[A-Za-z0-9._-]+$ ]]; then
  die "OPENCLAW_VERSION invalide : '${OPENCLAW_VERSION}' (autorisé : A-Z a-z 0-9 . _ -)"
fi
export OPENCLAW_VERSION

run() {
  if (( DRY_RUN )); then
    printf '  %s$%s %s\n' "${C_YELLOW}" "${C_RESET}" "$*"
  else
    "$@"
  fi
}

# Alias pratique pour docker compose avec projet + fichier nommés explicitement
dc() {
  run docker compose --project-name "${PROJECT_NAME}" --file "${COMPOSE_FILE}" "$@"
}

# =============================================================================
# 1. Pré-checks
# =============================================================================
log "Étape 1/8 — Vérifications préalables"

command -v docker >/dev/null || die "docker n'est pas installé"
docker compose version >/dev/null 2>&1 \
  || die "plugin 'docker compose' manquant — installer docker-compose-plugin"

# build.tags dans docker-compose.yml requiert Compose >= 2.13.
# Parsing robuste : strip 'v' initial (certains wrappers le préfixent),
# extraction major/minor par regex (fallback sûr avec set -u).
compose_ver="$(docker compose version --short 2>/dev/null || echo '0.0.0')"
compose_ver="${compose_ver#v}"
if [[ ! "${compose_ver}" =~ ^([0-9]+)\.([0-9]+) ]]; then
  die "Impossible de parser la version Compose : '${compose_ver}'"
fi
cv_major="${BASH_REMATCH[1]}"
cv_minor="${BASH_REMATCH[2]}"
if (( cv_major < 2 )) || { (( cv_major == 2 )) && (( cv_minor < 13 )); }; then
  die "docker compose ${compose_ver} trop ancien — il faut >= 2.13 (build.tags)"
fi

# Tag d'image — SHA git si possible, sinon timestamp pour garantir l'unicité
# (nécessaire pour que --rollback puisse cibler une image précédente).
if command -v git >/dev/null 2>&1 && [[ -d "${SCRIPT_DIR}/.git" ]]; then
  GIT_SHA="$(git -C "${SCRIPT_DIR}" rev-parse --short=12 HEAD 2>/dev/null || true)"
fi
if [[ -z "${GIT_SHA:-}" ]]; then
  GIT_SHA="build-$(date -u +%Y%m%d-%H%M%S)"
  warn "Pas de repo git — tag de build fallback : ${GIT_SHA}"
fi
export GIT_SHA

# .env
[[ -f "${ENV_FILE}" ]] || die ".env manquant. Copier ${ENV_EXAMPLE} → .env et remplir."

# Fichiers & dossiers requis par le build / le runtime
for required_file in Dockerfile requirements.txt pyproject.toml README.md; do
  [[ -f "${SCRIPT_DIR}/${required_file}" ]] \
    || die "${required_file} manquant à la racine — requis pour le build Docker"
done
for required_dir in src config skills; do
  [[ -d "${SCRIPT_DIR}/${required_dir}" ]] \
    || die "Dossier ${required_dir}/ manquant — requis pour le build Docker"
done

# Vérifie les variables critiques sans les logger. Détecte aussi les
# placeholders évidents (valeur vide, "changeme", "REPLACE_ME", "sk-ant-..."
# comme suffixe de point de suspension).
missing=()
placeholder_re='^(changeme|change_me|replace[_-]?me|xxx+|todo|your[_-].*|sk-ant-\.\.\.)$'
for var in ANTHROPIC_API_KEY TELEGRAM_BOT_TOKEN TELEGRAM_CHAT_ID TELEGRAM_WEBHOOK_SECRET DASHBOARD_BASIC_AUTH_PASS; do
  val="$(grep -E "^${var}=" "${ENV_FILE}" 2>/dev/null | head -n1 | cut -d= -f2- | tr -d '"' | tr -d "'" || true)"
  if [[ -z "${val}" ]]; then
    missing+=("${var} (vide)")
  elif [[ "${val,,}" =~ ${placeholder_re} ]]; then
    missing+=("${var} (placeholder)")
  elif (( ${#val} < 8 )); then
    missing+=("${var} (trop court : ${#val} chars)")
  fi
done

if (( ${#missing[@]} > 0 )); then
  die "Variables .env non renseignées ou invalides : ${missing[*]}"
fi

# Permissions .env — doit être chmod 600
chmod 600 "${ENV_FILE}" 2>/dev/null || warn "Impossible de chmod 600 .env (ignoré)"

# Horloge VPS — critique pour le trading (agrégation OHLC, rate-limits API,
# timestamps APScheduler). Warn non bloquant — on laisse l'opérateur décider.
if command -v timedatectl >/dev/null 2>&1; then
  ntp_status="$(timedatectl show --property=NTPSynchronized --value 2>/dev/null || echo 'unknown')"
  if [[ "${ntp_status}" != "yes" ]]; then
    warn "Horloge VPS non synchronisée NTP (statut=${ntp_status}) — risque de drift sur les décisions time-sensitive"
    warn "Activer : sudo timedatectl set-ntp true"
  fi
fi

# Docker daemon activé au boot (systemd uniquement)
if command -v systemctl >/dev/null 2>&1; then
  if ! systemctl is-enabled docker >/dev/null 2>&1; then
    warn "dockerd n'est pas enable au boot — le bot ne redémarrera pas après un reboot VPS"
    warn "Activer : sudo systemctl enable docker"
  fi
fi

# Validation syntaxique docker-compose (attrape les erreurs avant le build)
if (( DRY_RUN == 0 )); then
  docker compose --project-name "${PROJECT_NAME}" --file "${COMPOSE_FILE}" config --quiet \
    || die "docker-compose.yml invalide — voir 'docker compose config'"
fi

ok "Pré-checks OK — sha=${GIT_SHA} — openclaw=${OPENCLAW_VERSION}"

# =============================================================================
# 2. Dossiers & fichiers côté hôte
# =============================================================================
log "Étape 2/8 — Dossiers persistants"

run mkdir -p "${DATA_DIR}"/{cache,logs,analyses,simulation}

# KILL vit désormais sous data/ (même volume que le reste). Le container
# vérifie son existence via KILL_FILE_PATH=/app/data/KILL. Déploiement =
# bot actif → on s'assure que data/KILL N'EXISTE PAS.
if [[ -d "${KILL_FILE}" ]]; then
  die "${KILL_FILE} est un DOSSIER — corriger : rmdir '${KILL_FILE}'"
fi
if [[ -e "${KILL_FILE}" ]]; then
  warn "KILL-switch présent (${KILL_FILE}) — le bot restera gelé au démarrage"
  warn "Pour activer le bot : rm '${KILL_FILE}'"
fi

# Ownership pour que le user 10001:10001 dans le container puisse écrire.
# Si on n'est pas root, on ne peut pas chown : on vérifie au moins que
# data/ appartient déjà au bon UID, sinon le container échouera à écrire.
if [[ $EUID -eq 0 ]]; then
  run chown -R "${BOT_UID}:${BOT_GID}" "${DATA_DIR}"
else
  if (( DRY_RUN == 0 )); then
    current_uid="$(stat -c '%u' "${DATA_DIR}" 2>/dev/null || echo '?')"
    if [[ "${current_uid}" != "${BOT_UID}" && "${current_uid}" != "?" ]]; then
      die "Pas root et ${DATA_DIR} appartient à uid=${current_uid} (attendu ${BOT_UID}). Lancer une fois : sudo chown -R ${BOT_UID}:${BOT_GID} '${DATA_DIR}'"
    fi
  fi
  warn "Pas root — ownership data/ supposé déjà aligné sur uid ${BOT_UID}"
fi

ok "Dossiers prêts"

# =============================================================================
# 3. Réseau Docker partagé
# =============================================================================
log "Étape 3/8 — Réseau Docker ${NETWORK_NAME}"

if ! docker network inspect "${NETWORK_NAME}" >/dev/null 2>&1; then
  run docker network create "${NETWORK_NAME}"
  ok "Réseau ${NETWORK_NAME} créé"
else
  ok "Réseau ${NETWORK_NAME} déjà présent"
fi

# =============================================================================
# 4. Rollback express (si demandé)
# =============================================================================
if (( ROLLBACK )); then
  log "Mode --rollback"
  # Liste les images triées par CreatedAt desc (du plus récent au plus ancien).
  # On ignore latest / <none> / v-* (tags logiques OpenClaw, pas des SHAs).
  # tag_list[0] = image courante, tag_list[1] = image précédente.
  mapfile -t tag_list < <(
    docker images "${IMAGE_NAME}" --format '{{.CreatedAt}}|{{.Tag}}' \
      | sort -r \
      | cut -d'|' -f2 \
      | grep -vE '^(latest|<none>|v-.*)$' \
      | awk '!seen[$0]++'
  )
  if (( ${#tag_list[@]} < 2 )); then
    die "Rollback impossible : il faut au moins 2 images ${IMAGE_NAME} taguées par SHA ou timestamp (trouvées : ${#tag_list[@]})"
  fi
  prev_tag="${tag_list[1]}"
  warn "Rollback vers ${IMAGE_NAME}:${prev_tag} (courante : ${tag_list[0]})"
  # Sanity-check : l'image existe encore et est importable
  if (( DRY_RUN == 0 )); then
    docker image inspect "${IMAGE_NAME}:${prev_tag}" >/dev/null 2>&1 \
      || die "Image ${IMAGE_NAME}:${prev_tag} introuvable localement"
  fi
  export GIT_SHA="${prev_tag}"
  SKIP_BUILD=1
fi

# =============================================================================
# 5. Build (sauf --skip-build / --rollback)
# =============================================================================
if (( SKIP_BUILD == 0 )); then
  log "Étape 4/8 — Build image ${IMAGE_NAME}:${GIT_SHA} (openclaw=${OPENCLAW_VERSION})"
  build_args=(
    --build-arg "OPENCLAW_VERSION=${OPENCLAW_VERSION}"
    --build-arg "GIT_SHA=${GIT_SHA}"
  )
  if (( REFRESH_BASE )); then
    log "  → --refresh-base : pull de python:3.12-slim"
    build_args+=(--pull)
  fi
  dc build "${build_args[@]}"
  ok "Image buildée"
else
  log "Étape 4/8 — Build sauté (--skip-build ou --rollback)"
fi

# =============================================================================
# 6. Init SQLite — seulement si la base n'existe pas
# =============================================================================
log "Étape 5/8 — Initialisation SQLite"

if [[ ! -f "${DATA_DIR}/memory.db" ]]; then
  dc run --rm --entrypoint="" "${SERVICE_NAME}" \
      python -m src.memory.store --init
  ok "memory.db initialisée"
else
  ok "memory.db déjà présente — pas de ré-init"
fi

# =============================================================================
# 7. Up (en arrière-plan, remplace le container courant proprement)
# =============================================================================
log "Étape 6/8 — Démarrage du service"

dc up -d --remove-orphans

# =============================================================================
# 8. Healthcheck actif — attente jusqu'à 60 s
# =============================================================================
log "Étape 7/8 — Attente healthcheck"

if (( DRY_RUN )); then
  ok "Dry-run — healthcheck sauté"
else
  # Récupère le vrai ID du container via compose (robuste au renommage)
  cid="$(docker compose --project-name "${PROJECT_NAME}" --file "${COMPOSE_FILE}" ps -q "${SERVICE_NAME}" 2>/dev/null || true)"
  [[ -n "${cid}" ]] || die "Container ${SERVICE_NAME} introuvable après 'up -d'"

  status="starting"
  # 90s = start_period (20s) + 2× interval (30s) + marge. Suffisant pour
  # que le healthcheck passe au moins une fois.
  deadline=$(( SECONDS + 90 ))
  while (( SECONDS < deadline )); do
    status="$(docker inspect --format '{{.State.Health.Status}}' "${cid}" 2>/dev/null || echo 'starting')"
    case "${status}" in
      healthy)   ok "Container ${SERVICE_NAME} healthy"; break ;;
      unhealthy) die "Container ${SERVICE_NAME} unhealthy — voir 'docker compose logs ${SERVICE_NAME}'" ;;
      *)         printf '.'; sleep 2 ;;
    esac
  done
  printf '\n'
  if [[ "${status}" != "healthy" ]]; then
    die "Timeout healthcheck (90 s, dernier état : ${status}) — voir 'docker compose logs ${SERVICE_NAME}'"
  fi
fi

# =============================================================================
# 8b. Rétention d'images — ne prune qu'après un healthcheck OK
# =============================================================================
if (( PRUNE )) && (( DRY_RUN == 0 )); then
  log "Rétention — garde les ${IMAGE_RETENTION} dernières images SHA/timestamp"
  # Liste les tags triés du plus récent au plus ancien, exclut latest/v-*/<none>
  mapfile -t all_tags < <(
    docker images "${IMAGE_NAME}" --format '{{.CreatedAt}}|{{.Tag}}' \
      | sort -r \
      | cut -d'|' -f2 \
      | grep -vE '^(latest|<none>|v-.*)$' \
      | awk '!seen[$0]++'
  )
  # Supprime tout ce qui dépasse IMAGE_RETENTION (hors image courante)
  if (( ${#all_tags[@]} > IMAGE_RETENTION )); then
    for old_tag in "${all_tags[@]:IMAGE_RETENTION}"; do
      # Ne jamais supprimer le tag qui vient d'être déployé
      [[ "${old_tag}" == "${GIT_SHA}" ]] && continue
      log "  → rmi ${IMAGE_NAME}:${old_tag}"
      docker rmi "${IMAGE_NAME}:${old_tag}" 2>/dev/null || warn "échec rmi ${old_tag} (image encore référencée ?)"
    done
  fi
  # Nettoie les couches dangling (layers sans tag)
  docker image prune -f >/dev/null 2>&1 || true
  ok "Rétention appliquée"
fi

# =============================================================================
# 9. Rappel Caddy — détection + vérif d'appartenance à trading_net
# =============================================================================
log "Étape 8/8 — Caddy"
if (( DRY_RUN == 0 )); then
  caddy_name="$(docker ps --format '{{.Names}}' | grep -E '^caddy(-[0-9]+)?$' | head -n1 || true)"
  if [[ -n "${caddy_name}" ]]; then
    # Vérifie que Caddy et trading-bot partagent trading_net
    net_members="$(docker network inspect "${NETWORK_NAME}" --format '{{range .Containers}}{{.Name}} {{end}}' 2>/dev/null || true)"
    if ! grep -qwE "${caddy_name}" <<< "${net_members}"; then
      warn "${caddy_name} n'est PAS connecté à ${NETWORK_NAME} — DNS trading-bot:8080 échouera"
      warn "Corriger : docker network connect ${NETWORK_NAME} ${caddy_name}"
    else
      ok "${caddy_name} connecté à ${NETWORK_NAME}"
    fi
    warn "Pense à recharger Caddy si le Caddyfile a changé :"
    printf '      docker exec %s caddy reload --config /etc/caddy/Caddyfile\n' "${caddy_name}"
  else
    warn "Aucun container Caddy détecté — vérifier manuellement le reverse proxy"
  fi
fi

echo
ok "Déploiement terminé (sha=${GIT_SHA}, openclaw=${OPENCLAW_VERSION})"
printf '   Logs   : docker compose -p %s logs -f\n'   "${PROJECT_NAME}"
printf '   Status : docker compose -p %s ps\n'        "${PROJECT_NAME}"
printf '   Kill   : touch %s\n'                       "${KILL_FILE}"
printf '   Unkill : rm %s\n'                          "${KILL_FILE}"
