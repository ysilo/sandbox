#!/usr/bin/env bash
# scripts/smoke_docker.sh — smoke test Docker complet.
#
# Valide que :
#   1. `docker build` produit une image utilisable
#   2. `docker run ... --smoke` exit 0 (wiring)
#   3. le mode `--serve` boot et répond à /healthz
#
# Pré-requis :
#   - Docker installé
#   - `uv.lock` présent (sinon lancer `uv lock` une fois au préalable)
#
# Utilisation :
#   ./scripts/smoke_docker.sh
#
# Variables d'environnement :
#   IMAGE_TAG  — tag de l'image construite (default: openclaw-smoke:test)
#   PORT       — port host pour le test --serve (default: 8080)
#
# Exit code : 0 = tout OK, non-0 = échec détaillé.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$HERE"

IMAGE_TAG="${IMAGE_TAG:-openclaw-smoke:test}"
PORT="${PORT:-8080}"
CNAME="openclaw-smoke-$$"

cleanup() {
    docker rm -f "$CNAME" >/dev/null 2>&1 || true
}
trap cleanup EXIT

# Pré-flight : uv.lock est requis par le Dockerfile (`uv sync --frozen`).
if [ ! -f uv.lock ]; then
    echo "[smoke-docker] ERREUR : uv.lock absent. Exécuter 'uv lock' d'abord." >&2
    exit 2
fi

echo "[smoke-docker] 1/3 docker build $IMAGE_TAG"
docker build \
    --build-arg OPENCLAW_VERSION="smoke" \
    --build-arg GIT_SHA="$(git rev-parse --short HEAD 2>/dev/null || echo nosha)" \
    -t "$IMAGE_TAG" \
    .

echo "[smoke-docker] 2/3 docker run --smoke"
docker run --rm \
    --entrypoint python \
    "$IMAGE_TAG" \
    -m src.main --smoke --data-dir /tmp/smoke --log-dir /tmp/smoke/logs

echo "[smoke-docker] 3/3 docker run --serve + curl /healthz"
docker run -d \
    --name "$CNAME" \
    -p "$PORT:8080" \
    "$IMAGE_TAG" >/dev/null

# Attente du healthcheck avec timeout.
for i in $(seq 1 30); do
    if curl --fail --silent --max-time 2 "http://127.0.0.1:$PORT/healthz" >/dev/null; then
        echo "[smoke-docker] /healthz OK (après ${i}s)"
        exit 0
    fi
    sleep 1
done

echo "[smoke-docker] ERREUR : /healthz n'a jamais répondu (30s). Logs :" >&2
docker logs "$CNAME" >&2 || true
exit 1
