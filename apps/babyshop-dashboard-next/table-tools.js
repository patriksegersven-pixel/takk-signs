/* ─────────────────────────────────────────────────────────────────────────────
 * table-tools.js — shared spreadsheet-style column filters + CSV/Sheets export
 * for the Babyshop dashboard pages.
 *
 * Usage (either one):
 *   <table id="market-table" data-tools data-export-name="kv-market">…</table>
 *   TableTools.enhance(document.getElementById('market-table'), { … });
 *
 * Design constraints this file is built around — read before changing it:
 *   • The dashboard pages rebuild <tbody> (and sometimes <thead>) with
 *     innerHTML on every data refresh / page-level filter change. Everything
 *     here therefore has to be idempotent and re-derivable: header buttons are
 *     re-attached when missing, distinct-value lists are computed on open, and
 *     filter state lives on the instance (keyed by column index), never in the
 *     DOM that gets thrown away.
 *   • Tables sit inside overflow containers (.pl-wrap scrolls horizontally and
 *     has a sticky thead), so the dropdown is a single position:fixed element
 *     parented to <body> — inside the <th> it would be clipped.
 *   • Pages are self-contained: no build step, no dependencies. SheetJS is
 *     fetched from the CDN on the first .xlsx export only.
 * ───────────────────────────────────────────────────────────────────────────── */
(function () {
  'use strict';
  if (window.TableTools) return;

  var SHEETJS_URL = 'https://cdn.jsdelivr.net/npm/xlsx@0.18.5/dist/xlsx.full.min.js';

  /* ── Styles ───────────────────────────────────────────────────────────────
   * Every colour goes through var(--token, fallback) so the module inherits a
   * page's palette when it has one and still looks deliberate when it does not. */
  var CSS = [
    '.tt-bar{display:inline-flex;align-items:center;gap:6px;font-size:11px;',
    '  font-family:inherit;vertical-align:middle}',
    '.tt-bar button{font-family:inherit;font-size:11px;font-weight:600;cursor:pointer;',
    '  border:1px solid var(--border,#e5e7eb);background:var(--card,#fff);',
    '  color:var(--body,#475569);border-radius:var(--r-sm,4px);padding:4px 9px;',
    '  line-height:1.4;transition:background .12s,color .12s,border-color .12s}',
    '.tt-bar button:hover{color:var(--accent,#4285f4);border-color:var(--accent,#4285f4)}',
    '.tt-bar button:disabled{opacity:.55;cursor:default}',
    '.tt-exp{display:inline-flex}',
    '.tt-exp button:first-child{border-top-right-radius:0;border-bottom-right-radius:0}',
    '.tt-exp button:last-child{border-top-left-radius:0;border-bottom-left-radius:0;margin-left:-1px}',
    '.tt-clear{color:var(--accent,#4285f4)!important;border-color:var(--accent,#4285f4)!important;',
    '  background:var(--accent-bg,#eef2ff)!important}',
    '.tt-clear[hidden]{display:none}',
    /* header filter button — inline so it never changes the th's box */
    '.tt-fbtn{display:inline-flex;align-items:center;justify-content:center;',
    '  width:15px;height:15px;margin-left:5px;padding:0;border:none;border-radius:3px;',
    '  background:transparent;color:var(--muted,#94a3b8);cursor:pointer;',
    '  vertical-align:middle;flex:none;opacity:.65;transition:opacity .12s,color .12s}',
    '.tt-fbtn:hover,.tt-fbtn.tt-open{opacity:1;color:var(--accent,#4285f4);',
    '  background:var(--accent-bg,#eef2ff)}',
    '.tt-fbtn.tt-active{opacity:1;color:var(--accent,#4285f4)}',
    '.tt-fbtn svg{width:9px;height:9px;display:block}',
    'th.tt-th-active{color:var(--accent,#4285f4)!important}',
    'tr.tt-hidden{display:none!important}',
    /* dropdown */
    '.tt-panel{position:fixed;z-index:9999;display:none;flex-direction:column;',
    '  width:250px;background:var(--card,#fff);border:1px solid var(--border,#e5e7eb);',
    '  border-radius:var(--r-sm,4px);box-shadow:0 6px 22px rgba(15,23,42,.14);',
    '  font-family:inherit;font-size:12px;color:var(--body,#475569);',
    '  text-align:left;text-transform:none;letter-spacing:normal;font-weight:400}',
    '.tt-panel.tt-shown{display:flex}',
    '.tt-p-head{display:flex;align-items:center;gap:8px;padding:8px 10px;',
    '  border-bottom:1px solid var(--border-lt,#f1f3f5)}',
    '.tt-p-title{font-size:10px;font-weight:700;letter-spacing:.07em;text-transform:uppercase;',
    '  color:var(--muted,#94a3b8);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}',
    '.tt-p-x{margin-left:auto;border:none;background:none;cursor:pointer;font-size:15px;',
    '  line-height:1;color:var(--muted,#94a3b8);padding:0 2px;font-family:inherit}',
    '.tt-p-x:hover{color:var(--h,#0f172a)}',
    '.tt-p-search{margin:8px 10px 6px;padding:5px 8px;font-family:inherit;font-size:12px;',
    '  border:1px solid var(--border,#e5e7eb);border-radius:3px;outline:none;color:inherit}',
    '.tt-p-search:focus{border-color:var(--accent,#4285f4)}',
    '.tt-p-list{overflow-y:auto;max-height:190px;padding:2px 0;border-bottom:1px solid var(--border-lt,#f1f3f5)}',
    '.tt-opt{display:flex;align-items:center;gap:7px;padding:4px 10px;cursor:pointer;',
    '  white-space:nowrap;overflow:hidden}',
    '.tt-opt:hover{background:var(--bg,#f7f8fa)}',
    '.tt-opt span{overflow:hidden;text-overflow:ellipsis;font-variant-numeric:tabular-nums}',
    '.tt-opt input{flex:none;margin:0;accent-color:var(--accent,#4285f4)}',
    '.tt-opt-all{font-weight:600;color:var(--h,#0f172a);border-bottom:1px solid var(--border-lt,#f1f3f5)}',
    '.tt-p-empty{padding:10px;font-size:11px;color:var(--muted,#94a3b8);text-align:center}',
    '.tt-p-more{padding:6px 10px;font-size:10.5px;font-style:italic;color:var(--muted,#94a3b8);',
    '  border-top:1px dashed var(--border-lt,#f1f3f5)}',
    '.tt-p-cond{padding:8px 10px;display:flex;flex-direction:column;gap:6px}',
    '.tt-p-cond select,.tt-p-cond input{font-family:inherit;font-size:12px;padding:4px 6px;',
    '  border:1px solid var(--border,#e5e7eb);border-radius:3px;outline:none;',
    '  background:var(--card,#fff);color:inherit;width:100%}',
    '.tt-p-cond select:focus,.tt-p-cond input:focus{border-color:var(--accent,#4285f4)}',
    '.tt-p-vals{display:flex;gap:6px}',
    '.tt-p-vals[hidden]{display:none}',
    '.tt-p-foot{display:flex;gap:6px;padding:8px 10px;border-top:1px solid var(--border-lt,#f1f3f5)}',
    '.tt-p-foot button{flex:1;font-family:inherit;font-size:11px;font-weight:600;cursor:pointer;',
    '  padding:5px 8px;border-radius:3px;border:1px solid var(--border,#e5e7eb);',
    '  background:var(--card,#fff);color:var(--body,#475569)}',
    '.tt-p-foot button:hover{border-color:var(--accent,#4285f4);color:var(--accent,#4285f4)}',
    '.tt-p-done{background:var(--accent,#4285f4)!important;border-color:var(--accent,#4285f4)!important;',
    '  color:#fff!important}',
    '.tt-p-done:hover{opacity:.9;color:#fff!important}',
    /* scroll containment — a plain block so it inherits the card's own box */
    '.tt-scroll{overflow:auto;position:relative;max-width:100%}',
    '.tt-scroll > table{margin:0}',
    '.tt-scroll::-webkit-scrollbar{width:9px;height:9px}',
    '.tt-scroll::-webkit-scrollbar-track{background:transparent}',
    '.tt-scroll::-webkit-scrollbar-thumb{background:var(--border,#e5e7eb);border-radius:5px}',
    '.tt-scroll::-webkit-scrollbar-thumb:hover{background:var(--muted,#94a3b8)}'
  ].join('');

  /* One markup string, built once per button; the active (filled) look is applied
   * by flipping the path's fill attribute — see _syncHeaders for why the nodes
   * must never be replaced. */
  function funnelSVG(on) {
    return '<svg viewBox="0 0 12 12" aria-hidden="true"><path d="M1.2 2h9.6L7.1 6.4v4.1L4.9 9.1V6.4z"'
      + ' fill="' + (on ? 'currentColor' : 'none') + '" stroke="currentColor"'
      + ' stroke-width="1.3" stroke-linejoin="round"/></svg>';
  }

  function injectCSS() {
    if (document.getElementById('tt-style')) return;
    var s = document.createElement('style');
    s.id = 'tt-style';
    s.textContent = CSS;
    (document.head || document.documentElement).appendChild(s);
  }

  /* ── Value helpers ──────────────────────────────────────────────────────── */

  var WS = /[\s   ]+/g;

  /* Text of a cell, minus anything the page marked as decoration.
   * Walks child nodes instead of cloning: called once per cell per re-render. */
  function cellText(el, ignoreSel) {
    if (!el) return '';
    if (el.dataset && el.dataset.ttValue != null) return el.dataset.ttValue.trim();
    var out;
    if (!ignoreSel) {
      out = el.textContent;
    } else {
      out = '';
      (function walk(node) {
        for (var n = node.firstChild; n; n = n.nextSibling) {
          if (n.nodeType === 3) { out += n.nodeValue; continue; }
          if (n.nodeType !== 1) continue;
          if (n.matches && n.matches(ignoreSel)) continue;
          walk(n);
        }
      })(el);
    }
    return out.replace(WS, ' ').trim();
  }

  var SCALES = { k: 1e3, m: 1e6, b: 1e9 };

  /* Parse the FIRST numeric token in a formatted cell.
   *   "1,2M kr ▲ +4,2%" → 1200000   (compact suffix applied; the delta badge,
   *                                  which trails the real value, is ignored)
   *   "1 234 kr"        → 1234      ("kr" is not a K suffix — the letter has to
   *                                  stand alone to count as a magnitude)
   *   "12,3%"           → 12.3      (percent kept in display units)
   *   "(1 234)"         → -1234     "—" / "" → null
   * Handles sv-SE (space thousands, comma decimal) and en-US grouping alike. */
  function parseNum(raw) {
    if (raw == null) return null;
    var s = String(raw).replace(/[   ]/g, ' ').trim();
    if (!s) return null;
    var neg = false;
    var paren = s.match(/^\(\s*(.*?)\s*\)$/);
    if (paren) { neg = true; s = paren[1]; }
    var m = s.match(/[-−–+]?\s*\d[\d '’.,]*/);
    if (!m) return null;
    var tok = m[0], rest = s.slice(m.index + m[0].length);
    if (/^[-−–]/.test(tok.trim())) neg = !neg;
    tok = tok.replace(/[\s'’]/g, '').replace(/^[-−–+]/, '').replace(/[.,]+$/, '');
    var lc = tok.lastIndexOf(','), ld = tok.lastIndexOf('.');
    if (lc > -1 && ld > -1) {
      tok = lc > ld ? tok.replace(/\./g, '').replace(/,/g, '.')
                    : tok.replace(/,/g, '');
    } else if (lc > -1) {
      tok = (/^\d{1,3}(,\d{3})+$/.test(tok) || tok.split(',').length > 2)
              ? tok.replace(/,/g, '') : tok.replace(/,/g, '.');
    } else if (ld > -1) {
      if (/^\d{1,3}(\.\d{3})+$/.test(tok) || tok.split('.').length > 2) tok = tok.replace(/\./g, '');
    }
    var v = parseFloat(tok);
    if (!isFinite(v)) return null;
    // A magnitude suffix only counts when the letter is not the head of a word
    // ("M kr" and "1,2M" scale; "kr" and "kSEK" do not).
    var suf = rest.match(/^\s*([KkMmBb])(?![A-Za-zÀ-ÿ])/);
    if (suf) v *= SCALES[suf[1].toLowerCase()];
    return neg ? -v : v;
  }

  var TRANSPARENT = /^(transparent|rgba?\(\s*0\s*,\s*0\s*,\s*0\s*,\s*0\s*\))$/i;

  /* A sticky cell painted over scrolling rows has to be opaque, and these pages
   * theme through CSS vars, so the colour cannot be hard-coded. Walk the cell's
   * ancestors for the first non-transparent background (th → thead → table → card),
   * then --card, then white. */
  function resolveBg(el) {
    for (var n = el; n && n.nodeType === 1 && n !== document.documentElement; n = n.parentElement) {
      var c = getComputedStyle(n).backgroundColor;
      if (c && !TRANSPARENT.test(c.replace(/\s+/g, ' '))) return c;
    }
    var v = getComputedStyle(document.body).getPropertyValue('--card').trim();
    return v || '#ffffff';
  }

  /* With border-collapse:collapse a sticky cell's own border scrolls away with the
   * table, so the header edge is drawn as an inset shadow instead. */
  function resolveBorder(el) {
    var cs = getComputedStyle(el);
    var c = cs.borderBottomColor || cs.borderTopColor;
    if (c && !TRANSPARENT.test(c.replace(/\s+/g, ' '))) return c;
    var v = getComputedStyle(document.body).getPropertyValue('--border').trim();
    return v || '#e5e7eb';
  }

  /* Sticky is set inline, not via the injected stylesheet: table-tools.js is loaded
   * in <head> before each page's own <style>, so an equally specific page rule such
   * as `table.sortable thead th { position: relative }` would win the tie and kill
   * the stick. Inline also survives nothing — a page that rebuilds its <thead> drops
   * these, and refresh() puts them back. */
  function stickCell(cell, side, offset, bg, shadow) {
    var s = cell.style;
    if (s.position !== 'sticky') s.position = 'sticky';
    var v = offset + 'px';
    if (s[side] !== v) s[side] = v;
    if (!s.zIndex) s.zIndex = '3';
    if (bg && !s.backgroundColor) s.backgroundColor = bg;
    if (shadow && !s.boxShadow) s.boxShadow = shadow;
  }

  function unstickCell(cell) {
    ['position', 'top', 'bottom', 'zIndex', 'backgroundColor', 'boxShadow']
      .forEach(function (p) { cell.style[p] = ''; });
  }

  /* Rendered rows — a page-level search box that hides rows with the `hidden`
   * attribute counts as hidden here too, since this drives layout height. */
  function rowShown(r) {
    return !r.classList.contains('tt-hidden') && !r.hidden;
  }

  function todayISO() {
    var d = new Date(), p = function (n) { return (n < 10 ? '0' : '') + n; };
    return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate());
  }

  function slug(s) {
    return String(s || '').toLowerCase().replace(/[^a-z0-9]+/g, '-')
      .replace(/^-+|-+$/g, '').slice(0, 60) || 'table';
  }

  /* ── Lazy SheetJS loader (one in-flight promise, shared by every table) ──── */
  var sheetPromise = null;
  function loadSheetJS(url) {
    if (window.XLSX) return Promise.resolve(window.XLSX);
    if (sheetPromise) return sheetPromise;
    sheetPromise = new Promise(function (res, rej) {
      var s = document.createElement('script');
      s.src = url || SHEETJS_URL;
      s.async = true;
      s.onload = function () { window.XLSX ? res(window.XLSX) : rej(new Error('SheetJS did not load')); };
      s.onerror = function () { sheetPromise = null; rej(new Error('SheetJS could not be fetched')); };
      document.head.appendChild(s);
    });
    return sheetPromise;
  }

  function download(blob, name) {
    var url = URL.createObjectURL(blob), a = document.createElement('a');
    a.href = url; a.download = name; a.style.display = 'none';
    document.body.appendChild(a); a.click();
    setTimeout(function () { a.remove(); URL.revokeObjectURL(url); }, 0);
  }

  /* ── The shared dropdown (one per document) ──────────────────────────────── */
  var panel = null, panelOwner = null;   // panelOwner = { inst, col, btn }

  function buildPanel() {
    if (panel) return panel;
    panel = document.createElement('div');
    panel.className = 'tt-panel';
    panel.setAttribute('role', 'dialog');
    panel.innerHTML =
      '<div class="tt-p-head"><span class="tt-p-title"></span>'
      + '<button type="button" class="tt-p-x" aria-label="Close">×</button></div>'
      + '<input type="text" class="tt-p-search" placeholder="Search values…" aria-label="Search values">'
      + '<div class="tt-p-list"></div>'
      + '<div class="tt-p-cond">'
      + '<select class="tt-p-op" aria-label="Condition"></select>'
      + '<div class="tt-p-vals" hidden>'
      + '<input type="text" class="tt-p-a" aria-label="Value"><input type="text" class="tt-p-b" aria-label="Second value">'
      + '</div></div>'
      + '<div class="tt-p-foot"><button type="button" class="tt-p-clear">Clear</button>'
      + '<button type="button" class="tt-p-done">Done</button></div>';
    document.body.appendChild(panel);

    var q = function (s) { return panel.querySelector(s); };
    q('.tt-p-x').addEventListener('click', closePanel);
    q('.tt-p-done').addEventListener('click', closePanel);
    q('.tt-p-clear').addEventListener('click', function () {
      if (!panelOwner) return;
      panelOwner.inst._resetCol(panelOwner.col);
      renderPanelBody();
      panelOwner.inst.apply();
    });
    q('.tt-p-search').addEventListener('input', renderList);
    // One delegated listener for the whole list instead of one per option — at the
    // cap that is 180 listeners saved per open, and it survives list re-renders.
    q('.tt-p-list').addEventListener('change', function (e) {
      var box = e.target;
      if (!panelOwner || !box || box.type !== 'checkbox') return;
      var st = panelOwner.inst._state(panelOwner.col);
      if (box._ttAll) {
        var m = panelOwner.matches, on = box.checked, i;
        for (i = 0; i < m.length; i++) {
          if (on) delete st.excluded[m[i]]; else st.excluded[m[i]] = 1;
        }
        // Re-tick the rendered options in place rather than rebuilding the list:
        // rebuilding mid-change would drop the node the event came from.
        var boxes = panel.querySelectorAll('.tt-p-list .tt-opt:not(.tt-opt-all) input');
        for (i = 0; i < boxes.length; i++) boxes[i].checked = on;
        box.indeterminate = false;
      } else {
        if (box.checked) delete st.excluded[box._ttVal]; else st.excluded[box._ttVal] = 1;
        syncAllBox();
      }
      panelOwner.inst.apply();
    });
    q('.tt-p-op').addEventListener('change', function () {
      var st = panelOwner && panelOwner.inst._state(panelOwner.col);
      if (!st) return;
      st.op = this.value;
      syncCondInputs();
      panelOwner.inst.apply();
    });
    ['.tt-p-a', '.tt-p-b'].forEach(function (sel) {
      q(sel).addEventListener('input', function () {
        var st = panelOwner && panelOwner.inst._state(panelOwner.col);
        if (!st) return;
        st[sel === '.tt-p-a' ? 'a' : 'b'] = this.value;
        panelOwner.inst.apply();
      });
    });
    panel.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') { e.stopPropagation(); closePanel(); }
    });
    return panel;
  }

  function closePanel() {
    if (!panel) return;
    panel.classList.remove('tt-shown');
    if (panelOwner) {
      panelOwner.btn.classList.remove('tt-open');
      panelOwner.inst._syncHeaders();
      panelOwner = null;
    }
  }

  var NUM_OPS = [['', 'No condition'], ['eq', 'Equals'], ['gt', 'Greater than'],
                 ['lt', 'Less than'], ['between', 'Between']];
  var TXT_OPS = [['', 'No condition'], ['contains', 'Contains'],
                 ['ncontains', 'Does not contain'], ['eq', 'Equals']];

  function openPanel(inst, col, btn, label, numeric) {
    buildPanel();
    if (panelOwner && panelOwner.btn === btn) { closePanel(); return; }
    closePanel();
    panelOwner = { inst: inst, col: col, btn: btn, numeric: numeric,
                   container: btn.closest('.tt-scroll'),
                   /* Frozen on open: recomputing while the user ticks boxes would
                      make the list shrink under the pointer. */
                   values: inst._distinct(col) };
    btn.classList.add('tt-open');
    panel.querySelector('.tt-p-title').textContent = label || ('Column ' + (col + 1));
    var op = panel.querySelector('.tt-p-op');
    op.innerHTML = (numeric ? NUM_OPS : TXT_OPS).map(function (o) {
      return '<option value="' + o[0] + '">' + o[1] + '</option>';
    }).join('');
    panel.querySelector('.tt-p-search').value = '';
    renderPanelBody();
    panel.classList.add('tt-shown');
    placePanel();
    panel.querySelector('.tt-p-search').focus();
  }

  function renderPanelBody() {
    var st = panelOwner.inst._state(panelOwner.col);
    panel.querySelector('.tt-p-op').value = st.op || '';
    panel.querySelector('.tt-p-a').value = st.a || '';
    panel.querySelector('.tt-p-b').value = st.b || '';
    syncCondInputs();
    renderList();
  }

  function syncCondInputs() {
    var op = panel.querySelector('.tt-p-op').value;
    panel.querySelector('.tt-p-vals').hidden = !op;
    panel.querySelector('.tt-p-b').style.display = op === 'between' ? '' : 'none';
    panel.querySelector('.tt-p-a').placeholder = op === 'between' ? 'from' : 'value';
  }

  /* Checkbox list.
   *
   * State is an *excluded* set keyed by value, never a list of DOM nodes: values
   * that show up for the first time after a re-render default to visible, and the
   * checked state survives any re-render of this list.
   *
   * Only LIST_CAP options are ever put in the DOM. A column with ~1000 distinct
   * values took ~1.1s to open when each one became a <label> — the distinct scan is
   * cheap, building the option DOM was not. Everything past the cap stays reachable
   * through the search box, which always searches the FULL distinct list (o.values),
   * not the rendered slice.
   *
   * "Select all" semantics under truncation: it operates on every value matching the
   * current search — including matches past the cap — and on nothing else. With an
   * empty search box that is all distinct values; with "naver" typed it is every
   * Naver value whether rendered or not. The rendered slice is never the unit of
   * selection, and its tri-state is likewise computed over the full matching set.
   * That keeps one invariant: what the box says is what a click will do. */
  var LIST_CAP = 180;

  function renderList() {
    var o = panelOwner, st = o.inst._state(o.col);
    var needle = panel.querySelector('.tt-p-search').value.trim().toLowerCase();
    var matches = o.values, i;
    if (needle) {
      matches = [];
      for (i = 0; i < o.values.length; i++) {
        if (o.values[i].toLowerCase().indexOf(needle) > -1) matches.push(o.values[i]);
      }
    }
    o.matches = matches;                       // what "Select all" and the tri-state act on
    var list = panel.querySelector('.tt-p-list');
    list.textContent = '';
    o.allBox = null;
    if (!matches.length) {
      var empty = document.createElement('div');
      empty.className = 'tt-p-empty';
      empty.textContent = 'No matching values';
      list.appendChild(empty);
      return;
    }

    var frag = document.createDocumentFragment();
    var all = document.createElement('label');
    all.className = 'tt-opt tt-opt-all';
    var allBox = document.createElement('input');
    allBox.type = 'checkbox';
    allBox._ttAll = true;
    var allLabel = document.createElement('span');
    allLabel.textContent = needle ? 'Select all ' + matches.length + ' matching' : 'Select all';
    all.appendChild(allBox);
    all.appendChild(allLabel);
    frag.appendChild(all);
    o.allBox = allBox;

    // createElement over innerHTML: no HTML parse per option, and one insertion.
    var n = Math.min(matches.length, LIST_CAP);
    for (i = 0; i < n; i++) {
      var v = matches[i];
      var row = document.createElement('label');
      row.className = 'tt-opt';
      var box = document.createElement('input');
      box.type = 'checkbox';
      box.checked = !st.excluded[v];
      box._ttVal = v;
      var span = document.createElement('span');
      span.textContent = v === '' ? '(blank)' : v;
      span.title = v;
      row.appendChild(box);
      row.appendChild(span);
      frag.appendChild(row);
    }
    if (matches.length > n) {
      var more = document.createElement('div');
      more.className = 'tt-p-more';
      more.textContent = '…and ' + (matches.length - n) + ' more — type to search';
      frag.appendChild(more);
    }
    list.appendChild(frag);
    syncAllBox();
    list.scrollTop = 0;
  }

  /* Tri-state of "Select all", over the full matching set — not the rendered slice. */
  function syncAllBox() {
    var o = panelOwner;
    if (!o || !o.allBox) return;
    var st = o.inst._state(o.col), m = o.matches, on = 0;
    for (var i = 0; i < m.length; i++) if (!st.excluded[m[i]]) on++;
    o.allBox.checked = on === m.length;
    o.allBox.indeterminate = on > 0 && on < m.length;
  }

  /* Keep the dropdown on screen — it is position:fixed, so viewport-relative.
   * Flips above the button when it does not fit below, and caps its height (the
   * value list scrolls) so the footer buttons can never sit past the fold. */
  function placePanel(autoClose) {
    if (!panelOwner) return;
    var vw = window.innerWidth || document.documentElement.clientWidth || 1024;
    var vh = window.innerHeight || document.documentElement.clientHeight || 768;
    var r = panelOwner.btn.getBoundingClientRect();
    // Scrolling (the page, or a table inside its own scrollport) can carry the
    // anchoring header off screen; a dropdown floating next to nothing is worse
    // than a closed one. Only on scroll/resize — never on the initial open.
    if (autoClose) {
      if (r.bottom < 0 || r.top > vh || r.right < 0 || r.left > vw) { closePanel(); return; }
      var cont = panelOwner.container;
      if (cont) {
        var cr = cont.getBoundingClientRect();
        if (r.bottom < cr.top - 1 || r.top > cr.bottom + 1) { closePanel(); return; }
      }
    }
    var pw = panel.offsetWidth || 250;
    panel.style.left = Math.max(8, Math.min(r.left - 8, vw - pw - 8)) + 'px';
    panel.style.top = '0px';
    panel.style.maxHeight = 'none';
    var want = panel.offsetHeight;
    var below = vh - r.bottom - 12, above = r.top - 12, ph = want, top;
    if (want <= below) { top = r.bottom + 4; }
    else if (want <= above) { top = r.top - 4 - ph; }
    else if (below >= above) { ph = Math.max(160, below); top = r.bottom + 4; }
    else { ph = Math.max(160, above); top = r.top - 4 - ph; }
    top = Math.max(8, Math.min(top, vh - ph - 8));
    panel.style.top = top + 'px';
    panel.style.maxHeight = Math.min(ph, vh - 16) + 'px';
  }

  document.addEventListener('mousedown', function (e) {
    if (!panelOwner) return;
    if (panel.contains(e.target) || panelOwner.btn.contains(e.target)) return;
    closePanel();
  }, true);
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && panelOwner) closePanel();
  });
  window.addEventListener('resize', function () { if (panelOwner) placePanel(true); });
  // Capture phase on window: scroll does not bubble, but it is still dispatched
  // through the capture path — this is what catches a .tt-scroll container's own
  // scrolling as well as the page's.
  window.addEventListener('scroll', function () { if (panelOwner) placePanel(true); }, true);

  /* ── Instance ───────────────────────────────────────────────────────────── */

  var registry = [];   // [{ table, inst }] — WeakMap would do, but this also powers enhanceAll()

  function Tools(table, opts) {
    opts = opts || {};
    var d = table.dataset || {};
    this.table = table;
    this.opts = opts;
    this.ignoreSel = opts.exportIgnore || d.exportIgnore || null;
    this.exportName = opts.exportName || d.exportName || table.id
      || slug(document.title.split(/[·|—-]/)[0]);
    this.filters = opts.filters !== false && d.noFilters == null;
    this.exports = opts.exports !== false && d.noExport == null;
    this.footerMode = opts.exportFooter != null ? opts.exportFooter
      : (d.exportFooter != null ? d.exportFooter : 'auto');
    this.sheetUrl = opts.sheetJsUrl || SHEETJS_URL;
    this.onChange = opts.onChange || null;
    this.state = {};            // colIndex → { excluded:{}, op, a, b }
    this._numeric = {};         // colIndex → bool (re-derived on refresh)
    this._cols = null;          // parsed header cells for the active header row

    // Which columns get a filter button. 'all' (default), a list of indices, or
    // a predicate (index, thEl) → bool. data-filter-columns="0,3" / "none".
    var fc = opts.filterColumns != null ? opts.filterColumns : d.filterColumns;
    if (typeof fc === 'string') {
      fc = fc.trim().toLowerCase() === 'none' ? []
         : fc.trim().toLowerCase() === 'all' ? null
         : fc.split(',').map(function (n) { return parseInt(n, 10); })
             .filter(function (n) { return !isNaN(n); });
    }
    this.filterColumns = fc == null ? null : fc;

    // Scroll containment. A table longer than maxRows rendered rows is wrapped in
    // its own scrollport so the page stops growing; maxVh additionally caps that
    // port to a share of the viewport, without which a 25-row container is taller
    // than the screen and its sticky header never actually sticks in view.
    this.scroll = opts.scroll !== false && d.scroll !== 'false';
    this.maxRows = opts.maxRows != null ? +opts.maxRows : (d.maxRows != null ? +d.maxRows : 25);
    this.maxHeight = opts.maxHeight != null ? opts.maxHeight : d.maxHeight;
    this.maxVh = opts.maxVh != null ? +opts.maxVh : (d.maxVh != null ? +d.maxVh : 78);
    this.stickyFoot = opts.stickyFoot !== false && d.stickyFoot !== 'false';

    // Header row carrying the filter buttons: the LAST thead row by default, so
    // grouped headers (a spanning row above a leaf row) land on the leaf row.
    this.headerRowIndex = opts.headerRow != null ? +opts.headerRow
      : (d.headerRow != null ? +d.headerRow : -1);

    injectCSS();
    this._buildToolbar();
    this.refresh();
    if (opts.observe !== false) this._observe();
    registry.push({ table: table, inst: this });
  }

  Tools.prototype._headerRow = function () {
    var head = this.table.tHead;
    if (!head || !head.rows.length) return null;
    var i = this.headerRowIndex;
    return i < 0 ? head.rows[head.rows.length + i] : head.rows[i];
  };

  /* Header cells mapped to column indices, honouring colspan. Spanning cells get
   * no filter button — one button cannot address several columns. */
  Tools.prototype._headerCells = function () {
    var row = this._headerRow(), out = [];
    if (!row) return out;
    var col = 0;
    for (var i = 0; i < row.cells.length; i++) {
      var th = row.cells[i], span = th.colSpan || 1;
      out.push({ th: th, col: col, span: span });
      col += span;
    }
    return out;
  };

  Tools.prototype._bodyRows = function () {
    var rows = [], b = this.table.tBodies;
    for (var i = 0; i < b.length; i++) {
      for (var j = 0; j < b[i].rows.length; j++) rows.push(b[i].rows[j]);
    }
    return rows;
  };

  /* Cell for a column index. Rows whose cell count matches the header are the
   * common case; anything else (colspan'd empty-state rows) is skipped, which
   * makes such rows filter-transparent rather than wrongly hidden. */
  Tools.prototype._cell = function (row, col) {
    if (row.cells.length === this._colCount) return row.cells[col];
    var c = 0;
    for (var i = 0; i < row.cells.length; i++) {
      var span = row.cells[i].colSpan || 1;
      if (col < c + span) return span === 1 ? row.cells[i] : null;
      c += span;
    }
    return null;
  };

  Tools.prototype._val = function (row, col) {
    return cellText(this._cell(row, col), this.ignoreSel);
  };

  /* excluded uses a null prototype: the keys are arbitrary cell values, and on a
   * plain object a value of "__proto__" would silently fail to register. */
  function newColState() {
    return { excluded: Object.create(null), op: '', a: '', b: '' };
  }

  /* Emptiness in O(1). Object.keys().length allocated an array per call, which at a
   * thousand excluded values ran once per row per filter pass. */
  function hasKeys(o) {
    for (var k in o) return true;
    return false;
  }

  Tools.prototype._state = function (col) {
    if (!this.state[col]) this.state[col] = newColState();
    return this.state[col];
  };

  Tools.prototype._resetCol = function (col) {
    this.state[col] = newColState();
  };

  Tools.prototype._colActive = function (col) {
    var st = this.state[col];
    if (!st) return false;
    if (hasKeys(st.excluded)) return true;
    if (!st.op) return false;
    if (st.op === 'between') return st.a !== '' || st.b !== '';
    return st.a !== '';
  };

  Tools.prototype.activeCount = function () {
    var n = 0;
    for (var k in this.state) if (this._colActive(+k)) n++;
    return n;
  };

  Tools.prototype._matchCol = function (row, col) {
    var st = this.state[col];
    if (!st) return true;
    var raw = this._val(row, col);
    if (st.excluded[raw]) return false;
    if (!st.op) return true;
    if (this._numeric[col]) {
      var v = parseNum(raw);
      var a = parseNum(st.a), b = parseNum(st.b);
      if (st.op === 'between') {
        if (a == null && b == null) return true;
        if (v == null) return false;
        if (a != null && b != null) return v >= Math.min(a, b) && v <= Math.max(a, b);
        return a != null ? v >= a : v <= b;
      }
      if (a == null) return true;
      if (v == null) return false;
      if (st.op === 'gt') return v > a;
      if (st.op === 'lt') return v < a;
      return v === a;
    }
    var needle = String(st.a || '').toLowerCase();
    if (!needle) return true;
    var hay = raw.toLowerCase();
    if (st.op === 'contains') return hay.indexOf(needle) > -1;
    if (st.op === 'ncontains') return hay.indexOf(needle) === -1;
    return hay === needle;
  };

  /* Distinct values for one column, narrowed by the OTHER columns' filters —
   * the spreadsheet behaviour, so the list only offers values you could reach. */
  Tools.prototype._distinct = function (col) {
    var self = this, seen = Object.create(null), out = [];
    this._bodyRows().forEach(function (row) {
      for (var k in self.state) {
        if (+k !== col && self._colActive(+k) && !self._matchCol(row, +k)) return;
      }
      var cell = self._cell(row, col);
      if (!cell) return;
      var v = cellText(cell, self.ignoreSel);
      if (!(v in seen)) { seen[v] = 1; out.push(v); }
    });
    if (this._numeric[col]) {
      out.sort(function (a, b) {
        var x = parseNum(a), y = parseNum(b);
        if (x == null) return 1;
        if (y == null) return -1;
        return x - y;
      });
    } else {
      out.sort(function (a, b) { return a.localeCompare(b, 'sv'); });
    }
    return out;
  };

  /* Placeholders for "no value". Counted as neither numeric nor text, otherwise a
   * ratio column where half the rows render "—" would be misread as a text column
   * and lose its greater-than/between conditions. */
  var BLANKISH = /^(—|–|-|−|n\/?a|null|na)$/i;

  /* A column counts as numeric when most of its filled cells parse.
   *
   * Sampled with a stride rather than read in full: this runs for every column on
   * every refresh, and a thousand rows × a dozen columns is twelve thousand DOM text
   * walks for what is only a heuristic. The stride spans the whole table, so it does
   * not misread a column whose first screenful happens to be all dashes. */
  var NUMERIC_SAMPLE = 150;

  Tools.prototype._detectNumeric = function () {
    var self = this, rows = this._bodyRows();
    this._numeric = {};
    var override = this.opts.numericColumns;
    var stride = Math.max(1, Math.ceil(rows.length / NUMERIC_SAMPLE));
    this._headerCells().forEach(function (h) {
      if (h.span !== 1) return;
      if (override) { self._numeric[h.col] = override.indexOf(h.col) > -1; return; }
      var n = 0, ok = 0;
      for (var i = 0; i < rows.length; i += stride) {
        var v = cellText(self._cell(rows[i], h.col), self.ignoreSel);
        if (!v || BLANKISH.test(v)) continue;
        n++;
        if (parseNum(v) != null) ok++;
      }
      self._numeric[h.col] = n > 0 && ok / n >= 0.6;
    });
  };

  /* Idempotent: re-adds only the buttons a thead rebuild wiped out. */
  Tools.prototype._decorateHeaders = function () {
    if (!this.filters) return;
    var self = this;
    this._headerCells().forEach(function (h) {
      if (h.span !== 1) return;
      if (self.filterColumns && self.filterColumns.indexOf(h.col) === -1) return;
      if (h.th.querySelector(':scope > .tt-fbtn')) {
        h.th._ttCol = h.col;     // column index can shift when a header is rebuilt
        return;
      }
      var label = cellText(h.th, '.tt-fbtn') || ('Column ' + (h.col + 1));
      var btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'tt-fbtn';
      btn.innerHTML = funnelSVG(false);
      btn.dataset.ttActive = '0';
      btn.title = 'Filter ' + label;
      btn.setAttribute('aria-label', 'Filter ' + label);
      btn.addEventListener('click', function (e) {
        e.preventDefault(); e.stopPropagation();
        openPanel(self, h.th._ttCol, btn, cellText(h.th, '.tt-fbtn'), !!self._numeric[h.th._ttCol]);
      });
      h.th._ttCol = h.col;
      h.th.appendChild(btn);
    });
  };

  /* Never replace a filter button's child nodes here. This runs from closePanel(),
   * which runs from the document-level mousedown handler: rewriting innerHTML
   * detaches the very <svg> that received the mousedown, and Chrome then skips the
   * click event entirely — so clicking button B while A's panel was open only
   * closed A, and B needed a second click. Flip the existing path's fill instead,
   * and only when the state actually changed. */
  Tools.prototype._syncHeaders = function () {
    var self = this;
    this._headerCells().forEach(function (h) {
      var btn = h.th.querySelector(':scope > .tt-fbtn');
      if (!btn) return;
      var on = self._colActive(h.col);
      btn.classList.toggle('tt-active', on);
      h.th.classList.toggle('tt-th-active', on);
      if ((btn.dataset.ttActive === '1') === on) return;
      btn.dataset.ttActive = on ? '1' : '0';
      var path = btn.querySelector('path');
      if (path) path.setAttribute('fill', on ? 'currentColor' : 'none');
      else btn.innerHTML = funnelSVG(on);   // icon markup lost (page rewrote the th)
    });
    if (this.clearBtn) {
      var n = this.activeCount();
      this.clearBtn.hidden = n === 0;
      this.clearBtn.textContent = 'Clear filters' + (n > 1 ? ' (' + n + ')' : '');
    }
  };

  Tools.prototype._buildToolbar = function () {
    if (!this.exports && !this.filters) return;
    var self = this;
    var bar = document.createElement('div');
    bar.className = 'tt-bar';

    if (this.filters) {
      var clear = document.createElement('button');
      clear.type = 'button';
      clear.className = 'tt-clear';
      clear.textContent = 'Clear filters';
      clear.hidden = true;
      clear.addEventListener('click', function () { self.clearFilters(); });
      bar.appendChild(clear);
      this.clearBtn = clear;
    }

    if (this.exports) {
      var grp = document.createElement('div');
      grp.className = 'tt-exp';
      var csv = document.createElement('button');
      csv.type = 'button'; csv.textContent = 'CSV';
      csv.title = 'Download the visible rows as CSV';
      csv.addEventListener('click', function () { self.exportCSV(); });
      var xls = document.createElement('button');
      xls.type = 'button'; xls.textContent = 'Sheets';
      xls.title = 'Download the visible rows as .xlsx (opens in Google Sheets)';
      xls.addEventListener('click', function () {
        xls.disabled = true;
        var old = xls.textContent;
        xls.textContent = '…';
        self.exportXLSX().catch(function (e) {
          console.warn('[TableTools] xlsx export failed', e);
          alert('Could not build the .xlsx file — the spreadsheet library could not be loaded.');
        }).then(function () { xls.disabled = false; xls.textContent = old; });
      });
      grp.appendChild(csv); grp.appendChild(xls);
      bar.appendChild(grp);
    }

    var target = this.opts.toolbar || (this.table.dataset || {}).toolsToolbar;
    if (typeof target === 'string') target = document.querySelector(target);
    if (target) target.appendChild(bar);
    else this.table.parentNode.insertBefore(bar, this.table);   // fallback: just above the table
    this.bar = bar;
  };

  /* ── Scroll containment ─────────────────────────────────────────────────── */

  /* Wraps the table in its own scrollport once it is longer than maxRows, and keeps
   * the header (and footer totals) stuck. Idempotent — the MutationObserver calls
   * refresh() after every re-render, so this must never wrap twice.
   *
   * Deliberately one-way: once wrapped the table stays wrapped. max-height only
   * caps, so a container whose rows are filtered down to three simply shrinks to
   * fit; unwrapping would mean re-measuring and re-laying out on every keystroke in
   * a filter box for no visible gain. */
  Tools.prototype._ensureScroll = function () {
    if (!this.scroll) return;
    if (!this.wrap) {
      var rows = this._bodyRows();
      var shown = 0;
      for (var i = 0; i < rows.length; i++) if (rowShown(rows[i])) shown++;
      if (shown <= this.maxRows) return;
      var parent = this.table.parentNode;
      if (!parent) return;
      if (parent.classList && parent.classList.contains('tt-scroll')) {
        this.wrap = parent;                       // adopted (e.g. after a destroy/re-enhance)
      } else {
        var wrap = document.createElement('div');
        wrap.className = 'tt-scroll';
        parent.insertBefore(wrap, this.table);
        wrap.appendChild(this.table);             // moving the table fires no childList on it
        this.wrap = wrap;
      }
    }
    this._applyMaxHeight();
    this._applySticky();
  };

  Tools.prototype._applyMaxHeight = function () {
    if (!this.wrap) return;
    if (this.maxHeight) {
      var v = String(this.maxHeight).trim();
      this.wrap.style.maxHeight = /^[\d.]+$/.test(v) ? v + 'px' : v;
      return;
    }
    var rows = this._bodyRows(), probe = null;
    for (var i = 0; i < rows.length; i++) if (rowShown(rows[i])) { probe = rows[i]; break; }
    var rowH = probe ? probe.offsetHeight : 0;
    if (!rowH) return;                                        // nothing rendered to measure yet
    if (this._rowH && Math.abs(this._rowH - rowH) < 1.5) return;   // unchanged — skip the write
    this._rowH = rowH;
    var head = this.table.tHead ? this.table.tHead.offsetHeight : 0;
    var foot = this.table.tFoot ? this.table.tFoot.offsetHeight : 0;
    var px = Math.round(head + foot + rowH * this.maxRows + 2);
    this.wrap.style.maxHeight = px + 'px';
    // Viewport cap as CSS so it survives resizes. An engine without min() ignores
    // the assignment and keeps the plain px value set above.
    if (this.maxVh > 0) this.wrap.style.maxHeight = 'min(' + px + 'px, ' + this.maxVh + 'vh)';
  };

  Tools.prototype._applySticky = function () {
    if (!this.wrap) return;
    var head = this.table.tHead, r, c, row;
    if (head) {
      var top = 0;
      for (r = 0; r < head.rows.length; r++) {
        row = head.rows[r];
        for (c = 0; c < row.cells.length; c++) {
          stickCell(row.cells[c], 'top', top, resolveBg(row.cells[c]),
                    'inset 0 -1px 0 ' + resolveBorder(row.cells[c]));
        }
        top += row.offsetHeight;                  // grouped header rows stack below each other
      }
    }
    // <tfoot> totals stick to the bottom edge: the spreadsheet behaviour, and the
    // reason the totals row stays readable while scrolling a few hundred rows.
    if (this.stickyFoot && this.table.tFoot) {
      var foot = this.table.tFoot, bottom = 0;
      for (r = foot.rows.length - 1; r >= 0; r--) {
        row = foot.rows[r];
        for (c = 0; c < row.cells.length; c++) {
          stickCell(row.cells[c], 'bottom', bottom, resolveBg(row.cells[c]),
                    'inset 0 1px 0 ' + resolveBorder(row.cells[c]));
        }
        bottom += row.offsetHeight;
      }
    }
  };

  Tools.prototype._unwrap = function () {
    var t = this.table;
    ['thead', 'tfoot'].forEach(function (part) {
      var sec = t.querySelector(part);
      if (!sec) return;
      for (var r = 0; r < sec.rows.length; r++) {
        for (var c = 0; c < sec.rows[r].cells.length; c++) unstickCell(sec.rows[r].cells[c]);
      }
    });
    if (this.wrap && this.wrap.parentNode) {
      this.wrap.parentNode.insertBefore(t, this.wrap);
      this.wrap.remove();
    }
    this.wrap = null;
    this._rowH = 0;
  };

  /* ── Public per-instance operations ─────────────────────────────────────── */

  Tools.prototype.apply = function () {
    var self = this, cols = [];
    for (var k in this.state) if (this._colActive(+k)) cols.push(+k);
    var visible = 0;
    this._bodyRows().forEach(function (row) {
      var show = cols.every(function (c) { return self._matchCol(row, c); });
      row.classList.toggle('tt-hidden', !show);
      if (show) visible++;
    });
    this._visible = visible;
    this._syncHeaders();
    if (this.onChange) this.onChange(visible, this);
    return visible;
  };

  Tools.prototype.refresh = function () {
    this._colCount = (function (row) {
      if (!row) return 0;
      var n = 0;
      for (var i = 0; i < row.cells.length; i++) n += row.cells[i].colSpan || 1;
      return n;
    })(this._headerRow());
    this._detectNumeric();
    this._decorateHeaders();
    this.apply();
    this._ensureScroll();          // after apply(): the wrap test counts rendered rows
    if (panelOwner && panelOwner.inst === this) placePanel();
    return this;
  };

  Tools.prototype.clearFilters = function () {
    this.state = {};
    if (panelOwner && panelOwner.inst === this) {
      panelOwner.values = this._distinct(panelOwner.col);
      renderPanelBody();
    }
    this.apply();
    return this;
  };

  Tools.prototype.visibleRows = function () {
    return this._bodyRows().filter(function (r) { return !r.classList.contains('tt-hidden'); });
  };

  /* Header labels + visible body rows as a 2-D array of clean strings. */
  Tools.prototype.matrix = function () {
    var self = this, out = [];
    var head = this._headerRow();
    if (head) {
      var hs = [];
      for (var i = 0; i < head.cells.length; i++) {
        var th = head.cells[i], span = th.colSpan || 1;
        hs.push(cellText(th, this.ignoreSel ? this.ignoreSel + ',.tt-fbtn' : '.tt-fbtn'));
        for (var s = 1; s < span; s++) hs.push('');
      }
      out.push(hs);
    }
    this.visibleRows().forEach(function (row) {
      var r = [];
      for (var i = 0; i < row.cells.length; i++) {
        var td = row.cells[i], span = td.colSpan || 1;
        r.push(cellText(td, self.ignoreSel));
        for (var s = 1; s < span; s++) r.push('');
      }
      out.push(r);
    });
    // Footer totals are stale the moment any row is hidden, so 'auto' drops them
    // exactly then — judged on what is actually rendered, which also catches a
    // page's own search box hiding rows with the `hidden` attribute, not just this
    // module's column filters. exportFooter: true/false forces the behaviour.
    var wantFoot = this.footerMode === true || this.footerMode === 'true'
      || (this.footerMode === 'auto' && this._bodyRows().every(rowShown));
    if (wantFoot && this.table.tFoot) {
      for (var f = 0; f < this.table.tFoot.rows.length; f++) {
        var fr = this.table.tFoot.rows[f], rr = [];
        for (var c = 0; c < fr.cells.length; c++) {
          var fc = fr.cells[c], sp = fc.colSpan || 1;
          rr.push(cellText(fc, self.ignoreSel));
          for (var s2 = 1; s2 < sp; s2++) rr.push('');
        }
        out.push(rr);
      }
    }
    return out;
  };

  Tools.prototype.filename = function (ext) {
    return slug(this.exportName) + '-' + todayISO() + '.' + ext;
  };

  Tools.prototype.exportCSV = function () {
    var rows = this.matrix().map(function (r) {
      return r.map(function (v) {
        v = v == null ? '' : String(v);
        return /[",;\n\r]/.test(v) ? '"' + v.replace(/"/g, '""') + '"' : v;
      }).join(',');
    }).join('\r\n');
    // BOM first: without it Excel reads the UTF-8 as latin-1 and mangles å ä ö.
    download(new Blob(['﻿' + rows], { type: 'text/csv;charset=utf-8;' }), this.filename('csv'));
    return this;
  };

  Tools.prototype.exportXLSX = function () {
    var self = this, aoa = this.matrix();
    return loadSheetJS(this.sheetUrl).then(function (XLSX) {
      var ws = XLSX.utils.aoa_to_sheet(aoa);
      var wb = XLSX.utils.book_new();
      XLSX.utils.book_append_sheet(wb, ws, (self.exportName || 'Data').slice(0, 28));
      XLSX.writeFile(wb, self.filename('xlsx'));
      return self;
    });
  };

  /* Observe childList only, and only on the table's direct children plus each
   * tbody: our own writes are attribute changes (row class) or nodes nested
   * inside <th>, so nothing here can re-trigger itself. */
  Tools.prototype._observe = function () {
    var self = this, queued = false;
    // setTimeout, not requestAnimationFrame: a page rebuilding its tables in a
    // background tab gets no animation frames, and the rows would stay unfiltered
    // until it was looked at again. One task hop still coalesces the burst of
    // `tbody.innerHTML +=` mutations a render loop emits.
    var mo = new MutationObserver(function () {
      if (queued) return;
      queued = true;
      setTimeout(function () {
        queued = false;
        self._rebind();
        self.refresh();
      }, 0);
    });
    this._mo = mo;
    this._rebind = function () {
      mo.disconnect();
      mo.observe(self.table, { childList: true });        // tbody/thead swapped wholesale
      var b = self.table.tBodies;
      for (var i = 0; i < b.length; i++) mo.observe(b[i], { childList: true });
    };
    this._rebind();
  };

  Tools.prototype.destroy = function () {
    if (this._mo) this._mo.disconnect();
    if (panelOwner && panelOwner.inst === this) closePanel();
    this._headerCells().forEach(function (h) {
      var b = h.th.querySelector(':scope > .tt-fbtn');
      if (b) b.remove();
      h.th.classList.remove('tt-th-active');
    });
    this._bodyRows().forEach(function (r) { r.classList.remove('tt-hidden'); });
    this._unwrap();
    if (this.bar) this.bar.remove();
    registry = registry.filter(function (e) { return e.inst !== this; }, this);
  };

  /* ── Public API ─────────────────────────────────────────────────────────── */

  function resolve(t) {
    if (!t) return null;
    if (t instanceof Tools) return t;
    if (typeof t === 'string') t = document.querySelector(t);
    for (var i = 0; i < registry.length; i++) if (registry[i].table === t) return registry[i].inst;
    return null;
  }

  var API = {
    enhance: function (table, opts) {
      if (typeof table === 'string') table = document.querySelector(table);
      if (!table || table.tagName !== 'TABLE') return null;
      var existing = resolve(table);
      if (existing) { existing.refresh(); return existing; }   // enhance() is safe to re-call
      return new Tools(table, opts);
    },
    enhanceAll: function (root, opts) {
      var out = [];
      (root || document).querySelectorAll('table[data-tools]').forEach(function (t) {
        var i = API.enhance(t, opts);
        if (i) out.push(i);
      });
      return out;
    },
    get: resolve,
    refresh: function (table) { var i = resolve(table); return i ? i.refresh() : null; },
    refreshAll: function () { registry.forEach(function (e) { e.inst.refresh(); }); },
    clearFilters: function (table) { var i = resolve(table); return i ? i.clearFilters() : null; },
    exportCSV: function (table) { var i = resolve(table); return i ? i.exportCSV() : null; },
    exportXLSX: function (table) { var i = resolve(table); return i ? i.exportXLSX() : null; },
    visibleRows: function (table) { var i = resolve(table); return i ? i.visibleRows() : []; },
    parseNumber: parseNum,
    cellText: cellText,
    instances: function () { return registry.map(function (e) { return e.inst; }); },
    version: '1.0.0'
  };

  window.TableTools = API;

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () { API.enhanceAll(); });
  } else {
    API.enhanceAll();
  }
})();
