# babyshop-dashboard

Serves the Babyshop GP3 optimization dashboard at
`/babyshop-dashboard.html` (and `/`) on Cloud Run in `europe-north1`.

The page itself (`babyshop-dashboard.html`) is BUILT in the
[patriksegersven-pixel/babyshop](https://github.com/patriksegersven-pixel/babyshop)
repo (canonical file: `index.html` on branch
`claude/babyshop-gp3-optimization-co1auz`). To release a new version, copy the
latest build over `apps/babyshop-dashboard/babyshop-dashboard.html` here and
push to `main` — the `babyshop-dashboard-main` Cloud Build trigger deploys it.

## Endpoints
- `GET /` and `GET /babyshop-dashboard.html` — the dashboard
- `GET /health` — liveness probe

Note: the deploy step intentionally sets no `--service-account`, so the service
keeps whatever runtime identity it already has (the page is static; it needs none).
