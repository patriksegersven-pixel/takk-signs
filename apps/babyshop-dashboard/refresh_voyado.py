#!/usr/bin/env python3
"""
Voyado email snapshot — BigQuery → Firestore.

Writes `funnel_cache/<workspace>__voyado-email`, the single document the Voyado
tab reads. Same client/auth wiring and the same
{data, fetched_at, expires_at, ttl_seconds, workspace} wrapper as
refresh_customer_insights.py and bq_source.py.

SOURCE
  `project-a7ade44e-e7e3-4871-a83.voyado` marts (voyado_sync.py +
  voyado_marts.sql). Nothing else. Cost/spend is deliberately absent: Voyado is
  an owned channel and there is no per-sendout cost in the share, so any "ROAS"
  here would be revenue divided by nothing.

WHAT THE NUMBERS DO AND DO NOT MEAN (all of this is surfaced as `caveats`,
because a chart of these metrics without them is misleading)
  • `sent` is delivery ROWS. The share carries no bounce or delivery-status
    data, so there is no delivered-vs-bounced split and no deliverability rate.
  • Open rates are inflated by Apple Mail Privacy Protection pre-fetching
    images. The tab must lead on click rate and label open rate directional.
  • Revenue is LAST-CLICK within 7 days on Voyado's own contactId, so it is an
    upper bound on email's contribution, not a measurement of it. Only 25 of
    737,751 sendouts use a control group, so there is no holdout to difference
    against — this is not incrementality and must never be labelled as such.
  • Market comes from the contact, not the message: ~12% of contacts have no
    countryCode and every Voyado-erased contact has none either, so the
    'Unknown' bucket is real and is shown rather than dropped.
  • Transactional mail (76% of all sendouts) is excluded everywhere.

Run locally:  python3 refresh_voyado.py
              SKIP_FIRESTORE=1 VOYADO_OUT=/tmp/v.json python3 refresh_voyado.py
"""
from __future__ import annotations
import datetime, json, os, time
from google.cloud import bigquery

BQ_PROJECT = os.environ.get("VOYADO_BQ_PROJECT", "project-a7ade44e-e7e3-4871-a83")
BQ_DATASET = os.environ.get("VOYADO_BQ_DATASET", "voyado")
BQ_LOCATION = os.environ.get("BQ_LOCATION", "EU")

WORKSPACE  = os.environ.get("FUNNEL_WORKSPACE", "-Ln87GcdqU9CMJV6zMBY")
COLLECTION = "funnel_cache"
DOC_KEY    = "voyado-email"
TTL        = 30 * 24 * 3600
FIRESTORE_PROJECT = os.environ.get("FIRESTORE_PROJECT", "project-a7ade44e-e7e3-4871-a83")

WINDOW_DAYS = int(os.environ.get("VOYADO_WINDOW_DAYS", "30"))
TREND_DAYS  = int(os.environ.get("VOYADO_TREND_DAYS", "90"))
# Campaign table depth. 2,214 manual sendouts exist in total, so 200 covers
# roughly the last half-year of real campaigns without bloating the document.
CAMPAIGN_LIMIT = int(os.environ.get("VOYADO_CAMPAIGN_LIMIT", "200"))
CAMPAIGN_DAYS  = int(os.environ.get("VOYADO_CAMPAIGN_DAYS", "180"))

# Same reasoning as refresh_customer_insights.DOC_BUDGET_BYTES: measure compact
# JSON, which over-states Firestore's own accounting, so this trips before
# Firestore rejects the write. When it trips, shorten `trend`/`campaigns`
# rather than raising the number.
DOC_BUDGET_BYTES = 900_000

CAVEATS = [
    "'sent' counts delivery rows — the share has NO bounce or delivery-status "
    "data, so there is no deliverability rate and no hard/soft bounce split",
    "open rate is inflated by Apple Mail Privacy Protection (machine opens); "
    "lead on click rate and read open rate as directional only",
    "revenue is LAST-CLICK within 7 days on Voyado's contactId — an upper bound "
    "on email's contribution, not incrementality",
    "only 25 of 737,751 sendouts use a control group, so no holdout comparison "
    "is possible; turning control groups on in Voyado is what would fix this",
    "transactional mail (76% of sendouts) is excluded from every metric",
    "market comes from the contact, not the message; ~12% of contacts have no "
    "countryCode and Voyado-erased contacts have none, so 'Unknown' is real",
    "all money is SEK, converted from local currency at Voyado's fixed rates "
    "(constant-currency for all history)",
    "receipts before 2025-06 sit in a window where Voyado's own exchangeRate "
    "was unreliable; those rows are reconverted from the local amount",
    "consent history, promotions, abandoned cart, NPS and product views are "
    "empty in the share — ask Voyado to enable those feeds",
    "12-month revenue is computed from receipts; Voyado's own "
    "purchaseAmount12months runs ~15% higher for reasons not yet established, "
    "and is carried alongside as revenue_12m_voyado_sek",
]


def I(v):
    return int(round(float(v or 0)))


def F(v, nd=4):
    return None if v is None else round(float(v), nd)


def _credentials():
    """ADC in production (Cloud Run SA); gcloud user token as a local fallback.

    The fallback refreshes by shelling back out to gcloud — a bare token lasts
    about an hour and carries no refresh material, so a long local run dies with
    a RefreshError partway through. See voyado_sync._credentials for the full
    write-up; this copy exists because every module in this app wires its own
    client rather than sharing one.
    """
    try:
        import google.auth
        creds, _ = google.auth.default()
        return creds
    except Exception:
        import subprocess, google.oauth2.credentials

        class _GcloudToken(google.oauth2.credentials.Credentials):
            def refresh(self, request):  # noqa: ARG002 - signature fixed by google-auth
                self.token = subprocess.check_output(
                    ["gcloud", "auth", "print-access-token"]).decode().strip()
                # google-auth compares expiry against a NAIVE utcnow().
                self.expiry = datetime.datetime.utcnow() + datetime.timedelta(minutes=45)

        tok = subprocess.check_output(["gcloud", "auth", "print-access-token"]).decode().strip()
        c = _GcloudToken(tok)
        c.expiry = datetime.datetime.utcnow() + datetime.timedelta(minutes=45)
        return c


_client = None
def bq():
    global _client
    if _client is None:
        # Location pinned explicitly: when a referenced dataset is missing,
        # BigQuery falls back to the client default (US) and reports a location
        # mismatch instead of a plain "not found".
        _client = bigquery.Client(project=BQ_PROJECT, credentials=_credentials(),
                                  location=BQ_LOCATION)
    return _client


def D(name: str) -> str:
    return f"`{BQ_PROJECT}.{BQ_DATASET}.{name}`"


def _rows(sql, params=None):
    job = bq().query(sql, job_config=bigquery.QueryJobConfig(query_parameters=params or []))
    return list(job.result())


# ════════════════════════════════════════════════════════════════════════════
def coverage() -> dict:
    """What the warehouse actually holds, so the tab can date-stamp itself.

    Reported per table rather than as one 'last updated': the event tables are
    windowed and the dimension tables are full reloads, so they legitimately
    reach different dates and a single figure would hide a stalled backfill.
    """
    # `rows` is a RESERVED WORD in BigQuery — aliasing a column to it is a
    # syntax error, not a quoting nuisance. Hence row_count.
    r = _rows(f"""
      SELECT 'deliveries' AS t, COUNT(*) AS row_count, MIN(deliveryDate) AS lo, MAX(deliveryDate) AS hi
        FROM {D('deliveries')}
      UNION ALL SELECT 'opens',  COUNT(*), MIN(openDate),  MAX(openDate)  FROM {D('opens')}
      UNION ALL SELECT 'clicks', COUNT(*), MIN(clickDate), MAX(clickDate) FROM {D('clicks')}
      UNION ALL SELECT 'unsubscribes', COUNT(*), MIN(eventDate), MAX(eventDate) FROM {D('unsubscribes')}
      UNION ALL SELECT 'receipts', COUNT(*), MIN(createdOnDate), MAX(createdOnDate) FROM {D('receipts')}
      ORDER BY t
    """)
    return {x["t"]: {"rows": I(x["row_count"]),
                     "from": str(x["lo"]) if x["lo"] else None,
                     "to": str(x["hi"]) if x["hi"] else None} for x in r}


def kpis(start, end) -> dict:
    r = _rows(f"""
      SELECT
        SUM(sent) AS sent, SUM(unique_clickers) AS clickers, SUM(clicks) AS clicks,
        SUM(unique_openers) AS openers, SUM(unsubscribes) AS unsubs,
        SUM(attributed_orders) AS orders, SUM(attributed_revenue_sek) AS revenue
      FROM {D('email_daily')}
      WHERE event_date BETWEEN @s AND @e
    """, [bigquery.ScalarQueryParameter("s", "DATE", start),
          bigquery.ScalarQueryParameter("e", "DATE", end)])
    k = dict(r[0]) if r else {}
    sent = I(k.get("sent"))
    base = _rows(f"SELECT SUM(subscribed) AS subs, SUM(contacts) AS contacts FROM {D('subscriber_base')}")
    b = dict(base[0]) if base else {}
    return {
        "sent": sent,
        "unique_clickers": I(k.get("clickers")),
        "clicks": I(k.get("clicks")),
        "unique_openers": I(k.get("openers")),
        "unsubscribes": I(k.get("unsubs")),
        "click_rate": F(k["clickers"] / sent) if sent else None,
        "open_rate": F(k["openers"] / sent) if sent else None,
        "unsubscribe_rate": F(k["unsubs"] / sent) if sent else None,
        "attributed_orders": I(k.get("orders")),
        "attributed_revenue_sek": I(k.get("revenue")),
        "revenue_per_send_sek": F(float(k["revenue"] or 0) / sent, 2) if sent else None,
        "subscribers": I(b.get("subs")),
        "contacts": I(b.get("contacts")),
    }


def trend(start, end) -> list[dict]:
    """Daily series, split by manual vs automation — the two behave nothing
    alike (one spikes on send days, the other is a flat drip), so a combined
    line hides both."""
    r = _rows(f"""
      SELECT event_date, message_type,
             SUM(sent) AS sent, SUM(unique_clickers) AS clickers,
             SUM(unique_openers) AS openers, SUM(unsubscribes) AS unsubs,
             SUM(attributed_orders) AS orders, SUM(attributed_revenue_sek) AS revenue
      FROM {D('email_daily')}
      WHERE event_date BETWEEN @s AND @e
      GROUP BY event_date, message_type
      ORDER BY event_date, message_type
    """, [bigquery.ScalarQueryParameter("s", "DATE", start),
          bigquery.ScalarQueryParameter("e", "DATE", end)])
    return [{"date": str(x["event_date"]), "type": x["message_type"],
             "sent": I(x["sent"]), "clickers": I(x["clickers"]),
             "openers": I(x["openers"]), "unsubscribes": I(x["unsubs"]),
             "orders": I(x["orders"]), "revenue_sek": I(x["revenue"])} for x in r]


def markets(start, end) -> list[dict]:
    """List health and recent performance side by side, per market."""
    r = _rows(f"""
      WITH perf AS (
        SELECT market, SUM(sent) AS sent, SUM(unique_clickers) AS clickers,
               SUM(unsubscribes) AS unsubs, SUM(attributed_orders) AS orders,
               SUM(attributed_revenue_sek) AS revenue
        FROM {D('email_daily')}
        WHERE event_date BETWEEN @s AND @e
        GROUP BY market
      )
      SELECT b.market, b.contacts, b.subscribed, b.unsubscribed, b.undeliverable,
             b.subscribed_rate, b.revenue_12m_computed, b.revenue_12m_voyado,
             p.sent, p.clickers, p.unsubs, p.orders, p.revenue
      FROM {D('subscriber_base')} b
      FULL JOIN perf p USING (market)
      ORDER BY COALESCE(p.sent, 0) DESC, COALESCE(b.contacts, 0) DESC
    """, [bigquery.ScalarQueryParameter("s", "DATE", start),
          bigquery.ScalarQueryParameter("e", "DATE", end)])
    out = []
    for x in r:
        sent = I(x["sent"])
        out.append({
            "market": x["market"], "contacts": I(x["contacts"]),
            "subscribed": I(x["subscribed"]), "unsubscribed": I(x["unsubscribed"]),
            "undeliverable": I(x["undeliverable"]),
            "subscribed_rate": F(x["subscribed_rate"]),
            # Both exposed on purpose — see subscriber_base in voyado_marts.sql
            # for the unexplained ~15% gap between them.
            "revenue_12m_sek": I(x["revenue_12m_computed"]),
            "revenue_12m_voyado_sek": I(x["revenue_12m_voyado"]),
            "sent": sent, "clickers": I(x["clickers"]),
            "click_rate": F(x["clickers"] / sent) if sent else None,
            "unsubscribes": I(x["unsubs"]),
            "orders": I(x["orders"]), "revenue_sek": I(x["revenue"]),
        })
    return out


def campaign_count(since) -> int:
    """How many manual campaigns the window actually holds.

    Carried so the tab can say "200 of 431" rather than showing a capped list
    that reads as the complete one. A truncation nobody is told about is
    indistinguishable from full coverage.
    """
    r = _rows(f"""
      SELECT COUNT(*) AS n FROM {D('campaign_performance')}
      WHERE message_type = 'Manual campaign' AND scheduledDate >= @s AND sent > 0
    """, [bigquery.ScalarQueryParameter("s", "DATE", since)])
    return I(r[0]["n"]) if r else 0


def campaigns(since) -> list[dict]:
    """Manual campaigns only — an automation 'campaign' is one triggered send,
    so listing those individually would be 735k rows of noise. Automations are
    rolled up by workflow in `automations()` instead."""
    r = _rows(f"""
      SELECT messageId, messageName, subject, scheduledDate, sent, recipients,
             unique_openers, unique_clickers, unsubscribes, open_rate, click_rate,
             click_to_open_rate, unsubscribe_rate, attributed_orders,
             attributed_revenue_sek, attributed_aov_sek, revenue_per_send_sek,
             control_group_size
      FROM {D('campaign_performance')}
      WHERE message_type = 'Manual campaign' AND scheduledDate >= @s AND sent > 0
      ORDER BY scheduledDate DESC
      LIMIT @n
    """, [bigquery.ScalarQueryParameter("s", "DATE", since),
          bigquery.ScalarQueryParameter("n", "INT64", CAMPAIGN_LIMIT)])
    return [{"messageId": x["messageId"], "name": x["messageName"], "subject": x["subject"],
             "date": str(x["scheduledDate"]) if x["scheduledDate"] else None,
             "sent": I(x["sent"]), "recipients": I(x["recipients"]),
             "openers": I(x["unique_openers"]), "clickers": I(x["unique_clickers"]),
             "unsubscribes": I(x["unsubscribes"]),
             "open_rate": F(x["open_rate"]), "click_rate": F(x["click_rate"]),
             "click_to_open_rate": F(x["click_to_open_rate"]),
             "unsubscribe_rate": F(x["unsubscribe_rate"]),
             "orders": I(x["attributed_orders"]),
             "revenue_sek": I(x["attributed_revenue_sek"]),
             "aov_sek": F(x["attributed_aov_sek"], 2),
             "revenue_per_send_sek": F(x["revenue_per_send_sek"], 3),
             "control_group_size": I(x["control_group_size"])} for x in r]


def automations(since) -> list[dict]:
    """Automations rolled up by workflow — the flow is the unit people manage."""
    r = _rows(f"""
      SELECT COALESCE(workflowId, 'unknown') AS workflowId,
             ANY_VALUE(messageName) AS name,
             COUNT(*) AS sendouts, SUM(sent) AS sent,
             SUM(unique_openers) AS openers, SUM(unique_clickers) AS clickers,
             SUM(unsubscribes) AS unsubs, SUM(attributed_orders) AS orders,
             SUM(attributed_revenue_sek) AS revenue
      FROM {D('campaign_performance')}
      WHERE message_type = 'Automation' AND scheduledDate >= @s
      GROUP BY workflowId
      -- `sent` here is the SELECT alias (already SUM(sent)), NOT the source
      -- column. Writing HAVING SUM(sent) resolves the inner name to that alias
      -- too and BigQuery rejects it as "Aggregations of aggregations".
      HAVING sent > 0
      ORDER BY sent DESC
      LIMIT 100
    """, [bigquery.ScalarQueryParameter("s", "DATE", since)])
    out = []
    for x in r:
        sent = I(x["sent"])
        out.append({"workflowId": x["workflowId"], "name": x["name"],
                    "sendouts": I(x["sendouts"]), "sent": sent,
                    "openers": I(x["openers"]), "clickers": I(x["clickers"]),
                    "click_rate": F(x["clickers"] / sent) if sent else None,
                    "unsubscribes": I(x["unsubs"]), "orders": I(x["orders"]),
                    "revenue_sek": I(x["revenue"]),
                    "revenue_per_send_sek": F(float(x["revenue"] or 0) / sent, 3) if sent else None})
    return out


def revenue_share() -> list[dict]:
    """Email-touched share of ALL receipts revenue, by month and market.

    This is the section that answers "what is email worth" honestly: the
    denominator is every receipt, not just the attributed ones.
    """
    r = _rows(f"""
      SELECT month, market, orders, revenue_sek, email_attributed_orders,
             email_attributed_revenue_sek, email_revenue_share, contains_broken_fx_rows
      FROM {D('email_revenue_share')}
      ORDER BY month DESC, revenue_sek DESC
    """)
    return [{"month": str(x["month"]), "market": x["market"],
             "orders": I(x["orders"]), "revenue_sek": I(x["revenue_sek"]),
             "email_orders": I(x["email_attributed_orders"]),
             "email_revenue_sek": I(x["email_attributed_revenue_sek"]),
             "email_share": F(x["email_revenue_share"]),
             "fx_caveat": bool(x["contains_broken_fx_rows"])} for x in r]


def build_payload() -> dict:
    end = datetime.date.today()
    win_start = end - datetime.timedelta(days=WINDOW_DAYS - 1)
    trend_start = end - datetime.timedelta(days=TREND_DAYS - 1)
    camp_start = end - datetime.timedelta(days=CAMPAIGN_DAYS - 1)
    n_campaigns = campaign_count(camp_start)
    return {
        # The page and /api/voyado-email's skeleton both key off this: `null`
        # means "no snapshot exists yet, render the pending state". Omitting it
        # entirely made the tab treat a live payload as a skeleton.
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "sources": {
            "voyado": {"dataset": f"{BQ_PROJECT}.{BQ_DATASET}", "coverage": coverage()},
            "window": {"from": str(win_start), "to": str(end), "days": WINDOW_DAYS},
            "attribution": {"model": "last click", "window_days": 7,
                            "key": "voyado contactId"},
            "campaign_window": {"from": str(camp_start), "to": str(end),
                                "shown": min(CAMPAIGN_LIMIT, n_campaigns),
                                "total": n_campaigns},
        },
        "kpis": kpis(win_start, end),
        "trend": trend(trend_start, end),
        "markets": markets(win_start, end),
        "campaigns": campaigns(camp_start),
        "automations": automations(camp_start),
        "revenue_share": revenue_share(),
        "caveats": CAVEATS,
    }


def _check_size(payload: dict) -> int:
    """Refuse to write a document that is about to hit Firestore's 1 MiB cap."""
    n = len(json.dumps(payload, ensure_ascii=False))
    if n > DOC_BUDGET_BYTES:
        big = sorted(((len(json.dumps(v, ensure_ascii=False)), k)
                      for k, v in payload.items()), reverse=True)[:3]
        raise RuntimeError(
            f"Voyado payload is {n:,} B, over the {DOC_BUDGET_BYTES:,} B budget "
            f"(Firestore's hard limit is 1,048,576). Biggest sections: "
            + ", ".join(f"{k} {s:,} B" for s, k in big)
            + ". Shorten the trend/campaign windows — do not just raise the budget.")
    return n


def write_firestore(payload: dict) -> str:
    from google.cloud import firestore
    _check_size(payload)
    db = firestore.Client(project=FIRESTORE_PROJECT, credentials=_credentials())
    doc_id = f"{WORKSPACE}__{DOC_KEY}"
    db.collection(COLLECTION).document(doc_id).set({
        "data": payload, "fetched_at": firestore.SERVER_TIMESTAMP,
        "expires_at": time.time() + TTL, "ttl_seconds": TTL, "workspace": WORKSPACE})
    return f"{COLLECTION}/{doc_id}"


def main():
    t0 = time.time()
    p = build_payload()
    out = os.environ.get("VOYADO_OUT")
    if out:
        with open(out, "w", encoding="utf-8") as fh:
            json.dump(p, fh, ensure_ascii=False)
        print(f"   wrote {out} ({os.path.getsize(out):,} bytes)")
    where = "(skipped)" if os.environ.get("SKIP_FIRESTORE") else write_firestore(p)
    k = p["kpis"]
    print(f"✓ Voyado refresh · {where} · {p['sources']['window']['from']}→"
          f"{p['sources']['window']['to']} · sent {k['sent']:,} · "
          f"click rate {k['click_rate']} · unsub rate {k['unsubscribe_rate']} · "
          f"attributed {k['attributed_revenue_sek']:,} SEK over {k['attributed_orders']:,} orders · "
          f"{len(p['trend'])} trend rows · {len(p['markets'])} markets · "
          f"{len(p['campaigns'])} campaigns · {len(p['automations'])} automations · "
          f"{time.time()-t0:.1f}s")


if __name__ == "__main__":
    main()
