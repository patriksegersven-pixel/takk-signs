#!/usr/bin/env bash
# Scaffold a new app under apps/<name>/, create a dedicated runtime SA,
# and register a Cloud Build trigger scoped to that app's directory.
#
# Usage: ./scripts/new-app.sh <name> [--public]
#   --public  Allow unauthenticated invocations to the Cloud Run service.
#             Default is private (callers need a valid IAM token).
set -euo pipefail

NAME="${1:-}"
PUBLIC=0
shift || true
for arg in "$@"; do
  case "$arg" in
    --public) PUBLIC=1 ;;
    *) echo "Unknown arg: $arg" >&2; exit 1 ;;
  esac
done

if [[ -z "$NAME" ]]; then
  echo "Usage: $0 <app-name> [--public]" >&2
  exit 1
fi
if [[ ! "$NAME" =~ ^[a-z][a-z0-9-]{0,28}$ ]]; then
  echo "App name must be lowercase, start with a letter, only [a-z0-9-], max 29 chars (SA name limit)." >&2
  exit 1
fi

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APP_DIR="$REPO_ROOT/apps/$NAME"

if [[ -e "$APP_DIR" ]]; then
  echo "apps/$NAME already exists" >&2
  exit 1
fi

PROJECT_ID="project-a7ade44e-e7e3-4871-a83"
PROJECT_NUMBER="871631085269"
REGION="europe-north1"
CONNECTION="github-conn"
REPO_NAME="takk-signs"
BUILD_SA="projects/${PROJECT_ID}/serviceAccounts/${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
RUNTIME_SA="${NAME}-run@${PROJECT_ID}.iam.gserviceaccount.com"
AUTH_FLAG="--no-allow-unauthenticated"
[[ $PUBLIC -eq 1 ]] && AUTH_FLAG="--allow-unauthenticated"

echo ">> Creating runtime service account ${NAME}-run"
gcloud iam service-accounts create "${NAME}-run" \
  --display-name="Cloud Run runtime SA for ${NAME}" \
  --project="$PROJECT_ID" >/dev/null

echo ">> Granting datastore.user (Firestore) to runtime SA"
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${RUNTIME_SA}" \
  --role="roles/datastore.user" \
  --condition=None --quiet >/dev/null

echo ">> Allowing Cloud Build SA to act as ${NAME}-run during deploy"
gcloud iam service-accounts add-iam-policy-binding "$RUNTIME_SA" \
  --member="serviceAccount:${PROJECT_NUMBER}-compute@developer.gserviceaccount.com" \
  --role="roles/iam.serviceAccountUser" --quiet >/dev/null

echo ">> Scaffolding apps/$NAME/"
mkdir -p "$APP_DIR"

cat > "$APP_DIR/app.py" <<EOF
import os
from flask import Flask, jsonify

app = Flask(__name__)

@app.route("/")
def index():
    return jsonify(service="$NAME", revision=os.environ.get("K_REVISION", "local"))

@app.route("/health")
def health():
    return jsonify(status="ok")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
EOF

cat > "$APP_DIR/requirements.txt" <<'EOF'
Flask==3.0.3
gunicorn==23.0.0
google-cloud-firestore==2.20.0
EOF

cat > "$APP_DIR/Dockerfile" <<'EOF'
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV PORT=8080
EXPOSE 8080
CMD exec gunicorn --bind :$PORT --workers 2 --threads 8 --timeout 0 app:app
EOF

cat > "$APP_DIR/.dockerignore" <<'EOF'
.git
.gitignore
.dockerignore
__pycache__
*.pyc
*.pyo
.venv
venv
.env
.env.*
README.md
cloudbuild.yaml
.DS_Store
EOF

cat > "$APP_DIR/cloudbuild.yaml" <<EOF
substitutions:
  _SERVICE: $NAME
  _APP_DIR: apps/$NAME
  _REGION: $REGION
  _REPO: apps
  _MAX_INSTANCES: "10"

steps:
  - id: build
    name: gcr.io/cloud-builders/docker
    dir: \${_APP_DIR}
    args:
      - build
      - -t
      - \${_REGION}-docker.pkg.dev/\$PROJECT_ID/\${_REPO}/\${_SERVICE}:\$COMMIT_SHA
      - .

  - id: push
    name: gcr.io/cloud-builders/docker
    args:
      - push
      - \${_REGION}-docker.pkg.dev/\$PROJECT_ID/\${_REPO}/\${_SERVICE}:\$COMMIT_SHA

  - id: deploy
    name: gcr.io/google.com/cloudsdktool/cloud-sdk
    entrypoint: gcloud
    args:
      - run
      - deploy
      - \${_SERVICE}
      - --image=\${_REGION}-docker.pkg.dev/\$PROJECT_ID/\${_REPO}/\${_SERVICE}:\$COMMIT_SHA
      - --region=\${_REGION}
      - --platform=managed
      - ${AUTH_FLAG}
      - --service-account=${NAME}-run@\$PROJECT_ID.iam.gserviceaccount.com
      - --max-instances=\${_MAX_INSTANCES}
      - --set-env-vars=GCP_PROJECT=\$PROJECT_ID

images:
  - \${_REGION}-docker.pkg.dev/\$PROJECT_ID/\${_REPO}/\${_SERVICE}:\$COMMIT_SHA

options:
  logging: CLOUD_LOGGING_ONLY
EOF

cat > "$APP_DIR/README.md" <<EOF
# $NAME

Deployed to Cloud Run in \`$REGION\`. Image at \`$REGION-docker.pkg.dev/$PROJECT_ID/apps/$NAME\`.
Runtime service account: \`${RUNTIME_SA}\` (Firestore access only).
Public: $([[ $PUBLIC -eq 1 ]] && echo yes || echo "no — caller needs an IAM token")

## Endpoints
- \`GET /\` — hello + revision
- \`GET /health\` — liveness probe (don't use /healthz — Google's edge intercepts it)

## Adding a secret
\`\`\`bash
echo -n "my-secret-value" | gcloud secrets create ${NAME}-API_KEY --data-file=- --replication-policy=automatic
gcloud secrets add-iam-policy-binding ${NAME}-API_KEY \\
  --member="serviceAccount:${RUNTIME_SA}" --role="roles/secretmanager.secretAccessor"
# then in cloudbuild.yaml deploy step, add:  --update-secrets=API_KEY=${NAME}-API_KEY:latest
\`\`\`
EOF

echo ">> Registering Cloud Build trigger '${NAME}-main'"
gcloud builds triggers create github \
  --name="${NAME}-main" \
  --region="$REGION" \
  --repository="projects/${PROJECT_ID}/locations/${REGION}/connections/${CONNECTION}/repositories/${REPO_NAME}" \
  --branch-pattern='^main$' \
  --build-config="apps/${NAME}/cloudbuild.yaml" \
  --included-files="apps/${NAME}/**" \
  --service-account="$BUILD_SA"

echo ""
echo "✓ Done. Next: edit apps/$NAME/, commit, push to main — Cloud Build will deploy."
[[ $PUBLIC -eq 0 ]] && echo "  (Service is private. To call it: curl -H \"Authorization: Bearer \$(gcloud auth print-identity-token)\" <url>)"
