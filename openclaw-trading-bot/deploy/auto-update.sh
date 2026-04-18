#!/usr/bin/env bash
# =============================================================================
# openclaw-trading-bot — auto-update
# -----------------------------------------------------------------------------
# Wrapper idempotent invoqué par un timer systemd (ou un cron) qui :
#   1. Fait `git fetch origin main`
#   2. Compare HEAD local vs origin/main
#   3. Si différent et working tree propre → fast-forward merge + ./deploy.sh
#   4. Si identique → exit silencieux
#   5. Ping Telegram sur succès/échec si TELEGRAM_BOT_TOKEN présent dans .env
#
# Design : aucune action destructive. Si :
#   - working tree sale        → abort + log WARN (l'opérateur bosse dessus)
#   - branche courante ≠ main  → abort + log WARN
#   - fetch échoue             → abort + log ERR
#   - deploy.sh échoue         → log ERR + ping Telegram SEV:WARN
#                                 (healthcheck de deploy.sh rollback déjà si
#                                 nécessaire ; on ne force rien ici)
#
# Usage manuel :
#   ./deploy/auto-update.sh            # check + deploy si besoin
#   ./deploy/auto-update.sh --check    # check seulement (exit 0 à jour, 10 MAJ dispo)
#   ./deploy/auto-update.sh --force    # redeploy même sans nouveau commit
#
# Codes de retour :
#   0  = rien à faire OU deploy réussi
#   10 = mise à jour disponible (en mode --check)
#   1  = working tree sale / mauvaise branche / fetch KO
#   2  = deploy.sh a échoué
# =============================================================================

set -Eeuo pipefail
IFS=$'\n\t'

readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
readonly REPO_DIR="$(cd -- "${SCRIPT_DIR}/.." &>/dev/null && pwd)"
readonly ENV_FILE="${REPO_DIR}/.env"
readonly LOG_DIR="${REPO_DIR}/data/logs"
readonly LOG_FILE="${LOG_DIR}/auto-update.log"
readonly LOCK_FILE="${REPO_DIR}/.deployment.lock"
readonly TARGET_BRANCH="main"

MODE="run"            # run | check | force
case "${1:-}" in
  --check) MODE="check" ;;
  --force) MODE="force" ;;
  "")      : ;;
  *)       echo "Usage: $0 [--check|--force]" >&2 ; exit 1 ;;
esac

# --- logging ----------------------------------------------------------------
mkdir -p "${LOG_DIR}"
ts() { date -u +'%Y-%m-%dT%H:%M:%SZ'; }
log()  { printf '[%s] [INFO] %s\n'  "$(ts)" "$*" | tee -a "${LOG_FILE}"; }
warn() { printf '[%s] [WARN] %s\n'  "$(ts)" "$*" | tee -a "${LOG_FILE}" >&2; }
err()  { printf '[%s] [ERR]  %s\n'  "$(ts)" "$*" | tee -a "${LOG_FILE}" >&2; }

# --- notifications Telegram (optionnel) -------------------------------------
# Lit TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID depuis .env si présents. Sinon
# no-op. On ne charge JAMAIS .env entièrement dans le shell (risque de
# surcharger PATH, HOME, etc.) — on extrait seulement ces deux vars.
telegram_ping() {
  local msg="$1"
  local token chat
  [[ -r "${ENV_FILE}" ]] || return 0
  token="$(grep -E '^TELEGRAM_BOT_TOKEN=' "${ENV_FILE}" | head -1 | cut -d= -f2- | tr -d '"')" || true
  chat="$(grep  -E '^TELEGRAM_CHAT_ID='   "${ENV_FILE}" | head -1 | cut -d= -f2- | tr -d '"')" || true
  [[ -n "${token:-}" && -n "${chat:-}" ]] || return 0
  curl -fsS -m 10 -o /dev/null \
    -X POST "https://api.telegram.org/bot${token}/sendMessage" \
    --data-urlencode "chat_id=${chat}" \
    --data-urlencode "text=${msg}" \
    --data-urlencode "disable_web_page_preview=true" \
    || warn "telegram ping failed"
}

# --- lock contre runs concurrents (timer + humain) --------------------------
# Lock unique partagé avec deploy.sh (`.deployment.lock`). On le tient pour
# toute la session — `exec` sur fd 200, puis on exporte OPENCLAW_LOCK_HELD=1
# pour que deploy.sh ne retente pas un flock (sinon deadlock).
exec 200>"${LOCK_FILE}"
if command -v flock >/dev/null 2>&1; then
  flock -n 200 || { warn "déploiement déjà en cours (lock: ${LOCK_FILE})"; exit 0; }
fi
export OPENCLAW_LOCK_HELD=1

cd "${REPO_DIR}"

# --- pré-checks -------------------------------------------------------------
if [[ ! -d .git ]]; then
  err "repo git introuvable dans ${REPO_DIR}"
  exit 1
fi

current_branch="$(git rev-parse --abbrev-ref HEAD)"
if [[ "${current_branch}" != "${TARGET_BRANCH}" ]]; then
  warn "branche courante = ${current_branch} (attendu ${TARGET_BRANCH}) — abort auto-update"
  exit 1
fi

if ! git diff --quiet || ! git diff --cached --quiet; then
  warn "working tree sale (modifs non committées) — abort auto-update"
  exit 1
fi

# --- fetch ------------------------------------------------------------------
log "git fetch origin ${TARGET_BRANCH}"
if ! git fetch --prune origin "${TARGET_BRANCH}" 2>>"${LOG_FILE}"; then
  err "git fetch a échoué"
  exit 1
fi

local_sha="$(git rev-parse HEAD)"
remote_sha="$(git rev-parse "origin/${TARGET_BRANCH}")"
short_local="${local_sha:0:7}"
short_remote="${remote_sha:0:7}"

if [[ "${MODE}" != "force" && "${local_sha}" == "${remote_sha}" ]]; then
  log "à jour (${short_local}) — rien à faire"
  exit 0
fi

if [[ "${MODE}" == "check" ]]; then
  log "mise à jour disponible : ${short_local} → ${short_remote}"
  exit 10
fi

# --- pull + deploy ----------------------------------------------------------
if [[ "${local_sha}" != "${remote_sha}" ]]; then
  log "pull fast-forward : ${short_local} → ${short_remote}"
  if ! git merge --ff-only "origin/${TARGET_BRANCH}" >>"${LOG_FILE}" 2>&1; then
    err "ff-merge impossible (history divergence) — intervention humaine requise"
    telegram_ping "🔴 openclaw-trading-bot auto-update KO : history divergence, fix manuel requis"
    exit 1
  fi
else
  log "--force : redeploy sur ${short_local} sans nouveau commit"
fi

log "exec ./deploy.sh"
if ./deploy.sh >>"${LOG_FILE}" 2>&1; then
  new_sha="$(git rev-parse HEAD)"
  log "deploy OK sur ${new_sha:0:7}"
  telegram_ping "🟢 openclaw-trading-bot déployé : ${short_local} → ${new_sha:0:7}"
  exit 0
else
  rc=$?
  err "deploy.sh a échoué (rc=${rc}) — voir ${LOG_FILE}"
  telegram_ping "🔴 openclaw-trading-bot deploy KO (rc=${rc}) sur ${short_remote}. Voir data/logs/auto-update.log"
  exit 2
fi
