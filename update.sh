#!/usr/bin/env bash
# Pull the latest CI-built image and restart the WebUI.
# Usage: bash update.sh [tag]
set -euo pipefail
cd "$(dirname "$0")"

TAG="${1:-main}"
IMAGE="ghcr.io/yiiilin/ilab-conjure:${TAG}"

echo "==> Pulling ${IMAGE}"
docker compose -f compose.yaml -f compose.prod.yml pull app 2>&1 || {
  # compose.prod.yml pins :main; retag when a different tag was requested
  sed -i "s|ghcr.io/yiiilin/ilab-conjure:[^ ]*|${IMAGE}|" compose.prod.yml
  docker compose -f compose.yaml -f compose.prod.yml pull app
}

echo "==> Restarting"
docker compose -f compose.yaml -f compose.prod.yml up -d

echo "==> Health check"
for i in $(seq 1 12); do
  if curl -sf "http://127.0.0.1:${ILAB_HOST_PORT:-13221}/api/version" >/dev/null 2>&1 || \
     curl -sf "http://127.0.0.1:${ILAB_HOST_PORT:-13221}/" >/dev/null 2>&1; then
    echo "OK: ilab-conjure is up"
    exit 0
  fi
  sleep 2
done
echo "WARN: health check timed out — check container logs" >&2
exit 1
