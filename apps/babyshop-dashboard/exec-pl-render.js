/* ─────────────────────────────────────────────────────────────────────────────
 * exec-pl-render.js, the rendering half of the Executive P&L tab.
 *
 * Split out of babyshop-exec-pl.html only for size. It is loaded from that page
 * immediately after its inline block and reads the helpers that block publishes
 * on window.__execplHelpers, registering its own renderers on
 * window.__execplRender. Nothing here is reusable by another tab.
 *
 * THE RULE THAT SHAPES EVERY FUNCTION BELOW
 * -----------------------------------------
 * Posted, Projected and Forecast are three different things and are never
 * blended into one number:
 *
 *   Posted     what Finance has in the general ledger
 *   Projected  posted plus a fitted estimate, always carrying its posted share
 *   Forecast   Babyshop's own plan
 *
 * A projection exists only for the open month, only at group level, only down
 * to GP3, and never inside a cumulative window.
 *
 * NO `innerHTML +=` IN A ROW LOOP. Rows are pushed into an array and joined
 * once. The pattern this replaces is O(N squared) and froze the Inventory tab
 * for 60 seconds on production-scale data.
 * ───────────────────────────────────────────────────────────────────────────── */
(function () {
'use strict';

var H = window.__execplHelpers;
var R = (window.__execplRender = window.__execplRender || {});

/* The page's own helpers, re-exported here so this file reads normally. */
var esc = H.esc, sek = H.sek, signed = H.signed, msek = H.msek, pct = H.pct,
    pf = H.pf, el = H.el, lab = H.lab, chip = H.chip, dcell = H.dcell,
    agg = H.agg, pyAgg = H.pyAgg, fcFor = H.fcFor, wm = H.wm, pym = H.pym,
    rangeLabel = H.rangeLabel, isOpen = H.isOpen, pjOn = H.pjOn,
    projAgg = H.projAgg, pjLadder = H.pjLadder, pjPct = H.pjPct,
    bandPP = H.bandPP, skewTxt = H.skewTxt, skewRange = H.skewRange,
    gp1Observed = H.gp1Observed, est = H.est, estMonth = H.estMonth,
    THEIRS = H.THEIRS, THEIR_PCT = H.THEIR_PCT, ONE_OFF = H.ONE_OFF,
    SKEW = H.SKEW, PL = H.PL, MN = H.MN;

function D(){ return H.D(); }
function N(){ return H.N(); }
function S(){ return H.S(); }

/* Reason a rung is unavailable, in the reader's terms rather than the code's. */
var RSN = {
  geo:  'no country dimension',
  sup:  'not yet posted',
  open: 'open month enters posted-only'
};
function mstate(a){
  if (a.geo) return 'geo';
  if (a.allOpen) return 'sup';
  if (a.openM.length) return 'open';
  return 'ok';
}

/* ══ 1 · Notices ═════════════════════════════════════════════════════════════
 * Everything a reader needs in order not to misread the figures below, stated
 * before the figures rather than in a footnote after them.
 */
function renderNotices(){
  var d = D(), out = [];
  var fm = (d.forecast || {})._meta || {};
  var em = estMonth();
  var ob = gp1Observed();

  /* Forecast vintage, and the fact that even closed months are forecast. */
  var fy = [];
  for (var i = 1; i <= 12; i++) fy.push('2026-' + (i < 10 ? '0' : '') + i);
  var fyGross = H.fcLine('Gross Sales', fy), fyGp3 = H.fcLine('GP3', fy);
  out.push(
    '<div class="notice" style="background:var(--surf-2);color:var(--ink-2);border-color:var(--rule-2)">' +
    '<span style="color:var(--good-ink);font-weight:700">&#9679;</span><span>' +
    '<b style="color:var(--ink)">Every &ldquo;vs FC&rdquo; on this tab compares against Babyshop&rsquo;s own rolling forecast.</b> ' +
    esc(fm.vintage || 'rolling forecast') + (fm.exported ? ', exported ' + esc(fm.exported) : '') +
    ', ' + Object.keys((d.forecast || {}).lines || {}).length + ' P&amp;L lines &times; 12 months' +
    (fyGross ? ', FY gross ' + msek(fyGross) + ' M and FY GP3 ' + msek(fyGp3) + ' M' : '') + '. ' +
    '<b style="color:var(--ink)">All twelve months are labelled <em>prognos</em></b>, so even closed months are forecast rather than restated actuals: ' +
    'a &ldquo;vs FC&rdquo; on January is actual against forecast, not actual against actual. ' +
    'The forecast is <b>global only</b> (no market, shop or channel key exists in it), so selecting a single market ' +
    'withdraws every comparison rather than splitting one.' +
    '</span></div>');

  /* The open month, and what is and is not published for it. */
  if (em && isOpen(em.month)) {
    var bp = bandPP();
    out.push(
      '<div class="notice" style="background:var(--proj-bg);color:var(--ink-2);border-color:var(--proj-ln)">' +
      '<span style="color:var(--proj-ink);font-weight:700">&#9679;</span><span>' +
      '<b style="color:var(--ink)">' + esc(lab(em.month)) + ' is the open month and publishes a projection below GP1.</b> ' +
      'It carries a projected <b>GP2 of ' + pf(pjPct('gp2')) + ' and GP3 of ' + pf(pjPct('gp3')) + '</b> from a cost estimator ' +
      'fitted per line and backtested over six closed months, led by the margin percentage rather than the SEK figure ' +
      'because revenue and cost errors partly cancel in a ratio and do not in an absolute. ' +
      '<b style="color:var(--ink)">Three states stay visually separate and are never blended:</b> ' +
      '<span class="st st-bk">Posted</span> what is in the general ledger, ' +
      '<span class="st st-pj">Projected</span> posted plus estimate with the posted share always shown, and ' +
      '<span class="st st-fc">Forecast</span> Babyshop&rsquo;s own plan. ' +
      'Nothing below GP1 has posted, so those rungs are <b>100% estimated</b>, and the band is deliberately skewed ' +
      '<b>' + skewTxt() + '</b> rather than the symmetric &plusmn;' + bp.gp2_pp.toFixed(1) + ' pp the backtest gives at day ' +
      (em.as_of_day || '–') + '. <b style="color:var(--ink)">EBITDA stays suppressed for the open month</b>: the estimator is fitted on ' +
      'direct costs and stops at GP3. Multi-month windows stay posted-only, so ' + esc(lab(em.month)) +
      ' enters YTD and L12M at what has actually posted.' +
      '</span></div>');
  }

  /* A one-off large enough to dominate the headline has to be named up front. */
  var ytd = wm('YTD'), a = agg(ytd, 'ALL');
  if (ytd.indexOf(ONE_OFF.month) >= 0 && a.ebitda && Math.abs(ONE_OFF.sek / a.ebitda) > 0.25) {
    out.push(
      '<div class="notice"><span>&#9888;</span><span>' +
      '<b>YTD EBITDA is dominated by a one-off.</b> A single ' + esc(ONE_OFF.what) + ' posted to GL ' + esc(ONE_OFF.gl) +
      ' in ' + esc(lab(ONE_OFF.month)) + ' carries <b>' + sek(ONE_OFF.sek) + '</b> of income. It sits inside EBITDA but outside the ' +
      'GP3 ladder, in <em>Other operating items</em>. YTD EBITDA is ' + sek(a.ebitda) + '; strip the settlement and ' +
      '<b>underlying EBITDA is ' + sek(a.ebitda - ONE_OFF.sek) + '</b>. The forecast contains no such line, so any read of ' +
      'EBITDA against forecast that does not name this item is misleading.' +
      '</span></div>');
  }

  el('noticeTop').innerHTML = out.join('');

  /* Market selection withdraws two whole classes of figure. */
  var n = el('mktNotice');
  if (S().mkt !== 'ALL') {
    var gap = (d.checks || {}).item_ledger_vs_gl_cogs || {};
    var ms = wm(S().period);
    var gp = ms.map(function (m) { return gap[m] && gap[m].pct; })
               .filter(function (v) { return v != null; });
    n.innerHTML =
      '<div class="notice"><span>&#9888;</span><span>' +
      '<b>' + esc(S().mkt) + ' selected. Two things stop being available.</b> ' +
      'Net Shipping, Fulfillment, Transaction Fees and the whole overhead stack post to the general ledger at carrier, ' +
      'settlement and company granularity with <b>no country dimension at any grain</b>, so the ladder stops at contribution ' +
      'after marketing and GP2, GP3 and EBITDA cannot be built. ' +
      'And the forecast is global only, so <b>every forecast comparison is withdrawn</b> rather than split. ' +
      'COGS switches to the item-ledger basis and marketing to de-duplicated Funnel spend, the only two that carry a country' +
      (gp.length ? '; the item ledger runs ' + (gp.length === 1 ? (gp[0] > 0 ? '+' : '') + gp[0].toFixed(1) + '%'
        : (Math.min.apply(null, gp)).toFixed(1) + '% to ' + (Math.max.apply(null, gp) > 0 ? '+' : '') + (Math.max.apply(null, gp)).toFixed(1) + '%') +
        ' against the general ledger over this window on posting cut-off, which is why market rows do not foot to the ladder' : '') +
      '.</span></div>';
  } else {
    n.innerHTML = '';
  }
}

/* ══ 2 · Pacing tiles ════════════════════════════════════════════════════════ */
function renderTiles(){
  var s = S(), ms = wm(s.period), a = agg(ms, s.mkt);
  var b = fcFor(ms, s.mkt), p = (s.cmp === 'PY') ? pyAgg(ms, s.mkt) : null;
  var st = mstate(a), PJ = pjOn(ms, s.mkt), pa = PJ ? projAgg() : null;
  var refn = (s.cmp === 'PY') ? 'last year' : 'forecast';
  var t = [];

  function box(lbl, val, unit, vs, ch, vt, mark){
    var m = mark === 'D'
      ? ' <span class="mk mk-d" title="Directional. Composition is close but not identical between our GL grouping and Babyshop&#39;s forecast line.">D</span>'
      : mark === 'O'
      ? ' <span class="mk mk-d" title="Open window. It contains the open month, whose carrier, 3PL and marketing invoices have not posted.">O</span>'
      : mark === 'C'
      ? ' <span class="mk mk-c" title="Posted-to-date GP1% reads high intra-month: cost posts behind the revenue it belongs to, so the ratio is distorted until the month closes. Read the projected figure below.">!</span>'
      : '';
    return '<div class="tile"><div class="lab">' + lbl + m + '</div>' +
           '<div class="val">' + val + '<span class="u">' + unit + '</span></div>' +
           '<div class="vs">' + vs + '</div>' +
           (ch ? '<div style="margin-top:7px">' + ch + ' <span class="vs" style="margin-left:5px">' + vt + '</span></div>' : '') +
           '</div>';
  }
  function sup(name, why){
    return '<div class="tile"><div class="lab">' + name +
      ' <span class="mk mk-c" title="Not computable for this selection.">&#10005;</span></div>' +
      '<div class="val" style="font-size:17px;color:var(--ink-3);font-weight:500">Not available</div>' +
      '<div class="vs" style="white-space:normal;line-height:1.45;margin-top:4px;font-family:var(--font)">' + why + '</div></div>';
  }
  function pjbox(name, pctv, sekv, bpp, fcpct){
    var rg = skewRange(pctv);
    return '<div class="tile" style="border-color:var(--proj-ln)"><div class="lab">' + name +
      ' <span class="st st-pj">Projected</span></div>' +
      '<div class="val">' + pctv.toFixed(1) + '<span class="u">% of net sales</span></div>' +
      '<div class="vs">' + sek(sekv) + ' SEK, secondary</div>' +
      '<div style="margin-top:7px"><span class="delta" style="color:var(--proj-ink);background:var(--proj-bg)">' +
      skewTxt() + '</span> <span class="vs" style="margin-left:5px">' + msek(rg[0]) + 'M to ' + msek(rg[1]) + 'M</span></div>' +
      '<div class="vs" style="margin-top:4px">forecast ' + pf(fcpct) + ' &middot; backtest band &plusmn;' + bpp.toFixed(1) + ' pp</div></div>';
  }

  var ref = p || b;
  var rn = ref ? ref.net : null;
  t.push(box('Net Sales &middot; ' + esc(PL[s.period]), msek(a.net), 'M SEK',
    PJ ? ('posted &middot; projected ' + msek(pa.net) + 'M')
       : (rn ? ('vs ' + refn + ' ' + sek(rn)) : 'no ' + refn + ' for this window'),
    rn ? chip(a.net, rn, true) : '',
    rn ? ((a.net > rn ? '+' : '−') + msek(Math.abs(a.net - rn)) + 'M') : ''));

  var r1 = ref ? ref.gp1 : null;
  t.push(box('GP1 &middot; ' + pf(pct(a.gp1, a.net)) + (PJ ? ' posted' : ''), msek(a.gp1), 'M SEK',
    PJ ? ('posted &middot; projected ' + pf(pjPct('gp1')) + ', ' + msek(pa.gp1) + 'M')
       : (r1 ? ('vs ' + refn + ' ' + sek(r1)) : 'no ' + refn + ' for this window'),
    r1 ? chip(a.gp1, r1, true) : '',
    r1 ? ((a.gp1 > r1 ? '+' : '−') + msek(Math.abs(a.gp1 - r1)) + 'M') : '', PJ ? 'C' : null));

  if (PJ) {
    var bp = bandPP(), fc = fcFor([estMonth().month], 'ALL');
    t.push(pjbox('GP2', pjPct('gp2'), pa.gp2, bp.gp2_pp, fc ? pct(fc.gp2, fc.net) : null));
    t.push(pjbox('GP3', pjPct('gp3'), pa.gp3, bp.gp3_pp, fc ? pct(fc.gp3, fc.net) : null));
    t.push(sup('EBITDA', 'The estimator covers the ladder down to GP3. Overhead and other operating items are not modelled, ' +
      'so EBITDA stays suppressed for the open month rather than being half-estimated.'));
    t.push(box('Marketing &middot; ' + pf(pct(-a.mktg, a.net)) + ' of net', msek(-a.mktg), 'M SEK',
      'posted GL &middot; projected ' + msek(-pa.mktg) + 'M', ''));
    el('tiles').innerHTML = t.join('');
    return;
  }

  if (st === 'ok' || st === 'open') {
    var r3 = ref ? ref.gp3 : null;
    t.push(box('GP3 &middot; ' + pf(pct(a.gp3, a.net)) + (st === 'open' ? ' &middot; open' : ''), msek(a.gp3), 'M SEK',
      r3 ? ('vs ' + refn + ' ' + sek(r3)) : 'no ' + refn + ' for this window',
      r3 ? chip(a.gp3, r3, true) : '',
      r3 ? ((a.gp3 > r3 ? '+' : '−') + msek(Math.abs(a.gp3 - r3)) + 'M') : '', st === 'open' ? 'O' : null));
    var re = ref ? ref.ebitda : null;
    t.push(box('EBITDA &middot; ' + pf(pct(a.ebitda, a.net)) + (st === 'open' ? ' &middot; open' : ''), msek(a.ebitda), 'M SEK',
      re ? ('vs ' + refn + ' ' + sek(re)) : 'no ' + refn + ' for this window',
      re ? chip(a.ebitda, re, true) : '',
      re ? ((a.ebitda > re ? '+' : '−') + msek(Math.abs(a.ebitda - re)) + 'M') : '', st === 'open' ? 'O' : null));
  } else if (st === 'geo') {
    t.push(sup('GP3', 'Shipping, fulfilment, transaction fees and overhead post to the general ledger with no country dimension, ' +
      'so GP3 and EBITDA cannot be built for one market.'));
    t.push(sup('EBITDA', 'Overhead posts at company level. There is no market key to split it on.'));
  } else {
    t.push(sup('GP3', 'The open month&rsquo;s carrier, 3PL and marketing invoices have not posted. A figure here would overstate the result.'));
    t.push(sup('EBITDA', 'Same reason, and the estimator stops at GP3.'));
  }

  t.push(box(a.geo ? 'Marketing &middot; Funnel de-duplicated' : ('Marketing &middot; ' + pf(pct(-a.mktg, a.net)) + ' of net'),
    msek(-a.mktg), 'M SEK',
    a.geo ? 'de-duplicated Funnel &middot; no market forecast' : 'GL 5911&ndash;5990 media',
    (!a.geo && b && s.cmp === 'FC') ? chip(a.mktg, b.mktg, true) : '',
    (!a.geo && b && s.cmp === 'FC') ? ((a.mktg > b.mktg ? '+' : '−') + msek(Math.abs(a.mktg - b.mktg)) + 'M') : ''));

  el('tiles').innerHTML = t.join('');
}

R.notices = renderNotices;
R.tiles = renderTiles;
R.mstate = mstate;
R.RSN = RSN;
})();

/* ═══════════════════════════════════════════════════════════════════════════
 * Part 2: the open-month projection card, and the live day-grain cards.
 * ═══════════════════════════════════════════════════════════════════════════ */
(function () {
'use strict';
var H = window.__execplHelpers;
var R = window.__execplRender;
var esc = H.esc, sek = H.sek, msek = H.msek, pct = H.pct, pf = H.pf, el = H.el,
    lab = H.lab, dayName = H.dayName, chip = H.chip, agg = H.agg, wm = H.wm,
    fcFor = H.fcFor, pjOn = H.pjOn, projAgg = H.projAgg, pjLadder = H.pjLadder,
    pjPct = H.pjPct, bandPP = H.bandPP, skewTxt = H.skewTxt,
    skewRange = H.skewRange, gp1Observed = H.gp1Observed, est = H.est,
    estMonth = H.estMonth, SKEW = H.SKEW, isOpen = H.isOpen;
function D(){ return H.D(); } function N(){ return H.N(); } function S(){ return H.S(); }

/* A posted-vs-estimated bar. Wherever a projected figure appears, the share of
 * it that has actually posted appears beside it, so a projection can never be
 * mistaken for a posted number. */
function splitBar(bk, tot){
  var s = tot ? Math.max(0, Math.min(100, Math.abs(bk) / Math.abs(tot) * 100)) : 0;
  return '<div class="split" role="img" aria-label="' + s.toFixed(0) + ' per cent posted">' +
    '<i class="bk" style="width:' + s.toFixed(1) + '%"></i>' +
    '<i class="es" style="width:' + (100 - s).toFixed(1) + '%"></i></div>' +
    '<div class="split-l"><span>' + s.toFixed(0) + '% posted</span>' +
    '<span>' + (100 - s).toFixed(0) + '% estimated</span></div>';
}

function renderProjection(){
  var card = el('projCard'), s = S(), ms = wm(s.period), em = estMonth();
  var on = !!(em && s.mkt === 'ALL' && ms.indexOf(em.month) >= 0 && isOpen(em.month));
  card.hidden = !on;
  if (!on) return;

  var P = pjLadder(), pa = projAgg(), posted = agg([em.month], 'ALL');
  var fc = fcFor([em.month], 'ALL'), bp = bandPP(), ob = gp1Observed();
  var e = est();

  function metric(name, pctv, sekv, bpp, fcv){
    var rg = skewRange(pctv);
    return '<div class="pj-m"><div class="lab">' + name + ' <span class="st st-pj">Projected</span></div>' +
      '<div class="big">' + pf(pctv) + '<span class="bnd">' + skewTxt() + '</span></div>' +
      '<div class="sekline">' + sek(sekv) + ' SEK, secondary</div>' +
      '<div class="rng">range ' + msek(rg[0]) + 'M to ' + msek(rg[1]) + 'M &middot; backtest band at day ' +
      (em.as_of_day || '–') + ' &plusmn;' + bpp.toFixed(1) + ' pp</div>' +
      '<div class="rng" style="margin-top:9px;color:var(--proj-ink)">every deduction below GP1 is <b>0% posted</b>, ' +
      'so this rung is carried entirely on the estimate</div>' +
      (fcv != null ? '<div class="rng" style="margin-top:6px">forecast ' + pf(pct(fcv, fc.net)) +
        ' &middot; ' + sek(fcv) + '</div>' : '') + '</div>';
  }

  var h = [];
  h.push('<div class="pj-m" style="background:var(--surf-2);border-color:var(--rule)">' +
    '<div class="lab">Net Sales <span class="st st-pj">Projected</span></div>' +
    '<div class="big">' + msek(pa.net) + '<span class="bnd" style="color:var(--ink-3)">M SEK</span></div>' +
    '<div class="sekline">' + sek(posted.net) + ' posted to date</div>' +
    '<div style="margin-top:9px">' + splitBar(posted.net, pa.net) + '</div>' +
    (fc ? '<div class="rng" style="margin-top:6px">forecast ' + sek(fc.net) + '</div>' : '') + '</div>');

  var gp1High = pjPct('gp1') > ob.hi;
  h.push('<div class="pj-m" style="background:var(--surf-2);border-color:var(--rule)">' +
    '<div class="lab">GP1 <span class="st st-pj">Projected</span>' +
    (gp1High ? ' <span class="mk mk-c" title="Projected GP1 sits above every month observed this year.">!</span>' : '') +
    '</div>' +
    '<div class="big">' + pf(pjPct('gp1')) + '</div>' +
    '<div class="sekline">' + sek(pa.gp1) + ' SEK</div>' +
    '<div class="rng">' + (gp1High ? 'above the whole observed 2026 range, ' : 'within the observed 2026 range, ') +
    ob.lo.toFixed(1) + ' to ' + ob.hi.toFixed(1) + '%' +
    (ob.last != null ? ', last closed month ' + ob.last.toFixed(1) + '%' : '') + '</div>' +
    '<div style="margin-top:9px">' + splitBar(posted.gp1, pa.gp1) + '</div>' +
    (fc ? '<div class="rng" style="margin-top:6px">forecast ' + pf(pct(fc.gp1, fc.net)) + ' &middot; ' + sek(fc.gp1) + '</div>' : '') +
    '</div>');

  h.push(metric('GP2', pjPct('gp2'), pa.gp2, bp.gp2_pp, fc ? fc.gp2 : null));
  h.push(metric('GP3', pjPct('gp3'), pa.gp3, bp.gp3_pp, fc ? fc.gp3 : null));
  el('pjHead').innerHTML = h.join('');

  /* Why the percentage leads and why the band is skewed. Both are computed
   * against the live snapshot rather than asserted. */
  var adj = (D().components[em.month] || {}).cogs_adjustments;
  var adjHist = D().months.filter(function (m) { return !isOpen(m) && m.slice(0,4) === '2026'; })
    .map(function (m) { return (D().components[m] || {}).cogs_adjustments || 0; });
  var adjMean = adjHist.length ? adjHist.reduce(function (x,y){ return x+y; }, 0) / adjHist.length : 0;

  el('pjWhy').innerHTML =
    '<strong>The margin percentage is the number to read; the SEK figure is secondary.</strong> ' +
    'Revenue error and cost error partly cancel in a ratio and do not in an absolute: over-project revenue and the ' +
    'driver-scaled cost lines scale with it, so the margin is self-stabilising in a way the krona figure is not. ' +
    '<strong>The band is deliberately skewed, ' + skewTxt() + ' rather than symmetric &plusmn;' + bp.gp2_pp.toFixed(1) + ' pp.</strong> ' +
    'Two things push this month to the optimistic edge. ' +
    (gp1High
      ? ('Projected GP1 of ' + pf(pjPct('gp1')) + ' sits <em>above every month observed this year</em> (range ' +
         ob.lo.toFixed(1) + ' to ' + ob.hi.toFixed(1) + '%). ')
      : ('Projected GP1 of ' + pf(pjPct('gp1')) + ' sits inside the observed range, which removes one of the two reasons for the skew. ')) +
    'And the price, sample and stock-adjustment block (GL 4014 &middot; 4037 &middot; 4055) has posted <b>' +
    (adj < 0 ? '−' : '') + sek(Math.abs(adj)) + '</b> so far against a 2026 monthly mean of <b>' +
    (adjMean < 0 ? '−' : '+') + sek(Math.abs(adjMean)) + '</b>. The COGS rate embeds an average adjustment, so if the usual ' +
    'pattern reasserts before close, COGS rises and both margins fall. ' +
    'Practical read: <b>GP2 about ' + pf(pjPct('gp2')) + ' and GP3 about ' + pf(pjPct('gp3')) +
    '</b>, treated as the optimistic edge, and re-read once the month is genuinely closed.';

  renderBandChart();
  renderUnpredictable();

  el('pjTitle').innerHTML = esc(lab(em.month)) + ' projected, standing at day ' + (em.as_of_day || '–') + ' of the month';
  var src = (e || {}).source || {};
  el('pjSub').innerHTML = 'Posted plus estimate &middot; margin first, SEK second' +
    (src.bc_last_sync ? ' &middot; last BC sync ' + esc(src.bc_last_sync) : '');
}

/* The backtest band, by day of month. It narrows and then STOPS: at month end
 * the revenue side is fully known, so what is left is pure cost-rate error. A
 * band that visibly tightened to nothing as the month closed would be a lie. */
function renderBandChart(){
  var e = est(), bt = (e || {}).backtest || {}, em = estMonth();
  var rec = bt.recommended_band_pp || {}, pm = bt.per_month || [];
  if (!pm.length) { el('pjBand').innerHTML = ''; el('pjBandNote').innerHTML = ''; return; }

  /* Median absolute margin error across the backtest months, per checkpoint day. */
  var days = Object.keys(pm[0].by_day || {}).map(Number).sort(function (a,b){ return a-b; });
  function med(arr){
    var v = arr.slice().sort(function (a,b){ return a-b; });
    if (!v.length) return null;
    var i = Math.floor(v.length / 2);
    return v.length % 2 ? v[i] : (v[i-1] + v[i]) / 2;
  }
  var gp2err = [], gp3err = [];
  days.forEach(function (d) {
    var e2 = [], e3 = [];
    pm.forEach(function (m) {
      var bd = (m.by_day || {})[String(d)], act = m.actual_pct || {};
      if (!bd || act.gp2 == null) return;
      e2.push(Math.abs(bd.gp2_pct - act.gp2));
      e3.push(Math.abs(bd.gp3_pct - act.gp3));
    });
    gp2err.push(med(e2) || 0);
    gp3err.push(med(e3) || 0);
  });

  var W = 560, Ht = 210, PLx = 42, PRx = 118, PT = 16, PB = 44;
  var pw = W - PLx - PRx, ph = Ht - PT - PB, ymax = 8;
  function bx(d){ return PLx + (d - 1) / 30 * pw; }
  function by(v){ return PT + ph - (Math.min(v, ymax) / ymax * ph); }

  var o = ['<svg viewBox="0 0 ' + W + ' ' + Ht + '" width="100%" role="img" ' +
           'aria-label="Median margin error and published band by day of month">'];
  [0,2,4,6,8].forEach(function (g) {
    o.push('<line class="gridline" x1="' + PLx + '" y1="' + by(g).toFixed(1) + '" x2="' + (W-PRx) + '" y2="' + by(g).toFixed(1) + '"></line>' +
           '<text class="tick" x="' + (PLx-7) + '" y="' + (by(g)+3).toFixed(1) + '" text-anchor="end">' + g + '</text>');
  });

  /* published band as a step area */
  var pts = [];
  for (var d = 1; d <= 31; d++) {
    var v = 5.5;
    Object.keys(rec).forEach(function (k) {
      var p = k.split('-');
      if (d >= +p[0] && d <= +p[1]) v = rec[k].gp2_pp;
    });
    pts.push([bx(d), by(v)]);
  }
  var path = pts.map(function (q){ return q[0].toFixed(1) + ' ' + q[1].toFixed(1); }).join(' L ');
  o.push('<path d="M ' + path + ' L ' + bx(31).toFixed(1) + ' ' + by(0).toFixed(1) + ' L ' +
         bx(1).toFixed(1) + ' ' + by(0).toFixed(1) + ' Z" fill="var(--proj-bg)" stroke="none"></path>');
  o.push('<path d="M ' + path + '" fill="none" stroke="var(--proj)" stroke-width="1.6" stroke-dasharray="5 3"></path>');

  function series(vals, cls, dot, name){
    var p = days.map(function (dd, i){ return [bx(dd), by(vals[i])]; });
    var out = ['<path class="' + cls + '" d="M ' +
      p.map(function (q){ return q[0].toFixed(1) + ' ' + q[1].toFixed(1); }).join(' L ') + '"></path>'];
    p.forEach(function (q, i) {
      out.push('<circle cx="' + q[0].toFixed(1) + '" cy="' + q[1].toFixed(1) + '" r="3" class="' + dot + ' ring">' +
        '<title>day ' + days[i] + ': ' + vals[i].toFixed(1) + ' pp median ' + name + ' error</title></circle>');
    });
    return out.join('');
  }
  o.push(series(gp2err, 'ln-1', 'dot-1', 'GP2'));
  o.push(series(gp3err, 'ln-2', 'dot-2', 'GP3'));

  var floor = gp2err[gp2err.length - 1];
  o.push('<line x1="' + bx(days[days.length-2] || 25).toFixed(1) + '" y1="' + by(floor).toFixed(1) +
    '" x2="' + (W-PRx+10) + '" y2="' + by(floor).toFixed(1) + '" stroke="var(--crit)" stroke-width="1" stroke-dasharray="3 3"></line>');
  o.push('<text class="tick" x="' + (W-PRx+14) + '" y="' + (by(floor)-3).toFixed(1) +
    '" style="fill:var(--crit-ink);font-weight:700">floor ' + floor.toFixed(1) + ' pp</text>');
  ['at month end revenue is','fully known, so this is','pure cost-rate error'].forEach(function (t, i) {
    o.push('<text class="tick" x="' + (W-PRx+14) + '" y="' + (by(floor)+9+i*11).toFixed(1) +
      '" style="fill:var(--ink-3)">' + t + '</text>');
  });

  var today = (em || {}).as_of_day;
  if (today) {
    o.push('<line x1="' + bx(today).toFixed(1) + '" y1="' + PT + '" x2="' + bx(today).toFixed(1) + '" y2="' + (PT+ph) +
      '" stroke="var(--rule-2)" stroke-width="1"></line>' +
      '<text class="tick" x="' + (bx(today)+4).toFixed(1) + '" y="' + (PT+9) +
      '" style="fill:var(--ink-2);font-weight:700">day ' + today + '</text>');
  }
  o.push('<line class="axis" x1="' + PLx + '" y1="' + by(0).toFixed(1) + '" x2="' + (W-PRx) + '" y2="' + by(0).toFixed(1) + '"></line>');
  [1,5,10,15,20,25,31].forEach(function (d) {
    o.push('<text class="tickb" x="' + bx(d).toFixed(1) + '" y="' + (Ht-PB+16) + '" text-anchor="middle">' + d + '</text>');
  });
  o.push('<text class="tick" x="' + PLx + '" y="' + (Ht-PB+31) + '" style="fill:var(--ink-3)">day of month</text>');
  o.push('<text class="tick" x="' + (PLx-7) + '" y="' + (PT-4) + '" text-anchor="end" style="fill:var(--ink-3)">pp</text>');
  o.push('</svg>');

  el('pjBand').innerHTML = o.join('') +
    '<div class="legend" style="margin-top:7px;font-size:11px">' +
    '<span class="k"><span class="swl" style="background:var(--s1)"></span>GP2 median error</span>' +
    '<span class="k"><span class="swl" style="background:var(--s2)"></span>GP3 median error</span>' +
    '<span class="k"><span class="swl" style="background:var(--proj)"></span>published band</span></div>';

  el('pjBandNote').innerHTML =
    (bt.months ? esc(bt.months.length) + ' closed months, ' + esc(bt.months[0]) + ' to ' +
      esc(bt.months[bt.months.length-1]) + ', scored out of sample' : 'Scored out of sample') +
    ': rates, maturity curves and pacing profiles are refit from the closed months strictly before each backtest month. ' +
    '<strong>The band narrows and then stops.</strong> GP2 median error runs ' +
    gp2err.map(function (v, i){ return v.toFixed(1) + ' pp at day ' + days[i]; }).join(', ') + '. ' +
    'The per-day figures are <em>not monotone</em> and on six months the wiggles are sampling noise, so the published ' +
    'band is smoothed rather than read off literally, and it is drawn to a floor rather than to zero.';
}

/* Two lines are labelled rather than modelled. Fitting either would be fitting
 * noise, and the second is why the band has a floor. */
function renderUnpredictable(){
  var e = est(), lines = (e || {}).lines || {}, np = (e || {}).not_predictable || [];
  var em = estMonth(), parts = (em || {}).parts || {};
  var out = [];
  np.forEach(function (k) {
    var L = lines[k];
    if (!L) {
      if (k === 'gl_4055_inventory_adjustments') {
        var adj = (D().components[em.month] || {}).cogs_adjustments;
        out.push('<div class="u"><div class="t"><b>Inventory adjustments</b><span class="g">GL 4055</span></div>' +
          '<div class="f">posted ' + (adj < 0 ? '−' : '') + sek(Math.abs(adj)) + ' this month</div>' +
          '<p>No driver relationship at all. The variance is embedded in the COGS rate as an average and is not ' +
          'removable, which is the single reason the confidence band has a floor and does not close at month end.</p></div>');
      }
      return;
    }
    out.push('<div class="u"><div class="t"><b>' + esc(L.label || k) + '</b>' +
      '<span class="g">' + esc(L.driver_desc || '') + '</span></div>' +
      '<div class="f">MAPE ' + (L.backtest_mape_pct != null ? L.backtest_mape_pct + '%' : 'n/a') +
      ' &middot; median error ' + sek(L.backtest_median_abs_err_sek) + ' SEK</div>' +
      '<p>' + (L.rate === 0
        ? 'Not predictable. A nil forecast beat every fitted model, so nothing is added to what has actually posted (' +
          sek(parts[k]) + ' SEK).'
        : 'Carried, but imprecise. Read it as a level rather than a derived figure.') + '</p></div>');
  });
  out.push('<p style="margin:2px 0 0;font-size:11.5px;color:var(--ink-3);line-height:1.5">' +
    'These are shown as labels rather than fitted values. Fitting them would be fitting noise.</p>');
  el('pjUnpred').innerHTML = out.join('');
}

R.projection = renderProjection;
R.splitBar = splitBar;
})();

/* ═══════════════════════════════════════════════════════════════════════════
 * Part 3: day grain, on ORDER DATE, live from Norce.
 *
 * This is the one section of the tab that is not Business Central and not
 * posting date. It exists because the ladder above it cannot answer "how did we
 * trade today": a BC posting day is a shipment-and-invoicing batch, so a
 * Saturday posts almost nothing and a Monday carries the backlog.
 *
 * THE VOCABULARY IS THE CLIENT'S. "Booked" means when the purchase took place.
 * So these cards say ORDERED, the ladder says POSTED, and the page never uses
 * "booked" for either.
 *
 * THE DEFAULT IS THE LATEST COMPLETE DAY, named by its actual date. Today is
 * shown too but is explicitly provisional, and the only comparison it is
 * allowed to drive is against the SAME CLOCK TIME on the previous day, where
 * neither side is maturing relative to the other. A partial figure never
 * appears under a heading that implies a whole one, and no period is ever
 * seeded with a scaled placeholder.
 * ═══════════════════════════════════════════════════════════════════════════ */
(function () {
'use strict';
var H = window.__execplHelpers;
var R = window.__execplRender;
var esc = H.esc, sek = H.sek, msek = H.msek, pct = H.pct, pf = H.pf, el = H.el,
    dayName = H.dayName, chip = H.chip;
function D(){ return H.D(); } function N(){ return H.N(); }

function renderDay(){
  var n = N();
  if (!n) {
    el('dgCards').innerHTML =
      '<article class="dgc"><div class="stripe"></div><div class="bd">' +
      '<div class="dg-prim"><div class="k">Ordered</div>' +
      '<div class="v" style="font-size:17px;color:var(--ink-3);font-weight:500">Reading live from Norce&hellip;</div></div>' +
      '<p class="dg-note">The ladder above does not wait for this: it is a snapshot read, while this card makes ' +
      'about fifteen live API calls.</p></div></article>';
    el('dgFresh').innerHTML = '';
    el('dgSplit').innerHTML = '';
    el('dgNote').innerHTML = '';
    return;
  }

  if (!n.available) {
    el('dgCards').innerHTML =
      '<article class="dgc dead"><div class="stripe"></div>' +
      '<div class="hd"><h3>Ordered, unavailable</h3></div>' +
      '<div class="bd"><div class="dg-prim"><div class="k">Live Norce read</div>' +
      '<div class="v" style="font-size:19px;color:var(--ink-3);font-weight:500">No figure</div></div>' +
      '<p class="dg-note"><b>Stated reason:</b> ' + esc(n.reason || 'the live API did not answer') + '</p>' +
      '<p class="dg-note">Nothing is substituted. A day card with no live read shows no number rather than a stale ' +
      'one, a scaled one, or the posting-date figure from the ladder, which measures something else entirely.</p>' +
      '</div></article>';
    el('dgFresh').innerHTML = '<span><b>Basis</b> order date</span><span><b>Source</b> Norce API, live</span>' +
      '<span style="color:var(--crit-ink)">&#9679; unavailable</span>';
    el('dgSplit').innerHTML = '';
    el('dgNote').innerHTML = esc(n.basis_warning || '');
    el('dgTitle').innerHTML = 'Ordered';
    return;
  }

  var c = n.complete_day, p = n.partial_day, lfl = n.like_for_like;
  var out = [];

  /* ── Card A: the latest COMPLETE day. The default, and the headline. ─────── */
  out.push(
    '<article class="dgc"><div class="stripe"></div>' +
    '<div class="hd"><h3>Ordered &middot; ' + esc(dayName(c.date)) + '</h3>' +
    '<span class="dt">' + esc(c.date) + ' &middot; complete day</span></div>' +
    '<div class="bd">' +
    '<div class="dg-prim"><div class="k">Order intake, ex VAT, gross of returns ' +
    '<span class="st st-or">Order date</span></div>' +
    '<div class="v">' + sek(c.revenue_sek) + '<span class="u">SEK</span></div>' +
    '<div class="vs" style="font-family:var(--mono);font-size:11.5px;color:var(--ink-3);margin-top:5px">' +
    sek(c.orders) + ' orders &middot; AOV ' + sek(c.aov_sek) + ' SEK &middot; ' + sek(c.units) + ' units</div></div>' +
    '<div class="dg-sec"><span class="lb">of which freight</span><b>' + sek(c.freight_sek) + '</b>' +
    '<span class="lb">merchandise</span><b>' + sek(c.revenue_sek - c.freight_sek) + '</b></div>' +
    marketTable(c) +
    '</div></article>');

  /* ── Card B: today, provisional, and honest about it. ───────────────────── */
  var share = p.elapsed_share_of_prev_day;
  var dRev = (lfl && lfl.prev.revenue_sek) ? chip(lfl.today.revenue_sek, lfl.prev.revenue_sek, true) : '';
  var dOrd = (lfl && lfl.prev.orders) ? chip(lfl.today.orders, lfl.prev.orders, true) : '';
  out.push(
    '<article class="dgc prov"><div class="stripe"></div>' +
    '<div class="hd"><h3>Ordered so far &middot; ' + esc(dayName(p.date)) + '</h3>' +
    '<span class="dt">to ' + esc((n.fetched_at_local || '').slice(11)) + ' &middot; provisional</span></div>' +
    '<div class="bd">' +
    '<div class="dg-prim"><div class="k">Partial day ' +
    '<span class="mk mk-d" title="This day is still accruing. It is shown as a partial figure under a heading that says so, and it is never compared against a whole day.">P</span></div>' +
    '<div class="v">' + sek(p.revenue_sek) + '<span class="u">SEK so far</span></div>' +
    '<div class="vs" style="font-family:var(--mono);font-size:11.5px;color:var(--ink-3);margin-top:5px">' +
    sek(p.orders) + ' orders &middot; AOV ' + sek(p.aov_sek) + ' SEK</div></div>' +
    (share != null
      ? '<div><div class="matbar" role="img" aria-label="' + (share*100).toFixed(0) + ' per cent of the previous day had arrived by this time">' +
        '<i style="width:' + Math.max(0, Math.min(100, share*100)).toFixed(1) + '%"></i></div>' +
        '<div class="split-l"><span>' + (share*100).toFixed(0) + '% of ' + esc(c.date) + ' had arrived by this time</span></div></div>'
      : '') +
    '<div class="dg-sec" style="flex-direction:column;align-items:stretch;gap:6px">' +
    '<span class="lb">Like for like, both truncated at ' + esc(lfl ? lfl.cutoff_local : '') +
    ', the only comparison a maturing day may drive</span>' +
    '<div style="display:flex;gap:10px;align-items:baseline;flex-wrap:wrap">' + dRev +
    '<span style="font-family:var(--mono);font-size:11px;color:var(--ink-3)">revenue, vs ' +
    sek(lfl ? lfl.prev.revenue_sek : null) + ' at the same point on ' + esc(c.date) + '</span></div>' +
    '<div style="display:flex;gap:10px;align-items:baseline;flex-wrap:wrap">' + dOrd +
    '<span style="font-family:var(--mono);font-size:11px;color:var(--ink-3)">orders, vs ' +
    sek(lfl ? lfl.prev.orders : null) + '</span></div>' +
    '</div>' +
    '<p class="dg-note"><b>Not a forecast of the day.</b> Nothing here is grossed up to a full day. ' +
    'The completeness figure is a measured share of one named day, not a claim about this one.</p>' +
    '</div></article>');

  el('dgCards').innerHTML = out.join('');
  el('dgTitle').innerHTML = 'Ordered &middot; latest complete day and today so far';

  var dg = n.diagnostics || {};
  el('dgFresh').innerHTML =
    '<span><b>Basis</b> order date (the purchase), not posting date</span>' +
    '<span><b>Source</b> Norce API, queried live</span>' +
    '<span><b>Read at</b> ' + esc(n.fetched_at_local || '') + ' ' + esc((n.timezone || '').split('/').pop()) + '</span>' +
    '<span><b>Cost</b> ' + esc(dg.http_calls) + ' calls, ' + esc(dg.elapsed_s) + 's, ' + sek(dg.orders_read) + ' orders</span>' +
    '<span><b>Cached</b> ' + esc(n.cache_ttl_s) + 's</span>' +
    (dg.freight_vs_shipping_line_sek === 0
      ? '<span style="color:var(--good-ink)">&#9679; header freight ties to the shipping line exactly</span>'
      : '<span style="color:var(--warn-ink)">&#9679; header freight and shipping line differ by ' +
        sek(dg.freight_vs_shipping_line_sek) + ' SEK</span>');

  renderHourSplit(n);

  /* The sentence that stops someone diffing the two bases. */
  var lag = n.lag || {}, rec = n.reconciliation || {}, mea = n.measure || {};
  el('dgNote').innerHTML =
    '<b>These cards and the ladder above are two different measures, and must never be summed or differenced.</b> ' +
    esc(n.basis_warning || '') + ' ' +
    '<br><br><b>Order to posting lag.</b> ' + esc(lag.note || '') + ' ' +
    '<br><br><b>Reconciliation.</b> ' + esc(rec.note || '') + ' ' +
    '<br><br><b>What the measure is.</b> ' + esc(mea.scope || '') + ' ' +
    'Every order status except 6 is counted: orders are born at status 2 and move to 4 within about two days, so ' +
    'filtering to &ldquo;confirmed&rdquo; would zero this card every morning, and status 5 is not a cancellation either. ' +
    'Revenue is merchandise plus the header freight, because the ledger includes freight and the two tie exactly. ' +
    esc(mea.fx_note || '') + ' ' +
    '<br><br><b>No margin is shown here.</b> ' + esc(mea.no_margin || '');
}

/* Market split for the complete day, on the same ISO-2 axis as the ladder's
 * market table. Resolved from the delivery country, never from the application
 * key: four applications are single-country but the rest are not, and one of
 * them alone carries KR, KZ, JP and IL. */
function marketTable(c){
  var rows = Object.keys(c.by_country || {});
  if (!rows.length) return '';
  var tot = c.revenue_sek || 1;
  var out = ['<table class="dgl"><tbody>'];
  rows.slice(0, 7).forEach(function (k) {
    var r = c.by_country[k];
    out.push('<tr><td class="l">' + esc(k) + '</td><td>' + sek(r.revenue_sek) +
      ' <span style="color:var(--ink-3);font-weight:400">' + pf(r.revenue_sek / tot * 100) + '</span></td></tr>');
  });
  if (rows.length > 7) {
    var rest = rows.slice(7).reduce(function (s, k) { return s + c.by_country[k].revenue_sek; }, 0);
    out.push('<tr><td class="l">' + (rows.length - 7) + ' more countries</td><td>' + sek(rest) + '</td></tr>');
  }
  out.push('<tr class="s"><td class="l">All markets</td><td>' + sek(c.revenue_sek) + '</td></tr>');
  out.push('</tbody></table>');
  return out.join('');
}

/* Cumulative intake by hour, both days on one axis. This is what makes the
 * partial figure readable: the shape of a day is visible, so a morning number
 * does not read as a catastrophe or an evening one as a record. */
function renderHourSplit(n){
  var c = n.complete_day, p = n.partial_day;
  if (!c || !c.by_hour) { el('dgSplit').innerHTML = ''; return; }
  function cum(a){ var t = 0; return a.map(function (v) { t += v; return t; }); }
  var cc = cum(c.by_hour), pp = cum(p.by_hour);
  var mx = Math.max(cc[23], pp[23], 1);
  var W = 880, Ht = 130, PLx = 44, PRx = 12, PT = 14, PB = 26;
  var pw = W - PLx - PRx, ph = Ht - PT - PB;
  function x(h){ return PLx + h / 23 * pw; }
  function y(v){ return PT + ph - (v / mx * ph); }

  var o = ['<svg class="spark" viewBox="0 0 ' + W + ' ' + Ht + '" width="100%" role="img" ' +
    'aria-label="Cumulative order intake by hour, the latest complete day and today so far">'];
  [0, 0.5, 1].forEach(function (f) {
    o.push('<line class="gridline" x1="' + PLx + '" y1="' + y(mx*f).toFixed(1) + '" x2="' + (W-PRx) + '" y2="' + y(mx*f).toFixed(1) + '"></line>' +
      '<text class="tick" x="' + (PLx-7) + '" y="' + (y(mx*f)+3).toFixed(1) + '" text-anchor="end">' + msek(mx*f) + 'M</text>');
  });
  function line(vals, cls, upto){
    var pts = [];
    for (var h = 0; h <= (upto == null ? 23 : upto); h++) pts.push(x(h).toFixed(1) + ' ' + y(vals[h]).toFixed(1));
    return '<path class="' + cls + '" d="M ' + pts.join(' L ') + '"></path>';
  }
  var nowH = +((n.fetched_at_local || '00:00').slice(11, 13) || 0);
  o.push(line(cc, 'ln-3'));
  o.push(line(pp, 'ln-1', nowH));
  o.push('<circle cx="' + x(nowH).toFixed(1) + '" cy="' + y(pp[nowH]).toFixed(1) + '" r="4" class="dot-1 ring"><title>' +
    esc(p.date) + ' to ' + nowH + ':00, ' + sek(pp[nowH]) + ' SEK</title></circle>');
  o.push('<line class="axis" x1="' + PLx + '" y1="' + y(0).toFixed(1) + '" x2="' + (W-PRx) + '" y2="' + y(0).toFixed(1) + '"></line>');
  [0,3,6,9,12,15,18,21,23].forEach(function (h) {
    o.push('<text class="tickb" x="' + x(h).toFixed(1) + '" y="' + (Ht-PB+15) + '" text-anchor="middle">' +
      (h < 10 ? '0' : '') + h + ':00</text>');
  });
  o.push('</svg>');

  el('dgSplit').innerHTML = o.join('') +
    '<div class="legend" style="margin-top:7px;font-size:11px">' +
    '<span class="k"><span class="swl" style="background:var(--s3)"></span>' + esc(c.date) + ', complete</span>' +
    '<span class="k"><span class="swl" style="background:var(--s1)"></span>' + esc(p.date) + ', to ' +
    esc((n.fetched_at_local || '').slice(11)) + '</span>' +
    '<span style="color:var(--ink-3)">Cumulative intake through the day, local time. The partial line stops where the day has.</span></div>';
}

R.day = renderDay;
})();

/* ═══════════════════════════════════════════════════════════════════════════
 * Part 4: the scorecard band and the ladder.
 * ═══════════════════════════════════════════════════════════════════════════ */
(function () {
'use strict';
var H = window.__execplHelpers;
var R = window.__execplRender;
var esc = H.esc, sek = H.sek, msek = H.msek, pct = H.pct, pf = H.pf, el = H.el,
    lab = H.lab, chip = H.chip, dcell = H.dcell, agg = H.agg, pyAgg = H.pyAgg,
    fcFor = H.fcFor, wm = H.wm, pjOn = H.pjOn, projAgg = H.projAgg,
    pjPct = H.pjPct, bandPP = H.bandPP, skewTxt = H.skewTxt, estMonth = H.estMonth,
    isOpen = H.isOpen;
function D(){ return H.D(); } function S(){ return H.S(); }
var mstate = R.mstate, RSN = R.RSN, splitBar = R.splitBar;

/* ══ Scorecard ═══════════════════════════════════════════════════════════════
 * The same ladder read across four windows at once, so a month is never read
 * on its own. The open month's own column may be projected; the cumulative
 * columns never are.
 */
var SCW = [['LASTCLOSED','Last closed month'], ['MTD','Open month to date'],
           ['YTD','Year to date'], ['L12M','Last 12 months']];

function renderScore(){
  var s = S();
  var cols = SCW.map(function (w) {
    var ms = wm(w[0]);
    return { k:w[0], n:w[1], ms:ms, a:agg(ms, s.mkt), b:fcFor(ms, s.mkt),
             pj: pjOn(ms, s.mkt) ? projAgg() : null };
  }).filter(function (c) { return c.ms.length; });

  var head = ['<thead><tr><th class="l">Metric</th>'];
  cols.forEach(function (c) {
    var hot = (c.k === s.period) ? ' style="color:var(--ink)"' : '';
    var w = c.b ? '' : (s.mkt !== 'ALL' ? ' · no market forecast' : ' · no forecast for this window');
    head.push('<th' + hot + '>' + esc(c.n) + '<small>' + esc(c.ms.length === 1 ? lab(c.ms[0])
      : lab(c.ms[0]) + ' – ' + lab(c.ms[c.ms.length-1])) + esc(w) + '</small></th>');
  });
  head.push('</tr></thead>');

  var body = ['<tbody>'];
  function line(name, fn, cls){
    var cells = cols.map(fn);
    body.push('<tr' + (cls ? ' class="' + cls + '"' : '') + '><td class="l">' + name + '</td>' + cells.join('') + '</tr>');
  }
  function cell(v, p, ch, st){
    return '<td class="' + (st === 'open' ? 'open' : '') + '"><div class="m-v">' + (v == null ? '–' : (v < 0 ? '−' : '') + sek(Math.abs(v))) + '</div>' +
      (p != null ? '<div class="m-p">' + pf(p) + ' of net</div>' : '') +
      (ch ? '<div class="m-d">' + ch + '</div>' : '') +
      (st === 'open' ? '<div class="why">' + RSN.open + '</div>' : '') + '</td>';
  }
  function supc(why){
    return '<td class="sup"><div class="m-v">Not available</div><div class="why">' + esc(why) + '</div></td>';
  }
  function pjcell(pctv, sekv, fcv){
    return '<td style="background:var(--proj-bg)"><div class="m-v" style="color:var(--proj-ink)">' + pf(pctv) + '</div>' +
      '<div class="m-p">' + sek(sekv) + ' SEK</div>' +
      '<div class="m-d"><span class="st st-pj">Projected</span></div>' +
      '<div class="why">' + skewTxt() + ' skewed band' + (fcv != null ? ' &middot; forecast ' + pf(fcv) : '') + '</div></td>';
  }

  line('Net Sales', function (c) {
    return cell(c.a.net, null, c.b ? chip(c.a.net, c.b.net, true) : null, mstate(c.a) === 'open' ? 'open' : 'ok'); });
  line('GP1', function (c) {
    return cell(c.a.gp1, pct(c.a.gp1, c.a.net), c.b ? chip(c.a.gp1, c.b.gp1, true) : null,
      mstate(c.a) === 'open' ? 'open' : 'ok'); }, 'grp');

  line('GP2 <span class="mk mk-p" title="Projected for the open month from the fitted cost estimator. Suppressed for a single market, which has no logistics dimension.">P</span>',
    function (c) {
      var st = mstate(c.a);
      if (c.pj) return pjcell(pjPct('gp2'), c.pj.gp2, c.b ? pct(c.b.gp2, c.b.net) : null);
      if (st === 'geo') return supc(RSN.geo);
      if (st === 'sup') return supc(RSN.sup);
      return cell(c.a.gp2, pct(c.a.gp2, c.a.net), c.b ? chip(c.a.gp2, c.b.gp2, true) : null, st);
    });
  line('GP3 <span class="mk mk-p" title="Projected for the open month from the fitted cost estimator. Suppressed for a single market.">P</span>',
    function (c) {
      var st = mstate(c.a);
      if (c.pj) return pjcell(pjPct('gp3'), c.pj.gp3, c.b ? pct(c.b.gp3, c.b.net) : null);
      if (st === 'geo') return supc(RSN.geo);
      if (st === 'sup') return supc(RSN.sup);
      return cell(c.a.gp3, pct(c.a.gp3, c.a.net), c.b ? chip(c.a.gp3, c.b.gp3, true) : null, st);
    });
  line('Total Overhead', function (c) {
    var st = mstate(c.a);
    if (st === 'geo') return supc('overhead posts at company level');
    if (st === 'sup') return supc(RSN.sup);
    return cell(c.a.toh, pct(-c.a.toh, c.a.net), c.b ? chip(-c.a.toh, -c.b.toh, false) : null, st);
  }, 'grp');
  line('EBITDA <span class="mk mk-d" title="Unavailable whenever the month is open or a single market is selected. The estimator stops at GP3.">D</span>',
    function (c) {
      var st = mstate(c.a);
      if (c.pj) return supc('the estimator stops at GP3; overhead is not modelled');
      if (st === 'geo') return supc(RSN.geo);
      if (st === 'sup') return supc(RSN.sup);
      return cell(c.a.ebitda, pct(c.a.ebitda, c.a.net), c.b ? chip(c.a.ebitda, c.b.ebitda, true) : null, st);
    });
  line('Return rate', function (c) {
    var r = pct(-c.a.ret, c.a.gross);
    return '<td><div class="m-v">' + pf(r) + '</div><div class="m-p">of gross sales</div>' +
      (c.b ? '<div class="m-d">' + chip(r, pct(-c.b.ret, c.b.gross), false) + '</div>' : '') + '</td>';
  }, 'grp');
  line('Marketing &middot; % of net', function (c) {
    return cell(c.a.mktg, pct(-c.a.mktg, c.a.net), c.b ? chip(c.a.mktg, c.b.mktg, true) : null, 'ok'); });
  body.push('</tbody>');

  el('scTable').innerHTML = head.join('') + body.join('');
  el('scTitle').innerHTML = s.mkt === 'ALL'
    ? 'Finance&rsquo;s margin ladder by time window'
    : 'Margin ladder by time window &middot; ' + esc(s.mkt) + ' only';
}

/* ══ The ladder ══════════════════════════════════════════════════════════════
 * Finance's own management-report format, their line names in their order, each
 * expandable to the named general-ledger accounts behind it and each of those
 * against its own forecast sub-line.
 */
var open = {};

function OHH(){
  return [
    'GL 7210 salaries and the related holiday, pension and social-charge accounts, the 7210-7699 block except 7213 and 7640',
    'GL 7213 intercompany recharged customer service. Posts in quarterly lumps, not monthly, and ties to the forecast to the krona',
    'GL 5982 marketing consultants, the account Finance keeps out of Total Marketing',
    'GL 6420 audit fee &middot; 6530 accounting and payroll &middot; 6580 legal advisory',
    'GL 6550 other consultants &middot; 7640 interim consultants',
    'GL 6540 IT consultants',
    'GL 4571 software and ERP &middot; 4572 e-commerce and marketing &middot; 4574 hosting &middot; 4575 other IT &middot; 4576 personnel and productivity',
    'GL 5011 premise rent &middot; 5090 other costs of premises',
    'The residual of GL 5000-6999 after every other rung has taken its accounts, so an account Finance opens next month lands here visibly rather than dropping out of the ladder. It has to absorb 5929, 5940 and 5981, marketing accounts outside Finance&rsquo;s Total Marketing with no obvious home in their overhead list',
    'GL 6994 test costs &middot; 6996 inspection &middot; 6997 memberships'
  ];
}
var OHF = [null,null,null,null,null,null,null,null,'?',null];

function ladderRows(a, st, b){
  var rows = [];
  function row(k, n, hint, v, kind, pctOn, detail){
    rows.push({ k:k, n:n, h:hint, v:v, t:kind, p:pctOn, d:detail || null });
  }
  var c = a.c || {}, F = b || {};

  row('gross', 'Gross Sales', 'ex VAT, product only, excludes the SHIPPING pseudo-SKU', a.gross, 'in', true,
    a.geo ? null : [
      { n:'Invoiced product revenue', v:a.gross, f:F.gross, h:'sales invoice item lines, FX-normalised at BC&rsquo;s own daily rate for the posting date' },
      { n:'Return fees charged (not revenue)', v:-c.return_fee, h:'RETURNFEE pseudo-SKU &middot; GL 3055. Finance books this in Transaction Fees, not revenue, so it is excluded here and reappears in Other operating items.', mut:1 }
    ]);
  row('ret', 'Returns', 'product returns plus goodwill compensation', a.ret, 'out', true,
    a.geo ? null : [
      { n:'Product returns', v:-c.returns_product, h:'credit-memo item lines' },
      { n:'Goodwill compensation', v:-c.compensation, h:'COMPENSATION pseudo-SKU. Credits, not returned goods, which is why they sit in Returns rather than in COGS.' },
      { n:'Returns (total)', v:a.ret, f:F.ret, h:'the two lines above, against the forecast&rsquo;s single Returns line' },
      { n:'Shipping returns (in Net Shipping)', v:-c.ship_returns, h:'refunded postage. Finance nets this against shipping revenue.', mut:1 }
    ]);
  row('net', 'Net Sales', null, a.net, 'sub', true);
  row('cogs', 'Total COGS',
    a.geo ? 'item ledger, the only basis with a country dimension' : 'GL 4006 plus price, sample and stock adjustments',
    a.cogs, 'out', true,
    a.geo ? null : [
      { n:'Cost of goods sold', v:-c.cogs_4006, f:F.cogs ? null : null, h:'GL 4006, their line of the same name',
        fname:'Cost of goods sold' },
      { n:'Change in write down', v:null, f:null, flag:'?',
        h:'Their COGS structure names this line, the forecast file carries no values for it, and GL 4054 (slow-moving and stock-take provision) sits outside the Total COGS definition that reconciles to their July report. It is inside Other operating items instead, not forced into COGS.' },
      { n:'Other costs of goods sold', v:-c.cogs_adjustments, fname:'Other costs of goods sold',
        h:'GL 4014 price differences &middot; 4037 samples &middot; 4055 stock adjustments' }
    ]);
  row('gp1', 'GP1', null, a.gp1, 'sub', true);

  if (a.geo) {
    row('mktg', 'Total Marketing', 'de-duplicated Funnel spend; GL media has no country dimension', a.mktg, 'out', true);
    row('contrib', 'Contribution after marketing', null, a.contrib, 'sub', true);
    ['Net Shipping','Total Fulfillment','Transaction Fees','GP2','GP3','Other operating items',
     'Total Overhead','EBITDA','Depreciation and amortisation','EBIT','Net financial items','EBT · Net income']
      .forEach(function (n) { row('una-' + n, n, null, null, 'una', false); });
    return rows;
  }

  var sc = a.ship_cost || {}, fa = a.ful_acc || {};
  row('nship', 'Net Shipping', 'shipping revenue net of refunds, less freight and packaging', a.nship, 'out', true, [
    { n:'Shipping revenue', v:a.shipRevNet, fname:'Shipping revenue',
      h:'SHIPPING pseudo-SKU net of refunded postage. Ties to GL postage income 3049-3052 within 0.1%.' },
    { n:'Shipping costs - outbound', v:-(sc['4336']||0), fname:'Shipping costs - outbound', h:'GL 4336' },
    { n:'Shipping costs - returns', v:-(sc['4338']||0), fname:'Shipping costs - returns', h:'GL 4338' },
    { n:'Other shipping costs', v:-((sc['4337']||0)+(sc['4450']||0)), fname:'Other shipping costs',
      h:'GL 4337 other freight &middot; 4450 packaging and consumables' }
  ]);
  row('ful', 'Total Fulfillment', '3PL and value-added services', a.ful, 'out', true, [
    { n:'Fulfillment VAS - inbound',       v:-(fa['4480']||0), fname:'Fulfillment VAS - inbound',       h:'GL 4480' },
    { n:'Fulfillment VAS - outbound',      v:-(fa['4481']||0), fname:'Fulfillment VAS - outbound',      h:'GL 4481' },
    { n:'Fulfillment VAS - returns',       v:-(fa['4482']||0), fname:'Fulfillment VAS - returns',       h:'GL 4482' },
    { n:'Fulfillment 3PL - outbound',      v:-(fa['4497']||0), fname:'Fulfillment 3PL - outbound',      h:'GL 4497' },
    { n:'Fulfillment 3PL - returns',       v:-(fa['4498']||0), fname:'Fulfillment 3PL - returns',       h:'GL 4498' },
    { n:'Fulfillment 3PL - warehousing',   v:-(fa['4499']||0), fname:'Fulfillment 3PL - warehousing',   h:'GL 4499' }
  ]);
  row('tf', 'Transaction Fees', 'GL 4372-4375, a net credit in some months', a.tf, 'in', true,
    [{ n:'Transaction fees', v:a.tf, fname:'Transaction fees',
       h:'Klarna 4372 &middot; Walley 4373 &middot; Adyen/Amex 4374 &middot; other 4375. Walley&rsquo;s revenue share can exceed the Adyen and Amex fees, which is why this block nets to a credit in some months.' }]);

  if (st === 'sup') {
    rows[rows.length-1].part = 1; rows[rows.length-2].part = 1; rows[rows.length-3].part = 1;
    row('una-GP2', 'GP2', null, null, 'una', false);
  } else {
    row('gp2', 'GP2', null, a.gp2, 'sub', true);
  }

  row('mktg', 'Total Marketing', 'GL 5911-5990 media spend, excluding consultants', a.mktg, 'out', true, [
    { n:'Marketing ad spend',   v:-c.mktg_ad_5911, fname:'Marketing ad spend', h:'GL 5911' },
    { n:'Other marketing costs', v:-c.mktg_other,  fname:'Other marketing costs',
      h:'GL 5913 affiliate &middot; 5914 influencers &middot; 5916 other digital &middot; 5917 SEO &middot; 5990 offline' },
    { n:'Marketing consultants (excluded)', v:-c.mktg_consultants_5982, mut:1,
      h:'GL 5982. Finance keeps this out of Total Marketing; it sits in overhead as Consultants - marketing.' }
  ]);

  if (st === 'sup') {
    rows[rows.length-1].part = 1;
    ['GP3','Other operating items','Total Overhead','EBITDA','Depreciation and amortisation','EBIT',
     'Net financial items','EBT · Net income'].forEach(function (n) { row('una-' + n, n, null, null, 'una', false); });
    return rows;
  }
  row('gp3', 'GP3', null, a.gp3, 'sub', true);
  if (st === 'pj') {
    ['Other operating items','Total Overhead','EBITDA','Depreciation and amortisation','EBIT',
     'Net financial items','EBT · Net income'].forEach(function (n) { row('una-' + n, n, null, null, 'una', false); });
    return rows;
  }

  row('oo', 'Other operating items',
    'the P&L movements the GP3 ladder does not carry, so EBITDA ties to the ledger rather than being modelled',
    a.oo, 'in', false, [
      { n:'Return fee income', v:-c.return_fee, h:'GL 3055, excluded from the revenue rungs by Finance&rsquo;s own definition and reappearing here' },
      { n:'Everything else', v:a.oo + c.return_fee, flag:'?',
        h:'EBITDA is swept from the general ledger rather than built up, so this line is derived as the difference that makes the ladder meet it. It carries popup, intercompany and Swish sales posted as ledger-account lines, one-off and other external income, the slow-moving write-down, the inbound-freight and purchase-to-inventory flow, and the small basis differences between invoice lines and the GL revenue accounts. Because it is a residual rather than a list, an account Finance opens next month lands here visibly instead of vanishing.' }
    ]);
  row('toh', 'Total Overhead', 'personnel, consultants, IT, occupancy and quality', a.toh, 'out', true,
    (D().overhead_groups || []).map(function (n, i) {
      return { n:n, v:-a.oh[i], fname:n, h:OHH()[i], flag:OHF[i] };
    }));
  var oneOff = (a.ms.indexOf(H.ONE_OFF.month) >= 0) ? H.ONE_OFF.sek : 0;
  row('ebitda', 'EBITDA',
    oneOff ? ('includes a ' + sek(oneOff) + ' one-off ' + H.ONE_OFF.what + '; underlying ' + sek(a.ebitda - oneOff)) : null,
    a.ebitda, 'sub', true);
  row('da', 'Depreciation and amortisation', 'the whole GL 7800-7899 block', a.da, 'out', true,
    [{ n:'Amortization & Depreciation', v:a.da, fname:'Amortization & Depreciation',
       h:'GL 7800-7899. Not 7810 alone: GL 7801 carries real money and a 7810-only rule left it in neither D&amp;A nor EBITDA.' }]);
  row('ebit', 'EBIT', null, a.ebit, 'sub', true);
  row('fin', 'Net financial items', 'interest and FX. Lumpy by construction: interest posts on accrual dates, not monthly',
    a.fin, 'in', false, [
      { n:'Interest net', v:-c.interest, fname:'Interest net',
        h:'GL 8390 financial income &middot; 8413 bank interest &middot; 8415 other interest. Posts on accrual dates a handful of times a year.' },
      { n:'FX, realised and unrealised', v:-c.fx, flag:'?',
        h:'GL 7960 &middot; 7983 &middot; 7984 &middot; 8329 &middot; 8429. The forecast carries no FX line; we treat it as a financial item.' }
    ]);
  row('ebt', 'EBT · Net income', 'no tax line posts in 2026, so Net Income equals EBT rather than being estimated',
    a.ebt, 'sub', true);
  return rows;
}

R.ladderRows = ladderRows;
R.scorecard = renderScore;
R.ladderOpenState = open;
})();

/* ═══════════════════════════════════════════════════════════════════════════
 * Part 5: drawing the ladder.
 * ═══════════════════════════════════════════════════════════════════════════ */
(function () {
'use strict';
var H = window.__execplHelpers;
var R = window.__execplRender;
var esc = H.esc, sek = H.sek, pct = H.pct, pf = H.pf, el = H.el, lab = H.lab,
    dcell = H.dcell, agg = H.agg, pyAgg = H.pyAgg, fcFor = H.fcFor, fcLine = H.fcLine,
    wm = H.wm, pjOn = H.pjOn, projAgg = H.projAgg, pjPct = H.pjPct,
    estMonth = H.estMonth, est = H.est;
function D(){ return H.D(); } function S(){ return H.S(); }
var mstate = R.mstate, open = R.ladderOpenState;

/* Which estimator lines feed which rung, so the expandable detail can show the
 * driver, the fitted rate and that line's own backtest error where it belongs. */
var RUNG_LINES = {
  cogs:  ['cogs_gl'],
  nship: ['ship_out','ship_ret_freight','ship_misc','packaging'],
  ful:   ['vas','tpl_out','tpl_ret','tpl_wh'],
  tf:    ['txfee'],
  mktg:  ['marketing']
};

function renderLadder(){
  var s = S(), ms = wm(s.period), PJ = pjOn(ms, s.mkt);
  var a = PJ ? projAgg() : agg(ms, s.mkt);
  var b = fcFor(ms, s.mkt);
  var p = (s.cmp === 'PY') ? pyAgg(ms, s.mkt) : null;
  var st = PJ ? 'pj' : mstate(a);
  var rows = R.ladderRows(a, st, b);

  /* prior-year comparison, built through the same row model so keys line up */
  var PY = null;
  if (p) { PY = {}; R.ladderRows(p, mstate(p), null).forEach(function (r) { PY[r.k] = r; }); }

  /* Waterfall geometry: each deduction draws from the running total down. */
  var mx = Math.max(Math.abs(a.gross), 1), BW = 290, run = 0, geo = [];
  function cl(v){ return Math.max(0, Math.min(BW, v)); }
  rows.forEach(function (r) {
    if (r.v == null) { geo.push(null); return; }
    if (r.t === 'sub') { geo.push([0, cl(r.v / mx * BW)]); run = r.v; }
    else if (r.t === 'in' && r.k === 'gross') { geo.push([0, cl(r.v / mx * BW)]); run = r.v; }
    else { geo.push([cl((run + r.v) / mx * BW), cl(run / mx * BW)]); run += r.v; }
  });

  /* In projection mode the last column carries the POSTED share of each rung,
   * so an estimate can never be read as a posted figure. */
  var SH = null;
  if (PJ) {
    var posted = a.posted, em = estMonth();
    var P = H.pjLadder();
    SH = {
      gross:[posted.gross, a.gross, 'of gross sales'],
      ret:  [posted.ret,   a.ret,   'of returns'],
      net:  [posted.net,   a.net,   'of net sales'],
      cogs: [posted.cogs,  a.cogs,  'of COGS'],
      nship:[P.nship.posted, P.nship.projected, 'of freight and packaging'],
      ful:  [P.ful.posted,   P.ful.projected,   'of fulfilment'],
      tf:   [P.tf.posted,    P.tf.projected,    'nothing added'],
      mktg: [posted.mktg,  a.mktg,  'of marketing']
    };
  }
  function shareCell(k, kind){
    var sh = SH[k];
    if (!sh) return (kind === 'sub') ? '<span class="na">derived</span>' : '';
    var tot = Math.abs(sh[1]), bk = Math.abs(sh[0] || 0);
    var p0 = tot ? Math.max(0, Math.min(100, bk / tot * 100)) : 0;
    return '<div style="min-width:96px">' +
      '<div class="split" style="min-width:96px" role="img" aria-label="' + p0.toFixed(0) + ' per cent posted">' +
      '<i class="bk" style="width:' + p0.toFixed(1) + '%"></i>' +
      '<i class="es" style="width:' + (100-p0).toFixed(1) + '%"></i></div>' +
      '<div class="split-l"><span>' + p0.toFixed(0) + '% posted</span></div>' +
      '<div class="split-l" style="margin-top:0"><span>' + esc(sh[2]) + '</span></div></div>';
  }

  var out = ['<thead><tr><th class="l" style="width:236px">Line</th>' +
    '<th class="l" style="width:290px">Waterfall</th>' +
    '<th>' + (PJ ? 'Projected' : 'Actual') + '</th><th>% of Net Sales</th><th>vs FC</th>' +
    '<th>' + (PJ ? 'Posted share' : (p ? 'vs PY' : 'vs PY')) + '</th></tr></thead><tbody>'];

  rows.forEach(function (r, i) {
    if (r.t === 'una') {
      out.push('<tr class="una"><td class="l rung">' + esc(r.n) +
        ' <span class="mk mk-c" title="Not computable for this selection.">&#10005;</span></td>' +
        '<td class="wf"></td><td colspan="3" class="l" style="text-align:left;font-size:11.5px;white-space:normal">' +
        (a.geo
          ? 'Unavailable by market. Shipping, fulfilment, transaction fees and overhead post to the general ledger at carrier, settlement and company granularity, with no country dimension at any grain.'
          : (PJ
            ? 'Not projected. The estimator is fitted on the direct-cost lines and stops at GP3; overhead, other operating items, D&amp;A and financial items have no fitted driver, so these rungs stay suppressed rather than being half-estimated.'
            : 'Unavailable. The open month&rsquo;s carrier, 3PL and marketing invoices have not posted, and these rungs are never estimated from a partial month.')) +
        '</td><td><span class="na">n/a</span></td></tr>');
      return;
    }

    var g = geo[i];
    var cls = r.t === 'sub' ? 'bar-sub' : (r.v >= 0 ? 'bar-in' : 'bar-out');
    var bar = '<svg viewBox="0 0 290 20" width="290" height="20" role="img" aria-label="' + esc(r.n) + '">' +
      '<rect x="' + Math.min(g[0], g[1]).toFixed(2) + '" y="4" width="' +
      Math.max(Math.abs(g[1] - g[0]), 1.5).toFixed(2) + '" height="12" rx="2" class="' + cls + '">' +
      '<title>' + esc(r.n) + ': ' + sek(Math.abs(r.v)) + ' SEK</title></rect></svg>';

    var isSub = r.t === 'sub';
    var hero = (r.k === 'gp3' || r.k === 'ebitda' || r.k === 'contrib');
    var mark = '';
    if (r.k === 'tf') mark = ' <span class="mk mk-d" title="Definitional gap against Finance. For July they reported +270 TSEK where our GL grouping gives +167 TSEK; no other GL account carries the difference, so it is a presentation or accrual difference rather than a missing cost.">D</span>';
    if (r.k === 'cogs' && !a.geo) mark = ' <span class="mk mk-b" title="General-ledger basis, matching Finance. The item ledger is the market-attributable basis and differs month to month on the posting cut-off.">B</span>';
    if (r.k === 'oo') mark = ' <span class="mk mk-d" title="No forecast counterpart. These are real P&amp;L movements the GP3 ladder does not carry; they are shown explicitly so EBITDA ties to the general ledger rather than being forced.">?</span>';
    if (r.k === 'fin') mark = ' <span class="mk mk-d" title="Interest posts on accrual dates, not monthly, and FX swings either way. Read EBITDA monthly and the tail annually.">D</span>';
    if (r.part) mark = ' <span class="mk mk-d" title="Partial. The open month&#39;s carrier, 3PL and marketing invoices have not fully posted, so this line is incomplete.">P</span>';
    if ((r.k === 'gp2' || r.k === 'gp3' || r.k === 'ebitda') && st === 'open')
      mark = ' <span class="mk mk-d" title="Open window. It contains the open month, whose carrier, 3PL and marketing invoices have not fully posted.">O</span>';
    if (PJ && SH[r.k]) mark = ' <span class="mk mk-p" title="Projected: posted to date plus a fitted estimate for what has not posted. The posted share is in the last column.">P</span>';
    if (PJ && (r.k === 'gp2' || r.k === 'gp3'))
      mark = ' <span class="mk mk-p" title="Projected. Read the margin percentage rather than the SEK figure: revenue and cost errors partly cancel in the ratio and do not in the absolute.">P</span>';

    var canExp = !!r.d;
    var fcv = (b && b[r.k] != null) ? b[r.k] : null;

    out.push('<tr' + (hero ? ' class="hero' + (canExp ? ' exp' : '') + '"'
                           : (isSub ? ' class="sub' + (canExp ? ' exp' : '') + '"'
                                    : (canExp ? ' class="exp"' : ''))) +
      (canExp ? ' data-x="' + esc(r.k) + '" tabindex="0" role="button" aria-expanded="' +
                (open[r.k] ? 'true' : 'false') + '"' : '') + '>' +
      '<td class="l rung">' + esc(r.n) + mark +
      (r.h ? '<span class="hint">' + r.h + '</span>' : '') + '</td>' +
      '<td class="wf">' + bar + '</td>' +
      '<td>' + (r.v < 0 ? '−' : '') + sek(Math.abs(r.v)) + '</td>' +
      '<td>' + (r.p ? ((r.v < 0 ? '−' : '') + pf(Math.abs(pct(r.v, a.net)))) : '') + '</td>' +
      '<td>' + (fcv != null ? dcell(r.v - fcv) : '<span class="na">n/a</span>') + '</td>' +
      '<td>' + (PJ ? shareCell(r.k, r.t)
                   : (PY && PY[r.k] && PY[r.k].v != null ? dcell(r.v - PY[r.k].v) : '<span class="na">n/a</span>')) +
      '</td></tr>');

    if (!canExp || !open[r.k]) return;

    /* Estimator attribution, shown inside the rung it belongs to. */
    if (PJ && RUNG_LINES[r.k]) {
      var lines = est().lines || {}, parts = (estMonth() || {}).parts || {};
      var any = RUNG_LINES[r.k].filter(function (k) { return lines[k]; });
      if (any.length) {
        out.push('<tr class="kid hdr"><td class="l">Estimator &middot; driver, fitted rate, backtest error</td>' +
          '<td></td><td>Projected</td><td></td><td>Posted</td><td>Share</td></tr>');
        any.forEach(function (k) {
          var L = lines[k], tot = parts[k] || 0;
          var bk = 0;   /* posted share of a fitted line comes from the rung total */
          var relm = L.reliable ? '' : ' <span class="mk mk-c" title="Not driver-predictable. See the note.">!</span>';
          out.push('<tr class="kid"><td class="l" style="padding-left:28px">' +
            '<span style="color:var(--proj-ink);font-weight:600">' + esc(L.label || k) + '</span>' + relm +
            '<span class="hint" style="max-width:52ch">driver <b>' + esc(L.driver_desc) + '</b>' +
            (L.rate ? ' at <b>' + (L.rate > 100 ? sek(L.rate) : L.rate.toFixed(4)) + '</b>' : '') +
            (L.prior_month_weight ? ', with ' + (L.prior_month_weight*100).toFixed(0) + '% of the prior month blended in' : '') +
            ' &middot; backtest MAPE <b>' + L.backtest_mape_pct + '%</b>, median error ' +
            sek(L.backtest_median_abs_err_sek) + ' SEK</span></td><td></td>' +
            '<td style="color:var(--proj-ink)">' + sek(tot) + '</td><td></td><td></td><td></td></tr>');
        });
      }
    }

    out.push('<tr class="kid hdr"><td class="l">Line detail &middot; named GL accounts, each against its own forecast sub-line</td>' +
      '<td></td><td>' + (PJ ? 'Projected' : 'Actual') + '</td><td></td><td>vs FC</td><td></td></tr>');

    r.d.forEach(function (x) {
      var fv = (x.fname != null) ? fcLine(x.fname, ms) : (x.f !== undefined ? x.f : null);
      var fm = x.flag
        ? ' <span class="mk mk-' + (x.flag === '!' ? 'c' : 'd') + '" title="' + esc(x.h || '') + '">' + x.flag + '</span>'
        : '';
      out.push('<tr class="kid"><td class="l">' +
        (x.mut ? '<span style="color:var(--ink-3)">' + esc(x.n) + '</span>' : esc(x.n)) + fm +
        (x.h ? '<span class="hint" style="max-width:52ch">' + x.h + '</span>' : '') + '</td><td></td>' +
        '<td' + (x.mut ? ' style="color:var(--ink-3)"' : '') + '>' +
        (x.v == null ? '<span class="na">n/a</span>' : ((x.v < 0 ? '−' : '') + sek(Math.abs(x.v)))) + '</td>' +
        '<td></td><td>' + ((fv != null && x.v != null && !x.mut) ? dcell(x.v - fv) : '<span class="na">–</span>') +
        '</td><td></td></tr>');
    });
  });
  out.push('</tbody>');

  el('ladTable').innerHTML = out.join('');

  [].forEach.call(el('ladTable').querySelectorAll('tr.exp'), function (tr) {
    function toggle(){ open[tr.dataset.x] = !open[tr.dataset.x]; renderLadder(); }
    tr.addEventListener('click', toggle);
    tr.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggle(); }
    });
  });

  var endRung = a.geo ? 'contribution after marketing'
    : (st === 'sup' ? 'GP1' : (st === 'pj' ? 'GP3, projected' : 'EBT'));
  el('ladTitle').innerHTML = 'Gross Sales to ' + endRung + ' &middot; ' + esc(H.PL[s.period]) +
    (s.mkt === 'ALL' ? '' : ' &middot; ' + esc(s.mkt));
  el('ladSub').innerHTML = PJ
    ? ('<span class="st st-pj">Projected</span> posted plus estimate, standing at day ' +
       ((estMonth() || {}).as_of_day || '–') +
       ' &middot; the last column is the posted share of each rung &middot; click a line for its estimator: driver, fitted rate and that line&rsquo;s own backtest error')
    : 'Finance&rsquo;s management-report format and their own sub-line names &middot; SEK, ex VAT, posting date &middot; click a line for its named GL accounts and their forecast deltas';

  /* Export only: a column filter on a P&L ladder is nonsense, but finance
   * readers do want the thing in a spreadsheet. */
  if (window.TableTools) {
    H.tools.ladder = window.TableTools.enhance(el('ladTable'),
      { filters:false, scroll:false, exportName:'exec-pl-ladder' });
  }
}

R.ladder = renderLadder;
})();

/* ═══════════════════════════════════════════════════════════════════════════
 * Part 6: reconciliation, variance bridge, markets.
 * ═══════════════════════════════════════════════════════════════════════════ */
(function () {
'use strict';
var H = window.__execplHelpers;
var R = window.__execplRender;
var esc = H.esc, sek = H.sek, msek = H.msek, pct = H.pct, pf = H.pf, el = H.el,
    lab = H.lab, dcell = H.dcell, agg = H.agg, fcFor = H.fcFor, wm = H.wm,
    pjOn = H.pjOn, projAgg = H.projAgg, pjPct = H.pjPct, skewTxt = H.skewTxt,
    skewRange = H.skewRange, THEIRS = H.THEIRS, THEIR_PCT = H.THEIR_PCT,
    estMonth = H.estMonth, isOpen = H.isOpen;
function D(){ return H.D(); } function S(){ return H.S(); }
var mstate = R.mstate;

/* ══ Reconciliation to Babyshop's own July 2026 management report ══════════
 * "Ours" is recomputed from the live snapshot every render, so if carrier
 * invoices post into July after their report was issued, the delta MOVES and
 * the panel says so. That is the point: a tie that is asserted once and never
 * re-checked is not a tie.
 */
var RECON_MONTH = '2026-07';
var WHY = {
  gross:['Tied','Rounding only. Our invoice lines reproduce their gross sales to 0.01%.'],
  ret:['Tied','Their Returns is product returns plus goodwill compensation. Shipping returns sit in Net Shipping and return fees in Transaction Fees. Every other combination was tested; this is the only one that reproduces their figure.'],
  net:['Tied','Follows from the two lines above.'],
  cogs:['Tied','Their basis is GL 4006 plus price, sample and stock adjustments, not the item ledger, which is several hundred TSEK away for July on the posting cut-off between June and July.'],
  gp1:['Tied','Ties once COGS is on their GL basis.'],
  nship:['Ledger moved','This line tied within 5 TSEK when the mapping was built. Customer freight (GL 4336) and returns freight (GL 4338) have posted into July since their report was issued. The mapping has not changed; the ledger has, and it will resolve when Finance restates or issues the next report on the current ledger.'],
  ful:['Tied','Exactly GL 4480 + 4481 + 4482 + 4497 + 4498 + 4499. Their own forecast sub-lines name these six in the same order, which independently confirms the grouping.'],
  tf:['Open','Definitional. No GL account carries the difference; the 4370-4379 block swings either way month to month, so this is an accrual or presentation difference in the management report rather than a missing cost.'],
  gp2:['Open','The transaction-fee gap plus the late freight postings above. Every other input ties.'],
  mktg:['Tied','GL media accounts 5911-5990, excluding marketing consultants (5982). Their forecast splits this into ad spend and other marketing costs, which maps onto GL 5911 and the rest exactly.'],
  gp3:['Open','The transaction-fee gap plus late freight. Nothing else differs.'],
  ebitda:['Approx','Their July summary states EBITDA of about −0.7 M. Their figure is quoted to one decimal, so this is agreement, not a tie to the krona.']
};
var RECON_ROWS = [
  ['gross','Gross Sales'],['ret','Returns'],['net','Net Sales'],['cogs','Total COGS'],
  ['gp1','GP1'],['nship','Net Shipping'],['ful','Total Fulfillment'],['tf','Transaction Fees'],
  ['gp2','GP2'],['mktg','Total Marketing'],['gp3','GP3'],['ebitda','EBITDA']
];
var TIE_TSEK = 25000;

function renderRecon(){
  if (D().months.indexOf(RECON_MONTH) < 0) { el('recTable').innerHTML = ''; return; }
  var a = agg([RECON_MONTH], 'ALL');
  var out = ['<thead><tr><th class="l">Line &middot; ' + esc(lab(RECON_MONTH)) + '</th><th>Ours</th>' +
    '<th>Their report</th><th>Delta</th><th class="l">Status</th>' +
    '<th class="l" style="width:40%">Why</th></tr></thead><tbody>'];
  var tied = 0;
  RECON_ROWS.forEach(function (r) {
    var k = r[0], ours = a[k], theirs = THEIRS[k], d = (ours == null) ? null : ours - theirs;
    var w = WHY[k], ok = (w[0] === 'Tied');
    if (ok) tied++;
    var big = d != null && Math.abs(d) > TIE_TSEK;
    out.push('<tr' + (['gp1','gp2','gp3','ebitda'].indexOf(k) >= 0 ? ' class="sub"' : '') + '>' +
      '<td class="l rung">' + esc(r[1]) + '</td>' +
      '<td>' + (ours < 0 ? '−' : '') + sek(Math.abs(ours)) + '</td>' +
      '<td>' + (theirs < 0 ? '−' : '') + sek(Math.abs(theirs)) +
      (k === 'ebitda' ? ' <span class="mk mk-b" title="Quoted by Babyshop as about −0.7 M, not to the krona.">&asymp;</span>' : '') + '</td>' +
      '<td class="' + (big ? 'neg' : '') + '">' + (d > 0 ? '+' : '−') + sek(Math.abs(d)) + '</td>' +
      '<td class="l"><span class="sev-pill ' + (ok ? 'ok' : 'open') + '">' + esc(w[0]) + '</span></td>' +
      '<td class="l" style="white-space:normal;font-size:11.5px;line-height:1.5;color:var(--ink-2)">' + esc(w[1]) + '</td></tr>');
  });
  out.push('</tbody>');
  el('recTable').innerHTML = out.join('');

  var openLines = RECON_ROWS.filter(function (r) {
    var d = a[r[0]] - THEIRS[r[0]];
    return Math.abs(d) > TIE_TSEK;
  });
  el('recSum').innerHTML =
    '<b>' + (RECON_ROWS.length - openLines.length) + ' of ' + RECON_ROWS.length +
    ' lines agree within ' + (TIE_TSEK/1000) + ' TSEK, and EBITDA lands ' +
    sek(Math.abs(a.ebitda - THEIRS.ebitda)) + ' from Babyshop&rsquo;s own stated figure.</b> ' +
    (openLines.length
      ? 'Open: ' + openLines.map(function (r) {
          return '<b>' + esc(r[1]) + '</b> (' + (a[r[0]] - THEIRS[r[0]] > 0 ? '+' : '−') +
                 sek(Math.abs(a[r[0]] - THEIRS[r[0]])) + ')'; }).join(', ') + '. '
      : '') +
    'These figures are recomputed from the live snapshot on every refresh, not transcribed, so a line that ties ' +
    'today can drift tomorrow when invoices post into a closed month after the report was issued. ' +
    'Percentages use their denominator, Net Sales: our GP1 ' + pf(pct(a.gp1, a.net)) + ' against their ' +
    THEIR_PCT.gp1 + '%, GP2 ' + pf(pct(a.gp2, a.net)) + ' against ' + THEIR_PCT.gp2 + '%, GP3 ' +
    pf(pct(a.gp3, a.net)) + ' against ' + THEIR_PCT.gp3 + '%.';

  if (window.TableTools) {
    H.tools.recon = window.TableTools.enhance(el('recTable'),
      { filters:false, scroll:false, exportName:'exec-pl-july-reconciliation' });
  }
}

/* ══ Variance bridge ═════════════════════════════════════════════════════════ */
function renderBridge(){
  var s = S(), ms = wm(s.period), PJ = pjOn(ms, s.mkt);
  var a = PJ ? projAgg() : agg(ms, s.mkt);
  var b = fcFor(ms, s.mkt), st = PJ ? 'pj' : mstate(a);
  var host = el('brChart'), note = el('brNote'), ttl = el('brTitle');

  el('brSub').innerHTML = PJ
    ? 'Projected GP3 against forecast GP3 &middot; the estimator stops at GP3, so this walk does too'
    : 'Like-for-like forecast lines &middot; ' + esc(((D().forecast || {})._meta || {}).vintage || 'rolling forecast');

  if (!b || a.geo || st === 'sup') {
    host.innerHTML = '<div class="notice" style="background:var(--surf-2);color:var(--ink-2);border-color:var(--rule)">' +
      '<span>&#9888;</span><span>' +
      (a.geo ? '<b>No bridge at market level.</b> The forecast is global only, and GP3 and EBITDA cannot be built for one market.'
        : (st === 'sup' ? '<b>No bridge for an open month.</b> EBITDA does not exist for it yet.'
          : '<b>No forecast for this window.</b> The rolling forecast covers 2026 only.')) +
      '</span></div>';
    note.innerHTML = ''; ttl.innerHTML = 'Why EBITDA differs from forecast';
    return;
  }

  var steps = PJ
    ? [['FC GP3','start',b.gp3,null],
       ['Gross Sales','d',a.gross-b.gross,'perf'], ['Returns','d',a.ret-b.ret,'perf'],
       ['COGS','d',a.cogs-b.cogs,'perf'], ['Shipping','d',a.nship-b.nship,'est'],
       ['Fulfilment','d',a.ful-b.ful,'est'], ['Txn fees','d',a.tf-b.tf,'est'],
       ['Marketing','d',a.mktg-b.mktg,'est'], ['Projected GP3','end',a.gp3,null]]
    : [['FC EBITDA','start',b.ebitda,null],
       ['Gross Sales','d',a.gross-b.gross,'perf'], ['Returns','d',a.ret-b.ret,'perf'],
       ['COGS','d',a.cogs-b.cogs,'perf'], ['Shipping','d',a.nship-b.nship,'perf'],
       ['Fulfilment','d',a.ful-b.ful,'perf'], ['Txn fees','d',a.tf-b.tf,'perf'],
       ['Marketing','d',a.mktg-b.mktg,'perf'], ['Other ops','d',a.oo,'noplan'],
       ['Overhead','d',a.toh-b.toh,'perf'], ['Actual EBITDA','end',a.ebitda,null]];

  var S0 = PJ ? b.gp3 : b.ebitda, S1 = PJ ? a.gp3 : a.ebitda;
  var chk = S0;
  steps.forEach(function (x) { if (x[1] === 'd') chk += x[2]; });
  var resid = S1 - chk;
  if (Math.abs(resid) > 1) steps.splice(steps.length-1, 0, ['Residual','d',resid,'resid']);
  steps.forEach(function (x) { if (x[1] === 'd') x[1] = x[2] >= 0 ? 'up' : 'dn'; });

  var W = 880, Ht = 300, PLx = 62, PRx = 14, PT = 22, PB = 62;
  var pw = W-PLx-PRx, ph = Ht-PT-PB;
  var run = S0, lo = Math.min(0, S0, S1), hi = Math.max(S0, S1);
  steps.forEach(function (x) { if (x[1] === 'up' || x[1] === 'dn') { run += x[2]; lo = Math.min(lo, run); hi = Math.max(hi, run); } });
  hi *= 1.16; lo = Math.min(lo, 0) * 1.10;
  function y(v){ return PT + ph - ((v - lo) / (hi - lo) * ph); }
  var slot = pw / steps.length, bw = Math.min(24, slot * 0.5);

  var o = ['<svg viewBox="0 0 ' + W + ' ' + Ht + '" width="100%" role="img" aria-label="Bridge from forecast to actual">' +
    '<defs><pattern id="bp2" width="9" height="9" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">' +
    '<rect width="9" height="9" fill="var(--s1)"></rect><line x1="0" y1="0" x2="0" y2="9" stroke="var(--surf)" stroke-width="3.8"></line></pattern>' +
    '<pattern id="bpj" width="7" height="7" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">' +
    '<rect width="7" height="7" fill="var(--proj)"></rect><line x1="0" y1="0" x2="0" y2="7" stroke="var(--surf)" stroke-width="3"></line></pattern></defs>'];
  var stp = Math.max(1, Math.ceil((hi-lo)/4/5e6)) * 5e6;
  for (var gv = Math.ceil(lo/stp)*stp; gv <= hi; gv += stp) {
    o.push('<line class="gridline" x1="' + PLx + '" y1="' + y(gv).toFixed(1) + '" x2="' + (W-PRx) + '" y2="' + y(gv).toFixed(1) + '"></line>' +
      '<text class="tick" x="' + (PLx-8) + '" y="' + (y(gv)+3).toFixed(1) + '" text-anchor="end">' + Math.round(gv/1e6) + 'M</text>');
  }
  run = 0;
  steps.forEach(function (x, i) {
    var cx = PLx + slot*i + slot/2, X = cx - bw/2, y0, y1, cls;
    if (x[1] === 'start') { y0 = y(x[2]); y1 = y(0); run = x[2]; cls = 'bar-sub'; }
    else if (x[1] === 'end') { y0 = y(x[2]); y1 = y(0); cls = 'bar-sub'; }
    else if (x[1] === 'up') { y0 = y(run + x[2]); y1 = y(run); run += x[2]; cls = 'bar-in'; }
    else { y0 = y(run); y1 = y(run + x[2]); run += x[2]; cls = 'bar-out'; }
    var ht = (x[3] === 'noplan' || x[3] === 'resid'), es = (x[3] === 'est');
    o.push('<rect x="' + X.toFixed(1) + '" y="' + Math.min(y0,y1).toFixed(1) + '" width="' + bw.toFixed(1) +
      '" height="' + Math.max(Math.abs(y1-y0), 2).toFixed(1) + '" rx="2" class="' + (ht||es ? '' : cls) + '"' +
      (ht ? ' fill="url(#bp2)"' : (es ? ' fill="url(#bpj)"' : '')) + '><title>' + esc(x[0]) + ': ' + sek(x[2]) + ' SEK' +
      (es ? ' (estimated, nothing posted yet)' : '') + '</title></rect>');
    if (PJ && x[1] === 'end') {
      var bLo = y(skewRange(pjPct('gp3'))[0]), bHi = y(skewRange(pjPct('gp3'))[1]);
      o.push('<line x1="' + cx.toFixed(1) + '" y1="' + bHi.toFixed(1) + '" x2="' + cx.toFixed(1) + '" y2="' + bLo.toFixed(1) +
        '" stroke="var(--proj)" stroke-width="2"></line>' +
        '<line x1="' + (cx-7).toFixed(1) + '" y1="' + bHi.toFixed(1) + '" x2="' + (cx+7).toFixed(1) + '" y2="' + bHi.toFixed(1) + '" stroke="var(--proj)" stroke-width="2"></line>' +
        '<line x1="' + (cx-7).toFixed(1) + '" y1="' + bLo.toFixed(1) + '" x2="' + (cx+7).toFixed(1) + '" y2="' + bLo.toFixed(1) +
        '" stroke="var(--proj)" stroke-width="2"><title>' + skewTxt() + ' band</title></line>');
    }
    if (i < steps.length-1 && x[1] !== 'end') {
      var yc = (x[1] === 'start') ? y(x[2]) : y(run);
      o.push('<line x1="' + (X+bw).toFixed(1) + '" y1="' + yc.toFixed(1) + '" x2="' +
        (PLx + slot*(i+1) + slot/2 - bw/2).toFixed(1) + '" y2="' + yc.toFixed(1) + '" stroke="var(--rule-2)" stroke-width="1"></line>');
    }
    var sg = (x[1] === 'up' ? '+' : (x[1] === 'dn' ? '−' : ''));
    o.push('<text class="dlab" x="' + cx.toFixed(1) + '" y="' + (Math.min(y0,y1)-6).toFixed(1) + '" text-anchor="middle">' +
      sg + (Math.abs(x[2])/1e6).toFixed(1) + 'M</text>' +
      '<text class="tickb" x="' + cx.toFixed(1) + '" y="' + (Ht-PB+18) + '" text-anchor="middle">' + esc(x[0]) + '</text>');
    if (x[3] === 'noplan') o.push('<text class="tick" x="' + cx.toFixed(1) + '" y="' + (Ht-PB+31) + '" text-anchor="middle">no FC line</text>');
    else if (x[3] === 'resid') o.push('<text class="tick" x="' + cx.toFixed(1) + '" y="' + (Ht-PB+31) + '" text-anchor="middle">residual</text>');
  });
  o.push('<line class="axis" x1="' + PLx + '" y1="' + y(0).toFixed(1) + '" x2="' + (W-PRx) + '" y2="' + y(0).toFixed(1) + '"></line></svg>');
  host.innerHTML = o.join('');

  if (PJ) {
    ttl.innerHTML = 'Why projected GP3 is ' + sek(Math.abs(a.gp3-b.gp3)) + ' ' +
      (a.gp3 >= b.gp3 ? 'ahead of' : 'behind') + ' forecast';
    note.innerHTML = '<strong style="color:var(--ink)">This bridge runs to projected GP3, not to EBITDA.</strong> ' +
      'The estimator is fitted on the direct-cost lines only, so overhead and other operating items are left out rather ' +
      'than guessed, and the walk stops where the model does. ' +
      '<strong style="color:var(--ink)">The four hatched bars are entirely estimated:</strong> shipping, fulfilment, ' +
      'transaction fees and marketing are 0% posted intra-month, so each is a fitted rate on a projected driver rather ' +
      'than a partly posted actual. Gross Sales, Returns and COGS are part posted and part projected; their posted share ' +
      'is in the ladder above. <strong style="color:var(--ink)">The whisker on the end bar is the band</strong>, ' +
      skewTxt() + ' on the margin. Read the end bar as a range, not as a point.';
    return;
  }
  ttl.innerHTML = 'Why EBITDA is ' + sek(Math.abs(a.ebitda-b.ebitda)) + ' ' +
    (a.ebitda >= b.ebitda ? 'ahead of' : 'behind') + ' forecast';
  note.innerHTML = '<strong style="color:var(--ink)">This bridge is like-for-like.</strong> The rolling forecast carries ' +
    'shipping, fulfilment, transaction fees and every overhead line separately, so each bar compares a forecast line ' +
    'against the same GL grouping and the walk closes without a pro-rata split. ' +
    '<strong style="color:var(--ink)">One bar has no forecast counterpart:</strong> <em>Other ops</em> is hatched because ' +
    'Babyshop&rsquo;s P&amp;L has no such line. It carries real movements that sit outside the GP3 ladder but inside EBITDA. ' +
    'Volume and price are not separated: the forecast budgets money only, with no order or AOV target, so any such split ' +
    'would be invented.';
}

R.reconciliation = renderRecon;
R.bridge = renderBridge;
})();

/* ═══════════════════════════════════════════════════════════════════════════
 * Part 7: markets, and the trend panel.
 * ═══════════════════════════════════════════════════════════════════════════ */
(function () {
'use strict';
var H = window.__execplHelpers;
var R = window.__execplRender;
var esc = H.esc, sek = H.sek, msek = H.msek, pct = H.pct, pf = H.pf, el = H.el,
    lab = H.lab, agg = H.agg, fcFor = H.fcFor, wm = H.wm, pjOn = H.pjOn,
    projAgg = H.projAgg, pjPct = H.pjPct, skewTxt = H.skewTxt,
    skewRange = H.skewRange, estMonth = H.estMonth, isOpen = H.isOpen,
    gp1Observed = H.gp1Observed;
function D(){ return H.D(); } function S(){ return H.S(); }
var mstate = R.mstate;
var expOther = false;

/* ══ Markets ═════════════════════════════════════════════════════════════════
 * Stops at contribution after marketing. Below that line BC has nothing with a
 * country on it: logistics posts at carrier-invoice granularity, payment fees
 * at settlement granularity, overhead at company level, and shipmentMethodId is
 * empty on every row.
 *
 * COGS here is the ITEM LEDGER, the only cost basis carrying a country, and it
 * is a different basis from the group ladder's general ledger. The two disagree
 * month to month on the posting cut-off. That gap is stated rather than scaled
 * away, because scaling market rows onto the GL total would silently turn a
 * measurement into an allocation.
 */
function renderMarkets(){
  var s = S(), ms = wm(s.period);
  var tot = agg(ms, 'ALL');
  var codes = (D().market_countries || []);
  var rows = codes.map(function (k) { return { k:k, a:agg(ms, k) }; })
    .filter(function (r) { return r.a.orders > 0 || r.a.gross !== 0; })
    .sort(function (x, y) { return (y.a.contrib || 0) - (x.a.contrib || 0); });

  var maxc = Math.max.apply(null, rows.map(function (r) { return r.a.contrib || 0; }).concat([1]));
  var sumNet = rows.reduce(function (t, r) { return t + r.a.net; }, 0);

  var out = ['<thead><tr><th class="l">Market</th><th>Orders</th><th>Net Sales</th><th>AOV</th>' +
    '<th>COGS</th><th>GP1</th><th>GP1%</th><th>Return rate</th><th>Marketing</th>' +
    '<th class="l" style="width:112px">Contribution</th><th>after marketing</th><th>Share</th></tr></thead><tbody>'];

  rows.forEach(function (r) {
    var a = r.a, k = r.k, g1 = pct(a.gp1, a.net), mark = '';
    var isUn = (k === 'UNATTRIBUTED');
    var isOther = (k === 'Other');
    if (isUn) mark = ' <span class="mk mk-b" title="Not a market. Item-ledger entries whose customer carries no country, kept visible so the market rows still sum to the item-ledger total rather than quietly losing cost.">B</span>';
    if (g1 != null && (g1 < 0 || g1 > 70) && !isUn)
      mark = ' <span class="mk mk-c" title="GP1% outside the plausible 0 to 70% band. Check the MARKET_MARGIN_BAND rule in the watchlist before quoting this row.">!</span>';
    if (isOther) mark += ' <span class="mk mk-d" title="A residual bucket, not a country. Click to see what it is made of.">?</span>';

    out.push('<tr' + (isOther ? ' class="exp" id="otherRow" aria-expanded="' + expOther + '" tabindex="0" role="button"' : '') + '>' +
      '<td class="l"><strong>' + esc(isUn ? 'Unattributed' : k) + '</strong>' + mark + '</td>' +
      '<td>' + sek(a.orders) + '</td><td>' + sek(a.net) + '</td>' +
      '<td>' + (a.orders ? sek(a.net / a.orders) : '<span class="na">–</span>') + '</td>' +
      '<td>' + sek(-a.cogs) + '</td><td>' + sek(a.gp1) + '</td>' +
      '<td' + (g1 != null && g1 < 10 ? ' class="neg"' : '') + '>' + pf(g1) + '</td>' +
      '<td>' + pf(pct(-a.ret, a.gross)) + '</td><td>' + sek(-a.mktg) + '</td>' +
      '<td class="l" style="padding-left:9px"><svg viewBox="0 0 104 12" width="104" height="12" role="img" aria-label="' + esc(k) + '">' +
      '<rect x="0" y="1" width="' + (Math.max(a.contrib || 0, 0) / maxc * 104).toFixed(1) + '" height="10" rx="2" class="bar-in">' +
      '<title>' + esc(k) + ': ' + sek(a.contrib) + ' SEK</title></rect></svg></td>' +
      '<td>' + sek(a.contrib) + '</td><td>' + pf(pct(a.net, sumNet)) + '</td></tr>');

    if (isOther && expOther) out.push(otherDetail(ms, a, tot));
  });
  out.push('</tbody><tfoot>');

  var st = mstate(tot);
  var mktCogs = rows.reduce(function (t, r) { return t - r.a.cogs; }, 0);
  var mktGp1  = rows.reduce(function (t, r) { return t + r.a.gp1; }, 0);
  var mktMk   = rows.reduce(function (t, r) { return t - r.a.mktg; }, 0);
  out.push('<tr><td class="l">Sum of markets</td><td>' + sek(tot.orders) + '</td><td>' + sek(sumNet) + '</td>' +
    '<td>' + sek(sumNet / (tot.orders || 1)) + '</td><td>' + sek(mktCogs) + '</td><td>' + sek(mktGp1) + '</td>' +
    '<td>' + pf(pct(mktGp1, sumNet)) + '</td><td>' + pf(pct(-tot.ret, tot.gross)) + '</td><td>' + sek(mktMk) + '</td>' +
    '<td class="l"></td><td>' + sek(mktGp1 - mktMk) + '</td><td>100%</td></tr>');

  /* The basis difference, stated as a row rather than left for the reader. */
  var gapAll = (D().checks || {}).item_ledger_vs_gl_cogs || {};
  var glCogs = -tot.cogs, glMk = -tot.mktg;
  out.push('<tr class="grp"><td class="l" colspan="4">Group ladder, general-ledger basis' +
    ' <span class="mk mk-b" title="The group ladder takes COGS and marketing from the general ledger; the market rows take them from the item ledger and de-duplicated Funnel spend, the only two bases with a country. The rows are not meant to foot.">B</span></td>' +
    '<td>' + sek(glCogs) + '</td><td>' + sek(tot.gp1) + '</td><td>' + pf(pct(tot.gp1, tot.net)) + '</td>' +
    '<td></td><td>' + sek(glMk) + '</td><td class="l"></td><td>' + sek(tot.gp1 - glMk) + '</td><td></td></tr>');
  out.push('<tr class="grp"><td class="l" colspan="11" style="white-space:normal;line-height:1.5">' +
    'Difference is basis, not error: item ledger against general ledger runs ' +
    (function () {
      var g = ms.map(function (m) { return gapAll[m] && gapAll[m].pct; }).filter(function (v) { return v != null; });
      if (!g.length) return 'on a different posting cut-off';
      return (Math.min.apply(null, g)).toFixed(1) + '% to ' + (Math.max.apply(null, g) > 0 ? '+' : '') +
             (Math.max.apply(null, g)).toFixed(1) + '% per month over this window';
    })() +
    ', and de-duplicated Funnel spend against GL media differs by agency and non-media spend. ' +
    'Neither is scaled onto the other.</td><td></td></tr>');

  if (st === 'ok' || st === 'open') {
    out.push('<tr class="grp"><td class="l" colspan="9">Net Shipping + Fulfillment + Transaction Fees, group only, no country dimension' +
      ' <span class="mk mk-d" title="GL 4336/4337/4338/4450, 4480-4499 and 4370-4379 post at carrier and settlement granularity. shipmentMethodId is 100% empty.">D</span>' +
      (st === 'open' ? ' &middot; excludes the unposted open month' : '') +
      '</td><td class="l"></td><td class="neg">' + sek(tot.nship + tot.ful + tot.tf) + '</td><td></td></tr>');
    out.push('<tr><td class="l">Group GP3</td><td colspan="8"></td><td class="l"></td><td>' + sek(tot.gp3) +
      '</td><td>' + pf(pct(tot.gp3, tot.net)) + '</td></tr>');
    out.push('<tr class="grp"><td class="l" colspan="9">Total Overhead, group only, no country dimension' +
      ' <span class="mk mk-d" title="Personnel, consultants, IT, occupancy and quality post at company level with no market key.">D</span>' +
      ' &middot; including other operating items ' + sek(tot.oo) + '</td><td class="l"></td>' +
      '<td class="neg">' + sek(tot.oo + tot.toh) + '</td><td></td></tr>');
    out.push('<tr><td class="l">Group EBITDA</td><td colspan="8"></td><td class="l"></td><td>' + sek(tot.ebitda) +
      '</td><td>' + pf(pct(tot.ebitda, tot.net)) + '</td></tr>');
  } else {
    out.push('<tr class="grp"><td class="l" colspan="12">GP2, GP3 and EBITDA are unavailable for this window: ' +
      'the open month&rsquo;s carrier, 3PL and marketing invoices have not posted.</td></tr>');
  }
  out.push('</tfoot>');
  el('mktTable').innerHTML = out.join('');

  var or = el('otherRow');
  if (or) {
    var t = function () { expOther = !expOther; renderMarkets(); };
    or.addEventListener('click', t);
    or.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); t(); } });
  }

  el('mktFoot').innerHTML =
    '<strong style="color:var(--ink)">There is no per-market forecast anywhere on this tab.</strong> ' +
    'The rolling forecast is a single global set of P&amp;L lines with no market, shop or channel key, so market ' +
    'performance is shown against the group and against itself, never against a target that does not exist. ' +
    '<strong style="color:var(--ink)">Per-market logistics is an allocation, not a fact,</strong> and is therefore not ' +
    'published on these rows: the snapshot carries a flat by-order-count spread in <code>logistics_allocation</code>, ' +
    'every figure flagged <code>allocated: true</code>, shaped so a real per-order cost can replace it later. ' +
    'Click <strong style="color:var(--ink)">Other</strong> to see what that bucket is made of.';

  if (window.TableTools) {
    H.tools.markets = window.TableTools.enhance(el('mktTable'),
      { scroll:false, exportName:'exec-pl-markets', exportFooter:true });
    if (H.tools.markets && H.tools.markets.clearFilters) H.tools.markets.clearFilters();
  }
}

/* "Other" is a residual bucket, not a country, so expanding it shows what it
 * actually is, its month-by-month composition, rather than a country list the
 * snapshot does not carry. Raising EXEC_PL_MARKET_TOP_N is what would add one. */
function otherDetail(ms, a, tot){
  var out = ['<tr class="kid hdr"><td class="l">What &ldquo;Other&rdquo; is</td><td colspan="11"></td></tr>',
    '<tr class="kid"><td class="l" colspan="12" style="white-space:normal;font-size:11.5px;line-height:1.55;color:var(--ink-2)">' +
    'The residual of every country outside the top ' +
    ((D().market_countries || []).length - 2) + ' by net sales over the whole history window, so the named set stays ' +
    'stable month to month instead of reshuffling. The snapshot does not carry per-country detail inside it; raising ' +
    '<code>EXEC_PL_MARKET_TOP_N</code> on the refresh job is what would break it out, at the cost of document size. ' +
    'It is shown by month below, which is the breakdown that does exist.</td></tr>',
    '<tr class="kid hdr"><td class="l">Month</td><td>Orders</td><td>Net Sales</td><td>AOV</td><td>COGS</td>' +
    '<td>GP1</td><td>GP1%</td><td>Return rate</td><td>Marketing</td><td class="l"></td><td>Contribution</td><td></td></tr>'];
  ms.forEach(function (m) {
    var r = agg([m], 'Other');
    if (!r.orders && !r.gross) return;
    out.push('<tr class="kid"><td class="l">' + esc(lab(m)) + '</td><td>' + sek(r.orders) + '</td>' +
      '<td>' + sek(r.net) + '</td><td>' + (r.orders ? sek(r.net/r.orders) : '–') + '</td>' +
      '<td>' + sek(-r.cogs) + '</td><td>' + sek(r.gp1) + '</td><td>' + pf(pct(r.gp1, r.net)) + '</td>' +
      '<td>' + pf(pct(-r.ret, r.gross)) + '</td><td>' + sek(-r.mktg) + '</td><td class="l"></td>' +
      '<td>' + sek(r.contrib) + '</td><td></td></tr>');
  });
  return out.join('');
}

/* ══ Trend ═══════════════════════════════════════════════════════════════════
 * SEK and percentages are never plotted on one axis. The open month is drawn
 * split: solid for what has posted, hatched for the projected remainder, so the
 * bar cannot be read as a closed month.
 */
function renderTrend(){
  var s = S(), months = D().months.slice(), mkt = s.mkt;
  var per = months.map(function (m) { return agg([m], mkt); });
  var em = estMonth(), showFC = (mkt === 'ALL');
  var pa = (showFC && em && isOpen(em.month)) ? projAgg() : null;

  var TW = 920, APL = 54, APR = 16, AT = 26, AH = 150, BT = AT+AH+52, BH = 104, THt = BT+BH+40;
  var pw = TW-APL-APR, slot = pw / months.length;
  var rmax = Math.max.apply(null, per.map(function (a) { return a.net; })) * 1.12;
  var fcNet = months.map(function (m) { return showFC ? H.fcLine('Net Sales', [m]) : null; });
  fcNet.forEach(function (v) { if (v != null) rmax = Math.max(rmax, v*1.08); });
  if (pa) rmax = Math.max(rmax, pa.net*1.12);
  var step = Math.max(1, Math.ceil(rmax/3/1e7)) * 1e7;
  function ay(v){ return AT + AH - (v/rmax*AH); }
  function tx(i){ return APL + slot*i + slot/2; }
  var pmin = -6, pmax = 60;
  function by(v){ return BT + BH - ((Math.max(Math.min(v, pmax), pmin) - pmin)/(pmax-pmin)*BH); }

  var o = ['<svg viewBox="0 0 ' + TW + ' ' + THt + '" width="100%" role="img" aria-label="Monthly net sales and margin">' +
    '<defs><pattern id="tp2" width="9" height="9" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">' +
    '<rect width="9" height="9" fill="var(--s1)"></rect><line x1="0" y1="0" x2="0" y2="9" stroke="var(--surf)" stroke-width="3.8"></line></pattern>' +
    '<pattern id="tpj" width="7" height="7" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">' +
    '<rect width="7" height="7" fill="var(--proj)"></rect><line x1="0" y1="0" x2="0" y2="7" stroke="var(--surf)" stroke-width="3"></line></pattern></defs>' +
    '<text class="tickb" x="' + APL + '" y="12" style="font-weight:600;fill:var(--ink)">Net Sales · MSEK</text>'];
  for (var gv = 0; gv <= rmax; gv += step) {
    o.push('<line class="gridline" x1="' + APL + '" y1="' + ay(gv).toFixed(1) + '" x2="' + (TW-APR) + '" y2="' + ay(gv).toFixed(1) + '"></line>' +
      '<text class="tick" x="' + (APL-8) + '" y="' + (ay(gv)+3).toFixed(1) + '" text-anchor="end">' + Math.round(gv/1e6) + '</text>');
  }
  var bw = Math.min(24, slot*0.55);
  months.forEach(function (m, i) {
    var v = per[i].net, op = isOpen(m);
    if (op && pa) {
      o.push('<rect x="' + (tx(i)-bw/2).toFixed(1) + '" y="' + ay(pa.net).toFixed(1) + '" width="' + bw.toFixed(1) +
        '" height="' + Math.max(ay(v)-ay(pa.net), 0).toFixed(1) + '" rx="2" fill="url(#tpj)"><title>' + esc(lab(m)) +
        ' projected: ' + sek(pa.net) + ' SEK, of which ' + sek(pa.net-v) + ' still estimated</title></rect>' +
        '<rect x="' + (tx(i)-bw/2).toFixed(1) + '" y="' + ay(v).toFixed(1) + '" width="' + bw.toFixed(1) +
        '" height="' + Math.max(ay(0)-ay(v), 0).toFixed(1) + '" rx="2" class="bar-in"><title>' + esc(lab(m)) +
        ' posted to date: ' + sek(v) + ' SEK</title></rect>');
      return;
    }
    o.push('<rect x="' + (tx(i)-bw/2).toFixed(1) + '" y="' + ay(v).toFixed(1) + '" width="' + bw.toFixed(1) +
      '" height="' + Math.max(ay(0)-ay(v), 0).toFixed(1) + '" rx="2" class="' + (op ? '' : 'bar-in') + '"' +
      (op ? ' fill="url(#tp2)"' : '') + '><title>' + esc(lab(m)) + ': ' + sek(v) + ' SEK' + (op ? ' (not closed)' : '') + '</title></rect>');
  });
  o.push('<line class="axis" x1="' + APL + '" y1="' + ay(0).toFixed(1) + '" x2="' + (TW-APR) + '" y2="' + ay(0).toFixed(1) + '"></line>');
  if (showFC) {
    var bp = [];
    fcNet.forEach(function (v, i) { if (v != null) bp.push([tx(i), ay(v)]); });
    if (bp.length > 1) {
      o.push('<path class="ln-3" d="M ' + bp.map(function (q) { return q[0].toFixed(1)+' '+q[1].toFixed(1); }).join(' L ') + '"></path>');
      fcNet.forEach(function (v, i) {
        if (v != null) o.push('<circle cx="' + tx(i).toFixed(1) + '" cy="' + ay(v).toFixed(1) +
          '" r="3.2" class="dot-3 ring"><title>' + esc(lab(months[i])) + ' forecast: ' + sek(v) + '</title></circle>');
      });
      o.push('<text class="tickb" x="' + (bp[0][0]-6).toFixed(1) + '" y="' + (bp[0][1]-9).toFixed(1) +
        '" text-anchor="end" style="fill:var(--ink-2)">FC</text>');
    }
  }

  o.push('<text class="tickb" x="' + APL + '" y="' + (BT-16) + '" style="font-weight:600;fill:var(--ink)">Margin · % of Net Sales</text>');
  [0,20,40].forEach(function (g) {
    o.push('<line class="gridline" x1="' + APL + '" y1="' + by(g).toFixed(1) + '" x2="' + (TW-APR) + '" y2="' + by(g).toFixed(1) + '"></line>' +
      '<text class="tick" x="' + (APL-8) + '" y="' + (by(g)+3).toFixed(1) + '" text-anchor="end">' + g + '%</text>');
  });

  var lastI = months.length-1, openLast = isOpen(months[lastI]);
  var g1 = per.map(function (a) { return pct(a.gp1, a.net); });
  var g1posted = g1[lastI];
  /* The open month plots its PROJECTED GP1, not its posted-to-date figure:
   * intra-month, cost posts behind the revenue it belongs to, so the posted
   * ratio reads far too high and would draw a spike that is pure cut-off. */
  if (openLast && pa) g1[lastI] = pjPct('gp1');
  var g1p = g1.map(function (v, i) { return tx(i).toFixed(1) + ' ' + by(v).toFixed(1); });
  if (openLast && pa && months.length > 1) {
    o.push('<path class="ln-1" d="M ' + g1p.slice(0, lastI).join(' L ') + '"></path>');
    o.push('<path class="ln-1" style="stroke-dasharray:5 3" d="M ' + g1p[lastI-1] + ' L ' + g1p[lastI] + '"></path>');
  } else {
    o.push('<path class="ln-1" d="M ' + g1p.join(' L ') + '"></path>');
  }

  var hasG3 = (mkt === 'ALL');
  if (hasG3) {
    var p3 = [], p3i = [];
    months.forEach(function (m, i) {
      if (!isOpen(m)) { p3.push([tx(i), by(pct(per[i].gp3, per[i].net))]); p3i.push(i); }
    });
    if (p3.length > 1) {
      o.push('<path class="ln-2" d="M ' + p3.map(function (q) { return q[0].toFixed(1)+' '+q[1].toFixed(1); }).join(' L ') + '"></path>');
      p3.forEach(function (q, j) {
        o.push('<circle cx="' + q[0].toFixed(1) + '" cy="' + q[1].toFixed(1) + '" r="3.2" class="dot-2 ring"><title>' +
          esc(lab(months[p3i[j]])) + ' GP3: ' + pf(pct(per[p3i[j]].gp3, per[p3i[j]].net)) + '</title></circle>');
      });
      if (openLast && pa) {
        var gy = by(pjPct('gp3')), pv = p3[p3.length-1], cxl = tx(lastI);
        var wLo = by(pjPct('gp3') - H.SKEW.dn), wHi = by(pjPct('gp3') + H.SKEW.up);
        o.push('<path class="ln-2" style="stroke-dasharray:5 3" d="M ' + pv[0].toFixed(1) + ' ' + pv[1].toFixed(1) +
          ' L ' + cxl.toFixed(1) + ' ' + gy.toFixed(1) + '"></path>' +
          '<line x1="' + cxl.toFixed(1) + '" y1="' + wHi.toFixed(1) + '" x2="' + cxl.toFixed(1) + '" y2="' + wLo.toFixed(1) +
          '" stroke="var(--proj)" stroke-width="2.5"><title>GP3 band ' + skewTxt() + '</title></line>' +
          '<line x1="' + (cxl-6).toFixed(1) + '" y1="' + wHi.toFixed(1) + '" x2="' + (cxl+6).toFixed(1) + '" y2="' + wHi.toFixed(1) + '" stroke="var(--proj)" stroke-width="2.5"></line>' +
          '<line x1="' + (cxl-6).toFixed(1) + '" y1="' + wLo.toFixed(1) + '" x2="' + (cxl+6).toFixed(1) + '" y2="' + wLo.toFixed(1) + '" stroke="var(--proj)" stroke-width="2.5"></line>' +
          '<circle cx="' + cxl.toFixed(1) + '" cy="' + gy.toFixed(1) + '" r="4" fill="var(--proj)" class="ring"><title>' +
          esc(lab(months[lastI])) + ' GP3 projected: ' + pf(pjPct('gp3')) + ', band ' + skewTxt() + '</title></circle>' +
          '<text class="tick" x="' + cxl.toFixed(1) + '" y="' + (wLo+13).toFixed(1) +
          '" text-anchor="middle" style="fill:var(--proj-ink);font-weight:700">projected</text>');
      }
    }
  }
  g1.forEach(function (v, i) {
    var isP = (openLast && pa && i === lastI);
    o.push('<circle cx="' + tx(i).toFixed(1) + '" cy="' + by(v).toFixed(1) + '" r="' + (isP ? 4 : 3.2) + '" ' +
      (isP ? 'fill="var(--proj)" class="ring"' : 'class="dot-1 ring"') + '><title>' + esc(lab(months[i])) + ' GP1' +
      (isP ? ' projected: ' + pf(v) + ' (posted to date reads ' + pf(g1posted) + ', inflated by the posting cut-off)' : ': ' + pf(v)) +
      '</title></circle>');
  });
  o.push('<text class="dlab" x="' + (tx(0)-6).toFixed(1) + '" y="' + (by(g1[0])-8).toFixed(1) + '" text-anchor="start">GP1%</text>');
  o.push('<line class="axis" x1="' + APL + '" y1="' + by(pmin).toFixed(1) + '" x2="' + (TW-APR) + '" y2="' + by(pmin).toFixed(1) + '"></line>');
  months.forEach(function (m, i) {
    var q = m.split('-');
    o.push('<text class="tickb" x="' + tx(i).toFixed(1) + '" y="' + (THt-18) + '" text-anchor="middle">' + H.MN[+q[1]-1] + '</text>');
    if (q[1] === '01' || i === 0)
      o.push('<text class="tick" x="' + tx(i).toFixed(1) + '" y="' + (THt-5) + '" text-anchor="middle">' + q[0] + '</text>');
  });
  o.push('</svg>');
  el('trChart').innerHTML = o.join('');

  var lg = ['<span class="k"><span class="sw" style="background:var(--s1)"></span>Net Sales, posted</span>'];
  if (showFC) lg.push('<span class="k"><span class="swl" style="background:var(--s3)"></span>Forecast Net Sales</span>');
  lg.push('<span class="k"><span class="swl" style="background:var(--s1)"></span>GP1%</span>');
  if (hasG3) lg.push('<span class="k"><span class="swl" style="background:var(--s2)"></span>GP3%</span>');
  if (pa) lg.push('<span class="k"><span class="sw" style="background:var(--proj)"></span>Projected, open month</span>');
  lg.push('<span class="k"><svg width="13" height="13"><defs><pattern id="lg2" width="9" height="9" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">' +
    '<rect width="9" height="9" fill="var(--s1)"></rect><line x1="0" y1="0" x2="0" y2="9" stroke="var(--surf)" stroke-width="3.8"></line></pattern></defs>' +
    '<rect width="13" height="13" rx="2" fill="url(#lg2)"></rect></svg>Not closed</span>');
  el('trLegend').innerHTML = lg.join('');

  el('trTitle').innerHTML = 'Net Sales and margin &middot; ' + months.length + ' months' +
    (mkt === 'ALL' ? '' : ' &middot; ' + esc(mkt));
  el('trNote').innerHTML = months[0] === D().months[0]
    ? ('History opens at ' + esc(lab(months[0])) + ', when Business Central went live, so that month is a part month. ')
    : '';
  el('trNote').innerHTML += (pa
    ? ('<strong style="color:var(--ink)">The open month is drawn as projected.</strong> Its net-sales bar is split: the ' +
       'solid part is ' + sek(per[lastI].net) + ' posted to date, the hatched part is the projected remainder to ' +
       sek(pa.net) + '. Both margin lines run dashed into it because that point is projected, and the GP3 point carries ' +
       'its ' + skewTxt() + ' band as a whisker rather than sitting as a bare dot. ' +
       '<strong style="color:var(--ink)">Its GP1% plots at the projected ' + pf(pjPct('gp1')) + ', not at what has posted.</strong> ' +
       'Posted-to-date GP1 currently reads ' + pf(g1posted) + ', which is far too high: intra-month, cost posts behind the ' +
       'revenue it belongs to, and plotting that against closed months would show a spike that is purely a cut-off artefact.')
    : ('<strong style="color:var(--ink)">GP3% is not plotted for a single market</strong>: shipping, fulfilment and ' +
       'transaction fees have no country dimension, and the open month is not projected per market either.'));
}

R.markets = renderMarkets;
R.trend = renderTrend;
})();

/* ═══════════════════════════════════════════════════════════════════════════
 * Part 8: the watchlist, and the provenance panel.
 *
 * The watchlist is DETERMINISTIC. Every card is a named rule with a threshold,
 * evaluated against the live snapshot on each refresh, and it renders its own
 * evaluation so a reader can check the arithmetic. Nothing here is generated
 * commentary, and a rule that passes says so rather than disappearing.
 * ═══════════════════════════════════════════════════════════════════════════ */
(function () {
'use strict';
var H = window.__execplHelpers;
var R = window.__execplRender;
var esc = H.esc, sek = H.sek, msek = H.msek, pct = H.pct, pf = H.pf, el = H.el,
    lab = H.lab, agg = H.agg, fcFor = H.fcFor, wm = H.wm, pjOn = H.pjOn,
    pjPct = H.pjPct, estMonth = H.estMonth, isOpen = H.isOpen,
    gp1Observed = H.gp1Observed, THEIRS = H.THEIRS, ONE_OFF = H.ONE_OFF,
    skewTxt = H.skewTxt;
function D(){ return H.D(); } function N(){ return H.N(); } function S(){ return H.S(); }

function card(sev, tag, title, fig, body, rid, rex){
  return '<article class="w ' + sev + '"><div class="stripe"></div><div class="in">' +
    '<div class="top"><span class="sev">' + esc(tag) + '</span></div>' +
    '<h3>' + title + '</h3><div class="fig">' + fig + '</div><p>' + body + '</p>' +
    '<div class="rule"><span class="rid">' + esc(rid) + '</span><span class="rex">' + rex + '</span></div>' +
    '</div></article>';
}

function renderWatchlist(){
  var d = D(), ms = wm(S().period), ytd = wm('YTD');
  var a = agg(ytd, 'ALL'), b = fcFor(ytd, 'ALL');
  var em = estMonth(), out = [], fired = 0, total = 0;

  function push(html, didFire){ total++; if (didFire) fired++; out.push(html); }

  /* 1 · A single non-recurring item must not dominate the headline. */
  if (ytd.indexOf(ONE_OFF.month) >= 0 && a.ebitda) {
    var share = Math.abs(ONE_OFF.sek / a.ebitda) * 100, hit = share > 25;
    push(card(hit ? 'crit' : 'ok', hit ? 'One-off' : 'Cleared',
      'YTD EBITDA is ' + share.toFixed(0) + '% one-off ' + esc(ONE_OFF.what),
      sek(ONE_OFF.sek),
      'A single ' + esc(ONE_OFF.what) + ' posted to GL ' + esc(ONE_OFF.gl) + ' in ' + esc(lab(ONE_OFF.month)) +
      ' carries <b>' + sek(ONE_OFF.sek) + '</b> of income. It sits inside EBITDA but outside the GP3 ladder, in ' +
      '<em>Other operating items</em>. YTD EBITDA is ' + sek(a.ebitda) + '; strip it and <b>underlying EBITDA is ' +
      sek(a.ebitda - ONE_OFF.sek) + '</b>. The forecast carries no such line.',
      'EBITDA_ONE_OFF_MATERIALITY',
      'Rule &middot; no single non-recurring item &gt; 25% of YTD EBITDA<br>Evaluated &middot; ' +
      sek(ONE_OFF.sek) + ' &divide; ' + sek(a.ebitda) + ' = <em>' + share.toFixed(1) + '%</em> (threshold 25%)'), hit);
  }

  /* 2 · Logistics must have posted for any month older than a few days. */
  if (em) {
    var c = d.components[em.month] || {};
    var logis = ['4497','4498','4499'].reduce(function (t, k) { return t + ((c.fulfillment || {})[k] || 0); }, 0);
    var hit2 = (logis === 0);
    push(card(hit2 ? 'warn' : 'ok', hit2 ? 'Open month' : 'Cleared',
      hit2 ? esc(lab(em.month)) + ' logistics has not posted, so GP2 and GP3 publish as a projection'
           : esc(lab(em.month)) + ' logistics has posted',
      hit2 ? '0 SEK posted' : sek(logis),
      hit2
        ? ('3PL outbound, returns and warehousing are all <b>zero</b> for ' + esc(lab(em.month)) +
           ', and marketing shows ' + sek(-agg([em.month],'ALL').mktg) + '. GP2 and GP3 are therefore published as a ' +
           '<em>projection</em>, marked <span class="mk mk-p">P</span>, led by the margin percentage and carried with a ' +
           'skewed band; the posted share of every rung is shown beside it, and below GP1 that share is <b>0%</b>. ' +
           '<b>EBITDA stays suppressed</b>: the estimator stops at GP3. Cumulative windows stay posted-only.')
        : 'GL 4497, 4498 and 4499 have all posted for this month, so it publishes in full rather than as a projection.',
      'LOGISTICS_POSTED',
      'Rule &middot; sum(GL 4497 + 4498 + 4499) &gt; 0 for the open month<br>Evaluated &middot; ' +
      esc(em.month) + ' &rarr; <em>' + sek(logis) + ' SEK</em>' +
      (hit2 ? ' &middot; response: project and label, never suppress silently' : ', passing')), hit2);
  }

  /* 3 · A projection whose GP1 sits outside everything ever observed. */
  if (em && isOpen(em.month)) {
    var ob = gp1Observed(), p1 = pjPct('gp1'), hit3 = (p1 > ob.hi || p1 < ob.lo);
    push(card(hit3 ? 'warn' : 'ok', 'Projection quality',
      hit3 ? 'The projected band is skewed downward' : 'The projection sits inside the observed range',
      skewTxt(),
      hit3
        ? ('Projected GP1 of <b>' + pf(p1) + '</b> sits <em>outside</em> the 2026 observed range of ' +
           ob.lo.toFixed(1) + ' to ' + ob.hi.toFixed(1) + '%. The published band is therefore asymmetric, <b>' +
           skewTxt() + '</b> rather than the symmetric backtest band, and the central case should be read as the ' +
           'optimistic edge rather than the middle.')
        : ('Projected GP1 of ' + pf(p1) + ' is inside the 2026 observed range of ' + ob.lo.toFixed(1) + ' to ' +
           ob.hi.toFixed(1) + '%. The skew is retained on the inventory-adjustment argument alone.'),
      'PROJECTION_QUALITY',
      'Rule &middot; projected GP1% within the observed monthly range for the year<br>Evaluated &middot; <em>' +
      pf(p1) + '</em> against ' + ob.lo.toFixed(1) + '&ndash;' + ob.hi.toFixed(1) + '% (' +
      (hit3 ? 'outside' : 'inside') + ')'), hit3);
  }

  /* 4 · Transaction fees against Babyshop's own reported figure. */
  var jul = agg(['2026-07'], 'ALL');
  if (d.months.indexOf('2026-07') >= 0) {
    var tfGap = Math.abs(jul.tf - THEIRS.tf), hit4 = tfGap > 50000;
    push(card(hit4 ? 'warn' : 'ok', hit4 ? 'Reconciling' : 'Cleared',
      'Transaction fees differ from Finance by ' + Math.round(tfGap/1000) + ' TSEK',
      (jul.tf > 0 ? '+' : '−') + Math.round(Math.abs(jul.tf)/1000) + ' vs ' + (THEIRS.tf > 0 ? '+' : '−') + Math.round(Math.abs(THEIRS.tf)/1000),
      'For July, GL 4372-4375 nets to a <b>credit</b> of ' + sek(Math.abs(jul.tf)) + ': Walley&rsquo;s revenue share exceeds ' +
      'the Adyen and Amex fees. Finance also books this line as a small net income, at ' + sek(Math.abs(THEIRS.tf)) + '. ' +
      'The remainder is a <b>definitional gap on one month, not a missing cost</b>: no GL account carries it. It is the ' +
      'larger half of the GP3 gap that carries into our EBITDA.',
      'TRANSACTION_FEE_DEFINITION',
      'Rule &middot; abs(our transaction-fee total &minus; their reported figure) &le; 50 TSEK per month<br>Evaluated &middot; ' +
      '2026-07 &rarr; <em>' + Math.round(tfGap/1000) + ' TSEK</em> (threshold 50)'), hit4);

    /* 5 · Every direct-cost line against their report. */
    var lines = [['nship','Net Shipping'],['ful','Fulfillment'],['mktg','Marketing'],['cogs','COGS']];
    var worst = null;
    lines.forEach(function (L) {
      var g = jul[L[0]] - THEIRS[L[0]];
      if (!worst || Math.abs(g) > Math.abs(worst.g)) worst = { n:L[1], g:g };
    });
    var hit5 = Math.abs(worst.g) > 25000;
    push(card(hit5 ? 'warn' : 'ok', hit5 ? 'Ledger moved' : 'Cleared',
      hit5 ? esc(worst.n) + ' has drifted out of tie' : 'Every direct-cost line ties',
      (worst.g > 0 ? '+' : '−') + (Math.abs(worst.g)/1000).toFixed(1) + ' TSEK',
      hit5
        ? ('<b>' + esc(worst.n) + '</b> is ' + sek(Math.abs(worst.g)) + ' from Babyshop&rsquo;s July figure. Carrier and ' +
           'freight invoices post into July after their report is issued, so the mapping has not changed: the ledger has. ' +
           'This panel recomputes from the live snapshot every refresh, which is why the number moves.')
        : 'Every direct-cost line is within 25 TSEK of Babyshop&rsquo;s reported July figure.',
      'DIRECT_COST_MAPPING',
      'Rule &middot; each direct-cost line within 25 TSEK of Babyshop&rsquo;s reported figure<br>Evaluated &middot; ' +
      lines.map(function (L) {
        return esc(L[1]) + ' <em>' + ((jul[L[0]]-THEIRS[L[0]])/1000).toFixed(1) + '</em>'; }).join(' &middot; ') +
      ' (threshold 25)'), hit5);
  }

  /* 6 · Accounts no ladder line claims. EBT is not the posted GL result while
   *     any of them post, and this quantifies the gap for the chosen window. */
  var gapByMonth = (d.checks || {}).ebt_vs_gl_net_result || {};
  var unclaimedWin = ms.reduce(function (t, m) { return t + (gapByMonth[m] || 0); }, 0);
  var unclaimedYtd = ytd.reduce(function (t, m) { return t + (gapByMonth[m] || 0); }, 0);
  var accts = Object.keys((d.checks || {}).unclaimed_pl_accounts || {})
    .filter(function (k) { return Math.abs(d.checks.unclaimed_pl_accounts[k]) >= 1000000; }).sort();
  var hit6 = Math.abs(unclaimedYtd) > 1000;
  push(card(hit6 ? 'warn' : 'ok', hit6 ? 'Unclaimed' : 'Cleared',
    hit6 ? 'Ladder EBT is not the posted general-ledger result' : 'Ladder EBT ties to the general ledger',
    (unclaimedYtd > 0 ? '+' : '−') + sek(Math.abs(unclaimedYtd)),
    hit6
      ? ('P&amp;L accounts with real activity that no ladder rung claims: <b>GL ' + accts.join(', ') + '</b> among them. ' +
         'Across YTD they carry <b>' + sek(Math.abs(unclaimedYtd)) + '</b>, so ladder EBT sits that far from the booked GL ' +
         'net result. For the selected window the gap is <b>' + sek(Math.abs(unclaimedWin)) + '</b>. A tie test run on a ' +
         'single month where none of them post would pass and would be telling you nothing.')
      : 'Every P&amp;L account with activity is claimed by a ladder rung for this window.',
    'EBT_GL_TIE',
    'Rule &middot; abs(ladder EBT &minus; sum of all P&amp;L GL accounts) &le; 1 TSEK<br>Evaluated &middot; YTD &rarr; <em>' +
    sek(Math.abs(unclaimedYtd)) + ' SEK</em> across ' +
    Object.keys((d.checks || {}).unclaimed_pl_accounts || {}).length + ' unclaimed accounts (threshold 1 TSEK)'), hit6);

  /* 7 · COGS against plan. */
  if (b) {
    /* Stated in COST terms, so "under" unambiguously means less cost spent. */
    var costAct = -a.cogs, costFc = -b.cogs;
    var cg = (costAct - costFc) / Math.abs(costFc) * 100, hit7 = Math.abs(cg) > 5;
    push(card(hit7 ? 'warn' : 'ok', 'Structural',
      'COGS is running ' + (cg > 0 ? 'over' : 'under') + ' forecast by ' + Math.abs(cg).toFixed(1) + '%',
      (cg > 0 ? '+' : '−') + Math.abs(cg).toFixed(1) + '%',
      'On Finance&rsquo;s own GL basis, cost of goods is ' + msek(costAct) + ' M against a forecast of ' +
      msek(costFc) + ' M. Not a mapping error: the basis reproduces their July COGS closely. Net sales are ' +
      ((a.net - b.net) / b.net * 100).toFixed(1) + '% against forecast while COGS is ' + cg.toFixed(1) + '%, so the ' +
      'shortfall is <b>volume, not margin</b>. GP1 lands ' + msek(Math.abs(a.gp1 - b.gp1)) + ' M ' +
      (a.gp1 > b.gp1 ? 'ahead' : 'behind') + ' on ' + msek(Math.abs(a.net - b.net)) + ' M ' +
      (a.net > b.net ? 'more' : 'less') + ' revenue.',
      'COGS_PLAN_VARIANCE',
      'Rule &middot; abs(BC COGS &minus; forecast COGS) &le; 5.0% over the YTD window<br>Evaluated &middot; ' +
      sek(costAct) + ' vs ' + sek(costFc) + ' &rarr; <em>' + cg.toFixed(1) + '%</em> (threshold 5.0%)'), hit7);
  }

  /* 8 · Shipping sold below cost. */
  var shipRev = a.shipRevNet, shipOut = (a.ship_cost || {})['4336'] || 0;
  if (shipOut) {
    var rec = shipRev / shipOut * 100, hit8 = rec < 60;
    var withRet = shipRev / (shipOut + ((a.ship_cost || {})['4338'] || 0)) * 100;
    push(card(hit8 ? 'warn' : 'ok', 'Margin drag',
      'Shipping is sold ' + (hit8 ? 'far below cost' : 'near cost'),
      rec.toFixed(1) + '% recovered',
      sek(shipRev) + ' of shipping revenue net of refunds against ' + sek(shipOut) + ' of customer freight. ' +
      'Adding returns freight the recovery falls to ' + withRet.toFixed(1) + '%. This is a structural pricing choice ' +
      'rather than a data problem, and it is visible against the forecast&rsquo;s own four shipping sub-lines rather than ' +
      'a single blended direct-cost line.',
      'SHIPPING_COST_RECOVERY',
      'Rule &middot; shipping revenue &divide; GL 4336 &ge; 60% over the selected window<br>Evaluated &middot; ' +
      msek(shipRev) + 'M &divide; ' + msek(shipOut) + 'M = <em>' + rec.toFixed(1) + '%</em> (threshold 60%)'), hit8);
  }

  /* 9 · Any market-month GP1% outside a plausible band. This is the rule that
   *     would have caught the item-ledger mis-attribution before it published. */
  /* Evaluated per market-MONTH, not on the window total. A single contaminated
   * month averages away over a year: the wholesale clearance that put Germany
   * deep negative in May is invisible in its YTD figure.
   *
   * The materiality floor is on max(net sales, COGS), NOT on net sales alone.
   * A clearance sold at roughly 1 SEK a unit against full cost produces tiny
   * revenue and large cost, so a revenue-only floor filters out precisely the
   * month the rule exists to catch. That mistake was made here first. */
  var bad = [];
  (d.market_countries || []).forEach(function (k) {
    if (k === 'UNATTRIBUTED') return;
    ms.forEach(function (m) {
      var r = agg([m], k);
      if (Math.max(r.net || 0, -r.cogs || 0) < 250000) return;
      var g = pct(r.gp1, r.net);
      if (g == null || !isFinite(g)) return;
      if (g < 0 || g > 70) bad.push({ k:k, m:m, g:g });
    });
  });
  var hit9 = bad.length > 0;
  push(card(hit9 ? 'crit' : 'ok', hit9 ? 'Implausible' : 'Cleared',
    hit9 ? bad.length + ' market-month' + (bad.length > 1 ? 's' : '') + ' outside the plausible margin band'
         : 'Every market-month sits in a plausible margin band',
    hit9 ? bad.slice(0, 3).map(function (x) { return x.k + ' ' + esc(x.m.slice(2)) + ' ' + x.g.toFixed(1) + '%'; }).join(' · ') +
           (bad.length > 3 ? ' · +' + (bad.length - 3) + ' more' : '')
         : (function () {
             var all = [];
             (d.market_countries || []).forEach(function (k) {
               if (k === 'UNATTRIBUTED') return;
               ms.forEach(function (m) {
                 var r = agg([m], k);
                 if (Math.max(r.net || 0, -r.cogs || 0) < 250000) return;
                 var v = pct(r.gp1, r.net);
                 if (v != null && isFinite(v)) all.push(v);
               });
             });
             return all.length ? all.length + ' market-months, ' + Math.min.apply(null, all).toFixed(0) + ' to ' +
                    Math.max.apply(null, all).toFixed(0) + '%' : 'nothing material';
           })(),
    hit9
      ? ('Market GP1% is built from the item ledger, the only cost basis carrying a country. A market-month outside 0 to ' +
         '70% is either a contaminated transaction (a wholesale clearance at near-zero price against full cost will do it) ' +
         'or a country-attribution failure. <b>Check which before quoting the row.</b> Firing: ' +
         bad.map(function (x) { return '<b>' + esc(x.k) + ' ' + esc(x.m) + '</b> at ' + x.g.toFixed(1) + '%'; }).join(', ') + '.')
      : ('Market GP1% is built from the item ledger, the only cost basis carrying a country. The country comes from the ' +
         'customer on each entry, not from its document number: the item ledger books a sale under its posted shipment ' +
         'number, and that series overlaps numerically with the invoice series about four months back, so a document-number ' +
         'join matches almost every row to an unrelated older invoice. That is what this rule exists to catch.'),
    'MARKET_MARGIN_BAND',
    'Rule &middot; every market-month with net sales or COGS over 250 TSEK has GP1% within 0 to 70%<br>Evaluated &middot; ' +
    (hit9 ? bad.map(function (x) { return '<em>' + esc(x.k) + ' ' + esc(x.m) + ' ' + x.g.toFixed(1) + '%</em>'; }).join(' &middot; ')
          : '<em>all pass</em>') + ' (band 0&ndash;70%)'), hit9);

  /* 10 · The two cost bases must not drift far apart. */
  var gapSrc = (d.checks || {}).item_ledger_vs_gl_cogs || {};
  var gaps = ms.map(function (m) { return gapSrc[m] && gapSrc[m].pct; }).filter(function (v) { return v != null; });
  if (gaps.length) {
    var worstGap = gaps.reduce(function (x, y) { return Math.abs(y) > Math.abs(x) ? y : x; }, 0);
    var hit10 = Math.abs(worstGap) > 5;
    push(card(hit10 ? 'warn' : 'info', 'Cut-off',
      'The two COGS bases diverge by up to ' + Math.abs(worstGap).toFixed(1) + '% in a month',
      (worstGap > 0 ? '+' : '−') + Math.abs(worstGap).toFixed(1) + '%',
      'The group ladder takes COGS from the general ledger; the market rows take it from the item ledger, the only basis ' +
      'with a country. They measure the same thing on different posting cut-offs and swing month to month. ' +
      '<b>The ladder is unaffected</b>, being GL throughout, and the market rows are not scaled onto it, ' +
      'because that would turn a measurement into an allocation. It is why no single market-month should be read alone.',
      'PERIOD_CUTOFF_SKEW',
      'Rule &middot; abs(item-ledger COGS &minus; GL COGS) &le; 5.0% in each month<br>Evaluated &middot; ' +
      ms.map(function (m) { var g = gapSrc[m] && gapSrc[m].pct;
        return g == null ? '' : esc(m.slice(5)) + ' <em>' + g.toFixed(1) + '%</em>'; })
        .filter(Boolean).join(' &middot; ') + ' (threshold 5.0%)'), hit10);
  }

  /* 11 · The live order feed, and the basis rule it exists under. */
  var n = N();
  if (n) {
    var okN = !!n.available;
    var dg = n.diagnostics || {};
    push(card(okN ? 'ok' : 'crit', okN ? 'Live' : 'Unavailable',
      okN ? 'The order-date day cards are live' : 'The order-date day cards have no live read',
      okN ? dg.http_calls + ' calls, ' + dg.elapsed_s + 's' : 'no figure',
      okN
        ? ('Order intake is read straight from the Norce API on a ' + esc(n.cache_ttl_s) + '-second cache, ' +
           sek(dg.orders_read) + ' orders over a two-day window. <b>It is a different basis from the ladder</b> and is ' +
           'never summed with it. The header freight ties to the shipping line ' +
           (dg.freight_vs_shipping_line_sek === 0 ? 'exactly' : 'within ' + sek(dg.freight_vs_shipping_line_sek) + ' SEK') +
           ', which is what confirms the revenue definition.')
        : ('The card shows no number and states why. Nothing is substituted: not a stale figure, not a scaled one, ' +
           'and not the posting-date figure from the ladder, which measures something else. Reason given: ' +
           esc(n.reason || 'unknown') + '.'),
      'DAY_GRAIN_LIVE_READ',
      'Rule &middot; the live Norce read succeeds, and header freight equals the shipping line<br>Evaluated &middot; ' +
      (okN ? '<em>available</em>, freight delta <em>' + sek(dg.freight_vs_shipping_line_sek) + ' SEK</em>'
           : '<em>unavailable</em>') + ' &middot; never substitute a scaled or stale figure'), !okN);
  }

  /* 12 · Prior-year cost lines are indicative only. */
  var pyMonths = H.pym(ms);
  if (pyMonths && pyMonths.some(function (m) { return m.slice(0,4) === '2025'; })) {
    push(card('warn', 'Indicative only',
      'Prior-year cost lines do not reconcile',
      '2025 margin is indicative',
      'Business Central went live mid-June 2025 and cost posting was still being migrated through that summer. ' +
      'Prior-year <b>revenue is sound</b>; prior-year <b>margin is indicative only</b>, and any GP1 or GP3 comparison ' +
      'against 2025 should be read as directional rather than as a like-for-like variance.',
      'PRIOR_YEAR_COST_COMPLETENESS',
      'Rule &middot; prior-year window contains only months after cost migration completed<br>Evaluated &middot; ' +
      'window includes <em>' + esc(pyMonths.filter(function (m) { return m.slice(0,4) === '2025'; }).join(', ')) +
      '</em> &middot; revenue usable, margin indicative'), true);
  }

  el('watch').innerHTML = out.join('');
  el('wlTitle').innerHTML = total + ' rules, evaluated on this refresh';
  el('wlSub').innerHTML = 'Deterministic thresholds against the live snapshot, not generated commentary &middot; <b>' +
    fired + ' firing</b>, ' + (total - fired) + ' passing &middot; snapshot generated ' +
    esc((d.generated_at || '').replace('T',' ').replace('+00:00',' UTC'));
}

R.watchlist = renderWatchlist;
})();

/* ═══════════════════════════════════════════════════════════════════════════
 * Part 9: provenance, the ladder-to-ledger bridge, and the notes.
 * ═══════════════════════════════════════════════════════════════════════════ */
(function () {
'use strict';
var H = window.__execplHelpers;
var R = window.__execplRender;
var esc = H.esc, sek = H.sek, msek = H.msek, pct = H.pct, pf = H.pf, el = H.el,
    lab = H.lab, agg = H.agg, wm = H.wm, estMonth = H.estMonth, skewTxt = H.skewTxt;
function D(){ return H.D(); } function N(){ return H.N(); } function S(){ return H.S(); }

function renderProvenance(){
  var d = D(), ms = wm(S().period), a = agg(ms, 'ALL');
  var n = N(), em = estMonth();
  var bc = (d.sources || {}).bc || {}, fm = (d.forecast || {})._meta || {};

  el('howL').innerHTML = [
    '<p><strong>This is posted revenue, not attributed revenue.</strong> Every figure originates in Business Central ' +
    'sales invoices, credit memos, item-ledger entries and the general ledger, which is what Finance has posted. The KV ' +
    'Overview tab reports what marketing attribution assigned. The two answer different questions and will not match ' +
    'to the krona.</p>',

    '<p><strong>Two bases live on this page, and they are never mixed.</strong> The ladder, the scorecard, the bridge, ' +
    'the markets and the trend are all <em>posting date</em>, because that is what reconciles to Finance&rsquo;s own ' +
    'management report. The day cards are <em>order date</em>, because that is what a day means to a reader. ' +
    'Babyshop&rsquo;s word <em>booked</em> means when the purchase took place, so it is reserved for the day cards; ' +
    'the ledger basis is called <em>posted</em> or <em>invoiced</em> throughout. ' +
    (n && n.basis_warning ? esc(n.basis_warning) : '') + '</p>',

    '<p><strong>The ladder closes to the general ledger.</strong> Gross Sales through GP3 is built from invoice lines and ' +
    'named GL accounts. EBITDA is <em>swept</em> from the ledger rather than built up, so it ties to the posted operating ' +
    'result by construction, and <em>Other operating items</em> is the visible line that makes the ladder meet it. ' +
    'Total Marketing and Other overhead are residual-based for the same reason: an account Finance opens next month lands ' +
    'somewhere visible instead of vanishing.</p>',

    '<p><strong>Amounts are FX-normalised.</strong> Invoices are booked in six currencies at document value; every line is ' +
    'converted at Business Central&rsquo;s own daily rate for its posting date. Skipping that understates gross sales by ' +
    'roughly 28%. ' + esc((d.checks || {}).fx_note || '') + '</p>',

    '<p><strong>Posted, projected and forecast are three different things.</strong> ' +
    '<span class="st st-bk">Posted</span> is what Finance has in the ledger. <span class="st st-pj">Projected</span> is ' +
    'posted plus a fitted estimate, and it always shows the posted share, so a projection can never be mistaken for a ' +
    'posted figure. <span class="st st-fc">Forecast</span> is Babyshop&rsquo;s own plan. They are never added together ' +
    'or blended. Only the open month is ever projected, only down to GP3, and only at group level: a projection never ' +
    'enters a cumulative window and is never built for a single market.</p>',

    '<p><strong>Read the margin, not the krona, on anything projected.</strong> Revenue error and cost error partly cancel ' +
    'in a ratio and do not in an absolute, because over-projecting revenue scales the driver-based cost lines with it. ' +
    'The percentage is therefore the headline everywhere a projection appears and the SEK figure sits underneath it, and ' +
    'the band is skewed ' + skewTxt() + ' rather than symmetric.</p>',

    '<p><strong>Allocated figures are marked as allocated.</strong> Per-market logistics is a flat by-order-count spread of ' +
    'a group total, not a measurement: BC posts logistics at carrier-invoice granularity with no geography and ' +
    '<code>shipmentMethodId</code> is empty on every row. It is carried in the snapshot flagged ' +
    '<code>allocated: true</code> and is not published on the market rows. Market COGS and market marketing are real ' +
    'measures on their own bases, and those bases differ from the ladder&rsquo;s, which is stated on the table rather than ' +
    'scaled away.</p>',

    '<p><strong>Markers mean something.</strong> ' +
    '<span class="mk mk-b">B</span> a basis difference rather than performance &middot; ' +
    '<span class="mk mk-d">D</span> directional, comparable in level but not in composition &middot; ' +
    '<span class="mk mk-c">!</span> contaminated, or known to be incomplete &middot; ' +
    '<span class="mk mk-d">?</span> a mapping that cannot be made clean &middot; ' +
    '<span class="mk mk-d">O</span> window contains an unposted month &middot; ' +
    '<span class="mk mk-p">P</span> projected, not posted. Nothing here is allocated silently.</p>'
  ].join('');

  /* ── Ladder EBT against the posted GL result ─────────────────────────────
   * The prototype showed a revenue-side bridge that closed to a few SEK. That
   * bridge is not reproducible from this snapshot and, more importantly, it was
   * the wrong test: it passed on a month where the unclaimed accounts happened
   * not to post. This is the test that does not flatter itself.
   */
  var gapM = (d.checks || {}).ebt_vs_gl_net_result || {};
  var glM = (d.checks || {}).gl_net_result || {};
  var glSum = ms.reduce(function (t, m) { return t + (glM[m] || 0); }, 0);
  var gapSum = ms.reduce(function (t, m) { return t + (gapM[m] || 0); }, 0);
  var unc = (d.checks || {}).unclaimed_pl_accounts || {};
  var big = Object.keys(unc).filter(function (k) { return Math.abs(unc[k]) >= 1000000; }).sort();

  var rows = ['<div><span>Ladder EBT, this window</span><span class="n">' + sek(a.ebt) + '</span></div>'];
  big.forEach(function (k) {
    rows.push('<div><span>GL ' + esc(k) + ', claimed by no rung</span><span class="n">' +
      (unc[k] > 0 ? '−' : '+') + sek(Math.abs(unc[k])) + '</span></div>');
  });
  rows.push('<div><span>Other unclaimed accounts, all history</span><span class="n">' +
    sek(Object.keys(unc).filter(function (k) { return Math.abs(unc[k]) < 1000000; })
      .reduce(function (t, k) { return t + unc[k]; }, 0)) + '</span></div>');
  rows.push('<div><span>Gap on this window</span><span class="n">' +
    (gapSum > 0 ? '+' : '−') + sek(Math.abs(gapSum)) + '</span></div>');
  rows.push('<div><span>Posted GL net result, this window</span><span class="n">' + sek(glSum) + '</span></div>');
  el('glBridge').innerHTML = rows.join('');

  el('glBridgeNote').innerHTML =
    'Ladder EBT does <b>not</b> equal the posted general-ledger result, and the difference is named rather than hidden. ' +
    (big.length ? 'GL ' + big.join(', ') + ' carry real money that no ladder rung claims. ' : '') +
    'Over this window that is <b>' + sek(Math.abs(gapSum)) + '</b>. A tie test run on a single month where none of them ' +
    'post would pass and would be telling you nothing, which is exactly what happened when the ladder was first built ' +
    'against July alone. The individual account totals above cover the whole history window, not just this selection.';

  /* ── Notes ─────────────────────────────────────────────────────────────── */
  var notes = [];
  notes.push('<div><b>SOURCE</b> Business Central <code>bc_*</code> in <code>' + esc(bc.project || '') + '.' +
    esc(bc.dataset || '') + '</code>, read nightly by <code>exec-pl-refresh</code> into one Firestore document that this ' +
    'page fetches in a single read. Max posting date <b>' + esc(bc.max_posting_date || '–') + '</b>, last modified <b>' +
    esc((bc.last_modified || '–').slice(0, 19)) + '</b>. Snapshot generated ' +
    esc((d.generated_at || '').replace('T',' ')) + '.</div>');

  notes.push('<div><b>FORECAST</b> ' + esc(fm.vintage || '') + (fm.source ? ', <code>' + esc(fm.source) + '</code>' : '') +
    '. <b>Global only</b>: no market, shop or channel key exists, so selecting a single market removes every forecast ' +
    'comparison rather than splitting one. <b>All twelve months are labelled prognos</b>, so even closed months are ' +
    'forecast rather than restated actuals.</div>');

  notes.push('<div><b>DAY GRAIN</b> ' + (n && n.available
    ? ('Order date, live from the Norce API, cached ' + esc(n.cache_ttl_s) + ' seconds. The default card is the ' +
       '<b>latest complete day</b>, named by its actual date; today is shown separately and marked provisional, and the ' +
       'only comparison it drives is against the same clock time on the previous day, where neither side is maturing. ' +
       'Revenue is merchandise plus header freight, ex VAT, gross of returns, every order status except 6. ' +
       '<b>No margin is shown</b>: line-level cost is unpopulated and the price-list cost covers about 61% of order value. ' +
       esc((n.lag || {}).note || '') + ' ' + esc((n.reconciliation || {}).note || ''))
    : 'Order date, live from the Norce API. Currently unavailable, and the card says so rather than substituting anything.') +
    '</div>');

  notes.push('<div><b>MARKET BREAKDOWN</b> Stops at contribution after marketing. Below that line BC has nothing carrying ' +
    'a country: logistics posts at carrier-invoice granularity, payment fees at settlement granularity, overhead at ' +
    'company level. Market COGS is the item ledger and market marketing is de-duplicated Funnel spend, the only ' +
    'two bases with a country, so market rows do not foot to the ladder, and the gap is posting cut-off and ' +
    'definition, not error. ' + esc((d.checks || {}).item_ledger_note || '') + '</div>');

  notes.push('<div><b>MARKET COUNTRY KEY</b> The item ledger books a sale under its posted <em>shipment</em> number, whose ' +
    'number series overlaps numerically with the invoice series about four months back. Joining the two on document ' +
    'number matches almost every row to an unrelated older invoice and silently reshuffles the country mix. The country ' +
    'here comes from the <em>customer</em> on each entry instead, and <code>MARKET_MARGIN_BAND</code> in the watchlist ' +
    'exists to catch it if that ever stops being true.</div>');

  if (em) {
    notes.push('<div><b>PROJECTION METHOD</b> Per line, standing at day <b>' + esc(em.as_of_day) + '</b>: ' +
      '<code>estimate = posted_to_date + (1 &minus; maturity) &times; rate &times; driver</code>. Posting maturity comes ' +
      'from closed months and uses <code>lastModifiedDateTime</code>, not posting date, because BC backdates. Rates are ' +
      'medians over the closed months before the open one, so a single inventory revaluation cannot reprice a line. ' +
      'Each line&rsquo;s driver, fitted rate and backtest error sit in its expandable row in the ladder. ' +
      '<b>Two lines are labelled rather than modelled</b> and one of them, the inventory-adjustment account, is why the ' +
      'confidence band has a floor and does not close at month end.</div>');
  }

  notes.push('<div><b>UNRECONCILED, AND STAYING VISIBLE</b> The transaction-fee definitional gap against Finance&rsquo;s ' +
    'July report; the P&amp;L accounts no ladder rung claims, which are quantified in the bridge above and mean ladder EBT ' +
    'is not the posted GL net result; prior-year cost lines, which are indicative only because BC went live mid-June 2025 ' +
    'with cost posting still migrating; and the one-off ' + esc(H.ONE_OFF.what) + ' inflating YTD EBITDA, which the ' +
    'forecast has no line for. None of these is resolved by this tab. All of them are rules in the watchlist.</div>');

  (d.caveats || []).forEach(function (c) {
    notes.push('<div><b>CAVEAT</b> ' + esc(c) + '</div>');
  });

  el('notes').innerHTML = notes.join('');
}

R.provenance = renderProvenance;
})();
