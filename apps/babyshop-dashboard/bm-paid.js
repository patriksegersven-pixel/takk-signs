/* bm-paid.js: "Paid media levers" section of the Black Month tab.
 * Payload: bm_paid.build(). Google tROAS / budget levers, Meta allocation vs the
 * approved plan, new vs returning per channel, GP3 + marginal ROAS per market. */
(function () {
  'use strict';
  var F = BM.fmt, esc = BM.esc;
  var MKTS = ['SE', 'NO', 'FI', 'DK', 'ROW'];
  var TYPE_LABEL = { kuling: 'Kuling always-on', prospecting: 'Prospecting', retargeting: 'Catalog retargeting',
                     brand_funded: 'Brand-funded (outside plan)', unclassified: 'Unclassified' };
  var CH_LABEL = { google: 'Google Ads', meta: 'Meta', other: 'Other / organic', unknown: 'Unknown' };
  // Section-local UI state, kept across market switches.
  var ui = { gAll: false, gOnlyAction: false, nvrWin: 'l28', gp3Win: 'l28', prodView: 'brand' };
  var last = null;

  var CSS = [
    '#bm-paid .bm-grid-2>div{min-width:0}',
    '#bm-paid .pd-tabs{display:flex;gap:4px;flex-wrap:wrap;margin:0 0 8px}',
    '#bm-paid .pd-tabs button{background:var(--card-2);color:var(--body);border:1px solid var(--border);border-radius:999px;padding:3px 10px;font-size:12px;cursor:pointer;font-family:inherit}',
    '#bm-paid .pd-tabs button.on{background:var(--accent);color:#111;border-color:var(--accent);font-weight:600}',
    '#bm-paid .pd-head{display:flex;justify-content:space-between;align-items:baseline;gap:8px;flex-wrap:wrap}',
    '#bm-paid .pd-head h3{margin-bottom:6px}',
    '#bm-paid .pd-sub{color:var(--muted);font-size:12px;margin:-2px 0 8px}',
    '#bm-paid .pd-name{max-width:280px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;display:inline-block;vertical-align:bottom}',
    '#bm-paid .bm-table td{white-space:nowrap}',
    '#bm-paid .pd-bar{position:relative;height:5px;background:var(--border);border-radius:3px;width:80px;margin:4px 0 0 auto}',
    '#bm-paid .pd-bar i{position:absolute;left:0;top:0;bottom:0;border-radius:3px;background:var(--s-actual)}',
    '#bm-paid .pd-bar b{position:absolute;top:-3px;bottom:-3px;width:2px;background:var(--accent)}',
    '#bm-paid .pd-flag{display:flex;gap:6px;flex-wrap:wrap}',
    '#bm-paid details summary{cursor:pointer;color:var(--muted);font-size:12px;margin-top:8px}',
    '#bm-paid .pd-notes li{margin:2px 0}',
    '#bm-paid .pd-kpi-alert .val{color:var(--warn)}',
    '#bm-paid .pd-kpi-err .val{color:var(--down)}',
    '#bm-paid .pd-alert{border:1px solid var(--down);background:var(--down-bg);color:var(--h);border-radius:8px;padding:10px 14px;margin:12px 0;font-size:13px}',
    '#bm-paid .pd-alert b{color:var(--down)}',
    '#bm-paid .pd-guard{border:1px solid var(--accent);background:var(--accent-bg);border-radius:8px;padding:10px 14px;margin-bottom:12px;font-size:13px;display:grid;gap:4px}',
    '@media (max-width:600px){#bm-paid .bm-kpis{grid-template-columns:1fr 1fr}#bm-paid .pd-name{max-width:150px}#bm-paid .bm-kpi .val{font-size:20px}}'
  ].join('');

  function injectCss() {
    if (document.getElementById('bm-paid-css')) return;
    var s = document.createElement('style');
    s.id = 'bm-paid-css'; s.textContent = CSS;
    document.head.appendChild(s);
  }

  function sum(a) { var t = 0; (a || []).forEach(function (v) { t += v || 0; }); return t; }
  function sumTo(a, n) { var t = 0; for (var i = 0; i < n && i < (a || []).length; i++) t += a[i] || 0; return t; }
  function addArr(dst, src) { (src || []).forEach(function (v, i) { if (v != null) dst[i] = (dst[i] || 0) + v; }); }
  function inMkt(state, m) { return state.market === 'all' || state.market === m; }
  function inBrand(state, b) { return !state.brand || state.brand === 'all' || state.brand === (b || 'babyshop'); }
  function inF(state, r) { return inMkt(state, r.market) && inBrand(state, r.brand); }
  function brandOfCustomer(n) { return /lekmer/i.test(n || '') ? 'lekmer' : 'babyshop'; }
  function mkb(v, r) {           // market cell with a Lekmer tag (Babyshop is the default)
    return mk(v) + (r && r.brand === 'lekmer' ? ' <span class="bm-pill muted">Lekmer</span>' : '');
  }
  function gPacing(d, state) {
    var g = d.google;
    return (state.brand && state.brand !== 'all' && g.pacing_by_brand) ? (g.pacing_by_brand[state.brand] || {}) : g.pacing;
  }
  var metaOn = function (d, state) { return inBrand(state, 'babyshop'); };
  function div(a, b) { return b ? a / b : null; }
  function pill(cls, txt) { return '<span class="bm-pill ' + (cls || 'muted') + '">' + esc(txt) + '</span>'; }
  function mk(m) { return '<span style="color:' + BM.color.market(m) + ';font-weight:600">' + esc(m) + '</span>'; }
  function tabs(name, opts, cur) {
    return '<div class="pd-tabs" data-tab="' + name + '">' + opts.map(function (o) {
      return '<button data-v="' + o[0] + '"' + (o[0] === cur ? ' class="on"' : '') + '>' + esc(o[1]) + '</button>';
    }).join('') + '</div>';
  }
  function roasCell(v, target) {
    if (v == null) return '<span class="muted">–</span>';
    if (!target) return F.roas(v);
    var r = v / target;
    return '<span class="' + (r < 0.85 ? 'down' : r > 1.15 ? 'up' : '') + '">' + F.roas(v) + '</span>';
  }
  function paceBar(actual, plan, month) {
    var w = month ? Math.min(100, actual / month * 100) : 0, m = month ? Math.min(100, plan / month * 100) : 0;
    return '<div class="pd-bar" title="MTD actual vs month plan; tick = where plan says we should be"><i style="width:' + w.toFixed(1) + '%"></i><b style="left:' + m.toFixed(1) + '%"></b></div>';
  }

  /* index of the last complete day (yesterday) inside the month, -1 before 1 Nov */
  function asofIdx(d) { return d.days.indexOf(d.asof); }
  function todayIdx(d) { return d.days.indexOf(d.today); }

  /* Pacing rows: [{key,label, plan[], actual[], month, today?}] → table html */
  function pacingTable(rows, d, firstLabel) {
    var n = asofIdx(d) + 1, ti = todayIdx(d), daysLeft = ti >= 0 ? d.days.length - ti : d.days.length;
    var tr = rows.map(function (r) {
      var pm = sumTo(r.plan, n), am = sumTo(r.actual, n);
      var pace = div(am, pm);
      var yday = n > 0 ? r.actual[n - 1] : null, ypl = n > 0 ? r.plan[n - 1] : null;
      var need = daysLeft > 0 ? (r.month - am) / daysLeft : null;
      var tplan = ti >= 0 ? r.plan[ti] : null;
      return {
        label: r.label, month: r.month, pm: pm, am: am, pace: pace, yday: yday, ypl: ypl,
        need: need, tplan: tplan, today: r.today, bar: paceBar(am, pm, r.month)
      };
    });
    return BM.table(tr, [
      { key: 'label', label: firstLabel, fmt: function (v) { return v; } },
      { key: 'month', label: 'Month plan', num: true, fmt: F.sek },
      { key: 'pm', label: 'Plan MTD', num: true, fmt: F.sek },
      { key: 'am', label: 'Actual MTD', num: true, fmt: function (v, r) { return F.sek(v) + r.bar; } },
      { key: 'pace', label: 'vs plan', num: true, fmt: function (v) { return v == null ? '–' : F.pct(v, 0); } },
      { key: 'yday', label: 'Yesterday / plan', num: true, fmt: function (v, r) { return F.sek(v) + ' <span class="muted">/ ' + F.sek(r.ypl) + '</span>'; } },
    ]);
  }

  function cumChart(canvas, d, series) {
    var n = asofIdx(d) + 1;
    var labels = d.days.map(function (x) { return +x.slice(8); });
    var ds = [];
    series.forEach(function (s) {
      var c = 0, ca = 0;
      ds.push({ type: 'line', label: s.label + ' plan', data: s.plan.map(function (v) { c += v || 0; return c; }),
        borderColor: s.color, borderDash: [4, 4], borderWidth: 1.5, pointRadius: 0, fill: false });
      ds.push({ type: 'line', label: s.label + ' actual', data: s.actual.map(function (v, i) { if (i >= n) return null; ca += v || 0; return ca; }),
        borderColor: s.color, borderWidth: 2.5, pointRadius: 0, fill: false });
    });
    BM.chart(canvas, {
      data: { labels: labels, datasets: ds },
      options: {
        maintainAspectRatio: false, interaction: { mode: 'index', intersect: false },
        plugins: { legend: { labels: { boxWidth: 12, filter: function (i) { return series.length < 4 || /actual/.test(i.text); } } },
          tooltip: { callbacks: { label: function (c) { return c.dataset.label + ': ' + F.sek(c.parsed.y); } } } },
        scales: { y: { ticks: { callback: function (v) { return F.sek(v); } } }, x: { ticks: { maxTicksLimit: 15 } } }
      }
    });
  }

  /* ── Guardrail ──────────────────────────────────────────────────────────── */
  function hrPick(hr, state) {
    if (!hr || typeof hr !== 'object') return hr;
    var b = hr[state.brand && state.brand !== 'all' ? state.brand : 'all'];
    if (!b || typeof b !== 'object' || !(b.all || b.SE)) return hr;       // unknown shape: show as-is
    return b[state.market === 'all' ? 'all' : state.market] || null;
  }
  function hrHtml(v) {
    if (v == null) return '<span class="muted">Not published by the pacing section yet.</span>';
    if (typeof v === 'number') return '<b>' + F.sek(v) + ' SEK</b>';
    if (typeof v !== 'object') return esc(v);
    if (v.headroom_mtd !== undefined) {
      var cls = function (x) { return x == null ? '' : x < 0 ? 'down' : 'up'; };
      return '<b class="' + cls(v.headroom_remaining) + '">' + F.sek(v.headroom_remaining) + ' SEK</b> left this month' +
        ' (<span class="' + cls(v.headroom_per_day) + '">' + F.sek(v.headroom_per_day) + '/day</span>) · to date ' +
        '<span class="' + cls(v.headroom_mtd) + '">' + F.sek(v.headroom_mtd) + '</span> (spent ' + F.sek(v.actual_spend_mtd) +
        ' of ' + F.sek(v.allowed_spend_mtd) + ' allowed) · earned COS ' + F.pct(v.earned_cos_mtd, 1) + ' vs ' + F.pct(v.cos_target, 1);
    }
    return Object.keys(v).filter(function (k) { return v[k] == null || typeof v[k] !== 'object'; }).map(function (k) {
      var x = v[k];
      return esc(k.replace(/_/g, ' ')) + ': <b>' + (typeof x === 'number' ? (Math.abs(x) < 10 ? F.roas(x) : F.sek(x)) : esc(x)) + '</b>';
    }).join(' · ');
  }
  function marginOf(d, brand, market) {
    var m = ((d.guardrail || {}).margins || {})[(brand || 'all') + ':' + (market || 'all')];
    return m || {};
  }
  function guardrailHtml(d, state) {
    var g = d.guardrail || {}, inc = g.incrementality || {};
    function cnt(rows) {
      var o = { more: 0, less: 0 };
      (rows || []).forEach(function (r) {
        if (!inF(state, r) || r.inc_marginal == null) return;
        if (r.inc_marginal > 1.1) o.more++; else if (r.inc_marginal < 0.9) o.less++;
      });
      return o;
    }
    var gc = cnt(d.google.campaigns), mc = cnt(d.meta.lines);
    var h = ['<div class="pd-guard">'];
    h.push('<div class="pd-guard-rule"><b>Rule:</b> ' + esc(g.rule || '') + '</div>');
    h.push('<div><b>Spend headroom vs Finance GP3 target</b> <span class="muted">(pacing, all marketing)</span>: ' + hrHtml(hrPick(g.headroom, state)) + '</div>');
    var mv = marginOf(d, state.brand === 'all' ? 'all' : state.brand, state.market);
    h.push('<div class="muted">GP2 margin used: <b>' + F.pct(mv.gm2, 1) + '</b> ' + (mv.basis === 'norce' ? '(Norce basis: ' + esc(g.margin_basis_label || '') + ')' : '(Funnel export, fallback)') + '. Google profit ROAS stays platform-reported (Babyshop\'s own profit definition).</div>');
    h.push('<div class="muted">Signals now' + (state.market === 'all' ? '' : ' (' + esc(state.market) + ')') + ': Google ' + gc.more + ' campaigns earn &gt;1 incremental per extra krona, ' +
      gc.less + ' below 1 · Meta ' + mc.more + ' lines above, ' + mc.less + ' below.</div>');
    if ((g.budget_errors || []).length) h.push('<div class="bm-error">Limited by budget (budgets must never bind): ' + g.budget_errors.map(esc).join(', ') + '</div>');
    h.push('<div class="muted" style="font-size:11px">Incrementality assumptions (not yet tested): ' + Object.keys(inc).map(function (k) {
      return esc(k.replace(/_/g, ' ')) + ' ' + F.pct(inc[k], 0); }).join(' · ') + '</div>');
    h.push('</div>');
    return h.join('');
  }

  /* ── Google ─────────────────────────────────────────────────────────────── */
  function incCell(v) {
    if (v == null) return '<span class="muted">–</span>';
    return '<span class="' + (v > 1.1 ? 'up' : v < 0.9 ? 'down' : '') + '">' + F.roas(v) + '</span>';
  }
  function gp10(v) { return v == null ? '<span class="muted">–</span>' : '<span class="' + (v >= 0 ? 'up' : 'down') + '">' + (v >= 0 ? '+' : '−') + F.sek(Math.abs(v)) + '</span>'; }

  function googleHtml(d, state) {
    var g = d.google, h = [];
    var n = asofIdx(d) + 1;
    // Spend by market: context only (no budget).
    var gp = gPacing(d, state);
    var srows = MKTS.filter(function (m) { return inMkt(state, m) && gp[m] && (sum(gp[m].actual) || gp[m].today_so_far); }).map(function (m) {
      var a = gp[m].actual || [];
      return { m: m, yday: n > 0 ? a[n - 1] : null, l7: sumTo(a.slice(Math.max(0, n - 7)), 7), mtd: sumTo(a, n), today: gp[m].today_so_far };
    });
    h.push('<div class="pd-head"><h3>Google Ads · spend (context, no budget)</h3></div>');
    h.push('<div class="pd-sub">Funnel SEK cost through ' + F.date(d.asof) + '; today = live API. Spend is judged by incremental GP3 below, not against a plan. L7 counts November days only.</div>');
    h.push(BM.table(srows, [
      { key: 'm', label: 'Market', fmt: mk },
      { key: 'yday', label: 'Yesterday', num: true, fmt: F.sek },
      { key: 'l7', label: 'Last 7 d (Nov)', num: true, fmt: F.sek },
      { key: 'mtd', label: 'MTD', num: true, fmt: F.sek },
      { key: 'today', label: 'Today so far', num: true, fmt: F.sek }
    ]));

    var camps = g.campaigns.filter(function (c) { return inF(state, c); });
    var limited = camps.filter(function (c) { return c.budget_limited; });
    if (limited.length) h.push('<div class="bm-error" style="margin-top:8px">ERROR: ' + limited.length + ' campaign(s) limited by budget. Budgets are meant never to bind: ' + limited.map(function (c) { return esc(c.market + ' ' + c.name); }).join(', ') + '</div>');
    var act = function (c) { return c.suggestion && (c.suggestion.action === 'lower' || c.suggestion.action === 'raise' || c.suggestion.action === 'error'); };
    var cols = [
      { key: 'market', label: 'Mkt', fmt: mkb },
      { key: 'name', label: 'Campaign', fmt: function (v, r) { return '<span class="pd-name" title="' + esc(v) + ' · ' + esc(r.bidding) + '">' + esc(v) + '</span><div class="muted" style="font-size:11px">' + esc(r.channel) + ' · incr. ' + F.pct(r.incrementality, 0) + '</div>'; } },
      { key: 'troas', label: 'tROAS', num: true, fmt: function (v) { return v == null ? '<span class="muted">–</span>' : F.roas(v); } },
      { key: 'yday_cost_sek', label: 'Spend yday', num: true, fmt: F.sek },
      { key: 'l7_cost_sek', label: 'Spend L7', num: true, fmt: F.sek },
      { key: 'l7_roas', label: 'Avg ROAS L7', num: true, fmt: function (v) { return v == null ? '<span class="muted">–</span>' : F.roas(v); } },
      { key: 'marginal_roas', label: 'Marginal ROAS', num: true, fmt: function (v, r) {
          if (v == null) return '<span class="muted">–</span>';
          var t = r.marginal_basis === 'curve' ? 'ROAS-sims curve ' + (r.curve_run || '') + ', κ-calibrated. Extra krona from −10 % tROAS: ' + F.roas(v) + '; krona cut by +10 %: ' + F.roas(r.marginal_roas_up) : '28-day spend/profit fit (weak)';
          return '<span title="' + esc(t) + '">' + F.roas(v) + '</span> <span class="muted" style="font-size:11px">' + (r.marginal_basis === 'curve' ? 'curve' : 'fit') + '</span>'; } },
      { key: 'inc_marginal', label: 'Incr. marginal', num: true, fmt: incCell },
      { key: 'inc_gp3_per_10k', label: 'Incr. GP3 / +10 k', num: true, fmt: gp10 },
      { key: 'suggestion', label: 'Suggested move', fmt: function (v) { return v ? pill(v.cls, v.text) : ''; } },
      { key: 'lost_rank', label: 'Headroom', fmt: function (v, r) {
          if (!r.headroom) return '<span class="muted">none seen</span>';
          var parts = [];
          if (v != null && v >= 0.1) parts.push('rank ' + F.pct(v, 0));
          var m = /\+(\d+)%/.exec(r.headroom_why || '');
          if (m) parts.push('+' + m[1] + ' % at −10 %');
          return '<span class="up" title="' + esc(r.headroom_why || '') + '">' + esc(parts.join(' · ') || 'yes') + '</span>'; } },
      { key: 'last_change', label: 'Last tROAS change', fmt: function (v, r) {
          if (!v) return '<span class="muted">none in 45 d</span>';
          return F.date(v.date) + ' ' + F.roas(v.old) + '→' + F.roas(v.new) + (r.cooldown ? ' ' + pill('muted', 'cooldown → ' + F.date(r.cooldown_until)) : ''); } },
    ];
    h.push('<div class="pd-head"><h3>Google campaigns · incremental marginal profit ROAS</h3>' +
      tabs('gfilter', [['all', 'All'], ['act', 'Moves only']], ui.gOnlyAction ? 'act' : 'all') + '</div>');
    h.push('<div class="pd-sub">Platform-reported: conversion value = profit, so ROAS = profit ÷ spend. Marginal = profit from the next krona (ROAS-sims curve, κ-calibrated; "fit" = 28-day fallback). Incremental = marginal × incrementality assumption. Lower tROAS ~10 % where incremental marginal &gt; 1 with headroom, raise ~10 % where &lt; 1. Change tROAS only via apply_troas.py, outside the 14-day cooldown.</div>');
    [['non-brand', 'Non-brand'], ['brand', 'Brand']].forEach(function (sg) {
      var list = camps.filter(function (c) { return c.segment === sg[0] && (!ui.gOnlyAction || act(c)); });
      var shown = (ui.gAll || sg[0] === 'brand') ? list : list.slice(0, 25);
      var sp = 0; list.forEach(function (c) { sp += c.l7_cost_sek || 0; });
      h.push('<h3 style="margin-top:10px">' + sg[1] + ' <span class="muted" style="font-weight:400">· ' + list.length + ' campaigns · ' + F.sek(sp) + ' SEK L7</span></h3>');
      h.push(BM.table(shown, cols, { empty: 'No campaigns' }));
      if (sg[0] === 'non-brand' && list.length > 25) h.push('<div class="pd-tabs" data-tab="gall" style="margin-top:6px"><button data-v="' + (ui.gAll ? 'less' : 'more') + '">' + (ui.gAll ? 'Show top 25' : 'Show all ' + list.length) + '</button></div>');
    });

    // Change log + seasonality
    var ch = g.changes.filter(function (c) { return inMkt(state, mktOfCustomer(c.customer_name)) && inBrand(state, brandOfCustomer(c.customer_name)); });
    var cd = 0, free = 0;
    camps.forEach(function (c) { if (c.cooldown) cd++; else if (c.troas) free++; });
    h.push('<div class="bm-grid-2"><div>');
    h.push('<h3>tROAS changes (roas_sims.target_changes)</h3><div class="pd-sub">' + cd + ' active campaigns in the ' + g.cooldown_days + '-day cooldown, ' + free + ' free to change. Change tROAS only with pipeline/apply_troas.py.</div>');
    h.push(BM.table(ch.slice(0, 15), [
      { key: 'd', label: 'Date', fmt: F.date },
      { key: 'customer_name', label: 'Account', fmt: function (v) { return esc(String(v || '').replace('Babyshop ', '')); } },
      { key: 'campaign_name', label: 'Campaign', fmt: function (v) { return '<span class="pd-name" title="' + esc(v) + '">' + esc(v) + '</span>'; } },
      { key: 'old_target', label: 'Old → new', num: true, fmt: function (v, r) { return F.roas(v) + ' → ' + F.roas(r.new_target); } },
      { key: 'predicted_gp3_pct', label: 'Pred. GP3', num: true, fmt: function (v) { return v == null ? '–' : F.delta(v / 100, 0); } },
      { key: 'source', label: 'Source', fmt: function (v) { return '<span class="muted" title="' + esc(v) + '">' + esc(String(v || '').slice(0, 22)) + '</span>'; } }
    ], { empty: 'No logged changes in 45 days' }));
    h.push('</div><div>');
    h.push('<h3>Post-BF seasonality adjustments</h3><div class="pd-sub">Agreed 4 Nov: Tue 1 Dec −45 %, Wed 2 Dec −30 % (Search + Shopping). Review on ~18 Nov whether tROAS stays raised through BF.</div>');
    var pbb = g.post_bf_by_brand || { babyshop: g.post_bf }, sRows = [];
    Object.keys(pbb).forEach(function (b) {
      if (!inBrand(state, b)) return;
      MKTS.forEach(function (m) { var p = pbb[b][m]; if (p && inMkt(state, m)) sRows.push({ m: m, market: m, brand: b, d1: p.day1, d2: p.day2 }); });
    });
    function adj(a) { return a ? pill('up', Math.round((a.modifier - 1) * 100) + ' % · ' + (a.channels || []).join('+').toLowerCase()) : pill('down', 'missing'); }
    h.push(BM.table(sRows, [
      { key: 'm', label: 'Account', fmt: mkb },
      { key: 'd1', label: 'Post-BF dag 1 (1 Dec)', fmt: adj },
      { key: 'd2', label: 'Post-BF dag 2 (2 Dec)', fmt: adj }
    ]));
    var other = g.seasonality.filter(function (s) { return inF(state, s) && !/post.?bf/i.test(s.name); });
    if (other.length) h.push('<div class="bm-note">Other adjustments from Nov on: ' + other.map(function (s) { return esc(s.market + ' ' + s.name + ' ' + s.start.slice(0, 10)); }).join(' · ') + '</div>');
    h.push('</div></div>');
    return h.join('');
  }
  function mktOfCustomer(n) { var m = /\b(SE|NO|FI|DK|ROW)\b/.exec(n || ''); return m ? m[1] : 'ROW'; }

  /* ── Meta ───────────────────────────────────────────────────────────────── */
  // Meta has its own section now (bm-meta.js): new vs existing customers, frequency
  // and a scale / hold / cut verdict per market, tactic and ad-set group.
  function metaPointer() {
    return '<h3>Meta</h3><div class="bm-note">Meta levers moved to their own section: <a href="#bm-meta" style="color:var(--accent)">Meta · scale / hold / cut</a>.</div>';
  }

  function metaHtml(d, state) {
    var m = d.meta, p = m.pacing, h = [];
    if (!metaOn(d, state)) return '<h3>Meta</h3><div class="bm-note warn">' + esc(d.meta_brand_note || 'No Lekmer Meta data.') + '</div>';
    if (state.brand === 'all' && d.meta_brand_note) h.push('<div class="bm-note" style="margin-top:12px">' + esc(d.meta_brand_note) + '</div>');
    var rt = m.retargeting_check;
    if (rt && !rt.running) {
      var fb = rt.funnel_by_brand || {}, lk = fb.lekmer || {};
      h.push('<div class="pd-alert"><b>Catalog retargeting is not running.</b> ' +
        F.sek(rt.warehouse_mtd) + ' SEK spent so far in November against ' + F.sek(rt.plan_to_date) + ' planned to date (' +
        F.sek(rt.plan_month) + ' for the month); ' + F.sek(rt.warehouse_prev_month) + ' in October. ' +
        'Funnel books no Babyshop Meta cost as retargeting at all (its catalog campaigns land under prospecting). ' +
        (lk.nov_ly ? 'Last November\'s ' + F.sek(lk.nov_ly) + ' SEK of Funnel retargeting was Lekmer\'s; ' : '') +
        'Lekmer has no Meta cost this month' + (rt.lekmer_meta_last_month ? ' (last spend ' + esc(rt.lekmer_meta_last_month) + ')' : '') + '.</div>');
    }
    var mk_ = MKTS.filter(function (x) { return inMkt(state, x) && p.market[x] && (sum(p.market[x].plan) || sum(p.market[x].actual)); });
    var mRows = mk_.map(function (x) { var r = p.market[x]; return { label: mk(x), plan: r.plan, actual: r.actual, month: sum(r.plan) }; });
    if (mRows.length > 1) {
      var tp = [], ta = [];
      mRows.forEach(function (r) { addArr(tp, r.plan); addArr(ta, r.actual); });
      mRows.push({ label: '<b>Total</b>', plan: tp, actual: ta, month: sum(tp) });
    }
    // By type: all markets, or the selected market (plan = type plan × market share).
    var share = state.market === 'all' ? 1 : (d.plan.meta_share[state.market] || 0);
    var tRows = Object.keys(p.type).filter(function (t) { return sum(p.type[t].plan) || sum(p.type[t].actual); }).map(function (t) {
      var plan = p.type[t].plan.map(function (v) { return v * share; });
      var actual = p.type[t].actual;
      if (state.market !== 'all') {
        // daily type actuals are not split by market; scale by the MTD market share of that type
        var tot = 0, mine = 0;
        p.market_type_mtd.forEach(function (r) { if (r.type === t) { tot += r.spend; if (r.market === state.market) mine += r.spend; } });
        var f = tot ? mine / tot : 0;
        actual = actual.map(function (v) { return v == null ? null : v * f; });
      }
      return { label: esc(TYPE_LABEL[t] || t), plan: plan, actual: actual, month: sum(plan), t: t };
    });
    h.push('<div class="pd-head"><h3>Meta · spend vs the approved starting plan (context)</h3></div>');
    h.push('<div class="pd-sub">Approved 200 k SEK is the starting point, not a cap: Kuling 60 k / prospecting 20 k / catalog retargeting 120 k; SE 60 / NO 20 / FI 10 / DK 10. Move money between lines by the incremental marginal GP3 below.</div>');
    h.push('<div class="bm-grid-2"><div>' + pacingTable(mRows, d, 'Market') + '<div style="height:10px"></div>' + pacingTable(tRows, d, 'Campaign type') + '</div><div class="bm-chart"><canvas id="pd-m-chart"></canvas></div></div>');
    var lines = (m.lines || []).filter(function (l) { return inF(state, l); });
    h.push('<h3>Meta lines · incremental marginal GP2 per krona</h3><div class="pd-sub">Market × campaign type, last 28 days. Platform-reported pixel revenue ROAS → marginal (28-day spend/revenue fit; "avg" = average ROAS used as an upper bound when the fit is weak) × GP2 margin (Norce basis, from pacing) × incrementality assumption. Above 1 the next krona adds GP3 (scale up), below 1 it loses GP3 (scale down). The last-click column is the Funnel GA4 view for the whole market, as a sanity check.</div>');
    h.push(BM.table(lines, [
      { key: 'market', label: 'Mkt', fmt: mk },
      { key: 'type', label: 'Line', fmt: function (v) { return esc(TYPE_LABEL[v] || v); } },
      { key: 'l28_spend', label: 'Spend L28', num: true, fmt: F.sek },
      { key: 'plan_month', label: 'Start plan / month', num: true, fmt: function (v) { return v == null ? '<span class="muted">–</span>' : F.sek(v); } },
      { key: 'avg_roas', label: 'Avg ROAS (platform)', num: true, fmt: F.roas },
      { key: 'gp2_margin', label: 'GP2 margin', num: true, fmt: function (v, r) { return F.pct(v, 0) + ' <span class="muted" style="font-size:11px">' + (r.margin_basis === 'norce' ? 'Norce' : 'Funnel') + '</span>'; } },
      { key: 'lastclick_gp2_roas_market', label: 'Last-click GP2 ROAS', num: true, fmt: function (v) { return v == null ? '<span class="muted">–</span>' : F.roas(v); } },
      { key: 'marginal_roas', label: 'Marginal ROAS', num: true, fmt: function (v, r) { return v == null ? '–' : F.roas(v) + ' <span class="muted" style="font-size:11px">' + (r.marginal_basis === 'fit' ? 'fit' : 'avg') + '</span>'; } },
      { key: 'incrementality', label: 'Incr.', num: true, fmt: function (v) { return v == null ? '–' : F.pct(v, 0); } },
      { key: 'inc_marginal', label: 'Incr. marginal GP2', num: true, fmt: incCell },
      { key: 'inc_gp3_per_10k', label: 'Incr. GP3 / +10 k', num: true, fmt: gp10 },
      { key: 'suggestion', label: 'Suggestion', fmt: function (v) { return v ? pill(v.cls, v.text) : ''; } }
    ], { empty: 'No Meta lines with spend' }));
    if (m.unclassified.length) h.push('<div class="bm-note warn">Unclassified campaigns (not counted in the type plan): ' + m.unclassified.map(esc).join(', ') + '</div>');

    var camps = m.campaigns.filter(function (c) { return inF(state, c); });
    h.push('<h3>Meta campaigns · ROAS / CPA</h3><div class="pd-sub">Platform-reported: Meta pixel purchase value ÷ spend (revenue ROAS, Meta attribution). L7 vs the 7 days before.</div>');
    h.push(BM.table(camps, [
      { key: 'market', label: 'Mkt', fmt: mk },
      { key: 'campaign', label: 'Campaign', fmt: function (v) { return '<span class="pd-name" title="' + esc(v) + '">' + esc(v) + '</span>'; } },
      { key: 'type', label: 'Type', fmt: function (v) { return pill(v === 'unclassified' ? 'warn' : 'muted', TYPE_LABEL[v] || v); } },
      { key: 'mtd_spend', label: 'Spend MTD', num: true, fmt: F.sek },
      { key: 'mtd_roas', label: 'ROAS MTD', num: true, fmt: F.roas },
      { key: 'mtd_cpa', label: 'CPA MTD', num: true, fmt: F.num },
      { key: 'l7_spend', label: 'Spend L7', num: true, fmt: F.sek },
      { key: 'l7_roas', label: 'ROAS L7', num: true, fmt: function (v, r) { return F.roas(v) + (r.p7_roas ? ' ' + F.delta(v / r.p7_roas - 1, 0) : ''); } },
      { key: 'l7_cpa', label: 'CPA L7', num: true, fmt: F.num }
    ], { empty: 'No Meta spend' }));
    var ads = m.adsets.filter(function (a) { return inF(state, a); }).slice(0, 30);
    h.push('<details><summary>Ad sets, last 7 days (' + ads.length + ')</summary>' + BM.table(ads, [
      { key: 'market', label: 'Mkt', fmt: mk },
      { key: 'campaign', label: 'Campaign', fmt: function (v) { return '<span class="pd-name" title="' + esc(v) + '">' + esc(v) + '</span>'; } },
      { key: 'adset', label: 'Ad set', fmt: function (v) { return '<span class="pd-name" title="' + esc(v) + '">' + esc(v) + '</span>'; } },
      { key: 'l7_spend', label: 'Spend', num: true, fmt: F.sek },
      { key: 'l7_purchases', label: 'Purch.', num: true, fmt: F.num },
      { key: 'l7_roas', label: 'ROAS', num: true, fmt: F.roas },
      { key: 'l7_cpa', label: 'CPA', num: true, fmt: F.num },
      { key: 'l7_cpc', label: 'CPC', num: true, fmt: F.roas }
    ]) + '</details>');
    h.push('<details><summary>Campaign-type rules</summary><div class="bm-note">' + m.type_rules.map(esc).join('<br>') + '<br>First match wins; anything else is "unclassified".</div></details>');

    // Products / brands / categories (catalog delivery)
    var pr = m.products, view = ui.prodView, prow;
    function rollup(list) {
      var acc = {};
      list.filter(function (r) { return inMkt(state, r.market); }).forEach(function (r) {
        var a = acc[r.name] || (acc[r.name] = { name: r.name, spend: 0, purchases: 0, value: 0 });
        a.spend += r.spend || 0; a.purchases += r.purchases || 0; a.value += (r.roas || 0) * (r.spend || 0);
      });
      return Object.keys(acc).map(function (k) { var a = acc[k]; a.roas = div(a.value, a.spend); return a; })
        .sort(function (a, b) { return b.spend - a.spend; }).slice(0, 20);
    }
    if (view === 'product') prow = pr.products.filter(function (r) { return inMkt(state, r.market); }).slice(0, 20);
    else prow = rollup(view === 'brand' ? pr.brands : pr.categories);
    var cov = 0; pr.products.forEach(function (r) { if (inMkt(state, r.market)) cov += r.spend || 0; });
    h.push('<div class="pd-head"><h3>Meta catalog delivery · what the money sells</h3>' +
      tabs('prod', [['brand', 'Brands'], ['category', 'Categories'], ['product', 'Products']], view) + '</div>');
    h.push('<div class="pd-sub">Catalog (DPA) ads only, ' + F.date(pr.window[0]) + '–' + F.date(pr.window[1]) + '. Spend covered here: ' + F.sek(pr.coverage_spend) + ' SEK of all Meta spend.</div>');
    var cols = [{ key: 'name', label: view === 'product' ? 'Product' : view === 'brand' ? 'Brand' : 'Category', fmt: function (v, r) {
      return '<span class="pd-name" title="' + esc(v) + '">' + esc(v) + '</span>' + (view === 'product' ? '<div class="muted" style="font-size:11px">' + esc(r.maker) + ' · ' + esc(r.category) + '</div>' : ''); } }];
    if (view === 'product') cols.push({ key: 'market', label: 'Mkt', fmt: mk });
    cols.push({ key: 'spend', label: 'Spend', num: true, fmt: F.sek },
      { key: 'purchases', label: 'Purch.', num: true, fmt: F.num },
      { key: 'roas', label: 'ROAS (platform)', num: true, fmt: F.roas });
    h.push(BM.table(prow, cols, { empty: 'No catalog delivery in this window' }));
    return h.join('');
  }

  /* ── New vs returning ───────────────────────────────────────────────────── */
  function nvrHtml(d, state) {
    var n = d.nvr, w = n.windows[ui.nvrWin] || n.windows.l28, h = [];
    if (!w) return '';
    var rows = w.rows.filter(function (r) { return inF(state, r) && r.channel !== 'unknown'; });
    if (state.market === 'all' && rows.length) {      // add a cross-market total per channel
      var acc = {};
      rows.forEach(function (r) {
        var a = acc[r.channel] || (acc[r.channel] = { market: 'All', channel: r.channel, orders: 0, new: 0, new_est: 0, spend: 0, hasSpend: false });
        a.orders += r.orders; a.new += r.new; a.new_est += r.new_est || 0;
        if (r.spend != null) { a.spend += r.spend; a.hasSpend = true; }
      });
      var tot = Object.keys(acc).map(function (k) {
        var a = acc[k]; a.new_share = div(a.new, a.orders);
        if (!a.hasSpend) a.spend = null;
        a.cost_per_new = a.spend ? a.spend / a.new_est : null; a.total = true; return a;
      }).sort(function (a, b) { return b.orders - a.orders; });
      rows = tot.concat(rows);
    }
    h.push('<div class="pd-head"><h3>New vs returning customers by channel</h3>' +
      tabs('nvr', [['mtd', 'Month to date'], ['l28', 'Last 28 days']], ui.nvrWin) + '</div>');
    h.push('<div class="pd-sub">' + esc(n.basis) + ' Window ' + F.date(w.from) + '–' + F.date(w.to) + '.</div>');
    h.push(BM.table(rows, [
      { key: 'market', label: 'Mkt', fmt: function (v, r) { return r.total ? '<b>All</b>' : mkb(v, r); } },
      { key: 'channel', label: 'Channel', fmt: function (v) { return esc(CH_LABEL[v] || v); } },
      { key: 'orders', label: 'Orders (matched)', num: true, fmt: F.num },
      { key: 'new_share', label: 'New share', num: true, fmt: function (v) { return F.pct(v, 0); } },
      { key: 'new_est', label: 'New cust. (est.)', num: true, fmt: F.num },
      { key: 'spend', label: 'Spend', num: true, fmt: function (v) { return v == null ? '<span class="muted">n/a</span>' : F.sek(v); } },
      { key: 'cost_per_new', label: 'Cost / new cust.', num: true, fmt: function (v) { return v == null ? '<span class="muted">–</span>' : F.num(v); } }
    ], { empty: 'No matched orders' }));
    var mr = (w.match_rate || []).filter(function (x) { return inF(state, x) && x.rate; })
      .map(function (x) { return (x.brand === 'lekmer' ? 'Lekmer ' : '') + x.market + ' ' + F.pct(x.rate, 0); });
    var gp = n.google_platform || {}, se = inBrand(state, 'babyshop') ? gp['babyshop:SE'] : null;
    (n.unavailable || []).forEach(function (t) { if (inBrand(state, /^lekmer/i.test(t) ? 'lekmer' : 'babyshop')) h.push('<div class="bm-note warn">' + esc(t) + '</div>'); });
    h.push('<div class="bm-note">Order ↔ GA4 match rate: ' + mr.join(' · ') + '.' +
      (se && se.new_share != null ? ' Cross-check: Google\'s own new-customer label (SE account only — the others lack customer-acquisition data) puts ' + F.pct(se.new_share, 0) + ' of SE conversions as new, L28.' : '') + '</div>');
    return h.join('');
  }

  /* ── GP3 + marginal ROAS ───────────────────────────────────────────────── */
  function gp3Html(d, state) {
    var w = d.gp3[ui.gp3Win] || d.gp3.l28, h = [];
    var mar = {};
    d.marginal.forEach(function (r) { mar[(r.brand || 'babyshop') + '|' + r.market + '|' + r.channel] = r; });
    var rows = w.rows.filter(function (r) { return inF(state, r) && (r.spend || r.gp2); }).map(function (r) {
      var m = mar[(r.brand || 'babyshop') + '|' + r.market + '|' + r.channel] || {};
      var mv = m.marginal, conf = m.confidence;
      var move = conf === 'none' || conf === 'low' ? ['muted', 'too noisy'] :
        mv >= 1.3 ? ['up', 'add budget'] : mv < 1.0 ? ['down', 'trim'] : ['', 'hold'];
      return Object.assign({}, r, { mroas: mv, conf: conf, fit: m, move: move });
    });
    h.push('<div class="pd-head"><h3>GP3 by market × channel · where the next krona goes</h3>' +
      tabs('gp3', [['mtd', 'MTD'], ['l14', 'L14'], ['l28', 'L28']], ui.gp3Win) + '</div>');
    h.push('<div class="pd-sub">GP3 = GP2 − spend. GP2 = channel revenue (Funnel, GA4 last click) × the market\'s GP2 margin on pacing\'s Norce basis (merchandise − returns − COGS − shipping/pick &amp; pack); rows marked "Funnel" fall back to Funnel\'s own GP2. Marginal ROAS = GP2 from the last krona, estimated from 28 days of daily spend/GP2 pairs — an estimate, not a forecast. Above 1.0 the last krona still adds GP3.</div>');
    h.push(BM.table(rows, [
      { key: 'market', label: 'Mkt', fmt: mkb },
      { key: 'channel', label: 'Channel', fmt: function (v) { return esc(CH_LABEL[v] || v); } },
      { key: 'spend', label: 'Spend', num: true, fmt: F.sek },
      { key: 'gp2', label: 'GP2', num: true, fmt: function (v, r) { return F.sek(v) + (r.gp2_basis === 'funnel' ? ' <span class="muted" style="font-size:11px">Funnel</span>' : ''); } },
      { key: 'gp3', label: 'GP3', num: true, fmt: function (v) { return '<span class="' + (v < 0 ? 'down' : 'up') + '">' + F.sek(v) + '</span>'; } },
      { key: 'gp2_roas', label: 'GP2 ROAS (avg)', num: true, fmt: function (v) { return v == null ? '–' : '<span class="' + (v < 1 ? 'down' : '') + '">' + F.roas(v) + '</span>'; } },
      { key: 'mroas', label: 'Marginal ROAS (est.)', num: true, fmt: function (v, r) {
          if (v == null) return '<span class="muted">–</span>';
          var t = 'slope ' + r.fit.b + ', R² ' + r.fit.r2 + ', ' + r.fit.n + ' days, spend CV ' + r.fit.spend_cv;
          return '<span title="' + esc(t) + '" class="' + (r.conf === 'low' ? 'muted' : v < 1 ? 'down' : 'up') + '">' + F.roas(v) + '</span> <span class="muted" style="font-size:11px">' + esc(r.conf) + '</span>'; } },
      { key: 'move', label: 'Signal', fmt: function (v) { return pill(v[0], v[1]); } }
    ]));
    var mrows = (w.markets || []).filter(function (r) { return inF(state, r) && (r.norce_rev || r.paid_spend); });
    if (mrows.length) {
      h.push('<h3>Actual sales vs paid spend (Norce ordered)</h3><div class="pd-sub">Norce ordered revenue, ex VAT, gross of returns, Stockholm days, ' + F.date(w.from) + '–' + F.date(w.to) + '. Paid COS = Google + Meta spend ÷ merchandise revenue.</div>');
      h.push(BM.table(mrows, [
        { key: 'market', label: 'Mkt', fmt: mkb },
        { key: 'norce_rev', label: 'Ordered revenue', num: true, fmt: F.sek },
        { key: 'norce_merch', label: 'of which merchandise', num: true, fmt: F.sek },
        { key: 'orders', label: 'Orders', num: true, fmt: F.num },
        { key: 'paid_spend', label: 'Google + Meta spend', num: true, fmt: F.sek },
        { key: 'paid_cos', label: 'Paid COS', num: true, fmt: function (v) { return F.pct(v, 1); } },
        { key: 'cos_target', label: 'COS target (all marketing)', num: true, fmt: function (v) { return '<span class="muted">' + F.pct(v, 1) + '</span>'; } }
      ]));
    }
    return h.join('');
  }

  function kpis(d, state) {
    var n = asofIdx(d) + 1, ga = 0, gy = 0, mp = 0, ma = 0, err = 0, lower = 0, raise = 0, pbMiss = 0, mUp = 0, mDown = 0;
    var gp = gPacing(d, state), pbb = d.google.post_bf_by_brand || { babyshop: d.google.post_bf };
    MKTS.forEach(function (m) {
      if (!inMkt(state, m)) return;
      var g = gp[m];
      if (g) { ga += sumTo(g.actual, n); gy += n > 0 ? (g.actual[n - 1] || 0) : 0; }
      var x = d.meta.pacing.market[m];
      if (x && metaOn(d, state)) { mp += sumTo(x.plan, n); ma += sumTo(x.actual, n); }
      Object.keys(pbb).forEach(function (b) {
        var pb = pbb[b][m];
        if (pb && inBrand(state, b) && (!pb.day1 || !pb.day2)) pbMiss++;
      });
    });
    d.google.campaigns.forEach(function (c) {
      if (!inF(state, c)) return;
      if (c.budget_limited) err++;
      var a = (c.suggestion || {}).action;
      if (a === 'lower') lower++; else if (a === 'raise') raise++;
    });
    (d.meta.lines || []).forEach(function (l) {
      if (!inF(state, l)) return;
      var a = (l.suggestion || {}).action;
      if (a === 'up' || a === 'test') mUp++; else if (a === 'down') mDown++;
    });
    function k(lbl, val_, sub, cls) {
      return '<div class="bm-kpi' + (cls ? ' ' + cls : '') + '"><div class="lbl">' + lbl + '</div><div class="val">' + val_ + '</div><div class="sub">' + sub + '</div></div>';
    }
    return '<div class="bm-kpis">' +
      k('Google tROAS moves', lower + ' ↓ · ' + raise + ' ↑', 'lower = spend more · raise = spend less') +
      k('Meta lines', mUp + ' up · ' + mDown + ' down', 'by incremental marginal GP3') +
      k('Budget-limited', String(err), err ? 'ERROR: budgets must never bind' : 'none, as intended', err ? 'pd-kpi-err' : '') +
      k('Google spend', F.sek(gy), 'yesterday · MTD ' + F.sek(ga) + ' (context)') +
      k('Meta spend MTD', F.sek(ma), 'starting plan ' + F.sek(mp) + ' to date') +
      k('Post-BF adjustments', pbMiss ? pbMiss + ' missing' : 'All set', '1 Dec −45 % · 2 Dec −30 %', pbMiss ? 'pd-kpi-alert' : '') +
      '</div>';
  }

  function render(el, d, state) {
    injectCss();
    last = { el: el, d: d, state: state };
    var h = [guardrailHtml(d, state), kpis(d, state), googleHtml(d, state), metaPointer(), nvrHtml(d, state), gp3Html(d, state)];
    h.push('<details><summary>Data notes</summary><ul class="bm-note pd-notes">' + d.notes.map(function (n) { return '<li>' + esc(n) + '</li>'; }).join('') + '</ul></details>');
    if (d.errors && d.errors.length) h.push('<div class="bm-error" style="font-size:12px;margin-top:8px">' + d.errors.map(esc).join('<br>') + '</div>');
    el.innerHTML = h.join('');

    // Charts
    var mc = el.querySelector('#pd-m-chart');
    if (mc) {
      var tcol = { kuling: BM.color.accent, prospecting: BM.color.fc, retargeting: BM.color.market('NO') };
      var ms = Object.keys(d.plan.meta_by_type).map(function (t) {
        var share = state.market === 'all' ? 1 : (d.plan.meta_share[state.market] || 0);
        var f = 1;
        if (state.market !== 'all') {
          var tot = 0, mine = 0;
          d.meta.pacing.market_type_mtd.forEach(function (r) { if (r.type === t) { tot += r.spend; if (r.market === state.market) mine += r.spend; } });
          f = tot ? mine / tot : 0;
        }
        var p = d.meta.pacing.type[t];
        return { label: TYPE_LABEL[t], plan: p.plan.map(function (v) { return v * share; }),
          actual: p.actual.map(function (v) { return v == null ? null : v * f; }), color: tcol[t] };
      });
      cumChart(mc, d, ms);
    }

    // Tab handlers (rebuild from the cached args)
    el.querySelectorAll('.pd-tabs').forEach(function (t) {
      t.addEventListener('click', function (e) {
        var b = e.target.closest('button'); if (!b) return;
        var v = b.getAttribute('data-v'), name = t.getAttribute('data-tab');
        if (name === 'gfilter') { ui.gOnlyAction = v === 'act'; }
        else if (name === 'gall') ui.gAll = v === 'more';
        else if (name === 'nvr') ui.nvrWin = v;
        else if (name === 'gp3') ui.gp3Win = v;
        else if (name === 'prod') ui.prodView = v;
        render(last.el, last.d, last.state);
      });
    });
  }

  BM.register('paid', { title: 'Paid media levers', render: render });
})();
