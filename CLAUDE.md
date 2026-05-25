# Project conventions

This is the `takk-signs` monorepo — all Claude-built apps, agents, and dashboards live under `apps/<name>/`. Each one is an independent Cloud Run service.

## Pipeline overview

- Every directory under `apps/` is one deployable service
- Each app has its own Cloud Build trigger with `--included-files=apps/<name>/**` filter — a change to one app does NOT rebuild others; a docs-only change to root files triggers nothing
- Triggers fire on push to `main` only
- Each trigger uses the app's own `apps/<name>/cloudbuild.yaml`
- After every push, verify the target app actually deployed:
  ```bash
  gcloud builds list --region=europe-north1 --limit=5
  gcloud builds log <id> --region=europe-north1
  ```
  A failed Cloud Run deploy silently keeps the previous revision serving traffic.

## Stack

- Python 3.12 + Flask + Gunicorn (canonical app shape — see `apps/takk-signs/`)
- Container build via Cloud Build, stored in Artifact Registry at `europe-north1-docker.pkg.dev/project-a7ade44e-e7e3-4871-a83/apps/<service>`
- Deploys to Cloud Run in `europe-north1`
- Firestore Native in `europe-north1` for persistence

## Cloud Build YAML

- Always escape shell variables as `$$VAR` (e.g. `$$DIRS`, `$$IMAGE`)
- Only `$PROJECT_ID`, `$COMMIT_SHA`, `$BUILD_ID` use single `$` — these are Cloud Build built-ins
- Never use bare `$VARIABLE` for shell logic inside steps
- **Substitution defaults do NOT recursively expand other substitutions.** Defining `_RUNTIME_SA: foo@$PROJECT_ID.iam...` keeps `$PROJECT_ID` literal. Expand `$PROJECT_ID` directly in step `args:` instead (where Cloud Build does substitute it).
- Builds using a non-default service account REQUIRE `options.logging: CLOUD_LOGGING_ONLY` (or `logs_bucket`). Without one, builds fail at scheduling time.
- 2nd-gen GitHub triggers REQUIRE `--service-account=projects/<PROJECT_ID>/serviceAccounts/<sa>` on `gcloud builds triggers create github` and on `update`. Without it: `INVALID_ARGUMENT` with no detail.

## Docker

- Default Dockerfile shape (Python): see `apps/takk-signs/Dockerfile` — `python:3.12-slim`, install requirements, gunicorn 2 workers × 8 threads
- Chain build and push with `&&` when using shell-form steps so failed builds never push images
- Never use `*.md` in `.dockerignore` without explicitly adding `!PROMPT.md` as exception if you have one
- Always verify the image exists before the Cloud Run deploy step
- Push only `:$COMMIT_SHA` — don't push `:latest` (concurrent builds race the tag)

## npm (for any future TypeScript app)

- Always use `npm install` not `npm ci` unless a `package-lock.json` is confirmed in the repo
- Never add `package-lock.json` to `.gitignore`
- Always commit `package-lock.json`

## Local docker build for TypeScript apps (mandatory before push)

TS apps are more fragile to package-resolution bugs than Python — `npm install <path>` symlinks rather than copies, peer-dep handling differs by npm version. Before pushing any change to a TS app's `Dockerfile`, `src/`, `package.json`, or `tsconfig.json`:
```bash
docker build apps/<name>
```
One ~3-min local build saves a ~10-min full Cloud Build cycle. Skip ONLY if the change is plainly text-only.

Python apps are more forgiving — `python -m py_compile apps/<name>/app.py` syntax check usually suffices.

## Anthropic SDK

- Always use `@anthropic-ai/sdk` version `0.80.0` or higher (TS) / `anthropic` `0.40.0`+ (Python)
- Use `messages.create()` not `messages.parse()`
- Model IDs use bare names, no date suffix: `claude-sonnet-4-6`, `claude-opus-4-7`, `claude-haiku-4-5`
- Always test a new API key with a curl call before deploying it to a service

## Firestore

### Dotted keys in `.set(merge=True)`

- `doc.set({"daily_data.2025-04-09": {...}}, merge=True)` does NOT create a nested path — it creates a LITERAL top-level field named `daily_data.2025-04-09`. After 365 days you hit the 20k-field doc limit.
- Always use nested dicts instead: `doc.set({"daily_data": {"2025-04-09": {...}}}, merge=True)` — Firestore deep-merges nested maps correctly.
- Dotted-path syntax only works with `update()`, not `set()`.

### Collection isolation

- NEVER share a Firestore collection between two features that write different schemas. Even with `merge=True`, a write from feature A can make feature B's existence checks return the wrong answer.
- Give each feature its own clearly-named collection. The extra collection cost is zero; the debugging cost of a collision is hours.

### Query patterns

- `where + order_by` requires a composite index. Same for `where + where`.
- Default: don't use Firestore `order_by` at all on small collections — pull all docs for the partition key (limit 200), sort + filter Python-side.
- For collections that DO need server-side ordering (>1000 docs/partition), pre-create the index via `gcloud firestore indexes composite create` BEFORE deploying the code that uses it. Indexes take 1-3 min to build.

### Operational

- `gcloud` has NO `firestore documents` subcommand. For ad-hoc reads/writes use the REST API with your gcloud auth token:
  ```bash
  TOKEN=$(gcloud auth print-access-token)
  PROJ=project-a7ade44e-e7e3-4871-a83
  curl -sH "Authorization: Bearer $TOKEN" \
    "https://firestore.googleapis.com/v1/projects/$PROJ/databases/(default)/documents/coll/doc"
  ```

## MCP on Cloud Run (FastMCP) — when you add an MCP service

### New services

- Every new Cloud Run MCP service should include OAuth (FastMCP GoogleProvider) by default. Only skip OAuth if explicitly not needed. Never start with `--allow-unauthenticated` and retrofit later.
- Never write imports, OAuth setup, or server boilerplate from memory — always copy from the most similar working service and modify from there.
- Before writing `cloudbuild.yaml` for a new service, verify the SA has `secretAccessor` on every secret you reference.

### OAuth with FastMCP GoogleProvider

- `required_scopes` controls BOTH scope validation AND what scopes are requested upstream. For user email use `["openid", "https://www.googleapis.com/auth/userinfo.email"]` (full URL form).
- After publishing to Production, add server-side domain filtering via `FASTMCP_ALLOWED_EMAIL_DOMAINS` — consent screen alone won't restrict by domain.
- When creating a new OAuth client, immediately add the Cloud Run redirect URI `https://<service-url>/auth/callback` to Authorized redirect URIs. FastMCP GoogleProvider uses `/auth/callback`, not `/callback`.

### Firestore as OAuth token store

- FastMCP's default file-based token store causes infinite 401 loops under concurrent connections (Claude.ai sends parallel requests). Use Firestore via `key_value.aio.stores.firestore.FirestoreStore` as `client_storage`.
- Firestore document IDs cannot contain `/`. MCP client IDs like `https://claude.ai/oauth/...` contain slashes. Always wrap `FirestoreStore` with a key-encoding layer (base64 URL-safe encode). The `put()` method must accept `ttl` parameter, `get/delete` must accept `collection`.

### Long-running async work

- MCP tools that start `asyncio.create_task(...)` for background work need `--min-instances=1` + `--no-cpu-throttling`. Otherwise the container scales to zero after the originating request ends, killing the background task mid-execution.
- Always save state incrementally inside long loops (every N items, not just at end). When the container DOES get recycled, partial progress in GCS/Firestore lets the next run resume.

### Long-running MCP tools

- MCP tools that may run >60s should return immediately with a job ID and provide a polling/status tool
- MCP tools calling paid APIs must be idempotent or have run-locks. Claude.ai retries on timeout; without a lock, each retry triggers duplicate API calls.
- In-memory state lost on server restart. For production, use Firestore-backed job store instead of in-memory dicts.

### Starlette + FastMCP lifespan

When wrapping FastMCP's `http_app()` in a Starlette app, ALWAYS pass `lifespan=mcp_asgi.lifespan` to the Starlette constructor. Without this, FastMCP's StreamableHTTPSessionManager fails with "Task group is not initialized".

### FastMCP circular imports

In FastMCP projects, always create `mcp_server` in a separate `app.py` module — never in `server.py` which imports tool modules. Tool files import from `app.py`, `server.py` imports both.

## Cloud Run gotchas (apply to ALL services)

- `--set-env-vars` uses commas as key=value separators. If a VALUE contains commas, use alternate delimiter syntax: `--set-env-vars "^;^KEY1=val1;KEY2=val,with,commas"`
- `host="0.0.0.0"` is required for any HTTP server on Cloud Run — `127.0.0.1` won't accept external traffic
- After deploying, verify the latest revision is actually serving traffic. Failed revisions silently keep traffic on the old one:
  ```bash
  gcloud run services describe <service> --region=europe-north1 \
    --format='value(status.latestReadyRevisionName,status.traffic[0].revisionName)'
  ```
- **Google Frontend reserves `/health*` paths** (intercepts `/healthz`, `/livez`, `/readyz`) — request never reaches the container, you get a Google-branded 404. Use `/health` (no `-z`) or `/_status`.
- Updating a GCP secret version does NOT take effect on running revisions — secrets are cached at revision start. Trigger a new revision after rotating a secret:
  ```bash
  gcloud run services update <service> --region=europe-north1 \
    --update-labels="secret-rotation=$(date +%s)"
  ```

## Google APIs

- When calling Google APIs via REST, always verify the API version is current — check against client library version or official docs
- Always pass `supportsAllDrives: true` for Google Drive operations — files may be in Shared Drives
- When updating Google Sheets, use `batchUpdate` to update only changed cells, preserving other data
- When appending rows to Sheets, read the actual row number from `appendRes.data.updates.updatedRange` — never count rows
- Service accounts have NO Drive storage quota. Always copy into a Shared Drive folder where the SA has `canAddChildren=true`.

## Pre-push checklist (steel-man)

After completing any code change, perform two rounds of adversarial self-review BEFORE pushing:

1. **Round 1**: Actively argue against your implementation — field name mismatches, missing error handling, wrong assumptions, missing dependencies, edge cases with empty data, type conversion bugs. Fix everything found.
2. **Round 2**: Steel-man again after the fixes — verify fixes are correct, check for regressions, confirm against authoritative sources where needed.

For deploy changes verify ALL of these before pushing:
1. **Imports** — every import path exists. Grep the installed package or copy from working service. Never guess.
2. **Secrets** — every secret in `cloudbuild.yaml --update-secrets` is accessible by the service's SA:
   ```bash
   gcloud run services describe <service> --region=europe-north1 \
     --format='value(spec.template.spec.serviceAccountName)'
   gcloud secrets get-iam-policy <secret>
   ```
3. **Module paths** — every new file is importable
4. **OAuth redirect URIs** — registered BEFORE first deploy if new OAuth client
5. **GCS buckets** — exist before deploy if referenced via env var (idempotent: `gsutil mb -l europe-north1 -p project-a7ade44e-e7e3-4871-a83 gs://foo`)
6. **Firestore queries** — any `where + order_by` needs a composite index OR refactor to single-where + Python-side sort

## Default infrastructure

- **GCP project:** `project-a7ade44e-e7e3-4871-a83` (number `871631085269`, name "Data Visualization")
- **Region:** `europe-north1` (Cloud Run, Artifact Registry, Firestore, Cloud Build)
- **Artifact Registry:** `europe-north1-docker.pkg.dev/project-a7ade44e-e7e3-4871-a83/apps`
- **Cloud Build GitHub connection:** `github-conn`, linked repo `takk-signs`
- **Cloud Build service account:** `871631085269-compute@developer.gserviceaccount.com` (default compute SA)
- **Runtime SA pattern:** `<service>-run@project-a7ade44e-e7e3-4871-a83.iam.gserviceaccount.com` — one per service, scoped to `roles/datastore.user` by default (Firestore access only). Add more roles per app as needed.
- **Firestore:** Native mode, single `(default)` database in `europe-north1`

## Adding a new app

```bash
./scripts/new-app.sh <name>           # private (default — caller needs IAM token)
./scripts/new-app.sh <name> --public  # --allow-unauthenticated
```

This:
1. Creates `apps/<name>/` with Flask app, Dockerfile, cloudbuild.yaml
2. Creates a dedicated runtime SA `<name>-run@…` with `roles/datastore.user`
3. Grants Cloud Build SA `iam.serviceAccountUser` on the new runtime SA
4. Registers a Cloud Build trigger `<name>-main` scoped to `apps/<name>/**`

After running, `git add apps/<name>/` and push — Cloud Build deploys.

## Known open items

- **Compute SA holds `roles/owner`** on the project. This is broader than needed for build & deploy. Until reduced, a compromised app could escalate (apps use their own scoped runtime SA, so the blast radius from a running container is bounded — but the build SA itself is over-privileged). To fix: reduce to specific roles (`run.admin`, `artifactregistry.writer`, `iam.serviceAccountUser`, `logging.logWriter`, `storage.admin`, `cloudbuild.builds.builder`) and remove `owner`.
- **No CI tests** in `cloudbuild.yaml`. A Python syntax error in `app.py` builds and pushes successfully, then Cloud Run boot-loops. Old revision keeps serving (safe), but next deploy fails until fixed. Add a `python -m py_compile` step at minimum.
- **No branch protection on `main`.** Anyone with push access can deploy directly. Consider enabling required-PR + status-check via `gh api repos/<owner>/<repo>/branches/main/protection`.
- **No build notifications.** A failed Cloud Build run is silent unless you check `gcloud builds list`. Wire Pub/Sub → Slack/email when this matters.

## Git commits

- NEVER add `Co-Authored-By: Claude ...` or any other Claude/Anthropic attribution to commit messages
- NEVER add "Generated with Claude Code" or similar footers
- Commit messages should contain only the substantive change description

## CRITICAL: Deployment safety

When deploying a specific app, ONLY stage and commit files for that app's directory. Never use `git add -A`, `git add .`, or stage files from other directories. A push to `main` triggers Cloud Build, which deploys ALL apps whose directories changed — staging unrelated changes (even accidentally) can redeploy other services with untested modifications. Always use explicit file paths: `git add apps/<name>/file1 apps/<name>/file2`.
