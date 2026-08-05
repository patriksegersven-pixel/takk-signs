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
 * auction headroom is actually left (see README, "Incrementality").
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
 *
 * Both tabs follow the same rules: append only, write only, idempotent per day,
 * self-pruning. A missing impression-share metric is written as a BLANK cell and
 * never as 0 — the dashboard treats 0/blank alike as "no data" and falls back to
 * the flat class factor, so a fabricated 0 would silently mean "no headroom".
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

/* executeInParallel caps each child's return string at ~100 KB. */
var MAX_RETURN_CHARS = 90000;

/* Parallel execution passes a single string argument, so config travels packed. */
var ARG_SEPARATOR = '||';
/* Written as escapes on purpose: this file gets copy-pasted into the Google Ads
   script editor, where a literal control character would not survive the trip. */
var ROW_SEPARATOR  = '\u001E';   // ASCII record separator - untypeable in Ads entity names
var CELL_SEPARATOR = '\u001F';   // ASCII unit separator
/* executeInParallel hands back exactly ONE string per account, so both datasets
   travel inside that single string: <sim rows> GS <share rows>. A payload with no
   group separator at all is a sims-only return, which still decodes correctly. */
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
    CONFIG.INCLUDE_PORTFOLIO ? '1' : '0', CONFIG.COLLECT_SHARES ? '1' : '0'].join(ARG_SEPARATOR);

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
 * receives them all together. Two datasets travel in that one string, separated
 * by DATASET_SEPARATOR: the simulation rows first (the primary payload), the
 * impression-share rows second.
 */
function collectSimulations(packedArgs) {
  var parts = String(packedArgs).split(ARG_SEPARATOR);
  var runDate = parts[0];
  var includeCampaigns = parts[1] === '1';
  var verbose = parts[2] === '1';
  var includePortfolio = parts[3] === '1';
  var collectShares = parts[4] === '1';

  var rows = [];
  var shareRows = [];
  try {
    if (includePortfolio) rows = rows.concat(portfolioSimulations(runDate, verbose));
    if (includeCampaigns) rows = rows.concat(campaignSimulations(runDate, verbose));

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
  } catch (e) {
    Logger.log('ERROR in ' + AdsApp.currentAccount().getName() + ': ' + e);
    throw e;   // surface as ERROR in the callback instead of a silent empty account
  }
  Logger.log(AdsApp.currentAccount().getName() + ': ' + rows.length + ' simulation point(s), ' +
    shareRows.length + ' impression-share row(s).');

  return packDatasets(encodeRows(rows), encodeRows(shareRows));
}

/**
 * Fit both encoded datasets inside the ~100 KB parallel-return cap, trimming on a
 * row boundary rather than letting a mid-row cut corrupt a snapshot. Shares go
 * first because they are an enrichment: losing them degrades brand and
 * private-label campaigns to their flat class factor, while losing simulation
 * rows loses the entire point of the run.
 */
function packDatasets(sims, shares) {
  var name = AdsApp.currentAccount().getName();
  var budget = MAX_RETURN_CHARS - DATASET_SEPARATOR.length;

  if (sims.length + shares.length > budget) {
    var room = budget - sims.length;
    var trimmed = room <= 0 ? '' : trimToRow(shares, room);
    Logger.log('WARNING ' + name + ': combined payload ' + (sims.length + shares.length) +
      ' chars exceeds the parallel-return cap; impression-share rows trimmed from ' +
      shares.length + ' to ' + trimmed.length + ' chars.');
    shares = trimmed;
  }
  if (sims.length > budget) {   // simulations alone overflow: trim them too, last resort
    var kept = trimToRow(sims, budget);
    Logger.log('WARNING ' + name + ': simulation payload ' + sims.length +
      ' chars still exceeds the cap after dropping shares; trimmed to ' + kept.length + ' chars.');
    sims = kept;
    shares = '';
  }
  return sims + DATASET_SEPARATOR + shares;
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
      String(camp.name || '').replace(/[,\r\n]+/g, ';'),
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

/** One spreadsheet row per simulation point, in HEADERS order. */
function buildRow(customer, entity, point, runDate) {
  return [
    customer.name,
    String(entity.name || '').replace(/[,\r\n\u001E\u001F]+/g, ';'),   // one line, no separator collisions
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
 * Split one child's return value into its two datasets. A string with no group
 * separator is a simulations-only payload (what an older child returns), and a
 * child that produced one dataset but not the other yields an empty side —
 * both decode to [] without special-casing.
 */
function decodePayload(str) {
  var s = String(str == null ? '' : str);
  var i = s.indexOf(DATASET_SEPARATOR);
  var simPart = i < 0 ? s : s.slice(0, i);
  var sharePart = i < 0 ? '' : s.slice(i + DATASET_SEPARATOR.length);
  return {
    raw: decodeRows(simPart, HEADERS.length, isSimNumericCol),
    shares: decodeRows(sharePart, SHARE_HEADERS.length, isShareNumericCol)
  };
}

/** Simulation columns that must come back as numbers: the point metrics and the current target. */
function isSimNumericCol(i) { return (i >= 6 && i <= 12) || i === 2; }

/** Impression-share columns. Campaign Id stays a string — it is an identifier, not a quantity. */
function isShareNumericCol(i) { return i >= SHARE_NUMERIC_FROM && i <= SHARE_NUMERIC_TO; }

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
  for (var i = 0; i < results.length; i++) {
    if (results[i].getStatus() !== 'OK') {
      Logger.log('Account ' + results[i].getCustomerId() + ' failed: ' + results[i].getError());
      continue;
    }
    var decoded = decodePayload(results[i].getReturnValue());
    allRows = allRows.concat(decoded.raw);
    allShares = allShares.concat(decoded.shares);
  }

  if (!allRows.length && !allShares.length) {
    Logger.log('Nothing returned — no simulation points and no impression shares. The sheet is untouched.');
    return;
  }

  /* Derive the run date from the rows themselves: recomputing it from the
     clock here can disagree with the children when a run straddles midnight.
     Simulations are the primary payload, so they name the date whenever present. */
  var runDate = allRows.length
    ? String(allRows[0][RUN_DATE_COL - 1])
    : String(allShares[0][SHARE_RUN_DATE_COL - 1]);

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
