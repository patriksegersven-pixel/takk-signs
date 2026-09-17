#!/usr/bin/env bash
#
# One-time setup for the Daily performance snapshot job.
#
#   apps/babyshop-dashboard/refresh_daily_perf.py   BigQuery `norce` -> Firestore
#   GET /api/daily-perf                             the history half of the tab
#   GET /api/norce-today                            the live half (already wired)
#
# ── WHAT THIS JOB DOES, AND WHAT IT DELIBERATELY DOES NOT ───────────────────
# It aggregates the `norce` dataset to day x market on ORDER DATE and writes one
# Firestore document with every comparison the tab makes: same weekday last
# week, the trailing four-week same-weekday average, year-on-year at -364 days,
# rolling 7 and 28 day windows, the market split, month-to-date pacing and a
# 460-day series.
#
# It does NOT produce today, or yesterday's headline figure. Those are read live
# from the Norce API by norce_today.py, which is already attached to the SERVICE
# (setup-exec-pl.sh step 3 put NORCE_CLIENT_ID / NORCE_CLIENT_SECRET there for
# the Executive P&L day cards, and this tab reuses the same endpoint). If those
# secrets are ever detached, this tab still renders: yesterday falls back to the
# BigQuery copy with a visible label and today states that it is unavailable.
#
# ── PERMISSIONS ─────────────────────────────────────────────────────────────
# Same-project only. The `norce` dataset lives in project-a7ade44e alongside the
# job, so the runtime SA needs nothing beyond the bigquery.jobUser + dataset
# access it already has for the other Norce-reading jobs (segments,
# customer-insights, bundles). No cross-project grant, unlike the exec-pl job.
# No secrets and no env vars of its own.
#
# ── IMAGE ───────────────────────────────────────────────────────────────────
# Cloud Run jobs do NOT follow the service's image and do NOT auto-deploy from
# Cloud Build. By default this script repoints the job at whatever image the
# babyshop-dashboard SERVICE is running, which is what you want right after a
# normal deploy. To pin one explicitly:
#
#   IMAGE=europe-north1-docker.pkg.dev/project-a7ade44e-e7e3-4871-a83/apps/babyshop-dashboard:<sha> \
#     ./pipeline/setup-daily-perf.sh
#
# Re-run this script after ANY deploy that touches refresh_daily_perf.py, or the
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
JOB="${JOB:-daily-perf-refresh}"

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

echo "== 1. Create/update the Cloud Run job =="
ACTION=create
gcloud run jobs describe "$JOB" --project="$PROJECT" --region="$REGION" >/dev/null 2>&1 && ACTION=update
gcloud run jobs "$ACTION" "$JOB" \
  --project="$PROJECT" --region="$REGION" \
  --image="$IMAGE" \
  --service-account="$RUNTIME_SA" \
  --command=python3 --args=refresh_daily_perf.py \
  --task-timeout=10m --memory=1Gi --max-retries=1
echo "   ${ACTION}d $JOB -> python3 refresh_daily_perf.py"

echo "== 2. Daily Cloud Scheduler job =="
# 05:45 Stockholm. It must run AFTER norce-sync (01:00), which is what closes
# yesterday in BigQuery, and the earlier slots are taken: 04:00 segments,
# 04:15 bundles, 04:30 meta, 05:00 exec-pl, 05:15 voyado-refresh,
# 05:30 roas-sims. 05:45 is clear and still lands before anyone reads the tab.
ACTION=create
gcloud scheduler jobs describe "${JOB}-nightly" --project="$PROJECT" \
  --location="$SCHEDULER_REGION" >/dev/null 2>&1 && ACTION=update
gcloud scheduler jobs "$ACTION" http "${JOB}-nightly" \
  --project="$PROJECT" \
  --location="$SCHEDULER_REGION" \
  --schedule="45 5 * * *" \
  --time-zone="Europe/Stockholm" \
  --uri="https://run.googleapis.com/v2/projects/${PROJECT}/locations/${REGION}/jobs/${JOB}:run" \
  --http-method=POST \
  --oauth-service-account-email="$RUNTIME_SA" \
  --attempt-deadline=180s
echo "   ${ACTION}d ${JOB}-nightly (45 5 * * *) -> $JOB"

cat <<EOF

== 3. First run + smoke test ==
   gcloud run jobs execute ${JOB} --project=${PROJECT} --region=${REGION} --wait

   TOKEN=\$(gcloud auth print-access-token)
   curl -sH "Authorization: Bearer \$TOKEN" \\
     "https://firestore.googleapis.com/v1/projects/${PROJECT}/databases/(default)/documents/funnel_cache/-Ln87GcdqU9CMJV6zMBY__daily-perf" \\
     | head -c 600

== 4. A note on the freshness watchdog ==
   This job is a good candidate for freshness_watchdog.py: a stale daily-perf
   document means the comparisons on the tab silently age, which is exactly the
   failure norce-sync had. The tab itself also cross-checks the live read
   against the BigQuery copy on the day they overlap and prints a warning when
   they diverge by more than 0.5%.
EOF
