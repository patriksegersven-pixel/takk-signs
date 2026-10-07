# Opportunity engine

Finds business-development opportunities in Babyshop's own data, sizes each as
incremental GP2, and keeps one de-duplicated, ranked list — the
**Opportunities** tab (`/babyshop-opportunities.html`).

## Parts

| Part | Where | Role |
|---|---|---|
| Rules | `RULES.md` | Data access, metric conventions, evidence bar, sizing model, output format. Every lens follows it. |
| Lenses | `lenses/*.md` | One playbook per domain: `crm`, `demand`, `operations`, `offer`, `conversion`, `explorer`. |
| Ledger | `../opp_ledger.py` | The only writer. Validates, sizes, applies the floor, upserts by fingerprint. Firestore `bizdev_opportunities` + `bizdev_needs_data`. |
| Page + API | `../babyshop-opportunities.html`, `/api/opportunities` | Ranked list, effort × impact matrix, accept/reject. |
| Run archive | `runs/` | The curated candidate file of every ingested run. |

## One cycle

1. **Lens run.** An agent is given `RULES.md`, one lens playbook, the current
   ledger extract (levers + statuses) and an output path. It queries the
   warehouse read-only and writes a candidate JSON file.
2. **Curate.** Check each candidate's numbers against the warehouse, merge
   near-duplicates (the ingest report lists `same_lever` twins), tighten
   uplifts that the evidence does not carry, and add a `curator_note` where
   the sizing was changed.
3. **Ingest.** `python3 opp_ledger.py ingest runs/<file>.json --dry-run`, then
   without `--dry-run`. Candidates under the floor are dropped; an item that
   already exists is updated in place; a rejected item is left alone.
4. **Decide.** Accept or reject on the page. A rejection needs a reason and
   sticks. Accepted items move to piloting → shipped → measured.
5. **Calibrate.** When an item is measured, compare the result with its
   sizing and adjust the uplift ranges the lenses are allowed to use.

## The model in one place

- Floor: 10 000 kr incremental GP2 per month, base case, full rollout.
- GP2/month = baseline × uplift (× flow-through for volume drivers).
- Confidence: measured 0.8, cross-market 0.5, benchmark 0.3.
- Effort in person-weeks: S 1, M 3, L 8, XL 20.
- Score = GP2/month × confidence ÷ effort weeks.
- Finland is the test market: `fi_pilot` items run there for 4–6 weeks
  against SE/NO/DK before rollout.

Constants live in `opp_ledger.py`; `RULES.md` mirrors them for the lenses.
Change both together.

## Not built yet

- Lens runs are started by hand; nothing is scheduled.
- `offer`, `conversion` and `explorer` have playbooks but have not been run.
- Measured results are recorded as a status only; the calibration step is
  manual.
