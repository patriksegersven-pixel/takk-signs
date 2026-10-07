#!/usr/bin/env bash
#
# One-time setup for the E-com Funnel snapshot job.
#
#   apps/babyshop-dashboard/refresh_ecom.py   BigQuery (Bluebird warehouse) -> Firestore
#   GET /api/ecom                             what the E-com Funnel tab reads
#
# ── PREREQUISITE: cross-project BigQuery read ────────────────────────────────
# Like meta-refresh, this job reads the NEW stack's warehouse,
# claude-private-499703, while the query JOB is billed to project-a7ade44e
# (where the runtime SA already holds bigquery.jobUser). It needs READ on:
#
#   babyshop_marts     every mart the tab reads (agg_daily_kpis_by_channel,
#                      agg_daily_events_by_session_channel, agg_daily_landing_page_*,
#                      agg_daily_kpis_by_brand, agg_daily_kpis_by_gads_product,
#                      agg_daily_on_site_search, agg_funnel_snapshots)
#   babyshop_staging   stg_ga4__items_by_source_daily — a VIEW, the source of the
#                      products, categories and subcategories (GA4 item_id is
#                      unusable on this property, so products are keyed on
#                      name + brand, which only the item rows carry)
#   babyshop_raw       that view resolves against items_by_source_daily_native,
#                      and nothing else, with the CALLER's permissions
#
# All three were granted to 871631085269-compute@ for meta-refresh on
# 2026-09-04 (see setup-meta.sh), so NOTHING new is needed.
#
# RESOLVED 2026-10-07 — do NOT repoint the products query at
# babyshop_staging.int_ga4_item_rows. That view also joins the reporting
# FX-rates view, which resolves to claude-private-499703:bluebird_shared
# .currency_rates_daily; the SA has no grant there (deliberately — it is a
# dataset shared by every client) and the first production run 403'd on it,
# leaving products empty. refresh_ecom.py now reads the plain staging view and
# converts item revenue with the rate the warehouse itself applied, implied by
# babyshop_marts.agg_daily_kpis_by_brand (item_revenue / item_revenue_native).
# Every other source is a base TABLE in babyshop_marts.
#
# The user funnel is read from the MART (babyshop_marts.agg_funnel_snapshots, a
# table), never from the staging view over babyshop_funnel, so that dataset
# needs no grant.
#
# The job only READS BigQuery and writes 15 Firestore docs
# (funnel_cache/<ws>__ecom__<market>_<days>); no secrets, no env vars, no
# internet egress.
#
# Idempotent: re-running updates the job in place (and repoints it at the
# service's CURRENT image — run it after any deploy that touches
# refresh_ecom.py, because Cloud Run jobs do not follow the service image).
#
set -euo pipefail

PROJECT="${PROJECT:-project-a7ade44e-e7e3-4871-a83}"
REGION="${REGION:-europe-north1}"
SERVICE="${SERVICE:-babyshop-dashboard}"
# Cloud Scheduler is NOT offered in europe-north1 (pipeline/SETUP-STATUS.md).
SCHEDULER_REGION="${SCHEDULER_REGION:-europe-west1}"
JOB="${JOB:-ecom-refresh}"
# 05:30 UTC, and deliberately in UTC rather than Europe/Stockholm:
#   • the tab's newest day is YESTERDAY's GA4, which only reaches the marts with
#     the warehouse's morning dbt build, bb-marts-dbt-am, scheduled at 04:00 UTC
#     (per bb-marts/deploy.sh; itself downstream of the GA4 connector). That
#     cron is in UTC, so a Stockholm-time schedule here would drift an hour
#     against it twice a year. 90 minutes of headroom covers the fleet-wide
#     build; if a run still beats it, the snapshot says so itself
#     (meta.data_through + a note) rather than showing a silent dip.
#   • 05:30 UTC is 06:30 / 07:30 Stockholm — after meta-refresh (04:30
#     Stockholm, 30 min timeout) has finished, so the two cross-project readers
#     never overlap, and before the working day starts.
# UNVERIFIED: the 04:00 UTC slot and the build's duration were read from the
# repo, not observed in claude-private-499703. Check the job's finish time once
# and move SCHEDULE if needed.
SCHEDULE="${SCHEDULE:-30 5 * * *}"
SCHEDULE_TZ="${SCHEDULE_TZ:-Etc/UTC}"

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
echo "   (that SA needs READER on claude-private-499703:babyshop_marts,"
echo "    :babyshop_staging and :babyshop_raw — see the header of this script)"

echo "== 1. Create/update the Cloud Run job =="
ACTION=create
gcloud run jobs describe "$JOB" --project="$PROJECT" --region="$REGION" >/dev/null 2>&1 && ACTION=update
gcloud run jobs "$ACTION" "$JOB" \
  --project="$PROJECT" --region="$REGION" \
  --image="$IMAGE" \
  --service-account="$RUNTIME_SA" \
  --command=python3 --args=refresh_ecom.py \
  --task-timeout=20m --memory=1Gi --max-retries=1
echo "   ${ACTION}d $JOB -> python3 refresh_ecom.py"

echo "== 2. Daily Cloud Scheduler job =="
ACTION=create
gcloud scheduler jobs describe "${JOB}-daily" --project="$PROJECT" \
  --location="$SCHEDULER_REGION" >/dev/null 2>&1 && ACTION=update
gcloud scheduler jobs "$ACTION" http "${JOB}-daily" \
  --project="$PROJECT" \
  --location="$SCHEDULER_REGION" \
  --schedule="$SCHEDULE" \
  --time-zone="$SCHEDULE_TZ" \
  --uri="https://run.googleapis.com/v2/projects/${PROJECT}/locations/${REGION}/jobs/${JOB}:run" \
  --http-method=POST \
  --oauth-service-account-email="$RUNTIME_SA" \
  --attempt-deadline=180s
echo "   ${ACTION}d ${JOB}-daily ($SCHEDULE $SCHEDULE_TZ) -> $JOB"

cat <<EOF

== 3. First run + smoke test ==
   gcloud run jobs execute ${JOB} --project=${PROJECT} --region=${REGION} --wait

   # the job log states the client_site and market values it found, every
   # section that degraded ("WARN ecom: …") and each document's size
   gcloud logging read 'resource.type="cloud_run_job" AND resource.labels.job_name="${JOB}"' \\
     --project=${PROJECT} --limit=60 --format='value(textPayload)'

   TOKEN=\$(gcloud auth print-access-token)
   curl -sH "Authorization: Bearer \$TOKEN" \\
     "https://firestore.googleapis.com/v1/projects/${PROJECT}/databases/(default)/documents/funnel_cache/-Ln87GcdqU9CMJV6zMBY__ecom__all_28" \\
     | head -c 600
EOF
