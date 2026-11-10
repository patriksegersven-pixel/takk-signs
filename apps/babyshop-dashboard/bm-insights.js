/* bm-insights.js — "Today's actions" section of the Black Month tab.
 *
 * Payload: bm_insights.build() → {today, cards, stats, alerts, rules, events,
 * seasonality, checklist, snapshots, levers, event_kinds, notes}.
 * Live: GET /api/blackmonth/insights/live overlays fresh cards, manual events
 * and checklist ticks (decisions show without waiting for the refresh job).
 * Writes: POST /api/blackmonth/insights/{cards/<id>/status | events | checklist/<id>}.
 * In ?mock=1 the buttons update the page only and say so.
 */
(function () {
  'use strict';
  var MOCK = new URLSearchParams(location.search).get('mock') === '1';
  var API = '/api/blackmonth/insights';
  var esc = BM.esc, fmt = BM.fmt;
  var ui = { histStatus: 'all', logKind: 'all', showForm: false, toast: '', allAlerts: false, open: {} };
  var TOP_N = 8;

  var LEVER_LABEL = {
    google_troas: 'Google tROAS', google_budget: 'Google budget', meta_allocation: 'Meta allocation',
    creative: 'Creative', offer: 'Offer', stock: 'Stock', site: 'Site', other: 'Other'
  };
  var KIND_LABEL = {
    troas: 'tROAS', google_budget: 'Google budget', meta_budget: 'Meta budget', bidding: 'Bidding',
    status: 'Campaign', meta_ads: 'Meta ads', seasonality: 'Seasonality', offer_launch: 'Offer live', offer_end: 'Offer end',
    site: 'Site', stock: 'Stock', other: 'Other'
  };
  var SECTION_LABEL = { pacing: 'Pacing', paid: 'Paid levers', creatives: 'Creatives', offers: 'Offers & stock' };

  function injectCss() {
    if (document.getElementById('bmi-css')) return;
    var s = document.createElement('style');
    s.id = 'bmi-css';
    s.textContent = [
      '#bm-insights .bmi-bar{display:flex;flex-wrap:wrap;gap:10px;align-items:center;justify-content:space-between;margin-bottom:12px}',
      '#bm-insights .bmi-who{display:flex;gap:6px;align-items:center;font-size:12px;color:var(--muted)}',
      '#bm-insights input,#bm-insights select,#bm-insights textarea{background:var(--card-2);color:var(--h);border:1px solid var(--border);border-radius:6px;padding:5px 8px;font:inherit;font-size:13px;min-width:0}',
      '#bm-insights .bmi-cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:12px}',
      '@media (max-width:640px){#bm-insights .bmi-cards{grid-template-columns:1fr}}',
      '#bm-insights .bmi-card{background:var(--card-2);border:1px solid var(--border-lt);border-radius:10px;padding:14px;display:flex;flex-direction:column;gap:8px}',
      '#bm-insights .bmi-card.st-accepted{border-color:var(--accent)}',
      '#bm-insights .bmi-card.st-rejected,#bm-insights .bmi-card.st-done{opacity:.6}',
      '#bm-insights .bmi-card h4{margin:0;color:var(--h);font-size:15px;font-weight:600;line-height:1.3}',
      '#bm-insights .bmi-tags{display:flex;flex-wrap:wrap;gap:6px;align-items:center;font-size:11px;color:var(--muted)}',
      '#bm-insights .bmi-act{margin:0;color:var(--body);font-size:13px;line-height:1.45}',
      '#bm-insights .bmi-act b{color:var(--h)}',
      '#bm-insights .bmi-impact{font-size:20px;font-weight:600;color:var(--accent)}',
      '#bm-insights .bmi-impact.neg{color:var(--down)}',
      '#bm-insights .bmi-impact small{font-size:12px;font-weight:400;color:var(--muted);margin-left:6px}',
      '#bm-insights details{font-size:12px;color:var(--muted)}',
      '#bm-insights details summary{cursor:pointer}',
      '#bm-insights details ul{margin:6px 0 0;padding-left:16px}',
      '#bm-insights details li{margin:2px 0}',
      '#bm-insights details li b{color:var(--body);font-weight:500}',
      '#bm-insights .bmi-btns{display:flex;gap:6px;flex-wrap:wrap;margin-top:auto}',
      '#bm-insights button.bmi-b{background:var(--card);color:var(--body);border:1px solid var(--border);border-radius:6px;padding:6px 12px;font:inherit;font-size:13px;cursor:pointer}',
      '#bm-insights button.bmi-b:hover{border-color:var(--muted)}',
      '#bm-insights button.bmi-b.pri{background:var(--accent);color:#111;border-color:var(--accent);font-weight:600}',
      '#bm-insights button.bmi-b.on{outline:2px solid var(--accent)}',
      '#bm-insights button.bmi-b:disabled{opacity:.5;cursor:default}',
      '#bm-insights .bmi-dec{font-size:11px;color:var(--muted)}',
      '#bm-insights .bmi-chk{font-size:11px;margin-top:4px;line-height:1.5}',
      '#bm-insights .bmi-alerts{display:flex;flex-direction:column;gap:6px;align-items:stretch}',
      '#bm-insights .bmi-alerts>button{align-self:flex-start}',
      '#bm-insights .bmi-alert{display:grid;grid-template-columns:70px 96px 1fr auto;gap:10px;align-items:baseline;padding:8px 10px;background:var(--card-2);border-radius:8px;font-size:13px}',
      '#bm-insights .bmi-alert .a-h{color:var(--h)}',
      '#bm-insights .bmi-alert .a-a{color:var(--muted);font-size:12px;margin-top:2px}',
      '#bm-insights .bmi-alert a{color:var(--muted);font-size:12px;white-space:nowrap}',
      '@media (max-width:640px){#bm-insights .bmi-alert{grid-template-columns:auto auto 1fr}#bm-insights .bmi-alert a{grid-column:3}}',
      '#bm-insights .bmi-check{display:grid;grid-template-columns:22px 56px 1fr;gap:8px;padding:7px 0;border-bottom:1px solid var(--border-lt);font-size:13px;align-items:start}',
      '#bm-insights .bmi-check .c-t{color:var(--h)}',
      '#bm-insights .bmi-check .c-d{color:var(--muted);font-size:12px;margin-top:2px}',
      '#bm-insights .bmi-check input{margin-top:3px;accent-color:var(--accent)}',
      '#bm-insights .bmi-check.is-done .c-t{text-decoration:line-through;color:var(--muted)}',
      '#bm-insights .bmi-day{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.05em;margin:12px 0 4px}',
      '#bm-insights .bmi-ev{display:grid;grid-template-columns:96px auto 1fr;gap:8px;font-size:13px;padding:4px 0;align-items:baseline}',
      '#bm-insights .bmi-ev .e-t{color:var(--body)}',
      '#bm-insights .bmi-ev .e-by{color:var(--muted);font-size:11px}',
      '#bm-insights .bmi-ev details{grid-column:3}',
      '#bm-insights .bmi-ev table{font-size:12px;border-collapse:collapse;margin-top:4px}',
      '#bm-insights .bmi-ev td{padding:1px 8px 1px 0}',
      '#bm-insights .bmi-log{max-height:520px;overflow-y:auto;padding-right:4px}',
      '#bm-insights .bmi-form{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:6px;margin:8px 0;padding:10px;background:var(--card-2);border-radius:8px}',
      '#bm-insights .bmi-form .wide{grid-column:1/-1}',
      '@media (max-width:640px){#bm-insights .bmi-form{grid-template-columns:1fr 1fr}}',
      '#bm-insights .bmi-chips{display:flex;gap:4px;flex-wrap:wrap;margin-bottom:6px}',
      '#bm-insights .bmi-chips button{background:var(--card-2);color:var(--muted);border:1px solid var(--border-lt);border-radius:999px;padding:2px 10px;font:inherit;font-size:12px;cursor:pointer}',
      '#bm-insights .bmi-chips button.on{color:#111;background:var(--accent);border-color:var(--accent)}',
      '#bm-insights .bmi-toast{font-size:12px;color:var(--warn);min-height:16px}',
      '#bm-insights .bmi-empty{color:var(--muted);font-size:13px;padding:10px;background:var(--card-2);border-radius:8px}',
      '#bm-insights .bmi-top{display:flex;flex-direction:column;gap:6px}',
      '#bm-insights details.bmi-row{background:var(--card-2);border:1px solid var(--border-lt);border-radius:8px;font-size:13px;color:var(--body)}',
      '#bm-insights details.bmi-row[open]{border-color:var(--border)}',
      '#bm-insights details.bmi-row>summary{list-style:none;display:grid;grid-template-columns:22px 64px 120px 1fr auto;gap:10px;align-items:center;padding:9px 12px;cursor:pointer}',
      '#bm-insights details.bmi-row>summary::-webkit-details-marker{display:none}',
      '#bm-insights details.bmi-row>summary .r-n{color:var(--muted);font-variant-numeric:tabular-nums}',
      '#bm-insights details.bmi-row>summary .r-h{color:var(--h);line-height:1.35}',
      '#bm-insights details.bmi-row>summary .r-i{font-weight:600;white-space:nowrap;font-variant-numeric:tabular-nums}',
      '#bm-insights details.bmi-row>summary .r-i.neg{color:var(--down)}#bm-insights details.bmi-row>summary .r-i.pos{color:var(--accent)}',
      '#bm-insights .bmi-more{padding:0 12px 12px 108px;display:flex;flex-direction:column;gap:8px}',
      '#bm-insights .bmi-more ul{margin:0;padding-left:16px;color:var(--muted);font-size:12px}',
      '@media (max-width:640px){#bm-insights details.bmi-row>summary{grid-template-columns:18px auto 1fr;row-gap:4px}#bm-insights details.bmi-row>summary .r-m{grid-column:2/4}#bm-insights details.bmi-row>summary .r-h{grid-column:1/-1}#bm-insights details.bmi-row>summary .r-i{grid-column:1/-1}#bm-insights .bmi-more{padding:0 12px 12px}}',
      '#bm-insights details.bmi-fold{margin-top:14px;font-size:13px;color:var(--body)}',
      '#bm-insights details.bmi-fold>summary{color:var(--h);font-weight:600;font-size:14px;padding:6px 0}',
      '#bm-insights details.bmi-fold>summary .muted{font-weight:400;font-size:12px}'
    ].join('\n');
    document.head.appendChild(s);
  }

  function who() { try { return localStorage.getItem('bm_who') || ''; } catch (e) { return ''; } }
  function setWho(v) { try { localStorage.setItem('bm_who', v); } catch (e) { /* private mode */ } }

  function inMarket(m, state) {
    return state.market === 'all' || !m || m === 'all' || m === state.market;
  }
  /* Rows without a brand (or 'all') concern both shops and stay visible under either filter. */
  function inBrand(b, state) {
    var f = state.brand || 'all';
    return f === 'all' || !b || b === 'all' || b === f;
  }
  function inScope(row, state) { return inMarket(row.market, state) && inBrand(row.brand, state); }
  function brandPill(b) { return b && b !== 'all' ? '<span class="bm-pill muted">' + (b === 'lekmer' ? 'Lekmer' : 'Babyshop') + '</span>' : ''; }
  function sevPill(s) { return '<span class="bm-pill ' + (s === 'high' ? 'down' : s === 'medium' ? 'warn' : 'muted') + '">' + esc(s) + '</span>'; }
  function mkt(m) { return '<span class="bm-pill muted">' + esc(!m || m === 'all' ? 'All' : m) + '</span>'; }
  function statusPill(s) {
    var c = { proposed: 'muted', accepted: 'up', done: 'up', measured: 'up', rejected: 'down' }[s] || 'muted';
    return '<span class="bm-pill ' + c + '">' + esc(s) + '</span>';
  }
  function verdictPill(v) {
    if (!v) return '<span class="muted">–</span>';
    var c = { worked: 'up', no_effect: 'muted', backfired: 'down', inconclusive: 'warn' }[v] || 'muted';
    return '<span class="bm-pill ' + c + '">' + esc(v.replace('_', ' ')) + '</span>';
  }
  /* Verification badge: {result: done|not_done|cannot_verify, evidence, checked_at}. */
  function checkHtml(chk, due, today, ticked) {
    if (!chk) return '';
    var past = due && today && due < today;
    var lab, cls;
    if (chk.result === 'done') { lab = 'verified done ✓'; cls = 'up'; }
    else if (chk.result === 'not_done') { lab = ticked ? 'ticked, data disagrees' : 'not done yet'; cls = past ? 'down' : 'warn'; }
    else { lab = 'can\'t verify — tick manually'; cls = 'muted'; }
    return '<div class="bmi-chk"><span class="bm-pill ' + cls + '">' + lab + '</span> <span class="muted">' + esc(chk.evidence || '') +
      (chk.checked_at ? ' · checked ' + esc(chk.checked_at.slice(5, 16).replace('T', ' ')) : '') + '</span></div>';
  }
  function signedSek(v) { return v == null ? '–' : (v > 0 ? '+' : v < 0 ? '−' : '') + fmt.sek(Math.abs(v)); }
  function evVal(v) {
    if (typeof v === 'number') return Math.abs(v) >= 1000 ? fmt.sek(v) : (Math.round(v * 100) / 100).toString().replace('.', ',');
    return esc(v);
  }
  function localIso() {           // 2026-11-04T09:12:00 in the browser's zone, like the server's
    var d = new Date(), z = d.getTimezoneOffset() * 6e4;
    return new Date(d - z).toISOString().slice(0, 19);
  }
  function stamp(iso) { return iso ? fmt.date(iso.slice(0, 10)) + ' ' + iso.slice(11, 16) : ''; }

  /* ── live overlay ──────────────────────────────────────────────────── */
  function loadLive(data, el) {
    if (MOCK || data._live) return;
    data._live = 'loading';
    fetch(API + '/live', { credentials: 'same-origin' })
      .then(function (r) { return r.json(); })
      .then(function (j) {
        if (!j || !j.ok) { data._live = 'failed'; return; }
        data._live = 'ok';
        var ver = {};
        (data.cards || []).forEach(function (c) { if (c.verification) ver[c.id] = c.verification; });
        data.cards = (j.cards || data.cards).map(function (c) { if (ver[c.id] && !c.verification) c.verification = ver[c.id]; return c; });
        data.stats = j.stats || data.stats;
        var auto = (data.events || []).filter(function (e) { return e.source !== 'manual'; });
        data.events = auto.concat((j.events || []).map(function (e) { e.source = 'manual'; return e; }))
          .sort(function (a, b) { return (b.date + (b.when || b.ts || '')).localeCompare(a.date + (a.when || a.ts || '')); });
        applyTicks(data, j.checklist_ticks || []);
        rerender();
      })
      .catch(function () { data._live = 'failed'; });
  }
  function applyTicks(data, ticks) {
    var last = {};
    ticks.slice().sort(function (a, b) { return (a.ts || '').localeCompare(b.ts || ''); })
      .forEach(function (t) { last[t.ref] = t; });
    (data.checklist || []).forEach(function (c) {
      var t = last[c.id];
      if (!t) return;
      if (t.detail === 'undo') {
        if (c.state === 'done') c.state = c.check && c.check.result === 'done' ? 'verified' : ((c.until || c.due) < data.today ? 'overdue' : 'open');
        c.ticked_by = null;
      }
      else { c.state = 'done'; c.ticked_by = t.by; c.ticked_at = t.ts; }      // a manual tick always wins
    });
  }
  function rerender() {
    var root = document.querySelector('#bm-insights .bm-body');
    var p = BM.payload('insights');
    if (root && p) render(root, p, BM.state);
  }

  function post(path, body) {
    if (MOCK) return Promise.resolve({ ok: true, mock: true });
    return fetch(API + path, {
      method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body)
    }).then(function (r) { return r.json().catch(function () { return { ok: false, error: 'HTTP ' + r.status }; }); });
  }
  function toast(msg) { ui.toast = msg; var t = document.querySelector('#bm-insights .bmi-toast'); if (t) t.textContent = msg; }
  function needWho() {
    var w = who().trim();
    if (!w) { toast('Type your name first (top right), so decisions are attributed.'); var i = document.querySelector('#bm-insights .bmi-who input'); if (i) i.focus(); }
    return w;
  }

  /* ── pieces ────────────────────────────────────────────────────────── */
  function cardHtml(c) {
    var imp = c.impact || {};
    var mid = imp.gp3_sek;
    var range = (imp.low_sek != null && imp.high_sek != null && (imp.low_sek !== mid || imp.high_sek !== mid))
      ? signedSek(imp.low_sek) + ' to ' + signedSek(imp.high_sek) : '';
    var ev = (c.evidence || []).map(function (e) {
      return '<li><b>' + esc(e.metric) + '</b>: ' + evVal(e.value) +
        (e.section ? ' <span class="muted">· ' + esc(SECTION_LABEL[e.section] || e.section) + '</span>' : '') +
        (e.note ? ' — ' + esc(e.note) : '') + '</li>';
    }).join('');
    var st = c.status || 'proposed';
    var dec = '';
    if (c.decided_by || c.decided_at) dec = (st === 'rejected' ? 'Rejected' : 'Accepted') + ' by ' + esc(c.decided_by || '?') + ' ' + stamp(c.decided_at);
    if (c.done_at) dec += (dec ? ' · ' : '') + 'done ' + stamp(c.done_at);
    if (c.outcome) dec += (dec ? ' · ' : '') + 'outcome: ' + esc(c.outcome.verdict) + ' (' + signedSek(c.outcome.measured_gp3_sek) + ')';
    var btn = function (to, label, pri) {
      return '<button class="bmi-b' + (pri ? ' pri' : '') + (st === to ? ' on' : '') + '" data-act="status" data-id="' + esc(c.id) +
        '" data-to="' + to + '"' + (st === to || st === 'measured' ? ' disabled' : '') + '>' + label + '</button>';
    };
    return '<article class="bmi-card st-' + esc(st) + '">' +
      '<div class="bmi-tags"><span class="bm-pill warn">' + esc(LEVER_LABEL[c.lever] || c.lever) + '</span>' + mkt(c.market) + brandPill(c.brand) +
      statusPill(st) + '<span>' + esc(c.confidence || '') + ' confidence</span>' +
      (c.source === 'manual' ? '<span>· manual</span>' : '') + '<span>· ' + esc(fmt.date(c.created)) + '</span></div>' +
      '<h4>' + esc(c.headline) + '</h4>' +
      '<p class="bmi-act"><b>Do:</b> ' + esc(c.action) + '</p>' +
      '<div class="bmi-impact' + (mid < 0 ? ' neg' : '') + '">' + signedSek(mid) + ' GP3<small>' +
      (range ? esc(range) + ' · ' : '') + esc(imp.horizon || '') + '</small></div>' +
      '<details><summary>Evidence (' + (c.evidence || []).length + ')' + (c.rationale ? ' and reasoning' : '') + '</summary><ul>' + ev + '</ul>' +
      (c.rationale ? '<p>' + esc(c.rationale) + '</p>' : '') + (imp.basis ? '<p>Impact basis: ' + esc(imp.basis) + '</p>' : '') + '</details>' +
      '<div class="bmi-btns">' + btn('accepted', 'Accept', st === 'proposed') + btn('rejected', 'Reject') + btn('done', 'Done', st === 'accepted') +
      (st !== 'proposed' && st !== 'measured' ? '<button class="bmi-b" data-act="status" data-id="' + esc(c.id) + '" data-to="proposed">Undo</button>' : '') + '</div>' +
      (dec ? '<div class="bmi-dec">' + dec + (c.owner ? ' · owner ' + esc(c.owner) : '') + '</div>' : '') +
      (c.verification ? checkHtml(c.verification, null, null, st === 'done') : '') +
      '</article>';
  }

  /* One ranked list: the agent's open cards and the rule alerts. High rule alerts
     first, then open cards, then medium alerts, then low; by size within a tier.
     Only the top TOP_N show; each row opens to the action, evidence and buttons. */
  function topItems(open, alerts) {
    var tier = { high: 0, medium: 2, low: 3 };
    var items = open.map(function (c) {
      return { kind: 'card', c: c, tier: c.status === 'proposed' ? 1 : 2, size: Math.abs((c.impact || {}).gp3_sek || 0) };
    }).concat(alerts.map(function (a) {
      return { kind: 'alert', a: a, tier: tier[a.severity] == null ? 3 : tier[a.severity], size: Math.abs(a.impact_sek || 0) };
    }));
    return items.sort(function (x, y) { return x.tier - y.tier || y.size - x.size; });
  }

  function rowHtml(it, n) {
    var key, sev, m, b, head, imp, body;
    if (it.kind === 'card') {
      var c = it.c, gp = (c.impact || {}).gp3_sek;
      key = 'c:' + c.id; sev = '<span class="bm-pill warn">agent</span>'; m = c.market; b = c.brand; head = c.headline;
      imp = gp == null ? '' : '<span class="r-i ' + (gp < 0 ? 'neg' : 'pos') + '">' + signedSek(gp) + ' GP3</span>';
      body = cardHtml(c);
    } else {
      var a = it.a;
      key = 'a:' + a.rule + ':' + a.market + ':' + a.brand + ':' + a.headline.slice(0, 40);
      sev = sevPill(a.severity); m = a.market; b = a.brand; head = a.headline;
      imp = a.impact_sek ? '<span class="r-i neg">' + signedSek(a.impact_sek) + '</span>' : '';
      var ev = (a.evidence || []).map(function (e) { return '<li><b>' + esc(e.metric) + '</b>: ' + evVal(e.value) + '</li>'; }).join('');
      body = '<div><b>Do:</b> ' + esc(a.action) + '</div>' + (ev ? '<ul>' + ev + '</ul>' : '') +
        (a.section && SECTION_LABEL[a.section] ? '<a href="#bm-' + esc(a.section) + '" class="muted">' + esc(SECTION_LABEL[a.section]) + ' →</a>' : '');
    }
    return '<details class="bmi-row" data-key="' + esc(key) + '"' + (ui.open[key] ? ' open' : '') + '><summary>' +
      '<span class="r-n">' + n + '</span>' + sev + '<span class="r-m">' + mkt(m) + brandPill(b) + '</span>' +
      '<span class="r-h">' + esc(head) + '</span>' + (imp || '<span></span>') + '</summary>' +
      '<div class="bmi-more">' + body + '</div></details>';
  }

  function topHtml(items) {
    if (!items.length) return '<div class="bmi-empty">Nothing to act on for this filter.</div>';
    var shown = ui.allAlerts ? items : items.slice(0, TOP_N);
    var more = items.length - TOP_N;
    return '<div class="bmi-top">' + shown.map(function (it, i) { return rowHtml(it, i + 1); }).join('') +
      (more > 0 ? '<button class="bmi-b" data-act="allalerts" style="align-self:flex-start">' +
        (ui.allAlerts ? 'Show top ' + TOP_N : 'Show ' + more + ' more') + '</button>' : '') + '</div>';
  }

  function alertsHtml(alerts, state) {
    var rows = (alerts || []).filter(function (a) { return inScope(a, state); });
    if (!rows.length) return '<div class="bmi-empty">No rule alerts for this market.</div>';
    var more = rows.length - 8;
    if (!ui.allAlerts && more > 0) rows = rows.slice(0, 8);
    return '<div class="bmi-alerts">' + rows.map(function (a) {
      var ev = (a.evidence || []).map(function (e) { return esc(e.metric) + ' ' + evVal(e.value); }).join(' · ');
      return '<div class="bmi-alert">' + sevPill(a.severity) + '<span>' + mkt(a.market) + brandPill(a.brand) + '</span>' +
        '<div><div class="a-h">' + esc(a.headline) + '</div><div class="a-a">' + esc(a.action) +
        (ev ? ' <span title="' + esc(ev) + '">ⓘ</span>' : '') + '</div></div>' +
        (a.section && SECTION_LABEL[a.section] ? '<a href="#bm-' + esc(a.section) + '">' + esc(SECTION_LABEL[a.section]) + ' →</a>' : '<span></span>') +
        '</div>';
    }).join('') + (more > 0 ? '<button class="bmi-b" data-act="allalerts">' + (ui.allAlerts ? 'Show top 8' : 'Show all ' + (more + 8)) + '</button>' : '') + '</div>';
  }

  function checklistHtml(items, today, state) {
    return (items || []).filter(function (c) { return inBrand(c.brand, state); }).map(function (c) {
      var done = c.state === 'done' || c.state === 'verified';
      var info = c.state === 'explained';                 // reference for a review, nothing to tick
      var lockedAuto = c.state === 'verified' && !c.ticked_by;
      var pill = { overdue: 'down', today: 'warn', done: 'up', verified: 'up', explained: 'up', open: 'muted' }[c.state] || 'muted';
      return '<div class="bmi-check' + (done ? ' is-done' : '') + '">' +
        (info ? '<span></span>' : '<input type="checkbox" data-act="tick" data-id="' + esc(c.id) + '"' + (done ? ' checked' : '') + (lockedAuto ? ' disabled title="Checked automatically from the data"' : '') + '>') +
        '<div><div>' + esc(fmt.date(c.due)) + '</div><span class="bm-pill ' + pill + '">' + esc(c.state) + '</span></div>' +
        '<div><div class="c-t">' + (c.lever === 'meta_allocation' || c.lever === 'creative' ? '<span class="bm-pill muted">Meta</span> ' : '') +
        brandPill(c.brand) + (c.brand && c.brand !== 'all' ? ' ' : '') + esc(c.title) + '</div><div class="c-d">' + esc(c.detail || '') +
        (c.owner ? ' <span class="muted">· owner ' + esc(c.owner) + '</span>' : '') +
        (c.auto_note ? ' <span class="' + (c.state === 'verified' ? 'up' : 'warn') + '">' + esc(c.auto_note) + '</span>' : '') +
        (c.ticked_by ? ' · ticked by ' + esc(c.ticked_by) : '') + '</div>' +
        (info ? '' : checkHtml(c.check, c.until || c.due, today, !!c.ticked_by)) + '</div></div>';
    }).join('');
  }

  function eventsHtml(events, state, kinds) {
    var rows = (events || []).filter(function (e) {
      return inScope(e, state) && (ui.logKind === 'all' || e.kind === ui.logKind);
    });
    var chips = ['all'].concat(kinds).map(function (k) {
      return '<button data-act="logkind" data-k="' + k + '" class="' + (ui.logKind === k ? 'on' : '') + '">' + esc(k === 'all' ? 'All' : (KIND_LABEL[k] || k)) + '</button>';
    }).join('');
    var h = ['<div class="bmi-chips">' + chips + '</div><div class="bmi-log">'];
    var day = null;
    rows.slice(0, 250).forEach(function (e) {
      if (e.date !== day) { day = e.date; h.push('<div class="bmi-day">' + esc(fmt.date(day)) + '</div>'); }
      var det = '';
      if (e.items && e.items.length) {
        det = '<details><summary>' + esc(e.tag || 'campaigns') + '</summary><table>' + e.items.map(function (i) {
          var big = i.step != null && Math.abs(i.step) > 0.2;
          return '<tr><td>' + esc(i.campaign) + '</td><td class="num">' + BM.fmt.roas(i.old) + ' → ' + BM.fmt.roas(i.new) + '</td><td class="' + (big ? 'warn' : 'muted') + '">' +
            (i.step == null ? '' : (i.step >= 0 ? '+' : '−') + Math.abs(Math.round(i.step * 100)) + ' %') + '</td></tr>';
        }).join('') + '</table></details>';
      } else if (e.detail) det = '<div class="e-by">' + esc(e.detail) + '</div>';
      h.push('<div class="bmi-ev"><span class="bm-pill ' + (e.source === 'manual' ? 'warn' : 'muted') + '">' + esc(KIND_LABEL[e.kind] || e.kind) + '</span>' +
        '<span>' + mkt(e.market) + brandPill(e.brand) + '</span><div><span class="e-t">' + esc(e.title) + '</span>' +
        (e.by ? ' <span class="e-by">· ' + esc(String(e.by).split('@')[0]) + '</span>' : '') + det + '</div></div>');
    });
    if (!rows.length) h.push('<div class="bmi-empty">No changes logged for this filter.</div>');
    h.push('</div>');
    return h.join('');
  }

  function formHtml(data, state) {
    if (!ui.showForm) return '<button class="bmi-b" data-act="form">+ Log a change</button>';
    var kinds = (data.event_kinds || []).map(function (k) { return '<option value="' + k + '">' + esc(KIND_LABEL[k] || k) + '</option>'; }).join('');
    var ms = ['all', 'SE', 'NO', 'FI', 'DK', 'ROW'].map(function (m) {
      return '<option value="' + m + '"' + (m === state.market ? ' selected' : '') + '>' + (m === 'all' ? 'All markets' : m) + '</option>';
    }).join('');
    return '<form class="bmi-form" data-act="addevent">' +
      '<input type="date" name="date" value="' + esc(data.today) + '" required>' +
      '<select name="kind">' + kinds + '</select><select name="market">' + ms + '</select>' +
      '<select name="brand">' + ['all', 'babyshop', 'lekmer'].map(function (b) {
        return '<option value="' + b + '"' + (b === (state.brand || 'all') ? ' selected' : '') + '>' + (b === 'all' ? 'Both shops' : b === 'lekmer' ? 'Lekmer' : 'Babyshop') + '</option>';
      }).join('') + '</select>' +
      '<button class="bmi-b pri" type="submit">Save</button>' +
      '<input class="wide" name="title" placeholder="What changed, e.g. “Kuling −30 % live in SE”" maxlength="200" required>' +
      '<input class="wide" name="detail" placeholder="Detail (optional): amounts, links" maxlength="1000">' +
      '</form>';
  }

  function historyHtml(cards, state) {
    var rows = (cards || []).filter(function (c) {
      return inScope(c, state) && c.status !== 'proposed' && (ui.histStatus === 'all' || c.status === ui.histStatus);
    });
    var chips = ['all', 'accepted', 'done', 'measured', 'rejected'].map(function (k) {
      return '<button data-act="hist" data-k="' + k + '" class="' + (ui.histStatus === k ? 'on' : '') + '">' + k + '</button>';
    }).join('');
    return '<div class="bmi-chips">' + chips + '</div>' + BM.table(rows, [
      { key: 'created', label: 'Date', fmt: function (v) { return esc(fmt.date(v)); } },
      { key: 'market', label: 'Mkt', fmt: function (v, r) { return esc(v || 'all') + (r.brand && r.brand !== 'all' ? ' <span class="muted">' + esc(r.brand === 'lekmer' ? 'Lekmer' : 'Babyshop') + '</span>' : ''); } },
      { key: 'lever', label: 'Lever', fmt: function (v) { return esc(LEVER_LABEL[v] || v); } },
      { key: 'headline', label: 'Insight' },
      { key: 'status', label: 'Status', fmt: function (v, r) { return statusPill(v) + (r.decided_by ? ' <span class="muted">' + esc(r.decided_by) + '</span>' : ''); } },
      { key: 'impact', label: 'Expected', num: true, fmt: function (v) { return signedSek((v || {}).gp3_sek); } },
      { key: 'verification', label: 'Check', fmt: function (v) {
        if (!v) return '<span class="muted">–</span>';
        var c = { done: 'up', not_done: 'warn', cannot_verify: 'muted' }[v.result] || 'muted';
        return '<span class="bm-pill ' + c + '" title="' + esc(v.evidence || '') + '">' + esc(v.result.replace('_', ' ')) + '</span>';
      } },
      { key: 'outcome', label: 'Outcome', fmt: function (v) { return verdictPill(v && v.verdict) + (v && v.method ? ' <span class="muted" title="' + esc(v.method + (v.detail ? ' — ' + v.detail : '')) + '">ⓘ</span>' : ''); } },
      { key: 'outcome', label: 'Measured', num: true, fmt: function (v) { return v ? signedSek(v.measured_gp3_sek) : '–'; } }
    ], { empty: 'No decided cards yet.' });
  }

  /* ── render ────────────────────────────────────────────────────────── */
  function render(el, data, state) {
    injectCss();
    loadLive(data, el);
    var today = data.today;
    var cards = (data.cards || []).filter(function (c) { return inScope(c, state); });
    var open = cards.filter(function (c) { return c.status === 'proposed' || c.status === 'accepted' || c.created === today; })
      .sort(function (a, b) {
        var ta = a.created === today ? 0 : 1, tb = b.created === today ? 0 : 1;
        if (ta !== tb) return ta - tb;
        var sa = a.status === 'proposed' ? 0 : 1, sb = b.status === 'proposed' ? 0 : 1;
        if (sa !== sb) return sa - sb;
        return ((b.impact || {}).gp3_sek || 0) - ((a.impact || {}).gp3_sek || 0);
      });
    var nToday = open.filter(function (c) { return c.created === today; }).length;
    var proposed = open.filter(function (c) { return c.status === 'proposed'; });
    var accepted = cards.filter(function (c) { return ['accepted', 'done', 'measured'].indexOf(c.status) >= 0; });
    var sum = function (arr) { return arr.reduce(function (s, c) { return s + ((c.impact || {}).gp3_sek || 0); }, 0); };
    var measured = cards.filter(function (c) { return c.outcome && c.outcome.verdict; });
    var scored = measured.filter(function (c) { return c.outcome.verdict !== 'inconclusive'; });
    var worked = measured.filter(function (c) { return c.outcome.verdict === 'worked'; });
    var gotGp3 = measured.reduce(function (s, c) { return s + (c.outcome.measured_gp3_sek || 0); }, 0);
    var nextDue = (data.checklist || []).filter(function (c) { return inBrand(c.brand, state) && ['done', 'verified', 'explained'].indexOf(c.state) < 0; })[0];
    var alerts = (data.alerts || []).filter(function (a) { return inScope(a, state); });
    var highs = alerts.filter(function (a) { return a.severity === 'high'; }).length;

    var h = [];
    h.push('<div class="bmi-bar"><div class="bmi-toast">' + esc(ui.toast || (MOCK ? 'Mock data: buttons update this page only.' : '')) + '</div>' +
      '<label class="bmi-who">You <input data-act="who" value="' + esc(who()) + '" placeholder="your name" size="12"></label></div>');
    h.push('<div class="bm-kpis">' +
      '<div class="bm-kpi"><div class="lbl">Open actions</div><div class="val">' + proposed.length + '</div><div class="sub">' +
      signedSek(sum(proposed)) + ' GP3 if all accepted · ' + nToday + ' new today</div></div>' +
      '<div class="bm-kpi"><div class="lbl">Accepted this month</div><div class="val">' + accepted.length + '</div><div class="sub">' +
      signedSek(sum(accepted)) + ' GP3 expected</div></div>' +
      '<div class="bm-kpi"><div class="lbl">Hit rate</div><div class="val">' + (scored.length ? fmt.pct(worked.length / scored.length, 0) : '–') +
      '</div><div class="sub">' + worked.length + ' of ' + scored.length + ' scored worked · ' + signedSek(gotGp3) + ' measured</div></div>' +
      '<div class="bm-kpi"><div class="lbl">Rule alerts</div><div class="val' + (highs ? ' down' : '') + '">' + alerts.length + '</div><div class="sub">' +
      highs + ' high</div></div>' +
      '<div class="bm-kpi"><div class="lbl">Next on the checklist</div><div class="val" style="font-size:16px">' +
      (nextDue ? esc(fmt.date(nextDue.due)) : 'All done') + '</div><div class="sub">' + (nextDue ? esc(nextDue.title) : '') + '</div></div>' +
      '</div>');

    var items = topItems(open, alerts);
    h.push('<h3>Top actions <span class="muted" style="font-weight:400">· ' + Math.min(TOP_N, items.length) + ' of ' + items.length +
      ', most important first · click a row for the detail' + (nToday ? '' : ' · no new agent cards today (runs weekdays ~07:30)') + '</span></h3>');
    h.push(topHtml(items));

    var openChecks = (data.checklist || []).filter(function (c) { return inBrand(c.brand, state) && ['done', 'verified', 'explained'].indexOf(c.state) < 0; }).length;
    h.push(fold('chk', 'Checklist', openChecks + ' open', checklistHtml(data.checklist, today, state)));
    h.push(fold('log', 'Change log', (data.events || []).filter(function (e) { return inScope(e, state); }).length + ' changes',
      '<div style="margin:6px 0">' + formHtml(data, state) + '</div>' + eventsHtml(data.events, state, kindsPresent(data.events))));
    h.push(fold('hist', 'Decided actions', accepted.length + ' accepted', historyHtml(cards, state)));

    var notes = data.notes || [];
    if (notes.length) h.push('<div class="bm-note">' + notes.map(esc).join('<br>') + '</div>');
    el.innerHTML = h.join('');
    wire(el, data, state);
  }

  function fold(key, title, sub, inner) {
    return '<details class="bmi-fold" data-key="f:' + key + '"' + (ui.open['f:' + key] ? ' open' : '') + '><summary>' + esc(title) +
      ' <span class="muted">· ' + esc(sub) + '</span></summary>' + inner + '</details>';
  }

  function kindsPresent(events) {
    var seen = {};
    (events || []).forEach(function (e) { seen[e.kind] = 1; });
    return Object.keys(seen);
  }

  function wire(el, data, state) {
    el.onclick = function (ev) {
      var b = ev.target.closest('[data-act]');
      if (!b || b.tagName === 'INPUT' && b.type !== 'checkbox' || b.tagName === 'FORM') return;
      var act = b.getAttribute('data-act');
      if (act === 'logkind') { ui.logKind = b.getAttribute('data-k'); render(el, data, state); }
      else if (act === 'hist') { ui.histStatus = b.getAttribute('data-k'); render(el, data, state); }
      else if (act === 'form') { ui.showForm = true; render(el, data, state); }
      else if (act === 'allalerts') { ui.allAlerts = !ui.allAlerts; render(el, data, state); }
      else if (act === 'status') setStatus(el, data, state, b.getAttribute('data-id'), b.getAttribute('data-to'));
    };
    el.querySelectorAll('details[data-key]').forEach(function (d) {
      d.addEventListener('toggle', function () { ui.open[d.getAttribute('data-key')] = d.open; });
    });
    el.onchange = function (ev) {
      var t = ev.target;
      if (t.getAttribute('data-act') === 'who') { setWho(t.value.trim()); toast(''); }
      if (t.getAttribute('data-act') === 'tick') tick(el, data, state, t.getAttribute('data-id'), t.checked, t);
    };
    var f = el.querySelector('form[data-act="addevent"]');
    if (f) f.onsubmit = function (ev) { ev.preventDefault(); addEvent(el, data, state, f); };
  }

  function setStatus(el, data, state, id, to) {
    var by = needWho();
    if (!by) return;
    var c = (data.cards || []).filter(function (x) { return x.id === id; })[0];
    if (!c) return;
    post('/cards/' + encodeURIComponent(id) + '/status', { status: to, by: by }).then(function (j) {
      if (!j.ok) { toast('Not saved: ' + (j.error || 'error')); return; }
      if (j.card) Object.assign(c, j.card);
      else {                                       // mock
        var ts = localIso();
        if (to === 'accepted' || to === 'rejected') { c.decided_by = by; c.decided_at = ts; }
        if (to === 'done') { c.done_at = ts; if (!c.decided_at) { c.decided_by = by; c.decided_at = ts; } }
        if (to === 'proposed') { c.decided_by = null; c.decided_at = null; c.done_at = null; }
        c.status = to;
      }
      ui.toast = j.mock ? 'Mock: not saved.' : 'Saved: ' + to + '.';
      render(el, data, state);
    }).catch(function (e) { toast('Not saved: ' + e); });
  }

  function tick(el, data, state, id, done, input) {
    var by = needWho();
    if (!by) { input.checked = !done; return; }
    post('/checklist/' + encodeURIComponent(id), { done: done, by: by }).then(function (j) {
      if (!j.ok) { toast('Not saved: ' + (j.error || 'error')); input.checked = !done; return; }
      applyTicks(data, [{ ref: id, by: by, ts: localIso(), detail: done ? '' : 'undo' }]);
      ui.toast = j.mock ? 'Mock: not saved.' : 'Checklist saved.';
      render(el, data, state);
    }).catch(function (e) { toast('Not saved: ' + e); input.checked = !done; });
  }

  function addEvent(el, data, state, f) {
    var by = needWho();
    if (!by) return;
    var body = { date: f.date.value, kind: f.kind.value, market: f.market.value, brand: f.brand.value, title: f.title.value.trim(), detail: f.detail.value.trim(), by: by };
    if (!body.title) return;
    post('/events', body).then(function (j) {
      if (!j.ok) { toast('Not saved: ' + (j.error || 'error')); return; }
      var e = j.event || Object.assign({ ts: localIso() }, body);
      e.source = 'manual';
      data.events = [e].concat(data.events || []).sort(function (a, b) {
        return (b.date + (b.when || b.ts || '')).localeCompare(a.date + (a.when || a.ts || ''));
      });
      ui.showForm = false;
      ui.toast = j.mock ? 'Mock: not saved.' : 'Logged.';
      render(el, data, state);
    }).catch(function (e) { toast('Not saved: ' + e); });
  }

  BM.register('insights', { title: "Today's actions", render: render });
})();
