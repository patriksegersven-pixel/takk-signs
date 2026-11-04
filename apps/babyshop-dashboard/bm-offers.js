/* bm-offers.js — "Offers & stock" section of the Black Month tab.
 * Payload: bm_offers.py. Answers: which offers run where, how deep/wide they
 * are, what they do to GP2, and which hero products will not last to Cyber
 * Monday (plus Shopping spend still going to sold-out / low-cover SKUs). */
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
    low:   ['down', '< 7 days', 'under 7 days of cover'],
    sizes: ['warn', 'Sizes gone', 'with a third of selling sizes gone'],
    bw:    ['warn', 'Short by CM', 'short by Cyber Monday'],
    ok:    ['muted', 'OK', 'ok']
  };

  /* ── action list: what to do today ─────────────────────────────────── */
  function actions(d, m) {
    var out = [], today = d.today;
    var soon = new Date(today + 'T12:00:00'); soon.setDate(soon.getDate() + 7);
    var soonIso = soon.toISOString().slice(0, 10);
    var gaps = d.offers.filter(function (o) {
      return !o.hidden && o.status !== 'ended' && o.warnings.length && o.start <= soonIso &&
        (m === 'all' || o.markets.indexOf(m) >= 0);
    });
    if (gaps.length) out.push(['warn', '<b>' + gaps.length + ' offer' + (gaps.length > 1 ? 's' : '') +
      ' starting within 7 days</b> with gaps in the calendar: ' + gaps.map(function (o) {
        return esc(o.name.slice(0, 40)) + ' (' + esc(o.warnings.join(', ')) + ')';
      }).join('; ') + '.']);

    var e = d.economics;
    if (e && e.bands && e.bands[m]) {
      e.bands[m].l14.forEach(function (b) {
        if (b.gp2_m != null && b.gp2_m < 0.05 && b.share > 0.01)
          out.push(['down', '<b>' + esc(b.band) + ' lines lose money</b>: GP2 margin ' + F.pct(b.gp2_m) +
            ' on ' + F.sek(b.rev) + ' (' + F.pct(b.share) + ' of revenue, last 14 days). Cap depth or pull these SKUs from ads.']);
      });
    }
    if (e && e.per_offer) {
      e.per_offer.forEach(function (po) {
        var o = d.offers.filter(function (x) { return x.id === po.id; })[0];
        var r = po.by_market[m];
        if (!o || !r || !r.during || !r.pre || o.status !== 'live') return;
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
      var prods = st.products.filter(function (p) { return mval(p.rev, m) > 0; });
      var short = prods.filter(function (p) { return p.flag !== 'ok'; })
        .sort(function (a, b) { return mval(b.rev, m) - mval(a.rev, m); });
      var urgent = short.filter(function (p) { return p.flag === 'oos' || p.flag === 'low'; });
      var cnt = {};
      short.forEach(function (p) { cnt[p.flag] = (cnt[p.flag] || 0) + 1; });
      if (short.length) out.push([urgent.length ? 'down' : 'warn', '<b>Hero stock:</b> ' +
        ['oos', 'low', 'sizes', 'bw'].filter(function (f) { return cnt[f]; }).map(function (f) {
          return cnt[f] + ' ' + FLAG[f][2];
        }).join(' · ') + '. Biggest: ' +
        (urgent.length ? urgent : short).slice(0, 3).map(function (p) {
          return esc(p.title.slice(0, 40)) + ' (' + F.sek(mval(p.rev, m)) + ', ' + FLAG[p.flag][2] + ')';
        }).join(', ') + '. Reorder or shift ad money to products with cover.']);
      var sp = st.ad_spend && st.ad_spend[m];
      if (sp && (sp.oos + sp.gone + sp.low) > 0)
        out.push(['warn', '<b>' + F.sek(sp.oos + sp.gone + sp.low) + ' SEK Shopping spend</b> last 7 days went to SKUs that are sold out (' +
          F.sek(sp.oos + sp.gone) + ') or under 7 days of cover (' + F.sek(sp.low) + '), ' + F.pct((sp.oos + sp.gone + sp.low) / sp.total) +
          ' of Shopping spend. Exclude them or lower their bids.']);
    }
    if (!out.length) out.push(['up', 'Nothing urgent in offers or stock for this market.']);
    return '<ul class="of-actions">' + out.map(function (a) { return '<li class="' + a[0] + '">' + a[1] + '</li>'; }).join('') + '</ul>';
  }

  /* ── KPIs: last 14 days vs the 14 before ───────────────────────────── */
  function kpis(e, m) {
    var bm = e && e.by_market[m];
    if (!bm || !bm.l14) return '<p class="muted">No offer economics for this market.</p>';
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
      '</div><p class="bm-note">Last 14 days ' + dmy(e.windows.l14.from) + '–' + dmy(e.windows.l14.to) +
      ' vs ' + dmy(e.windows.p14.from) + '–' + dmy(e.windows.p14.to) + '. Norce orders (gross), SEK ex VAT.</p>';
  }

  /* ── offer calendar ────────────────────────────────────────────────── */
  function calendar(d, m) {
    var per = {};
    ((d.economics && d.economics.per_offer) || []).forEach(function (p) { per[p.id] = p; });
    var rows = d.offers.filter(function (o) {
      return (m === 'all' || o.markets.indexOf(m) >= 0) && !(o.hidden && o.status !== 'live');
    });
    var cols = [
      { key: 'status', label: '', fmt: function (v) { return '<span class="bm-pill ' + STATUS[v] + '">' + v + '</span>'; } },
      { key: 'start', label: 'Dates', fmt: function (v, o) { return dmy(o.start) + '–' + dmy(o.end); } },
      { key: 'name', label: 'Offer', fmt: function (v, o) {
          return '<div class="of-name wrap">' + esc(v) + '<small>' + esc([o.type, o.offer_type].filter(Boolean).join(' · ')) +
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
    var ctx = d.context.filter(function (c) { return m === 'all' || c.markets.indexOf(m) >= 0; });
    return BM.table(rows, cols, { empty: 'No offers in the calendar for this market.' }) +
      (ctx.length ? '<details class="of-ctx"><summary>Also running, no offer (' + ctx.length + ')</summary>' + ctx.map(function (c) {
        return '<span>' + esc(c.name.slice(0, 50)) + ' ' + dmy(c.start) + '–' + dmy(c.end) + '</span>';
      }).join('') + '</details>' : '') +
      '<p class="bm-note">From the Airtable campaign calendar. Depth and scope are read from the name and brief (no depth field). ' +
      '"vs pre" = the offer\'s days so far vs the 14 days before it started, in the offer\'s markets; other offers overlap.</p>';
  }

  /* ── daily chart + depth bands ─────────────────────────────────────── */
  function dailyChart(el, e, m) {
    var cv = el.querySelector('#of-daily');
    var s = (e && e.daily && e.daily[m]) || [];
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
    var b = e && e.bands && e.bands[m];
    if (!b) return '';
    var prev = {};
    (b.p14 || []).forEach(function (r) { prev[r.band] = r; });
    var max = Math.max.apply(null, b.l14.map(function (r) { return r.share; }).concat([0.01]));
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
      (e.cts_per_order && m !== 'all' && e.cts_per_order[m] ? ' (' + e.cts_per_order[m] + ' SEK/order in ' + m + ')' : '') + '.</p>';
  }

  /* ── width by category ─────────────────────────────────────────────── */
  function width(w, m) {
    var rows = (w || []).filter(function (r) { return r.m[m] && r.m[m].rev > 0; })
      .sort(function (a, b) { return b.m[m].rev - a.m[m].rev; }).slice(0, 12);
    return BM.table(rows, [
      { key: 'cat', label: 'Category' },
      { key: 'm', label: 'Revenue 14 d', num: true, fmt: function (v) { return F.sek(v[m].rev); } },
      { key: 'm', label: 'Sold on offer', num: true, fmt: function (v) { return F.pct(v[m].off_share, 0); } },
      { key: 'm', label: 'Realised depth', num: true, fmt: function (v) { return F.pct(v[m].depth, 0); } },
      { key: 'feed_on_sale', label: 'SKUs on markdown', num: true, fmt: function (v, r) { return v == null ? '–' : F.pct(v, 0) + ' <span class="muted">of ' + F.num(r.skus_in_stock) + '</span>'; } },
      { key: 'feed_depth', label: 'Markdown depth', num: true, fmt: function (v) { return F.pct(v, 0); } }
    ], { empty: 'No category data.' }) +
      '<p class="bm-note">Realised = what customers paid on offer lines. SKUs on markdown and their depth come from the feed today (SE prices, in-stock SKUs).</p>';
  }

  /* ── hero products & stock ─────────────────────────────────────────── */
  function heroes(st, m) {
    if (!st) return '<p class="muted">Stock data unavailable.</p>';
    var list = st.products.filter(function (p) { return mval(p.rev, m) > 0 || mval(p.ad7, m) > 0; });
    var probs = list.filter(function (p) { return p.flag !== 'ok' || mval(p.ad7_oos, m) + mval(p.ad7_low, m) > 0; });
    var rows = (heroMode === 'problems' ? probs : list.slice().sort(function (a, b) { return mval(b.rev, m) - mval(a.rev, m); }).slice(0, 50))
      .sort(function (a, b) { return mval(b.rev, m) - mval(a.rev, m); });
    var sp = st.ad_spend && st.ad_spend[m];
    var head = sp ? '<div class="bm-kpis" style="margin-bottom:10px">' +
      '<div class="bm-kpi"><div class="lbl">Shopping spend 7 d</div><div class="val">' + F.sek(sp.total) + '</div><div class="sub">Google Shopping + PMax, product level</div></div>' +
      '<div class="bm-kpi"><div class="lbl">… on sold-out SKUs</div><div class="val ' + (sp.oos + sp.gone > 0 ? 'down' : '') + '">' + F.sek(sp.oos + sp.gone) + '</div><div class="sub">' + F.pct((sp.oos + sp.gone) / (sp.total || 1)) + ' · ' + F.sek(sp.gone) + ' already dropped from the feed</div></div>' +
      '<div class="bm-kpi"><div class="lbl">… on &lt; 7 days cover</div><div class="val ' + (sp.low > 0 ? 'warn' : '') + '">' + F.sek(sp.low) + '</div><div class="sub">' + F.pct(sp.low / (sp.total || 1)) + ' of Shopping spend</div></div>' +
      '<div class="bm-kpi"><div class="lbl">Products to act on</div><div class="val">' + probs.length + '</div><div class="sub">stock problem or spend on sold-out / low SKUs · "Problems only"</div></div>' +
      '</div>' : '<p class="bm-note">Google Ads product spend unavailable this run.</p>';
    var cols = [
      { key: 'flag', label: '', fmt: function (v) { return '<span class="bm-pill ' + FLAG[v][0] + '">' + FLAG[v][1] + '</span>'; } },
      { key: 'title', label: 'Product', fmt: function (v, p) {
          var t = esc(v);
          return '<div class="of-prod" title="' + t + '">' + (p.url ? '<a href="' + esc(p.url) + '" target="_blank" rel="noopener">' + t + '</a>' : t) +
            '<br><small>' + esc(p.brand) + (p.cat ? ' · ' + esc(p.cat) : '') + '</small></div>';
        } },
      { key: 'rev', label: 'Rev 14 d', num: true, fmt: function (v) { return F.sek(mval(v, m)); } },
      { key: 'depth', label: 'Markdown', num: true, fmt: function (v) { return v ? F.pct(v, 0) : '<span class="muted">–</span>'; } },
      { key: 'stock', label: 'Stock', num: true, fmt: F.num },
      { key: 'cover', label: 'Days cover', num: true, fmt: function (v) {
          return v == null ? '–' : '<span class="' + (v < 7 ? 'down' : v < 21 ? 'warn' : '') + '">' + F.num(v) + '</span>';
        } },
      { key: 'short_to_cm', label: 'Short by CM', num: true, fmt: function (v) { return v ? '<span class="warn">' + F.num(v) + '</span>' : '<span class="muted">0</span>'; } },
      { key: 'oos_selling', label: 'Sizes out', num: true, fmt: function (v, p) {
          return p.skus_selling ? '<span class="' + (v ? 'warn' : 'muted') + '">' + v + '/' + p.skus_selling + '</span>' : '–';
        } },
      { key: 'ad7', label: 'Shopping 7 d', num: true, fmt: function (v) { return F.sek(mval(v, m)); } },
      { key: 'ad7_oos', label: 'on sold-out / low', num: true, fmt: function (v, p) {
          var x = mval(v, m) + mval(p.ad7_low, m);
          return x ? '<span class="down">' + F.sek(x) + '</span>' : '<span class="muted">–</span>';
        } }
    ];
    var u = st.bw_uplift || {};
    return head + BM.table(rows, cols, { empty: 'No products.' }) +
      '<p class="bm-note">Stock is shared across markets; cover uses all-market sales ' + dmy(st.velocity_window[0]) + '–' + dmy(st.velocity_window[1]) +
      '. "Short by CM" assumes last year\'s Black Week pace (×' + (u.all ? String(u.all).replace('.', ',') : '–') +
      ' units/day vs late Oct) from 23 Nov. ' + F.num(st.gone_from_feed.skus) + ' SKUs that sold in the last 14 days (' + F.sek(st.gone_from_feed.rev14) +
      ' SEK) are no longer in the feed and count as sold out.</p>';
  }

  BM.register('offers', {
    title: 'Offers & stock',
    render: function (el, d, state) {
      injectCss();
      var m = state.market || 'all', e = d.economics;
      var notes = (d.notes || []).map(function (n) { return '<li>' + esc(n) + '</li>'; }).join('');
      var failed = Object.keys(d.sources || {}).filter(function (k) { return d.sources[k] && d.sources[k].ok === false; });
      el.innerHTML =
        actions(d, m) +
        kpis(e, m) +
        '<div class="of-sub"><h3>Offer calendar</h3></div>' + calendar(d, m) +
        '<div class="bm-grid-2"><div><h3>Daily: revenue, depth and GP2 margin</h3><div class="bm-chart"><canvas id="of-daily"></canvas></div></div>' +
        '<div><h3>GP2 by discount depth</h3>' + bands(e, m) + '</div></div>' +
        '<h3>Width and depth by category</h3>' + width(d.width, m) +
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
