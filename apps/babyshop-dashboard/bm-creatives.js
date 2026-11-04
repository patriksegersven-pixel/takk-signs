/* bm-creatives.js — Black Month "Creatives" section.
 *
 * Payload: bm_creatives.py. Meta creative scorecard with Scale / Keep / Watch /
 * Kill verdicts (thresholds in data.rules), new-this-week early reads, results
 * by naming-convention tag, DPA / catalog apart. All filtering by state.market
 * and state.brand (verdicts use each brand's own market averages)
 * happens here over the embedded rows; nothing is fetched.
 */
(function () {
  'use strict';
  var esc = BM.esc, fmt = BM.fmt;
  // Which slice of the scorecard is open. Survives market switches.
  var view = 'actions';
  var VIEWS = [
    ['actions', 'Act today'], ['learning', 'Learning'], ['keep', 'Keep'], ['off', 'Already off'], ['all', 'All']
  ];
  var NEW_CARDS = 8;
  var VERDICT_CLS ={ Scale: 'up', Kill: 'down', Watch: 'warn', Keep: 'muted' };

  var CSS = [
    '#bm-creatives .cr-rules{margin:10px 0 4px;font-size:13px}',
    '#bm-creatives .cr-rules summary{cursor:pointer;color:var(--muted)}',
    '#bm-creatives .cr-rules ul{margin:8px 0 0;padding-left:18px;color:var(--body)}',
    '#bm-creatives .cr-rules li{margin:3px 0}',
    '#bm-creatives .cr-tabs{display:flex;gap:4px;flex-wrap:wrap;margin:14px 0 8px}',
    '#bm-creatives .cr-tabs button{background:var(--card-2);color:var(--body);border:1px solid var(--border);border-radius:999px;padding:4px 12px;font:inherit;font-size:12px;cursor:pointer}',
    '#bm-creatives .cr-tabs button.on{background:var(--accent);color:#111;border-color:var(--accent);font-weight:600}',
    '#bm-creatives .cr-tabs .n{opacity:.7;margin-left:4px}',
    '#bm-creatives .cr-thumb{width:44px;height:44px;border-radius:6px;object-fit:cover;background:var(--card-2);display:block;border:1px solid var(--border-lt)}',
    '#bm-creatives .cr-thumb.ph{display:flex;align-items:center;justify-content:center;color:var(--muted);font-size:10px}',
    '#bm-creatives .cr-name{color:var(--h);max-width:300px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}',
    '#bm-creatives .cr-sub{color:var(--muted);font-size:11px;margin-top:2px;white-space:nowrap}',
    '#bm-creatives .cr-mk{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:4px;vertical-align:0}',
    '#bm-creatives .cr-why{font-size:11px;color:var(--muted);margin-top:3px;max-width:240px;white-space:normal}',
    '#bm-creatives .cr-table td{vertical-align:top}',
    '#bm-creatives .bm-table td.num{white-space:nowrap}',
    '#bm-creatives .cr-table td.c-verdict{min-width:170px}',
    '#bm-creatives .cr-new{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:10px}',
    '#bm-creatives .cr-card{background:var(--card-2);border:1px solid var(--border-lt);border-radius:8px;padding:10px;display:flex;gap:10px}',
    '#bm-creatives .cr-card .cr-thumb{width:64px;height:64px;flex:none}',
    '#bm-creatives .cr-card .cr-name{max-width:none;white-space:normal;font-size:13px;line-height:1.3}',
    '#bm-creatives .cr-card .kv{display:grid;grid-template-columns:auto auto;gap:1px 10px;font-size:12px;margin-top:6px}',
    '#bm-creatives .cr-card .kv span:nth-child(odd){color:var(--muted)}',
    '#bm-creatives .cr-card .kv span:nth-child(even){text-align:right;font-variant-numeric:tabular-nums;color:var(--body)}',
    '#bm-creatives .cr-bar{height:6px;background:var(--grid);border-radius:3px;min-width:60px;overflow:hidden}',
    '#bm-creatives .cr-bar i{display:block;height:100%;background:var(--accent)}',
    '#bm-creatives .cr-conv{background:var(--card-2);border:1px solid var(--border-lt);border-radius:8px;padding:12px 14px;font-size:13px}',
    '#bm-creatives .cr-conv code{display:block;color:var(--accent);font-size:13px;margin:4px 0;white-space:normal;word-break:break-word}',
    '#bm-creatives .cr-conv table{font-size:12px;margin-top:8px;border-collapse:collapse}',
    '#bm-creatives .cr-conv td{padding:2px 10px 2px 0;vertical-align:top;color:var(--body)}',
    '#bm-creatives .cr-conv td:first-child{color:var(--muted);white-space:nowrap}',
    '#bm-creatives .cr-tagtables{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(320px,100%),1fr));gap:12px 18px}',
    '#bm-creatives .cr-tagtables td:first-child{min-width:90px}',
    '#bm-creatives .cr-tagtables h3{margin-top:6px}',
    '#bm-creatives .cr-callout{background:var(--warn-bg);color:var(--warn);border-radius:8px;padding:8px 12px;font-size:13px;margin:10px 0}',
    '#bm-creatives .cr-callout b{color:var(--h)}',
    '#bm-creatives a.cr-link{color:var(--muted);font-size:11px;text-decoration:none}',
    '#bm-creatives a.cr-link:hover{color:var(--accent)}',
    /* phone: scorecard rows become cards */
    '@media (max-width:640px){',
    ' #bm-creatives .cr-table thead{display:none}',
    ' #bm-creatives .cr-table,#bm-creatives .cr-table tbody{display:block}',
    ' #bm-creatives .cr-table tr{display:grid;grid-template-columns:52px 1fr 1fr;gap:2px 10px;padding:10px 0;border-bottom:1px solid var(--border-lt)}',
    ' #bm-creatives .cr-table td{border:0;padding:0;display:block}',
    ' #bm-creatives .cr-table td.c-thumb{grid-row:span 2}',
    ' #bm-creatives .cr-table td.c-name,#bm-creatives .cr-table td.c-verdict{grid-column:2 / span 2}',
    ' #bm-creatives .cr-table td.num{text-align:left;font-size:12px}',
    ' #bm-creatives .cr-table td.num::before{content:attr(data-l);color:var(--muted);margin-right:6px}',
    /* the 3 leading cells (thumb, verdict, name) put the first number at an even index */
    ' #bm-creatives .cr-table td.num:nth-of-type(even){grid-column:2}',
    ' #bm-creatives .cr-table td.num:nth-of-type(odd){grid-column:3}',
    ' #bm-creatives .cr-table td.num .cr-sub{display:inline;margin-left:4px}',    ' #bm-creatives .cr-name{max-width:none;white-space:normal}',
    ' #bm-creatives .cr-why{max-width:none}',
    ' #bm-creatives .cr-bar{min-width:32px}',
    ' #bm-creatives .cr-table tr:hover td{background:none}',
    '}'
  ].join('\n');

  function injectCss() {
    if (document.getElementById('bm-creatives-css')) return;
    var s = document.createElement('style');
    s.id = 'bm-creatives-css';
    s.textContent = CSS;
    document.head.appendChild(s);
  }

  // Ratios in the payload are percentages (1.43 = 1.43 %); BM.fmt.pct wants fractions.
  function pp(v, d) { return v == null ? '–' : fmt.pct(v / 100, d == null ? 1 : d); }
  function kr(v) { return v == null ? '–' : fmt.sek(v); }
  function mkDot(m) { return '<span class="cr-mk" style="background:' + BM.color.market(m) + '"></span>'; }

  function thumb(id) {
    // The Meta tab's mirror; a 404 (never mirrored) falls back to a placeholder.
    return '<img class="cr-thumb" loading="lazy" alt="" src="/api/meta/image/' + esc(id) + '"' +
      ' onerror="this.outerHTML=\'<div class=&quot;cr-thumb ph&quot;>no img</div>\'">';
  }

  function verdictPill(c) {
    return '<span class="bm-pill ' + VERDICT_CLS[c.verdict] + '">' + esc(c.verdict) + '</span>' +
      (c.active ? '' : ' <span class="bm-pill muted">off</span>');
  }

  function fatigueCell(f) {
    if (!f || f.status === 'n/a') return '<span class="muted" title="' + esc(f && f.reason) + '">–</span>';
    var t = 'first 3 days → last 3: CTR ' + pp(f.first.link_ctr, 2) + ' → ' + pp(f.last.link_ctr, 2) +
      ', ROAS ' + fmt.roas(f.first.roas) + ' → ' + fmt.roas(f.last.roas) +
      ', freq ' + fmt.roas(f.first.freq) + ' → ' + fmt.roas(f.last.freq);
    if (f.status === 'fatiguing') return '<span class="bm-pill warn" title="' + esc(t) + '">fatiguing</span>';
    return '<span class="bm-pill up" title="' + esc(t) + '">ok</span>';
  }

  function relRoas(c) {
    if (c.roas == null) return '–';
    var cls = c.rel_roas == null ? '' : c.rel_roas >= 1.3 ? 'up' : c.rel_roas < 0.8 ? 'down' : '';
    return '<span class="' + cls + '">' + fmt.roas(c.roas) + '</span>' +
      (c.rel_roas == null ? '' : '<div class="cr-sub">' + fmt.roas(c.rel_roas) + '× mkt</div>');
  }

  function td(label, html) { return '<td class="num" data-l="' + esc(label) + '">' + html + '</td>'; }

  function scoreRows(rows) {
    var h = ['<div class="bm-table-wrap"><table class="bm-table cr-table"><thead><tr>',
      '<th></th><th>Verdict</th><th>Creative</th>',
      '<th class="num">Spend 7d</th><th class="num">Nov spend</th><th class="num">Days</th>',
      '<th class="num">Impr.</th><th class="num">Hook</th><th class="num">CTR</th><th class="num">CPC</th>',
      '<th class="num">Purch.</th><th class="num">ROAS</th><th class="num">CPA</th><th class="num">Freq</th>',
      '<th class="num">Fatigue</th></tr></thead><tbody>'];
    rows.forEach(function (c) {
      h.push('<tr>',
        '<td class="c-thumb">', thumb(c.ad_id), '</td>',
        '<td class="c-verdict">', verdictPill(c), '<div class="cr-why">', esc(c.reason), '</div></td>',
        '<td class="c-name"><div class="cr-name" title="', esc(c.ad_name), '">', esc(c.name), '</div>',
        '<div class="cr-sub">', mkDot(c.market), c.brand === 'lekmer' ? 'Lekmer ' : '', esc(c.market), ' · ', esc(c.type), ' · ', esc(c.format),
        ' · <a class="cr-link" target="_blank" rel="noopener" href="', esc(c.ads_manager_url), '">Ads Manager ↗</a></div></td>',
        td('Spend 7d', kr(c.spend)), td('Nov', kr(c.nov_spend)), td('Days', fmt.num(c.days_live)),
        td('Impr.', fmt.sek(c.impressions)), td('Hook', pp(c.hook_rate)), td('CTR', pp(c.ctr, 2)),
        td('CPC', c.cpc == null ? '–' : fmt.roas(c.cpc)), td('Purch.', fmt.num(c.purchases)),
        td('ROAS', relRoas(c)), td('CPA', kr(c.cpa)), td('Freq', c.freq == null ? '–' : fmt.roas(c.freq)),
        td('Fatigue', fatigueCell(c.fatigue)),
        '</tr>');
    });
    if (!rows.length) h.push('<tr><td colspan="15" class="muted">Nothing in this slice.</td></tr>');
    h.push('</tbody></table></div>');
    return h.join('');
  }

  function isLearning(c) { return c.verdict === 'Watch' && /^learning/.test(c.reason); }
  function slice(rows, v) {
    return rows.filter(function (c) {
      if (v === 'all') return true;
      if (v === 'off') return !c.active;
      if (!c.active) return false;
      if (v === 'learning') return isLearning(c);
      if (v === 'keep') return c.verdict === 'Keep';
      return c.verdict !== 'Keep' && !isLearning(c);          // actions
    });
  }

  function newCards(rows, refs) {
    if (!rows.length) return '<p class="muted">No creative started spending in the last 7 days.</p>';
    var shown = rows.slice(0, NEW_CARDS), rest = rows.slice(NEW_CARDS);
    var h = ['<div class="cr-new">'];
    shown.forEach(function (c) {
      var ref = (refs[c.brand] || refs.all || {})[c.market] || {};
      h.push('<div class="cr-card">', thumb(c.ad_id), '<div style="min-width:0;flex:1">',
        '<div class="cr-name" title="', esc(c.ad_name), '">', esc(c.name), '</div>',
        '<div class="cr-sub">', mkDot(c.market), esc(c.market), ' · ', esc(c.format), ' · since ', fmt.date(c.first_spend), '</div>',
        '<div style="margin-top:4px">', verdictPill(c), '</div>',
        '<div class="kv">',
        '<span>Spend 7d</span><span>', kr(c.spend), '</span>',
        '<span>CTR</span><span>', pp(c.ctr, 2), ' <span class="muted">/ ', pp(ref.link_ctr, 2), '</span></span>',
        c.hook_rate == null ? '' : '<span>Hook</span><span>' + pp(c.hook_rate) + ' <span class="muted">/ ' + pp(ref.hook_rate) + '</span></span>',
        '<span>Purchases</span><span>', fmt.num(c.purchases), '</span>',
        '<span>ROAS</span><span>', fmt.roas(c.roas), c.rel_roas == null ? '' : ' <span class="muted">(' + fmt.roas(c.rel_roas) + '×)</span>', '</span>',
        '</div><div class="cr-why">', esc(c.reason), '</div></div></div>');
    });
    h.push('</div>');
    if (rest.length) {
      var restSpend = rest.reduce(function (s, c) { return s + (c.spend || 0); }, 0);
      h.push('<p class="bm-note">+ ', rest.length, ' more new creatives with ', kr(restSpend),
        ' between them: too little to read yet. They are under "Learning" in the scorecard.</p>');
    }
    h.push('<p class="bm-note">Largest spend first. CTR and hook after the slash: the market average over the same 7 days.</p>');
    return h.join('');
  }

  function tagTable(list, label) {
    var max = list.reduce(function (m, g) { return Math.max(m, g.share || 0); }, 0) || 1;
    return BM.table(list.slice(0, 10), [
      { key: 'value', label: label },
      { key: 'share', label: 'Share', fmt: function (v) {
        return '<div class="cr-bar" title="' + fmt.pct(v) + '"><i style="width:' + Math.round((v || 0) / max * 100) + '%"></i></div>';
      } },
      { key: 'spend', label: 'Spend', num: true, fmt: kr },
      { key: 'roas', label: 'ROAS', num: true, fmt: function (v) { return fmt.roas(v); } },
      { key: 'roas_idx', label: 'vs avg', num: true, fmt: function (v) { return v == null ? '–' : fmt.delta(v - 1, 0); } },
      { key: 'cpa', label: 'CPA', num: true, fmt: kr },
      { key: 'hook_rate', label: 'Hook', num: true, fmt: function (v) { return pp(v); } }
    ], { empty: 'No spend' });
  }

  function convention(cv) {
    var h = ['<div class="cr-conv"><b style="color:var(--h)">Proposed ad naming convention</b>',
      '<code>', esc(cv.pattern), '</code>',
      '<div class="muted">e.g. <span style="color:var(--body)">', esc(cv.example), '</span></div><table>'];
    cv.fields.forEach(function (f) { h.push('<tr><td>', esc(f.label), '</td><td>', esc(f.values), '</td></tr>'); });
    h.push('</table><p class="bm-note">', esc(cv.notes), '</p></div>');
    return h.join('');
  }

  function render(el, data, state) {
    injectCss();
    var mk = state.market, br = state.brand || 'all';
    var brands = data.brands || {};
    var missing = Object.keys(brands).filter(function (b) { return !brands[b].available; });
    if (br !== 'all' && brands[br] && !brands[br].available) {
      el.innerHTML = '<div class="cr-callout"><b>No ' + esc(brands[br].label) + ' Meta data.</b> ' +
        esc(brands[br].label) + '\'s Meta ad account is not loaded into the warehouse: the connector reads one ' +
        'account (Babyshop), and no ad or campaign name mentions ' + esc(brands[br].label) + '. This section fills ' +
        'in once the account is added to the Meta connector.</div>';
      return;
    }
    var inMk = function (c) {
      return (mk === 'all' || c.market === mk) && (br === 'all' || (c.brand || 'babyshop') === br);
    };
    var rows = data.creatives.filter(inMk);
    var dpa = data.dpa.filter(inMk);
    var ref = (data.market_ref[br] || data.market_ref.all || {})[mk] || {};
    var w = data.windows;

    var live = rows.filter(function (c) { return c.active; });
    var count = function (f) { return live.filter(f).length; };
    var spendOf = function (list) { return list.reduce(function (s, c) { return s + (c.spend || 0); }, 0); };
    var nKill = count(function (c) { return c.verdict === 'Kill'; });
    var nScale = count(function (c) { return c.verdict === 'Scale'; });
    var nFat = count(function (c) { return c.fatigue.status === 'fatiguing'; });
    var learning = live.filter(isLearning);
    var tags = (data.tags[br] || data.tags.all)[mk] || data.tags.all.all;

    var h = [];
    h.push('<div class="bm-kpis">',
      kpi('Live creatives', fmt.num(live.length), (rows.length - live.length) + ' more spent in Nov, now off · ' + dpa.length + ' DPA'),
      kpi('Spend, judging week', kr(spendOf(rows)), fmt.date(w.judge.from) + ' – ' + fmt.date(w.judge.to)),
      kpi('Market ROAS / CPA', fmt.roas(ref.roas) + ' <span style="font-size:15px;color:var(--muted)">/ ' + kr(ref.cpa) + '</span>',
        'non-DPA, same week · floor ' + kr(ref.floor) + ' to judge'),
      kpi('Act today', '<span class="down">' + nKill + '</span> kill · <span class="up">' + nScale + '</span> scale',
        nFat + ' fatiguing'),
      kpi('Untagged spend', fmt.pct(tags.untagged_share, 0), 'last ' + w.tags.days + ' days, outside the BM convention'),
      '</div>');

    if (br === 'all' && missing.length) {
      h.push('<p class="bm-note">', esc(missing.map(function (b) { return brands[b].label; }).join(', ')),
        ' Meta ads are not in the warehouse yet, so everything below is Babyshop only.</p>');
    }

    if (learning.length >= 10) {
      var share = spendOf(learning) / (spendOf(live) || 1);
      h.push('<div class="cr-callout"><b>', learning.length, ' live creatives</b> are still under the spend floor and share ',
        fmt.pct(share, 0), ' of this week\'s spend. Meta can\'t learn on that many: consolidate into fewer ads ',
        'per ad set before Black Week so each one reaches a verdict.</div>');
    }

    h.push('<details class="cr-rules"><summary>How verdicts are decided</summary><ul>');
    data.rules.forEach(function (r) { h.push('<li><b>', esc(r.verdict), ':</b> ', esc(r.rule), '</li>'); });
    h.push('</ul></details>');

    h.push('<div class="cr-tabs" role="group" aria-label="Scorecard slice">');
    VIEWS.forEach(function (v) {
      h.push('<button data-view="', v[0], '" class="', v[0] === view ? 'on' : '', '">', v[1],
        '<span class="n">', slice(rows, v[0]).length, '</span></button>');
    });
    h.push('</div><div class="cr-score">', scoreRows(slice(rows, view)), '</div>');
    h.push('<p class="bm-note">Every creative that spent in November (', fmt.date(w.november.from), ' – ',
      fmt.date(w.november.to), '). Columns are the judging week; Nov spend runs to yesterday. Sorted: kill, scale, ',
      'fatiguing, then by spend. Hover a fatigue pill for first-3 vs last-3 days.</p>');

    var fresh = rows.filter(function (c) { return c.new; })
      .sort(function (a, b) { return (b.spend || 0) - (a.spend || 0); });
    h.push('<h3>New this week <span class="muted" style="font-weight:400">· first spend ',
      fmt.date(w.new.from), ' – ', fmt.date(w.new.to), '</span></h3>', newCards(fresh, data.market_ref));

    h.push('<h3>Results by tag <span class="muted" style="font-weight:400">· ', fmt.date(w.tags.from), ' – ',
      fmt.date(w.tags.to), ', ', kr(tags.total_spend), ' spend, ROAS ', fmt.roas(tags.roas), '</span></h3>');
    if (tags.untagged_share > 0.5) {
      h.push('<p class="bm-note" style="margin-top:0">', fmt.pct(tags.untagged_share, 0), ' of spend is not on the ',
        'BM naming convention yet, so these groups are parsed from today\'s ad names (best effort). ',
        'Name November ads with the convention below and this table becomes exact.</p>');
    }
    h.push('<div class="cr-tagtables">');
    data.tag_dims.forEach(function (d) {
      h.push('<div><h3>', esc(d.label), '</h3>', tagTable(tags.dims[d.key] || [], d.label), '</div>');
    });
    h.push('<div><h3>Naming</h3>', convention(data.convention), '</div></div>');

    h.push('<h3>DPA / catalog</h3>', scoreRows(dpa),
      '<p class="bm-note">Catalog ads show products from the feed, not a creative, so they are kept apart and judged ',
      'against the same market average. Fatigue there means the audience, not the asset.</p>');

    h.push('<details class="cr-rules"><summary>Data notes</summary><ul>');
    data.notes.forEach(function (n) { h.push('<li>', esc(n), '</li>'); });
    h.push('</ul></details>');

    el.innerHTML = h.join('');
    el.querySelectorAll('[data-view]').forEach(function (b) {
      b.addEventListener('click', function () {
        view = b.getAttribute('data-view');
        el.querySelectorAll('[data-view]').forEach(function (x) { x.classList.toggle('on', x === b); });
        el.querySelector('.cr-score').innerHTML = scoreRows(slice(rows, view));
      });
    });
  }

  function kpi(lbl, val, sub) {
    return '<div class="bm-kpi"><div class="lbl">' + esc(lbl) + '</div><div class="val">' + val +
      '</div><div class="sub">' + esc(sub) + '</div></div>';
  }

  BM.register('creatives', { title: 'Creatives', render: render });
})();
