/* ─────────────────────────────────────────────────────────────────────────────
 * nav.js — the single source of truth for the Babyshop dashboard header.
 *
 * ADDING A PAGE
 * -------------
 * One line in PAGES below, and one <script src="/nav.js"></script> in the new
 * page. Nothing else — no other file lists the tabs.
 *
 *     { id:'churn', title:'Churn', href:'/babyshop-churn.html',
 *       group:'customers', desc:'Who stops buying, and when' }
 *
 *   id       stable key; also what a page may pin via <header data-page="…">
 *   title    the label in the menu and in the search
 *   href     absolute path (must match the route added in app.py)
 *   group    one of MENUS[].key → listed under that dropdown, in registry order
 *            'pinned'   → its own always-visible tab (keep to one or two)
 *            'archived' → hidden from the nav and the search; the page still
 *                         works by direct link
 *   desc     one short line shown under the title in menus and search
 *   badge    optional 'test' | 'ad hoc' marker
 *   pinUntil optional YYYY-MM-DD: a 'pinned' page drops back into `fallback`
 *            (a MENUS key) after that date
 *
 * HOW IT RENDERS
 * --------------
 * A page carries `<header id="app-header" data-page="…"></header>` and, if it
 * has its own header controls, a `<template data-header-extra>` holding them.
 * This script (loaded WITHOUT defer, immediately after that markup) renders the
 * header synchronously and moves the template's contents into the header's
 * right-hand side, so the page's own inline JS — which reaches for things like
 * #refreshPill — finds them already in the DOM.
 *
 * Header = logo · pinned tabs · one dropdown per MENUS group · search (⌘K /
 * Ctrl+K). The dropdown holding the current page is underlined and names the
 * page ("Marketing · Meta creatives"). Below 900px the dropdowns fold away and
 * the menu button opens the same search panel, which lists every page grouped.
 * ───────────────────────────────────────────────────────────────────────────── */
(function () {
  'use strict';
  if (window.BabyshopNav) return;

  /* ── Page registry ──────────────────────────────────────────────────────── */
  var PAGES = [
    { id:'blackmonth',title:'Black Month',       href:'/babyshop-blackmonth.html',          group:'pinned',      pinUntil:'2026-12-07', fallback:'performance', desc:'November war room: pacing, levers, actions' },

    { id:'kv',        title:'KV Overview',       href:'/babyshop-dashboard.html',           group:'performance', desc:'Revenue, GP and ad cost vs budget' },
    { id:'execpl',    title:'Executive P&L WIP', href:'/babyshop-exec-pl.html',             group:'archived',    desc:'P&L on Business Central posting dates' },
    { id:'dailyperf', title:'Daily performance', href:'/babyshop-daily-perf.html',          group:'archived',    desc:'Ordered today vs plan, Norce' },

    { id:'meta',      title:'Meta creatives',    href:'/babyshop-meta-dashboard.html',      group:'marketing',   desc:'Which ads to scale, keep or kill' },
    { id:'sims',      title:'ROAS Simulations',  href:'/babyshop-roas-simulations.html',    group:'marketing',   desc:'Google tROAS what-ifs per campaign' },
    { id:'roas',      title:'ROAS Impact',       href:'/babyshop-roas-impact.html',         group:'marketing',   desc:'Effect of past tROAS changes', badge:'ad hoc' },
    { id:'sos',       title:'Share of Search',   href:'/babyshop-sos-dashboard.html',       group:'marketing',   desc:'Brand search share vs competitors' },
    { id:'voyado',    title:'Email (Voyado)',    href:'/babyshop-voyado-dashboard.html',    group:'marketing',   desc:'Email sends, opens and revenue' },

    { id:'ecom',      title:'E-com Funnel',      href:'/babyshop-ecom-dashboard.html',      group:'site',        desc:'Sessions to orders, step by step' },
    { id:'search',    title:'Internal search',   href:'/babyshop-search-dashboard.html',    group:'site',        desc:'What visitors search for and don’t find' },

    { id:'customers', title:'Customer Insights', href:'/babyshop-customer-dashboard.html',  group:'customers',   desc:'New vs returning, cohorts, value' },
    { id:'segments',  title:'Segments',          href:'/babyshop-segments-dashboard.html',  group:'customers',   desc:'Customer segments and their behaviour' },

    { id:'products',  title:'Products',          href:'/babyshop-product-dashboard.html',   group:'assortment',  desc:'Product, brand and category performance' },
    { id:'inventory', title:'Inventory',         href:'/babyshop-inventory-dashboard.html', group:'assortment',  desc:'Stock levels and stockout risk' },
    { id:'bundles',   title:'Bundles',           href:'/babyshop-bundles-dashboard.html',   group:'assortment',  desc:'Products bought together' },
    { id:'shipping',  title:'Shipping cost',     href:'/babyshop-shipping-cost.html',       group:'assortment',  desc:'Carrier cost per order and market' },

    { id:'opps',      title:'Opportunities',     href:'/babyshop-opportunities.html',       group:'growth',      desc:'Ranked initiatives sized in GP2' },
    { id:'stoy',      title:'Stoy Test',         href:'/babyshop-stoy-dashboard.html',      group:'growth',      desc:'Stoy funnel-shift test', badge:'test' },
    { id:'shiptest',  title:'Shipping Threshold Test', href:'/babyshop-shipping-test.html', group:'growth',      desc:'Nordic free-shipping threshold test', badge:'test' }
  ];

  /* Dropdowns, in header order. */
  var MENUS = [
    { key:'performance', label:'Performance' },
    { key:'marketing',   label:'Marketing' },
    { key:'site',        label:'Site & conversion' },
    { key:'customers',   label:'Customers' },
    { key:'assortment',  label:'Assortment & ops' },
    { key:'growth',      label:'Growth' }
  ];

  var LOGO_SRC = '/babyshop-logo.svg';
  var IS_MAC = /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent || '');

  /* ── Helpers ────────────────────────────────────────────────────────────── */
  function esc(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;' }[c];
    });
  }

  /* Resolve date-limited pins once, so everything below sees the final group. */
  (function resolvePins() {
    var today = new Date().toISOString().slice(0, 10);
    for (var i = 0; i < PAGES.length; i++) {
      var p = PAGES[i];
      if (p.group === 'pinned' && p.pinUntil && today > p.pinUntil) p.group = p.fallback || 'performance';
    }
  })();

  function currentPage(host) {
    var pinned = host && host.getAttribute('data-page');
    if (pinned) {
      for (var i = 0; i < PAGES.length; i++) if (PAGES[i].id === pinned) return PAGES[i];
    }
    var path = (location.pathname || '').replace(/\/+$/, '');
    var file = path.slice(path.lastIndexOf('/'));
    for (var j = 0; j < PAGES.length; j++) {
      var h = PAGES[j].href;
      if (file === h.slice(h.lastIndexOf('/'))) return PAGES[j];
    }
    for (var k = 0; k < PAGES.length; k++) if (PAGES[k].id === 'kv') return PAGES[k];   /* "/" → KV */
    return PAGES[0];
  }

  function menuLabel(key) {
    for (var i = 0; i < MENUS.length; i++) if (MENUS[i].key === key) return MENUS[i].label;
    return '';
  }

  function pagesIn(group) {
    return PAGES.filter(function (p) { return p.group === group; });
  }

  function badgeHTML(p) {
    if (!p.badge) return '';
    return '<span class="nav-badge' + (p.badge === 'ad hoc' ? ' is-adhoc' : '') + '">' + esc(p.badge) + '</span>';
  }

  function itemHTML(p, current) {
    var isCur = p.id === current.id;
    return '<a href="' + esc(p.href) + '"' + (isCur ? ' aria-current="page"' : '') + '>' +
             '<span class="nav-item-main"><span class="nav-item-title">' + esc(p.title) + '</span>' + badgeHTML(p) + '</span>' +
             (p.desc ? '<span class="nav-item-desc">' + esc(p.desc) + '</span>' : '') +
           '</a>';
  }

  /* ── Render ─────────────────────────────────────────────────────────────── */
  function render() {
    var host = document.getElementById('app-header');
    if (!host || host.getAttribute('data-nav-rendered')) return;

    var current = currentPage(host);
    var html = '';

    pagesIn('pinned').forEach(function (p) {
      var active = p.id === current.id;
      html += '<a class="nav-tab nav-pin' + (active ? ' active' : '') + '" href="' + esc(p.href) + '"' +
              (active ? ' aria-current="page"' : '') + ' title="' + esc(p.desc || '') + '">' + esc(p.title) + '</a>';
    });

    MENUS.forEach(function (m) {
      var items = pagesIn(m.key);
      if (!items.length) return;
      var holds = current.group === m.key;
      var id = 'nav-menu-' + m.key;
      /* Disclosure, not an ARIA menu widget: the contents are ordinary
       * navigation links, so aria-expanded + aria-controls describes it
       * honestly and middle-click / open-in-new-tab keep working. */
      html += '<div class="nav-more' + (holds ? ' holds-current' : '') + '">' +
                '<button type="button" class="nav-more-btn" aria-expanded="false" aria-controls="' + id + '">' +
                  '<span class="nav-more-label">' + esc(m.label) + '</span>' +
                  (holds ? '<span class="nav-more-cur">· ' + esc(current.title) + '</span>' : '') +
                  '<span class="caret" aria-hidden="true">▼</span>' +
                '</button>' +
                '<div class="nav-menu" id="' + id + '">' +
                  items.map(function (p) { return itemHTML(p, current); }).join('') +
                '</div>' +
              '</div>';
    });

    var key = IS_MAC ? '⌘K' : 'Ctrl K';
    host.innerHTML =
      '<div class="hdr-left">' +
        '<button type="button" class="nav-burger" aria-label="All dashboards" aria-haspopup="dialog">' +
          '<span></span><span></span><span></span>' +
        '</button>' +
        '<a class="brand-logo-link" href="/babyshop-dashboard.html" aria-label="Babyshop">' +
          '<img class="brand-logo" src="' + LOGO_SRC + '" alt="Babyshop">' +
        '</a>' +
        '<span class="nav-cur-mobile">' + esc(current.title) + '</span>' +
      '</div>' +
      '<nav class="hdr-nav" aria-label="Dashboards"><div class="nav-tabs">' + html + '</div></nav>' +
      '<div class="hdr-right">' +
        '<button type="button" class="nav-search-btn" aria-haspopup="dialog">' +
          '<span class="nav-search-icon" aria-hidden="true"></span><span class="nav-search-txt">Search</span>' +
          '<kbd>' + key + '</kbd>' +
        '</button>' +
      '</div>';

    host.setAttribute('data-nav-rendered', '1');

    /* Move the page's own header controls (refresh pill, live dot, toggles)
     * into the right-hand slot. Cloning would break inline JS that keeps a
     * reference, so the template's nodes are adopted as-is. */
    var right = host.querySelector('.hdr-right');
    var tpl = document.querySelector('template[data-header-extra]');
    if (tpl && tpl.content) {
      right.appendChild(tpl.content);
      tpl.parentNode.removeChild(tpl);
    }
    var extra = document.getElementById('header-extra');
    if (extra && extra !== right) right.appendChild(extra);

    [].forEach.call(host.querySelectorAll('.nav-more'), wireDropdown);
    wirePalette(host, current);
    ensureFavicon();
  }

  /* ── Dropdown behaviour: click to open, Escape / outside click to close ──── */
  var openMenus = [];
  function closeAll(except) {
    openMenus.forEach(function (f) { if (f !== except) f(false); });
  }

  function wireDropdown(more) {
    var btn = more.querySelector('.nav-more-btn');
    var items = [].slice.call(more.querySelectorAll('.nav-menu a'));

    function setOpen(open) {
      more.setAttribute('data-open', open ? 'true' : 'false');
      btn.setAttribute('aria-expanded', open ? 'true' : 'false');
    }
    function isOpen() { return more.getAttribute('data-open') === 'true'; }
    setOpen(false);
    openMenus.push(setOpen);

    btn.addEventListener('click', function (e) {
      e.stopPropagation();
      var next = !isOpen();
      closeAll(setOpen);
      setOpen(next);
    });
    /* Once one menu is open, hovering a sibling switches to it (menubar feel). */
    btn.addEventListener('mouseenter', function () {
      var anyOpen = document.querySelector('#app-header .nav-more[data-open="true"]');
      if (anyOpen && anyOpen !== more) { closeAll(setOpen); setOpen(true); }
    });

    btn.addEventListener('keydown', function (e) {
      if (e.key === 'ArrowDown' || e.key === 'Down') {
        e.preventDefault();
        closeAll(setOpen);
        setOpen(true);
        if (items[0]) items[0].focus();
      }
    });

    more.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' || e.key === 'Esc') {
        if (isOpen()) { setOpen(false); btn.focus(); }
        return;
      }
      var idx = items.indexOf(document.activeElement);
      if (idx === -1) return;
      if (e.key === 'ArrowDown' || e.key === 'Down') {
        e.preventDefault();
        (items[idx + 1] || items[0]).focus();
      } else if (e.key === 'ArrowUp' || e.key === 'Up') {
        e.preventDefault();
        (items[idx - 1] || items[items.length - 1]).focus();
      }
    });

    document.addEventListener('click', function (e) {
      if (isOpen() && !more.contains(e.target)) setOpen(false);
    });
    document.addEventListener('keydown', function (e) {
      if ((e.key === 'Escape' || e.key === 'Esc') && isOpen()) { setOpen(false); btn.focus(); }
    });
  }

  /* ── Search palette (⌘K / Ctrl+K, the search button, and the mobile menu) ─ */
  function wirePalette(host, current) {
    var listed = PAGES.filter(function (p) { return p.group !== 'archived'; });
    var order = ['pinned'].concat(MENUS.map(function (m) { return m.key; }));

    var wrap = document.createElement('div');
    wrap.className = 'nav-palette';
    wrap.setAttribute('role', 'dialog');
    wrap.setAttribute('aria-modal', 'true');
    wrap.setAttribute('aria-label', 'Find a dashboard');
    wrap.hidden = true;
    wrap.innerHTML =
      '<div class="nav-palette-box">' +
        '<input type="search" class="nav-palette-input" placeholder="Find a dashboard…" autocomplete="off" spellcheck="false" aria-label="Find a dashboard">' +
        '<div class="nav-palette-list" role="listbox"></div>' +
        '<div class="nav-palette-foot"><span><kbd>↑</kbd><kbd>↓</kbd> move</span><span><kbd>↵</kbd> open</span><span><kbd>esc</kbd> close</span></div>' +
      '</div>';
    document.body.appendChild(wrap);

    var input = wrap.querySelector('.nav-palette-input');
    var list = wrap.querySelector('.nav-palette-list');
    var results = [], sel = 0, lastFocus = null;

    function score(p, q) {
      if (!q) return 1;
      var t = p.title.toLowerCase(), hay = (t + ' ' + (p.desc || '') + ' ' + menuLabel(p.group)).toLowerCase();
      if (t.indexOf(q) === 0) return 3;
      if (t.indexOf(q) > -1) return 2;
      return q.split(/\s+/).every(function (w) { return hay.indexOf(w) > -1; }) ? 1 : 0;
    }

    function draw() {
      var q = input.value.trim().toLowerCase();
      var h = [];
      results = [];
      if (q) {
        results = listed.map(function (p) { return { p: p, s: score(p, q) }; })
          .filter(function (r) { return r.s > 0; })
          .sort(function (a, b) { return b.s - a.s; })
          .map(function (r) { return r.p; });
        results.forEach(function (p, i) { h.push(rowHTML(p, i)); });
        if (!results.length) h.push('<div class="nav-palette-empty">No dashboard matches “' + esc(q) + '”</div>');
      } else {
        order.forEach(function (g) {
          var items = listed.filter(function (p) { return p.group === g; });
          if (!items.length) return;
          h.push('<div class="nav-menu-label">' + esc(g === 'pinned' ? 'Pinned' : menuLabel(g)) + '</div>');
          items.forEach(function (p) { h.push(rowHTML(p, results.length)); results.push(p); });
        });
      }
      list.innerHTML = h.join('');
      sel = Math.min(sel, Math.max(results.length - 1, 0));
      mark();
    }

    function rowHTML(p, i) {
      return '<a role="option" data-i="' + i + '" href="' + esc(p.href) + '"' +
             (p.id === current.id ? ' aria-current="page"' : '') + '>' +
               '<span class="nav-item-main"><span class="nav-item-title">' + esc(p.title) + '</span>' + badgeHTML(p) + '</span>' +
               (p.desc ? '<span class="nav-item-desc">' + esc(p.desc) + '</span>' : '') +
             '</a>';
    }

    function mark() {
      [].forEach.call(list.querySelectorAll('a[data-i]'), function (a) {
        var on = +a.getAttribute('data-i') === sel;
        a.classList.toggle('sel', on);
        a.setAttribute('aria-selected', on ? 'true' : 'false');
        if (on) a.scrollIntoView({ block: 'nearest' });
      });
    }

    function open() {
      closeAll(null);
      lastFocus = document.activeElement;
      wrap.hidden = false;
      input.value = '';
      sel = 0;
      draw();
      input.focus();
    }
    function close() {
      wrap.hidden = true;
      if (lastFocus && lastFocus.focus) lastFocus.focus();
    }

    input.addEventListener('input', function () { sel = 0; draw(); });
    input.addEventListener('keydown', function (e) {
      if (e.key === 'ArrowDown') { e.preventDefault(); sel = Math.min(sel + 1, results.length - 1); mark(); }
      else if (e.key === 'ArrowUp') { e.preventDefault(); sel = Math.max(sel - 1, 0); mark(); }
      else if (e.key === 'Enter') {
        e.preventDefault();
        var p = results[sel];
        if (p) { if (e.metaKey || e.ctrlKey) window.open(p.href, '_blank'); else location.href = p.href; }
      }
    });
    list.addEventListener('mousemove', function (e) {
      var a = e.target.closest && e.target.closest('a[data-i]');
      if (a && +a.getAttribute('data-i') !== sel) { sel = +a.getAttribute('data-i'); mark(); }
    });
    wrap.addEventListener('click', function (e) { if (e.target === wrap) close(); });
    wrap.addEventListener('keydown', function (e) { if (e.key === 'Escape' || e.key === 'Esc') { e.preventDefault(); close(); } });

    host.querySelector('.nav-search-btn').addEventListener('click', open);
    host.querySelector('.nav-burger').addEventListener('click', open);
    document.addEventListener('keydown', function (e) {
      if ((e.metaKey || e.ctrlKey) && !e.altKey && (e.key === 'k' || e.key === 'K')) {
        e.preventDefault();
        if (wrap.hidden) open(); else close();
      }
    });
  }

  function ensureFavicon() {
    if (document.querySelector('link[rel~="icon"]')) return;
    var link = document.createElement('link');
    link.rel = 'icon';
    link.type = 'image/svg+xml';
    link.href = LOGO_SRC;
    (document.head || document.documentElement).appendChild(link);
  }

  window.BabyshopNav = { PAGES: PAGES, MENUS: MENUS, render: render };

  /* Synchronous when the placeholder is already parsed (the documented usage);
   * deferred only as a safety net if someone loads this from <head>. */
  if (document.getElementById('app-header')) render();
  else document.addEventListener('DOMContentLoaded', render);
})();
