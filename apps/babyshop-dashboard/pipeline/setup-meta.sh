#!/usr/bin/env bash
#
# One-time setup for the Meta creatives snapshot job.
#
#   apps/babyshop-dashboard/refresh_meta.py   BigQuery (Bluebird warehouse) -> Firestore
#   GET /api/meta                             what the Meta creatives tab reads
#
# ── PREREQUISITE: cross-project BigQuery read (run these ONCE, by hand) ──────
# This is the only job on the legacy dashboard that reads a dataset in ANOTHER
# project: the Meta mart lives in the new stack's warehouse,
# claude-private-499703. The query JOB is billed to project-a7ade44e (where the
# runtime SA already holds bigquery.jobUser), so the SA needs nothing but
# dataViewer on the two kuvio datasets. Grant it as a kuvio owner:
#
#   bq --account=patrik@kuvio.io --project_id=claude-private-499703 \
#      add-iam-policy-binding \
#      --member=serviceAccount:871631085269-compute@developer.gserviceaccount.com \
#      --role=roles/bigquery.dataViewer \
#      claude-private-499703:babyshop_marts
#
#   bq --account=patrik@kuvio.io --project_id=claude-private-499703 \
#      add-iam-policy-binding \
#      --member=serviceAccount:871631085269-compute@developer.gserviceaccount.com \
#      --role=roles/bigquery.dataViewer \
#      claude-private-499703:babyshop_staging
#
# Without both, the job fails with "Access Denied: Table
# claude-private-499703:babyshop_marts.agg_daily_kpis_by_ad".
#
# Note that 871631085269-compute@ is the legacy project's DEFAULT compute SA,
# so this grant gives every Cloud Run job and service in project-a7ade44e read
# access to those two datasets. A dedicated SA would be tighter; that is a
# deliberate open decision, not an oversight.
#
# Otherwise this job only READS BigQuery and writes one Firestore doc, so it
# needs no secrets and no env vars of its own.
#
# Idempotent: re-running updates the job in place (and repoints it at the
# service's CURRENT image — run it after any deploy that touches
# refresh_meta.py, because Cloud Run jobs do not follow the service image).
#
set -euo pipefail

PROJECT="${PROJECT:-project-a7ade44e-e7e3-4871-a83}"
REGION="${REGION:-europe-north1}"
SERVICE="${SERVICE:-babyshop-dashboard}"
# Cloud Scheduler is NOT offered in europe-north1 (pipeline/SETUP-STATUS.md).
SCHEDULER_REGION="${SCHEDULER_REGION:-europe-west1}"
JOB="${JOB:-meta-refresh}"

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
echo "   (that SA needs bigquery.dataViewer on claude-private-499703:babyshop_marts"
echo "    and :babyshop_staging — see the header of this script)"

echo "== 1. Create/update the Cloud Run job =="
ACTION=create
gcloud run jobs describe "$JOB" --project="$PROJECT" --region="$REGION" >/dev/null 2>&1 && ACTION=update
gcloud run jobs "$ACTION" "$JOB" \
  --project="$PROJECT" --region="$REGION" \
  --image="$IMAGE" \
  --service-account="$RUNTIME_SA" \
  --command=python3 --args=refresh_meta.py \
  --task-timeout=30m --memory=1Gi --max-retries=1
echo "   ${ACTION}d $JOB -> python3 refresh_meta.py"

echo "== 2. Daily Cloud Scheduler job =="
# 04:30 Stockholm, after bundles-refresh (04:15) and well after the warehouse's
# own Meta loads (~03:58). DAILY, not weekly, and that is not a cadence
# preference: the creative thumbnails are signed Meta CDN URLs that expire
# about four days after they are minted, so a snapshot has to be re-cut long
# before then or the tab renders rows of broken images.
ACTION=create
gcloud scheduler jobs describe "${JOB}-daily" --project="$PROJECT" \
  --location="$SCHEDULER_REGION" >/dev/null 2>&1 && ACTION=update
gcloud scheduler jobs "$ACTION" http "${JOB}-daily" \
  --project="$PROJECT" \
  --location="$SCHEDULER_REGION" \
  --schedule="30 4 * * *" \
  --time-zone="Europe/Stockholm" \
  --uri="https://run.googleapis.com/v2/projects/${PROJECT}/locations/${REGION}/jobs/${JOB}:run" \
  --http-method=POST \
  --oauth-service-account-email="$RUNTIME_SA" \
  --attempt-deadline=180s
echo "   ${ACTION}d ${JOB}-daily (30 4 * * *) -> $JOB"

cat <<EOF

== 3. First run + smoke test ==
   gcloud run jobs execute ${JOB} --project=${PROJECT} --region=${REGION} --wait

   TOKEN=\$(gcloud auth print-access-token)
   curl -sH "Authorization: Bearer \$TOKEN" \\
     "https://firestore.googleapis.com/v1/projects/${PROJECT}/databases/(default)/documents/funnel_cache/-Ln87GcdqU9CMJV6zMBY__meta" \\
     | head -c 600
EOF
