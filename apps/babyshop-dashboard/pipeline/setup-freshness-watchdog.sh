#!/usr/bin/env bash
#
# Setup for the data freshness watchdog.
#
#   apps/babyshop-dashboard/freshness_watchdog.py   asserts data LANDED
#   Cloud Run job  freshness-watchdog               (europe-north1)
#   Scheduler job  freshness-watchdog-daily         (europe-west1, 07:00 Sthlm)
#   Alert policy   "Data freshness stale"           -> existing email channel
#
# WHY
#   Two silent failures inside two days, both invisible because success was an
#   exit code rather than data arriving: norce-sync green for four weeks while
#   writing nothing, and the Google Ads connector green while 5 of 8 accounts
#   stopped returning rows. This job asserts the thing the dashboards actually
#   depend on.
#
# WHEN TO RE-RUN
#   Idempotent. Re-run after any change to freshness_watchdog.py, because Cloud
#   Run jobs do NOT follow the service image (CLAUDE.md). It repoints the job at
#   the service's CURRENT image, so run it after the deploy that ships the
#   change, not before.
#
# ALREADY DEPLOYED (15 Sep 2026)
#   The job was created out of band, ahead of this file being merged, from a
#   standalone image built only from freshness_watchdog.py:
#     europe-north1-docker.pkg.dev/<project>/apps/freshness-watchdog:2026-09-15-*
#   Running this script after the merge moves it onto the dashboard image like
#   every other job here, after which that standalone image can be deleted.
#
set -euo pipefail

PROJECT="${PROJECT:-project-a7ade44e-e7e3-4871-a83}"
REGION="${REGION:-europe-north1}"
SERVICE="${SERVICE:-babyshop-dashboard}"
# Cloud Scheduler is not offered in europe-north1 (pipeline/SETUP-STATUS.md).
SCHEDULER_REGION="${SCHEDULER_REGION:-europe-west1}"
JOB="${JOB:-freshness-watchdog}"
# Existing email channel, the one the "Cloud Build failure" policy already uses.
CHANNEL="${CHANNEL:-7935008251541699152}"

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
# --max-retries=0, deliberately unlike the other jobs here. A retry cannot make
# stale data fresh: it just runs the same query again and mails a second alert.
# One failed execution per day is the signal.
ACTION=create
gcloud run jobs describe "$JOB" --project="$PROJECT" --region="$REGION" >/dev/null 2>&1 && ACTION=update
gcloud run jobs "$ACTION" "$JOB" \
  --project="$PROJECT" --region="$REGION" \
  --image="$IMAGE" \
  --service-account="$RUNTIME_SA" \
  --command=python3 --args=freshness_watchdog.py \
  --task-timeout=10m --memory=512Mi --max-retries=0
echo "   ${ACTION}d $JOB -> python3 freshness_watchdog.py"

echo "== 2. Cross-project read access (verify, do not assume) =="
# The watchdog reads three projects. Both remote grants already exist (the
# dashboard reads the Funnel export, and meta-refresh reads the new warehouse),
# so this only proves it rather than granting anything.
for REMOTE in babyshop-funnel-data claude-private-499703; do
  echo "   $REMOTE:"
  gcloud projects get-iam-policy "$REMOTE" \
    --flatten='bindings[].members' \
    --filter="bindings.members:serviceAccount:${RUNTIME_SA}" \
    --format='value(bindings.role)' 2>/dev/null | sort -u | sed 's/^/     /' || \
    echo "     (no project level roles: access is via dataset ACL, check with bq show)"
done
echo "   claude-private-499703:babyshop_raw dataset ACL:"
bq show --format=prettyjson claude-private-499703:babyshop_raw 2>/dev/null \
  | python3 -c "import json,sys;print('\n'.join('     '+a.get('role','')+' '+a.get('userByEmail','') for a in json.load(sys.stdin).get('access',[]) if 'userByEmail' in a))" \
  || echo "     (could not read dataset ACL from this account, this is expected)"

echo "== 3. Daily Cloud Scheduler job =="
# 07:00 Stockholm: after every nightly producer has finished (norce-sync 03:00,
# customer-insights 03:30, segments 04:00, bundles 04:15, voyado 04:30,
# meta 04:30, exec-pl 05:00, roas-sims 05:30) and before the working day, so a
# stale source is known before anyone reads a dashboard.
ACTION=create
gcloud scheduler jobs describe "${JOB}-daily" --project="$PROJECT" \
  --location="$SCHEDULER_REGION" >/dev/null 2>&1 && ACTION=update
gcloud scheduler jobs "$ACTION" http "${JOB}-daily" \
  --project="$PROJECT" \
  --location="$SCHEDULER_REGION" \
  --schedule="0 7 * * *" \
  --time-zone="Europe/Stockholm" \
  --uri="https://run.googleapis.com/v2/projects/${PROJECT}/locations/${REGION}/jobs/${JOB}:run" \
  --http-method=POST \
  --oauth-service-account-email="$RUNTIME_SA" \
  --attempt-deadline=180s
echo "   ${ACTION}d ${JOB}-daily (0 7 * * *) -> $JOB"

echo "== 4. Alert policy on the FRESHNESS_ALERT marker =="
# Same shape and same channel as the existing "Cloud Build failure" policy
# (conditionMatchedLog). The marker is matched by text, not by severity: Cloud
# Run job stderr lands at DEFAULT severity, not ERROR, so a severity filter
# would never fire. Rate limited to one mail an hour, auto-closing after a day,
# because a stale source stays stale until someone fixes it.
TOKEN="$(gcloud auth print-access-token)"
if curl -s "https://monitoring.googleapis.com/v3/projects/${PROJECT}/alertPolicies" \
     -H "Authorization: Bearer $TOKEN" | grep -q '"Data freshness stale"'; then
  echo "   policy already exists, leaving it alone"
else
  curl -sX POST "https://monitoring.googleapis.com/v3/projects/${PROJECT}/alertPolicies" \
    -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
    -d '{
      "displayName": "Data freshness stale",
      "combiner": "OR",
      "conditions": [{
        "displayName": "A critical BigQuery source is past its freshness threshold",
        "conditionMatchedLog": {
          "filter": "resource.type=\"cloud_run_job\" AND resource.labels.job_name=\"'"${JOB}"'\" AND textPayload:\"FRESHNESS_ALERT\""
        }
      }],
      "notificationChannels": ["projects/'"${PROJECT}"'/notificationChannels/'"${CHANNEL}"'"],
      "enabled": true,
      "alertStrategy": {"notificationRateLimit": {"period": "3600s"}, "autoClose": "86400s"}
    }' | python3 -c "import json,sys; d=json.load(sys.stdin); print('   created' if 'name' in d else '   ERROR: '+json.dumps(d)[:300])"
fi

cat <<EOF

== 5. First run ==
   # Exits 1 while any source is stale. That is the point, not a bug.
   gcloud run jobs execute ${JOB} --project=${PROJECT} --region=${REGION} --wait
   gcloud logging read 'resource.type="cloud_run_job" AND
     resource.labels.job_name="${JOB}"' --project=${PROJECT} --limit=40 \\
     --format='value(textPayload)' --order=asc

== 6. Re-deriving thresholds ==
   # Prints each source's observed cadence next to the configured threshold.
   gcloud run jobs execute ${JOB} --project=${PROJECT} --region=${REGION} \\
     --args=freshness_watchdog.py,--calibrate --wait
EOF
