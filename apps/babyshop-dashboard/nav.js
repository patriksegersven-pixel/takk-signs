/* ─────────────────────────────────────────────────────────────────────────────
 * nav.js — the single source of truth for the Babyshop dashboard header.
 *
 * ADDING A PAGE
 * -------------
 * One line in PAGES below, and one <script src="/nav.js"></script> in the new
 * page. Nothing else — no other file lists the tabs.
 *
 *     { id:'churn', title:'Churn', href:'/babyshop-churn.html',
 *       group:'core', subtitle:'Churn & Retention' }
 *
 *   id       stable key; also what a page may pin via <header data-page="…">
 *   title    the tab label
 *   href     absolute path (must match the route added in app.py)
 *   group    'core'     → always visible as a tab, in registry order
 *            'channels' → "More reports ▾" under CHANNELS
 *            'tests'    → "More reports ▾" under TESTS, badged
 *            'adhoc'    → "More reports ▾" under AD HOC, badged
 *            'archived' → hidden from the nav entirely; the page still works,
 *                         and still shows itself as the active tab when open
 *   subtitle small uppercase caption beside the logo (optional; falls back to
 *            the title)
 *
 * Keep 'core' to ~5 entries. Everything else — one-off tests, ad-hoc pulls,
 * channel deep-dives — goes in a non-core group so the tab row never wraps.
 * When the current page is non-core it is ALSO rendered as a temporary tab
 * next to the core ones, so you can always see where you are.
 *
 * HOW IT RENDERS
 * --------------
 * A page carries `<header id="app-header" data-page="…"></header>` and, if it
 * has its own header controls, a `<template data-header-extra>` holding them.
 * This script (loaded WITHOUT defer, immediately after that markup) renders the
 * header synchronously and moves the template's contents into the header's
 * right-hand side, so the page's own inline JS — which reaches for things like
 * #refreshPill — finds them already in the DOM.
 * ───────────────────────────────────────────────────────────────────────────── */
(function () {
  'use strict';
  if (window.BabyshopNav) return;

  /* ── Page registry ──────────────────────────────────────────────────────── */
  var PAGES = [
    { id:'kv',        title:'KV Overview',       href:'/babyshop-dashboard.html',           group:'core',     subtitle:'KV Performance Dashboard' },
    { id:'products',  title:'Products',          href:'/babyshop-product-dashboard.html',   group:'core',     subtitle:'Product Performance Dashboard' },
    { id:'customers', title:'Customer Insights', href:'/babyshop-customer-dashboard.html',  group:'core',     subtitle:'Customer Insights' },
    { id:'segments',  title:'Segments',          href:'/babyshop-segments-dashboard.html',  group:'core',     subtitle:'Customer Segments' },
    { id:'sos',       title:'Share of Search',   href:'/babyshop-sos-dashboard.html',       group:'core',     subtitle:'Share of Search' },
    { id:'inventory', title:'Inventory',         href:'/babyshop-inventory-dashboard.html', group:'core',     subtitle:'Inventory & Stockout Insights' },
    { id:'bundles',   title:'Bundles',           href:'/babyshop-bundles-dashboard.html',   group:'core',     subtitle:'Product Bundles' },
    { id:'voyado',    title:'Email (Voyado)',    href:'/babyshop-voyado-dashboard.html',    group:'channels', subtitle:'Email · Voyado Engage' },
    { id:'meta',      title:'Meta creatives',    href:'/babyshop-meta-dashboard.html',      group:'channels', subtitle:'Meta · Creative Performance' },
    { id:'stoy',      title:'Stoy Test',         href:'/babyshop-stoy-dashboard.html',      group:'tests',    subtitle:'Stoy Funnel-Shift Test' },
    { id:'roas',      title:'ROAS Impact',       href:'/babyshop-roas-impact.html',         group:'adhoc',    subtitle:'ROAS Impact Monitor' },
    { id:'sims',      title:'ROAS Simulations',  href:'/babyshop-roas-simulations.html',    group:'adhoc',    subtitle:'ROAS Simulations' }
  ];

  /* Section headings + badge styling for the non-core groups, in menu order. */
  var GROUPS = [
    { key:'channels', label:'Channels', badge:'',       badgeClass:'is-channel' },
    { key:'tests',    label:'Tests',    badge:'test',   badgeClass:'' },
    { key:'adhoc',    label:'Ad hoc',   badge:'ad hoc', badgeClass:'is-adhoc' }
  ];

  var LOGO_SRC = '/babyshop-logo.svg';

  /* ── Helpers ────────────────────────────────────────────────────────────── */
  function esc(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;' }[c];
    });
  }

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
    return PAGES[0];   /* "/" redirects to KV Overview */
  }

  function badgeFor(group) {
    for (var i = 0; i < GROUPS.length; i++) {
      if (GROUPS[i].key === group && GROUPS[i].badge) {
        return '<span class="nav-badge ' + GROUPS[i].badgeClass + '">' + esc(GROUPS[i].badge) + '</span>';
      }
    }
    return '';
  }

  function tabHTML(page, active) {
    return '<a class="nav-tab' + (active ? ' active' : '') + '" href="' + esc(page.href) + '"' +
           (active ? ' aria-current="page"' : '') + '>' + esc(page.title) +
           (page.group === 'core' ? '' : badgeFor(page.group)) + '</a>';
  }

  function menuHTML(current) {
    var out = '', any = false;
    for (var g = 0; g < GROUPS.length; g++) {
      var grp = GROUPS[g], items = [];
      for (var i = 0; i < PAGES.length; i++) {
        if (PAGES[i].group === grp.key) items.push(PAGES[i]);
      }
      if (!items.length) continue;
      if (any) out += '<hr>';
      any = true;
      out += '<div class="nav-menu-label">' + esc(grp.label) + '</div>';
      for (var k = 0; k < items.length; k++) {
        var p = items[k], isCur = p.id === current.id;
        out += '<a href="' + esc(p.href) + '"' + (isCur ? ' aria-current="page"' : '') + '>' +
               '<span>' + esc(p.title) + '</span>' +
               (grp.badge ? '<span class="nav-badge ' + grp.badgeClass + '">' + esc(grp.badge) + '</span>' : '') +
               '</a>';
      }
    }
    return out;
  }

  /* ── Render ─────────────────────────────────────────────────────────────── */
  function render() {
    var host = document.getElementById('app-header');
    if (!host || host.getAttribute('data-nav-rendered')) return;

    var current = currentPage(host);
    var tabs = '';
    for (var i = 0; i < PAGES.length; i++) {
      if (PAGES[i].group === 'core') tabs += tabHTML(PAGES[i], PAGES[i].id === current.id);
    }
    /* Non-core current page gets a temporary tab so "where am I" stays answerable. */
    if (current.group !== 'core') tabs += tabHTML(current, true);

    var menu = menuHTML(current);

    host.innerHTML =
      '<div class="hdr-left">' +
        '<a class="brand-logo-link" href="/babyshop-dashboard.html" aria-label="Babyshop">' +
          '<img class="brand-logo" src="' + LOGO_SRC + '" alt="Babyshop">' +
        '</a>' +
        '<span class="brand-rule" aria-hidden="true"></span>' +
        '<span class="brand-sub">' + esc(current.subtitle || current.title) + '</span>' +
      '</div>' +
      '<nav class="hdr-nav" aria-label="Dashboards">' +
        '<div class="nav-tabs">' + tabs +
          /* Disclosure, not an ARIA menu widget: the contents are ordinary
           * navigation links, so aria-expanded + aria-controls describes it
           * honestly and middle-click / open-in-new-tab keep working. */
          (menu
            ? '<div class="nav-more">' +
                '<button type="button" class="nav-more-btn" aria-expanded="false" aria-controls="nav-more-menu">' +
                  'More reports<span class="caret" aria-hidden="true">▼</span>' +
                '</button>' +
                '<div class="nav-menu" id="nav-more-menu">' + menu + '</div>' +
              '</div>'
            : '') +
        '</div>' +
      '</nav>' +
      '<div class="hdr-right"></div>';

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

    wireDropdown(host);
    ensureFavicon();
  }

  /* ── Dropdown behaviour: click to open, Escape / outside click to close ──── */
  function wireDropdown(host) {
    var more = host.querySelector('.nav-more');
    if (!more) return;
    var btn = more.querySelector('.nav-more-btn');
    var items = [].slice.call(more.querySelectorAll('.nav-menu a'));

    function setOpen(open) {
      more.setAttribute('data-open', open ? 'true' : 'false');
      btn.setAttribute('aria-expanded', open ? 'true' : 'false');
    }
    function isOpen() { return more.getAttribute('data-open') === 'true'; }
    setOpen(false);

    btn.addEventListener('click', function (e) {
      e.stopPropagation();
      setOpen(!isOpen());
    });

    btn.addEventListener('keydown', function (e) {
      if (e.key === 'ArrowDown' || e.key === 'Down') {
        e.preventDefault();
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

  function ensureFavicon() {
    if (document.querySelector('link[rel~="icon"]')) return;
    var link = document.createElement('link');
    link.rel = 'icon';
    link.type = 'image/svg+xml';
    link.href = LOGO_SRC;
    (document.head || document.documentElement).appendChild(link);
  }

  window.BabyshopNav = { PAGES: PAGES, render: render };

  /* Synchronous when the placeholder is already parsed (the documented usage);
   * deferred only as a safety net if someone loads this from <head>. */
  if (document.getElementById('app-header')) render();
  else document.addEventListener('DOMContentLoaded', render);
})();
