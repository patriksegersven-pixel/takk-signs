/**
 * Babyshop — GP3 Optimization
 * Target ROAS bid-simulation collector (Google Ads MCC script)
 * ---------------------------------------------------------------------------
 * Pulls Google's Target ROAS bid simulations for every portfolio bidding
 * strategy (and, optionally, every campaign-level Target ROAS strategy) in the
 * configured accounts, and APPENDS them to the "Raw" tab of a spreadsheet as a
 * dated snapshot. It also collects last-7-days impression-share metrics per
 * campaign into a second "Shares" tab, which the dashboard uses to derive
 * incrementality factors for brand and private-label campaigns from how much
 * auction headroom is actually left (see README, "Incrementality"), and the
 * ACTUAL performance of the same entities over the same window into a third
 * "Actuals" tab, so the dashboard can show what really happened next to what
 * the simulator projects.
 *
 * Design rules this script follows:
 *   - Append only. It never calls clearContent() and never rewrites history;
 *     the dashboard's trend view depends on old snapshots surviving.
 *   - Write only. The sheet is an output sink; nothing is read back from it
 *     except the header row, so a broken sheet can never break a run.
 *   - Idempotent per day. Re-running on the same date replaces that date's rows
 *     rather than duplicating them.
 *   - Self-pruning. Rows older than LOOKBACK_PRUNE_DAYS are dropped so the
 *     sheet cannot grow without bound.
 *
 * Install:
 *   Google Ads MCC → Tools & Settings → Bulk actions → Scripts → + (new script)
 *   Paste this file, set the CONFIG block, Authorise, Preview, then Run.
 *   Schedule it Daily or Weekly. Weekly matches the 7-day simulation window.
 *
 * Output columns (must stay in sync with apps-script/webapp.gs and the
 * dashboard's COLUMN_MAP):
 *   "Raw"    Customer Name | Bidding Strategy Name | Current Target Roas |
 *            Bidding Strategy Id | Start Date | End Date | TARGET ROAS | Conversions |
 *            Conversions Value | Clicks | Cost Micros | Impressions |
 *            Top Slot Impressions | Current Campaign Strategy | Currency | Run Date
 *   "Shares" Customer Name | Campaign Id | Campaign Name | Search IS | Top IS |
 *            Abs Top IS | Currency | Run Date
 *   "Actuals" Customer Name | Bidding Strategy Id | Bidding Strategy Name | Level |
 *            Start Date | End Date | Cost Micros | Conversions | Conversions Value |
 *            Conv Time Conversions | Conv Time Conversions Value | Currency | Run Date
 *
 * All three tabs follow the same rules: append only, write only, idempotent per day,
 * self-pruning. A missing metric is written as a BLANK cell and never as 0. For
 * impression share the dashboard treats 0/blank alike as "no data" and falls back to
 * the flat class factor, so a fabricated 0 would silently mean "no headroom"; on the
 * Actuals tab a REAL 0 is a measurement (spend that returned nothing) and must stay
 * distinguishable from "the API did not report this".
 *
 * TWO ATTRIBUTION SCHEMES ON THE ACTUALS TAB
 *   "Conversions" / "Conversions Value" are Google's standard CLICK-TIME metrics:
 *   a conversion is counted on the date of the click that led to it. "Conv Time
 *   Conversions" / "Conv Time Conversions Value" are the same conversions counted on
 *   the date they HAPPENED (metrics.*_by_conversion_date) — the "by conversion time"
 *   columns, which is the default view in this account's Google Ads UI. Cost is
 *   identical under both, so the tab carries it once. Neither is more correct: click
 *   time answers "what did this spend eventually return", conversion time answers
 *   "what was banked in this window". Both lag — conversions keep arriving for days
 *   after the window closes and are back-dated under either scheme.
 *
 * ACTUALS WINDOW
 *   Each Actuals row is measured over the SAME start/end dates as the simulation it
 *   joins to, not over a fixed last-7-days range, so actual and simulated ROAS
 *   describe the same week. Google refreshes simulation windows per entity, so one
 *   GAQL query is issued per DISTINCT window in the account (normally exactly one).
 *   Entities whose simulation carried no window fall back to LAST_7_DAYS.
 *
 * JOIN KEY
 *   Actuals rows join to Raw rows on (Customer Name, Bidding Strategy Id, Run Date).
 *   The id is the campaign id for campaign-level rows and the bidding strategy id for
 *   portfolio-level rows — the same id the Raw tab carries, from the same run.
 */

/* ========================== CONFIG ========================== */

var CONFIG = {
  /** Full URL of the target spreadsheet. The sheet itself stays private;
      this URL only identifies it — access is governed by Drive sharing. */
  SPREADSHEET_URL: 'https://docs.google.com/spreadsheets/d/1x4GJxXSPzmJ-53hpal-0KN6_tLzhFjvJMzH2GRy0KD8/edit',

  /** Child account CIDs to process, with or without dashes. */
  ACCOUNT_IDS: [
    '485-148-5396',   // Babyshop SE  (SEK)
    '862-394-5183',   // Babyshop NO  (NOK)
    '830-823-2278',   // Lekmer NO    (NOK)
    '778-011-4635',   // Lekmer SE    (SEK)
    '616-139-9704',   // Babyshop FI  (EUR)
    '554-148-7401',   // Babyshop ROW (SEK)
    '275-639-7225',   // Lekmer DK    (DKK)
    '205-429-4342'    // Babyshop DK  (SEK)
  ],

  /** Tab that receives the appended snapshots. Created automatically. */
  SHEET_NAME: 'Raw',

  /**
   * Tab that receives the per-campaign impression-share snapshots. Created
   * automatically, pruned on the same schedule as Raw. Set COLLECT_SHARES to
   * false to skip the extra GAQL query per account; the dashboard then falls
   * back to flat class incrementality factors everywhere.
   */
  SHARES_SHEET_NAME: 'Shares',
  COLLECT_SHARES: true,

  /**
   * Tab that receives the actual (measured) performance of every simulated entity over
   * that entity's own simulation window, under both attribution schemes. Created
   * automatically, pruned on the same schedule as Raw. Set COLLECT_ACTUALS to false to
   * skip one GAQL query per distinct simulation window per account; the dashboard then
   * hides its actual-ROAS columns entirely.
   */
  ACTUALS_SHEET_NAME: 'Actuals',
  COLLECT_ACTUALS: true,

  /** Drop snapshots whose Run Date is older than this many days. */
  LOOKBACK_PRUNE_DAYS: 90,

  /**
   * Collect campaign-level Target ROAS simulations. Must stay ON for Babyshop:
   * validated against the live API (Aug 2026), Google exposes these accounts'
   * simulations on campaign_simulation — bidding_strategy_simulation returns
   * nothing for SE / Lekmer NO / Lekmer DK, and only one strategy elsewhere.
   */
  INCLUDE_CAMPAIGNS: true,

  /**
   * Also collect portfolio-strategy-level simulations. OFF by default: the
   * campaigns inside those strategies already show up in campaign_simulation,
   * so including both levels double-counts the same auction traffic in any
   * cross-strategy total. Turn on only for a one-off comparison.
   */
  INCLUDE_PORTFOLIO: false,

  /** Log every simulation point. Noisy; useful when debugging a single account. */
  VERBOSE: false
};

/* ========================== CONSTANTS ========================== */

var HEADERS = [
  'Customer Name', 'Bidding Strategy Name', 'Current Target Roas', 'Bidding Strategy Id',
  'Start Date', 'End Date', 'TARGET ROAS', 'Conversions', 'Conversions Value', 'Clicks',
  'Cost Micros', 'Impressions', 'Top Slot Impressions', 'Current Campaign Strategy',
  'Currency', 'Run Date'
];

var RUN_DATE_COL = HEADERS.indexOf('Run Date') + 1;   // 1-based, for pruning

var SHARE_HEADERS = [
  'Customer Name', 'Campaign Id', 'Campaign Name', 'Search IS', 'Top IS', 'Abs Top IS',
  'Currency', 'Run Date'
];

/* The Run Date sits in a DIFFERENT column on each tab, which is why every writer
   helper below takes the run-date column index as a parameter rather than closing
   over one module-level constant. */
var SHARE_RUN_DATE_COL = SHARE_HEADERS.indexOf('Run Date') + 1;

/* Impression-share columns, 0-based — restored to numbers when decoding. */
var SHARE_NUMERIC_FROM = 3, SHARE_NUMERIC_TO = 5;

var ACTUAL_HEADERS = [
  'Customer Name', 'Bidding Strategy Id', 'Bidding Strategy Name', 'Level',
  'Start Date', 'End Date', 'Cost Micros', 'Conversions', 'Conversions Value',
  'Conv Time Conversions', 'Conv Time Conversions Value', 'Currency', 'Run Date'
];

var ACTUAL_RUN_DATE_COL = ACTUAL_HEADERS.indexOf('Run Date') + 1;

/* Metric columns, 0-based. The Bidding Strategy Id stays a string on purpose — it is an
   identifier, not a quantity, and the dashboard joins on it as text. */
var ACTUAL_NUMERIC_FROM = 6, ACTUAL_NUMERIC_TO = 10;

/* Column offsets inside a built simulation row (HEADERS order), used to derive which
   entities were simulated over which window before the actuals queries are issued. */
var SIM_NAME_IDX = HEADERS.indexOf('Bidding Strategy Name');
var SIM_ID_IDX = HEADERS.indexOf('Bidding Strategy Id');
var SIM_START_IDX = HEADERS.indexOf('Start Date');
var SIM_END_IDX = HEADERS.indexOf('End Date');

/**
 * The two levels actuals can be measured at, mirroring the two simulation collectors.
 *   resource / field  GAQL resource and field prefix
 *   result            key the response object carries (AdsApp.search lowerCamelCases it)
 *   level             written to the "Level" column so a consumer can tell them apart
 * Portfolio-level rows only ever appear when CONFIG.INCLUDE_PORTFOLIO is on.
 */
var ACTUAL_SOURCES = {
  campaign:  { level: 'campaign',  resource: 'campaign',         field: 'campaign',         result: 'campaign' },
  portfolio: { level: 'portfolio', resource: 'bidding_strategy', field: 'bidding_strategy', result: 'biddingStrategy' }
};

/* executeInParallel caps each child's return string at ~100 KB. */
var MAX_RETURN_CHARS = 90000;

/* Parallel execution passes a single string argument, so config travels packed. */
var ARG_SEPARATOR = '||';
/* Written as escapes on purpose: this file gets copy-pasted into the Google Ads
   script editor, where a literal control character would not survive the trip. */
var ROW_SEPARATOR  = '\u001E';   // ASCII record separator - untypeable in Ads entity names
var CELL_SEPARATOR = '\u001F';   // ASCII unit separator
/* executeInParallel hands back exactly ONE string per account, so all three datasets
   travel inside that single string: <sim rows> GS <share rows> GS <actual rows>. A
   payload carrying fewer group separators than that is a partial return — the missing
   datasets decode to [] without special-casing. */
var DATASET_SEPARATOR = '\u001D';   // ASCII group separator

/* ========================== ENTRY POINTS ========================== */

function main() {
  if (!CONFIG.SPREADSHEET_URL) {
    throw new Error('Set CONFIG.SPREADSHEET_URL before running this script.');
  }
  var runDate = todayInManagerTimezone();
  Logger.log('GP3 simulation collector — run date ' + runDate);

  var accountIds = CONFIG.ACCOUNT_IDS.map(function (id) { return String(id).trim(); })
    .filter(function (id) { return id.length > 0; });
  if (!accountIds.length) throw new Error('CONFIG.ACCOUNT_IDS is empty.');

  var payload = [runDate, CONFIG.INCLUDE_CAMPAIGNS ? '1' : '0', CONFIG.VERBOSE ? '1' : '0',
    CONFIG.INCLUDE_PORTFOLIO ? '1' : '0', CONFIG.COLLECT_SHARES ? '1' : '0',
    CONFIG.COLLECT_ACTUALS ? '1' : '0'].join(ARG_SEPARATOR);

  var accounts = AdsManagerApp.accounts().withIds(accountIds).get();
  var found = 0;
  while (accounts.hasNext()) { accounts.next(); found++; }
  Logger.log('Matched ' + found + ' of ' + accountIds.length + ' configured account id(s).');

  AdsManagerApp.accounts()
    .withIds(accountIds)
    .executeInParallel('collectSimulations', 'writeSnapshot', payload);
}

/**
 * Runs once inside each child account. Must return a string; the callback
 * receives them all together. Three datasets travel in that one string, separated
 * by DATASET_SEPARATOR: the simulation rows first (the primary payload), the
 * impression-share rows second, the actual-performance rows third.
 */
function collectSimulations(packedArgs) {
  var parts = String(packedArgs).split(ARG_SEPARATOR);
  var runDate = parts[0];
  var includeCampaigns = parts[1] === '1';
  var verbose = parts[2] === '1';
  var includePortfolio = parts[3] === '1';
  var collectShares = parts[4] === '1';
  var collectActuals = parts[5] === '1';

  var rows = [];
  var shareRows = [];
  var actualRows = [];
  try {
    /* The actuals queries are scoped to the entities that were actually simulated and
       to those entities' own simulation windows, so the plan is derived from the rows
       each collector produced rather than re-queried. */
    var plans = [];
    if (includePortfolio) {
      var portfolioRows = portfolioSimulations(runDate, verbose);
      rows = rows.concat(portfolioRows);
      plans.push({ source: ACTUAL_SOURCES.portfolio, windows: simulationWindows(portfolioRows) });
    }
    if (includeCampaigns) {
      var campaignRows = campaignSimulations(runDate, verbose);
      rows = rows.concat(campaignRows);
      plans.push({ source: ACTUAL_SOURCES.campaign, windows: simulationWindows(campaignRows) });
    }

    /* Shares are a SECONDARY payload: a campaign type that reports no impression
       share, or a query the account cannot serve, must never cost us the
       simulations. Hence its own catch — the outer one still surfaces a failed
       simulation collection as ERROR in the callback. */
    if (collectShares) {
      try {
        shareRows = campaignShares(runDate, verbose);
      } catch (se) {
        Logger.log('WARNING ' + AdsApp.currentAccount().getName() +
          ': impression-share collection failed (' + se + '). Simulations are unaffected; ' +
          'the dashboard falls back to flat class incrementality factors.');
        shareRows = [];
      }
    }

    /* Actuals are a SECONDARY payload too, and catch per level inside — see
       collectActualRows(). Losing them only hides two display columns. */
    if (collectActuals) actualRows = collectActualRows(runDate, plans, verbose);
  } catch (e) {
    Logger.log('ERROR in ' + AdsApp.currentAccount().getName() + ': ' + e);
    throw e;   // surface as ERROR in the callback instead of a silent empty account
  }
  Logger.log(AdsApp.currentAccount().getName() + ': ' + rows.length + ' simulation point(s), ' +
    shareRows.length + ' impression-share row(s), ' + actualRows.length + ' actual-performance row(s).');

  return packDatasets(encodeRows(rows), encodeRows(shareRows), encodeRows(actualRows));
}

/**
 * Fit all three encoded datasets inside the ~100 KB parallel-return cap, trimming on a
 * row boundary rather than letting a mid-row cut corrupt a snapshot.
 *
 * Trim order is value order, cheapest first: actuals are display-only, shares change
 * which incrementality factor every brand / private-label recommendation was read off,
 * and the simulations ARE the run.
 */
function packDatasets(sims, shares, actuals) {
  var name = AdsApp.currentAccount().getName();
  var budget = MAX_RETURN_CHARS - 2 * DATASET_SEPARATOR.length;
  actuals = actuals || '';

  if (sims.length + shares.length + actuals.length > budget) {
    var roomA = budget - sims.length - shares.length;
    var trimmedA = roomA <= 0 ? '' : trimToRow(actuals, roomA);
    Logger.log('WARNING ' + name + ': combined payload ' +
      (sims.length + shares.length + actuals.length) +
      ' chars exceeds the parallel-return cap; actual-performance rows trimmed from ' +
      actuals.length + ' to ' + trimmedA.length + ' chars.');
    actuals = trimmedA;
  }
  if (sims.length + shares.length > budget) {
    var room = budget - sims.length;
    var trimmed = room <= 0 ? '' : trimToRow(shares, room);
    Logger.log('WARNING ' + name + ': payload still ' + (sims.length + shares.length) +
      ' chars after dropping actuals; impression-share rows trimmed from ' +
      shares.length + ' to ' + trimmed.length + ' chars.');
    shares = trimmed;
  }
  if (sims.length > budget) {   // simulations alone overflow: trim them too, last resort
    var kept = trimToRow(sims, budget);
    Logger.log('WARNING ' + name + ': simulation payload ' + sims.length +
      ' chars still exceeds the cap after dropping shares and actuals; trimmed to ' +
      kept.length + ' chars.');
    sims = kept;
    shares = '';
    actuals = '';
  }
  return sims + DATASET_SEPARATOR + shares + DATASET_SEPARATOR + actuals;
}

/** Truncate an encoded dataset at the last complete row that fits in `limit`. */
function trimToRow(encoded, limit) {
  if (encoded.length <= limit) return encoded;
  var cut = encoded.lastIndexOf(ROW_SEPARATOR, limit);
  return cut > 0 ? encoded.slice(0, cut) : '';
}

/* ========================== COLLECTORS ========================== */

/** Target ROAS simulations for portfolio (shared) bidding strategies. */
function portfolioSimulations(runDate, verbose) {
  var customer = accountInfo();
  var out = [];

  var query =
    'SELECT ' +
    '  bidding_strategy_simulation.start_date, ' +
    '  bidding_strategy_simulation.end_date, ' +
    '  bidding_strategy_simulation.type, ' +
    '  bidding_strategy_simulation.target_roas_point_list.points, ' +
    '  bidding_strategy.id, ' +
    '  bidding_strategy.name, ' +
    '  bidding_strategy.type, ' +
    '  bidding_strategy.target_roas.target_roas, ' +
    '  bidding_strategy.maximize_conversion_value.target_roas ' +
    'FROM bidding_strategy_simulation ' +
    "WHERE bidding_strategy_simulation.type = 'TARGET_ROAS'";

  var it = AdsApp.search(query);
  while (it.hasNext()) {
    var row = it.next();
    var sim = row.biddingStrategySimulation || {};
    var strat = row.biddingStrategy || {};
    var points = sim.targetRoasPointList && sim.targetRoasPointList.points;
    if (!points || !points.length) continue;

    var currentTarget = roasOrNull(strat.targetRoas && strat.targetRoas.targetRoas);
    if (currentTarget == null) {
      currentTarget = roasOrNull(strat.maximizeConversionValue && strat.maximizeConversionValue.targetRoas);
    }

    for (var i = 0; i < points.length; i++) {
      out.push(buildRow(customer, {
        name: strat.name,
        id: strat.id,
        currentTarget: currentTarget,
        biddingType: strat.type,
        startDate: sim.startDate,
        endDate: sim.endDate
      }, points[i], runDate));
    }
    if (verbose) Logger.log('  portfolio ' + strat.name + ': ' + points.length + ' point(s)');
  }
  return out;
}

/** Target ROAS simulations for campaigns (the main source for these accounts). */
function campaignSimulations(runDate, verbose) {
  var customer = accountInfo();
  var out = [];
  var strategyTargets = portfolioTargetMap();

  var query =
    'SELECT ' +
    '  campaign_simulation.start_date, ' +
    '  campaign_simulation.end_date, ' +
    '  campaign_simulation.type, ' +
    '  campaign_simulation.target_roas_point_list.points, ' +
    '  campaign.id, ' +
    '  campaign.name, ' +
    '  campaign.bidding_strategy, ' +
    '  campaign.bidding_strategy_type, ' +
    '  campaign.target_roas.target_roas, ' +
    '  campaign.maximize_conversion_value.target_roas ' +
    'FROM campaign_simulation ' +
    "WHERE campaign_simulation.type = 'TARGET_ROAS'";

  var it = AdsApp.search(query);
  while (it.hasNext()) {
    var row = it.next();
    var sim = row.campaignSimulation || {};
    var camp = row.campaign || {};
    var points = sim.targetRoasPointList && sim.targetRoasPointList.points;
    if (!points || !points.length) continue;

    /* Current target lives in one of three places, and unset values arrive as
       0 rather than null (validated live, Aug 2026): the campaign's own tROAS,
       the campaign's Maximize Conversion Value target, or — for campaigns in a
       portfolio strategy — on the bidding_strategy resource. A target of 0 is
       never legitimate, so 0 always means "look at the next source". */
    var currentTarget = roasOrNull(camp.targetRoas && camp.targetRoas.targetRoas);
    if (currentTarget == null) {
      currentTarget = roasOrNull(camp.maximizeConversionValue && camp.maximizeConversionValue.targetRoas);
    }
    if (currentTarget == null && camp.biddingStrategy) {
      var stratId = String(camp.biddingStrategy).split('/').pop();
      currentTarget = roasOrNull(strategyTargets[stratId]);
    }

    for (var i = 0; i < points.length; i++) {
      out.push(buildRow(customer, {
        name: camp.name,
        id: camp.id,
        currentTarget: currentTarget,
        biddingType: camp.biddingStrategyType,
        startDate: sim.startDate,
        endDate: sim.endDate
      }, points[i], runDate));
    }
    if (verbose) Logger.log('  campaign ' + camp.name + ': ' + points.length + ' point(s)');
  }
  return out;
}

/**
 * Last-7-days impression share per enabled campaign — the auction-headroom input
 * behind the dashboard's dynamic incrementality factors.
 *
 * WHY: a brand campaign already holding ~100% absolute-top impression share has
 * almost nothing left to win, so marginal spend on it is close to pure defence and
 * its incremental value is near the floor. One being outbid still has defensive
 * headroom to buy back. Private label is read off plain search impression share:
 * there the question is presence at all, not position.
 *
 * Google returns these metrics BUCKETED at the extremes: "<10%" arrives as 0.0999
 * and anything above 90% is reported as >0.9. They are taken at face value here —
 * the bucketing is documented in the README rather than smoothed away.
 *
 * A campaign whose channel type reports no impression share (Performance Max,
 * Display, video) yields ABSENT metrics. Those are written as BLANK cells, never
 * as 0: downstream, blank and 0 both mean "no data, fall back to the class
 * factor", and inventing a 0 would read as "no headroom at all".
 */
function campaignShares(runDate, verbose) {
  var customer = accountInfo();
  var out = [];

  var query =
    'SELECT ' +
    '  campaign.id, ' +
    '  campaign.name, ' +
    '  metrics.search_impression_share, ' +
    '  metrics.search_top_impression_share, ' +
    '  metrics.search_absolute_top_impression_share ' +
    'FROM campaign ' +
    'WHERE segments.date DURING LAST_7_DAYS ' +
    "  AND campaign.status = 'ENABLED'";

  var it = AdsApp.search(query);
  while (it.hasNext()) {
    var row = it.next();
    var camp = row.campaign || {};
    var m = row.metrics || {};
    var search = shareOrBlank(m.searchImpressionShare);
    var top = shareOrBlank(m.searchTopImpressionShare);
    var absTop = shareOrBlank(m.searchAbsoluteTopImpressionShare);
    if (search === '' && top === '' && absTop === '') continue;   // nothing worth a row

    out.push([
      customer.name,
      String(camp.id == null ? '' : camp.id),
      safeName(camp.name),
      search,
      top,
      absTop,
      customer.currency,
      runDate
    ]);
    if (verbose) {
      Logger.log('  shares ' + camp.name + ': IS ' + search + ', top ' + top + ', abs top ' + absTop);
    }
  }
  return out;
}

/**
 * An impression-share metric is either a number in [0,1] or nothing at all.
 * Anything unparseable becomes a blank cell rather than a fabricated 0.
 */
function shareOrBlank(v) {
  if (v == null || v === '') return '';
  var n = Number(v);
  if (isNaN(n) || n < 0) return '';
  return n > 1 ? 1 : n;   // the API reports fractions; clamp a stray percentage-style value
}

/**
 * Group already-built simulation rows by their simulation window, so the actuals for a
 * window can be fetched in ONE query covering every entity Google simulated over it.
 * Returns [{ start, end, ids: { id: name } }], with a blank start/end meaning "the
 * simulation carried no window" — those fall back to LAST_7_DAYS downstream.
 */
function simulationWindows(rows) {
  var byWindow = {};
  var keys = [];
  for (var i = 0; i < rows.length; i++) {
    var id = String(rows[i][SIM_ID_IDX] == null ? '' : rows[i][SIM_ID_IDX]).trim();
    if (!id) continue;                                   // nothing to join an actuals row to
    var start = isoDateOrBlank(rows[i][SIM_START_IDX]);
    var end = isoDateOrBlank(rows[i][SIM_END_IDX]);
    var key = start + '|' + end;
    if (!byWindow[key]) { byWindow[key] = { start: start, end: end, ids: {} }; keys.push(key); }
    byWindow[key].ids[id] = String(rows[i][SIM_NAME_IDX] == null ? '' : rows[i][SIM_NAME_IDX]);
  }
  return keys.map(function (k) { return byWindow[k]; });
}

/**
 * A date from these rows is interpolated straight into a GAQL string, so only a literal
 * yyyy-MM-dd is ever accepted; anything else degrades to the LAST_7_DAYS fallback.
 */
function isoDateOrBlank(v) {
  var m = String(v == null ? '' : v).trim().match(/^\d{4}-\d{2}-\d{2}$/);
  return m ? m[0] : '';
}

/**
 * Actual performance for every simulated entity, at both levels, degrading in two steps
 * rather than failing:
 *   1. each level gets its OWN catch — bidding_strategy and campaign are different
 *      resources with different metric support, and one failing must not cost us the
 *      other, nor the simulations, which are already collected by the time this runs;
 *   2. a failed level is retried WITHOUT the conversion-time metrics, which are the ones
 *      a resource may not serve, so click-time actuals still arrive.
 * Only when both attempts fail does a level yield nothing, and even then it is a WARNING.
 */
function collectActualRows(runDate, plans, verbose) {
  var out = [];
  for (var i = 0; i < plans.length; i++) {
    var level = plans[i].source.level;
    try {
      out = out.concat(entityActuals(runDate, plans[i].windows, plans[i].source, verbose, true));
      continue;
    } catch (ae) {
      Logger.log('WARNING ' + AdsApp.currentAccount().getName() + ': actual-performance collection ' +
        'failed at ' + level + ' level (' + ae + '). Retrying without the conversion-time ' +
        'metrics, which are the ones a resource may not support.');
    }
    /* Degrade rather than vanish, the same rule the other secondary payloads follow: losing
       the conversion-time columns is much cheaper than losing the measurement entirely. The
       conversion-time cells then stay BLANK, which the dashboard renders as a dash. */
    try {
      out = out.concat(entityActuals(runDate, plans[i].windows, plans[i].source, verbose, false));
      Logger.log('  recovered click-time actuals at ' + level + ' level; the conversion-time ' +
        'columns will be blank for this account.');
    } catch (ae2) {
      Logger.log('WARNING ' + AdsApp.currentAccount().getName() + ': actual-performance collection ' +
        'failed at ' + level + ' level even without the conversion-time metrics (' + ae2 +
        '). Simulations and impression shares are unaffected; the dashboard hides its ' +
        'actual-ROAS columns for these rows.');
    }
  }
  return out;
}

/**
 * One row per simulated entity per window, carrying cost plus BOTH attribution schemes:
 *
 *   metrics.conversions / metrics.conversions_value
 *       CLICK TIME — counted on the date of the click that led to the conversion.
 *   metrics.conversions_by_conversion_date / metrics.conversions_value_by_conversion_date
 *       CONVERSION TIME — counted on the date the conversion happened. These are the
 *       "by conv. time" columns and the default view in this account's Google Ads UI.
 *
 * Cost is the same figure under both schemes, so it is carried once and the dashboard
 * divides each value by it. Rows with no spend in the window are skipped outright: ROAS
 * is undefined there and a blank row would only add noise to the join.
 *
 * The query is filtered by (but does not select) segments.date, so the API aggregates the
 * whole window into one row per entity. Every entity in the account comes back; rows for
 * entities with no simulation are dropped here rather than in the query, because a GAQL
 * id list long enough to cover a large account is worse than a client-side filter.
 *
 * `withConvTime` false drops the two conversion-time metrics from the SELECT — the retry path
 * in collectActualRows() for a resource that will not serve them. Their cells then come out
 * BLANK (never 0), so the dashboard shows a dash for conversion time and the real figure for
 * click time instead of losing both.
 */
function entityActuals(runDate, windows, source, verbose, withConvTime) {
  var customer = accountInfo();
  var out = [];

  for (var w = 0; w < windows.length; w++) {
    var win = windows[w];
    var wanted = win.ids;
    var ids = Object.keys(wanted);
    if (!ids.length) continue;

    var range = (win.start && win.end)
      ? "segments.date BETWEEN '" + win.start + "' AND '" + win.end + "'"
      : 'segments.date DURING LAST_7_DAYS';

    var query =
      'SELECT ' +
      '  ' + source.field + '.id, ' +
      '  ' + source.field + '.name, ' +
      '  metrics.cost_micros, ' +
      '  metrics.conversions, ' +
      '  metrics.conversions_value' +
      (withConvTime === false ? ' '
        : ', metrics.conversions_by_conversion_date, metrics.conversions_value_by_conversion_date ') +
      'FROM ' + source.resource + ' ' +
      'WHERE ' + range;

    var matched = 0;
    var it = AdsApp.search(query);
    while (it.hasNext()) {
      var row = it.next();
      var ent = row[source.result] || {};
      var id = String(ent.id == null ? '' : ent.id);
      if (!id || !wanted.hasOwnProperty(id)) continue;    // not simulated: nothing to compare against
      var m = row.metrics || {};
      var costMicros = metricOrBlank(m.costMicros);
      if (!(costMicros > 0)) continue;                    // no spend in the window: ROAS is undefined

      out.push([
        customer.name,
        id,
        safeName(wanted[id] || ent.name),
        source.level,
        win.start,
        win.end,
        costMicros,
        metricOrBlank(m.conversions),
        metricOrBlank(m.conversionsValue),
        metricOrBlank(m.conversionsByConversionDate),
        metricOrBlank(m.conversionsValueByConversionDate),
        customer.currency,
        runDate
      ]);
      matched++;
      if (verbose) {
        Logger.log('  actuals ' + source.level + ' ' + (wanted[id] || id) + ': cost ' +
          (costMicros / 1e6) + ' ' + customer.currency + ', click-time value ' +
          metricOrBlank(m.conversionsValue) + ', conv-time value ' +
          metricOrBlank(m.conversionsValueByConversionDate));
      }
    }
    Logger.log('  actuals ' + source.level + ' ' + (win.start && win.end ? win.start + '..' + win.end : 'last 7 days') +
      ': ' + matched + ' of ' + ids.length + ' simulated entity/entities had spend.');
  }
  return out;
}

/**
 * A performance metric is either a number or nothing at all. Absent stays BLANK — never 0.
 * On this tab a real 0 IS a measurement (spend that returned nothing), so the two must
 * stay distinguishable; a fabricated 0 would read as a genuine 0.00x ROAS.
 */
function metricOrBlank(v) {
  if (v == null || v === '') return '';
  var n = Number(v);   // int64 metrics arrive from the API as strings
  return isNaN(n) ? '' : n;
}

/**
 * Map of portfolio bidding strategy id -> current Target ROAS, used to resolve
 * the real target for campaigns whose bidding runs through a portfolio.
 */
function portfolioTargetMap() {
  var map = {};
  var query =
    'SELECT bidding_strategy.id, ' +
    '  bidding_strategy.target_roas.target_roas, ' +
    '  bidding_strategy.maximize_conversion_value.target_roas ' +
    'FROM bidding_strategy';
  var it = AdsApp.search(query);
  while (it.hasNext()) {
    var row = it.next();
    var strat = row.biddingStrategy || {};
    var target = roasOrNull(strat.targetRoas && strat.targetRoas.targetRoas);
    if (target == null) {
      target = roasOrNull(strat.maximizeConversionValue && strat.maximizeConversionValue.targetRoas);
    }
    if (strat.id != null && target != null) map[String(strat.id)] = target;
  }
  return map;
}

/** A ROAS target of null, '', or 0 all mean "not set here". */
function roasOrNull(v) {
  if (v == null || v === '') return null;
  var n = Number(v);
  return isNaN(n) || n === 0 ? null : n;
}

/* ========================== ROW BUILDING ========================== */

function accountInfo() {
  var acct = AdsApp.currentAccount();
  return { name: acct.getName(), currency: acct.getCurrencyCode() };
}

/**
 * Flatten an entity name to one line with no transport separators in it. Every writer uses
 * this: a name carrying a cell, row or dataset separator would not corrupt one cell, it
 * would silently re-cut the whole payload.
 */
function safeName(v) {
  return String(v == null ? '' : v).replace(/[,\r\n\u001D\u001E\u001F]+/g, ';');
}

/** One spreadsheet row per simulation point, in HEADERS order. */
function buildRow(customer, entity, point, runDate) {
  return [
    customer.name,
    safeName(entity.name),                             // one line, no separator collisions
    entity.currentTarget == null ? '' : Number(entity.currentTarget),
    String(entity.id == null ? '' : entity.id),
    entity.startDate || '',
    entity.endDate || '',
    numOr(point.targetRoas, ''),
    numOr(point.biddableConversions, 0),
    numOr(point.biddableConversionsValue, 0),
    numOr(point.clicks, 0),
    numOr(point.costMicros, 0),
    numOr(point.impressions, 0),
    numOr(point.topSlotImpressions, 0),
    entity.biddingType || '',
    customer.currency,
    runDate
  ];
}

function numOr(v, fallback) {
  if (v == null || v === '') return fallback;
  var n = Number(v);
  return isNaN(n) ? fallback : n;
}

/* ========================== TRANSPORT ========================== */

/* executeInParallel can only hand back a string, so rows are flattened with
   separators that cannot appear in Ads data and rebuilt in the callback. */
function encodeRows(rows) {
  return rows.map(function (r) {
    return r.map(function (c) { return c == null ? '' : String(c); }).join(CELL_SEPARATOR);
  }).join(ROW_SEPARATOR);
}

/**
 * Split one child's return value into its three datasets. A payload carrying fewer group
 * separators than that (a simulations-only return, or a child that produced one dataset
 * but not the others) leaves the missing sides undefined, and every one of them decodes
 * to [] without special-casing.
 */
function decodePayload(str) {
  var parts = String(str == null ? '' : str).split(DATASET_SEPARATOR);
  return {
    raw: decodeRows(parts[0] || '', HEADERS.length, isSimNumericCol),
    shares: decodeRows(parts[1] || '', SHARE_HEADERS.length, isShareNumericCol),
    actuals: decodeRows(parts[2] || '', ACTUAL_HEADERS.length, isActualNumericCol)
  };
}

/** Simulation columns that must come back as numbers: the point metrics and the current target. */
function isSimNumericCol(i) { return (i >= 6 && i <= 12) || i === 2; }

/** Impression-share columns. Campaign Id stays a string — it is an identifier, not a quantity. */
function isShareNumericCol(i) { return i >= SHARE_NUMERIC_FROM && i <= SHARE_NUMERIC_TO; }

/** Actuals metric columns. Bidding Strategy Id stays a string, for the same reason. */
function isActualNumericCol(i) { return i >= ACTUAL_NUMERIC_FROM && i <= ACTUAL_NUMERIC_TO; }

/**
 * Rebuild one dataset. `width` is the tab's column count and `isNumeric(i)` says
 * which columns to restore to numbers so the sheet sorts and sums them.
 */
function decodeRows(str, width, isNumeric) {
  if (!str) return [];
  return str.split(ROW_SEPARATOR).filter(function (s) { return s.length > 0; })
    .map(function (line) {
      var cells = line.split(CELL_SEPARATOR);
      /* Pad or trim to exactly the tab's width: one malformed row must not be
         able to abort the whole setValues() write. */
      while (cells.length < width) cells.push('');
      if (cells.length > width) cells.length = width;
      return cells.map(function (c, i) {
        if (!isNumeric(i)) return c;
        var n = Number(c);
        return c !== '' && !isNaN(n) ? n : c;
      });
    });
}

/* ========================== SHEET WRITER ========================== */

/** Callback: runs once in the MCC after every account has reported. */
function writeSnapshot(results) {
  var allRows = [];
  var allShares = [];
  var allActuals = [];
  for (var i = 0; i < results.length; i++) {
    if (results[i].getStatus() !== 'OK') {
      Logger.log('Account ' + results[i].getCustomerId() + ' failed: ' + results[i].getError());
      continue;
    }
    var decoded = decodePayload(results[i].getReturnValue());
    allRows = allRows.concat(decoded.raw);
    allShares = allShares.concat(decoded.shares);
    allActuals = allActuals.concat(decoded.actuals);
  }

  if (!allRows.length && !allShares.length && !allActuals.length) {
    Logger.log('Nothing returned — no simulation points, impression shares or actuals. The sheet is untouched.');
    return;
  }

  /* Derive the run date from the rows themselves: recomputing it from the
     clock here can disagree with the children when a run straddles midnight.
     Simulations are the primary payload, so they name the date whenever present. */
  var runDate = allRows.length ? String(allRows[0][RUN_DATE_COL - 1])
    : allShares.length ? String(allShares[0][SHARE_RUN_DATE_COL - 1])
    : String(allActuals[0][ACTUAL_RUN_DATE_COL - 1]);

  var ss = SpreadsheetApp.openByUrl(CONFIG.SPREADSHEET_URL);
  var tz = ss.getSpreadsheetTimeZone();

  /* Each dataset is written independently: an account set that returned sims but
     no shares (or the reverse) still updates the tab it does have. */
  if (allRows.length) {
    writeTab(ss, CONFIG.SHEET_NAME, HEADERS, RUN_DATE_COL, allRows, runDate, tz);
  } else {
    Logger.log('No simulation points returned — the "' + CONFIG.SHEET_NAME + '" tab is untouched.');
  }
  if (allShares.length) {
    writeTab(ss, CONFIG.SHARES_SHEET_NAME, SHARE_HEADERS, SHARE_RUN_DATE_COL, allShares, runDate, tz);
  } else {
    Logger.log('No impression-share rows returned — the "' + CONFIG.SHARES_SHEET_NAME +
      '" tab is untouched and the dashboard falls back to flat class incrementality factors.');
  }
  if (allActuals.length) {
    writeTab(ss, CONFIG.ACTUALS_SHEET_NAME, ACTUAL_HEADERS, ACTUAL_RUN_DATE_COL, allActuals, runDate, tz);
  } else {
    Logger.log('No actual-performance rows returned — the "' + CONFIG.ACTUALS_SHEET_NAME +
      '" tab is untouched and the dashboard hides its actual-ROAS columns.');
  }
}

/**
 * Append one dated snapshot to one tab, applying the same four guarantees to
 * every tab: validated headers, a grid grown to fit, a same-day re-run that
 * replaces itself rather than duplicating, and pruning past the retention window.
 */
function writeTab(ss, name, headers, runDateCol, rows, runDate, tz) {
  var sheet = openSheet(ss, name);
  ensureHeaders(sheet, headers);
  removeRunDate(sheet, runDate, tz, runDateCol);   // makes a same-day re-run idempotent
  appendRows(sheet, rows, headers);
  pruneOldRows(sheet, runDate, tz, runDateCol);

  Logger.log('Appended ' + rows.length + ' row(s) for ' + runDate + ' to "' + name +
    '". That tab now holds ' + Math.max(0, sheet.getLastRow() - 1) + ' data row(s).');
}

function openSheet(ss, name) {
  var sheet = ss.getSheetByName(name);
  if (!sheet) {
    sheet = ss.insertSheet(name);
    Logger.log('Created tab "' + name + '".');
  }
  return sheet;
}

/** Write the header row if the tab is empty; refuse to write under a foreign one. */
function ensureHeaders(sheet, headers) {
  if (sheet.getMaxColumns() < headers.length) {   // a fresh tab can be narrower than the data
    sheet.insertColumnsAfter(sheet.getMaxColumns(), headers.length - sheet.getMaxColumns());
  }
  if (sheet.getLastRow() === 0) {
    sheet.getRange(1, 1, 1, headers.length).setValues([headers]).setFontWeight('bold');
    sheet.setFrozenRows(1);
    Logger.log('Bootstrapped header row on "' + sheet.getName() + '".');
    return;
  }
  var existing = sheet.getRange(1, 1, 1, headers.length).getValues()[0];
  for (var i = 0; i < headers.length; i++) {
    if (String(existing[i]).trim() !== headers[i]) {
      throw new Error('Header mismatch in "' + sheet.getName() + '" column ' + (i + 1) +
        ': expected "' + headers[i] + '", found "' + existing[i] +
        '". Rows are written positionally, so a mismatched header would corrupt ' +
        'every consumer - fix or clear the tab before running again.');
    }
  }
}

function appendRows(sheet, rows, headers) {
  var startRow = Math.max(sheet.getLastRow(), 1) + 1;
  var lastNeeded = startRow + rows.length - 1;
  if (sheet.getMaxRows() < lastNeeded) {   // getRange() never grows the grid itself
    sheet.insertRowsAfter(sheet.getMaxRows(), lastNeeded - sheet.getMaxRows());
  }
  sheet.getRange(startRow, 1, rows.length, headers.length).setValues(rows);
}

/** Delete rows whose Run Date equals runDate, so today's run replaces itself. */
function removeRunDate(sheet, runDate, tz, runDateCol) {
  deleteRowsWhere(sheet, function (value) { return normaliseDate(value, tz) === runDate; },
    'same-day re-run', runDateCol);
}

/** Delete rows whose Run Date is older than the retention window. */
function pruneOldRows(sheet, runDate, tz, runDateCol) {
  var cutoff = new Date(runDate + 'T00:00:00Z');
  cutoff.setUTCDate(cutoff.getUTCDate() - CONFIG.LOOKBACK_PRUNE_DAYS);
  var cutoffIso = Utilities.formatDate(cutoff, 'UTC', 'yyyy-MM-dd');
  deleteRowsWhere(sheet, function (value) {
    var d = normaliseDate(value, tz);
    return d !== '' && d < cutoffIso;
  }, 'older than ' + cutoffIso, runDateCol);
}

/**
 * Delete matching rows bottom-up in contiguous blocks: one deleteRows() call per
 * block instead of per row keeps a 90-day sheet well inside the execution limit.
 * `runDateCol` differs per tab, so it is always passed in.
 */
function deleteRowsWhere(sheet, predicate, reason, runDateCol) {
  var lastRow = sheet.getLastRow();
  if (lastRow < 2) return;

  var values = sheet.getRange(2, runDateCol, lastRow - 1, 1).getValues();
  var deleted = 0;
  var blockEnd = -1;

  for (var i = values.length - 1; i >= -1; i--) {
    var match = i >= 0 && predicate(values[i][0]);
    if (match && blockEnd < 0) blockEnd = i;
    if (!match && blockEnd >= 0) {
      var startSheetRow = i + 3;                       // +2 for header/offset, +1 past the non-match
      var count = blockEnd - i;
      sheet.deleteRows(startSheetRow, count);
      deleted += count;
      blockEnd = -1;
    }
  }
  if (deleted) Logger.log('Removed ' + deleted + ' row(s) from "' + sheet.getName() + '" (' + reason + ').');
}

/* ========================== DATES ========================== */

/* AdsApp.currentAccount() resolves to the manager account when called from MCC
   context (AdsManagerApp has no currentAccount() method — verified in prod). */
function todayInManagerTimezone() {
  return Utilities.formatDate(new Date(), AdsApp.currentAccount().getTimeZone(), 'yyyy-MM-dd');
}

/** Sheet cells may hold a Date object or a string; compare as yyyy-MM-dd.
    Sheets stores our written date strings as date-typed cells - instants at
    midnight in the SPREADSHEET's timezone - so they must be formatted back in
    that same timezone or the day shifts (e.g. to yesterday in UTC). */
function normaliseDate(value, tz) {
  if (value instanceof Date) return Utilities.formatDate(value, tz, 'yyyy-MM-dd');
  var s = String(value == null ? '' : value).trim();
  var m = s.match(/^(\d{4})-(\d{2})-(\d{2})/);
  return m ? m[0] : '';
}
