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
#   babyshop_staging   int_ga4_item_rows — a VIEW, the source of the products,
#                      categories and subcategories (GA4 item_id is unusable on
#                      this property, so products are keyed on name + brand,
#                      which only this view carries)
#   babyshop_raw       that view resolves against the raw GA4 items table with
#                      the CALLER's permissions
#
# All three were granted to 871631085269-compute@ for meta-refresh on
# 2026-09-04 (see setup-meta.sh), so in the normal case NOTHING new is needed.
#
# ONE POSSIBLE GAP (not yet verified AS THE SERVICE ACCOUNT — the 2026-10-07
# real-data runs were made as a kuvio user): int_ga4_item_rows also joins the
# reporting FX-rates view (stg_reference__currency_rates_reporting). If that
# view resolves against a dataset outside the three above, the products query
# 403s. The job does not fail — products / categories / subcategories arrive
# empty with a note, and the log carries "WARN ecom: products failed —
# Forbidden: … Access Denied: Table <project>:<dataset>.<table>". Grant READER
# on the dataset that message names, the same way setup-meta.sh does it (as a
# kuvio owner):
#
#   export CLOUDSDK_CORE_ACCOUNT=patrik@kuvio.io
#   ds=<dataset from the error>
#   bq --project_id=claude-private-499703 show --format=prettyjson claude-private-499703:$ds \
#     | python3 -c 'import json,sys; d=json.load(sys.stdin); d["access"].append({"role":"READER","userByEmail":"871631085269-compute@developer.gserviceaccount.com"}); json.dump({"access":d["access"]},open("/tmp/"+sys.argv[1]+".json","w"))' $ds \
#     && bq --project_id=claude-private-499703 update --source /tmp/$ds.json claude-private-499703:$ds
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
