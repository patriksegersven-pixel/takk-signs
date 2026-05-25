# takk-signs (monorepo)

All Claude-built apps, agents, and dashboards live in this repo. Each one is an independent service under `apps/<name>/`, with its own Cloud Build trigger that fires **only when its directory changes**.

## Layout

```
apps/
  takk-signs/        ← first app: Flask hello + Firestore round-trip
    app.py
    Dockerfile
    cloudbuild.yaml
    requirements.txt
    README.md
  <next-app>/        ← add new apps here
scripts/
  new-app.sh         ← scaffolds a new app + registers its trigger
```

## Pipeline

```
main branch push  →  Cloud Build trigger (per app)  →  build container  →
push to Artifact Registry  →  deploy to Cloud Run  →  done
```

- **GCP project:** `project-a7ade44e-e7e3-4871-a83`
- **Region:** `europe-north1` (Cloud Run, Artifact Registry, Firestore)
- **Selective builds:** each trigger has `--included-files=apps/<name>/**`, so a change to one app doesn't rebuild the others. A docs-only change (root `README.md`) triggers nothing.

## Add a new app

```bash
./scripts/new-app.sh my-new-service
```

This creates `apps/my-new-service/` from a template (Flask + Dockerfile + cloudbuild.yaml) and registers a Cloud Build trigger scoped to that directory. Push to main and Cloud Run deploys it.

## Per-app docs
- [takk-signs](apps/takk-signs/README.md)
