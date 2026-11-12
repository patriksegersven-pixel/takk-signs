/* bm-meta.js: "Meta · scale / hold / cut" section of the Black Month tab.
 * Payload: bm_meta.build(). Three levels you can move (market → new vs existing
 * customers → catalog/creative × offer/evergreen × line), each with a verdict,
 * the reason, the action, Norce vs Meta economics and frequency. */
(function () {
  'use strict';
  var F = BM.fmt, esc = BM.esc;
  var MKTS = ['SE', 'NO', 'FI', 'DK', 'ROW'];
  var V = {
    scale: ['up', 'Scale'], test: ['up', 'Scale · test'], hold: ['muted', 'Hold'], cut: ['down', 'Cut'], data: ['muted', 'Too little data']
  };
  var ui = { win: 'l7', open: {}, why: {} };   // open: row id → children shown; why: row id → reasons shown
  var last = null;

  var CSS = [
    '#bm-meta .mt-sub{color:var(--muted);font-size:12px;margin:-2px 0 10px}',
    '#bm-meta .mt-tabs{display:flex;gap:4px;flex-wrap:wrap;margin:0 0 8px}',
    '#bm-meta .mt-tabs button{background:var(--card-2);color:var(--body);border:1px solid var(--border);border-radius:999px;padding:3px 10px;font-size:12px;cursor:pointer;font-family:inherit}',
    '#bm-meta .mt-tabs button.on{background:var(--accent);color:#111;border-color:var(--accent);font-weight:600}',
    '#bm-meta .mt-head{display:flex;justify-content:space-between;align-items:baseline;gap:8px;flex-wrap:wrap}',
    '#bm-meta .bm-table td{white-space:nowrap;vertical-align:top;padding:6px 6px}',
    '#bm-meta .bm-table th{white-space:normal;vertical-align:bottom;max-width:110px}',
    '#bm-meta tr.mt-r{cursor:pointer}',
    '#bm-meta tr.lv-market td{font-weight:600;color:var(--h);border-top:1px solid var(--border)}',
    '#bm-meta tr.lv-detail td{font-size:12.5px}',
    '#bm-meta .mt-lbl{display:flex;align-items:flex-start;gap:6px}',
    '#bm-meta .mt-car{display:inline-block;width:16px;color:var(--accent);font-size:14px;line-height:1;flex:none;padding-top:1px}',
    '#bm-meta .mt-why1{display:block;font-weight:400;color:var(--muted);font-size:11.5px;white-space:normal;max-width:230px;margin-top:2px}',
    '#bm-meta tr.mt-why td{background:var(--card-2);white-space:normal;font-size:12px;color:var(--body);padding:8px 12px 10px}',
    '#bm-meta tr.mt-why ul{margin:4px 0 0 16px;padding:0}',
    '#bm-meta .mt-act{white-space:normal!important;min-width:150px;max-width:200px;font-size:12px;font-weight:400!important;color:var(--body)!important}',
    '#bm-meta .mt-act div{margin-top:4px}',
    '#bm-meta .mt-be{font-size:11px;color:var(--muted)}',
    '#bm-meta .mt-cand{font-size:10px;margin-left:4px}',
    '#bm-meta details summary{cursor:pointer;color:var(--muted);font-size:12px;margin-top:10px}',
    '#bm-meta .mt-notes li{margin:2px 0}',
    '#bm-meta .mt-flag .val{color:var(--warn)}',
    '@media (max-width:600px){#bm-meta .bm-kpis{grid-template-columns:1fr 1fr}#bm-meta .bm-kpi .val{font-size:20px}#bm-meta .mt-why1{max-width:220px}}'
  ].join('');

  function injectCss() {
    if (document.getElementById('bm-meta-css')) return;
    var s = document.createElement('style');
    s.id = 'bm-meta-css'; s.textContent = CSS;
    document.head.appendChild(s);
  }

  function div(a, b) { return b ? a / b : null; }
  function dPct(v) { return v == null ? '–' : (v >= 0 ? '+' : '−') + Math.abs(Math.round(v * 100)) + ' %'; }
  function mk(m) { return m === 'all' ? 'All markets' : '<span style="color:' + BM.color.market(m) + ';font-weight:600">' + esc(m) + '</span>'; }
  function pill(v, conf) {
    var p = V[v] || V.data;
    return '<span class="bm-pill ' + p[0] + '">' + p[1] + '</span>' +
      (conf === 'low' ? ' <span class="muted" style="font-size:11px">low confidence</span>' : '');
  }
  function rid(r) { return [r.level, r.market, r.seg || '', r.fmt || '', r.offer || '', r.line || ''].join('|'); }
  function parentId(r) {
    if (r.level === 'tactic') return ['market', r.market, '', '', '', ''].join('|');
    if (r.level === 'detail') return ['tactic', r.market, r.seg, '', '', ''].join('|');
    return null;
  }
  function freqOf(r, w) {
    var f = (r.freq || {})[w] || {};
    if (r.level === 'market') return f.all;
    return f[r.seg];
  }
  function freqCell(r, w) {
    var v = freqOf(r, w), d = last.d.thresholds;
    if (v == null) return '<span class="muted">–</span>';
    var lim = r.seg === 'existing' ? d.freq_existing : r.seg === 'new' ? d.freq_new : null;
    var h = v.toFixed(1).replace('.', ',');
    var hot = lim != null && w === 'l7' && v > lim;
    var s = '<span class="' + (hot ? 'warn' : '') + '">' + h + '</span>';
    if (r.level === 'market' && w === 'l7') {
      var f = r.freq.l7 || {};
      s += '<div class="mt-be">new ' + (f.new != null ? f.new.toFixed(1).replace('.', ',') : '–') +
        ' · existing <span class="' + (f.existing > d.freq_existing ? 'warn' : '') + '">' +
        (f.existing != null ? f.existing.toFixed(1).replace('.', ',') : '–') + '</span></div>';
    }
    return s;
  }
  function costCell(r, w) {
    var x = r.w[w], be = r.be, out = [];
    function one(cost, br, tag) {
      if (cost == null) return;
      var cls = br == null ? '' : cost <= br ? 'up' : 'down';
      out.push('<span class="' + cls + '">' + F.num(cost) + '</span>' +
        '<span class="mt-be"> / ' + (br == null ? '–' : F.num(br)) + (tag ? ' ' + tag : '') + '</span>');
    }
    // cost per customer for the selected window; break-even from the verdict basis
    var cn = x.n_new ? x.spend_new / x.n_new : null, ce = x.n_ret ? x.spend_existing / x.n_ret : null;
    if (r.seg !== 'existing') one(cn && x.spend_new ? Math.round(cn) : null, be.new, r.level === 'market' ? 'new' : '');
    if (r.seg !== 'new') one(ce && x.spend_existing ? Math.round(ce) : null, be.existing, r.level === 'market' ? 'existing' : '');
    return out.length ? out.join('<br>') : '<span class="muted">–</span>';
  }
  function ordersCell(r, w) {
    var x = r.w[w];
    if (r.seg === 'new') return F.num(x.n_new);
    if (r.seg === 'existing') return F.num(x.n_ret);
    return F.num(x.n_new + x.n_ret) + '<div class="mt-be">' + F.num(x.n_new) + ' new · ' + F.num(x.n_ret) + ' ret.</div>';
  }
  function retCell(r) {
    var m = r.marginal || {};
    if (m.low == null) return '<span class="muted">–</span>';
    var t = last.d.thresholds;
    function c(v) { return '<span class="' + (v >= (t.test || 1) ? 'up' : v < t.cut ? 'down' : '') + '">' + F.roas(v) + '</span>'; }
    return c(m.low) + ' <span class="muted">–</span> ' + c(m.high);
  }

  function rowsFor(d, state) {
    var rows = d.rows.filter(function (r) { return state.market === 'all' ? true : r.market === state.market; });
    var byId = {};
    rows.forEach(function (r) { byId[rid(r)] = r; });
    // default open state: markets open (show new vs existing); the selected market's tactics open too
    rows.forEach(function (r) {
      var id = rid(r);
      if (ui.open[id] === undefined) ui.open[id] = r.level === 'market' ? true : (r.level === 'tactic' && state.market !== 'all');
    });
    var order = [];
    var mkts = state.market === 'all' ? ['all'].concat(MKTS) : [state.market];
    mkts.forEach(function (m) {
      rows.filter(function (r) { return r.level === 'market' && r.market === m; }).forEach(function (mr) {
        order.push(mr);
        if (!ui.open[rid(mr)]) return;
        ['new', 'existing'].forEach(function (s) {
          rows.filter(function (r) { return r.level === 'tactic' && r.market === m && r.seg === s; }).forEach(function (tr) {
            order.push(tr);
            if (!ui.open[rid(tr)]) return;
            rows.filter(function (r) { return r.level === 'detail' && r.market === m && r.seg === s; }).forEach(function (x) { order.push(x); });
          });
        });
      });
    });
    return order;
  }

  function tableHtml(d, state) {
    var w = ui.win, rows = rowsFor(d, state);
    var hasKids = {};
    d.rows.forEach(function (r) { var p = parentId(r); if (p) hasKids[p] = true; });
    var h = ['<div class="bm-table-wrap"><table class="bm-table"><thead><tr>',
      '<th>Segment</th><th>Verdict and action</th>',
      '<th class="num">Spend ' + (w === 'l7' ? '7d' : 'MTD') + '</th>',
      '<th class="num">Norce orders</th>',
      '<th class="num">Cost per customer / break-even</th>',
      '<th class="num">Incremental GP3 per krona</th>',
      '<th class="num">Frequency</th>',
      '</tr></thead><tbody>'];
    rows.forEach(function (r) {
      var id = rid(r), x = r.w[w], p7 = r.w.p7;
      var indent = r.level === 'tactic' ? 16 : r.level === 'detail' ? 34 : 0;
      var car = hasKids[id] ? '<span class="mt-car" data-tog="' + esc(id) + '">' + (ui.open[id] ? '▾' : '▸') + '</span>' : '<span class="mt-car"></span>';
      var lbl = r.level === 'market' ? mk(r.market) : esc(r.label);
      var why1 = r.reasons && r.reasons[0] ? '<span class="mt-why1">' + esc(r.reasons[0]) + '</span>' : '';
      var spendShare = r.level === 'market' && x.spend ? '<div class="mt-be">' + F.pct(div(x.spend_existing, x.spend), 0) + ' to existing</div>' : '';
      h.push('<tr class="mt-r lv-' + r.level + '" data-id="' + esc(id) + '">',
        '<td><div class="mt-lbl" style="padding-left:' + indent + 'px">' + car + '<div>' + lbl + why1 + '</div></div></td>',
        '<td class="mt-act">' + pill(r.verdict, r.confidence) + (r.candidate ? '<span class="bm-pill warn mt-cand">scale candidate</span>' : '') +
          '<div>' + esc(r.action) + '</div></td>',
        '<td class="num">' + F.sek(x.spend) + '<div class="mt-be">' + (w === 'l7' ? dPct(div(x.spend - p7.spend, p7.spend)) + ' vs prior 7d' : '') + '</div>' + spendShare + '</td>',
        '<td class="num">' + ordersCell(r, w) + '<div class="mt-be">Meta ' + F.num(x.meta_purch) +
          (div(x.meta_purch, x.n_new + x.n_ret) ? ' (' + div(x.meta_purch, x.n_new + x.n_ret).toFixed(1).replace('.', ',') + '×)' : '') + '</div></td>',
        '<td class="num">' + costCell(r, w) + '</td>',
        '<td class="num">' + retCell(r) + '</td>',
        '<td class="num">' + freqCell(r, w) + '</td>',
        '</tr>');
      if (ui.why[id]) {
        var lines = (r.reasons || []).map(function (t) { return '<li>' + esc(t) + '</li>'; });
        lines.push('<li>Marginal method: ' + esc(r.method) + (r.basis === 'mtd' ? ' · basis: month to date' : '') + '</li>');
        if (r.headroom_per_day != null) lines.push('<li>' + esc(r.market) + ' GP3 headroom (pacing): ' + F.sek(r.headroom_per_day) + ' SEK/day</li>');
        if (r.adsets && r.adsets.length) lines.push('<li>Ad sets: ' + esc(r.adsets.join(' · ')) + '</li>');
        if (r.campaigns && r.campaigns.length) lines.push('<li>Campaigns: ' + esc(r.campaigns.join(' · ')) + '</li>');
        h.push('<tr class="mt-why"><td colspan="6"><b>Why ' + esc((V[r.verdict] || V.data)[1]) + '</b><ul>' + lines.join('') + '</ul></td></tr>');
      }
    });
    if (!rows.length) h.push('<tr><td colspan="6" class="muted">No Meta spend in this market this month.</td></tr>');
    h.push('</tbody></table></div>');
    return h.join('');
  }

  function kpis(d, state) {
    var r = d.rows.filter(function (x) { return x.level === 'market' && x.market === state.market; })[0] ||
            (state.market === 'all' ? d.rows.filter(function (x) { return x.level === 'market' && x.market === 'all'; })[0] : null);
    if (!r) return '';
    var l7 = r.w.l7, p7 = r.w.p7, f = r.freq.l7 || {}, t = d.thresholds;
    var cn = l7.n_new ? Math.round(l7.spend_new / l7.n_new) : null;
    function k(lbl, val, sub, cls) {
      return '<div class="bm-kpi ' + (cls || '') + '"><div class="lbl">' + esc(lbl) + '</div><div class="val">' + val + '</div><div class="sub">' + sub + '</div></div>';
    }
    return '<div class="bm-kpis">' +
      k('Meta spend 7d', F.sek(l7.spend), F.delta(div(l7.spend - p7.spend, p7.spend), 0) + ' vs prior 7d · MTD ' + F.sek(r.w.mtd.spend)) +
      k('Spend on existing customers', F.pct(div(l7.spend_existing, l7.spend), 0),
        'frequency ' + (f.existing != null ? f.existing.toFixed(1).replace('.', ',') : '–') + ' in 7d (limit ' + t.freq_existing + ')',
        f.existing > t.freq_existing ? 'mt-flag' : '') +
      k('Cost per new customer', cn == null ? '–' : F.num(cn) + ' SEK',
        'break-even ' + (r.be.new == null ? '–' : F.num(r.be.new)) + ' SEK · Norce, 7d') +
      k('New customers from Meta', F.num(l7.n_new), 'Norce, 7d · Meta counts ' + F.num(l7.meta_purch_new) + ' prospecting purchases') +
      k('Meta count ÷ Norce orders', div(l7.meta_purch, l7.n_new + l7.n_ret) ? div(l7.meta_purch, l7.n_new + l7.n_ret).toFixed(1).replace('.', ',') + '×' : '–',
        F.num(l7.meta_purch) + ' Meta purchases vs ' + F.num(l7.n_new + l7.n_ret) + ' Norce orders') +
      '</div>';
  }

  function chartSeries(d, state) {
    var m = state.market === 'all' ? 'all' : state.market;
    return { n: d.series[m + '|new'] || { spend: [], orders: [] }, e: d.series[m + '|existing'] || { spend: [], orders: [] } };
  }

  function methodHtml(d) {
    var t = d.thresholds, rep = d.repeat || {};
    var mk_ = MKTS.concat(['all']).filter(function (m) { return rep[m]; });
    var repRows = mk_.map(function (m) {
      return { market: m, gm2: d.gm2[m], repeat_rev: rep[m].repeat_rev, repeat_gp2: rep[m].repeat_rev * (d.gm2[m] || 0),
               rate: rep[m].repeat_rate, customers: rep[m].customers, demand: d.demand[m] };
    });
    return '<details><summary>How the verdict is set</summary><div class="bm-note" style="color:var(--body)">' +
      '<ol style="margin:6px 0 6px 18px;padding:0">' +
      '<li><b>Worth of an extra krona</b>: incremental GP3 per krona = (new orders × (first-order GP2 + 12-month repeat GP2) × ' + d.incrementality.new +
      ' + returning orders × GP2 × ' + d.incrementality.existing + ') ÷ spend, marginal not average. Shown as a range: Norce click orders (conservative) – Meta\'s own count (optimistic). ' +
      '≥ ' + t.scale + ' Scale · ' + (t.test || 1) + '–' + t.scale + ' Scale · test · ' + t.cut + '–' + (t.test || 1) + ' Hold · < ' + t.cut + ' Cut. ' +
      'Spending more is allowed whenever the extra krona brings back more than it costs (above 1), so anything above break-even leans up. Confident only when both ends agree.</li>' +
      '<li><b>Saturation</b>: new-customer frequency > ' + t.freq_new + ' in 7 days takes one step down (Scale → test → Hold) instead of blocking; existing-customer frequency > ' + t.freq_existing +
      ' forces Cut on that segment unless even Norce clicks pay back (then Hold); on a market row it means: move money from existing to new. CPM and cost per Meta purchase both up > ' + Math.round(t.cost_rise * 100) + ' % while spend rose: one step down.</li>' +
      '<li><b>Headroom</b>: shown for context only. A market over its allowed spend (pacing section) can still add Meta spend that pays for itself, since that keeps GP3 whole.</li>' +
      '<li><b>Data</b>: below ' + F.num(t.min_spend) + ' SEK or ' + t.min_orders + ' Norce orders in 7 days the month to date is used (low confidence); below that too, no verdict.</li>' +
      '</ol>Steps: Scale = up to +' + Math.round(t.step_up * 100) + ' % then wait 3 days; Scale · test = the same step, kept only if the measured slope (shown once spend has moved) stays above 1 (bigger jumps reset Meta\'s learning); Cut = −' + Math.round(t.step_down * 100) + ' %, existing customers via the existing-customer budget cap first.</div>' +
      BM.table(repRows, [
        { key: 'market', label: 'Market', fmt: mk },
        { key: 'gm2', label: 'GP2 margin', num: true, fmt: function (v) { return F.pct(v, 1); } },
        { key: 'repeat_rev', label: 'Repeat revenue 12m', num: true, fmt: F.num },
        { key: 'repeat_gp2', label: 'Repeat GP2 12m', num: true, fmt: F.num },
        { key: 'rate', label: 'Repeat rate', num: true, fmt: function (v) { return F.pct(v, 0); } },
        { key: 'customers', label: 'Cohort size', num: true, fmt: F.num },
        { key: 'demand', label: 'Demand 7d vs prior', num: true, fmt: function (v) { return v == null ? '–' : v.toFixed(2).replace('.', ',') + '×'; } }
      ]) + '</details>';
  }

  function render(el, d, state) {
    injectCss();
    last = { el: el, d: d, state: state };
    var h = [];
    if (state.brand === 'lekmer') {
      el.innerHTML = '<div class="bm-note">Lekmer has no Meta spend this month (its ad account spent ' + F.sek((d.idle_accounts || {}).Lekmer || 0) +
        ' SEK in November). Everything here is Babyshop.</div>';
      return;
    }
    h.push('<div class="mt-sub">Babyshop Meta, by the levels you can move: market → new vs existing customers (Meta\'s own audience split) → catalog/creative × offer/evergreen × line. ' +
      'Verdicts use the last 7 days (to ' + esc(F.date(d.asof)) + '). Click a row for the reasons; ▸ opens the level below.' +
      (state.brand === 'all' ? ' Lekmer has no Meta spend this month.' : '') + '</div>');
    h.push(kpis(d, state));
    h.push('<div class="mt-head"><h3>Scale / hold / cut</h3>' +
      '<div class="mt-tabs" data-tab="win"><button data-v="l7"' + (ui.win === 'l7' ? ' class="on"' : '') + '>Last 7 days</button>' +
      '<button data-v="mtd"' + (ui.win === 'mtd' ? ' class="on"' : '') + '>Month to date</button></div></div>');
    h.push(tableHtml(d, state));
    h.push('<h3>Daily spend and Norce orders · new vs existing customers</h3><div class="bm-chart"><canvas id="mt-chart"></canvas></div>');
    h.push(methodHtml(d));
    h.push('<details><summary>Data notes</summary><ul class="bm-note mt-notes">' + (d.notes || []).map(function (n) { return '<li>' + esc(n) + '</li>'; }).join('') + '</ul></details>');
    if (d.errors && d.errors.length) h.push('<div class="bm-error" style="font-size:12px;margin-top:8px">' + d.errors.map(esc).join('<br>') + '</div>');
    el.innerHTML = h.join('');

    var c = el.querySelector('#mt-chart');
    if (c) {
      var s = chartSeries(d, state), lab = d.days.map(function (x) { return +x.slice(8); });
      var cNew = BM.color.accent, cEx = BM.color.market('NO');
      BM.chart(c, {
        data: { labels: lab, datasets: [
          { type: 'bar', label: 'Spend new', data: s.n.spend, backgroundColor: cNew, stack: 's', yAxisID: 'y' },
          { type: 'bar', label: 'Spend existing', data: s.e.spend, backgroundColor: cEx, stack: 's', yAxisID: 'y' },
          { type: 'line', label: 'Norce new customers', data: s.n.orders, borderColor: cNew, borderWidth: 2, pointRadius: 2, yAxisID: 'y2' },
          { type: 'line', label: 'Norce returning', data: s.e.orders, borderColor: cEx, borderWidth: 2, borderDash: [4, 3], pointRadius: 2, yAxisID: 'y2' }
        ] },
        options: {
          maintainAspectRatio: false, interaction: { mode: 'index', intersect: false },
          plugins: { legend: { labels: { boxWidth: 12 } },
            tooltip: { callbacks: { label: function (x) { return x.dataset.label + ': ' + (x.dataset.yAxisID === 'y' ? F.sek(x.parsed.y) + ' SEK' : F.num(x.parsed.y)); } } } },
          scales: { y: { stacked: true, ticks: { callback: function (v) { return F.sek(v); } } },
            y2: { position: 'right', grid: { display: false }, beginAtZero: true, title: { display: true, text: 'orders' } },
            x: { stacked: true } }
        }
      });
    }

    el.querySelector('.mt-tabs').addEventListener('click', function (e) {
      var b = e.target.closest('button'); if (!b) return;
      ui.win = b.getAttribute('data-v'); render(last.el, last.d, last.state);
    });
    el.querySelectorAll('tr.mt-r').forEach(function (tr) {
      tr.addEventListener('click', function (e) {
        if (e.target.closest('.bm-q')) return;
        var tog = e.target.closest('[data-tog]'), id = tr.getAttribute('data-id');
        if (tog) ui.open[id] = !ui.open[id];
        else ui.why[id] = !ui.why[id];
        render(last.el, last.d, last.state);
      });
    });
  }

  BM.register('meta', { title: 'Meta · scale / hold / cut', render: render });
})();
