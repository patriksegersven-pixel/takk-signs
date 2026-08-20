#!/usr/bin/env python3
"""
Voyado Engage (Delta Share) → BigQuery extraction for email/CRM reporting.

Lands the Engage share in `project-a7ade44e-e7e3-4871-a83.voyado` (EU, so it can
be joined against the Funnel export and the Norce marts, which are both EU) and
then applies the mart SQL in `voyado_marts.sql`.

Mirrors norce_sync.py: module-level config, a MissingCredentials error naming the
env vars that are still unset, staging-table MERGEs, a `sync_state` watermark
table, and a `__main__` block that runs locally.

WHY DELTA SHARE AND NOT THE API
  The Engage REST API v3 is contact/transaction/promotion shaped — it has no
  campaign-statistics endpoints at all. Delta Share is the only route to sends,
  opens, clicks and bounces, and Voyado includes the raw data products at no
  extra cost.

SHARE FACTS (audited against the live share 2026-08-17 — see voyado-probe.py)
  • One share, one schema, 21 tables, 5.7 GB, single tenant `babyshop`.
    There is NO market dimension on messages; market comes from
    contacts.countryCode joined on contactId.
  • SIX TABLES ARE EMPTY and are therefore not synced: consentlatest,
    promotionlatest, promotionrecipientlatest, abandonedcart, npsresponselatest,
    productview. Consent history is consequently unavailable — the contact-level
    acceptsEmail/statusEmail/canReceiveEmail flags are the only permission
    signal. Re-check periodically; if Voyado switches these feeds on, add them
    to SPECS and they will start landing.
  • CHANGE DATA FEED IS NOT ENABLED on the share (`/changes` → HTTP 400) even
    though the tables carry the changeDataFeed writer feature. That is a share
    setting only Voyado can flip. Until they do, incremental loads use
    jsonPredicateHints on the `*YearMonthDate` columns, which prune at FILE
    level only — a `> 2026-08-10` hint returned rows back to 2026-08-05 — so
    every window is re-filtered client-side in `_window()`.
  • `load_as_pandas(limit=N)` does NOT push the limit down; it downloads whole
    parquet files. Never use it to peek at a table.
  • messagelatest is one row per SENDOUT, not per campaign: 737,751 rows, 99.7%
    `messageSource=automation` and 76% `isTransactional=true`. The marts filter
    transactional out; without that, order confirmations swamp every campaign.
  • Only 25 of 737,751 sendouts have controlGroupPercent > 0, so the control
    group tables are synced but holdout analysis is not yet meaningful.
  • THERE IS NO BOUNCE OR DELIVERY-STATUS DATA. emailunsubscribelatest is
    unsubscribes only (see the note on that table below), and the share has no
    MessageDeliveryStatus equivalent. A "delivered" count in the marts is
    really a SENT count — nothing here distinguishes a hard bounce from an
    inbox placement.
  • Receipt amounts need conversion and CANNOT trust `exchangeRate`: 13,325 of
    151,378 non-SEK receipts carry exchangeRate = 1.0 with the local amount
    left unconverted, all of them between 2025-01 and 2025-05. `totalLocalPrice`
    (receipts) and `localPrice` (items) are ALWAYS the true local amount, so
    voyado_marts.sql converts those with a per-currency rate derived from the
    good rows rather than reading exchangeRate per receipt.
  • THE `*YearMonthDate` COLUMNS DO NOT ALL MEAN THE SAME THING. On the message
    tables (deliveryYearMonthDate, clickYearMonthDate, openYearMonthDate,
    event_year_month_date) they are true DAYS, which is what makes the windowed
    reads work. On the receipt tables (createdOnYearMonthDate,
    transactionYearMonthDate) they are MONTH-TRUNCATED — 20 distinct values
    across 511 trading days. The receipt date columns are therefore derived from
    the timestamps instead; see COLUMNS.
  • History starts 2025-03-21 for messages; receipts go back to 2025-01-07.

IDENTITY AND PII (read this before changing anything)
  The share carries far more PII than Norce did. contactslatest alone has
  `socialSecurityNumber` (personnummer), `email`, `mobilePhone`, `firstName`,
  `lastName`, `street`, `careOf` and `birthDate`; messageopenlatest and
  messageclicklatest also carry raw `email`.

  So this job uses a strict per-table ALLOWLIST (`COLUMNS` below), never a
  denylist: a column that Voyado adds later cannot leak by default, it simply
  will not be selected. Nothing outside the allowlist reaches BigQuery, a log
  line or a local file.

  The one derived-from-PII field persisted is the same one norce_sync.py uses:

      customer_hash = SHA256(LOWER(TRIM(email)))   hex, computed AT EXTRACT TIME

  Identical normalisation on both sides, which is what makes Voyado contacts
  joinable to Norce order history.

  `changeType = 'DELETE'` rows are applied as DELETEs against the target table
  (see `_apply_deletes`), so a Voyado-side erasure propagates on the next sync
  rather than lingering in the warehouse — the analogue of norce_sync.py's
  purge_forgotten().

  NOT allowlisted anywhere: socialSecurityNumber, email (hashed then dropped),
  mobilePhone, firstName, lastName, street, careOf, birthDate. `externalId` and
  `memberNumber` ARE kept as join keys — they are opaque system ids on this
  tenant, but eyeball them once after the first backfill and drop them here if
  this tenant ever puts a personal number in either.

CREDENTIALS (env var, wired from Secret Manager)
  VOYADO_SHARE_PROFILE   path to the Delta Sharing profile (`config.share`)

  The profile is a JSON file holding a NON-EXPIRING bearer token — treat it like
  an API key. On Cloud Run mount it as a file rather than an env value:
      --set-secrets=/secrets/voyado/config.share=voyado-share-profile:latest
  Without it this job prints exactly which env var is missing and exits 0
  WITHOUT creating the dataset — a scheduled run must not page anyone.

MEMORY
  deliveredmessagelatest is 3.2 GB and messageopenlatest 2.2 GB, so neither can
  be pulled in one frame. Both are read in `--chunk-days` windows (default 7,
  ~900k delivery rows) and projected to the allowlist before anything is
  retained. Give the Cloud Run job --memory=4Gi.

Run locally:
    python3 voyado_sync.py                       # incremental (last --days)
    python3 voyado_sync.py --backfill            # full history from 2025-03-21
    python3 voyado_sync.py --backfill --from 2026-01-01
    python3 voyado_sync.py --only messages,clicks
    python3 voyado_sync.py --marts-only          # re-apply voyado_marts.sql
"""
from __future__ import annotations
import argparse, datetime, hashlib, json, os, sys, time
from typing import Any, Iterator

import pandas as pd
from google.cloud import bigquery

# ── Delta Share ──────────────────────────────────────────────────────────────
SHARE_PROFILE = os.environ.get("VOYADO_SHARE_PROFILE", "/secrets/voyado/config.share")
SHARE_NAME    = os.environ.get("VOYADO_SHARE_NAME", "babyshop_prod_share_engage_data")
SHARE_SCHEMA  = os.environ.get("VOYADO_SHARE_SCHEMA", "engage_data")

# Voyado rate-limits Delta Share at 10 req/s and refreshes the raw products 2-4
# times per day, so there is nothing to gain from running this more often.
HISTORY_START = os.environ.get("VOYADO_HISTORY_START", "2025-03-21")
# Window size for the two multi-gigabyte event tables. 7 days ≈ 900k delivery
# rows ≈ 700 MB resident; raise it only if the job has the memory to match.
CHUNK_DAYS    = int(os.environ.get("VOYADO_CHUNK_DAYS", "7"))
# How far back an incremental run re-reads. Voyado backdates nothing, but the
# file-level pruning means a window is cheap to widen and the MERGE makes the
# overlap idempotent.
INCREMENTAL_DAYS = int(os.environ.get("VOYADO_INCREMENTAL_DAYS", "5"))

# ── BigQuery ─────────────────────────────────────────────────────────────────
BQ_PROJECT  = os.environ.get("VOYADO_BQ_PROJECT", "project-a7ade44e-e7e3-4871-a83")
BQ_DATASET  = os.environ.get("VOYADO_BQ_DATASET", "voyado")
# EU (multi-region), matching babyshop-funnel-data.bs_funnel_export and the
# norce dataset so the marts join without a cross-region copy.
BQ_LOCATION = os.environ.get("VOYADO_BQ_LOCATION", "EU")

# Lives at the app root, NOT under pipeline/ — .dockerignore excludes `pipeline/`,
# so anything in there is missing from the container image at runtime.
MARTS_SQL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "voyado_marts.sql")


class MissingCredentials(RuntimeError):
    """No Delta Share profile on disk — the sync cannot run."""


def missing_credentials() -> list[str]:
    return [] if os.path.isfile(SHARE_PROFILE) else ["VOYADO_SHARE_PROFILE"]


# ════════════════════════════════════════════════════════════════════════════
#  Row shaping — the only place raw PII is ever touched
# ════════════════════════════════════════════════════════════════════════════
def customer_hash(email: str | None) -> str | None:
    """SHA256 of the lower/trimmed email, hex. Returns None for a blank email.

    Byte-identical to norce_sync.customer_hash — the two must never drift, or
    the Voyado↔Norce join silently returns nothing. Lower+trim before hashing so
    the same person always lands on one hash.
    """
    if not email or not isinstance(email, str):
        return None
    e = email.strip().lower()
    return hashlib.sha256(e.encode("utf-8")).hexdigest() if e else None


S = bigquery.SchemaField

# Per-table ALLOWLIST: (source column, target column, BigQuery type).
# A source column absent from this map is never transferred. `email` appears
# only as the input to customer_hash and is dropped immediately afterwards.
COLUMNS: dict[str, list[tuple[str, str, str]]] = {
    "messages": [
        ("messageId", "messageId", "STRING"), ("messageName", "messageName", "STRING"),
        ("subject", "subject", "STRING"), ("status", "status", "STRING"),
        ("scheduledDateTime", "scheduledDateTime", "TIMESTAMP"),
        ("recipientCount", "recipientCount", "INT64"), ("channel", "channel", "STRING"),
        ("sendList", "sendList", "STRING"), ("messageSource", "messageSource", "STRING"),
        ("isTransactional", "isTransactional", "BOOL"),
        ("controlGroupPercent", "controlGroupPercent", "FLOAT64"),
        ("senderStoreId", "senderStoreId", "STRING"),
        ("workflowId", "workflowId", "STRING"),
        ("workflowActivityId", "workflowActivityId", "STRING"),
        ("isWorkflowTemplate", "isWorkflowTemplate", "BOOL"),
        ("workflowMessageId", "workflowMessageId", "STRING"),
        ("changeType", "changeType", "STRING"),
        ("import_date_time", "importDateTime", "TIMESTAMP"),
    ],
    "deliveries": [
        ("deliveryId", "deliveryId", "STRING"), ("messageId", "messageId", "STRING"),
        ("contactId", "contactId", "STRING"),
        ("deliveryDateTime", "deliveryDateTime", "TIMESTAMP"),
        ("deliveryYearMonthDate", "deliveryDate", "DATE"),
        ("changeType", "changeType", "STRING"),
        ("import_date_time", "importDateTime", "TIMESTAMP"),
    ],
    "opens": [
        ("openId", "openId", "INT64"), ("messageId", "messageId", "STRING"),
        ("deliveryId", "deliveryId", "STRING"), ("contactId", "contactId", "STRING"),
        ("openDateTime", "openDateTime", "TIMESTAMP"),
        ("openYearMonthDate", "openDate", "DATE"),
        ("userAgent", "userAgent", "STRING"), ("messageClass", "messageClass", "STRING"),
        ("changeType", "changeType", "STRING"),
        ("import_date_time", "importDateTime", "TIMESTAMP"),
    ],
    "clicks": [
        ("clickId", "clickId", "INT64"), ("messageId", "messageId", "STRING"),
        ("deliveryId", "deliveryId", "STRING"), ("contactId", "contactId", "STRING"),
        ("clickDateTime", "clickDateTime", "TIMESTAMP"),
        ("clickYearMonthDate", "clickDate", "DATE"),
        ("url", "url", "STRING"), ("userAgent", "userAgent", "STRING"),
        ("messageClass", "messageClass", "STRING"),
        ("changeType", "changeType", "STRING"),
        ("import_date_time", "importDateTime", "TIMESTAMP"),
    ],
    # The only table from Voyado's Kafka pipeline rather than the BI exporter,
    # hence the snake_case source names. Renamed to match its siblings so the
    # marts read consistently.
    #
    # THIS IS UNSUBSCRIBES ONLY — there is NO bounce data in this share. The
    # `BounceType` column is never a bounce type: it is the literal string
    # 'None' on the 93,788 rows before 2026-05-04 and SQL NULL on the 42,374
    # after (Voyado changed the event format on that date; LinkId went the same
    # way, from an all-zero GUID to NULL). Both halves are the same unsubscribe
    # event. The columns are still carried in case Voyado starts populating
    # them, but nothing may report hard/soft bounce off this table. Delivery
    # status lives in a MessageDeliveryStatus export that is NOT part of the
    # share — ask Voyado to add it if bounce reporting is needed.
    "unsubscribes": [
        ("delivery_id", "deliveryId", "STRING"), ("message_id", "messageId", "STRING"),
        ("contact_id", "contactId", "STRING"), ("EventTime", "eventTime", "TIMESTAMP"),
        ("BounceType", "bounceType", "STRING"), ("LinkId", "linkId", "STRING"),
        ("event_year_month_date", "eventDate", "DATE"),
        ("kafka_timestamp", "kafkaTimestamp", "TIMESTAMP"),
        ("import_date_time", "importDateTime", "TIMESTAMP"),
    ],
    "control_group": [
        ("messageControlGroupId", "messageControlGroupId", "INT64"),
        ("messageId", "messageId", "STRING"), ("contactId", "contactId", "STRING"),
        ("changeType", "changeType", "STRING"),
        ("import_date_time", "importDateTime", "TIMESTAMP"),
    ],
    # NOTE what is absent by PII policy: socialSecurityNumber, email,
    # mobilePhone, firstName, lastName, street, careOf, birthDate.
    #
    # Also absent because they are 100% NULL on this tenant (audited over all
    # 483,415 contacts, 2026-08-17): rfm, recency, frequency, monetary, age,
    # gender, latestNpsGrade, canReceiveEmail, canReceiveSms, canReceivePostal,
    # externalId. Voyado's own RFM model is simply not switched on here, so the
    # segmentation has to be built from purchaseAmount*/receipts instead — and
    # `acceptsEmail` + `statusEmail` are the permission signal, NOT
    # `canReceiveEmail`, which looks authoritative and is always NULL.
    "contacts": [
        ("contactId", "contactId", "STRING"), ("contactType", "contactType", "STRING"),
        ("isApproved", "isApproved", "BOOL"), ("isDeleted", "isDeleted", "BOOL"),
        ("zipCode", "zipCode", "STRING"), ("city", "city", "STRING"),
        ("countryCode", "countryCode", "STRING"), ("country", "country", "STRING"),
        ("acceptsEmail", "acceptsEmail", "BOOL"), ("acceptsSms", "acceptsSms", "BOOL"),
        ("acceptsPostal", "acceptsPostal", "BOOL"),
        ("statusEmail", "statusEmail", "STRING"), ("statusSms", "statusSms", "STRING"),
        ("statusPostal", "statusPostal", "STRING"),
        ("registrationDateTime", "registrationDateTime", "TIMESTAMP"),
        ("lastModifiedDateTime", "lastModifiedDateTime", "TIMESTAMP"),
        ("deletedDateTime", "deletedDateTime", "TIMESTAMP"),
        ("deletedReason", "deletedReason", "STRING"),
        ("purchaseAmountTotal", "purchaseAmountTotal", "FLOAT64"),
        ("numberOfArticlesTotal", "numberOfArticlesTotal", "INT64"),
        ("averageReceiptTotal", "averageReceiptTotal", "FLOAT64"),
        ("latestReceiptDateTime", "latestReceiptDateTime", "TIMESTAMP"),
        ("purchaseFrequencyTotal", "purchaseFrequencyTotal", "FLOAT64"),
        ("purchaseAmount12months", "purchaseAmount12months", "FLOAT64"),
        ("numberOfArticles12Months", "numberOfArticles12Months", "INT64"),
        ("averageReceipt12Months", "averageReceipt12Months", "FLOAT64"),
        ("purchaseFrequency12months", "purchaseFrequency12months", "FLOAT64"),
        ("recruitedStoreId", "recruitedStoreId", "STRING"),
        ("currentStoreId", "currentStoreId", "STRING"),
        ("memberNumber", "memberNumber", "STRING"), ("source", "source", "STRING"),
        ("creationTime", "creationTime", "TIMESTAMP"),
        ("bonusPoints", "bonusPoints", "FLOAT64"),
        ("secrecyMarked", "secrecyMarked", "BOOL"),
        ("isClearedAfterDeletion", "isClearedAfterDeletion", "BOOL"),
        ("changeType", "changeType", "STRING"),
        ("import_date_time", "importDateTime", "TIMESTAMP"),
    ],
    "receipts": [
        ("receiptId", "receiptId", "STRING"), ("contactId", "contactId", "STRING"),
        ("storeId", "storeId", "STRING"), ("storeExternalId", "storeExternalId", "STRING"),
        ("receiptNumber", "receiptNumber", "STRING"),
        ("totalGrossPrice", "totalGrossPrice", "FLOAT64"),
        ("totalQuantity", "totalQuantity", "INT64"),
        ("totalReturned", "totalReturned", "FLOAT64"),
        ("totalLocalPrice", "totalLocalPrice", "FLOAT64"),
        ("exchangeRate", "exchangeRate", "FLOAT64"),
        ("localCurrency", "localCurrency", "STRING"),
        ("numberOfItems", "numberOfItems", "INT64"),
        ("numberOfPurchases", "numberOfPurchases", "INT64"),
        ("numberOfReturns", "numberOfReturns", "INT64"),
        ("createdOnDateTime", "createdOnDateTime", "TIMESTAMP"),
        # NOT from createdOnYearMonthDate. Despite the shared naming, the
        # receipt tables' *YearMonthDate columns are MONTH-truncated (20
        # distinct values across 511 real trading days), while the message
        # tables' are true days. Taking the string would have parked every
        # month's revenue on the 1st in the daily series.
        ("createdOnDateTime", "createdOnDate", "DATE"),
        ("externalId", "externalId", "STRING"),
        ("changeType", "changeType", "STRING"),
        ("import_date_time", "importDateTime", "TIMESTAMP"),
    ],
    # articleSpare1-10 / articleDateSpare1-5 are deliberately not carried: 15
    # untyped free-text columns nothing reads. Add them here if a use appears.
    # `articleGroup` is not carried either — 0 of 869,317 rows populated. `sku`
    # and `articleNumber` are both 100% populated and are the product keys.
    # `type` is 'Purchase' or 'Return'; Return rows carry NEGATIVE quantity and
    # grossPaidPrice, so a plain SUM is already net of returns.
    "receipt_items": [
        ("receiptItemId", "receiptItemId", "STRING"), ("receiptId", "receiptId", "STRING"),
        ("contactId", "contactId", "STRING"), ("storeId", "storeId", "STRING"),
        ("transactionDateTime", "transactionDateTime", "TIMESTAMP"),
        # Month-truncated at source — same trap as receipts.createdOnDate above.
        ("transactionDateTime", "transactionDate", "DATE"),
        ("type", "type", "STRING"), ("sku", "sku", "STRING"),
        ("articleNumber", "articleNumber", "STRING"), ("quantity", "quantity", "FLOAT64"),
        ("grossPaidPrice", "grossPaidPrice", "FLOAT64"),
        ("grossPaidPricePerUnit", "grossPaidPricePerUnit", "FLOAT64"),
        ("localPrice", "localPrice", "FLOAT64"),
        ("localCurrency", "localCurrency", "STRING"),
        ("discounts", "discounts", "FLOAT64"),
        ("discountPercent", "discountPercent", "FLOAT64"),
        ("marginPercent", "marginPercent", "FLOAT64"),
        ("taxPercent", "taxPercent", "FLOAT64"),
        ("articleName", "articleName", "STRING"),
        ("awardsBonus", "awardsBonus", "BOOL"),
        ("changeType", "changeType", "STRING"),
        ("import_date_time", "importDateTime", "TIMESTAMP"),
    ],
}

# Per-target sync spec.
#   source   Delta Share table name
#   key      MERGE key (also the de-dup key within a batch)
#   window   date column used for chunked reads; None = the table is small
#            enough to reload whole every run
#   hash     source column to turn into customer_hash, then drop
SPECS: dict[str, dict[str, Any]] = {
    "messages":      dict(source="messagelatest",              key=["messageId"],             window=None),
    "deliveries":    dict(source="deliveredmessagelatest",     key=["deliveryId"],            window="deliveryYearMonthDate"),
    "opens":         dict(source="messageopenlatest",          key=["openId"],                window="openYearMonthDate",    hash="email"),
    "clicks":        dict(source="messageclicklatest",         key=["clickId"],               window="clickYearMonthDate",   hash="email"),
    "unsubscribes":  dict(source="emailunsubscribelatest",     key=["eventKey"],              window="event_year_month_date"),
    "control_group": dict(source="messagecontrolgrouplatest",  key=["messageControlGroupId"], window=None),
    "contacts":      dict(source="contactslatest",             key=["contactId"],             window=None, hash="email"),
    "receipts":      dict(source="receiptslatest",             key=["receiptId"],             window=None),
    "receipt_items": dict(source="receiptitemslatest",         key=["receiptItemId"],         window=None),
}

# Partition/cluster where the volume justifies it. deliveries and opens are the
# two multi-gigabyte tables; the rest are tens of megabytes.
_PARTITION = {"deliveries": "deliveryDate", "opens": "openDate", "clicks": "clickDate",
              "unsubscribes": "eventDate", "receipts": "createdOnDate",
              "receipt_items": "transactionDate"}
_CLUSTER = {"deliveries": ["messageId", "contactId"], "opens": ["messageId", "contactId"],
            "clicks": ["messageId", "contactId"], "unsubscribes": ["messageId", "contactId"],
            "contacts": ["contactId", "countryCode"], "receipts": ["contactId"],
            "receipt_items": ["receiptId", "contactId"], "messages": ["messageSource", "channel"],
            "control_group": ["messageId"]}


def schema_for(table: str) -> list[bigquery.SchemaField]:
    """Declared BigQuery schema for a target table, plus the derived columns."""
    fields = [S(tgt, typ) for _, tgt, typ in COLUMNS[table]]
    if SPECS[table].get("hash"):
        fields.append(S("customer_hash", "STRING"))
    if table == "unsubscribes":
        # emailunsubscribelatest has no primary key of its own — see _shape().
        fields.append(S("eventKey", "STRING"))
    return fields


SCHEMAS: dict[str, list[bigquery.SchemaField]] = {t: schema_for(t) for t in SPECS}
SCHEMAS["sync_state"] = [S("step", "STRING"), S("watermark", "TIMESTAMP"),
                         S("run_at", "TIMESTAMP"), S("rows", "INT64"),
                         S("share_version", "INT64")]


# ════════════════════════════════════════════════════════════════════════════
#  Delta Share reads
# ════════════════════════════════════════════════════════════════════════════
def _url(source: str) -> str:
    return f"{SHARE_PROFILE}#{SHARE_NAME}.{SHARE_SCHEMA}.{source}"


def share_version(source: str) -> int | None:
    import delta_sharing
    try:
        return int(delta_sharing.get_table_version(_url(source)))
    except Exception:
        return None


def _hint(column: str, lo: datetime.date, hi: datetime.date) -> str:
    """File-skipping hint for `lo <= column <= hi`.

    The *YearMonthDate columns are STRINGS ('2026-08-17'), not dates, so the
    literals are typed as strings — ISO-8601 sorts lexicographically, which is
    what makes a string range predicate correct here.

    This is a HINT: the server drops files whose parquet stats cannot overlap
    the range and returns the rest whole, so the caller must still filter rows.
    """
    def cmp(op: str, value: str) -> dict:
        return {"op": op, "children": [
            {"op": "column", "name": column, "valueType": "string"},
            {"op": "literal", "value": value, "valueType": "string"}]}
    return json.dumps({"op": "and", "children": [
        cmp("greaterThanOrEqual", lo.isoformat()), cmp("lessThanOrEqual", hi.isoformat())]})


def fetch(source: str, window: str | None = None,
          lo: datetime.date | None = None, hi: datetime.date | None = None,
          attempts: int = 4) -> pd.DataFrame:
    """Read one share table, optionally restricted to a date window.

    Import is local so that --marts-only and the credential check run without
    delta-sharing installed, and so an import error surfaces against the step
    that actually needs it.

    RETRIES ARE NOT OPTIONAL HERE. A window of deliveredmessagelatest pulls
    hundreds of megabytes of parquet straight from blob storage, and a single
    dropped connection surfaces as

        ArrowInvalid: External error: Reqwest Error: error decoding response body

    which killed a 3.2 GB backfill an hour in. The read is a pure GET, so
    retrying is safe; the MERGE downstream makes a re-read idempotent anyway.
    """
    import delta_sharing
    url = _url(source)
    hint = _hint(window, lo, hi) if (window and lo and hi) else None
    delay = 5.0
    for attempt in range(attempts):
        try:
            df = (delta_sharing.load_as_pandas(url, jsonPredicateHints=hint) if hint
                  else delta_sharing.load_as_pandas(url))
            break
        except Exception as exc:
            if attempt == attempts - 1:
                raise
            print(f"      retry {attempt+1}/{attempts-1} after {type(exc).__name__}: "
                  f"{str(exc)[:120]}", flush=True)
            time.sleep(delay)
            delay *= 2
    if hint and len(df) and window in df.columns:
        # The hint pruned FILES, not rows — apply the real bound.
        keep = df[window].astype("string")
        df = df[(keep >= lo.isoformat()) & (keep <= hi.isoformat())]
    return df


# ════════════════════════════════════════════════════════════════════════════
#  BigQuery
# ════════════════════════════════════════════════════════════════════════════
def _credentials():
    """ADC in production (Cloud Run SA); gcloud user token as a local fallback.

    The fallback REFRESHES, unlike a plain
    `google.oauth2.credentials.Credentials(token)`. A bare token is valid for
    about an hour and carries no refresh material, so a long run dies mid-flight
    with

        RefreshError: The credentials do not contain the necessary fields
        need to refresh the access token.

    which is exactly how the first deliveries/opens backfill failed after ~20
    minutes. Shelling back out to gcloud is the whole refresh: the user is
    already logged in, and this path never runs on Cloud Run, where
    google.auth.default() returns real service-account credentials.
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
                # google-auth compares expiry against a NAIVE utcnow(). Claim 45
                # minutes of the real ~60 so a refresh lands well before the
                # token actually dies.
                self.expiry = (datetime.datetime.utcnow()
                               + datetime.timedelta(minutes=45))

        tok = subprocess.check_output(["gcloud", "auth", "print-access-token"]).decode().strip()
        c = _GcloudToken(tok)
        c.expiry = datetime.datetime.utcnow() + datetime.timedelta(minutes=45)
        return c


_client = None
def bq() -> bigquery.Client:
    global _client
    if _client is None:
        _client = bigquery.Client(project=BQ_PROJECT, credentials=_credentials(),
                                  location=BQ_LOCATION)
    return _client


def T(name: str) -> str:
    return f"{BQ_PROJECT}.{BQ_DATASET}.{name}"


def ensure_dataset() -> None:
    """Create the dataset + every table if absent. Idempotent; safe to re-run.

    Deliberately called only AFTER the credential check, so a run with no profile
    leaves the project untouched rather than parking an empty dataset.
    """
    ds = bigquery.Dataset(f"{BQ_PROJECT}.{BQ_DATASET}")
    ds.location = BQ_LOCATION
    ds.description = ("Voyado Engage (Delta Share) landing tables for email/CRM "
                      "reporting. Hashed identity only — no raw PII.")
    bq().create_dataset(ds, exists_ok=True)
    for name, schema in SCHEMAS.items():
        t = bigquery.Table(T(name), schema=schema)
        if name in _PARTITION:
            t.time_partitioning = bigquery.TimePartitioning(field=_PARTITION[name])
        if name in _CLUSTER:
            t.clustering_fields = _CLUSTER[name]
        created = bq().create_table(t, exists_ok=True)
        # create_table(exists_ok=True) is a no-op on an EXISTING table — it does
        # NOT add columns. Append missing NULLABLE columns instead, which
        # BigQuery allows in place and which is idempotent. (Same reasoning as
        # norce_sync.ensure_dataset.)
        have = {f.name for f in created.schema}
        missing = [f for f in schema if f.name not in have]
        if missing:
            created.schema = list(created.schema) + [
                bigquery.SchemaField(f.name, f.field_type, mode="NULLABLE") for f in missing]
            bq().update_table(created, ["schema"])
            print(f"   + {name}: added column(s) {', '.join(f.name for f in missing)}")


def _shape(df: pd.DataFrame, table: str) -> pd.DataFrame:
    """Source frame → target frame: allowlist, rename, hash, coerce types.

    This is the ONLY place a raw-PII column is read, and it is dropped before
    returning. A source column missing from the share (Voyado has changed the
    export shape before) becomes an all-NULL target column rather than a crash.
    """
    spec = SPECS[table]
    out = pd.DataFrame(index=df.index)

    # Derive the hash FIRST, then let the allowlist drop the source column.
    if spec.get("hash"):
        src = spec["hash"]
        out["customer_hash"] = (df[src].map(customer_hash) if src in df.columns else None)

    for src, tgt, typ in COLUMNS[table]:
        if src not in df.columns:
            print(f"   !! {table}: source column '{src}' absent from the share — NULLed")
            out[tgt] = None
            continue
        col = df[src]
        if typ == "TIMESTAMP":
            out[tgt] = pd.to_datetime(col, errors="coerce", utc=True)
        elif typ == "DATE":
            # .dt.date leaves NaT (not None) in an object column, which pyarrow
            # cannot encode as date32 — hand BigQuery real Nones instead.
            d = pd.to_datetime(col, errors="coerce", utc=True).dt.date
            out[tgt] = d.where(pd.notna(d), None)
        elif typ == "INT64":
            out[tgt] = pd.to_numeric(col, errors="coerce").astype("Int64")
        elif typ == "FLOAT64":
            out[tgt] = pd.to_numeric(col, errors="coerce").astype("Float64")
        elif typ == "BOOL":
            # An all-NULL source column arrives as float64 NaN, and a direct
            # .astype("boolean") on that raises rather than yielding <NA>.
            out[tgt] = col.map(lambda v: None if pd.isna(v) else bool(v)).astype("boolean")
        else:
            out[tgt] = col.astype("string")

    if table == "unsubscribes":
        # This table has no id column. A bounce/unsubscribe is uniquely a
        # (delivery, moment, kind, link) — hashed so the MERGE key is one short
        # string rather than a four-column join, and so re-reading an overlapping
        # window upserts instead of duplicating.
        parts = [out[c].astype("string").fillna("") for c in
                 ("deliveryId", "eventTime", "bounceType", "linkId")]
        out["eventKey"] = ["|".join(v) for v in zip(*parts)]
        out["eventKey"] = out["eventKey"].map(
            lambda s: hashlib.sha256(s.encode("utf-8")).hexdigest()[:32])
    return out


def _stage(df: pd.DataFrame, table: str) -> str:
    """Load a frame into a fresh staging table and return its full name."""
    # Column ORDER must match the declared schema — _shape() derives
    # customer_hash first while schema_for() appends it last, so relying on the
    # client to line them up by name would silently load a hash into a
    # changeType column. Reindexing here also fails loudly on a missing column.
    df = df[[f.name for f in SCHEMAS[table]]]
    stg = T(f"_stg_{table}_{int(time.time()*1000)}")
    cfg = bigquery.LoadJobConfig(schema=SCHEMAS[table], write_disposition="WRITE_TRUNCATE")
    bq().load_table_from_dataframe(df, stg, job_config=cfg).result()
    # Staging is scratch — expire it so a crashed run cannot leave litter behind.
    t = bq().get_table(stg)
    t.expires = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=6)
    bq().update_table(t, ["expires"])
    return stg


def merge(df: pd.DataFrame, table: str) -> int:
    """Upsert a frame into `table` on its key. Empty input is a no-op.

    Rows are de-duplicated on the key first, the latest importDateTime winning.
    BigQuery's MERGE aborts the whole statement if one target row matches two
    source rows, and the share genuinely repeats keys across import batches.

    `changeType='DELETE'` rows are applied as deletes rather than upserts, so a
    Voyado-side erasure propagates instead of lingering.
    """
    if df is None or not len(df):
        return 0
    keys = SPECS[table]["key"]

    deletes = None
    if "changeType" in df.columns:
        # .fillna(False) matters: a NULL changeType leaves pd.NA in the mask and
        # boolean indexing on an NA-bearing mask raises rather than treating it
        # as False.
        mask = (df["changeType"].astype("string").str.upper() == "DELETE").fillna(False)
        if mask.any():
            deletes, df = df[mask], df[~mask]

    n = 0
    if len(df):
        if "importDateTime" in df.columns:
            df = df.sort_values("importDateTime").drop_duplicates(subset=keys, keep="last")
        else:
            df = df.drop_duplicates(subset=keys, keep="last")
        stg = _stage(df, table)
        cols = [f.name for f in SCHEMAS[table]]
        on = " AND ".join(f"t.{k} = s.{k}" for k in keys)
        upd = ", ".join(f"{c} = s.{c}" for c in cols if c not in keys)
        bq().query(
            f"MERGE `{T(table)}` t USING `{stg}` s ON {on} "
            f"WHEN MATCHED THEN UPDATE SET {upd} "
            f"WHEN NOT MATCHED THEN INSERT ({', '.join(cols)}) "
            f"VALUES ({', '.join('s.' + c for c in cols)})").result()
        bq().delete_table(stg, not_found_ok=True)
        n = len(df)

    if deletes is not None and len(deletes):
        # Reported separately by _apply_deletes, NOT netted off `n`: on the first
        # contacts run 166,618 of 483,415 rows are erasures, and subtracting them
        # made the log read "150,179 rows" for a table that had just landed
        # 316,797. Upserts and deletes are different facts.
        _apply_deletes(deletes, table, keys)
    return n


def _apply_deletes(df: pd.DataFrame, table: str, keys: list[str]) -> int:
    """Honour changeType='DELETE' from the share (GDPR erasure propagation)."""
    df = df.drop_duplicates(subset=keys)
    stg = _stage(df, table)
    on = " AND ".join(f"t.{k} = s.{k}" for k in keys)
    bq().query(f"DELETE FROM `{T(table)}` t WHERE EXISTS "
               f"(SELECT 1 FROM `{stg}` s WHERE {on})").result()
    bq().delete_table(stg, not_found_ok=True)
    print(f"   – {table}: {len(df):,} row(s) deleted (changeType=DELETE)")
    return len(df)


def watermark(step: str) -> datetime.datetime | None:
    try:
        rows = list(bq().query(
            f"SELECT MAX(watermark) w FROM `{T('sync_state')}` WHERE step = @s",
            job_config=bigquery.QueryJobConfig(query_parameters=[
                bigquery.ScalarQueryParameter("s", "STRING", step)])).result())
    except Exception:
        return None
    return rows[0]["w"] if rows else None


def set_watermark(step: str, w: datetime.datetime, n: int, version: int | None) -> None:
    bq().query(
        f"INSERT INTO `{T('sync_state')}` (step, watermark, run_at, `rows`, share_version) "
        f"VALUES (@s, @w, CURRENT_TIMESTAMP(), @n, @v)",
        job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("s", "STRING", step),
            bigquery.ScalarQueryParameter("w", "TIMESTAMP", w),
            bigquery.ScalarQueryParameter("n", "INT64", n),
            bigquery.ScalarQueryParameter("v", "INT64", version)])).result()


# ════════════════════════════════════════════════════════════════════════════
#  Sync steps
# ════════════════════════════════════════════════════════════════════════════
def _windows(lo: datetime.date, hi: datetime.date, days: int) -> Iterator[tuple[datetime.date, datetime.date]]:
    cur = lo
    while cur <= hi:
        end = min(cur + datetime.timedelta(days=days - 1), hi)
        yield cur, end
        cur = end + datetime.timedelta(days=1)


def sync_table(table: str, lo: datetime.date | None, hi: datetime.date,
               chunk_days: int = CHUNK_DAYS) -> int:
    """Sync one target table. Windowed tables chunk; the rest reload whole."""
    spec = SPECS[table]
    src, window = spec["source"], spec.get("window")
    t0, total = time.time(), 0

    if not window:
        # Tens of megabytes — a full reload is simpler than a delta and the
        # MERGE keeps it idempotent. contacts/receipts have no reliable
        # change-marker to window on anyway.
        df = fetch(src)
        total = merge(_shape(df, table), table) if len(df) else 0
        print(f"   {table:<14} {total:>9,} rows (full)  {time.time()-t0:.1f}s")
        return total

    start = lo or datetime.date.fromisoformat(HISTORY_START)
    failed: list[str] = []
    for w_lo, w_hi in _windows(start, hi, chunk_days):
        w0 = time.time()
        try:
            df = fetch(src, window, w_lo, w_hi)
            n = merge(_shape(df, table), table) if len(df) else 0
            del df
        except Exception as exc:
            # One window losing its network is not a reason to throw away the
            # other twelve. Record it, keep going, and report at the end so the
            # gap is visible and can be re-run with --from/--to rather than
            # silently leaving a hole in the middle of the series.
            failed.append(f"{w_lo}→{w_hi}")
            print(f"   {table:<14} {w_lo}→{w_hi}  FAILED after retries: "
                  f"{type(exc).__name__}: {str(exc)[:100]}")
            continue
        total += n
        print(f"   {table:<14} {w_lo}→{w_hi}  {n:>9,} rows  {time.time()-w0:.1f}s")
    print(f"   {table:<14} {total:>9,} rows total  {time.time()-t0:.1f}s"
          + (f"  ·  {len(failed)} WINDOW(S) MISSING: {', '.join(failed)}" if failed else ""))
    if failed:
        raise RuntimeError(f"{table}: {len(failed)} window(s) failed — {', '.join(failed)}. "
                           f"Re-run with --backfill --from/--chunk-days to fill the gap.")
    return total


def apply_marts() -> int:
    """(Re)create the mart views from voyado_marts.sql.

    Same convention as norce_sync.apply_marts: the file is the single source of
    truth, statements are separated by a double-at sentinel comment rather than
    `;` so a semicolon inside a string or comment can never split a statement in
    half, and ${DATASET} is substituted at run time.
    """
    with open(MARTS_SQL, encoding="utf-8") as fh:
        raw = fh.read()
    n = 0
    for chunk in raw.split("-- @@"):
        body = "\n".join(l for l in chunk.splitlines() if not l.strip().startswith("--")).strip()
        if not body.upper().startswith("CREATE"):
            continue
        bq().query(chunk.replace("${DATASET}", f"{BQ_PROJECT}.{BQ_DATASET}")).result()
        n += 1
    return n


# ════════════════════════════════════════════════════════════════════════════
def main() -> int:
    ap = argparse.ArgumentParser(description="Voyado Delta Share → BigQuery sync")
    ap.add_argument("--backfill", action="store_true", help="full history instead of the delta")
    ap.add_argument("--from", dest="from_date", help=f"backfill start date (default {HISTORY_START})")
    ap.add_argument("--days", type=int, default=INCREMENTAL_DAYS,
                    help="incremental look-back in days (default %(default)s)")
    ap.add_argument("--chunk-days", type=int, default=CHUNK_DAYS,
                    help="window size for the large event tables (default %(default)s)")
    ap.add_argument("--only", help="comma-separated target tables (default: all)")
    ap.add_argument("--marts-only", action="store_true", help="only re-apply voyado_marts.sql")
    args = ap.parse_args()

    t0 = time.time()

    # Touches BigQuery but never the share, so it runs without the profile —
    # which is the difference between being able to re-apply a mart fix from a
    # laptop and not.
    if args.marts_only:
        ensure_dataset()
        print(f"✓ Voyado marts applied · {apply_marts()} statements · {time.time()-t0:.1f}s")
        return 0

    missing = missing_credentials()
    if missing:
        # Graceful, not a crash: a scheduled run must not page anyone before the
        # secret exists. Nothing is created in BigQuery on this path.
        print(f"⚠️  Voyado sync skipped — no Delta Share profile at {SHARE_PROFILE}")
        print("    Set VOYADO_SHARE_PROFILE, or mount the secret on the job:")
        print("      --set-secrets=/secrets/voyado/config.share=voyado-share-profile:latest")
        return 0

    tables = [t.strip() for t in args.only.split(",")] if args.only else list(SPECS)
    unknown = [t for t in tables if t not in SPECS]
    if unknown:
        print(f"✗ Unknown table(s): {', '.join(unknown)}. Known: {', '.join(SPECS)}")
        return 1

    ensure_dataset()

    hi = datetime.date.today()
    if args.backfill:
        lo = datetime.date.fromisoformat(args.from_date or HISTORY_START)
        mode = f"backfill from {lo}"
    else:
        lo = hi - datetime.timedelta(days=args.days - 1)
        mode = f"incremental, last {args.days} day(s)"
    print(f"Voyado sync · {mode} · {len(tables)} table(s) · chunk {args.chunk_days}d")

    run_started = datetime.datetime.now(datetime.timezone.utc)
    counts: dict[str, int] = {}
    for table in tables:
        try:
            counts[table] = sync_table(table, lo, hi, args.chunk_days)
            set_watermark(table, run_started, counts[table], share_version(SPECS[table]["source"]))
        except Exception as e:
            # One table failing must not cost the whole run — the others still
            # land and the marts still rebuild on what is there.
            print(f"   !! {table} FAILED: {type(e).__name__}: {e}")
            counts[table] = -1

    n_marts = apply_marts()
    ok = {k: v for k, v in counts.items() if v >= 0}
    failed = [k for k, v in counts.items() if v < 0]
    print(f"✓ Voyado sync · " + " · ".join(f"{k} {v:,}" for k, v in ok.items())
          + (f" · FAILED: {', '.join(failed)}" if failed else "")
          + f" · marts {n_marts} · {time.time()-t0:.1f}s")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
