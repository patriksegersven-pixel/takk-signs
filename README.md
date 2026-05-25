# takk-signs

First app on the Claude → GitHub → Cloud Build → Cloud Run pipeline.

## Endpoints
- `GET /` — hello + revision
- `GET /healthz` — liveness probe
- `GET /firestore-test` — writes timestamp to Firestore `smoke-test/latest` and reads it back

## Stack
- Python 3.12 + Flask + Gunicorn
- Container built by Cloud Build, stored in Artifact Registry (`europe-north1-docker.pkg.dev/<project>/apps/takk-signs`)
- Deployed to Cloud Run (`europe-north1`)
- State in Firestore Native (`europe-north1`)

## Local dev
```bash
pip install -r requirements.txt
gcloud auth application-default login   # one-time
export GCP_PROJECT=project-a7ade44e-e7e3-4871-a83
python app.py
```

## Deploy
Just push to `main` — Cloud Build trigger handles the rest.
