#!/usr/bin/env bash
# setup-blackmonth.sh — Cloud Run job + scheduler for the Black Month tab.
#
# The job runs refresh_blackmonth.py, which builds every section (bm_common.SECTIONS)
# and writes funnel_cache/<ws>__bm__<key>. Hourly 06–23 Stockholm through November:
# the war room needs yesterday closed early and today's pacing during the day.
#
# Same image and runtime SA as the babyshop-dashboard service (so it already holds
# BigQuery, Firestore and the cross-project warehouse READER grants of meta-refresh).
# The Google Ads secrets are mounted the same way setup-roas-sims.sh wires them
# into the service. Re-run after every deploy that changes a bm_*.py file — Cloud
# Run jobs do NOT follow the service's image.
#
#   gcloud config set account patrik.segersven@gmail.com
#   IMAGE=<region>-docker.pkg.dev/.../babyshop-dashboard:<sha> ./setup-blackmonth.sh
set -euo pipefail

PROJECT="${PROJECT:-project-a7ade44e-e7e3-4871-a83}"
REGION="${REGION:-europe-north1}"
SERVICE="${SERVICE:-babyshop-dashboard}"
SCHEDULER_REGION="${SCHEDULER_REGION:-europe-west1}"
JOB="${JOB:-blackmonth-refresh}"
# paid/insights: Google Ads (read-only). offers: Airtable calendar, Norce order
# lines with list price (bm_offers_norce), Channable feed for stock. insights:
# Meta activity log — meta-ads-access-token is a COPY of the claude-private
# secret of the same name (2026-11-04, user-approved): rotate it in both places.
SECRETS="GOOGLE_ADS_DEVELOPER_TOKEN=GOOGLE_ADS_DEVELOPER_TOKEN:latest,GOOGLE_ADS_CLIENT_ID=GOOGLE_ADS_CLIENT_ID:latest,GOOGLE_ADS_CLIENT_SECRET=GOOGLE_ADS_CLIENT_SECRET:latest,GOOGLE_ADS_REFRESH_TOKEN=GOOGLE_ADS_REFRESH_TOKEN:latest,GOOGLE_ADS_LOGIN_CUSTOMER_ID=GOOGLE_ADS_LOGIN_CUSTOMER_ID:latest,AIRTABLE_PAT=AIRTABLE_PAT:latest,NORCE_CLIENT_ID=NORCE_CLIENT_ID:latest,NORCE_CLIENT_SECRET=NORCE_CLIENT_SECRET:latest,CHANNABLE_FEED_URL=channable-feed-url:latest,META_ADS_ACCESS_TOKEN=meta-ads-access-token:latest"

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

echo "== 0b. Runtime SA may read the Airtable token (the others are already granted) =="
gcloud secrets add-iam-policy-binding AIRTABLE_PAT --project="$PROJECT" \
  --member="serviceAccount:$RUNTIME_SA" --role=roles/secretmanager.secretAccessor >/dev/null

echo "== 1. Create/update the Cloud Run job =="
ACTION=create
gcloud run jobs describe "$JOB" --project="$PROJECT" --region="$REGION" >/dev/null 2>&1 && ACTION=update
gcloud run jobs "$ACTION" "$JOB" \
  --project="$PROJECT" --region="$REGION" \
  --image="$IMAGE" \
  --service-account="$RUNTIME_SA" \
  --command=python3 --args=refresh_blackmonth.py \
  --set-secrets="$SECRETS" \
  ${EXTRA_JOB_FLAGS:-} \
  --task-timeout=20m --memory=2Gi --max-retries=0
echo "   ${ACTION}d $JOB -> python3 refresh_blackmonth.py"

echo "== 2. Hourly scheduler, 06–23 Stockholm =="
ACTION=create
gcloud scheduler jobs describe "${JOB}-hourly" --project="$PROJECT" \
  --location="$SCHEDULER_REGION" >/dev/null 2>&1 && ACTION=update
gcloud scheduler jobs "$ACTION" http "${JOB}-hourly" \
  --project="$PROJECT" \
  --location="$SCHEDULER_REGION" \
  --schedule="10 6-23 * * *" \
  --time-zone="Europe/Stockholm" \
  --uri="https://run.googleapis.com/v2/projects/${PROJECT}/locations/${REGION}/jobs/${JOB}:run" \
  --http-method=POST \
  --oauth-service-account-email="$RUNTIME_SA" \
  --attempt-deadline=180s
echo "   ${ACTION}d ${JOB}-hourly (10 6-23 * * *) -> $JOB"

cat <<EOF
== 3. First run ==
   gcloud run jobs execute ${JOB} --project=${PROJECT} --region=${REGION} --wait
   Pause after November:
   gcloud scheduler jobs pause ${JOB}-hourly --project=${PROJECT} --location=${SCHEDULER_REGION}
EOF
