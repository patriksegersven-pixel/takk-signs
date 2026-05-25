# hello-test

Deployed to Cloud Run in `europe-north1`. Image at `europe-north1-docker.pkg.dev/project-a7ade44e-e7e3-4871-a83/apps/hello-test`.
Runtime service account: `hello-test-run@project-a7ade44e-e7e3-4871-a83.iam.gserviceaccount.com` (Firestore access only).
Public: no — caller needs an IAM token

## Endpoints
- `GET /` — hello + revision
- `GET /health` — liveness probe (don't use /healthz — Google's edge intercepts it)

## Adding a secret
```bash
echo -n "my-secret-value" | gcloud secrets create hello-test-API_KEY --data-file=- --replication-policy=automatic
gcloud secrets add-iam-policy-binding hello-test-API_KEY \
  --member="serviceAccount:hello-test-run@project-a7ade44e-e7e3-4871-a83.iam.gserviceaccount.com" --role="roles/secretmanager.secretAccessor"
# then in cloudbuild.yaml deploy step, add:  --update-secrets=API_KEY=hello-test-API_KEY:latest
```
