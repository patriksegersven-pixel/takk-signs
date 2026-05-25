#!/usr/bin/env bash
# Scaffold a new app under apps/<name>/ and register a Cloud Build trigger for it.
# Usage: ./scripts/new-app.sh <name>
set -euo pipefail

NAME="${1:-}"
if [[ -z "$NAME" ]]; then
  echo "Usage: $0 <app-name>" >&2
  exit 1
fi
if [[ ! "$NAME" =~ ^[a-z][a-z0-9-]{0,49}$ ]]; then
  echo "App name must be lowercase, start with a letter, and use only [a-z0-9-] (max 50 chars)." >&2
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
SA="projects/${PROJECT_ID}/serviceAccounts/${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"

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

steps:
  - id: build
    name: gcr.io/cloud-builders/docker
    dir: \${_APP_DIR}
    args:
      - build
      - -t
      - \${_REGION}-docker.pkg.dev/\$PROJECT_ID/\${_REPO}/\${_SERVICE}:\$COMMIT_SHA
      - -t
      - \${_REGION}-docker.pkg.dev/\$PROJECT_ID/\${_REPO}/\${_SERVICE}:latest
      - .

  - id: push
    name: gcr.io/cloud-builders/docker
    args:
      - push
      - --all-tags
      - \${_REGION}-docker.pkg.dev/\$PROJECT_ID/\${_REPO}/\${_SERVICE}

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
      - --allow-unauthenticated
      - --set-env-vars=GCP_PROJECT=\$PROJECT_ID

images:
  - \${_REGION}-docker.pkg.dev/\$PROJECT_ID/\${_REPO}/\${_SERVICE}:\$COMMIT_SHA
  - \${_REGION}-docker.pkg.dev/\$PROJECT_ID/\${_REPO}/\${_SERVICE}:latest

options:
  logging: CLOUD_LOGGING_ONLY
EOF

cat > "$APP_DIR/README.md" <<EOF
# $NAME

Deployed to Cloud Run in \`$REGION\`. Image at \`$REGION-docker.pkg.dev/$PROJECT_ID/apps/$NAME\`.

## Endpoints
- \`GET /\` — hello + revision
- \`GET /health\` — liveness probe (don't use /healthz — Google's edge intercepts it)
EOF

echo ">> Registering Cloud Build trigger '${NAME}-main'"
gcloud builds triggers create github \
  --name="${NAME}-main" \
  --region="$REGION" \
  --repository="projects/${PROJECT_ID}/locations/${REGION}/connections/${CONNECTION}/repositories/${REPO_NAME}" \
  --branch-pattern='^main$' \
  --build-config="apps/${NAME}/cloudbuild.yaml" \
  --included-files="apps/${NAME}/**" \
  --service-account="$SA"

echo ""
echo "✓ Done. Next: edit apps/$NAME/, commit, push to main — Cloud Build will deploy."
