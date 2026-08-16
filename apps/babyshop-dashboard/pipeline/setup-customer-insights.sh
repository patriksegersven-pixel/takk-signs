#!/usr/bin/env bash
#
# One-time setup for the Customer Insights pipeline.
#
#   apps/babyshop-dashboard/norce_sync.py                Norce OData -> BigQuery
#   apps/babyshop-dashboard/norce_marts.sql              the mart views it applies
#   apps/babyshop-dashboard/refresh_customer_insights.py BigQuery -> Firestore snapshot
#   GET /api/customer-insights                           what the dashboard reads
#
# STATUS: STUB — NOT YET RUN. The two Norce secrets do not exist. Read
# pipeline/CUSTOMER-INSIGHTS.md before running this, then run it ONCE with the
# credentials exported. It is idempotent — re-running only fills gaps.
#
#   export NORCE_CLIENT_ID=...
#   export NORCE_CLIENT_SECRET=...
#   ./pipeline/setup-customer-insights.sh
#
# WHY TWO CLOUD RUN JOBS AND NOT AN /internal/ ENDPOINT
#   The ROAS collector is an endpoint because one collection is ~30 s. A Norce
#   BACKFILL is ~416k orders across 13 applications — far past Cloud Run's
#   request ceiling, and the retry-on-timeout behaviour that makes Scheduler
#   safe for a 30 s job makes it actively harmful for an hour-long one. So the
#   sync and the snapshot refresh are Cloud Run JOBS, which have their own
#   task timeout and no HTTP request tied to them. Both reuse the service's
#   existing image, so they ship on the same Cloud Build as the dashboard.
#
# WHY NOT cloudbuild.yaml
#   Same reason as setup-roas-sims.sh: `--update-secrets=K=secret:latest` FAILS
#   outright when the secret does not exist yet, and a failed deploy silently
#   leaves the previous revision serving traffic (CLAUDE.md, "Pipeline
#   overview"). Secrets are wired out of band, once.
#
set -euo pipefail

PROJECT="${PROJECT:-project-a7ade44e-e7e3-4871-a83}"
REGION="${REGION:-europe-north1}"
SERVICE="${SERVICE:-babyshop-dashboard}"
# Cloud Scheduler is NOT offered in europe-north1 — the existing roas-sims-daily
# job lives in europe-west1 (pipeline/SETUP-STATUS.md). Match it.
SCHEDULER_REGION="${SCHEDULER_REGION:-europe-west1}"

SYNC_JOB="${SYNC_JOB:-norce-sync}"
SNAP_JOB="${SNAP_JOB:-customer-insights-refresh}"
BQ_DATASET="${BQ_DATASET:-norce}"

SECRETS=(
  NORCE_CLIENT_ID
  NORCE_CLIENT_SECRET
)

echo "== 0. Resolve the image + runtime service account the dashboard is running =="
# Both jobs reuse the service's image so they can never drift from the deployed
# code. --format on a plain field path is fine (CLAUDE.md, "gcloud --format
# breaks on annotation keys" only bites on keys containing '/').
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

echo "== 1. Create the two secrets and add a version =="
for NAME in "${SECRETS[@]}"; do
  VALUE="${!NAME:-}"
  if [[ -z "$VALUE" ]]; then
    echo "   !! \$$NAME is not exported — skipping. Re-run with it set."
    continue
  fi
  if ! gcloud secrets describe "$NAME" --project="$PROJECT" >/dev/null 2>&1; then
    gcloud secrets create "$NAME" --project="$PROJECT" --replication-policy=automatic
  fi
  printf '%s' "$VALUE" | gcloud secrets versions add "$NAME" --project="$PROJECT" --data-file=-
  echo "   ok $NAME"
done

echo "== 2. Grant the runtime SA secretAccessor on each =="
for NAME in "${SECRETS[@]}"; do
  gcloud secrets add-iam-policy-binding "$NAME" \
    --project="$PROJECT" \
    --member="serviceAccount:${RUNTIME_SA}" \
    --role=roles/secretmanager.secretAccessor >/dev/null
  echo "   ok $NAME"
done

echo "== 3. Verify the bindings actually landed (never trust step 2's output) =="
for NAME in "${SECRETS[@]}"; do
  gcloud secrets get-iam-policy "$NAME" --project="$PROJECT" \
    --flatten="bindings[].members" \
    --filter="bindings.members:serviceAccount:${RUNTIME_SA}" \
    --format="value(bindings.role)" | sort -u | sed "s/^/   $NAME /"
done

echo "== 4. BigQuery IAM =="
# norce_sync CREATES the dataset on first run, so the dataset-level grant cannot
# be made yet — jobUser + dataEditor at project level covers both the create and
# every load/MERGE afterwards.
for ROLE in roles/bigquery.jobUser roles/bigquery.dataEditor; do
  gcloud projects add-iam-policy-binding "$PROJECT" \
    --member="serviceAccount:${RUNTIME_SA}" --role="$ROLE" --condition=None >/dev/null
  echo "   ok $ROLE on $PROJECT"
done
echo "   NOTE: reading babyshop-funnel-data.bs_funnel_export is a SEPARATE project."
echo "   The dashboard already reads it (bq_source.py), so the grant exists; verify with:"
echo "     gcloud projects get-iam-policy babyshop-funnel-data \\"
echo "       --flatten='bindings[].members' \\"
echo "       --filter='bindings.members:serviceAccount:${RUNTIME_SA}' \\"
echo "       --format='value(bindings.role)' | sort -u"

echo "== 5. Create the two Cloud Run jobs =="
# --task-timeout: a cold backfill pulls ~416k orders + ~1.43M lines. 6 h is
# generous; the nightly delta finishes in minutes.
SECRET_FLAG="$(IFS=,; printf '%s' "$(for N in "${SECRETS[@]}"; do printf '%s=%s:latest,' "$N" "$N"; done)" | sed 's/,$//')"

for SPEC in "$SYNC_JOB|norce_sync.py|6h|2Gi" "$SNAP_JOB|refresh_customer_insights.py|30m|1Gi"; do
  IFS='|' read -r JOB SCRIPT TIMEOUT MEM <<<"$SPEC"
  ACTION=create
  gcloud run jobs describe "$JOB" --project="$PROJECT" --region="$REGION" >/dev/null 2>&1 && ACTION=update
  gcloud run jobs "$ACTION" "$JOB" \
    --project="$PROJECT" --region="$REGION" \
    --image="$IMAGE" \
    --service-account="$RUNTIME_SA" \
    --command=python3 --args="$SCRIPT" \
    --task-timeout="$TIMEOUT" --memory="$MEM" --max-retries=1 \
    --set-secrets="$SECRET_FLAG"
  echo "   ${ACTION}d $JOB -> python3 $SCRIPT"
done

echo "== 6. Nightly Cloud Scheduler jobs =="
# Order matters: sync first, snapshot after. 30 min apart rather than chained,
# because a job-completion trigger would need Eventarc + a Pub/Sub topic for no
# real benefit at this cadence — and the snapshot is happy with yesterday's
# Norce data if the sync ever overruns.
JOB_SA="${RUNTIME_SA}"
for SPEC in "${SYNC_JOB}-nightly|${SYNC_JOB}|0 3 * * *" "${SNAP_JOB}-nightly|${SNAP_JOB}|30 3 * * *"; do
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
    --oauth-service-account-email="$JOB_SA" \
    --attempt-deadline=180s
  echo "   ${ACTION}d $SJOB ($SCHEDULE) -> $TARGET"
done

cat <<EOF

== 7. Backfill (run ONCE, after the jobs exist) ==
   # Full history from the 2025-06-11 platform cutover. ~1-2 h.
   gcloud run jobs execute ${SYNC_JOB} --project=${PROJECT} --region=${REGION} \\
     --args=norce_sync.py,--backfill --wait

   # Nervous about the blast radius? Do one market first and check the marts:
   gcloud run jobs execute ${SYNC_JOB} --project=${PROJECT} --region=${REGION} \\
     --args=norce_sync.py,--backfill,--apps,babyshop-se --wait

   # Then the snapshot, so the tab picks the Norce sections up:
   gcloud run jobs execute ${SNAP_JOB} --project=${PROJECT} --region=${REGION} --wait

== 8. Smoke test ==
   bq query --project_id=${PROJECT} --location=EU --nouse_legacy_sql \\
     'SELECT COUNT(*) orders, COUNT(DISTINCT customer_hash) customers,
             MIN(order_date) first_order, MAX(order_date) last_order
      FROM \`${PROJECT}.${BQ_DATASET}.customer_orders\`'

   # The snapshot the dashboard reads:
   TOKEN=\$(gcloud auth print-access-token)
   curl -sH "Authorization: Bearer \$TOKEN" \\
     "https://firestore.googleapis.com/v1/projects/${PROJECT}/databases/(default)/documents/funnel_cache/-Ln87GcdqU9CMJV6zMBY__customer-insights" \\
     | head -c 600

== 9. Confirm nothing leaked ==
   No column in the norce dataset may hold an email, name, phone or street
   address. customer_hash is SHA256(LOWER(TRIM(email))) and is the ONLY
   identity field. Verify after the first backfill:
   bq show --schema --format=prettyjson ${PROJECT}:${BQ_DATASET}.orders
EOF
