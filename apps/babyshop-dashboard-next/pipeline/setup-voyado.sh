#!/usr/bin/env bash
#
# One-time setup for the Voyado email pipeline.
#
#   apps/babyshop-dashboard/voyado_sync.py        Delta Share -> BigQuery
#   apps/babyshop-dashboard/voyado_marts.sql      the mart views it applies
#   apps/babyshop-dashboard/refresh_voyado.py     BigQuery -> Firestore snapshot
#   GET /api/voyado-email                         what the dashboard reads
#
# Mirrors setup-customer-insights.sh: jobs reuse the SERVICE's deployed image so
# they can never drift from the deployed code at creation time (they still pin —
# after any deploy that touches voyado_*.py or refresh_voyado.py, re-run this
# script or repoint the jobs to the new SHA, exactly like norce-sync).
#
# Idempotent — safe to re-run; create-or-update throughout.
#
# THE SECRET IS A FILE, NOT AN ENV VAR
#   The Delta Sharing client takes a PATH to a .share profile (a JSON file
#   holding a non-expiring bearer token), so the secret is mounted at
#   /secrets/voyado/config.share rather than injected as an env var. The
#   secret `voyado-share-profile` is created out of band (it already exists;
#   created 2026-08-17 from the file Voyado's activation link produced) — this
#   script only verifies it and wires it on. To ROTATE: download a fresh
#   profile from Voyado, `gcloud secrets versions add voyado-share-profile
#   --data-file=<file>`, then re-run this script (jobs resolve :latest at
#   execution start, so a new version takes effect on the next run).
#
# WHY TWO CLOUD RUN JOBS AND NOT AN /internal/ ENDPOINT
#   Same reasoning as customer-insights: a full Voyado backfill reads ~17
#   months of multi-gigabyte event tables — far past Cloud Run's request
#   ceiling, and Scheduler's retry-on-timeout is harmful for hour-long work.
#   Jobs have their own task timeout and no HTTP request tied to them.
#
# SCHEDULE — TWICE DAILY, NOT NIGHTLY
#   Voyado refreshes the share's raw data products 2-4x per day; running more
#   often than twice a day just re-reads the same files. `30 4,14 * * *` is
#   one scheduler job per Cloud Run job, not four.
#
set -euo pipefail

PROJECT="${PROJECT:-project-a7ade44e-e7e3-4871-a83}"
REGION="${REGION:-europe-north1}"
SERVICE="${SERVICE:-babyshop-dashboard}"
# Cloud Scheduler is NOT offered in europe-north1 — every existing entry lives
# in europe-west1 (pipeline/SETUP-STATUS.md). Match it.
SCHEDULER_REGION="${SCHEDULER_REGION:-europe-west1}"

SYNC_JOB="${SYNC_JOB:-voyado-sync}"
SNAP_JOB="${SNAP_JOB:-voyado-refresh}"
SECRET_NAME="${SECRET_NAME:-voyado-share-profile}"
BQ_DATASET="${BQ_DATASET:-voyado}"

echo "== 0. Resolve the image + runtime service account the dashboard is running =="
IMAGE="$(gcloud run services describe "$SERVICE" \
  --project="$PROJECT" --region="$REGION" \
  --format='value(spec.template.spec.containers[0].image)')"
RUNTIME_SA="$(gcloud run services describe "$SERVICE" \
  --project="$PROJECT" --region="$REGION" \
  --format='value(spec.template.spec.serviceAccountName)')"
if [[ -z "$RUNTIME_SA" ]]; then
  # An unset serviceAccountName means the service runs as the default compute SA.
  PROJECT_NUMBER="$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')"
  RUNTIME_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
fi
echo "   image:      $IMAGE"
echo "   runtime SA: $RUNTIME_SA"

echo "== 1. Verify the Delta Share secret exists (created out of band) =="
if ! gcloud secrets describe "$SECRET_NAME" --project="$PROJECT" >/dev/null 2>&1; then
  echo "   !! secret $SECRET_NAME does not exist. Create it from the .share file:"
  echo "      gcloud secrets create $SECRET_NAME --replication-policy=automatic \\"
  echo "        --data-file=/path/to/config.share"
  exit 1
fi
echo "   ok $SECRET_NAME"

echo "== 2. Grant the runtime SA secretAccessor, then verify it landed =="
gcloud secrets add-iam-policy-binding "$SECRET_NAME" \
  --project="$PROJECT" \
  --member="serviceAccount:${RUNTIME_SA}" \
  --role=roles/secretmanager.secretAccessor >/dev/null
gcloud secrets get-iam-policy "$SECRET_NAME" --project="$PROJECT" \
  --flatten="bindings[].members" \
  --filter="bindings.members:serviceAccount:${RUNTIME_SA}" \
  --format="value(bindings.role)" | sort -u | sed "s/^/   $SECRET_NAME /"

echo "== 3. BigQuery IAM (idempotent; norce setup granted the same pair) =="
for ROLE in roles/bigquery.jobUser roles/bigquery.dataEditor; do
  gcloud projects add-iam-policy-binding "$PROJECT" \
    --member="serviceAccount:${RUNTIME_SA}" --role="$ROLE" --condition=None >/dev/null
  echo "   ok $ROLE on $PROJECT"
done

echo "== 4. Create/update the two Cloud Run jobs =="
# --task-timeout: a cold event backfill reads ~17 months of the 3.2 GB delivery
# table in windows; 6 h is generous. The twice-daily delta is minutes. 4Gi on
# the sync because a --chunk-days window of deliveries peaks well over 2 GiB
# resident; the snapshot job only runs BigQuery SQL and writes one document.
for SPEC in "$SYNC_JOB|voyado_sync.py|6h|4Gi" "$SNAP_JOB|refresh_voyado.py|30m|1Gi"; do
  IFS='|' read -r JOB SCRIPT TIMEOUT MEM <<<"$SPEC"
  ACTION=create
  gcloud run jobs describe "$JOB" --project="$PROJECT" --region="$REGION" >/dev/null 2>&1 && ACTION=update
  gcloud run jobs "$ACTION" "$JOB" \
    --project="$PROJECT" --region="$REGION" \
    --image="$IMAGE" \
    --service-account="$RUNTIME_SA" \
    --command=python3 --args="$SCRIPT" \
    --task-timeout="$TIMEOUT" --memory="$MEM" --max-retries=1 \
    --set-secrets="/secrets/voyado/config.share=${SECRET_NAME}:latest"
  echo "   ${ACTION}d $JOB -> python3 $SCRIPT"
done

echo "== 5. Twice-daily Cloud Scheduler jobs =="
# Sync first, snapshot 45 min later rather than chained — same reasoning as the
# norce pair: Eventarc buys nothing at this cadence, and the snapshot is happy
# with the previous sync's data if a sync ever overruns.
for SPEC in "${SYNC_JOB}-twicedaily|${SYNC_JOB}|30 4,14 * * *" "${SNAP_JOB}-twicedaily|${SNAP_JOB}|15 5,15 * * *"; do
  IFS='|' read -r SJOB TARGET SCHEDULE <<<"$SPEC"
  ACTION=create
  gcloud scheduler jobs describe "$SJOB" --project="$PROJECT" \
    --location="$SCHEDULER_REGION" >/dev/null 2>&1 && ACTION=update
  # Cloud Run Jobs are triggered through the Admin API :run endpoint with an
  # OAuth token — NOT the OIDC token an HTTPS Cloud Run service takes.
  gcloud scheduler jobs "$ACTION" http "$SJOB" \
    --project="$PROJECT" \
    --location="$SCHEDULER_REGION" \
    --schedule="$SCHEDULE" \
    --time-zone="Europe/Stockholm" \
    --uri="https://run.googleapis.com/v2/projects/${PROJECT}/locations/${REGION}/jobs/${TARGET}:run" \
    --http-method=POST \
    --oauth-service-account-email="$RUNTIME_SA" \
    --attempt-deadline=180s
  echo "   ${ACTION}d $SJOB ($SCHEDULE) -> $TARGET"
done

cat <<EOF

== 6. First production run (the local backfill already landed 2025->today) ==
   # Incremental — proves the whole path (secret mount, BQ writes, marts):
   gcloud run jobs execute ${SYNC_JOB} --project=${PROJECT} --region=${REGION} --wait
   gcloud run jobs execute ${SNAP_JOB} --project=${PROJECT} --region=${REGION} --wait

   # If the dataset ever needs rebuilding from scratch (~2-4 h):
   gcloud run jobs execute ${SYNC_JOB} --project=${PROJECT} --region=${REGION} \\
     --args=voyado_sync.py,--backfill --wait

== 7. Smoke test ==
   bq query --project_id=${PROJECT} --location=EU --nouse_legacy_sql \\
     'SELECT (SELECT COUNT(*) FROM \`${PROJECT}.${BQ_DATASET}.deliveries\`) sent_rows,
             (SELECT MAX(deliveryDate) FROM \`${PROJECT}.${BQ_DATASET}.deliveries\`) freshest'

   TOKEN=\$(gcloud auth print-access-token)
   curl -sH "Authorization: Bearer \$TOKEN" \\
     "https://firestore.googleapis.com/v1/projects/${PROJECT}/databases/(default)/documents/funnel_cache/-Ln87GcdqU9CMJV6zMBY__voyado-email" \\
     | head -c 600

== 8. Confirm nothing leaked ==
   No column in the voyado dataset may hold an email, personnummer, name, phone
   or street address. customer_hash is SHA256(LOWER(TRIM(email))) and is the
   ONLY identity field — same contract as the norce dataset. Verify:
   bq show --schema --format=prettyjson ${PROJECT}:${BQ_DATASET}.contacts
EOF
