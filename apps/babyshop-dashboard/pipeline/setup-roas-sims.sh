#!/usr/bin/env bash
#
# One-time setup for the Google Ads API ROAS-simulations pipeline.
#
#   apps/babyshop-dashboard/refresh_roas_sims.py   collector
#   POST /internal/refresh-roas-sims               Cloud Scheduler target
#   GET  /api/roas-sims                            what the dashboard reads
#
# Run this ONCE, after you have the five Google Ads credentials (see PIPELINE.md
# "Credential prerequisites"). It is idempotent — re-running only fills gaps.
#
#   export GOOGLE_ADS_DEVELOPER_TOKEN=...
#   export GOOGLE_ADS_CLIENT_ID=...
#   export GOOGLE_ADS_CLIENT_SECRET=...
#   export GOOGLE_ADS_REFRESH_TOKEN=...
#   export GOOGLE_ADS_LOGIN_CUSTOMER_ID=1234567890     # the MCC id, digits only
#   ./pipeline/setup-roas-sims.sh
#
# WHY THIS IS A SCRIPT AND NOT cloudbuild.yaml
#   `gcloud run deploy --update-secrets=K=secret:latest` FAILS outright when the secret
#   does not exist yet, and a failed deploy silently leaves the previous revision serving
#   traffic (CLAUDE.md, "Pipeline overview"). Baking the wiring into cloudbuild.yaml would
#   therefore break every deploy of this app between merging the code and creating the
#   secrets. It also matches what this service already does: DASH_PASS, FUNNEL_* and
#   INTERNAL_TOKEN are wired the same way, out of band, which is exactly why the deploy
#   step uses --update-env-vars rather than --set-env-vars.
#
set -euo pipefail

PROJECT="${PROJECT:-project-a7ade44e-e7e3-4871-a83}"
REGION="${REGION:-europe-north1}"
SERVICE="${SERVICE:-babyshop-dashboard}"
SCHEDULER_REGION="${SCHEDULER_REGION:-$REGION}"

SECRETS=(
  GOOGLE_ADS_DEVELOPER_TOKEN
  GOOGLE_ADS_CLIENT_ID
  GOOGLE_ADS_CLIENT_SECRET
  GOOGLE_ADS_REFRESH_TOKEN
  GOOGLE_ADS_LOGIN_CUSTOMER_ID
)

echo "== 0. Resolve the service's runtime service account =="
# --format on annotation keys with '/' breaks the projection language, but a plain
# field path is fine here (CLAUDE.md, "gcloud --format breaks on annotation keys").
RUNTIME_SA="$(gcloud run services describe "$SERVICE" \
  --project="$PROJECT" --region="$REGION" \
  --format='value(spec.template.spec.serviceAccountName)')"
if [[ -z "$RUNTIME_SA" ]]; then
  # An unset serviceAccountName means the service runs as the default compute SA.
  PROJECT_NUMBER="$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')"
  RUNTIME_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
fi
echo "   runtime SA: $RUNTIME_SA"

echo "== 1. Create the five secrets and add a version =="
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

echo "== 3. Verify the binding actually landed (never trust step 2's output) =="
for NAME in "${SECRETS[@]}"; do
  gcloud secrets get-iam-policy "$NAME" --project="$PROJECT" \
    --flatten="bindings[].members" \
    --filter="bindings.members:serviceAccount:${RUNTIME_SA}" \
    --format="value(bindings.role)" | sort -u | sed "s/^/   $NAME /"
done

echo "== 4. Wire the secrets onto the Cloud Run service =="
# --update-secrets (NOT --set-secrets): --set-secrets would drop any secret already
# mounted on this service. Same reason the deploy step uses --update-env-vars.
# --timeout=900: a full collection is ~5 GAQL queries x 8 accounts and the default
# 300 s request timeout is not comfortable headroom for it.
gcloud run services update "$SERVICE" \
  --project="$PROJECT" --region="$REGION" \
  --timeout=900 \
  --update-secrets="$(IFS=,; printf '%s' "$(for N in "${SECRETS[@]}"; do printf '%s=%s:latest,' "$N" "$N"; done)" | sed 's/,$//')"

echo "== 5. Confirm the new revision is the one serving traffic =="
gcloud run services describe "$SERVICE" --project="$PROJECT" --region="$REGION" \
  --format='value(status.latestReadyRevisionName,status.traffic[0].revisionName)'

echo "== 6. Daily Cloud Scheduler job =="
# Reuses the INTERNAL_TOKEN already on the service — read it back rather than asking for
# it again, so the two can never drift apart.
INTERNAL_TOKEN="${INTERNAL_TOKEN:-}"
if [[ -z "$INTERNAL_TOKEN" ]]; then
  echo "   !! \$INTERNAL_TOKEN is not exported. Export the same value the service has and"
  echo "      re-run, or create the job by hand with the command printed below."
else
  URL="$(gcloud run services describe "$SERVICE" --project="$PROJECT" --region="$REGION" \
        --format='value(status.url)')/internal/refresh-roas-sims"
  JOB=roas-sims-daily
  ACTION=create
  gcloud scheduler jobs describe "$JOB" --project="$PROJECT" \
    --location="$SCHEDULER_REGION" >/dev/null 2>&1 && ACTION=update
  gcloud scheduler jobs "$ACTION" http "$JOB" \
    --project="$PROJECT" \
    --location="$SCHEDULER_REGION" \
    --schedule="30 5 * * *" \
    --time-zone="Europe/Stockholm" \
    --uri="$URL" \
    --http-method=POST \
    --headers="X-Internal-Token=${INTERNAL_TOKEN}" \
    --attempt-deadline=900s
  echo "   ${ACTION}d $JOB -> $URL"
fi

cat <<'EOF'

== 7. Smoke test ==
   TOKEN=<the INTERNAL_TOKEN on the service>
   URL=$(gcloud run services describe babyshop-dashboard --region=europe-north1 \
         --format='value(status.url)')
   curl -sX POST -H "X-Internal-Token: $TOKEN" "$URL/internal/refresh-roas-sims" | head -c 2000
   #  503 {"status":"not-configured", ...}  -> a credential is still missing; it names which
   #  502 {"status":"error", ...}           -> every account failed; `accounts[]` says why
   #  200 {"status":"ok", "counts":{...}}   -> a snapshot was written

   curl -s "$URL/api/roas-sims?runs=1" | head -c 2000
   #  {"status":"no-snapshots", "error":"No ROAS simulation snapshots yet ..."}
   #  {"status":"ok", "rowCount":..., "runDates":[...]}

== 8. Optional: make the dashboard's access key real ==
   The ROAS Simulations page prompts for an access key and sends it as ?token=. Until
   ROAS_SIMS_TOKEN is set on the service, any key is accepted (the page is already behind
   the dashboard's own auth). To enforce it:
     KEY=$(openssl rand -hex 24)
     gcloud run services update babyshop-dashboard --region=europe-north1 \
       --update-env-vars="ROAS_SIMS_TOKEN=$KEY"
   Then enter $KEY once in the page's key gate.

== 9. The user-editable config ==
   The first refresh seeds roas_sim_config/config with the same defaults the sheet's
   Config tab carried (brand 0.20 / private-label 0.50 / generic 1.00, share floors and
   caps, GP2 multipliers). Edit it in the Firestore console or with the REST API — no
   deploy needed. See the module docstring in refresh_roas_sims.py.
EOF
