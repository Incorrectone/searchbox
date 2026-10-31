#!/usr/bin/env bash
# Build and deploy the hourly SIEM job to Cloud Run, then run it once.
#
#   siem/deploy.sh          (from the Codespace; safe to re-run)
#
# Needs: `terraform apply` in infra/gcp, gcloud logged in. The API-key secrets
# are optional; whichever has a value is passed to the job (siem/README.md).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
TF="$HERE/../infra/gcp"
PROJECT="${GCP_PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
REGION="$(cd "$TF" && terraform output -raw siem_region)"
BUCKET="$(cd "$TF" && terraform output -raw edge_logs_bucket)"
JOB_SA="$(cd "$TF" && terraform output -raw siem_job_service_account)"
SCHED_SA="$(cd "$TF" && terraform output -raw siem_scheduler_service_account)"

python3 "$HERE/job/test_job.py" >/dev/null || { echo "job tests failed; not deploying"; exit 1; }

# Only pass secrets that actually have a value: Cloud Run refuses to start a job
# whose secret reference has no version.
SECRETS=()
for pair in ABUSEIPDB_KEY=abuseipdb-key IPINFO_TOKEN=ipinfo-token; do
  name="${pair#*=}"
  if [ -n "$(gcloud secrets versions list "$name" --project "$PROJECT" --filter 'state=ENABLED' --format 'value(name)' --limit 1 2>/dev/null)" ]; then
    SECRETS+=("$pair:latest")
  else
    echo "note: secret $name has no value yet; that lookup is skipped"
  fi
done
SECRET_FLAG=()
if [ ${#SECRETS[@]} -gt 0 ]; then
  SECRET_FLAG=(--set-secrets "$(IFS=,; echo "${SECRETS[*]}")")
else
  SECRET_FLAG=(--clear-secrets)
fi

gcloud run jobs deploy siem-job \
  --project "$PROJECT" --region "$REGION" \
  --source "$HERE/job" \
  --service-account "$JOB_SA" \
  --set-env-vars "BUCKET=$BUCKET,DATASET=siem" \
  "${SECRET_FLAG[@]}" \
  --cpu 1 --memory 512Mi \
  --task-timeout 600 --max-retries 1 \
  --quiet

# Only the scheduler's account may start this job.
gcloud run jobs add-iam-policy-binding siem-job \
  --project "$PROJECT" --region "$REGION" \
  --member "serviceAccount:$SCHED_SA" --role roles/run.invoker --quiet >/dev/null

echo "== first run (the scheduler takes over at minute 7 of every hour)"
gcloud run jobs execute siem-job --project "$PROJECT" --region "$REGION" --wait
echo
echo "Logs: Cloud Console -> Cloud Run -> Jobs -> siem-job -> Logs"
echo "      (two JSON lines per run: {\"step\":\"load\",...} and {\"step\":\"enrich\",...})"
