#!/usr/bin/env bash
#
# One-time setup for the Executive P&L snapshot job.
#
#   apps/babyshop-dashboard/refresh_exec_pl.py   BigQuery (BC raw, cross-project) -> Firestore
#   GET /api/exec-pl                             what the Executive P&L tab reads
#
# ── PREREQUISITE: cross-project BigQuery read (ALREADY IN PLACE) ─────────────
# The BC tables live in the NEW stack's warehouse, claude-private-499703,
# dataset babyshop_raw, as bc_*. The query JOB is billed to project-a7ade44e
# (where the runtime SA already holds bigquery.jobUser), so the SA needs only
# READ on that one dataset.
#
# That grant already exists. The Meta creatives tab put
#   871631085269-compute@developer.gserviceaccount.com
# on claude-private-499703:babyshop_raw as READER (see setup-meta.sh, which
# granted babyshop_marts, babyshop_staging AND babyshop_raw). Every Cloud Run
# job and the dashboard service itself run as that same SA, so nothing new was
# needed for this tab. Verify with:
#
#   export CLOUDSDK_CORE_ACCOUNT=patrik@kuvio.io
#   bq --project_id=claude-private-499703 show --format=prettyjson \
#     claude-private-499703:babyshop_raw | python3 -c \
#     'import json,sys; [print(a) for a in json.load(sys.stdin)["access"]]'
#
# If it is ever lost, re-add it the same way setup-meta.sh documents — patch the
# dataset access list as a kuvio owner, APPENDING to the existing list. Note
# `bq` has no --account flag; use CLOUDSDK_CORE_ACCOUNT.
#
# The job also reads the Funnel export (babyshop-funnel-data) for de-duplicated
# ad spend, which is reconciling detail only. The runtime SA already has that
# from the customer-insights job. A Funnel outage degrades one panel and is
# caught inside the job; it never fails the run.
#
# Otherwise this job only READS BigQuery and writes one Firestore document, so
# it needs no secrets and no env vars of its own.
#
# ── IMAGE ────────────────────────────────────────────────────────────────────
# Cloud Run jobs do NOT follow the service's image. By default this script
# repoints the job at whatever image the babyshop-dashboard SERVICE is running,
# which is what you want after a normal deploy. Before this tab is merged to
# main there is no such image, so pass one explicitly:
#
#   IMAGE=europe-north1-docker.pkg.dev/project-a7ade44e-e7e3-4871-a83/apps/babyshop-dashboard:exec-pl-<sha> \
#     ./pipeline/setup-exec-pl.sh
#
# Re-run this script after ANY deploy that touches refresh_exec_pl.py, or the
# job keeps running the old code.
#
# Idempotent: re-running updates the job and the schedule in place.
#
set -euo pipefail

PROJECT="${PROJECT:-project-a7ade44e-e7e3-4871-a83}"
REGION="${REGION:-europe-north1}"
SERVICE="${SERVICE:-babyshop-dashboard}"
# Cloud Scheduler is NOT offered in europe-north1 (pipeline/SETUP-STATUS.md).
SCHEDULER_REGION="${SCHEDULER_REGION:-europe-west1}"
JOB="${JOB:-exec-pl-refresh}"

echo "== 0. Resolve the image + runtime SA =="
IMAGE="${IMAGE:-$(gcloud run services describe "$SERVICE" \
  --project="$PROJECT" --region="$REGION" \
  --format='value(spec.template.spec.containers[0].image)')}"
RUNTIME_SA="$(gcloud run services describe "$SERVICE" \
  --project="$PROJECT" --region="$REGION" \
  --format='value(spec.template.spec.serviceAccountName)')"
if [[ -z "$RUNTIME_SA" ]]; then
  PROJECT_NUMBER="$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')"
  RUNTIME_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
fi
echo "   image:      $IMAGE"
echo "   runtime SA: $RUNTIME_SA"
echo "   (that SA needs READ on claude-private-499703:babyshop_raw — see header)"

echo "== 1. Create/update the Cloud Run job =="
ACTION=create
gcloud run jobs describe "$JOB" --project="$PROJECT" --region="$REGION" >/dev/null 2>&1 && ACTION=update
gcloud run jobs "$ACTION" "$JOB" \
  --project="$PROJECT" --region="$REGION" \
  --image="$IMAGE" \
  --service-account="$RUNTIME_SA" \
  --command=python3 --args=refresh_exec_pl.py \
  --task-timeout=15m --memory=1Gi --max-retries=1
echo "   ${ACTION}d $JOB -> python3 refresh_exec_pl.py"

echo "== 2. Daily Cloud Scheduler job =="
# 05:00 Stockholm. The BC connector finishes its nightly sync about 03:35, and
# 04:00/04:15/04:30 are taken by segments, bundles and meta. 05:00 is clear and
# still ahead of roas-sims (05:30) and voyado-refresh (05:15).
ACTION=create
gcloud scheduler jobs describe "${JOB}-nightly" --project="$PROJECT" \
  --location="$SCHEDULER_REGION" >/dev/null 2>&1 && ACTION=update
gcloud scheduler jobs "$ACTION" http "${JOB}-nightly" \
  --project="$PROJECT" \
  --location="$SCHEDULER_REGION" \
  --schedule="0 5 * * *" \
  --time-zone="Europe/Stockholm" \
  --uri="https://run.googleapis.com/v2/projects/${PROJECT}/locations/${REGION}/jobs/${JOB}:run" \
  --http-method=POST \
  --oauth-service-account-email="$RUNTIME_SA" \
  --attempt-deadline=180s
echo "   ${ACTION}d ${JOB}-nightly (0 5 * * *) -> $JOB"

cat <<EOF

== 3. First run + smoke test ==
   gcloud run jobs execute ${JOB} --project=${PROJECT} --region=${REGION} --wait

   TOKEN=\$(gcloud auth print-access-token)
   curl -sH "Authorization: Bearer \$TOKEN" \\
     "https://firestore.googleapis.com/v1/projects/${PROJECT}/databases/(default)/documents/funnel_cache/-Ln87GcdqU9CMJV6zMBY__exec-pl" \\
     | head -c 600
EOF
