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
 * BM.el(html), BM.table(rows, columns). Build HTML strings in one pass — never
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
    setMarket: setMarket, setBrand: setBrand, reload: load,
    start: function () {
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
