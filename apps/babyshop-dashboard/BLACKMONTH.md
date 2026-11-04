# Black Month tab — section contract

Operational war-room tab for Babyshop, November 2026 (Black Friday 27 Nov,
Cyber Monday 30 Nov). Black theme. Page: `babyshop-blackmonth.html`, nav id
`blackmonth`. Built from five independent sections, each owned by one file set:

| key         | what it answers                                                        |
|-------------|------------------------------------------------------------------------|
| `insights`  | Today's actions: agent insight cards (accept/reject), rule alerts, change log, checklist |
| `pacing`    | MTD + daily vs forecast/target, daily forecast of ad cost / revenue / GP1 / GP2, landing forecast, YoY event-aligned, new vs returning, intraday today |
| `paid`      | Google + Meta levers: spend vs plan, profit ROAS vs tROAS, budget-limited campaigns, lost IS, Meta allocation vs approved plan, new vs returning per channel, GP3 per market/channel |
| `creatives` | Meta creative scorecard with scale/keep/kill verdicts, fatigue, results by naming-convention tag |
| `offers`    | Live offers per market (depth/width), offer GP2 economics, hero-product stock and ad spend on out-of-stock |

## Files a section owns (and nothing else)

- `bm_<key>.py` — `build(ctx: dict) -> dict`. Must return a JSON-serialisable
  dict (dates as ISO strings) under 900 kB. Raise on hard failure; the job logs
  it and keeps the previous snapshot. Put data caveats in the payload (e.g.
  `"notes": [...]`) so the page can show them.
- `bm-<key>.js` — `BM.register('<key>', {title, render(el, data, state)})`.
  Rebuild `el` from scratch on every call; respect `state.market`
  (`'all'|'SE'|'NO'|'FI'|'DK'`) and `state.brand` (`'all'|'babyshop'|'lekmer'`;
  carry a `brand` field on rows, and say so on the page when a source cannot split by brand). Use the `BM.*` helpers in `bm-core.js` and only
  the CSS tokens/classes defined in `babyshop-blackmonth.html` (`.bm-kpis`,
  `.bm-kpi`, `.bm-grid-2`, `.bm-chart`, `.bm-table`, `.bm-pill`, `.bm-note`,
  colours via `BM.color.*`). Section-specific CSS: inject one `<style>` from the
  JS with selectors prefixed `#bm-<key>`.
- `bm-mock/<key>.json` — a realistic payload (real numbers from a dry run are
  best) for local preview.
- optional `bm_<key>_api.py` — FastAPI `router = APIRouter()` with routes under
  `/api/blackmonth/<key>/...` (live reads, POSTs). Auto-mounted behind Basic auth.
- optional helpers `bm_<key>_*.py`.

Shared, owned by the integrator — do NOT edit: `bm_common.py`, `bm-core.js`,
`babyshop-blackmonth.html`, `refresh_blackmonth.py`, `app.py`, `nav.js`,
`requirements.txt`, `Dockerfile`, `cloudbuild.yaml`. If you need a change there
(a new pip package, a new secret/env var, a new IAM grant, a shared constant),
say so in your final report instead.

## Constants (bm_common.py)

Period 1–30 Nov 2026; `BLACK_FRIDAY`, `CYBER_MONDAY`, `BLACK_WEEK`, last-year
equivalents; `MARKETS = SE NO FI DK` (+ROW bucket); `budget_lines()` = Finance's
November forecast (gross 52.9M, GP1 20.3M, GP2 14.0M, marketing 5.54M; global
only); approved Meta plan 200k (Kuling 60k / prospecting 20k / retargeting 120k;
SE 60 / NO 20 / FI 10 / DK 10); Google plan ~3.1M (SE 54 / NO 28 / FI 8 / DK 7 / ROW 3).

## Running

```
cd apps/babyshop-dashboard
python3 refresh_blackmonth.py --section <key> --dry-run --out bm-mock/<key>.json
```
ADC as patrik.segersven@gmail.com reads BigQuery/Firestore in
project-a7ade44e-e7e3-4871-a83. Local preview: `.claude/launch.json` config
`bm-<key>` (uvicorn, DEV_MODE=true), open `/babyshop-blackmonth.html?mock=1`.

## Rules

- Read-only against every ad platform. Never change bids, budgets or tROAS.
- Legacy-stack data rules apply (cost de-duplication via bq_source's CTEs,
  returns netting, ROW bucket) — reuse bq_source / existing refresh modules
  rather than re-deriving metrics.
- Don't edit existing modules (refresh_meta.py, daily_targets.py, …); import
  from them. If one needs a change, report it.
- No commits, pushes or deploys from a section session; the integrator does it.
