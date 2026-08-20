#!/usr/bin/env bash
#
# One-time setup for the Bundles snapshot job.
#
#   apps/babyshop-dashboard/refresh_bundles.py   BigQuery (norce) -> Firestore
#   GET /api/bundles                             what the Bundles tab reads
#
# Prereqs: the Norce pipeline from setup-customer-insights.sh is live (norce
# dataset exists, norce-sync runs nightly at 03:00). The refresh job only READS
# BigQuery and writes one Firestore doc — the runtime SA's existing
# bigquery.jobUser + datastore.user cover it.
#
# This script ALSO mounts the Channable feed secret on the norce-sync job:
# sku_titles.image_link (the Bundles tab's product images) is loaded by
# sync_sku_titles(), which silently skips when CHANNABLE_FEED_URL is absent —
# historically the secret was only on the dashboard SERVICE.
#
# Idempotent: re-running updates the jobs in place (and repoints them at the
# service's CURRENT image — run it after any deploy that touches
# refresh_bundles.py or norce_sync.py, because Cloud Run jobs do not follow
# the service image).
#
set -euo pipefail

PROJECT="${PROJECT:-project-a7ade44e-e7e3-4871-a83}"
REGION="${REGION:-europe-north1}"
SERVICE="${SERVICE:-babyshop-dashboard}"
# Cloud Scheduler is NOT offered in europe-north1 (pipeline/SETUP-STATUS.md).
SCHEDULER_REGION="${SCHEDULER_REGION:-europe-west1}"
JOB="${JOB:-bundles-refresh}"

echo "== 0. Resolve the image + runtime SA the dashboard is running =="
IMAGE="$(gcloud run services describe "$SERVICE" \
  --project="$PROJECT" --region="$REGION" \
  --format='value(spec.template.spec.containers[0].image)')"
RUNTIME_SA="$(gcloud run services describe "$SERVICE" \
  --project="$PROJECT" --region="$REGION" \
  --format='value(spec.template.spec.serviceAccountName)')"
if [[ -z "$RUNTIME_SA" ]]; then
  PROJECT_NUMBER="$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')"
  RUNTIME_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
fi
echo "   image:      $IMAGE"
echo "   runtime SA: $RUNTIME_SA"

echo "== 1. Create/update the Cloud Run job =="
ACTION=create
gcloud run jobs describe "$JOB" --project="$PROJECT" --region="$REGION" >/dev/null 2>&1 && ACTION=update
gcloud run jobs "$ACTION" "$JOB" \
  --project="$PROJECT" --region="$REGION" \
  --image="$IMAGE" \
  --service-account="$RUNTIME_SA" \
  --command=python3 --args=refresh_bundles.py \
  --task-timeout=30m --memory=1Gi --max-retries=1
echo "   ${ACTION}d $JOB -> python3 refresh_bundles.py"

echo "== 2. Mount the Channable feed secret on norce-sync (images for bundles) =="
# Without this, sync_sku_titles() logs a skip and image_link stays NULL for
# any SKU added to the feed after the last manual backfill.
gcloud run jobs update norce-sync \
  --project="$PROJECT" --region="$REGION" \
  --set-secrets="CHANNABLE_FEED_URL=channable-feed-url:latest"
echo "   norce-sync now carries CHANNABLE_FEED_URL"

echo "== 3. Nightly Cloud Scheduler job =="
# 04:15 Stockholm: after norce-sync (03:00) and alongside segments-refresh
# (04:00). Not chained — yesterday's Norce data is an acceptable fallback.
ACTION=create
gcloud scheduler jobs describe "${JOB}-nightly" --project="$PROJECT" \
  --location="$SCHEDULER_REGION" >/dev/null 2>&1 && ACTION=update
gcloud scheduler jobs "$ACTION" http "${JOB}-nightly" \
  --project="$PROJECT" \
  --location="$SCHEDULER_REGION" \
  --schedule="15 4 * * *" \
  --time-zone="Europe/Stockholm" \
  --uri="https://run.googleapis.com/v2/projects/${PROJECT}/locations/${REGION}/jobs/${JOB}:run" \
  --http-method=POST \
  --oauth-service-account-email="$RUNTIME_SA" \
  --attempt-deadline=180s
echo "   ${ACTION}d ${JOB}-nightly (15 4 * * *) -> $JOB"

cat <<EOF

== 4. First run + smoke test ==
   gcloud run jobs execute ${JOB} --project=${PROJECT} --region=${REGION} --wait

   TOKEN=\$(gcloud auth print-access-token)
   curl -sH "Authorization: Bearer \$TOKEN" \\
     "https://firestore.googleapis.com/v1/projects/${PROJECT}/databases/(default)/documents/funnel_cache/-Ln87GcdqU9CMJV6zMBY__bundles" \\
     | head -c 600
EOF
