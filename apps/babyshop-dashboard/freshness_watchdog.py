#!/usr/bin/env python3
"""Data freshness watchdog: assert that every critical source actually LANDED data.

WHY THIS EXISTS
  Two silent failures were found within two days of each other, and both were
  invisible for weeks because success was measured by exit code instead of by
  data arriving:

    * norce-sync reported succeeded=1 every night from 19 Aug 2026 while
      writing nothing (its two secrets had been dropped from the job env, the
      script logged a skip and returned 0).
    * The Google Ads connector in claude-private-499703 reports success while
      5 of 8 accounts stopped returning rows on 13 Sep 2026.

  A green job says "the code ran". It does not say "the warehouse moved". This
  job asserts the second thing, which is the only one the dashboards care
  about.

WHAT IT DOES
  For every entry in CHECKS below it reads the max date actually present in the
  table (or, for snapshot tables that carry no usable date column, the table's
  last modified time) and compares it against that source's own threshold. Any
  source past its threshold is reported, and the job exits non-zero so Cloud
  Run records a FAILED execution.

ALERTING
  Every failure prints one line beginning FRESHNESS_ALERT, naming the source,
  its age and its threshold. The Cloud Monitoring policy "Data freshness stale"
  matches that marker in the logs and mails the existing email channel, reusing
  the same channel and the same conditionMatchedLog shape as the "Cloud Build
  failure" policy, which is the established pattern in this project.

  The policy matches the MARKER TEXT, not severity. Cloud Run job stderr lands
  at DEFAULT severity here, not ERROR, so a severity>=ERROR filter (the shape
  the Cloud Build policy uses) would never fire for this job. Verified against
  the actual log entries, do not "simplify" it back.

  The non-zero exit is the belt to that policy's braces: with no alert policy at
  all, a red execution still shows in the Cloud Run job list.

THRESHOLDS
  Not guessed. Each one was derived from the source's OWN observed update
  history: the largest gap between consecutive dates present in a healthy
  window (180 to 30 days back, so a current outage cannot widen its own
  threshold), plus slack for one missed run. `--calibrate` re-runs that
  measurement and prints what the thresholds would be today, so this stays
  honest as cadences change. Thresholds live in ONE place: the CHECKS table.

USAGE
  python3 freshness_watchdog.py              # the daily assertion
  python3 freshness_watchdog.py --calibrate  # re-derive thresholds from history
  python3 freshness_watchdog.py --dry-run    # print the SQL and the scan cost
  python3 freshness_watchdog.py --only gads  # substring filter on check names
"""
from __future__ import annotations

import argparse
import datetime
import os
import sys

from google.cloud import bigquery

# The job runs in the legacy project, which is also where queries are BILLED.
# Cross-project reads (claude-private-499703, babyshop-funnel-data) work
# because the runtime SA holds READER on those datasets.
BILLING_PROJECT = os.environ.get("BQ_BILLING_PROJECT", "project-a7ade44e-e7e3-4871-a83")
LOCATION = os.environ.get("BQ_LOCATION", "EU")
TZ = "Europe/Stockholm"

LEGACY = "project-a7ade44e-e7e3-4871-a83"
NEXT = "claude-private-499703"
FUNNEL = "babyshop-funnel-data"

# Lookback on the date column. Bounds the scan (and prunes partitions where the
# date column IS the partition column). A source staler than this reads as
# "no rows in window", which is an alert either way.
LOOKBACK_DAYS = 400


def C(name, table, date_col, max_stale_days, observed_gap, note, kind="max_date"):
    return dict(name=name, table=table, date_col=date_col,
                max_stale_days=max_stale_days, observed_gap=observed_gap,
                note=note, kind=kind)


def M(name, table, max_stale_days, note):
    """A snapshot table: no usable date column, so assert the WRITE instead."""
    return dict(name=name, table=table, date_col=None,
                max_stale_days=max_stale_days, observed_gap=None,
                note=note, kind="modified")


# ════════════════════════════════════════════════════════════════════════════
# THE ONE PLACE TO EDIT.
#
# max_stale_days = observed_gap (largest gap between consecutive dates in the
# healthy window) + slack for one missed run. observed_gap is recorded so a
# later reader can see what the number was derived from, and --calibrate
# re-measures it. Raise a threshold only after --calibrate shows the cadence
# genuinely changed, never to silence a real stall.
# ════════════════════════════════════════════════════════════════════════════
CHECKS = [
    # ---- Norce (legacy project), nightly norce-sync at 03:00 Stockholm ------
    # Orders arrive continuously, so a healthy morning sees today or yesterday.
    # Observed gap 1. This is the source that was 27 days stale.
    C("norce.orders", f"{LEGACY}.norce.orders", "OrderDate", 2, 1,
      "norce-sync nightly 03:00, feeds Segments / Customer Insights / Seasons"),
    # The products pass of the same job, truncate-loaded nightly. Catches a
    # half-broken sync where orders land but the product dimension does not.
    M("norce.products", f"{LEGACY}.norce.products", 2,
      "norce-sync products pass, truncate loaded nightly"),
    M("norce.sku_titles", f"{LEGACY}.norce.sku_titles", 3,
      "Channable feed titles + image_link (Bundles tab images)"),

    # ---- Funnel export (separate project), hourly funnel-bq-refresh ---------
    # Funnel itself lags 12 to 18 h, so today's partition may legitimately be
    # missing or partial for most of the day. Observed gap 1, threshold 2: this
    # asserts "yesterday exists", never "today is complete".
    C("funnel.funnel_data", f"{FUNNEL}.bs_funnel_export.funnel_data", "Date", 2, 1,
      "Funnel export, 12 to 18 h upstream lag, do NOT tighten to 1"),

    # ---- Voyado (legacy project), voyado-sync twice daily 04:30 / 14:30 -----
    C("voyado.receipts", f"{LEGACY}.voyado.receipts", "createdOnDate", 3, 2,
      "Delta Share receipts, observed gap 2"),
    C("voyado.receipt_items", f"{LEGACY}.voyado.receipt_items", "transactionDate", 3, 2,
      "Delta Share receipt lines, observed gap 2"),
    C("voyado.deliveries", f"{LEGACY}.voyado.deliveries", "deliveryDate", 2, 1,
      "email sends"),
    C("voyado.opens", f"{LEGACY}.voyado.opens", "openDate", 2, 1, "email opens"),
    C("voyado.clicks", f"{LEGACY}.voyado.clicks", "clickDate", 2, 1, "email clicks"),
    C("voyado.unsubscribes", f"{LEGACY}.voyado.unsubscribes", "eventDate", 2, 1,
      "unsubscribe events"),
    M("voyado.contacts", f"{LEGACY}.voyado.contacts", 2,
      "contact snapshot, truncate loaded on every sync"),

    # ---- Business Central (new project), nightly ---------------------------
    # postingDate skips weekends and Swedish holidays, so the healthy gaps here
    # are genuinely 2 to 5 days. Thresholds are gap + 2, which is why a stalled
    # BC sync takes about a week to page: tightening these buys false positives
    # every long weekend, not earlier detection.
    C("bc.general_ledger_entries", f"{NEXT}.babyshop_raw.bc_general_ledger_entries",
      "postingDate", 4, 2, "nightly BC sync"),
    C("bc.item_ledger_entries", f"{NEXT}.babyshop_raw.bc_item_ledger_entries",
      "postingDate", 6, 4, "nightly BC sync"),
    C("bc.sales_invoices", f"{NEXT}.babyshop_raw.bc_sales_invoices",
      "postingDate", 6, 4, "nightly BC sync"),
    C("bc.sales_credit_memos", f"{NEXT}.babyshop_raw.bc_sales_credit_memos",
      "postingDate", 7, 5, "returns, sparser by nature"),
    C("bc.return_receipts", f"{NEXT}.babyshop_raw.bc_return_receipts",
      "postingDate", 7, 5, "returns, sparser by nature"),
    # bc_sales_orders carries a single postingDate for every row (it is an open
    # order snapshot, not a dated fact stream), so a max date check on it is
    # meaningless. Assert the write instead.
    M("bc.sales_orders", f"{NEXT}.babyshop_raw.bc_sales_orders", 2,
      "open order snapshot, no usable date column"),
    M("bc.customers", f"{NEXT}.babyshop_raw.bc_customers", 3,
      "customer dimension snapshot"),

    # ---- Google Ads connector (new project), daily -------------------------
    # One row per campaign per day, observed gap 1, threshold 2 (yesterday must
    # exist). These are the tables where 5 of 8 accounts went quiet on
    # 13 Sep 2026 while the connector kept reporting success.
    C("gads.se_4851485396", f"{NEXT}.babyshop_raw.gads_se_4851485396_campaign_native",
      "segments_date", 2, 1, "Babyshop SE"),
    C("gads.se_7780114635", f"{NEXT}.babyshop_raw.gads_se_7780114635_campaign_native",
      "segments_date", 2, 1, "Lekmer SE"),
    C("gads.no_8308232278", f"{NEXT}.babyshop_raw.gads_no_8308232278_campaign_native",
      "segments_date", 2, 1, "NO"),
    C("gads.no_8623945183", f"{NEXT}.babyshop_raw.gads_no_8623945183_campaign_native",
      "segments_date", 2, 1, "NO"),
    C("gads.dk_2054294342", f"{NEXT}.babyshop_raw.gads_dk_2054294342_campaign_native",
      "segments_date", 2, 1, "DK"),
    C("gads.dk_2756397225", f"{NEXT}.babyshop_raw.gads_dk_2756397225_campaign_native",
      "segments_date", 2, 1, "DK"),
    C("gads.fi_6161399704", f"{NEXT}.babyshop_raw.gads_fi_6161399704_campaign_native",
      "segments_date", 2, 1, "FI"),
    C("gads.row_5541487401", f"{NEXT}.babyshop_raw.gads_row_5541487401_campaign_native",
      "segments_date", 2, 1, "ROW"),
]

ALERT = "FRESHNESS_ALERT"
NEVER = 9999  # stale_days stand-in for "no rows at all in the lookback window"


# ════════════════════════════════════════════════════════════════════════════
def client() -> bigquery.Client:
    return bigquery.Client(project=BILLING_PROJECT, location=LOCATION)


def _date_sql(c: dict) -> str:
    """One SELECT for one max_date check.

    CAST(col AS DATE) rather than DATE(col) so the same expression works for
    DATE, DATETIME and TIMESTAMP columns without per-table type lookups.

    FUTURE DATES ARE EXCLUDED, and that clause is load bearing. BC posts
    year-end accruals forward: bc_general_ledger_entries carries rows dated
    2027-01-01 today. An unbounded MAX() on that table reads as fresh forever,
    which would have made this watchdog blind to exactly the failure it exists
    to catch. A row dated in the future says nothing about whether the sync ran.
    """
    col, table = c["date_col"], c["table"]
    return (f"SELECT '{c['name']}' AS check_name, "
            f"MAX(CAST(`{col}` AS DATE)) AS max_date "
            f"FROM `{table}` "
            f"WHERE CAST(`{col}` AS DATE) BETWEEN "
            f"DATE_SUB(CURRENT_DATE('{TZ}'), INTERVAL {LOOKBACK_DAYS} DAY) "
            f"AND CURRENT_DATE('{TZ}')")


def _modified_sql(c: dict) -> str:
    """Last write time of a snapshot table, from dataset metadata (free)."""
    project, dataset, table = c["table"].split(".")
    return (f"SELECT '{c['name']}' AS check_name, "
            f"TIMESTAMP_MILLIS(last_modified_time) AS modified "
            f"FROM `{project}.{dataset}.__TABLES__` WHERE table_id = '{table}'")


def run_checks(bq: bigquery.Client, checks: list[dict]) -> list[dict]:
    """Run every check, isolating failures.

    One UNION ALL query per kind is the fast path (two BigQuery jobs for the
    whole watchdog). If it fails for ANY reason (a renamed column, a revoked
    cross-project grant, a dropped table), fall back to running the checks one
    at a time so a single broken source cannot blind the other twenty-five.
    That fallback is the whole point of this job: a watchdog that dies quietly
    is the failure mode it was built to catch.
    """
    results: dict[str, dict] = {}
    for kind, sql_for in (("max_date", _date_sql), ("modified", _modified_sql)):
        group = [c for c in checks if c["kind"] == kind]
        if not group:
            continue
        combined = "\nUNION ALL\n".join(sql_for(c) for c in group)
        try:
            for row in bq.query(combined).result():
                results[row["check_name"]] = dict(row)
        except Exception as e:
            print(f"   !! combined {kind} query failed, falling back to per check: {e!r}")
            for c in group:
                try:
                    rows = list(bq.query(sql_for(c)).result())
                    results[c["name"]] = dict(rows[0]) if rows else {}
                except Exception as e2:
                    results[c["name"]] = {"error": repr(e2)}

    today = datetime.date.today()
    out = []
    for c in checks:
        r = results.get(c["name"])
        rec = dict(c)
        if r is None:
            rec.update(status="ERROR", detail="no result returned", stale=NEVER, observed=None)
        elif "error" in r:
            rec.update(status="ERROR", detail=r["error"][:200], stale=NEVER, observed=None)
        elif c["kind"] == "max_date":
            d = r.get("max_date")
            if d is None:
                rec.update(status="STALE", stale=NEVER, observed=None,
                           detail=f"no rows within {LOOKBACK_DAYS} days")
            else:
                stale = (today - d).days
                rec.update(stale=stale, observed=d.isoformat(),
                           status="OK" if stale <= c["max_stale_days"] else "STALE",
                           detail=f"max {c['date_col']} = {d.isoformat()}")
        else:
            ts = r.get("modified")
            if ts is None:
                rec.update(status="ERROR", stale=NEVER, observed=None,
                           detail="table not found in dataset metadata")
            else:
                stale = (today - ts.date()).days
                rec.update(stale=stale, observed=ts.strftime("%Y-%m-%d %H:%M"),
                           status="OK" if stale <= c["max_stale_days"] else "STALE",
                           detail=f"last written {ts:%Y-%m-%d %H:%M} UTC")
        out.append(rec)
    return out


def calibrate(bq: bigquery.Client, checks: list[dict]) -> None:
    """Re-derive thresholds from each source's own history.

    Healthy window is 180 to 30 days back: recent enough to reflect the current
    cadence, old enough that an outage in progress cannot inflate its own
    threshold and hide itself.
    """
    group = [c for c in checks if c["kind"] == "max_date"]
    parts = []
    for c in group:
        col, table = c["date_col"], c["table"]
        parts.append(f"""
SELECT '{c['name']}' AS check_name,
       MAX(CASE WHEN d BETWEEN DATE_SUB(CURRENT_DATE('{TZ}'), INTERVAL 180 DAY)
                           AND DATE_SUB(CURRENT_DATE('{TZ}'), INTERVAL 30 DAY)
                THEN gap END) AS observed_gap,
       COUNT(DISTINCT d) AS distinct_dates
FROM (SELECT d, DATE_DIFF(d, LAG(d) OVER (ORDER BY d), DAY) AS gap
      FROM (SELECT DISTINCT CAST(`{col}` AS DATE) AS d FROM `{table}`
            WHERE CAST(`{col}` AS DATE) BETWEEN DATE_SUB(CURRENT_DATE('{TZ}'), INTERVAL 365 DAY)
                                            AND CURRENT_DATE('{TZ}')))""")
    rows = {r["check_name"]: dict(r) for r in bq.query(" UNION ALL ".join(parts)).result()}
    print(f"{'check':<28} {'in config':>9} {'observed':>9} {'dates':>6}  suggested max_stale_days")
    for c in group:
        r = rows.get(c["name"], {})
        gap, n = r.get("observed_gap"), r.get("distinct_dates")
        suggest = "" if gap is None else f"{gap + 1} to {gap + 2}"
        flag = "" if gap is None or gap == c["observed_gap"] else "   <-- cadence changed"
        print(f"{c['name']:<28} {c['observed_gap'] or '-':>9} {gap if gap is not None else '-':>9} "
              f"{n if n is not None else '-':>6}  {suggest}{flag}")
    print("\nSuggested = observed gap plus one or two days of slack. Take the low end for a "
          "daily source (one missed run is already a day late), the high end for a sparse "
          "one like BC postings, where a long weekend is a normal gap. Snapshot (modified) "
          "checks are not calibrated this way: their cadence is the job schedule, not the data.")


def main() -> int:
    ap = argparse.ArgumentParser(description="Assert that critical BigQuery sources are fresh")
    ap.add_argument("--calibrate", action="store_true",
                    help="re-derive thresholds from observed history instead of checking")
    ap.add_argument("--dry-run", action="store_true", help="print SQL and estimated scan, run nothing")
    ap.add_argument("--only", help="substring filter on check name (e.g. gads, voyado)")
    args = ap.parse_args()

    checks = [c for c in CHECKS if not args.only or args.only in c["name"]]
    if not checks:
        print(f"no checks match --only {args.only!r}")
        return 2

    bq = client()

    if args.dry_run:
        for kind, sql_for in (("max_date", _date_sql), ("modified", _modified_sql)):
            group = [c for c in checks if c["kind"] == kind]
            if not group:
                continue
            sql = "\nUNION ALL\n".join(sql_for(c) for c in group)
            job = bq.query(sql, job_config=bigquery.QueryJobConfig(dry_run=True,
                                                                  use_query_cache=False))
            print(f"-- {kind}: {len(group)} checks, "
                  f"{job.total_bytes_processed / 1e6:.1f} MB scanned\n{sql}\n")
        return 0

    if args.calibrate:
        calibrate(bq, checks)
        return 0

    t0 = datetime.datetime.now()
    results = run_checks(bq, checks)

    print(f"Freshness watchdog · {len(results)} checks · "
          f"{datetime.date.today()} ({TZ})\n")
    print(f"{'status':<7} {'check':<28} {'observed':<17} {'age':>4} {'limit':>6}  note")
    for r in sorted(results, key=lambda r: ({"STALE": 0, "ERROR": 1, "OK": 2}[r["status"]], r["name"])):
        age = "-" if r["stale"] == NEVER else str(r["stale"])
        mark = {"OK": "ok  ", "STALE": "STALE", "ERROR": "ERROR"}[r["status"]]
        print(f"{mark:<7} {r['name']:<28} {str(r['observed'] or '-'):<17} "
              f"{age:>4} {r['max_stale_days']:>6}  {r['note']}")

    bad = [r for r in results if r["status"] != "OK"]
    dur = (datetime.datetime.now() - t0).total_seconds()
    if not bad:
        print(f"\nAll {len(results)} sources fresh · {dur:.1f}s")
        return 0

    # One line per failing source, so the mail names the specific source rather
    # than just "a job failed". stderr keeps it out of the table above; the
    # alert policy matches the marker text, not the severity (see ALERTING).
    print(f"\n{len(bad)} of {len(results)} sources are not fresh · {dur:.1f}s")
    for r in bad:
        print(f"{ALERT} {r['name']} status={r['status']} age={r['stale']}d "
              f"limit={r['max_stale_days']}d table={r['table']} detail={r['detail']}",
              file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
