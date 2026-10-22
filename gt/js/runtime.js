/* Generic in-page translation runtime.
 *
 * Injected by adapters that control a browser/Electron page. Nothing here may
 * know about a specific game: selectors, skip list and hotkey all arrive in
 * CONFIG from the active profile.
 *
 * Contract with the host process (Python):
 *   window.__gt_report(json)   binding installed via Runtime.addBinding
 *   window.__GT.apply(json)    [{id, text}] -> write translation into the DOM
 *   window.__GT.setMode(m)     "translated" | "original"
 *   window.__GT.restore()      put every original back
 *   window.__GT.stats()        counters for the CLI
 *
 * Design notes:
 *   - Every text node keeps its own original, so restoring never depends on
 *     the host process still being alive.
 *   - The page never waits for the host: untranslated nodes keep the original
 *     text until a translation arrives.
 *   - Records are keyed by a monotonically increasing integer and hold a strong
 *     reference to the node; dead nodes are pruned on every scan.
 */
(function () {
  "use strict";

  // A previous run of this script may still be attached (the page outlives the
  // CDP session that injected it). Tear it down instead of reusing it: its
  // report binding belongs to a dead session and nobody is listening.
  if (window.__GT && typeof window.__GT.dispose === "function") {
    try { window.__GT.dispose(); } catch (e) { /* already broken */ }
    delete window.__GT;
  }

  var CONFIG = /*__GT_CONFIG__*/ null;
  if (!CONFIG) {
    return;
  }
  var REPORT = CONFIG.reportName || "__gt_report";

  var DEFAULT_SKIP_TAGS = ["SCRIPT", "STYLE", "NOSCRIPT", "TEXTAREA", "INPUT", "CODE", "PRE", "TITLE"];
  // An empty array in the profile means "no extra tags", not "skip nothing":
  // without this the default list would be replaced by [] and CSS text inside
  // a <style> element would be handed to the translator.
  var SKIP_TAGS = (CONFIG.skipTags && CONFIG.skipTags.length) ? CONFIG.skipTags : DEFAULT_SKIP_TAGS;
  var SKIP_SEL = (CONFIG.skipSelectors && CONFIG.skipSelectors.length)
    ? CONFIG.skipSelectors.join(",") : null;
  var HOTKEY = CONFIG.hotkey || "Ctrl+Shift+T";

  var state = {
    version: "1",
    mode: "translated",
    nextId: 1,
    records: new Map(),   // id -> {node, orig, trans, disp, kind}
    ids: new WeakMap(),   // Text -> id
    pending: new Set(),   // ids waiting to be reported
    translated: 0,
    reported: 0,
    applied: 0
  };

  var scanTimer = null;
  var flushTimer = null;
  var ticker = null;
  var observer = null;
  var lastScan = 0;

  function isSkipped(node) {
    var el = node.parentElement;
    if (!el) return true;
    if (SKIP_TAGS.indexOf(el.tagName) >= 0) return true;
    if (el.isContentEditable) return true;
    if (SKIP_SEL) {
      try {
        // Ancestors matter: the hotkey badge is a <div> inside the button we
        // matched, so testing only the matched element would miss it.
        if (el.closest(SKIP_SEL)) return true;
      } catch (e) { /* bad selector in profile: ignore */ }
    }
    return false;
  }

  function describe(el) {
    if (!el) return "";
    var s = el.tagName.toLowerCase();
    if (el.id) s += "#" + el.id;
    if (el.classList && el.classList.length) s += "." + el.classList[0];
    return s;
  }

  function noteNode(textNode, kind, where) {
    var id = state.ids.get(textNode);
    if (id === undefined) {
      id = state.nextId++;
      state.ids.set(textNode, id);
      state.records.set(id, {
        node: textNode,
        orig: textNode.nodeValue,
        trans: null,
        disp: textNode.nodeValue,
        kind: kind,
        where: where
      });
      state.pending.add(id);
      return;
    }

    var rec = state.records.get(id);
    if (!rec) {
      // id recycled from a WeakMap entry whose record was pruned
      state.ids.delete(textNode);
      noteNode(textNode, kind, where);
      return;
    }
    var value = textNode.nodeValue;
    if (value === rec.disp) return;          // unchanged since our last write
    if (value === rec.orig) return;          // host put the original back
    // The game rewrote this node (re-render, state change). Treat as new text.
    rec.orig = value;
    rec.trans = null;
    rec.disp = value;
    rec.kind = kind;
    rec.where = where;
    state.pending.add(id);
  }

  function walk(el, kind) {
    if (!el) return;
    if (SKIP_SEL) {
      try {
        if (el.closest(SKIP_SEL)) return;
      } catch (e) { /* bad selector in profile: ignore */ }
    }
    var walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT, null);
    var node;
    var where = describe(el);
    while ((node = walker.nextNode())) {
      var value = node.nodeValue;
      if (!value || !value.replace(/\s+/g, "")) continue;
      if (isSkipped(node)) continue;
      noteNode(node, kind, where);
    }
  }

  function prune() {
    state.records.forEach(function (rec, id) {
      if (!rec.node.isConnected) {
        state.records.delete(id);
        state.pending.delete(id);
      }
    });
  }

  function scan() {
    var start = Date.now();
    prune();
    for (var i = 0; i < CONFIG.selectors.length; i++) {
      var sel = CONFIG.selectors[i];
      var nodes;
      try {
        nodes = document.querySelectorAll(sel.css);
      } catch (e) {
        continue;
      }
      for (var j = 0; j < nodes.length; j++) {
        walk(nodes[j], sel.kind || "text");
      }
    }
    lastScan = Date.now();
    state.scanned = state.records.size;
    return Date.now() - start;
  }

  function flush() {
    if (flushTimer) {
      clearTimeout(flushTimer);
      flushTimer = null;
    }
    if (!state.pending.size || typeof window[REPORT] !== "function") return;
    var out = [];
    state.pending.forEach(function (id) {
      var rec = state.records.get(id);
      if (!rec) return;
      if (!rec.node.isConnected) return;
      out.push({ id: id, text: rec.orig, kind: rec.kind, where: rec.where });
    });
    state.pending.clear();
    if (!out.length) return;
    state.reported += out.length;
    try {
      window[REPORT](JSON.stringify(out));
    } catch (e) {
      // Host gone: keep the originals, retry on the next scan.
      for (var k = 0; k < out.length; k++) state.pending.add(out[k].id);
    }
  }

  function scheduleScan() {
    if (scanTimer) return;
    scanTimer = setTimeout(function () {
      scanTimer = null;
      scan();
      flush();
    }, 60);
  }

  function paint(rec) {
    if (!rec.node.isConnected) return;
    var value = (state.mode === "translated" && rec.trans !== null) ? rec.trans : rec.orig;
    if (rec.node.nodeValue === value) return;
    rec.disp = value;
    rec.node.nodeValue = value;
  }

  function setMode(mode) {
    state.mode = (mode === "original") ? "original" : "translated";
    state.records.forEach(paint);
    return state.mode;
  }

  function apply(payload) {
    var items;
    try {
      items = JSON.parse(payload);
    } catch (e) {
      return 0;
    }
    var n = 0;
    for (var i = 0; i < items.length; i++) {
      var rec = state.records.get(items[i].id);
      if (!rec) continue;
      rec.trans = items[i].text;
      paint(rec);
      n++;
    }
    state.applied += n;
    return n;
  }

  function restore() {
    state.mode = "original";
    state.records.forEach(function (rec) {
      if (!rec.node.isConnected) return;
      if (rec.node.nodeValue === rec.orig) return;
      rec.node.nodeValue = rec.orig;
      rec.disp = rec.orig;
    });
    return state.records.size;
  }

  function matchHotkey(event) {
    var parts = String(HOTKEY).toLowerCase().split("+");
    var key = parts.pop();
    if (key === "space") key = " ";
    if ((event.key || "").toLowerCase() !== key) return false;
    return !!event.ctrlKey === (parts.indexOf("ctrl") >= 0) &&
      !!event.shiftKey === (parts.indexOf("shift") >= 0) &&
      !!event.altKey === (parts.indexOf("alt") >= 0) &&
      !!event.metaKey === (parts.indexOf("meta") >= 0);
  }

  document.addEventListener("keydown", function (event) {
    if (!matchHotkey(event)) return;
    var tag = event.target && event.target.tagName;
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
    event.preventDefault();
    event.stopPropagation();
    setMode(state.mode === "translated" ? "original" : "translated");
  }, true);

  observer = new MutationObserver(function (mutations) {
    for (var i = 0; i < mutations.length; i++) {
      var m = mutations[i];
      if (m.type === "characterData" && m.target && state.ids.has(m.target)) {
        // We wrote it ourselves? noteNode compares against disp/orig and skips.
        scheduleScan();
        return;
      }
    }
    scheduleScan();
  });

  window.__GT = {
    version: "1",
    apply: apply,
    setMode: setMode,
    restore: restore,
    rescan: function () { scan(); flush(); },
    dispose: function () {
      // Put the originals back before the host loses the ability to do it.
      restore();
      if (observer) { observer.disconnect(); observer = null; }
      if (ticker) { clearInterval(ticker); ticker = null; }
      if (scanTimer) { clearTimeout(scanTimer); scanTimer = null; }
      if (flushTimer) { clearTimeout(flushTimer); flushTimer = null; }
      state.records.clear();
      state.pending.clear();
    },
    stats: function () {
      var hits = 0;
      state.records.forEach(function (rec) { if (rec.trans !== null) hits++; });
      return {
        mode: state.mode,
        nodes: state.records.size,
        translated: hits,
        reported: state.reported,
        applied: state.applied,
        lastScan: lastScan
      };
    }
  };

  function boot() {
    scan();
    flush();
    observer.observe(document.documentElement, {
      childList: true,
      subtree: true,
      characterData: true
    });
    // Backstop for hosts that swap large subtrees without mutations we can see
    // (canvas-ish frameworks, innerHTML on a detached root, etc.).
    ticker = setInterval(function () { scan(); flush(); }, 3000);
    // Flush in a separate tick so a burst of mutations produces one payload.
    flushTimer = setTimeout(flush, 200);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
