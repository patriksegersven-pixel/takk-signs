#!/usr/bin/env bash
# Tear down an app entirely: Cloud Run service, Cloud Build trigger,
# runtime SA, AR image stream, and local apps/<name>/ directory.
#
# Usage: ./scripts/delete-app.sh <name> [--yes]
#   --yes  Skip the confirmation prompt.
set -euo pipefail

NAME="${1:-}"
SKIP_CONFIRM=0
shift || true
for arg in "$@"; do
  case "$arg" in
    --yes) SKIP_CONFIRM=1 ;;
    *) echo "Unknown arg: $arg" >&2; exit 1 ;;
  esac
done

if [[ -z "$NAME" ]]; then
  echo "Usage: $0 <app-name> [--yes]" >&2
  exit 1
fi
if [[ ! "$NAME" =~ ^[a-z][a-z0-9-]{0,28}$ ]]; then
  echo "Invalid app name." >&2
  exit 1
fi

PROJECT_ID="project-a7ade44e-e7e3-4871-a83"
REGION="europe-north1"
REPO="apps"
RUNTIME_SA="${NAME}-run@${PROJECT_ID}.iam.gserviceaccount.com"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APP_DIR="$REPO_ROOT/apps/$NAME"

cat <<EOF
About to delete EVERYTHING for app: $NAME
  - Cloud Run service:      $NAME (region $REGION)
  - Cloud Build trigger:    ${NAME}-main
  - Runtime service account: $RUNTIME_SA
  - Artifact Registry images: $REGION-docker.pkg.dev/$PROJECT_ID/$REPO/$NAME (all tags)
  - Local directory:        apps/$NAME/
EOF

if [[ $SKIP_CONFIRM -eq 0 ]]; then
  read -r -p "Continue? Type the app name to confirm: " confirm
  if [[ "$confirm" != "$NAME" ]]; then
    echo "Aborted." >&2
    exit 1
  fi
fi

step() { echo ""; echo ">> $*"; }

step "Delete Cloud Run service"
gcloud run services delete "$NAME" --region="$REGION" --quiet 2>&1 | tail -3 || echo "  (not found)"

step "Delete Cloud Build trigger ${NAME}-main"
gcloud builds triggers delete "${NAME}-main" --region="$REGION" --quiet 2>&1 | tail -3 || echo "  (not found)"

step "Delete Artifact Registry image stream"
gcloud artifacts docker images delete "$REGION-docker.pkg.dev/$PROJECT_ID/$REPO/$NAME" --delete-tags --quiet 2>&1 | tail -3 || echo "  (no images to delete)"

step "Delete runtime service account"
gcloud iam service-accounts delete "$RUNTIME_SA" --quiet 2>&1 | tail -3 || echo "  (not found)"

step "Remove local apps/$NAME/"
if [[ -e "$APP_DIR" ]]; then
  rm -rf "$APP_DIR"
  echo "  removed"
  echo ""
  echo "Next: commit the deletion with"
  echo "  git add apps/$NAME"
  echo "  git commit -m \"delete app: $NAME\""
  echo "  git push"
else
  echo "  (no local dir to remove)"
fi

echo ""
echo "✓ App $NAME torn down."
