#!/usr/bin/env bash
# Build and deploy the Gemini relay to Cloud Run (Mumbai).
#
#   relay/deploy.sh "<flash-lite model id>,<gemma model id>"
#
# Needs: gcloud logged in, `terraform apply` done in infra/gcp, and both
# secrets given a value (relay/README.md). Safe to re-run: each run is a new
# Cloud Run revision, and the old one keeps serving until the new one is up.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
MODELS="${1:?usage: relay/deploy.sh \"model-a,model-b\"}"
PROJECT="${GCP_PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
REGION="${REGION:-asia-south1}"
SA="gemini-relay@${PROJECT}.iam.gserviceaccount.com"

python3 "$HERE/test_relay.py" >/dev/null || { echo "relay tests failed; not deploying"; exit 1; }

# --allow-unauthenticated: Cloud Run's own IAM check is off because the app VM
#   isn't a Google identity; the relay checks its own token instead.
# --max-instances 1 / --concurrency 20: a hard ceiling on cost and on abuse.
# --set-secrets: env vars filled from Secret Manager when an instance starts.
# ALLOWED_MODELS goes in an env-vars file so the comma isn't read as a separator.
ENVFILE="$(mktemp)"; trap 'rm -f "$ENVFILE"' EXIT
printf 'ALLOWED_MODELS: "%s"\n' "$MODELS" > "$ENVFILE"

gcloud run deploy gemini-relay \
  --project "$PROJECT" --region "$REGION" \
  --source "$HERE" \
  --service-account "$SA" \
  --allow-unauthenticated \
  --ingress all \
  --max-instances 1 --concurrency 20 \
  --cpu 1 --memory 256Mi --timeout 30 \
  --env-vars-file "$ENVFILE" \
  --set-secrets GEMINI_API_KEY=gemini-api-key:latest,RELAY_TOKEN=relay-token:latest \
  --quiet

URL="$(gcloud run services describe gemini-relay --project "$PROJECT" --region "$REGION" --format 'value(status.url)')"
echo
echo "health: $(curl -s "$URL/health")"
echo
echo "Set on the app VM in /etc/searchbox/api.env:"
echo "  SearchBox__Gemini__Endpoint=$URL/v1beta"
echo "  SearchBox__Gemini__ApiKey=<the relay token, NOT the Gemini key>"
