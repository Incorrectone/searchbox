#!/usr/bin/env bash
# Run a crawler command in the locked-down container.
#
#   deploy/run.sh prepare-model           # once: download + quantize the model
#   deploy/run.sh seed --kagi 50          # add domains
#   deploy/run.sh seed --file /data/blogs.txt
#   deploy/run.sh crawl                   # the weekly job (also run by the timer)
#   deploy/run.sh crawl --domain example.com --dry-run
#   deploy/run.sh search "memory leak debugging in rust"
#   deploy/run.sh search --bench /data/queries.txt
#
# crawler/data/ is mounted read-only at /data inside the container.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${ENV_FILE:-$HOME/.antiseo/crawler.env}"
MODELS="${MODELS:-$HOME/.antiseo/models}"
DATA="${DATA:-$HERE/../data}"
PG_IP="${PG_IP:-10.20.3.4}"
IMAGE="${IMAGE:-antiseo-crawler}"

[ -f "$ENV_FILE" ] || { echo "missing $ENV_FILE (see crawler/README.md)"; exit 1; }
mkdir -p "$MODELS"
PGHOST="$(sed -n 's/^PGHOST=//p' "$ENV_FILE")"

# The model folder is read-only, except for the one command that creates it.
MODEL_MOUNT="$MODELS:/models:ro"
[ "${1:-}" = "prepare-model" ] && MODEL_MOUNT="$MODELS:/models"

exec docker run --rm \
  --name "antiseo-${1:-crawl}-$$" \
  --user "$(id -u):$(id -g)" \
  --read-only \
  --tmpfs /tmp:rw,size=256m \
  --cap-drop=ALL \
  --security-opt no-new-privileges \
  --pids-limit 256 \
  --memory 2g \
  --cpus 3 \
  --env-file "$ENV_FILE" \
  --add-host "$PGHOST:$PG_IP" \
  -v "$MODEL_MOUNT" \
  -v "$DATA:/data:ro" \
  "$IMAGE" "$@"
