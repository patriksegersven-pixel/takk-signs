/* ─────────────────────────────────────────────────────────────────────────────
 * bm-core.js — runtime for the Black Month tab (babyshop-blackmonth.html).
 *
 * The page is a stack of independent SECTIONS. Each section ships one file,
 * /bm-<key>.js, which calls
 *
 *     BM.register('<key>', {
 *       title: 'Pacing vs forecast',
 *       render: function (el, data, state) { ... }   // el = the section body
 *     });
 *
 * bm-core fetches /api/blackmonth/<key> (or /bm-mock/<key>.json with ?mock=1),
 * then calls render(el, data, state). It calls render again whenever the
 * global market filter changes, so render must rebuild el from scratch.
 *
 *   state.market   'all' | 'SE' | 'NO' | 'FI' | 'DK'
 *   state.brand    'all' | 'babyshop' | 'lekmer'
 *
 * Helpers for sections: BM.fmt.*, BM.color.*, BM.chart(canvas, config),
 * BM.el(html), BM.table(rows, columns). Metric explanations ("?" tips) live in
 * bm-glossary.js, keyed by the label text the section renders. Build HTML strings in one pass — never
 * `innerHTML +=` in a loop (that froze the Inventory tab for 60 s).
 * ───────────────────────────────────────────────────────────────────────────── */
(function () {
  'use strict';
  var params = new URLSearchParams(location.search);
  var MOCK = params.get('mock') === '1';
  var registry = {};
  var payloads = {};
  var state = { market: params.get('market') || 'all', brand: params.get('brand') || 'all' };

  function css(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  var color = {
    get ink()   { return css('--h'); },
    get muted() { return css('--muted'); },
    get grid()  { return css('--grid'); },
    get up()    { return css('--up'); },
    get down()  { return css('--down'); },
    get warn()  { return css('--warn'); },
    get accent(){ return css('--accent'); },
    // Series: actual, plan/target, last year, forecast
    get actual(){ return css('--s-actual'); },
    get plan()  { return css('--s-plan'); },
    get ly()    { return css('--s-ly'); },
    get fc()    { return css('--s-fc'); },
    market: function (m) { return css('--m-' + String(m).toLowerCase()) || css('--muted'); }
  };

  var nf0 = new Intl.NumberFormat('sv-SE', { maximumFractionDigits: 0 });
  var nf1 = new Intl.NumberFormat('sv-SE', { maximumFractionDigits: 1, minimumFractionDigits: 1 });
  var fmt = {
    num: function (v) { return v == null || isNaN(v) ? '–' : nf0.format(v); },
    sek: function (v) {           // compact: 1,2 M / 845 k / 950
      if (v == null || isNaN(v)) return '–';
      var a = Math.abs(v);
      if (a >= 1e6) return nf1.format(v / 1e6) + ' M';
      if (a >= 1e4) return nf0.format(v / 1e3) + ' k';
      return nf0.format(v);
    },
    pct: function (v, d) {        // 0.123 → 12,3 %
      if (v == null || isNaN(v)) return '–';
      return (v * 100).toFixed(d == null ? 1 : d).replace('.', ',') + ' %';
    },
    roas: function (v) { return v == null || isNaN(v) ? '–' : v.toFixed(2).replace('.', ','); },
    delta: function (v, d) {      // signed % with colour class: <span class="up">+4,2 %</span>
      if (v == null || isNaN(v)) return '<span class="muted">–</span>';
      var s = (v >= 0 ? '+' : '−') + Math.abs(v * 100).toFixed(d == null ? 1 : d).replace('.', ',') + ' %';
      return '<span class="' + (v >= 0 ? 'up' : 'down') + '">' + s + '</span>';
    },
    date: function (iso) {        // 2026-11-27 → fre 27/11
      if (!iso) return '–';
      var d = new Date(iso + 'T12:00:00');
      return d.toLocaleDateString('sv-SE', { weekday: 'short' }) + ' ' + d.getDate() + '/' + (d.getMonth() + 1);
    }
  };

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  /* table(rows, cols): cols = [{key, label, fmt?: fn(value,row) → html, num?: bool}] */
  function table(rows, cols, opts) {
    opts = opts || {};
    var h = ['<div class="bm-table-wrap"><table class="bm-table"><thead><tr>'];
    cols.forEach(function (c) { h.push('<th' + (c.num ? ' class="num"' : '') + '>' + esc(c.label) + '</th>'); });
    h.push('</tr></thead><tbody>');
    rows.forEach(function (r) {
      h.push('<tr>');
      cols.forEach(function (c) {
        var v = r[c.key];
        h.push('<td' + (c.num ? ' class="num"' : '') + '>' + (c.fmt ? c.fmt(v, r) : esc(v)) + '</td>');
      });
      h.push('</tr>');
    });
    if (!rows.length) h.push('<tr><td colspan="' + cols.length + '" class="muted">' + esc(opts.empty || 'No rows') + '</td></tr>');
    h.push('</tbody></table></div>');
    return h.join('');
  }

  var charts = new WeakMap();
  function chart(canvas, config) {
    if (!window.Chart) return null;
    var old = charts.get(canvas);
    if (old) old.destroy();
    Chart.defaults.color = color.muted;
    Chart.defaults.borderColor = color.grid;
    Chart.defaults.font.family = css('--font');
    var c = new Chart(canvas, config);
    charts.set(canvas, c);
    return c;
  }

  /* ── "?" explanations ──────────────────────────────────────────────────
     bm-glossary.js registers label → text. A "?" goes after every KPI label,
     table header and h3 in a section whose own text has an entry; a single
     floating tip shows it on hover, keyboard focus or tap. A MutationObserver
     re-decorates after any re-render, including a section's own re-renders. */
  var gloss = {}, prefixes = [];
  function glossary(map) {
    Object.keys(map).forEach(function (k) {
      if (k.slice(-1) === '*') prefixes.push([k.slice(0, -1), map[k]]); else gloss[k] = map[k];
    });
  }
  function ownText(e) {
    var t = '';
    for (var n = e.firstChild; n; n = n.nextSibling) {
      if (n.nodeType === 3) t += n.textContent;
      else if (n.nodeType === 1 && !n.classList.contains('bm-q') && /^(B|I|EM|STRONG|SMALL)$/.test(n.tagName)) t += n.textContent;
      else if (n.nodeType === 1) break;
    }
    return t.replace(/\s+/g, ' ').trim();
  }
  function lookup(sec, t) {
    if (!t) return null;
    var hit = gloss[sec + '|' + t] || gloss[t];
    if (hit) return hit;
    for (var i = 0; i < prefixes.length; i++) {
      var k = prefixes[i][0], s = k.indexOf('|') > 0 ? k.split('|') : [null, k];
      if ((s[0] === null || s[0] === sec) && t.indexOf(s[1]) === 0) return prefixes[i][1];
    }
    return null;
  }
  function decorate(root) {
    var sec = root.id.replace(/^bm-/, '');
    root.querySelectorAll('.bm-kpi .lbl, th, h3').forEach(function (e) {
      if (e.querySelector(':scope > .bm-q')) return;
      var tip = lookup(sec, ownText(e));
      if (!tip) return;
      var q = document.createElement('span');
      q.className = 'bm-q'; q.tabIndex = 0; q.textContent = '?';
      q.setAttribute('role', 'button'); q.setAttribute('aria-label', tip);
      q.setAttribute('data-tip', tip);
      e.appendChild(q);
    });
  }
  var tipEl = null;
  function showTip(q) {
    if (!tipEl) { tipEl = document.createElement('div'); tipEl.className = 'bm-tip'; tipEl.setAttribute('role', 'tooltip'); document.body.appendChild(tipEl); }
    tipEl.textContent = q.getAttribute('data-tip');
    tipEl.style.left = '0px'; tipEl.style.top = '0px';      // measure at full width, not squeezed at the old spot
    tipEl.style.display = 'block';
    var r = q.getBoundingClientRect(), w = tipEl.offsetWidth, h = tipEl.offsetHeight;
    var left = Math.max(8, Math.min(r.left + r.width / 2 - w / 2, innerWidth - w - 8));
    var top = r.bottom + 8 + h > innerHeight ? r.top - h - 8 : r.bottom + 8;
    tipEl.style.left = left + 'px'; tipEl.style.top = Math.max(8, top) + 'px';
  }
  function hideTip() { if (tipEl) tipEl.style.display = 'none'; }
  function wireTips() {
    var over = function (ev) { var q = ev.target.closest && ev.target.closest('.bm-q'); if (q) showTip(q); };
    var out = function (ev) { if (ev.target.closest && ev.target.closest('.bm-q')) hideTip(); };
    document.addEventListener('mouseover', over);
    document.addEventListener('mouseout', out);
    document.addEventListener('focusin', over);
    document.addEventListener('focusout', out);
    document.addEventListener('click', function (ev) {          // tap: inside a <summary> or th, don't toggle/sort
      var q = ev.target.closest && ev.target.closest('.bm-q');
      if (q) { ev.preventDefault(); ev.stopPropagation(); q.focus(); showTip(q); }
    }, true);
    addEventListener('scroll', hideTip, { passive: true });
    var pending = false;
    new MutationObserver(function () {
      if (pending) return;
      pending = true;
      setTimeout(function () {            // not rAF: it never fires in a background tab
        pending = false;
        document.querySelectorAll('section.bm-section').forEach(decorate);
      }, 30);
    }).observe(document.querySelector('main') || document.body, { childList: true, subtree: true });
  }

  function sectionEl(key) { return document.getElementById('bm-' + key); }

  function renderOne(key) {
    var root = sectionEl(key), mod = registry[key];
    if (!root) return;
    var body = root.querySelector('.bm-body');
    var meta = root.querySelector('.bm-meta');
    var p = payloads[key];
    if (!mod) { body.innerHTML = '<p class="muted">Not built yet.</p>'; return; }
    if (mod.title) root.querySelector('h2').textContent = mod.title;
    if (!p) { body.innerHTML = '<p class="muted">Loading…</p>'; return; }
    if (p.error) { body.innerHTML = '<p class="bm-error">' + esc(p.error) + '</p>'; return; }
    meta.textContent = p.generated_at ? 'updated ' + p.generated_at.replace('T', ' ').slice(0, 16) : '';
    try { mod.render(body, p, state); }
    catch (e) {
      console.error(key, e);
      body.innerHTML = '<p class="bm-error">This section failed to render: ' + esc(e.message) + '</p>';
    }
  }

  function load(key) {
    var url = MOCK ? '/bm-mock/' + key + '.json' : '/api/blackmonth/' + key;
    return fetch(url, { credentials: 'same-origin' })
      .then(function (r) { return r.json().catch(function () { return { error: 'HTTP ' + r.status }; }); })
      .then(function (j) { payloads[key] = j; renderOne(key); })
      .catch(function (e) { payloads[key] = { error: String(e) }; renderOne(key); });
  }

  function setFilter(key, v) {
    state[key] = v;
    document.querySelectorAll('[data-' + key + ']').forEach(function (b) {
      b.classList.toggle('on', b.getAttribute('data-' + key) === v);
    });
    var u = new URL(location.href);
    if (v === 'all') u.searchParams.delete(key); else u.searchParams.set(key, v);
    history.replaceState(null, '', u);
    Object.keys(registry).forEach(renderOne);
  }
  function setMarket(m) { setFilter('market', m); }
  function setBrand(b) { setFilter('brand', b); }

  window.BM = {
    register: function (key, mod) { registry[key] = mod; if (payloads[key]) renderOne(key); },
    payload: function (key) { return payloads[key]; },
    state: state, fmt: fmt, color: color, esc: esc, table: table, chart: chart,
    setMarket: setMarket, setBrand: setBrand, reload: load, glossary: glossary,
    start: function () {
      wireTips();
      document.querySelectorAll('[data-market]').forEach(function (b) {
        b.addEventListener('click', function () { setMarket(b.getAttribute('data-market')); });
      });
      document.querySelectorAll('[data-brand]').forEach(function (b) {
        b.addEventListener('click', function () { setBrand(b.getAttribute('data-brand')); });
      });
      state.brand = state.brand || 'all';
      document.querySelectorAll('[data-brand]').forEach(function (b) {
        b.classList.toggle('on', b.getAttribute('data-brand') === state.brand);
      });
      setMarket(state.market);
      document.querySelectorAll('section.bm-section').forEach(function (s) {
        load(s.id.replace(/^bm-/, ''));
      });
    }
  };
})();
