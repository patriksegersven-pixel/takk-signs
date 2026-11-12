/* bm-pacing.js — "Pacing & forecast" section of the Black Month tab.
 *
 * Payload: bm_pacing.py. Everything is per market in data.markets[m], so the
 * market filter never refetches. The target is one daily shape of the monthly
 * forecast (data.shapes; a toggle appears only if there is more than one); the metric picker drives the charts
 * and the daily table. Today's live intake comes from /api/norce-today
 * (ordered basis), restated against today's Funnel-basis target with the
 * measured Norce/Funnel ratio from the payload.
 */
(function () {
  'use strict';
  var F = BM.fmt, esc = BM.esc;
  var ui = { shape: 'event', metric: 'gross' };
  var live = null, liveLoading = false;
  var spend = null, spendLoading = false;   // /api/blackmonth/pacing/spend-live (Google Ads + Meta)

  var METRICS = [
    { k: 'gross', label: 'Gross sales' },  // Norce merchandise
    { k: 'net', label: 'Net sales' },
    { k: 'gp1', label: 'GP1' },
    { k: 'gp2', label: 'GP2' },
    { k: 'cost', label: 'Marketing cost (all channels)', short: 'Marketing', cost: true },
    { k: 'gp3', label: 'GP3' }
  ];
  var EVENT = {
    black_friday: 'Black Friday', cyber_monday: 'Cyber Monday', bf_eve: 'BF eve',
    bf_saturday: 'BF Saturday', bf_sunday: 'BF Sunday', singles_day: "Singles' Day",
    fars_dag: "Father's Day (SE)", payday: 'Payday (SE)', alla_helgons: 'All Saints'
  };
  var EVENT_SHORT = {
    black_friday: 'BF', cyber_monday: 'CM', bf_eve: '', bf_saturday: '', bf_sunday: '',
    singles_day: '11.11', fars_dag: 'Fars dag', payday: 'Payday'
  };

  var CSS = '' +
    '#bm-pacing .pc-bar{display:flex;flex-wrap:wrap;gap:8px 16px;align-items:center;justify-content:space-between;margin-bottom:12px}' +
    '#bm-pacing .pc-seg{display:inline-flex;flex-wrap:wrap;gap:4px}' +
    '#bm-pacing .pc-seg button{background:var(--card-2);color:var(--body);border:1px solid var(--border);border-radius:6px;padding:4px 10px;font:inherit;font-size:12px;cursor:pointer}' +
    '#bm-pacing .pc-seg button.on{background:var(--accent-bg);color:var(--accent);border-color:var(--accent-dark)}' +
    '#bm-pacing .pc-basis{color:var(--muted);font-size:12px}' +
    '#bm-pacing .pc-tag{margin-top:6px}' +
    '#bm-pacing .pc-ladder tr.pc-ltot td{color:var(--h);font-weight:600}' +
    '#bm-pacing .pc-ladder tr.pc-lsub td:first-child{padding-left:18px;color:var(--muted)}' +
    '#bm-pacing .pc-head-hr{border-left-color:var(--up);margin-top:4px}' +
    '#bm-pacing .pc-bridge td{border-bottom:1px solid var(--border-lt)}' +
    '#bm-pacing table.pc-bridge td:first-child{white-space:normal}' +
    '#bm-pacing .pc-bridge tr.pc-btot td{color:var(--h);font-weight:600}' +
    '#bm-pacing .pc-bcell{width:40%;min-width:90px}' +
    '#bm-pacing .pc-bbar{height:8px;background:var(--card-2);border-radius:4px;overflow:hidden}' +
    '#bm-pacing .pc-bbar i{display:block;height:100%;border-radius:4px}' +
    '#bm-pacing .pc-bbar i.pos{background:var(--up)}#bm-pacing .pc-bbar i.neg{background:var(--down)}' +
    '#bm-pacing .pc-src{display:inline-block;font-size:10px;font-weight:600;letter-spacing:.03em;text-transform:uppercase;padding:1px 6px;border-radius:4px;vertical-align:middle}' +
    '#bm-pacing .pc-src-norce{background:var(--accent-bg);color:var(--accent)}' +
    '#bm-pacing .pc-src-funnel{background:var(--card);color:var(--muted);border:1px solid var(--border)}' +
    '#bm-pacing .pc-src-norce_bc,#bm-pacing .pc-src-model{background:var(--card);color:var(--accent);border:1px solid var(--accent-dark)}' +
    '#bm-pacing .pc-head{background:var(--card-2);border:1px solid var(--border-lt);border-left:3px solid var(--accent);border-radius:8px;padding:12px 14px;margin-bottom:12px;color:var(--h);font-size:14px;line-height:1.5}' +
    '#bm-pacing .pc-head b{color:var(--h)}' +
    '#bm-pacing .bm-kpi .val{font-size:22px}' +
    '#bm-pacing .bm-kpi .row{display:flex;justify-content:space-between;gap:8px;font-size:12px;margin-top:3px;color:var(--muted)}' +
    '#bm-pacing .bm-kpi .row span{white-space:nowrap}' +
    '#bm-pacing .bm-kpi .row span:last-child{text-align:right;font-variant-numeric:tabular-nums}' +
    '#bm-pacing .bm-table td:first-child,#bm-pacing .bm-table td.num{white-space:nowrap}' +
    '#bm-pacing .pc-today{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}' +
    '#bm-pacing .pc-legend{display:flex;flex-wrap:wrap;gap:12px;font-size:12px;color:var(--muted);margin:4px 0 6px}' +
    '#bm-pacing .pc-legend i{display:inline-block;width:14px;height:3px;vertical-align:middle;margin-right:5px;border-radius:2px}' +
    '#bm-pacing .bm-table tr.pc-now td{background:var(--accent-bg)}' +
    '#bm-pacing .bm-table tr.pc-ev td:first-child{color:var(--accent)}' +
    '#bm-pacing .bm-table tr.pc-future td{color:var(--muted)}' +
    '#bm-pacing .bm-table tr.pc-click{cursor:pointer}' +
    '#bm-pacing .pc-daily{max-height:520px;overflow:auto}' +
    '#bm-pacing .pc-daily thead th{position:sticky;top:0;background:var(--card);z-index:1}' +
    '#bm-pacing details.pc-notes summary{cursor:pointer;color:var(--muted);font-size:12px;margin-top:14px}' +
    '#bm-pacing details.pc-notes li{color:var(--muted);font-size:12px;margin:4px 0;line-height:1.45}' +
    '@media (max-width:640px){#bm-pacing .bm-kpis{grid-template-columns:1fr 1fr;gap:8px}#bm-pacing .bm-kpi{padding:10px}' +
    '#bm-pacing .bm-kpi .val{font-size:18px}#bm-pacing .bm-kpi .row{font-size:11px;gap:4px}#bm-pacing .bm-kpi .row .gap{display:none}' +
    '#bm-pacing .bm-chart{height:230px}}';

  function injectCss() {
    if (document.getElementById('bm-pacing-css')) return;
    var s = document.createElement('style');
    s.id = 'bm-pacing-css';
    s.textContent = CSS;
    document.head.appendChild(s);
  }

  /* ── small helpers ─────────────────────────────────────────────────────── */
  function sum(a, from, to) {
    var t = 0;
    for (var i = from || 0; i < (to == null ? a.length : to); i++) if (a[i] != null) t += a[i];
    return t;
  }
  function ratio(a, b) { return b ? a / b - 1 : null; }
  function signedSek(v) {
    if (v == null || isNaN(v)) return '–';
    return (v >= 0 ? '+' : '−') + F.sek(Math.abs(v));
  }
  // Colour a delta: for costs, over plan is a warning, not a win.
  function delta(v, isCost) {
    if (v == null || isNaN(v)) return '<span class="muted">–</span>';
    var s = (v >= 0 ? '+' : '−') + Math.abs(v * 100).toFixed(1).replace('.', ',') + ' %';
    var cls = isCost ? 'muted' : (v >= 0 ? 'up' : 'down');
    return '<span class="' + cls + '">' + s + '</span>';
  }
  function todayIso() { return new Date().toLocaleDateString('sv-SE', { timeZone: 'Europe/Stockholm' }); }
  function dm(iso) { var d = new Date(iso + 'T12:00:00'); return d.getDate() + '/' + (d.getMonth() + 1); }

  /* ── KPI cards ─────────────────────────────────────────────────────────── */
  // Every card names its source: Norce ordered (merchandise) or Funnel export.
  function basisTag(d, key) {
    var b = (d.basis || {})[key] || 'funnel';
    return '<span class="pc-src pc-src-' + b + '">' + esc((d.basis_label || {})[b] || b) + '</span>';
  }

  function kpiCard(label, val, rows, tag) {
    var h = '<div class="bm-kpi"><div class="lbl">' + esc(label) + '</div><div class="val">' + val + '</div>';
    rows.forEach(function (r) { h += '<div class="row"><span>' + r[0] + '</span><span>' + r[1] + '</span></div>'; });
    return h + (tag ? '<div class="pc-tag">' + tag + '</div>' : '') + '</div>';
  }

  function kpis(d, S) {
    var h = [];
    METRICS.forEach(function (m) {
      var s = S[m.k];
      h.push(kpiCard(m.label + ' MTD', F.sek(s.mtd), [
        ['vs target', '<em class="gap" style="font-style:normal">' + signedSek(s.mtd - s.target_mtd) + ' </em>' + delta(ratio(s.mtd, s.target_mtd), m.cost)],
        ['vs LY', delta(ratio(s.mtd, s.ly_mtd), m.cost)],
        [m.cost ? 'Projection' : 'Landing', F.sek(s.landing) + ' ' + delta(ratio(s.landing, s.target_month), m.cost)]
      ], basisTag(d, m.k)));
    });
    // COS stays Funnel ÷ Funnel (KV Overview definition), never Funnel cost ÷ Norce sales.
    var g = S.gross, fg = S.fgross, c = S.cost, o = S.orders, u = S.units;
    var cos = fg.mtd ? c.mtd / fg.mtd : null, cosT = g.target_mtd ? c.target_mtd / g.target_mtd : null;
    var cosL = fg.ly_mtd ? c.ly_mtd / fg.ly_mtd : null;
    var pp = function (a, b) {
      if (a == null || b == null) return '–';
      var x = (a - b) * 100;
      return '<span class="muted">' + (x >= 0 ? '+' : '−') + Math.abs(x).toFixed(1).replace('.', ',') + ' pp</span>';
    };
    h.push(kpiCard('COS % MTD', F.pct(cos), [
      ['plan', F.pct(cosT) + ' ' + pp(cos, cosT)],
      ['LY', F.pct(cosL) + ' ' + pp(cos, cosL)],
      ['ad cost ÷ gross', '']
    ], basisTag(d, 'fgross')));
    var aov = o.mtd ? g.mtd / o.mtd : null, aovL = o.ly_mtd ? g.ly_mtd / o.ly_mtd : null;
    h.push(kpiCard('Orders MTD', F.num(o.mtd), [
      ['vs LY', delta(ratio(o.mtd, o.ly_mtd))],
      ['LY full month', F.num(o.ly_month)],
      ['no order target', '']
    ], basisTag(d, 'orders')));
    h.push(kpiCard('AOV MTD', F.num(aov) + ' kr', [
      ['LY', F.num(aovL) + ' kr'],
      ['vs LY', delta(ratio(aov, aovL))],
      ['merch. ÷ orders', '']
    ], basisTag(d, 'gross')));
    h.push(kpiCard('Units MTD', F.num(u.mtd), [
      ['vs LY', delta(ratio(u.mtd, u.ly_mtd))],
      ['per order', o.mtd ? (u.mtd / o.mtd).toFixed(2).replace('.', ',') : '–'],
      ['LY per order', o.ly_mtd ? (u.ly_mtd / o.ly_mtd).toFixed(2).replace('.', ',') : '–']
    ], basisTag(d, 'units')));
    return '<div class="bm-kpis">' + h.join('') + '</div>';
  }

  function headline(d, S, mk) {
    var g = S.gross, p = S.gp2, q = S.gp3;
    if (!d.asof) return '<div class="pc-head">No November day is complete in both Norce and the Funnel export yet.</div>';
    var n = d.days.indexOf(d.asof) + 1, left = d.days.length - n;
    var yoyMtd = ratio(g.mtd, g.ly_mtd);
    var h = '<b>' + esc(BRAND[d.brand || 'all']) + ', ' + (mk === 'all' ? 'all markets' : mk) + ', through ' + esc(F.date(d.asof)) + ':</b> gross sales (Norce ordered, merchandise) ' +
      F.sek(g.mtd) + ', ' + delta(ratio(g.mtd, g.target_mtd)) + ' vs target and ' + delta(yoyMtd) + ' vs last year. ' +
      'GP3 (Funnel) ' + F.sek(q.mtd) + ' (' + delta(ratio(q.mtd, q.target_mtd)) + ' vs target). ';
    h += 'At the last ' + Math.min(d.run_rate_days, n) + ' days\' pace the month lands at <b>' + F.sek(g.landing) +
      '</b> gross / <b>' + F.sek(p.landing) + '</b> GP2 / <b>' + F.sek(q.landing) + '</b> GP3 against ' +
      F.sek(g.target_month) + ' / ' + F.sek(p.target_month) + ' / ' + F.sek(q.target_month) + ' forecast. ';
    if (left > 0) {
      h += 'To still hit the gross forecast the remaining ' + left + ' days need <b>' + F.sek(g.required_per_day) +
        '/day</b> (' + F.pct(g.required_ratio, 0) + ' of their daily target).';
    }
    if (mk === 'all' && !d.brand && d.yoy_forecast != null && yoyMtd != null && yoyMtd - d.yoy_forecast > 0.15) {
      h += ' <span class="warn">The forecast assumes ' + delta(d.yoy_forecast) + ' over last November; MTD is running ' +
        delta(yoyMtd) + '. Most of the month is still ahead.</span>';
    }
    return '<div class="pc-head">' + h + '</div>';
  }

  /* ── P&L ladder MTD: how gross becomes GP3, with the old Funnel figures ── */
  function ladderTable(d, S, mk) {
    var Mg = d.margins && d.margins[mk];
    var ci = d.cost_inputs || {};
    var g = S.gross.mtd;
    var pctOf = function (v) { return g ? F.pct(v / g) : '–'; };
    var rows = [
      ['Gross sales (merchandise)', 'gross', 'gross', 'fgross', false],
      ['− Returns (BC, posting date)', 'returns', null, null, true],
      ['Net sales', 'net', 'net', 'fnet', false],
      ['− COGS (Norce cost)', 'cogs', null, null, true],
      ['GP1', 'gp1', 'gp1', 'fgp1', false],
      ['− Direct variable cost', 'dv', null, null, true],
      ['GP2', 'gp2', 'gp2', 'fgp2', false],
      ['− Marketing cost (all channels)', 'cost', 'cost', 'cost', true],
      ['GP3', 'gp3', 'gp3', null, false]
    ];
    var h = ['<div class="bm-table-wrap"><table class="bm-table pc-ladder"><thead><tr><th>MTD</th><th class="num">Actual</th><th class="num">% of gross</th>' +
      '<th class="num">Target</th><th class="num">vs target</th><th class="num">LY</th><th class="num">Funnel export (old)</th><th>Source</th></tr></thead><tbody>'];
    rows.forEach(function (r) {
      var s = S[r[1]], t = r[2] ? S[r[2]] : null, isCost = r[4];
      var oldv = r[3] ? (r[3] === 'fgp1' || r[3] === 'fgp2' || r[3] === 'fnet' || r[3] === 'fgross' || r[3] === 'cost' ? S[r[3]].mtd : null) : null;
      if (r[1] === 'gp3' && S.fgp2) oldv = S.fgp2.mtd - S.cost.mtd;
      h.push('<tr class="' + (isCost ? 'pc-lsub' : 'pc-ltot') + '"><td>' + esc(r[0]) + '</td>' +
        '<td class="num">' + F.sek(s.mtd) + '</td><td class="num">' + pctOf(s.mtd) + '</td>' +
        '<td class="num">' + (t && t.target_mtd != null ? F.sek(t.target_mtd) : '') + '</td>' +
        '<td class="num">' + (t && t.target_mtd != null ? delta(ratio(s.mtd, t.target_mtd), isCost) : '') + '</td>' +
        '<td class="num">' + F.sek(s.ly_mtd) + '</td>' +
        '<td class="num muted">' + (oldv != null ? F.sek(oldv) : '') + '</td>' +
        '<td>' + basisTag(d, r[1]) + '</td></tr>');
    });
    h.push('</tbody></table></div>');
    var note = 'Direct variable cost = shipping model outbound (' + esc(ci.ship_rate_through ? 'rates to ' + ci.ship_rate_through : '') +
      ') + pick & pack ' + F.num(ci.pick_per_order) + ' + packaging ' + String(ci.pack_per_order || '').replace('.', ',') + ' SEK per order − Norce freight income + the forecast\'s other direct lines at ' +
      F.pct(ci.other_dv_rate) + ' of sales (warehousing, VAS, returns handling, transaction fees).' +
      (Mg && Mg.dv_per_order != null ? ' ' + F.num(Mg.dv_per_order) + ' SEK per order MTD.' : '') +
      ' BC returns through ' + esc(ci.bc_through || '–') + '. Returns post when processed, so early-month returns are mostly October orders; per-market return rates swing with BC\'s batch processing.';
    return '<h3>P&amp;L ladder, month to date</h3>' + h.join('') + '<p class="bm-note">' + note + '</p>';
  }

  /* ── Spend headroom vs the GP3 target ──────────────────────────────────── */
  // Rule (Babyshop, 2026-11-04): GP3 is the binding target; the 10.5 % COS is
  // only derived from assumed margins. Margin above forecast is room to spend.
  function signedCls(v) {
    if (v == null || isNaN(v)) return '<span class="muted">–</span>';
    return '<span class="' + (v >= 0 ? 'up' : 'down') + '">' + signedSek(v) + '</span>';
  }

  function headroomBlock(d, mk) {
    var H = d.headroom && d.headroom[mk];
    if (!H) return '';
    var pp = function (a, b) {
      if (a == null || b == null) return '–';
      var x = (a - b) * 100;
      return '<span class="' + (x >= 0 ? 'up' : 'down') + '">' + (x >= 0 ? '+' : '−') + Math.abs(x).toFixed(1).replace('.', ',') + ' pp</span>';
    };
    var verdict = H.earned_cos == null ? '' :
      (H.headroom_remaining >= 0
        ? 'Spend may rise to <b>' + F.pct(H.earned_cos) + ' COS</b> this month and still hit the GP3 target (forecast ' + F.pct(H.cos_target) + '): about <b>' +
          F.sek(H.headroom_per_day) + '/day</b> more than the current pace, Google and Meta together.'
        : H.earned_cos <= 0
          ? 'At the current pace GP2 does not reach the GP3 target <b>even with zero ad spend</b>: the gap is sales and margin, not spend (<b>' +
            F.sek(-H.headroom_remaining) + '</b> short at the expected spend).'
          : 'At the current pace GP2 only carries <b>' + F.pct(H.earned_cos) + ' COS</b> (forecast ' + F.pct(H.cos_target) + '): spend is <b>' +
            F.sek(-H.headroom_remaining) + '</b> over what still lands the GP3 target.');
    var b = H.bridge, src = function (k) { return basisTag(d, k); };
    var h = '<h3>Spend headroom vs GP3 target</h3>';
    h += '<div class="pc-head pc-head-hr">' + verdict + ' Only spend that adds at least as much incremental GP2 as it costs keeps GP3 whole.</div>';
    h += '<div class="bm-kpis">' +
      kpiCard('Allowed spend MTD', F.sek(H.allowed_spend_mtd), [
        ['actual marketing', F.sek(H.actual_spend_mtd)],
        ['headroom', signedCls(H.headroom_mtd)],
        ['= GP2 − GP3 target', '']], src('gp2')) +
      kpiCard('Allowed spend, month', F.sek(H.allowed_spend_month), [
        ['expected at pace', F.sek(H.expected_spend_month)],
        ['forecast marketing', F.sek(H.planned_spend_month)],
        ['headroom', signedCls(H.headroom_remaining)]], src('gp2')) +
      kpiCard('Headroom per day', signedSek(H.headroom_per_day), [
        ['remaining days', F.num(H.remaining_days)],
        ['allowed left', F.sek(H.allowed_spend_remaining)],
        ['spread by marketing index', '']], src('cost')) +
      kpiCard('Earned COS', F.pct(H.earned_cos), [
        ['MTD', F.pct(H.earned_cos_mtd)],
        ['forecast COS', F.pct(H.cos_target)],
        ['allowed ÷ gross (Norce)', '']], src('gross')) +
      kpiCard('GM1 MTD', F.pct(H.gm1_actual), [['forecast', F.pct(H.gm1_fc)], ['vs forecast', pp(H.gm1_actual, H.gm1_fc)], ['GP1 ÷ Norce gross', '']], src('gp1')) +
      kpiCard('GM2 MTD', F.pct(H.gm2_actual), [['forecast', F.pct(H.gm2_fc)], ['vs forecast', pp(H.gm2_actual, H.gm2_fc)], ['GP2 ÷ Norce gross', '']], src('gp2')) +
      '</div>';
    var ch = d.marketing_channels && d.marketing_channels[mk];
    if (ch && ch.length) {
      var tot = ch.reduce(function (a, c) { return a + c.mtd; }, 0);
      h += '<h3>Marketing cost by channel, MTD ' + src('cost') + '</h3>' + BM.table(ch, [
        { key: 'channel', label: 'Channel' },
        { key: 'mtd', label: 'MTD', num: true, fmt: F.sek },
        { key: 'mtd', label: 'Share', num: true, fmt: function (v) { return tot ? F.pct(v / tot, 0) : '–'; } },
        { key: 'ly_mtd', label: 'LY MTD', num: true, fmt: F.sek },
        { key: 'mtd', label: 'vs LY', num: true, fmt: function (v, r) { return delta(ratio(v, r.ly_mtd), true); } }
      ]) + '<p class="bm-note">Every channel with cost in the Funnel export, de-duplicated (the same total as the Marketing cost card, GP3 and COS). ' +
        'The forecast\'s 5.54 M marketing line is all ad spend and equals Funnel\'s all-channel Nov 2025 cost, so the scope matches.</p>';
    }
    if (b) {
      var steps = [
        ['Forecast GP2 MTD', b.target_gp2, true],
        ['Volume: sales vs target at forecast GM2', b.volume],
        ['GM1 margin vs forecast', b.gm1_margin],
        ['Variable cost (GM1 → GM2) vs forecast', b.variable_cost],
        ['Market mix (derived split)', b.market_mix],
        ['Actual GP2 MTD', b.actual_gp2, true]
      ];
      var max = Math.max.apply(null, steps.slice(1, 5).map(function (s) { return Math.abs(s[1] || 0); }).concat([1]));
      var rows = steps.map(function (s) {
        var bar = s[2] ? '' : '<div class="pc-bbar"><i class="' + (s[1] >= 0 ? 'pos' : 'neg') + '" style="width:' +
          Math.round(Math.abs(s[1] || 0) / max * 100) + '%"></i></div>';
        return '<tr' + (s[2] ? ' class="pc-btot"' : '') + '><td>' + esc(s[0]) + '</td><td class="num">' +
          (s[2] ? F.sek(s[1]) : signedCls(s[1])) + '</td><td class="pc-bcell">' + bar + '</td></tr>';
      }).join('');
      var mEff = (b.gm1_margin || 0) + (b.variable_cost || 0);
      h += '<h3>Where GP2 vs forecast comes from (MTD) ' + src('gp2') + '</h3>' +
        '<div class="bm-table-wrap"><table class="bm-table pc-bridge"><tbody>' + rows + '</tbody></table></div>' +
        '<p class="bm-note">Margin effect ' + signedSek(mEff) + ' vs volume effect ' + signedSek(b.volume) + ': headroom is coming mostly from <b>' +
        (Math.abs(mEff) >= Math.abs(b.volume) ? 'margin' : 'volume') + '</b>. Volume = (Norce merchandise − target) × forecast GM2. ' +
        'Margin = Norce merchandise × (GM1/GM2 on this ladder − forecast). "Market mix" is what is left when a market or brand target is a derived share (zero for all markets combined). ' +
        'BC books returns when they are processed, so GM1 runs high in a peak month until Black Week returns land in December.</p>';
    }
    if (mk === 'all') {
      var mrows = ['SE', 'NO', 'FI', 'DK', 'ROW'].filter(function (m) { return d.headroom[m]; }).map(function (m) {
        var x = d.headroom[m]; return { m: m, x: x };
      });
      h += '<h3>Headroom by market</h3>' + BM.table(mrows, [
        { key: 'm', label: 'Market', fmt: function (v) { return '<b style="color:' + BM.color.market(v) + '">' + esc(v) + '</b>'; } },
        { key: 'x', label: 'GM2 MTD', num: true, fmt: function (x) { return F.pct(x.gm2_actual); } },
        { key: 'x', label: 'Marketing MTD', num: true, fmt: function (x) { return F.sek(x.actual_spend_mtd); } },
        { key: 'x', label: 'Allowed MTD', num: true, fmt: function (x) { return F.sek(x.allowed_spend_mtd); } },
        { key: 'x', label: 'Headroom MTD', num: true, fmt: function (x) { return signedCls(x.headroom_mtd); } },
        { key: 'x', label: 'Headroom rest of month', num: true, fmt: function (x) { return signedCls(x.headroom_remaining); } },
        { key: 'x', label: 'Per day', num: true, fmt: function (x) { return signedSek(x.headroom_per_day); } },
        { key: 'x', label: 'Earned COS', num: true, fmt: function (x) { return F.pct(x.earned_cos); } },
        { key: 'x', label: 'GP3 vs target', num: true, fmt: function (x) { return delta(ratio(x.gp3_mtd, x.gp3_target_mtd)); } }
      ]) + '<p class="bm-note">The forecast has no market split: market headroom rides the October 2026 mix split of the global GP3 and marketing lines, so treat it as direction, not a budget.</p>';
    }
    return h;
  }

  /* ── Today, live from Norce ────────────────────────────────────────────── */
  // Same basis as the gross-sales actuals: Norce ordered merchandise, so it
  // compares directly with today's gross-sales target. The live payload's
  // revenue includes freight; it is taken off (per market at the day's overall
  // freight share, since the by-country split carries no freight figure).
  function todayBlock(d, mk, raw) {
    var t = todayIso(), i = d.days.indexOf(t);
    if (i < 0) return '';
    // The live intake has no shop split: under a brand filter show both shops, against both shops' target.
    var split = d.brand && d.brand !== 'all';
    if (split) d = view(raw, 'all');
    var M = d.markets[mk] || d.markets.all, tgt = M.targets[ui.shape].gross[i];
    var ev = d.events[t] ? ' · ' + esc(EVENT[d.events[t]] || d.events[t]) : '';
    var head = '<h3>Yesterday and today, ' + esc(F.date(t)) + ev + ' <span class="pc-src pc-src-norce">Norce ordered · live</span>' +
      (split ? ' <span class="muted" style="font-weight:400;font-size:12px">Babyshop + Lekmer: the live feed has no shop split</span>' : '') + '</h3>';
    if (!live) return head + '<p class="muted" id="pc-today-body">' + (liveLoading ? 'Loading live orders…' : 'Live orders not loaded.') + '</p>';
    if (!live.available) {
      return head + '<div class="pc-today">' + kpiCard('Today\'s target', F.sek(tgt), [['gross sales, merchandise', '']]) +
        '</div><p class="bm-note">Live Norce intake unavailable: ' + esc(live.reason || 'unknown') + '</p>';
    }
    var pd = live.partial_day || {}, cd = live.complete_day || {};
    var pick = function (day) {
      var merchShare = day.revenue_sek ? 1 - (day.freight_sek || 0) / day.revenue_sek : 1;
      if (mk === 'all') return { rev: (day.revenue_sek || 0) * merchShare, orders: day.orders || 0 };
      var bc = day.by_country || {}, c;
      if (mk === 'ROW') {                  // by_country is ISO-2: ROW = every non-Nordic country
        c = { revenue_sek: 0, orders: 0 };
        Object.keys(bc).forEach(function (k) {
          if (['SE', 'NO', 'FI', 'DK', 'unknown'].indexOf(k) < 0) { c.revenue_sek += bc[k].revenue_sek || 0; c.orders += bc[k].orders || 0; }
        });
      } else c = bc[mk] || {};
      return { rev: (c.revenue_sek || 0) * merchShare, orders: c.orders || 0 };
    };
    var now = pick(pd), y = pick(cd);
    var share = pd.elapsed_share_of_prev_day;
    var proj = share ? now.rev / share : null;
    var yTgt = i > 0 ? M.targets[ui.shape].gross[i - 1] : null;
    var cosCard = cosToday(d, M, i, mk, now.rev, y.rev, tgt, yTgt, share);
    var h = head + '<div class="pc-today">' +
      kpiCard('Ordered so far', F.sek(now.rev), [['orders', F.num(now.orders)], ['as of', esc(live.fetched_at_local || '').slice(11)]]) +
      kpiCard('Today\'s target', F.sek(tgt), [['so far vs full day', F.pct(tgt ? now.rev / tgt : null, 0)], ['day share elapsed', F.pct(share, 0)]]) +
      kpiCard('Pace for the day', F.sek(proj), [['vs target', delta(tgt && proj != null ? proj / tgt - 1 : null)], ['indication only', '']]) +
      kpiCard('Yesterday (ordered)', F.sek(y.rev), [['orders', F.num(y.orders)], ['vs its target', delta(yTgt ? y.rev / yTgt - 1 : null)]]) +
      cosCard.html + '</div>';
    h += '<p class="bm-note">Live from the Norce API: order date, Stockholm day, ex VAT, merchandise (freight taken off), gross of returns, every status except 6. ' +
      'Same basis as the gross-sales actuals above, so it compares directly with today\'s target. ' +
      'Pace = so far ÷ the share of yesterday\'s intake that had arrived by this time. ' + cosCard.note +
      (mk !== 'all' ? ' Live market split = delivery country; the history above uses the ordering application.' : '') + '</p>';
    return h;
  }

  // COS for today so far and yesterday, all advertising (rule 2026-11-12: COS always
  // means every ad channel unless it is labelled Google or Meta).
  //   yesterday: Funnel's all-channel cost for that day once it has loaded, else as today
  //   today:     Google Ads + Meta live from their APIs, plus the other channels
  //              (Naver, affiliate, price comparison …: Funnel has complete days only)
  //              at their month-to-date daily average × the share of the day elapsed
  // ÷ the live Norce intake above. Target COS = the day's marketing-cost target ÷ its
  // gross-sales target.
  function cosToday(d, M, i, mk, rev, yRev, tgt, yTgt, share) {
    var lbl = 'COS today (all advertising)';
    if (!spend) return { html: kpiCard(lbl, '<span class="muted">…</span>', [['loading live spend', '']]), note: '' };
    if (!spend.available) return { html: kpiCard(lbl, '–', [['live spend unavailable', '']]),
      note: 'Live ad spend unavailable: ' + esc(spend.reason || (spend.errors || []).join('; ') || 'unknown') + '.' };
    var D = spend.days || {}, st = ((D.today || {}).markets || {})[mk] || {}, sy = ((D.yesterday || {}).markets || {})[mk] || {};
    // other channels' daily rate: Funnel MTD through the last complete day
    var ch = (d.marketing_channels || {})[mk] || [], nDays = d.days.indexOf(d.asof) + 1, other = 0;
    ch.forEach(function (c) { if (c.channel !== 'Google Ads' && c.channel !== 'Paid Social (Meta)') other += c.mtd || 0; });
    var otherDay = nDays > 0 ? other / nDays : 0;
    var costT = (st.total || 0) + otherDay * (share || 0);
    var fy = i > 0 ? M.actual.cost[i - 1] : null, yFunnel = fy != null && fy > 0;
    var costY = yFunnel ? fy : (sy.total || 0) + otherDay;
    var tc = M.targets[ui.shape].cost || [];
    var cos = rev ? costT / rev : null, yCos = yRev ? costY / yRev : null;
    var cosT = tgt ? tc[i] / tgt : null, yCosT = (i > 0 && yTgt) ? tc[i - 1] / yTgt : null;
    var pp = function (a, b) {
      if (a == null || b == null) return '<span class="muted">–</span>';
      var x = (a - b) * 100;
      return '<span class="' + (x > 0 ? 'down' : 'up') + '">' + (x >= 0 ? '+' : '−') + Math.abs(x).toFixed(1).replace('.', ',') + ' pp</span>';
    };
    var html = kpiCard(lbl, F.pct(cos), [
      ['vs target ' + F.pct(cosT), pp(cos, cosT)],
      ['yesterday ' + F.pct(yCos), pp(yCos, yCosT) + ' <span class="muted">vs ' + F.pct(yCosT) + '</span>'],
      ['ad cost so far · as of', F.sek(costT) + ' · ' + esc((spend.fetched_at_local || '').slice(11))]
    ]);
    var note = 'COS = all advertising cost ÷ the ordered merchandise above; target = that day\'s marketing-cost target ÷ its gross-sales target. ' +
      'Today: Google Ads + Meta live from their APIs (' + F.sek(st.total || 0) + ' SEK so far) plus the other channels (Naver, affiliate, price comparison) at their month-to-date daily average ' +
      'scaled to the share of the day elapsed (' + F.sek(otherDay * (share || 0)) + ' SEK; they only reach Funnel as complete days). ' +
      'Yesterday: ' + (yFunnel ? 'Funnel\'s all-channel cost for the day.' : 'Funnel has not loaded it yet, so live Google Ads + Meta plus the other channels\' daily average.') +
      ' Cost is by the ad account\'s market, sales by delivery country. Not the same sales basis as the COS % MTD card (Funnel ÷ Funnel).' +
      ((spend.errors || []).length ? ' Missing from the live spend: ' + esc(spend.errors.join('; ')) + '.' : '');
    return { html: html, note: note };
  }

  function loadSpend() {
    if (spend || spendLoading) return;
    spendLoading = true;
    fetch('/api/blackmonth/pacing/spend-live', { credentials: 'same-origin' })
      .then(function (r) { return r.json(); })
      .then(function (j) { spend = j || { available: false, reason: 'empty response' }; })
      .catch(function (e) { spend = { available: false, reason: String(e) }; })
      .then(function () {
        spendLoading = false;
        var el = document.querySelector('#bm-pacing .bm-body'), p = BM.payload('pacing');
        if (el && p) render(el, p, BM.state);
      });
  }

  function loadLive() {
    if (live || liveLoading) return;
    liveLoading = true;
    fetch('/api/norce-today', { credentials: 'same-origin' })
      .then(function (r) { return r.json(); })
      .then(function (j) { live = j || { available: false, reason: 'empty response' }; })
      .catch(function (e) { live = { available: false, reason: String(e) }; })
      .then(function () {
        liveLoading = false;
        var el = document.querySelector('#bm-pacing .bm-body'), p = BM.payload('pacing');  // raw; render() applies the brand view
        if (el && p) render(el, p, BM.state);
      });
  }

  /* ── Charts ────────────────────────────────────────────────────────────── */
  function series(d, M) {
    var k = ui.metric, T = M.targets[ui.shape][k], A = M.actual[k], L = M.ly[k];
    var S = M.summaries[ui.shape][k];
    var n = d.asof ? d.days.indexOf(d.asof) + 1 : 0;
    var fc = T.map(function (v, i) { return i >= n && S.run_rate_ratio != null ? v * S.run_rate_ratio : null; });
    var req = T.map(function (v, i) { return i >= n && S.required_ratio != null ? v * S.required_ratio : null; });
    var cum = function (a, upto) {
      var r = 0; return a.map(function (v, i) { if (upto != null && i >= upto) return null; r += v || 0; return r; });
    };
    var cA = cum(A, n), cT = cum(T), cL = cum(L);
    var cF = T.map(function (v, i) { return null; });
    if (S.run_rate_ratio != null) {
      var run = n ? cA[n - 1] : 0;
      if (n) cF[n - 1] = run;
      for (var i = n; i < T.length; i++) { run += T[i] * S.run_rate_ratio; cF[i] = run; }
    }
    return { T: T, A: A, L: L, fc: fc, req: req, cA: cA, cT: cT, cL: cL, cF: cF, n: n, S: S };
  }

  function labels(d) {
    return d.days.map(function (iso) {
      var e = d.events[iso], s = e && EVENT_SHORT[e];
      return s ? [dm(iso), s] : dm(iso);
    });
  }

  function drawCharts(el, d, M) {
    var C = BM.color, s = series(d, M), lab = labels(d);
    var tooltip = { callbacks: { label: function (c) { return c.dataset.label + ': ' + F.sek(c.parsed.y); } } };
    var scales = {
      x: { grid: { display: false }, ticks: { autoSkip: true, maxRotation: 0, font: { size: 10 } } },
      y: { grid: { color: C.grid }, ticks: { callback: function (v) { return F.sek(v); } } }
    };
    BM.chart(el.querySelector('#pc-daily-chart'), {
      data: { labels: lab, datasets: [
        { type: 'bar', label: 'Actual', data: s.A, backgroundColor: C.actual, borderRadius: 2, order: 3 },
        { type: 'line', label: 'Target', data: s.T, borderColor: C.plan, backgroundColor: C.plan, borderWidth: 2, pointRadius: 0, tension: .25, order: 1 },
        { type: 'line', label: 'Last year (event-aligned)', data: s.L, borderColor: C.ly, borderDash: [4, 3], borderWidth: 1.5, pointRadius: 0, tension: .25, order: 2 },
        { type: 'line', label: 'Forecast at run-rate', data: s.fc, borderColor: C.fc, borderDash: [6, 4], borderWidth: 2, pointRadius: 0, tension: .25, order: 0, spanGaps: false }
      ] },
      options: { responsive: true, maintainAspectRatio: false, interaction: { mode: 'index', intersect: false },
        plugins: { legend: { display: false }, tooltip: tooltip }, scales: scales }
    });
    BM.chart(el.querySelector('#pc-cum-chart'), {
      type: 'line',
      data: { labels: lab, datasets: [
        { label: 'Actual (cumulative)', data: s.cA, borderColor: C.actual, borderWidth: 2.5, pointRadius: 0 },
        { label: 'Target (cumulative)', data: s.cT, borderColor: C.plan, borderWidth: 2, pointRadius: 0 },
        { label: 'Last year', data: s.cL, borderColor: C.ly, borderDash: [4, 3], borderWidth: 1.5, pointRadius: 0 },
        { label: 'Landing path', data: s.cF, borderColor: C.fc, borderDash: [6, 4], borderWidth: 2, pointRadius: 0 }
      ] },
      options: { responsive: true, maintainAspectRatio: false, interaction: { mode: 'index', intersect: false },
        plugins: { legend: { display: false }, tooltip: tooltip }, scales: scales }
    });
  }

  function legend(items) {
    return '<div class="pc-legend">' + items.map(function (it) {
      return '<span><i style="background:' + it[1] + (it[2] ? ';opacity:.8' : '') + '"></i>' + esc(it[0]) + '</span>';
    }).join('') + '</div>';
  }

  /* ── Daily table ───────────────────────────────────────────────────────── */
  function dailyTable(d, M) {
    var s = series(d, M), mdef = METRICS.filter(function (m) { return m.k === ui.metric; })[0];
    var t = todayIso(), h = [];
    h.push('<div class="bm-table-wrap pc-daily"><table class="bm-table"><thead><tr>' +
      '<th>Day</th><th>Event</th><th class="num">Target</th><th class="num">Actual</th><th class="num">vs target</th>' +
      '<th class="num">LY</th><th class="num">vs LY</th><th class="num">Cum. actual</th><th class="num">Cum. vs target</th>' +
      '<th class="num">Needed / day</th></tr></thead><tbody>');
    d.days.forEach(function (iso, i) {
      var past = i < s.n, ev = d.events[iso];
      var cls = [iso === t ? 'pc-now' : '', ev ? 'pc-ev' : '', !past ? 'pc-future' : ''].join(' ').trim();
      h.push('<tr' + (cls ? ' class="' + cls + '"' : '') + '>' +
        '<td>' + esc(F.date(iso)) + '</td>' +
        '<td>' + (ev ? esc(EVENT[ev] || ev) : '') + '</td>' +
        '<td class="num">' + F.sek(s.T[i]) + '</td>' +
        '<td class="num">' + (past ? F.sek(s.A[i]) : '') + '</td>' +
        '<td class="num">' + (past ? delta(ratio(s.A[i], s.T[i]), mdef.cost) : '') + '</td>' +
        '<td class="num">' + F.sek(s.L[i]) + '</td>' +
        '<td class="num">' + (past ? delta(ratio(s.A[i], s.L[i]), mdef.cost) : '') + '</td>' +
        '<td class="num">' + (past ? F.sek(s.cA[i]) : '') + '</td>' +
        '<td class="num">' + (past ? delta(ratio(s.cA[i], s.cT[i]), mdef.cost) : '') + '</td>' +
        '<td class="num">' + (!past && s.req[i] != null ? F.sek(s.req[i]) : '') + '</td>' +
        '</tr>');
    });
    h.push('</tbody></table></div>');
    return h.join('');
  }

  /* ── Markets (only on "all") ───────────────────────────────────────────── */
  function marketTable(d) {
    var tot = d.markets.all.summaries[ui.shape].gross.mtd;
    var rows = ['SE', 'NO', 'FI', 'DK', 'ROW'].map(function (m) {
      var S = d.markets[m].summaries[ui.shape];
      return { m: m, g: S.gross, p: S.gp2, q: S.gp3, c: S.cost, share: d.markets[m].share_of_ly.gross,
               now: tot ? S.gross.mtd / tot : null };
    });
    return BM.table(rows, [
      { key: 'm', label: 'Market', fmt: function (v) { return '<b data-m="' + esc(v) + '" style="color:' + BM.color.market(v) + '">' + esc(v) + '</b>'; } },
      { key: 'share', label: 'Share LY', num: true, fmt: function (v) { return F.pct(v, 0); } },
      { key: 'now', label: 'Share MTD', num: true, fmt: function (v, r) {
        var cls = v != null && Math.abs(v - r.share) > 0.04 ? 'warn' : '';
        return '<span class="' + cls + '">' + F.pct(v, 0) + '</span>'; } },
      { key: 'g', label: 'Gross MTD (Norce)', num: true, fmt: function (v) { return F.sek(v.mtd); } },
      { key: 'g', label: 'vs target', num: true, fmt: function (v) { return delta(ratio(v.mtd, v.target_mtd)); } },
      { key: 'g', label: 'vs LY', num: true, fmt: function (v) { return delta(ratio(v.mtd, v.ly_mtd)); } },
      { key: 'p', label: 'GP2 vs target', num: true, fmt: function (v) { return delta(ratio(v.mtd, v.target_mtd)); } },
      { key: 'c', label: 'Marketing MTD', num: true, fmt: function (v) { return F.sek(v.mtd); } },
      { key: 'q', label: 'GP3 MTD', num: true, fmt: function (v) { return F.sek(v.mtd); } },
      { key: 'q', label: 'GP3 vs target', num: true, fmt: function (v) { return delta(ratio(v.mtd, v.target_mtd)); } },
      { key: 'g', label: 'Gross landing', num: true, fmt: function (v) { return F.sek(v.landing) + ' ' + delta(ratio(v.landing, v.target_month)); } }
    ]).replace(/<tr><td><b data-m="(SE|NO|FI|DK)"/g, '<tr class="pc-click"><td><b data-m="$1"');
  }

  /* ── New vs returning ──────────────────────────────────────────────────── */
  function customerTable(d, mk) {
    if (!d.customers) return '<p class="muted">No customer data yet.</p>';
    var list = mk === 'all' ? ['all', 'SE', 'NO', 'FI', 'DK', 'ROW'] : [mk];
    var rows = [];
    list.forEach(function (m) {
      var c = d.customers[m];
      if (!c) return;
      ['new', 'ret'].forEach(function (k) {
        var cur = c.cur[k], ly = c.ly[k];
        var totC = c.cur.new.orders + c.cur.ret.orders, totL = c.ly.new.orders + c.ly.ret.orders;
        rows.push({
          market: m === 'all' ? 'All' : m, type: k === 'new' ? 'New' : 'Returning',
          orders: cur.orders, ordersLy: ratio(cur.orders, ly.orders),
          rev: cur.revenue, revLy: ratio(cur.revenue, ly.revenue),
          share: totC ? cur.orders / totC : null, shareLy: totL ? ly.orders / totL : null,
          aov: cur.orders ? cur.revenue / cur.orders : null, aovLy: ly.orders ? ly.revenue / ly.orders : null
        });
      });
    });
    return BM.table(rows, [
      { key: 'market', label: 'Market' }, { key: 'type', label: 'Customer' },
      { key: 'orders', label: 'Orders', num: true, fmt: F.num },
      { key: 'ordersLy', label: 'vs LY', num: true, fmt: function (v) { return delta(v); } },
      { key: 'rev', label: 'Revenue', num: true, fmt: F.sek },
      { key: 'revLy', label: 'vs LY', num: true, fmt: function (v) { return delta(v); } },
      { key: 'share', label: 'Order share', num: true, fmt: function (v) { return F.pct(v, 0); } },
      { key: 'shareLy', label: 'Share LY', num: true, fmt: function (v) { return F.pct(v, 0); } },
      { key: 'aov', label: 'AOV', num: true, fmt: F.num },
      { key: 'aovLy', label: 'AOV LY', num: true, fmt: F.num }
    ]);
  }

  /* ── Render ────────────────────────────────────────────────────────────── */
  function seg(name, opts, cur) {
    return '<div class="pc-seg" data-seg="' + name + '">' + opts.map(function (o) {
      return '<button data-v="' + esc(o[0]) + '" class="' + (o[0] === cur ? 'on' : '') + '">' + esc(o[1]) + '</button>';
    }).join('') + '</div>';
  }

  var BRAND = { all: 'Babyshop + Lekmer', babyshop: 'Babyshop', lekmer: 'Lekmer' };

  // The payload's top-level markets/headroom/customers are Babyshop + Lekmer;
  // `brands[b]` carries the same three blocks per shop. A brand view swaps them in.
  function view(raw, brand) {
    if (!brand || brand === 'all' || !raw.brands || !raw.brands[brand]) return raw;
    var v = Object.assign({}, raw, raw.brands[brand]);
    v.brand = brand;
    return v;
  }

  function scopeLine(d, mk, M) {
    var b = d.brand || 'all';
    var derived = [];
    if (mk !== 'all' || b !== 'all') {
      derived.push('<b>' + esc((b === 'all' ? '' : BRAND[b] + ' ') + (mk === 'all' ? 'all markets' : mk)) + '</b> target = ' +
        F.pct(M.share_of_ly.gross, 1) + ' of the forecast (its October 2026 share of gross sales; per metric for the rest). ' +
        'Derived: the forecast has no market or brand split.');
    }
    return '<p class="bm-note pc-scope"><b>Scope:</b> ' + esc(BRAND[b]) + (b === 'all'
      ? ', the same scope as Finance\'s forecast (it covers Babyshop + Lekmer combined; checked against BC).'
      : ' only.') + ' ' + derived.join('') + '</p>';
  }

  function render(el, raw, state) {
    injectCss();
    var d = view(raw, state.brand);
    var mk = d.markets[state.market] ? state.market : 'all';
    var M = d.markets[mk], S = M.summaries[ui.shape];
    var C = BM.color, mlabel = METRICS.filter(function (m) { return m.k === ui.metric; })[0].label;
    var shapes = Object.keys(d.shapes).map(function (k) { return [k, d.shapes[k]]; });

    var h = [];
    h.push('<div class="pc-bar">' + (shapes.length > 1 ? seg('shape', shapes, ui.shape) : '') +
      '<span class="pc-basis">Complete through ' + esc(d.asof ? F.date(d.asof) : '–') +
      '</span></div>');
    h.push(todayBlock(d, mk, raw));
    h.push(headline(d, S, mk));
    h.push(scopeLine(d, mk, M));
    h.push(kpis(d, S));
    h.push(ladderTable(d, S, mk));
    h.push(headroomBlock(d, mk));
    h.push('<h3>Daily: ' + esc(mlabel) + ' ' + basisTag(d, ui.metric) + '</h3>' + seg('metric', METRICS.map(function (m) { return [m.k, m.short || m.label]; }), ui.metric));
    h.push('<div class="bm-grid-2" style="margin-top:8px"><div>' +
      legend([['Actual', C.actual], ['Target', C.plan], ['Last year (event-aligned)', C.ly], ['Forecast at run-rate', C.fc]]) +
      '<div class="bm-chart"><canvas id="pc-daily-chart"></canvas></div></div><div>' +
      legend([['Actual', C.actual], ['Target', C.plan], ['Last year', C.ly], ['Landing path', C.fc]]) +
      '<div class="bm-chart"><canvas id="pc-cum-chart"></canvas></div></div></div>');
    var rrN = Math.min(d.run_rate_days, d.asof ? d.days.indexOf(d.asof) + 1 : 0);
    h.push('<p class="bm-note">Forecast = remaining daily target × the last ' + rrN + ' complete days\' actual ÷ target (' +
      (S[ui.metric].run_rate_ratio != null ? F.pct(S[ui.metric].run_rate_ratio, 0) : '–') + '). ' +
      '"Needed / day" = what each remaining day must deliver to land exactly on the month\'s forecast.</p>');
    h.push(dailyTable(d, M));
    if (mk === 'all') h.push('<h3>Markets</h3>' + marketTable(d) + '<p class="bm-note">Market targets split the global forecast by each market\'s October 2026 share. ' +
      'Gross sales and shares are Norce ordered by ordering application (ROW = EU/UK/NA/Asia/ROW apps); GP2, ad cost and GP3 are Funnel export by KV market. ' +
      'Where Share MTD differs from Share LY (amber) the split is stale (NO and DK have roughly doubled since last year), so read market "vs target" alongside "vs LY". Click a market to filter.</p>');
    h.push('<h3>New vs returning customers, MTD vs last year</h3>' + customerTable(d, mk) +
      '<p class="bm-note">Norce orders on order date; new = the customer\'s first-ever order in that shop. ' +
      'Norce history starts 11 Jun 2025, so last year\'s "new" is overstated. Revenue is merchandise ex shipping.</p>');
    h.push('<details class="pc-notes"><summary>How these numbers are built (' + d.notes.length + ' notes)</summary><ul>' +
      d.notes.map(function (n) { return '<li>' + esc(n) + '</li>'; }).join('') + '</ul></details>');
    el.innerHTML = h.join('');

    el.querySelectorAll('.pc-seg').forEach(function (g) {
      g.addEventListener('click', function (e) {
        var b = e.target.closest('button');
        if (!b) return;
        ui[g.getAttribute('data-seg')] = b.getAttribute('data-v');
        render(el, d, state);
      });
    });
    el.querySelectorAll('tr.pc-click').forEach(function (tr) {
      tr.addEventListener('click', function () { BM.setMarket(tr.querySelector('[data-m]').getAttribute('data-m')); });
    });
    drawCharts(el, d, M);
    loadLive();
    loadSpend();
  }

  BM.register('pacing', { title: 'Pacing & forecast', render: render });
})();
