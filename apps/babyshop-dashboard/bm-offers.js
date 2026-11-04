/* bm-offers.js — "Offers & stock" section of the Black Month tab.
 * Payload: bm_offers.py. Answers: which offers run where, how deep/wide they
 * are, what they do to GP2, which hero products will not last to Black Week /
 * Cyber Monday, and Shopping spend on days a SKU was actually out of stock.
 * Respects state.market and state.brand ('all' | 'babyshop' | 'lekmer'). */
(function () {
  'use strict';
  var F = BM.fmt, esc = BM.esc;
  var heroMode = 'top';      // 'top' | 'problems' — survives market switches

  var CSS = [
    '#bm-offers .of-actions{display:grid;gap:6px;margin:0 0 14px;padding:0;list-style:none}',
    '#bm-offers .of-actions li{background:var(--card-2);border:1px solid var(--border-lt);border-left:3px solid var(--warn);border-radius:6px;padding:8px 12px;font-size:13px;color:var(--body)}',
    '#bm-offers .of-actions li.down{border-left-color:var(--down)}',
    '#bm-offers .of-actions li.up{border-left-color:var(--up)}',
    '#bm-offers .of-actions b{color:var(--h)}',
    '#bm-offers .of-sub{display:flex;justify-content:space-between;align-items:baseline;gap:10px;flex-wrap:wrap}',
    '#bm-offers .of-sub h3{margin:18px 0 8px}',
    '#bm-offers .of-toggle{display:flex;gap:4px}',
    '#bm-offers .of-toggle button{background:var(--card-2);color:var(--body);border:1px solid var(--border);border-radius:999px;padding:3px 10px;font:inherit;font-size:12px;cursor:pointer}',
    '#bm-offers .of-toggle button.on{background:var(--accent);color:#111;border-color:var(--accent);font-weight:600}',
    '#bm-offers .of-name{max-width:340px;color:var(--h)}',
    '#bm-offers .of-name small{display:block;color:var(--muted);font-size:11px;margin-top:2px}',
    '#bm-offers .of-prod{max-width:300px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}',
    '#bm-offers .of-prod a{color:var(--h);text-decoration:none}',
    '#bm-offers .of-prod a:hover{color:var(--accent)}',
    '#bm-offers .of-prod small{color:var(--muted)}',
    '#bm-offers .of-warn{color:var(--warn);font-size:11px;display:block}',
    '#bm-offers .of-bar{display:inline-block;height:8px;background:var(--accent);border-radius:2px;vertical-align:middle;margin-right:6px;opacity:.8}',
    '#bm-offers .of-ctx{color:var(--muted);font-size:12px;margin-top:6px;line-height:1.7}',
    '#bm-offers .of-ctx summary{cursor:pointer;margin-bottom:4px}',
    '#bm-offers .of-ctx span{display:inline-block;background:var(--card-2);border:1px solid var(--border-lt);border-radius:999px;padding:0 8px;margin:0 4px 4px 0}',
    '#bm-offers .of-notes{margin:6px 0 0;padding-left:18px}',
    '#bm-offers .of-notes li{margin:3px 0}',
    '#bm-offers .bm-table td{white-space:nowrap}',
    '#bm-offers .bm-table td.wrap{white-space:normal}',
    '#bm-offers .bm-kpi .val small{font-size:13px;color:var(--muted);font-weight:500}',
    '@media (max-width:640px){#bm-offers .of-name{max-width:220px}#bm-offers .of-prod{max-width:170px}#bm-offers .bm-kpis{grid-template-columns:1fr 1fr;gap:8px}#bm-offers .bm-kpi{padding:10px}#bm-offers .bm-kpi .val{font-size:19px}}'
  ].join('');

  function injectCss() {
    if (document.getElementById('bm-offers-css')) return;
    var s = document.createElement('style');
    s.id = 'bm-offers-css';
    s.textContent = CSS;
    document.head.appendChild(s);
  }

  function pp(v) {            // percentage-point delta, coloured
    if (v == null || isNaN(v)) return '<span class="muted">–</span>';
    var s = (v >= 0 ? '+' : '−') + Math.abs(v * 100).toFixed(1).replace('.', ',') + ' pp';
    return '<span class="' + (v >= 0 ? 'up' : 'down') + '">' + s + '</span>';
  }
  function rel(a, b) { return a != null && b ? a / b - 1 : null; }
  function pct0(v) { return v == null ? '–' : Math.round(v * 100) + ' %'; }
  function sum(o) { var t = 0; for (var k in o) t += o[k] || 0; return t; }
  function mval(o, m) {        // per-market dicts; 'all' = stored total, else the sum
    if (!o) return 0;
    if (m !== 'all') return o[m] || 0;
    return o.all != null ? o.all : sum(o);
  }
  function dmy(iso) { var p = iso.split('-'); return +p[2] + '/' + +p[1]; }

  var STATUS = { live: 'up', upcoming: 'warn', ended: 'muted' };
  var FLAG = {
    oos:   ['down', 'Sold out', 'sold out'],
    prebw: ['warn', 'Out before BW', 'selling out before Black Week'],
    sizes: ['warn', 'Sizes gone', 'with a third of selling sizes gone'],
    bw:    ['warn', 'Short by CM', 'short by Cyber Monday'],
    ok:    ['muted', 'OK', 'ok']
  };
  var B = 'all';               // current brand filter, set by render()
  var BRAND_LABEL = { all: 'Babyshop + Lekmer', babyshop: 'Babyshop', lekmer: 'Lekmer' };

  /* brand views of a row. Products: stock is shared, so one row carries both
   * brands; flat dicts = both, rev_b / ad_b = per brand. */
  function prev_(p) { return B === 'all' ? p.rev : (p.rev_b && p.rev_b[B]) || {}; }
  function pad(p, k) { return B === 'all' ? p[k] : ((p.ad_b && p.ad_b[B]) || {})[k]; }
  function blk(o) { return o && o[B]; }                // economics blocks keyed [brand][market]
  function brandOk(row) { return B === 'all' || row.brand === B || row.brand === 'all'; }
  function offersFor(d, m) {                          // 'all' shows a two-storefront offer once
    var seen = {};
    return d.offers.filter(function (o) {
      if (!brandOk(o) || !(m === 'all' || o.markets.indexOf(m) >= 0)) return false;
      var k = o.id.split(':')[0];
      if (B === 'all' && seen[k]) return false;
      seen[k] = 1; return true;
    });
  }

  /* ── action list: what to do today ─────────────────────────────────── */
  function actions(d, m) {
    var out = [], today = d.today;
    var soon = new Date(today + 'T12:00:00'); soon.setDate(soon.getDate() + 7);
    var soonIso = soon.toISOString().slice(0, 10);
    var gaps = offersFor(d, m).filter(function (o) {
      return !o.hidden && o.status !== 'ended' && o.warnings.length && o.start <= soonIso;
    });
    if (gaps.length) out.push(['warn', '<b>' + gaps.length + ' offer' + (gaps.length > 1 ? 's' : '') +
      ' starting within 7 days</b> with gaps in the calendar: ' + gaps.map(function (o) {
        return esc(o.name.slice(0, 40)) + ' (' + esc(o.warnings.join(', ')) + ')';
      }).join('; ') + '.']);
    if (B === 'lekmer' && !offersFor(d, m).some(function (o) { return o.status !== 'ended'; }))
      out.push(['warn', '<b>No Lekmer offers</b> live or planned in the campaign calendar for November.']);

    var e = d.economics, bb = e && blk(e.bands);
    if (bb && bb[m]) {
      bb[m].l14.forEach(function (b) {
        if (b.gp2_m != null && b.gp2_m < 0.05 && b.share > 0.01)
          out.push(['down', '<b>' + esc(b.band) + ' lines lose money</b>: GP2 margin ' + F.pct(b.gp2_m) +
            ' on ' + F.sek(b.rev) + ' (' + F.pct(b.share) + ' of revenue, last 14 days). Cap depth on these SKUs or take them out of ads.']);
      });
    }
    if (e && e.per_offer) {
      e.per_offer.forEach(function (po) {
        var o = d.offers.filter(function (x) { return x.id === po.id; })[0];
        var r = po.by_market[m];
        if (!o || !brandOk(o) || !r || !r.during || !r.pre || o.status !== 'live') return;
        var dm = r.during.gp2_m - r.pre.gp2_m, dr = rel(r.during.rev_day, r.pre.rev_day);
        var dg = rel(r.during.gp2_day, r.pre.gp2_day);
        if (Math.abs(dm) >= 0.02)
          out.push([dg != null && dg < 0 ? 'down' : 'up', '<b>' + esc(o.name.slice(0, 48)) + '</b> (live): revenue/day ' +
            F.delta(dr) + ', GP2/day ' + F.delta(dg) + ', GP2 margin ' + F.pct(r.during.gp2_m) + ' vs ' +
            F.pct(r.pre.gp2_m) + ' the 14 days before.']);
      });
    }
    var st = d.stock;
    if (st) {
      var prods = st.products.filter(function (p) { return mval(prev_(p), m) > 0; })
        .sort(function (a, b) { return mval(prev_(b), m) - mval(prev_(a), m); });
      var oos = prods.filter(function (p) { return p.flag === 'oos'; });
      if (oos.length) out.push(['down', '<b>' + oos.length + ' hero product' + (oos.length > 1 ? 's' : '') + ' sold out</b>: ' +
        oos.slice(0, 3).map(function (p) { return esc(p.title.slice(0, 40)) + ' (' + F.sek(mval(prev_(p), m)) + ' in 14 d)'; }).join(', ') +
        '. Reorder or point traffic to alternatives.']);
      var pre = prods.filter(function (p) { return p.flag === 'prebw'; });
      if (pre.length) {
        var gp = pre.filter(function (p) { return p.gp2_unit != null; });
        out.push(['warn', '<b>' + pre.length + ' hero products will sell out before Black Week</b> at today\'s pace (' +
          pre.slice(0, 3).map(function (p) {
            return esc(p.title.slice(0, 34)) + ': ' + F.num(p.cover) + ' d cover, GP2 ' + (p.gp2_unit == null ? '–' : F.num(p.gp2_unit)) + '/unit';
          }).join('; ') + '). Protect stock for Black Week (ease ads or depth) or keep pushing if GP2/unit is low and stock is not coming back' +
          (gp.length ? '' : '') + '.']);
      }
      var sp = blk(st.ad_spend) && blk(st.ad_spend)[m];
      if (sp && sp.oos_today >= 100) {
        var live = (st.oos_now || []).filter(function (r) {
          return brandOk(r) && (m === 'all' || r.m === m) && r.spend_today > 0 && r.still_oos;
        });
        out.push([sp.oos_today >= 500 ? 'down' : 'warn', '<b>' + F.sek(sp.oos_today) + ' SEK Shopping spend today on ' + live.length +
          ' out-of-stock SKU' + (live.length === 1 ? '' : 's') + '</b>' + (live.length ? ' (e.g. ' + live.slice(0, 2).map(function (r) { return esc(r.title.slice(0, 32)); }).join(', ') + ')' : '') +
          '. Last 7 days: ' + F.sek(sp.oos_7d) + ' SEK on days the SKU was out of stock (' + F.pct(sp.oos_7d / (sp.total_7d || 1)) + ' of Shopping).']);
      } else if (sp) {
        out.push(['up', '<b>No meaningful Shopping spend on out-of-stock SKUs</b>: ' + F.sek(sp.oos_7d) + ' SEK in 7 days (' +
          F.pct(sp.oos_7d / (sp.total_7d || 1)) + ' of Shopping), counted only on days the SKU was actually out of stock.']);
      }
    }
    if (!out.length) out.push(['up', 'Nothing urgent in offers or stock for this selection.']);
    return '<ul class="of-actions">' + out.map(function (a) { return '<li class="' + a[0] + '">' + a[1] + '</li>'; }).join('') + '</ul>';
  }

  /* ── KPIs: last 14 days vs the 14 before ───────────────────────────── */
  function kpis(e, m) {
    var bm = e && blk(e.by_brand) && blk(e.by_brand)[m];
    if (!bm || !bm.l14) return '<p class="muted">No offer economics for this selection.</p>';
    var a = bm.l14, b = bm.p14 || {};
    function k(lbl, val, sub) {
      return '<div class="bm-kpi"><div class="lbl">' + lbl + '</div><div class="val">' + val + '</div><div class="sub">' + sub + '</div></div>';
    }
    return '<div class="bm-kpis">' +
      k('Revenue / day', F.sek(a.rev_day), F.delta(rel(a.rev_day, b.rev_day)) + ' vs prior 14 d') +
      k('Sold on offer', F.pct(a.off_share), pp(a.off_share - b.off_share) + ' · width') +
      k('Realised depth', F.pct(a.depth_all), pp(a.depth_all - b.depth_all) + ' · ' + pct0(a.md_share) + ' markdown, rest code') +
      k('GP2 margin', F.pct(a.gp2_m), pp(a.gp2_m - b.gp2_m) + ' · offer ' + F.pct(a.gp2_m_off) + ' / full ' + F.pct(a.gp2_m_full)) +
      k('GP2 / day', F.sek(a.gp2_day), F.delta(rel(a.gp2_day, b.gp2_day)) + ' vs prior 14 d') +
      k('GP2 / order', F.num(a.gp2_order) + ' <small>SEK</small>', 'offer orders ' + F.num(a.gp2_order_off) + ' · full-price ' + F.num(a.gp2_order_full)) +
      '</div><p class="bm-note">' + BRAND_LABEL[B] + ', last 14 days ' + dmy(e.windows.l14.from) + '–' + dmy(e.windows.l14.to) +
      ' vs ' + dmy(e.windows.p14.from) + '–' + dmy(e.windows.p14.to) + '. Norce orders (gross), SEK ex VAT.</p>';
  }

  /* ── offer calendar ────────────────────────────────────────────────── */
  function calendar(d, m) {
    var per = {};
    ((d.economics && d.economics.per_offer) || []).forEach(function (p) { per[p.id] = p; });
    var rows = offersFor(d, m).filter(function (o) { return !(o.hidden && o.status !== 'live'); });
    var cols = [
      { key: 'status', label: '', fmt: function (v) { return '<span class="bm-pill ' + STATUS[v] + '">' + v + '</span>'; } },
      { key: 'start', label: 'Dates', fmt: function (v, o) { return dmy(o.start) + '–' + dmy(o.end); } },
      { key: 'name', label: 'Offer', fmt: function (v, o) {
          return '<div class="of-name wrap">' + esc(v) + '<small>' + (B === 'all' ? esc(BRAND_LABEL[o.brand]) + ' · ' : '') +
            esc([o.type, o.offer_type].filter(Boolean).join(' · ')) +
            (o.markets.length < 4 ? ' · ' + esc(o.markets.join(' ')) : '') + '</small>' +
            (o.warnings.length ? '<span class="of-warn">⚠ ' + esc(o.warnings.join(' · ')) + '</span>' : '') + '</div>';
        } },
      { key: 'depth', label: 'Depth', fmt: function (v) { return v.label ? esc(v.label) : '<span class="muted">?</span>'; } },
      { key: 'scope_label', label: 'Scope (width)' },
      { key: 'code', label: 'Code', fmt: function (v) { return v ? '<code>' + esc(v) + '</code>' : '<span class="muted">–</span>'; } },
      { key: 'id', label: 'Rev/day vs pre', num: true, fmt: function (v) {
          var r = per[v] && per[v].by_market[m];
          return r && r.during ? F.sek(r.during.rev_day) + ' ' + F.delta(rel(r.during.rev_day, r.pre && r.pre.rev_day), 0) : '<span class="muted">–</span>';
        } },
      { key: 'id', label: 'GP2 margin vs pre', num: true, fmt: function (v) {
          var r = per[v] && per[v].by_market[m];
          return r && r.during ? F.pct(r.during.gp2_m) + ' ' + pp(r.pre ? r.during.gp2_m - r.pre.gp2_m : null) : '<span class="muted">–</span>';
        } }
    ];
    var ctx = d.context.filter(function (c) { return brandOk(c) && (m === 'all' || c.markets.indexOf(m) >= 0); });
    return BM.table(rows, cols, { empty: 'No offers in the calendar for ' + BRAND_LABEL[B] + (m === 'all' ? '' : ' ' + m) + '.' }) +
      (ctx.length ? '<details class="of-ctx"><summary>Also running, no offer (' + ctx.length + ')</summary>' + ctx.map(function (c) {
        return '<span>' + esc(c.name.slice(0, 50)) + ' ' + dmy(c.start) + '–' + dmy(c.end) + '</span>';
      }).join('') + '</details>' : '') +
      '<p class="bm-note">From the Airtable campaign calendar (storefront = brand). Depth and scope are read from the name and brief (no depth field). ' +
      '"vs pre" = the offer\'s days so far vs the 14 days before it started, in the offer\'s markets; other offers overlap.</p>';
  }

  /* ── daily chart + depth bands ─────────────────────────────────────── */
  function dailyChart(el, e, m) {
    var cv = el.querySelector('#of-daily');
    var s = (e && blk(e.daily) && blk(e.daily)[m]) || [];
    if (!cv || !s.length) return;
    BM.chart(cv, {
      data: {
        labels: s.map(function (r) { return dmy(r.d); }),
        datasets: [
          { type: 'bar', label: 'Revenue', data: s.map(function (r) { return r.rev; }), backgroundColor: BM.color.grid, yAxisID: 'y', order: 3 },
          { type: 'line', label: 'GP2 margin', data: s.map(function (r) { return r.gp2_m * 100; }), borderColor: BM.color.accent, backgroundColor: BM.color.accent, pointRadius: 0, borderWidth: 2, yAxisID: 'y1', tension: .25, order: 1 },
          { type: 'line', label: 'Depth on offer lines', data: s.map(function (r) { return r.depth_offer * 100; }), borderColor: BM.color.fc, backgroundColor: BM.color.fc, pointRadius: 0, borderWidth: 2, yAxisID: 'y1', tension: .25, order: 2 },
          { type: 'line', label: 'Share sold on offer', data: s.map(function (r) { return r.off_share * 100; }), borderColor: BM.color.ly, backgroundColor: BM.color.ly, borderDash: [4, 4], pointRadius: 0, borderWidth: 1.5, yAxisID: 'y1', order: 2 }
        ]
      },
      options: {
        maintainAspectRatio: false, interaction: { mode: 'index', intersect: false },
        plugins: { legend: { labels: { boxWidth: 10, boxHeight: 10 } },
          tooltip: { callbacks: { label: function (c) {
            return c.dataset.label + ': ' + (c.dataset.yAxisID === 'y' ? F.sek(c.raw) : c.raw.toFixed(1).replace('.', ',') + ' %');
          } } } },
        scales: {
          x: { grid: { display: false }, ticks: { maxRotation: 0, autoSkipPadding: 10 } },
          y: { position: 'left', ticks: { callback: function (v) { return F.sek(v); } } },
          y1: { position: 'right', min: 0, max: 100, grid: { display: false }, ticks: { callback: function (v) { return v + ' %'; } } }
        }
      }
    });
  }

  function bands(e, m) {
    var b = e && blk(e.bands) && blk(e.bands)[m];
    if (!b) return '';
    var prev = {};
    (b.p14 || []).forEach(function (r) { prev[r.band] = r; });
    var max = Math.max.apply(null, b.l14.map(function (r) { return r.share; }).concat([0.01]));
    var cts = e.cts_per_order && B !== 'all' && m !== 'all' ? e.cts_per_order[B + ':' + m] : null;
    return BM.table(b.l14, [
      { key: 'band', label: 'Discount band' },
      { key: 'share', label: 'Share of revenue', fmt: function (v) {
          return '<span class="of-bar" style="width:' + Math.round(60 * v / max) + 'px"></span>' + F.pct(v);
        } },
      { key: 'rev', label: 'Revenue', num: true, fmt: F.sek },
      { key: 'gp2_m', label: 'GP2 margin', num: true, fmt: function (v) {
          return '<span class="' + (v < 0.05 ? 'down' : v < 0.2 ? 'warn' : '') + '">' + F.pct(v) + '</span>';
        } },
      { key: 'band', label: 'Prior 14 d', num: true, fmt: function (v) { return prev[v] ? F.pct(prev[v].gp2_m) : '–'; } }
    ]) + '<p class="bm-note">Order lines by discount vs full price, last 14 days. GP2 after COGS and the per-order cost to serve' +
      (cts ? ' (' + cts + ' SEK/order)' : '') + '.</p>';
  }

  /* ── width by category ─────────────────────────────────────────────── */
  function width(w, m) {
    function v_(r) { return (r.b[B] || {})[m]; }
    var rows = (w || []).filter(function (r) { var x = v_(r); return x && x.rev > 0; })
      .sort(function (a, b) { return v_(b).rev - v_(a).rev; }).slice(0, 12);
    return BM.table(rows, [
      { key: 'cat', label: 'Category' },
      { key: 'b', label: 'Revenue 14 d', num: true, fmt: function (v, r) { return F.sek(v_(r).rev); } },
      { key: 'b', label: 'Sold on offer', num: true, fmt: function (v, r) { return F.pct(v_(r).off_share, 0); } },
      { key: 'b', label: 'Realised depth', num: true, fmt: function (v, r) { return F.pct(v_(r).depth, 0); } },
      { key: 'feed_on_sale', label: 'SKUs on markdown', num: true, fmt: function (v, r) { return v == null ? '–' : F.pct(v, 0) + ' <span class="muted">of ' + F.num(r.skus_in_stock) + '</span>'; } },
      { key: 'feed_depth', label: 'Markdown depth', num: true, fmt: function (v) { return F.pct(v, 0); } }
    ], { empty: 'No category data.' }) +
      '<p class="bm-note">Realised = what customers paid on offer lines. SKUs on markdown and their depth come from the feed today (Babyshop SE prices, in-stock SKUs; the catalogue is shared with Lekmer).</p>';
  }

  /* ── Shopping spend vs stock ───────────────────────────────────────── */
  function spendBlock(st, m) {
    var sp = blk(st.ad_spend) && blk(st.ad_spend)[m];
    if (!st.ad_spend) return '<p class="bm-note">Google Ads product spend unavailable this run.</p>';
    if (!sp) return '<p class="bm-note">No ' + BRAND_LABEL[B] + ' Google Ads account in ' + m + '.</p>';
    var daily = (blk(st.oos_daily) || {})[m] || [];
    var dd = daily.slice(-8, -1).map(function (r) { return dmy(r.d) + ' ' + F.sek(r.oos); }).join(' · ');
    function k(lbl, val, sub, cls) {
      return '<div class="bm-kpi"><div class="lbl">' + lbl + '</div><div class="val ' + (cls || '') + '">' + val + '</div><div class="sub">' + sub + '</div></div>';
    }
    var live = (st.oos_now || []).filter(function (r) { return brandOk(r) && (m === 'all' || r.m === m); });
    return '<div class="bm-kpis" style="margin-bottom:10px">' +
      k('Shopping spend 7 d', F.sek(sp.total_7d), 'Google Shopping + PMax, product level · today so far ' + F.sek(sp.total_today)) +
      k('… while out of stock', F.sek(sp.oos_7d), F.pct(sp.oos_7d / (sp.total_7d || 1)) + ' of 7 d · yesterday ' + F.sek(sp.oos_yday), sp.oos_7d / (sp.total_7d || 1) > 0.02 ? 'down' : '') +
      k('… today, still out of stock', F.sek(sp.oos_today), live.filter(function (r) { return r.spend_today > 0 && r.still_oos; }).length + ' SKUs with clicks today and no stock', sp.oos_today >= 500 ? 'down' : sp.oos_today > 0 ? 'warn' : '') +
      k('… on SKUs selling out before BW', F.sek(sp.prebw_7d), 'in stock and selling: not waste. Protect or push? See GP2/unit', '') +
      '</div><p class="bm-note">Out-of-stock spend per day (last 7): ' + (dd || '–') + '.</p>' +
      (live.length ? '<details class="bm-note"><summary>SKUs that got spend while out of stock, today or yesterday (' + live.length + ')</summary>' +
        BM.table(live.slice(0, 30), [
          { key: 'title', label: 'SKU', fmt: function (v, r) { return '<div class="of-prod" title="' + esc(v) + '">' + esc(v) + '<br><small>' + esc(r.maker) + ' · ' + esc(r.part) + (r.in_feed ? '' : ' · not in feed') + '</small></div>'; } },
          { key: 'brand', label: 'Brand', fmt: function (v, r) { return esc(BRAND_LABEL[v] || v) + ' ' + esc(r.m); } },
          { key: 'spend_today', label: 'Today', num: true, fmt: F.num },
          { key: 'spend_yday', label: 'Yesterday', num: true, fmt: F.num },
          { key: 'last_sale', label: 'Last sale', fmt: function (v) { return v ? dmy(v) : '–'; } }
        ]) + '</details>' : '');
  }

  /* ── hero products & stock ─────────────────────────────────────────── */
  function heroes(st, m) {
    if (!st) return '<p class="muted">Stock data unavailable.</p>';
    var list = st.products.filter(function (p) { return mval(prev_(p), m) > 0 || mval(pad(p, 'ad7'), m) > 0; });
    var probs = list.filter(function (p) { return p.flag !== 'ok' || mval(pad(p, 'ad7_oos'), m) > 0; });
    var rows = (heroMode === 'problems' ? probs : list.slice().sort(function (a, b) { return mval(prev_(b), m) - mval(prev_(a), m); }).slice(0, 50))
      .sort(function (a, b) { return mval(prev_(b), m) - mval(prev_(a), m); });
    var cols = [
      { key: 'flag', label: '', fmt: function (v) { return '<span class="bm-pill ' + FLAG[v][0] + '">' + FLAG[v][1] + '</span>'; } },
      { key: 'title', label: 'Product', fmt: function (v, p) {
          var t = esc(v);
          return '<div class="of-prod" title="' + t + '">' + (p.url ? '<a href="' + esc(p.url) + '" target="_blank" rel="noopener">' + t + '</a>' : t) +
            '<br><small>' + esc(p.maker) + (p.cat ? ' · ' + esc(p.cat) : '') + '</small></div>';
        } },
      { key: 'rev', label: 'Rev 14 d', num: true, fmt: function (v, p) { return F.sek(mval(prev_(p), m)); } },
      { key: 'gp2_unit', label: 'GP2 / unit', num: true, fmt: function (v) { return v == null ? '–' : '<span class="' + (v < 0 ? 'down' : '') + '">' + F.num(v) + '</span>'; } },
      { key: 'depth', label: 'Markdown', num: true, fmt: function (v) { return v ? F.pct(v, 0) : '<span class="muted">–</span>'; } },
      { key: 'stock', label: 'Stock', num: true, fmt: F.num },
      { key: 'cover', label: 'Days cover', num: true, fmt: function (v) {
          var pre = st.need_basis.pre_bw_days;
          return v == null ? '–' : '<span class="' + (v < 7 ? 'down' : v < pre ? 'warn' : '') + '">' + F.num(v) + '</span>';
        } },
      { key: 'short_to_cm', label: 'Short by CM', num: true, fmt: function (v) { return v ? '<span class="warn">' + F.num(v) + '</span>' : '<span class="muted">0</span>'; } },
      { key: 'oos_selling', label: 'Sizes out', num: true, fmt: function (v, p) {
          return p.skus_selling ? '<span class="' + (v ? 'warn' : 'muted') + '">' + v + '/' + p.skus_selling + '</span>' : '–';
        } },
      { key: 'ad7', label: 'Shopping 7 d', num: true, fmt: function (v, p) { return F.sek(mval(pad(p, 'ad7'), m)); } },
      { key: 'ad7_oos', label: 'while out of stock', num: true, fmt: function (v, p) {
          var x = mval(pad(p, 'ad7_oos'), m) + mval(pad(p, 'ad_oos_today'), m);
          return x ? '<span class="down">' + F.sek(x) + '</span>' : '<span class="muted">–</span>';
        } }
    ];
    var u = st.bw_uplift || {};
    return BM.table(rows, cols, { empty: 'No products.' }) +
      '<p class="bm-note">Stock is one pool for Babyshop and Lekmer; cover uses all-market sales of both ' + dmy(st.velocity_window[0]) + '–' + dmy(st.velocity_window[1]) +
      '. "Out before BW" = in stock but under ' + st.need_basis.pre_bw_days + ' days of cover (sells out before 23 Nov at this pace). ' +
      '"Short by CM" assumes last year\'s Black Week pace (×' + (u.all ? String(u.all).replace('.', ',') : '–') +
      ' units/day vs late Oct) from 23 Nov. GP2/unit = last 14 days, Norce lines. ' + F.num(st.gone_from_feed.skus) + ' SKUs that sold in the last 14 days (' + F.sek(st.gone_from_feed.rev14) +
      ' SEK) are no longer in the feed and count as sold out.</p>';
  }

  BM.register('offers', {
    title: 'Offers & stock',
    render: function (el, d, state) {
      injectCss();
      var m = state.market || 'all', e = d.economics;
      B = state.brand || 'all';
      var notes = (d.notes || []).map(function (n) { return '<li>' + esc(n) + '</li>'; }).join('');
      var failed = Object.keys(d.sources || {}).filter(function (k) { return d.sources[k] && d.sources[k].ok === false; });
      el.innerHTML =
        actions(d, m) +
        kpis(e, m) +
        '<div class="of-sub"><h3>Offer calendar</h3></div>' + calendar(d, m) +
        '<div class="bm-grid-2"><div><h3>Daily: revenue, depth and GP2 margin</h3><div class="bm-chart"><canvas id="of-daily"></canvas></div></div>' +
        '<div><h3>GP2 by discount depth</h3>' + bands(e, m) + '</div></div>' +
        '<h3>Width and depth by category</h3>' + width(d.width, m) +
        '<h3>Shopping spend vs stock</h3>' + (d.stock ? spendBlock(d.stock, m) : '') +
        '<div class="of-sub"><h3>Hero products and stock</h3><div class="of-toggle">' +
        '<button data-hm="top" class="' + (heroMode === 'top' ? 'on' : '') + '">Top 50</button>' +
        '<button data-hm="problems" class="' + (heroMode === 'problems' ? 'on' : '') + '">Problems only</button></div></div>' +
        heroes(d.stock, m) +
        '<details class="bm-note"><summary>How these numbers are built' + (failed.length ? ' · <span class="down">' + failed.length + ' source(s) failed: ' + esc(failed.join(', ')) + '</span>' : '') + '</summary><ul class="of-notes">' + notes + '</ul></details>';
      el.querySelectorAll('[data-hm]').forEach(function (b) {
        b.addEventListener('click', function () { heroMode = b.getAttribute('data-hm'); BM.setMarket(BM.state.market); });
      });
      dailyChart(el, e, m);
    }
  });
})();
