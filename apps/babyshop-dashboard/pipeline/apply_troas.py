#!/usr/bin/env python3
"""
Apply target-ROAS changes to Babyshop/Lekmer campaigns — THE ONLY sanctioned way.

WHY THIS FILE EXISTS
  Every applied target change must land in roas_sims.target_changes with the model's
  prediction at apply time, or it can never be scored and the calibration never
  learns. That rule lived in docs and memory, and on 2026-09-15 an ad-hoc script in
  another session applied 22 changes without logging one. This tool makes the log a
  side effect of applying, and adds two guards the ad-hoc path had none of:

    cooldown   refuses a campaign changed within roas_sims_bq.COOLDOWN_DAYS (14):
               the bidder re-learns for 1–2 weeks and a second step inside that
               window destroys the clean post-window the scoring needs. Override
               with --force-cooldown when you know what you are doing.
    step cap   refuses a move over 20 % of the current target (Google's guidance;
               the 2026-08-18 uncapped apply produced 1–2 weeks of noise).
               Override with --uncapped.

  (A daily reconciler in roas_sims_bq.py also logs anything that bypasses this tool,
  from the Ads change_event history — with a raw-curve prediction and a source that
  says so. It is the safety net, not the path.)

USAGE
  # 1. creds — never write them to a file; export inline from Secret Manager
  P=project-a7ade44e-e7e3-4871-a83
  for k in GOOGLE_ADS_DEVELOPER_TOKEN GOOGLE_ADS_CLIENT_ID GOOGLE_ADS_CLIENT_SECRET \
           GOOGLE_ADS_REFRESH_TOKEN GOOGLE_ADS_LOGIN_CUSTOMER_ID; do
    export $k="$(gcloud secrets versions access latest --secret=$k --project=$P)"; done

  # 2a. plan: JSON list of {"customer_id", "campaign_id", "new_target"}; names optional
  python3 pipeline/apply_troas.py --plan plan.json                     # dry run: validateOnly
  python3 pipeline/apply_troas.py --plan plan.json --apply --source rec-final-capped-2026-09-21

  # 2b. or let the calibrated view write the plan (the weekly routine's path):
  python3 pipeline/apply_troas.py --from-recs --write-plan plan.json   # dry run
  python3 pipeline/apply_troas.py --from-recs --apply --source rec-final-weekly-2026-09-29
     --from-recs selects generic-class campaigns (1:1 incrementality; brand and
     private-label recs are directional only) that are NOT in cooldown, spend at
     least --min-spend (1000/week, account currency) and whose rec_final differs
     from the live target by at least --min-move (5 %). Each step is clipped to the
     ±20 % cap and rounded to 2 dp. The result is an ordinary plan, so every guard
     below still runs on it.

  Dry run prints the full table (live target, calibrated rec, cooldown, step %, the
  predicted Δcost/ΔGP3 that would be logged) and runs every mutate with
  validate_only=True. --apply repeats the validation, mutates for real, reads every
  target back, and appends one target_changes row per campaign.

  Run from apps/babyshop-dashboard with the gmail account active for BigQuery
  (CLOUDSDK_CORE_ACCOUNT=patrik.segersven@gmail.com).
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import roas_sims_bq as rsb  # noqa: E402
from refresh_roas_sims import ACCOUNTS, ads_client, missing_credentials  # noqa: E402

STEP_CAP_PCT = 20.0
KAPPA_SANE = (0.5, 2.0)   # --from-recs: skip campaigns whose κ says the sim is off by >2x
ACCOUNT_BY_CID = {a["cid"]: a for a in ACCOUNTS}
CID_BY_LABEL = {a["label"]: a["cid"] for a in ACCOUNTS}


def plan_from_recs(min_spend: float, min_move_pct: float, classes: tuple[str, ...]) -> list[dict]:
    """Build a capped plan from v_calibrated_recs — the weekly routine's input.

    Selection: inc_class in `classes`, not in cooldown, avg 7-day spend ≥ min_spend,
    |rec_final − current| ≥ min_move_pct of current, and BOTH κ factors inside
    KAPPA_SANE — a κ of 3 means Google's curve and the measured actuals disagree by
    3x, which is a handful of conversions in a tiny market (ROW), not a level to
    optimise on. Step clipped to ±STEP_CAP_PCT. Campaigns whose live target is 0
    (MCV without a target) never appear in the view (current_target > 0 filter), so
    setting a FIRST target stays a manual decision.
    """
    from google.cloud import bigquery

    sql = f"""
        SELECT r.customer_name, r.strategy_id, r.strategy_name, r.current_target,
               r.rec_final, r.gate, r.cooldown, r.days_since_change, k.avg_7d_cost,
               r.k_cost, r.k_value
        FROM `{rsb.T("v_calibrated_recs")}` r
        LEFT JOIN `{rsb.T("v_kappa_calibrated")}` k USING (customer_name, strategy_id)
        WHERE r.inc_class IN UNNEST(@classes) AND r.current_target > 0
        ORDER BY r.customer_name, k.avg_7d_cost DESC
    """
    job = rsb.bq().query(sql, job_config=bigquery.QueryJobConfig(query_parameters=[
        bigquery.ArrayQueryParameter("classes", "STRING", list(classes))]))
    plan, why_not = [], []
    for r in job.result():
        cur, rec = float(r.current_target), float(r.rec_final)
        spend = float(r.avg_7d_cost or 0)
        move_pct = 100 * (rec / cur - 1)
        cid = CID_BY_LABEL.get(r.customer_name)
        skip = None
        if not cid:
            skip = "unknown account"
        elif r.cooldown:
            skip = f"cooldown {r.days_since_change}d"
        elif spend < min_spend:
            skip = f"spend {spend:,.0f}/wk < {min_spend:,.0f}"
        elif abs(move_pct) < min_move_pct:
            skip = f"rec within {min_move_pct:g}% ({move_pct:+.1f}%)"
        elif not (KAPPA_SANE[0] <= float(r.k_cost or 1) <= KAPPA_SANE[1]
                  and KAPPA_SANE[0] <= float(r.k_value or 1) <= KAPPA_SANE[1]):
            skip = (f"κ unreliable (cost {float(r.k_cost or 1):.2f}, value "
                    f"{float(r.k_value or 1):.2f}; sane band {KAPPA_SANE[0]}–{KAPPA_SANE[1]})")
        if skip:
            why_not.append((r.customer_name, r.strategy_name, skip))
            continue
        capped = max(-STEP_CAP_PCT, min(STEP_CAP_PCT, move_pct))
        plan.append({"customer_id": cid, "campaign_id": str(r.strategy_id),
                     "name": f"{r.customer_name} {r.strategy_name}",
                     "new_target": round(cur * (1 + capped / 100), 2),
                     "rec_final": rec, "clipped": abs(capped) < abs(move_pct) - 1e-9})
    if why_not:
        print(f"--from-recs skipped {len(why_not)} campaign(s):")
        for a, n, w in why_not:
            print(f"  {a:<12} {n[:40]:<40} {w}")
    return plan


def _live_campaigns(svc, plan: list[dict]) -> dict[tuple[str, str], dict]:
    """(customer_id, campaign_id) → live name/status/strategy/target."""
    out: dict[tuple[str, str], dict] = {}
    by_cid: dict[str, list[str]] = {}
    for p in plan:
        by_cid.setdefault(p["customer_id"], []).append(p["campaign_id"])
    for cid, ids in by_cid.items():
        q = ("SELECT campaign.id, campaign.name, campaign.status, "
             "campaign.bidding_strategy_type, campaign.bidding_strategy, "
             "campaign.target_roas.target_roas, "
             "campaign.maximize_conversion_value.target_roas "
             f"FROM campaign WHERE campaign.id IN ({','.join(ids)})")
        for r in svc.search(customer_id=cid, query=q):
            c = r.campaign
            st = c.bidding_strategy_type.name
            if st == "TARGET_ROAS":
                field, cur = "target_roas", c.target_roas.target_roas
            elif st == "MAXIMIZE_CONVERSION_VALUE":
                field, cur = "maximize_conversion_value.target_roas", \
                    c.maximize_conversion_value.target_roas
            else:
                field, cur = None, None
            out[(cid, str(c.id))] = {
                "name": c.name, "status": c.status.name, "strategy_type": st,
                "portfolio": bool(c.bidding_strategy), "field": field,
                "current": float(cur) if cur else None,
            }
    return out


def _calibration() -> dict[str, dict]:
    """strategy_id → v_calibrated_recs row (κ, recs, cooldown)."""
    sql = f"""
        SELECT strategy_id, customer_name, currency, inc_class, k_cost, k_value,
               current_target, rec_google, rec_calibrated, rec_final, gate,
               last_any_change_date, days_since_change, cooldown, run_date
        FROM `{rsb.T("v_calibrated_recs")}`
    """
    return {str(r.strategy_id): dict(r.items()) for r in rsb.bq().query(sql).result()}


def _mutate(client, cid: str, ops: list, validate_only: bool):
    svc = client.get_service("CampaignService")
    req = client.get_type("MutateCampaignsRequest")
    req.customer_id = cid
    req.operations.extend(ops)
    req.validate_only = validate_only
    req.partial_failure = False
    return svc.mutate_campaigns(request=req)


def _operation(client, cid: str, campaign_id: str, field: str, value: float):
    from google.api_core import protobuf_helpers

    op = client.get_type("CampaignOperation")
    camp = op.update
    camp.resource_name = client.get_service("CampaignService").campaign_path(cid, campaign_id)
    if field == "target_roas":
        camp.target_roas.target_roas = value
    else:
        camp.maximize_conversion_value.target_roas = value
    client.copy_from(op.update_mask, protobuf_helpers.field_mask(None, camp._pb))
    return op


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--plan", help="JSON: [{customer_id, campaign_id, new_target}]")
    ap.add_argument("--from-recs", action="store_true",
                    help="build the plan from v_calibrated_recs instead of --plan")
    ap.add_argument("--write-plan", help="with --from-recs: also save the generated plan here")
    ap.add_argument("--min-spend", type=float, default=1000.0,
                    help="--from-recs: min avg 7-day spend, account currency (default 1000)")
    ap.add_argument("--min-move", type=float, default=5.0,
                    help="--from-recs: min |rec_final - live| in %% to act (default 5)")
    ap.add_argument("--classes", default="generic",
                    help="--from-recs: comma list of inc_class values (default generic)")
    ap.add_argument("--apply", action="store_true", help="mutate for real (default: validateOnly)")
    ap.add_argument("--source", default=None,
                    help="target_changes.source tag, e.g. rec-final-capped-2026-09-21")
    ap.add_argument("--notes", default="", help="appended to every logged row")
    ap.add_argument("--force-cooldown", action="store_true",
                    help="allow campaigns changed within COOLDOWN_DAYS")
    ap.add_argument("--uncapped", action="store_true",
                    help=f"allow steps over {STEP_CAP_PCT:g}%% of the current target")
    args = ap.parse_args()

    if args.apply and not args.source:
        ap.error("--apply requires --source (what recommended these changes)")
    if bool(args.plan) == bool(args.from_recs):
        ap.error("give exactly one of --plan or --from-recs")
    missing = missing_credentials()
    if missing:
        print("missing env: " + ", ".join(missing) + "\n" + __doc__.split("USAGE")[1].split("# 2.")[0])
        return 2

    if args.from_recs:
        plan = plan_from_recs(args.min_spend, args.min_move,
                              tuple(c.strip() for c in args.classes.split(",") if c.strip()))
        if args.write_plan:
            json.dump(plan, open(args.write_plan, "w"), indent=1)
            print(f"wrote {len(plan)} row(s) to {args.write_plan}")
        if not plan:
            print("--from-recs: nothing to do this run.")
            return 0
    else:
        plan = json.load(open(args.plan))
    for p in plan:
        p["customer_id"] = str(p["customer_id"]).replace("-", "")
        p["campaign_id"] = str(p["campaign_id"])
        p["new_target"] = float(p["new_target"])
        if p["customer_id"] not in ACCOUNT_BY_CID:
            ap.error(f"unknown customer_id {p['customer_id']}")

    client = ads_client()
    ga = client.get_service("GoogleAdsService")
    live = _live_campaigns(ga, plan)
    cal = _calibration()
    today = _dt.date.today().isoformat()

    rows, blocked = [], []
    for p in plan:
        key = (p["customer_id"], p["campaign_id"])
        acct = ACCOUNT_BY_CID[p["customer_id"]]
        lv = live.get(key)
        c = cal.get(p["campaign_id"], {})
        why = []
        if not lv:
            why.append("campaign not found")
        else:
            if lv["portfolio"]:
                why.append("portfolio bidding strategy — set the target on the strategy, not here")
            if not lv["field"]:
                why.append(f"strategy {lv['strategy_type']} has no target ROAS")
            if lv["status"] != "ENABLED":
                why.append(f"status {lv['status']}")
            if lv["current"] and abs(lv["current"] - p["new_target"]) < 1e-9:
                why.append("already at target")
        step_pct = (100 * (p["new_target"] / lv["current"] - 1)
                    if lv and lv["current"] else None)
        # +0.05: a step clipped to exactly the cap by --from-recs and rounded to 2 dp
        # can land at 20.0000001 % — that is the cap, not a breach of it.
        if step_pct is not None and abs(step_pct) > STEP_CAP_PCT + 0.05 and not args.uncapped:
            why.append(f"step {step_pct:+.0f}% exceeds ±{STEP_CAP_PCT:g}% cap (--uncapped)")
        if c.get("cooldown") and not args.force_cooldown:
            why.append(f"cooldown: changed {c.get('days_since_change')}d ago on "
                       f"{c.get('last_any_change_date')} (--force-cooldown)")
        pred = {}
        if lv and lv["current"]:
            used, pts = rsb.curve_points(acct["label"], p["campaign_id"], today)
            if pts:
                pred = rsb.predict_change(pts, lv["current"], p["new_target"],
                                          k_cost=float(c.get("k_cost") or 1.0),
                                          k_value=float(c.get("k_value") or 1.0))
                pred["curve_run_date"] = used
        row = {**p, "account": acct["label"], "currency": acct["currency"],
               "live": lv, "cal": c, "step_pct": step_pct, "pred": pred, "blocked": why}
        (blocked if why else rows).append(row)

    # ── report ──────────────────────────────────────────────────────────────
    hdr = (f"{'account':<12} {'campaign':<38} {'type':<5} {'live':>6} {'new':>6} {'step':>6} "
           f"{'recFinal':>8} {'cool':>5} {'Δcost/wk':>10} {'ΔGP3/wk':>9}  status")
    print(hdr)
    print("-" * len(hdr))
    for r in rows + blocked:
        lv, c, pr = r["live"] or {}, r["cal"], r["pred"]
        t = "MCV" if (lv.get("strategy_type") == "MAXIMIZE_CONVERSION_VALUE") else "tROAS"
        cool = f"{c.get('days_since_change')}d" if c.get("cooldown") else "-"
        print(f"{r['account']:<12} {(lv.get('name') or r['campaign_id'])[:38]:<38} {t:<5} "
              f"{(lv.get('current') or 0):>6.2f} {r['new_target']:>6.2f} "
              f"{(r['step_pct'] if r['step_pct'] is not None else 0):>+5.0f}% "
              f"{(c.get('rec_final') or 0):>8.2f} {cool:>5} "
              f"{(pr.get('predicted_cost_delta_7d') or 0):>10,.0f} "
              f"{(pr.get('predicted_gp3_delta_7d') or 0):>9,.0f}  "
              + ("OK" if not r["blocked"] else "BLOCKED: " + "; ".join(r["blocked"])))
    print(f"\n{len(rows)} change(s) eligible, {len(blocked)} blocked. "
          f"Cooldown = {rsb.COOLDOWN_DAYS} days, step cap = ±{STEP_CAP_PCT:g}%.")
    if not rows:
        return 1

    # ── validateOnly, always ────────────────────────────────────────────────
    by_cid: dict[str, list] = {}
    for r in rows:
        by_cid.setdefault(r["customer_id"], []).append(
            _operation(client, r["customer_id"], r["campaign_id"], r["live"]["field"],
                       r["new_target"]))
    for cid, ops in by_cid.items():
        _mutate(client, cid, ops, validate_only=True)
    print(f"validateOnly: {sum(len(o) for o in by_cid.values())} operation(s) accepted "
          f"across {len(by_cid)} account(s).")
    if not args.apply:
        print("Dry run only. Re-run with --apply --source <tag> to mutate and log.")
        return 0

    # ── live mutate + read-back ─────────────────────────────────────────────
    # One account per request. A failure on one account must not stop the others
    # and, above all, must not stop the read-back and the log for the accounts
    # that DID change: on 2026-09-21 the 4th of 6 accounts raised, the loop
    # aborted, five campaigns were live at new targets and nothing was logged.
    applied_at = _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0)
    errors: dict[str, str] = {}
    for cid, ops in by_cid.items():
        try:
            _mutate(client, cid, ops, validate_only=False)
        except Exception as e:  # noqa: BLE001 - reported below, never swallowed
            errors[cid] = _ads_error(e)
            print(f"  ERROR {ACCOUNT_BY_CID[cid]['label']}: {errors[cid]}")
    after = _live_campaigns(ga, rows)
    mismatches = [r for r in rows
                  if abs((after.get((r["customer_id"], r["campaign_id"]), {}).get("current") or 0)
                         - r["new_target"]) > 1e-6]
    for r in rows:
        got = after.get((r["customer_id"], r["campaign_id"]), {}).get("current")
        print(f"  {r['account']:<12} {r['live']['name'][:38]:<38} {r['live']['current']:g} -> "
              f"{got}  {'OK' if r not in mismatches else 'MISMATCH'}")

    # ── log — the point of this tool ────────────────────────────────────────
    log_rows = []
    for r in rows:
        if r in mismatches:
            continue
        pr = r["pred"]
        log_rows.append({
            "change_date": applied_at.date().isoformat(),
            "applied_at": applied_at.isoformat().replace("+00:00", "Z"),
            "customer_id": r["customer_id"],
            "customer_name": r["account"],
            "campaign_id": r["campaign_id"],
            "campaign_name": r["live"]["name"],
            "field": r["live"]["field"],
            "old_target": r["live"]["current"],
            "new_target": r["new_target"],
            "source": args.source,
            "predicted_cost_pct": pr.get("predicted_cost_pct"),
            "predicted_gp3_pct": pr.get("predicted_gp3_pct"),
            "predicted_cost_delta_7d": pr.get("predicted_cost_delta_7d"),
            "predicted_gp3_delta_7d": pr.get("predicted_gp3_delta_7d"),
            "currency": r["currency"],
            "notes": (f"apply_troas.py. predicted_* from the κ-deflated curve of "
                      f"{pr.get('curve_run_date')} (κC {float(r['cal'].get('k_cost') or 1):.2f}, "
                      f"κV {float(r['cal'].get('k_value') or 1):.2f}); rec_final at apply "
                      f"{r['cal'].get('rec_final')}, step {r['step_pct']:+.0f}%"
                      + (f"; cooldown overridden ({r['cal'].get('days_since_change')}d)"
                         if r["cal"].get("cooldown") else "")
                      + (f". {args.notes}" if args.notes else "")),
        })
    if log_rows:
        from google.cloud import bigquery
        cfg = bigquery.LoadJobConfig(schema=rsb.SCHEMAS["target_changes"],
                                     write_disposition="WRITE_APPEND")
        rsb.bq().load_table_from_json(log_rows, rsb.T("target_changes"), job_config=cfg).result()
        print(f"logged {len(log_rows)} row(s) to {rsb.T('target_changes')} (source {args.source})")
    if errors or mismatches:
        print(f"WARNING: {len(mismatches)} campaign(s) did not read back at the new target "
              "and were NOT logged. Fix the cause and re-run the same plan: campaigns "
              "already at target are skipped, the rest are applied and logged.")
        for cid, msg in errors.items():
            print(f"  {ACCOUNT_BY_CID[cid]['label']}: {msg}")
        return 1
    return 0


def _ads_error(e: Exception) -> str:
    """GoogleAdsException → the actual failure reasons, not the gRPC wrapper."""
    fail = getattr(e, "failure", None)
    if fail is not None:
        return "; ".join(
            f"{err.error_code} {err.message}"
            + (f" [{'.'.join(p.field_name for p in err.location.field_path_elements)}]"
               if err.location.field_path_elements else "")
            for err in fail.errors) or str(e)
    return f"{type(e).__name__}: {e}"


if __name__ == "__main__":
    sys.exit(main())
