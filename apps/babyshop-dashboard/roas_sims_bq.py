#!/usr/bin/env python3
"""
ROAS Simulations — BigQuery export (the training log for our own bid-response model).

WHY THIS EXISTS
  refresh_roas_sims.py writes daily snapshots to Firestore with RETENTION_DAYS=90
  pruning — fine for serving the dashboard, fatal for model training: the history a
  calibration layer learns from was being deleted on a rolling window. This module
  flattens each snapshot's three row grids into partitioned BigQuery tables that are
  kept forever, and owns the `target_changes` prediction log that turns every applied
  tROAS change into a scoreable experiment.

  Verdict that motivated it (measured 2026-08-25, pre 12–17 Aug vs post 19–24 Aug,
  netted against unchanged campaigns): Google's simulated COST response was
  directionally right in 11/16 changed generic campaigns, the GP3/value response in
  only 7/16 — the curves systematically overstate marginal conversion value. The fix
  is a per-campaign optimism factor κ = realized value ÷ simulated value, learned from
  exactly the tables written here (see v_kappa below).

TABLES (dataset `roas_sims`, EU, day-partitioned on run_date)
  sim_points          one row per simulated target-ROAS point. `is_anchor` marks the
                      extra point Google returns AT the current target: it carries the
                      campaign's REAL spend, not a simulated counterfactual, and sits
                      visibly off the curve — every curve fit must exclude it.
  impression_shares   last-7-day search/top/abs-top impression share per campaign.
                      NULL means "not reported", never 0 (refresh_roas_sims.py docstring).
  actuals             what each simulated entity actually did over its own simulation
                      window, click-time and conversion-time.
  target_changes      the prediction log: every applied target change with the model's
                      predicted Δcost/ΔGP3 at apply time. Outcomes are NOT stored —
                      they are derived by joining actuals pre/post windows, so a late
                      conversion can never make a logged prediction stale.

IDEMPOTENCY
  Grids load into the run_date partition decorator (table$YYYYMMDD) with
  WRITE_TRUNCATE: a re-run of the same day replaces that day atomically and can never
  duplicate or touch any other day. `target_changes` is append-only and written by
  operators/tools, never by the refresh.

FAILURE ISOLATION
  The dashboard must keep serving even if BigQuery is down: refresh() calls
  export_snapshot() inside try/except and reports the error in its return value
  instead of raising. A missed day is re-exportable via --backfill.

Run locally:
  python3 roas_sims_bq.py --ensure           create dataset/tables/views only
  python3 roas_sims_bq.py --backfill         export every snapshot still in Firestore
  python3 roas_sims_bq.py --date 2026-08-25  export one snapshot from Firestore
"""
from __future__ import annotations

import datetime as _dt
import os
from typing import Any

from google.cloud import bigquery

BQ_PROJECT = os.environ.get("ROAS_SIMS_BQ_PROJECT",
                            os.environ.get("FIRESTORE_PROJECT")
                            or os.environ.get("GCP_PROJECT")
                            or "project-a7ade44e-e7e3-4871-a83")
BQ_DATASET = os.environ.get("ROAS_SIMS_BQ_DATASET", "roas_sims")
# EU (multi-region), matching `norce` and babyshop-funnel-data.bs_funnel_export so
# training queries can join covariates without a cross-region copy.
BQ_LOCATION = os.environ.get("ROAS_SIMS_BQ_LOCATION", "EU")


def _credentials():
    """ADC on Cloud Run; a self-refreshing gcloud token locally (norce_sync idiom)."""
    try:
        import google.auth
        creds, _ = google.auth.default()
        return creds
    except Exception:
        import subprocess

        import google.oauth2.credentials

        class _GcloudToken(google.oauth2.credentials.Credentials):
            def refresh(self, request):  # noqa: ARG002 - signature fixed by google-auth
                self.token = subprocess.check_output(
                    ["gcloud", "auth", "print-access-token"]).decode().strip()
                self.expiry = _dt.datetime.utcnow() + _dt.timedelta(minutes=45)

        tok = subprocess.check_output(["gcloud", "auth", "print-access-token"]).decode().strip()
        c = _GcloudToken(tok)
        c.expiry = _dt.datetime.utcnow() + _dt.timedelta(minutes=45)
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


S = bigquery.SchemaField
# All metrics are FLOAT64 even where the API returns integers (clicks, impressions):
# the grids round-trip through JSON where 12 and 12.0 are indistinguishable, and a
# FLOAT64 column accepts both while an INT64 load would fail on the latter.
SCHEMAS: dict[str, list[bigquery.SchemaField]] = {
    "sim_points": [
        S("run_date", "DATE", mode="REQUIRED"),
        S("customer_name", "STRING"),
        S("strategy_name", "STRING"),
        S("strategy_id", "STRING"),
        S("current_target", "FLOAT64",
          description="Live target at collection time; 0 = not set (MCV without tROAS)"),
        S("start_date", "DATE", description="Simulation window start"),
        S("end_date", "DATE", description="Simulation window end"),
        S("target_roas", "FLOAT64", description="Simulated target — the curve's x axis"),
        S("conversions", "FLOAT64"),
        S("conversions_value", "FLOAT64"),
        S("clicks", "FLOAT64"),
        S("cost", "FLOAT64", description="Currency units (converted from micros)"),
        S("impressions", "FLOAT64"),
        S("top_slot_impressions", "FLOAT64"),
        S("strategy_type", "STRING",
          description="TARGET_ROAS | MAXIMIZE_CONVERSION_VALUE"),
        S("currency", "STRING"),
        S("is_anchor", "BOOL", mode="REQUIRED",
          description="Point AT the current target: real spend, not simulated — exclude from curve fits"),
    ],
    "impression_shares": [
        S("run_date", "DATE", mode="REQUIRED"),
        S("customer_name", "STRING"),
        S("campaign_id", "STRING"),
        S("campaign_name", "STRING"),
        S("search_is", "FLOAT64", description="NULL = not reported by the API, never 0"),
        S("top_is", "FLOAT64"),
        S("abs_top_is", "FLOAT64"),
        S("currency", "STRING"),
    ],
    "actuals": [
        S("run_date", "DATE", mode="REQUIRED"),
        S("customer_name", "STRING"),
        S("strategy_id", "STRING"),
        S("strategy_name", "STRING"),
        S("level", "STRING", description="campaign | portfolio"),
        S("start_date", "DATE", description="The entity's own simulation window"),
        S("end_date", "DATE"),
        S("cost", "FLOAT64", description="Currency units (converted from micros)"),
        S("conversions", "FLOAT64", description="Click-time attribution"),
        S("conversions_value", "FLOAT64", description="Click-time attribution"),
        S("conv_time_conversions", "FLOAT64", description="Conversion-time attribution"),
        S("conv_time_conversions_value", "FLOAT64", description="Conversion-time attribution"),
        S("currency", "STRING"),
    ],
    "target_changes": [
        S("change_date", "DATE", mode="REQUIRED"),
        S("applied_at", "TIMESTAMP"),
        S("customer_id", "STRING", description="Digits only, no hyphens"),
        S("customer_name", "STRING"),
        S("campaign_id", "STRING"),
        S("campaign_name", "STRING"),
        S("field", "STRING",
          description="target_roas | maximize_conversion_value.target_roas"),
        S("old_target", "FLOAT64"),
        S("new_target", "FLOAT64"),
        S("source", "STRING",
          description="What recommended the change (model/run id), or 'manual'"),
        S("predicted_cost_pct", "FLOAT64",
          description="Predicted weekly cost change, % vs pre-change"),
        S("predicted_gp3_pct", "FLOAT64",
          description="Predicted weekly GP3 change, % vs pre-change"),
        S("predicted_cost_delta_7d", "FLOAT64", description="Currency units per week"),
        S("predicted_gp3_delta_7d", "FLOAT64", description="Currency units per week"),
        S("currency", "STRING"),
        S("notes", "STRING"),
    ],
    # Hand-loaded marginal measurements — same shape v_marginal_scoring derives
    # automatically, for scorings done outside the actuals pipeline (e.g. the
    # 2026-08-18 apply was measured directly off the Ads API before clean post
    # windows existed in `actuals`). Auto rows outrank these in v_lambda.
    "marginal_observations": [
        S("change_date", "DATE", mode="REQUIRED"),
        S("customer_name", "STRING"),
        S("campaign_id", "STRING"),
        S("campaign_name", "STRING"),
        S("currency", "STRING"),
        S("old_target", "FLOAT64"),
        S("new_target", "FLOAT64"),
        S("pre_start", "DATE"),
        S("pre_end", "DATE"),
        S("post_start", "DATE"),
        S("post_end", "DATE"),
        S("pre_cost", "FLOAT64"),
        S("delta_cost", "FLOAT64"),
        S("delta_gp2", "FLOAT64", description="Conversion-time GP2 change across the step"),
        S("realized_marginal", "FLOAT64", description="delta_gp2 / delta_cost"),
        S("sim_marginal", "FLOAT64",
          description="Sim-implied marginal over the same target segment, anchor excluded"),
        S("lambda", "FLOAT64", description="realized_marginal / sim_marginal"),
        S("source", "STRING"),
        S("notes", "STRING"),
    ],
}
_PARTITION = {"sim_points": "run_date", "impression_shares": "run_date",
              "actuals": "run_date", "target_changes": "change_date",
              "marginal_observations": "change_date"}
_CLUSTER = {"sim_points": ["customer_name", "strategy_name"],
            "actuals": ["customer_name", "strategy_name"]}

# κ — the daily sim-optimism measure that motivates this whole export. For each
# run and strategy, interpolate the simulated cost and value at the CURRENT target
# from that run's own curve (anchor excluded), and divide what the entity actually
# did over the same window by it. kappa_value < 1 ⇒ Google over-promised value.
# Consumers shrink per-campaign κ toward a class/account prior before using it.
_VIEWS = {
    "v_kappa": """
        WITH curve AS (
          SELECT run_date, customer_name, strategy_name, strategy_id, currency,
                 current_target, target_roas, cost, conversions_value,
                 LEAD(target_roas) OVER w AS t2,
                 LEAD(cost) OVER w AS cost2,
                 LEAD(conversions_value) OVER w AS value2
          FROM `{sim_points}`
          WHERE NOT is_anchor AND current_target > 0
          WINDOW w AS (PARTITION BY run_date, customer_name, strategy_id
                       ORDER BY target_roas)
        ),
        bracket AS (
          -- The segment containing the current target; clamp to the curve's ends
          -- when the live target sits outside the simulated range.
          SELECT *,
                 CASE
                   WHEN t2 IS NULL THEN 0.0
                   WHEN current_target <= target_roas THEN 0.0
                   WHEN current_target >= t2 THEN 1.0
                   ELSE (current_target - target_roas) / (t2 - target_roas)
                 END AS w
          FROM curve
          QUALIFY ROW_NUMBER() OVER (
            PARTITION BY run_date, customer_name, strategy_id
            ORDER BY CASE
              WHEN current_target BETWEEN target_roas AND IFNULL(t2, target_roas) THEN 0
              ELSE 1 END,
              ABS(current_target - target_roas)) = 1
        ),
        sim_at_current AS (
          SELECT run_date, customer_name, strategy_name, strategy_id, currency,
                 current_target,
                 cost + w * (IFNULL(cost2, cost) - cost)                    AS sim_cost,
                 conversions_value
                   + w * (IFNULL(value2, conversions_value) - conversions_value)
                                                                            AS sim_value
          FROM bracket
        )
        SELECT s.run_date, s.customer_name, s.strategy_name, s.strategy_id,
               s.currency, s.current_target, s.sim_cost, s.sim_value,
               a.start_date, a.end_date,
               a.cost                        AS actual_cost,
               a.conv_time_conversions_value AS actual_value,
               SAFE_DIVIDE(a.cost, s.sim_cost)                         AS kappa_cost,
               SAFE_DIVIDE(a.conv_time_conversions_value, s.sim_value) AS kappa_value
        FROM sim_at_current s
        JOIN `{actuals}` a
          USING (run_date, customer_name, strategy_id)
        WHERE a.cost > 0
    """,
    # Calibration v2 — κ with empirical-Bayes shrinkage. Per-campaign κ is a
    # spend-weighted mean over the last 28 days; overlapping 7-day windows mean
    # ~7 daily readings ≈ 1 independent observation, so effective weight is
    # days/7. Each campaign is shrunk toward its incrementality class's MEDIAN
    # (measured 2026-08-26: the generic-class MEAN was 1.45 against a median of
    # 1.01 — tiny ROW campaigns with garbage sims produce κ up to 13 and a mean
    # prior would inflate every campaign's κ above 1, flipping the correction's
    # sign) with prior strength τ=2 independent-week-equivalents. Raw κ is also
    # clamped to [0.25, 4]: outside that band the sim is noise, not a level to
    # learn from.
    # Class rules mirror incrementalityClass() in babyshop-roas-simulations.html
    # EXACTLY: 'brand' anywhere → brand; 'pb-generic' → generic; '-pb-' or
    # trailing '-pb' → private-label; else generic.
    # κ here is a LEVEL correction at the operating point. The curves' remaining
    # failure mode is SLOPE optimism (marginal value of moving the target),
    # which only v_change_scoring can measure — apply step caps on top.
    "v_kappa_calibrated": """
        WITH k AS (
          SELECT customer_name, strategy_name, strategy_id, currency,
                 CASE
                   WHEN LOWER(strategy_name) LIKE '%brand%' THEN 'brand'
                   WHEN LOWER(strategy_name) LIKE '%pb-generic%' THEN 'generic'
                   WHEN LOWER(strategy_name) LIKE '%-pb-%'
                     OR LOWER(strategy_name) LIKE '%-pb' THEN 'private-label'
                   ELSE 'generic'
                 END AS inc_class,
                 kappa_cost, kappa_value, actual_cost
          FROM `{v_kappa}`
          WHERE run_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 28 DAY)
            AND kappa_cost IS NOT NULL AND kappa_value IS NOT NULL
        ),
        per_campaign AS (
          SELECT customer_name, strategy_name, strategy_id, currency, inc_class,
                 COUNT(*) AS days,
                 COUNT(*) / 7.0 AS n_eff,
                 LEAST(4, GREATEST(0.25,
                   SUM(actual_cost * kappa_cost)  / SUM(actual_cost))) AS k_cost_raw,
                 LEAST(4, GREATEST(0.25,
                   SUM(actual_cost * kappa_value) / SUM(actual_cost))) AS k_value_raw,
                 AVG(actual_cost) AS avg_7d_cost
          FROM k
          GROUP BY 1, 2, 3, 4, 5
        ),
        class_prior AS (
          SELECT inc_class,
                 APPROX_QUANTILES(k_cost_raw,  100)[OFFSET(50)] AS k_cost_class,
                 APPROX_QUANTILES(k_value_raw, 100)[OFFSET(50)] AS k_value_class,
                 COUNT(*) AS class_campaigns
          FROM per_campaign
          GROUP BY 1
        )
        SELECT p.customer_name, p.strategy_name, p.strategy_id, p.currency,
               p.inc_class, p.days, p.avg_7d_cost,
               p.k_cost_raw, p.k_value_raw,
               c.k_cost_class, c.k_value_class, c.class_campaigns,
               (p.n_eff * p.k_cost_raw  + 2 * c.k_cost_class)  / (p.n_eff + 2)
                 AS k_cost,
               (p.n_eff * p.k_value_raw + 2 * c.k_value_class) / (p.n_eff + 2)
                 AS k_value
        FROM per_campaign p
        JOIN class_prior c USING (inc_class)
    """,
    # Scores every logged target change once clean windows exist on both sides:
    # pre = last collected 7-day window ENDING before the change, post = latest
    # window STARTING after it (never a window straddling the change day). GP3 is
    # conversion-time value − cost. Percentages are comparable to the predicted_*
    # columns; a row appears here ~8 days after its change and then keeps updating
    # as later windows land, so re-read before judging — bidding re-learns for
    # 1–2 weeks after a big step.
    "v_change_scoring": """
        WITH pre AS (
          SELECT c.change_date, c.campaign_id, a.cost,
                 a.conv_time_conversions_value - a.cost AS gp3,
                 a.start_date, a.end_date
          FROM `{target_changes}` c
          JOIN `{actuals}` a
            ON a.strategy_id = c.campaign_id AND a.customer_name = c.customer_name
          WHERE a.end_date < c.change_date AND a.cost > 0
          QUALIFY ROW_NUMBER() OVER (PARTITION BY c.change_date, c.campaign_id
                                     ORDER BY a.end_date DESC, a.run_date DESC) = 1
        ),
        post AS (
          SELECT c.change_date, c.campaign_id, a.cost,
                 a.conv_time_conversions_value - a.cost AS gp3,
                 a.start_date, a.end_date
          FROM `{target_changes}` c
          JOIN `{actuals}` a
            ON a.strategy_id = c.campaign_id AND a.customer_name = c.customer_name
          WHERE a.start_date > c.change_date AND a.cost > 0
          QUALIFY ROW_NUMBER() OVER (PARTITION BY c.change_date, c.campaign_id
                                     ORDER BY a.end_date DESC, a.run_date DESC) = 1
        )
        SELECT c.change_date, c.customer_name, c.campaign_name, c.campaign_id,
               c.currency, c.source, c.old_target, c.new_target,
               c.predicted_cost_pct, c.predicted_gp3_pct,
               pre.start_date  AS pre_window_start,  pre.end_date  AS pre_window_end,
               post.start_date AS post_window_start, post.end_date AS post_window_end,
               ROUND(pre.cost)  AS pre_cost,  ROUND(post.cost)  AS post_cost,
               ROUND(pre.gp3)   AS pre_gp3,   ROUND(post.gp3)   AS post_gp3,
               ROUND(100 * SAFE_DIVIDE(post.cost - pre.cost, pre.cost), 1)
                 AS realized_cost_pct,
               ROUND(100 * SAFE_DIVIDE(post.gp3 - pre.gp3, ABS(pre.gp3)), 1)
                 AS realized_gp3_pct,
               SIGN(c.predicted_cost_pct) = SIGN(post.cost - pre.cost)
                 AS cost_direction_hit,
               SIGN(c.predicted_gp3_pct) = SIGN(post.gp3 - pre.gp3)
                 AS gp3_direction_hit
        FROM `{target_changes}` c
        LEFT JOIN pre  USING (change_date, campaign_id)
        LEFT JOIN post USING (change_date, campaign_id)
    """,
    # Slope calibration — the layer the level-κ cannot provide. For every scored
    # change, the realized marginal (ΔGP2 ÷ Δcost across the step, conv-time)
    # against the sim-implied marginal over the SAME target segment on the
    # change-date curve (anchor excluded). lambda = realized ÷ sim-implied.
    # Measured for the 2026-08-18 apply on SE pb-generic: sim said +1.20 per
    # marginal krona, reality delivered −1.52 — level-κ saw none of that.
    "v_marginal_scoring": """
        WITH seg AS (
          SELECT run_date, customer_name, strategy_id,
                 target_roas, cost, conversions_value,
                 LEAD(target_roas) OVER w AS t2,
                 LEAD(cost) OVER w AS cost2,
                 LEAD(conversions_value) OVER w AS value2
          FROM `{sim_points}`
          WHERE NOT is_anchor
          WINDOW w AS (PARTITION BY run_date, customer_name, strategy_id
                       ORDER BY target_roas)
        ),
        targets AS (
          SELECT c.change_date, c.customer_name, c.campaign_id, x.which, x.t
          FROM `{target_changes}` c,
               UNNEST([STRUCT('old' AS which, c.old_target AS t),
                       STRUCT('new' AS which, c.new_target AS t)]) x
        ),
        interp AS (
          SELECT tg.change_date, tg.customer_name, tg.campaign_id, tg.which,
                 seg.cost AS c1, IFNULL(seg.cost2, seg.cost) AS c2,
                 seg.conversions_value AS v1,
                 IFNULL(seg.value2, seg.conversions_value) AS v2,
                 CASE WHEN seg.t2 IS NULL OR tg.t <= seg.target_roas THEN 0.0
                      WHEN tg.t >= seg.t2 THEN 1.0
                      ELSE (tg.t - seg.target_roas) / (seg.t2 - seg.target_roas)
                 END AS w
          FROM targets tg
          JOIN seg ON seg.run_date = tg.change_date
                  AND seg.customer_name = tg.customer_name
                  AND seg.strategy_id = tg.campaign_id
          QUALIFY ROW_NUMBER() OVER (
            PARTITION BY tg.change_date, tg.campaign_id, tg.which
            ORDER BY CASE WHEN tg.t BETWEEN seg.target_roas
                            AND IFNULL(seg.t2, seg.target_roas) THEN 0 ELSE 1 END,
                     ABS(tg.t - seg.target_roas)) = 1
        ),
        pivoted AS (
          SELECT change_date, customer_name, campaign_id,
                 MAX(IF(which = 'old', c1 + w * (c2 - c1), NULL)) AS sim_cost_old,
                 MAX(IF(which = 'old', v1 + w * (v2 - v1), NULL)) AS sim_value_old,
                 MAX(IF(which = 'new', c1 + w * (c2 - c1), NULL)) AS sim_cost_new,
                 MAX(IF(which = 'new', v1 + w * (v2 - v1), NULL)) AS sim_value_new
          FROM interp
          GROUP BY 1, 2, 3
        )
        SELECT *, SAFE_DIVIDE(realized_marginal, sim_marginal) AS lambda
        FROM (
          SELECT s.change_date, s.customer_name, s.campaign_name, s.campaign_id,
                 s.currency, s.source, s.old_target, s.new_target, s.pre_cost,
                 s.post_cost - s.pre_cost AS delta_cost,
                 (s.post_gp3 + s.post_cost) - (s.pre_gp3 + s.pre_cost) AS delta_gp2,
                 SAFE_DIVIDE((s.post_gp3 + s.post_cost) - (s.pre_gp3 + s.pre_cost),
                             s.post_cost - s.pre_cost) AS realized_marginal,
                 SAFE_DIVIDE(p.sim_value_new - p.sim_value_old,
                             p.sim_cost_new - p.sim_cost_old) AS sim_marginal
          FROM `{v_change_scoring}` s
          LEFT JOIN pivoted p USING (change_date, campaign_id)
          WHERE s.pre_cost IS NOT NULL AND s.post_cost IS NOT NULL
        )
    """,
    # Latest marginal evidence per campaign. Auto rows (v_marginal_scoring, fed
    # by the daily actuals windows) take precedence over hand-loaded rows in
    # `marginal_observations` for the same change — same measurement, consistent
    # windows. spend_increased is judged by realized Δcost, not target
    # direction: cost is ground truth across tROAS and MCV alike.
    "v_lambda": """
        WITH obs AS (
          SELECT change_date, customer_name, campaign_id, campaign_name, currency,
                 old_target, new_target, pre_cost, delta_cost, delta_gp2,
                 realized_marginal, sim_marginal, lambda, source, 1 AS pref
          FROM `{v_marginal_scoring}`
          WHERE realized_marginal IS NOT NULL
          UNION ALL
          SELECT change_date, customer_name, campaign_id, campaign_name, currency,
                 old_target, new_target, pre_cost, delta_cost, delta_gp2,
                 realized_marginal, sim_marginal, lambda, source, 0 AS pref
          FROM `{marginal_observations}`
        )
        SELECT * EXCEPT(pref), delta_cost > 0 AS spend_increased
        FROM obs
        QUALIFY ROW_NUMBER() OVER (PARTITION BY campaign_id
                                   ORDER BY change_date DESC, pref DESC) = 1
    """,
    # Google's latest curve with both axes deflated by shrunk κ, optimum
    # re-derived (anchor excluded, matching the dashboard's Rec. semantics) —
    # then GATED by measured marginal evidence, because κ is a level correction
    # and the sims' worst failure is slope optimism. The gate:
    #   revert-spend-increase    last change raised spend and its realized
    #                            marginal GP2/cost came in under 0.9 — the old
    #                            target was measurably better, so rec_final is
    #                            lifted to at least it. Compared against the
    #                            OLD target, not current: a rec that points up
    #                            but stops short of the refuted step's origin
    #                            still gets lifted the rest of the way.
    #   restore-profitable-spend last change cut spend that was returning over
    #                            1.1 per krona — rec_final restores at most the
    #                            old target.
    # Steps with |Δcost| under 5% of pre-spend carry no usable marginal signal
    # and never gate. rec_calibrated is kept alongside rec_final so the gate's
    # effect is always visible. Deliberately no step cap here: caps are an
    # apply-time policy, not a property of the curve.
    "v_calibrated_recs": """
        WITH base AS (
          SELECT run_date, customer_name, strategy_name, strategy_id, currency,
                 inc_class, strategy_type, kappa_days,
                 ANY_VALUE(current_target) AS current_target,
                 ANY_VALUE(k_cost)  AS k_cost,
                 ANY_VALUE(k_value) AS k_value,
                 ARRAY_AGG(STRUCT(target_roas, gp3_cal)
                           ORDER BY gp3_cal DESC LIMIT 1)[OFFSET(0)].target_roas
                   AS rec_calibrated,
                 ARRAY_AGG(STRUCT(target_roas, gp3_raw)
                           ORDER BY gp3_raw DESC LIMIT 1)[OFFSET(0)].target_roas
                   AS rec_google,
                 ROUND(MAX(gp3_cal)) AS gp3_cal_at_rec,
                 ROUND(ARRAY_AGG(STRUCT(target_roas, gp3_cal)
                       ORDER BY ABS(target_roas - current_target) LIMIT 1)
                       [OFFSET(0)].gp3_cal) AS gp3_cal_at_current
          FROM (
            SELECT s.run_date, s.customer_name, s.strategy_name, s.strategy_id,
                   s.currency, s.current_target, s.strategy_type, s.target_roas,
                   k.inc_class, k.k_cost, k.k_value, k.days AS kappa_days,
                   k.k_value * s.conversions_value - k.k_cost * s.cost AS gp3_cal,
                   s.conversions_value - s.cost AS gp3_raw
            FROM `{sim_points}` s
            JOIN `{v_kappa_calibrated}` k USING (customer_name, strategy_id)
            WHERE s.run_date = (SELECT MAX(run_date) FROM `{sim_points}`)
              AND NOT s.is_anchor
              AND s.current_target > 0
          )
          GROUP BY run_date, customer_name, strategy_name, strategy_id, currency,
                   inc_class, strategy_type, kappa_days
        ),
        gated AS (
          SELECT b.*,
                 l.change_date        AS last_change_date,
                 l.old_target         AS last_old_target,
                 l.realized_marginal  AS last_marginal,
                 l.sim_marginal       AS last_sim_marginal,
                 l.lambda             AS last_lambda,
                 CASE
                   WHEN l.campaign_id IS NULL
                     OR ABS(l.delta_cost) < 0.05 * l.pre_cost THEN NULL
                   WHEN l.spend_increased AND l.realized_marginal < 0.9
                        AND b.rec_calibrated < l.old_target
                     THEN 'revert-spend-increase'
                   WHEN NOT l.spend_increased AND l.realized_marginal > 1.1
                        AND b.rec_calibrated > l.old_target
                     THEN 'restore-profitable-spend'
                 END AS gate
          FROM base b
          LEFT JOIN `{v_lambda}` l ON l.campaign_id = b.strategy_id
        )
        SELECT * EXCEPT(gate), gate,
               CASE gate
                 WHEN 'revert-spend-increase'
                   THEN GREATEST(rec_calibrated, last_old_target)
                 WHEN 'restore-profitable-spend'
                   THEN LEAST(rec_calibrated, last_old_target)
                 ELSE rec_calibrated
               END AS rec_final
        FROM gated
    """,
}


def ensure_tables() -> None:
    """Create the dataset + tables + views if absent. Idempotent; safe to re-run."""
    ds = bigquery.Dataset(f"{BQ_PROJECT}.{BQ_DATASET}")
    ds.location = BQ_LOCATION
    ds.description = ("ROAS simulation history + prediction log for the in-house "
                      "bid-response model. Fed daily by refresh_roas_sims.py; "
                      "no PII (campaign-level aggregates only).")
    bq().create_dataset(ds, exists_ok=True)
    for name, schema in SCHEMAS.items():
        t = bigquery.Table(T(name), schema=schema)
        t.time_partitioning = bigquery.TimePartitioning(field=_PARTITION[name])
        if name in _CLUSTER:
            t.clustering_fields = _CLUSTER[name]
        created = bq().create_table(t, exists_ok=True)
        # exists_ok=True is a no-op on an existing table — append NULLABLE columns
        # in place when SCHEMAS grows a field (norce_sync idiom).
        have = {f.name for f in created.schema}
        missing = [f for f in schema if f.name not in have]
        if missing:
            created.schema = list(created.schema) + [
                bigquery.SchemaField(f.name, f.field_type, mode="NULLABLE",
                                     description=f.description)
                for f in missing]
            bq().update_table(created, ["schema"])
            print(f"   + {name}: added column(s) {', '.join(f.name for f in missing)}")
    for name, sql in _VIEWS.items():
        v = bigquery.Table(T(name))
        v.view_query = sql.format(sim_points=T("sim_points"), actuals=T("actuals"),
                                  target_changes=T("target_changes"),
                                  v_kappa=T("v_kappa"),
                                  v_kappa_calibrated=T("v_kappa_calibrated"),
                                  v_change_scoring=T("v_change_scoring"),
                                  v_marginal_scoring=T("v_marginal_scoring"),
                                  v_lambda=T("v_lambda"),
                                  marginal_observations=T("marginal_observations"))
        bq().delete_table(v, not_found_ok=True)   # views have no data; recreate freely
        bq().create_table(v)


# ── Flattening ───────────────────────────────────────────────────────────────

def _num(v: Any) -> float | None:
    """Grid cell → float, preserving the blank/zero distinction ('' stays NULL)."""
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _date(v: Any) -> str | None:
    s = str(v or "").strip()[:10]
    try:
        return _dt.date.fromisoformat(s).isoformat()
    except ValueError:
        return None


def _grid(doc: dict, key: str, default_cols: list[str]) -> tuple[list[str], list[list]]:
    """A snapshot's row grid by NAME-mapped columns (older docs may lack columns)."""
    import json
    cols = (doc.get("columns") or {}).get(
        {"rows_json": "raw", "shares_json": "shares", "actuals_json": "actuals"}[key])
    cols = list(cols) if isinstance(cols, list) else list(default_cols)
    try:
        rows = json.loads(doc.get(key) or "[]")
    except (TypeError, ValueError):
        rows = []
    rows = [list(r) + [""] * (len(cols) - len(r)) for r in rows
            if isinstance(r, list)]
    return cols, rows


def snapshot_to_rows(doc: dict) -> dict[str, list[dict]]:
    """Flatten one snapshot document (fresh or read back from Firestore) to BQ rows."""
    from refresh_roas_sims import ACTUAL_COLUMNS, RAW_COLUMNS, SHARE_COLUMNS

    run_date = _date(doc.get("run_date"))
    if not run_date:
        raise ValueError(f"snapshot has no valid run_date: {doc.get('run_date')!r}")

    cols, rows = _grid(doc, "rows_json", RAW_COLUMNS)
    i = {c: n for n, c in enumerate(cols)}
    sim_points = []
    for r in rows:
        current = _num(r[i["Current Target Roas"]]) or 0.0
        target = _num(r[i["TARGET ROAS"]])
        cost_micros = _num(r[i["Cost Micros"]])
        sim_points.append({
            "run_date": run_date,
            "customer_name": str(r[i["Customer Name"]]),
            "strategy_name": str(r[i["Bidding Strategy Name"]]),
            "strategy_id": str(r[i["Bidding Strategy Id"]]),
            "current_target": current,
            "start_date": _date(r[i["Start Date"]]),
            "end_date": _date(r[i["End Date"]]),
            "target_roas": target,
            "conversions": _num(r[i["Conversions"]]),
            "conversions_value": _num(r[i["Conversions Value"]]),
            "clicks": _num(r[i["Clicks"]]),
            "cost": None if cost_micros is None else cost_micros / 1e6,
            "impressions": _num(r[i["Impressions"]]),
            "top_slot_impressions": _num(r[i["Top Slot Impressions"]]),
            "strategy_type": str(r[i["Current Campaign Strategy"]]),
            "currency": str(r[i["Currency"]]),
            "is_anchor": bool(current and target is not None
                              and abs(target - current) < 1e-9),
        })

    cols, rows = _grid(doc, "shares_json", SHARE_COLUMNS)
    i = {c: n for n, c in enumerate(cols)}
    shares = [{
        "run_date": run_date,
        "customer_name": str(r[i["Customer Name"]]),
        "campaign_id": str(r[i["Campaign Id"]]),
        "campaign_name": str(r[i["Campaign Name"]]),
        "search_is": _num(r[i["Search IS"]]),
        "top_is": _num(r[i["Top IS"]]),
        "abs_top_is": _num(r[i["Abs Top IS"]]),
        "currency": str(r[i["Currency"]]),
    } for r in rows]

    cols, rows = _grid(doc, "actuals_json", ACTUAL_COLUMNS)
    i = {c: n for n, c in enumerate(cols)}
    actuals = []
    for r in rows:
        cost_micros = _num(r[i["Cost Micros"]])
        actuals.append({
            "run_date": run_date,
            "customer_name": str(r[i["Customer Name"]]),
            "strategy_id": str(r[i["Bidding Strategy Id"]]),
            "strategy_name": str(r[i["Bidding Strategy Name"]]),
            "level": str(r[i["Level"]]),
            "start_date": _date(r[i["Start Date"]]),
            "end_date": _date(r[i["End Date"]]),
            "cost": None if cost_micros is None else cost_micros / 1e6,
            "conversions": _num(r[i["Conversions"]]),
            "conversions_value": _num(r[i["Conversions Value"]]),
            "conv_time_conversions": _num(r[i["Conv Time Conversions"]]),
            "conv_time_conversions_value": _num(r[i["Conv Time Conversions Value"]]),
            "currency": str(r[i["Currency"]]),
        })

    return {"sim_points": sim_points, "impression_shares": shares, "actuals": actuals}


def export_snapshot(doc: dict, ensure: bool = True) -> dict:
    """
    Load one snapshot's grids into their run_date partitions (WRITE_TRUNCATE on the
    partition decorator: idempotent per day, other days untouched).
    """
    if ensure:
        ensure_tables()
    tables = snapshot_to_rows(doc)
    run_date = tables["sim_points"][0]["run_date"] if tables["sim_points"] \
        else _date(doc.get("run_date"))
    decorator = run_date.replace("-", "")
    loaded = {}
    for name in ("sim_points", "impression_shares", "actuals"):
        rows = tables[name]
        cfg = bigquery.LoadJobConfig(schema=SCHEMAS[name],
                                     write_disposition="WRITE_TRUNCATE")
        # Loading zero rows into the decorator still truncates the partition —
        # exactly right for a re-run whose grid was dropped by the byte budget.
        bq().load_table_from_json(rows, f"{T(name)}${decorator}",
                                  job_config=cfg).result()
        loaded[name] = len(rows)
    return {"run_date": run_date, **loaded}


def calibration_payload() -> dict:
    """
    Compact calibrated-recommendation block for the dashboard.

    One row per strategy from v_calibrated_recs, keyed the way the page indexes
    strategies (customer_name + strategy_id). The refresh writes this to
    Firestore `roas_sim_calibration/latest` right after the BigQuery export, so
    the block always reflects the snapshot that was just collected; the page
    treats a missing block exactly like a payload that predates the feature.
    """
    sql = f"""
        SELECT run_date, customer_name, strategy_id, strategy_name, inc_class,
               kappa_days, k_cost, k_value, current_target,
               rec_google, rec_calibrated, rec_final, gate,
               last_change_date, last_old_target, last_marginal,
               last_sim_marginal, gp3_cal_at_current, gp3_cal_at_rec
        FROM `{T("v_calibrated_recs")}`
    """
    rows = []
    run_date = None
    for r in bq().query(sql).result():
        run_date = r.run_date.isoformat()
        rows.append({
            "account": r.customer_name,
            "strategyId": str(r.strategy_id),
            "name": r.strategy_name,
            "incClass": r.inc_class,
            "kappaDays": r.kappa_days,
            "kCost": round(r.k_cost, 4) if r.k_cost is not None else None,
            "kValue": round(r.k_value, 4) if r.k_value is not None else None,
            "currentTarget": r.current_target,
            "recGoogle": r.rec_google,
            "recCalibrated": r.rec_calibrated,
            "recFinal": r.rec_final,
            "gate": r.gate,
            "lastChangeDate": r.last_change_date.isoformat() if r.last_change_date else None,
            "lastOldTarget": r.last_old_target,
            "lastMarginal": round(r.last_marginal, 4) if r.last_marginal is not None else None,
            "lastSimMarginal": round(r.last_sim_marginal, 4) if r.last_sim_marginal is not None else None,
            "gp3CalAtCurrent": r.gp3_cal_at_current,
            "gp3CalAtRec": r.gp3_cal_at_rec,
        })
    return {
        "run_date": run_date,
        "generated_at": (_dt.datetime.now(_dt.timezone.utc).replace(microsecond=0)
                         .isoformat().replace("+00:00", "Z")),
        "rows": rows,
    }


def backfill(dates: list[str] | None = None) -> list[dict]:
    """Export every snapshot still in Firestore (or just `dates`), oldest first."""
    from refresh_roas_sims import RUN_DATE_RE, SNAPSHOT_COLLECTION, _firestore

    db = _firestore()
    ids = sorted(ref.id for ref in db.collection(SNAPSHOT_COLLECTION).list_documents()
                 if RUN_DATE_RE.match(ref.id))
    if dates:
        ids = [d for d in ids if d in set(dates)]
    ensure_tables()
    out = []
    for d in ids:
        doc = db.collection(SNAPSHOT_COLLECTION).document(d).get().to_dict()
        if not doc:
            continue
        res = export_snapshot(doc, ensure=False)
        print(f"   {d}: sim_points={res['sim_points']} shares={res['impression_shares']} "
              f"actuals={res['actuals']}")
        out.append(res)
    return out


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--ensure", action="store_true", help="create dataset/tables/views only")
    ap.add_argument("--backfill", action="store_true", help="export every Firestore snapshot")
    ap.add_argument("--date", help="export one snapshot (YYYY-MM-DD) from Firestore")
    args = ap.parse_args()
    if args.ensure:
        ensure_tables()
        print(f"ensured {BQ_PROJECT}.{BQ_DATASET}")
    elif args.backfill:
        res = backfill()
        print(f"backfilled {len(res)} snapshot(s)")
    elif args.date:
        res = backfill([args.date])
        print(res)
    else:
        ap.print_help()
