#!/usr/bin/env bash
#
# One-time setup for the Meta creatives snapshot job.
#
#   apps/babyshop-dashboard/refresh_meta.py   BigQuery (Bluebird warehouse) -> Firestore
#   GET /api/meta                             what the Meta creatives tab reads
#
# ── PREREQUISITE: cross-project BigQuery read (done 2026-09-04, keep for reference) ──
# This is the only job on the legacy dashboard that reads a dataset in ANOTHER
# project: the Meta mart lives in the new stack's warehouse,
# claude-private-499703. The query JOB is billed to project-a7ade44e (where the
# runtime SA already holds bigquery.jobUser), so the SA needs nothing but READ
# on three kuvio datasets: babyshop_marts (the mart), babyshop_staging (the
# stg_meta__ads view) AND babyshop_raw (views resolve against ads_native with
# the caller's permissions — without raw access the "recent" query 403s).
#
# `bq add-iam-policy-binding` / `get-iam-policy` require allowlisting on that
# project and `bq` has no --account flag, so patch the dataset access list
# instead (as a kuvio owner):
#
#   export CLOUDSDK_CORE_ACCOUNT=patrik@kuvio.io
#   for ds in babyshop_marts babyshop_staging babyshop_raw; do
#     bq --project_id=claude-private-499703 show --format=prettyjson claude-private-499703:$ds \
#       | python3 -c 'import json,sys; d=json.load(sys.stdin); d["access"].append({"role":"READER","userByEmail":"871631085269-compute@developer.gserviceaccount.com"}); json.dump({"access":d["access"]},open("/tmp/"+sys.argv[1]+".json","w"))' $ds \
#       && bq --project_id=claude-private-499703 update --source /tmp/$ds.json claude-private-499703:$ds
#   done
#
# Without all three, the job fails with "Access Denied: Table
# claude-private-499703:babyshop_<dataset>.<table>".
#
# Note that 871631085269-compute@ is the legacy project's DEFAULT compute SA,
# so this grant gives every Cloud Run job and service in project-a7ade44e read
# access to those two datasets. A dedicated SA would be tighter; that is a
# deliberate open decision, not an oversight.
#
# ── IMAGE MIRROR (added in v3) ───────────────────────────────────────────────
# The job now also FETCHES the creative images over the public internet
# (scontent-*.xx.fbcdn.net and www.facebook.com/ads/image/) and stores resized
# JPEGs as base64 in Firestore collection `meta_creative_images`, one document
# per ad, which GET /api/meta/image/<ad_id> serves. This needs:
#   • egress to the internet — the job runs with Cloud Run's DEFAULT egress
#     (no VPC connector, no egress restrictions), so fbcdn is reachable. If a
#     VPC connector with "all traffic" egress is ever added to this job, the
#     mirror silently degrades to placeholders unless Cloud NAT is configured.
#   • no extra IAM: the runtime SA's roles/datastore.user already covers the
#     new collection.
# Every fetch is wrapped, so a dead URL or a network failure costs one
# thumbnail and never the run. Knobs: META_SKIP_IMAGES=1 disables the mirror,
# META_IMAGE_ADS=<n> caps it (local runs).
#
# Firestore cost: ~92 documents, 3-6 KiB for a 64px-only creative and 60-90 KiB
# for a full-size still or video poster. Documents carry a 30-day expires_at
# and are only re-fetched when they are older than 14 days or a better source
# appears, so a steady-state nightly run rewrites almost nothing.
#
# Otherwise this job only READS BigQuery and writes Firestore docs, so it
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
# own Meta loads (~03:58). DAILY, not weekly: the source URLs are signed Meta
# CDN links that expire about four days after minting, so the mirror has to
# re-cut before then or a newly launched ad never gets an image at all. (The
# images already mirrored survive — that is the point of the mirror — but the
# numbers still need a daily snapshot.)
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
