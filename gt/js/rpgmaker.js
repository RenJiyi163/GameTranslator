/* In-page runtime for RPG Maker MV / MZ.
 *
 * MV and MZ draw text into a canvas, so there is nothing to scrape and
 * nothing to swap in the DOM. What they do have is a small set of choke
 * points every string passes through before it becomes pixels. Hooking those
 * is exact: no OCR, no guessing about layout, and the engine keeps drawing
 * with its own fonts and word wrapping.
 *
 *   Window_Base.drawTextEx   choices, name box, help text, item descriptions
 *   Window_Base.drawText     menu commands, plain labels
 *   Game_Message.add         dialogue lines (drawn character by character and
 *                            never through drawTextEx)
 *
 * Contract with the host process, same as runtime.js:
 *   window.__gt_report(json)   binding installed via Runtime.addBinding
 *   window.__GT.apply(json)    [{id, text}] -> install translations
 *   window.__GT.setMode(m)     "translated" | "original"
 *   window.__GT.restore()      drop translations, redraw in the source language
 *   window.__GT.stats()        counters for the CLI
 */
(function () {
  "use strict";

  if (window.__GT && typeof window.__GT.dispose === "function") {
    try { window.__GT.dispose(); } catch (e) { /* previous session is dead */ }
    delete window.__GT;
  }

  var CONFIG = /*__GT_CONFIG__*/ null;
  if (!CONFIG) {
    return;
  }
  var REPORT = CONFIG.reportName || "__gt_report";
  var RESTART_MESSAGE = CONFIG.restartMessage !== false;
  // Strings that change on every draw (clock, money, save timestamps) would
  // each cost a backend call and never match again. The profile decides what
  // "noise" looks like for that game.
  var SKIP_PATTERNS = (CONFIG.skipPatterns || []).map(function (p) {
    try { return new RegExp(p); } catch (e) { return null; }
  }).filter(Boolean);

  // Must stay byte-for-byte equivalent to gt/text.py:normalize, because the
  // pipeline keys its cache and memory on that form.
  function norm(text) {
    if (!text) return "";
    var s = String(text);
    if (s.normalize) s = s.normalize("NFC");
    return s.replace(/\s+/g, " ").trim();
  }

  var map = Object.create(null);      // normalized source -> translation
  var reported = Object.create(null); // keys already handed to the host
  var pending = Object.create(null);
  var flushTimer = null;
  var installed = false;
  var unhook = [];

  var stats = {
    seen: 0,
    reported: 0,
    applied: 0,
    lookups: 0,
    hits: 0
  };

  function report(key, text, kind, where) {
    if (reported[key] || pending[key]) return;
    // Same rule as gt/text.py:is_translatable, Unicode-aware: Japanese and
    // Chinese sources must survive this filter too.
    if (!/[\p{L}]/u.test(key)) return;
    for (var i = 0; i < SKIP_PATTERNS.length; i++) {
      if (SKIP_PATTERNS[i].test(key)) return;
    }
    pending[key] = { id: key, text: text, kind: kind, where: where || "" };
    stats.reported++;
    if (flushTimer) return;
    flushTimer = setTimeout(flush, 200);
  }

  function flush() {
    flushTimer = null;
    var items = [];
    for (var k in pending) {
      reported[k] = true;
      items.push(pending[k]);
    }
    pending = Object.create(null);
    if (!items.length) return;
    var fn = window[REPORT];
    if (typeof fn !== "function") {
      // The binding dies with the CDP session; drop the marks so a later
      // session (with a fresh binding name) reports them again.
      for (var i = 0; i < items.length; i++) delete reported[items[i].id];
      return;
    }
    try {
      fn(JSON.stringify(items));
    } catch (e) { /* session gone */ }
  }

  function translate(text, kind, where) {
    var raw = text == null ? "" : String(text);
    var key = norm(raw);
    if (!key) return raw;
    stats.lookups++;
    if (stats.seen < 100000 && !(key in map)) stats.seen++;
    if (state.mode === "translated" && key in map) {
      stats.hits++;
      return map[key];
    }
    report(key, raw, kind, where);
    return raw;
  }

  var state = { mode: "translated" };

  // -- hooks -------------------------------------------------------------

  function hook(target, name, factory) {
    if (!target || typeof target[name] !== "function" || target[name].__gt) return;
    var original = target[name];
    var wrapped = factory(original);
    wrapped.__gt = true;
    target[name] = wrapped;
    unhook.push(function () {
      if (target[name] === wrapped) target[name] = original;
    });
  }

  function install() {
    if (installed) return true;
    if (typeof Window_Base === "undefined" || typeof Game_Message === "undefined") {
      return false;
    }

    hook(Window_Base.prototype, "drawTextEx", function (original) {
      return function (text, x, y, width) {
        return original.call(this, translate(text, "body", "drawTextEx"), x, y, width);
      };
    });

    hook(Window_Base.prototype, "drawText", function (original) {
      return function (text, x, y, maxWidth, align) {
        return original.call(this, translate(text, "label", "drawText"), x, y, maxWidth, align);
      };
    });

    // Dialogue arrives one line at a time and is rendered a character at a
    // time by Window_Message, so it never passes through drawTextEx.
    hook(Game_Message.prototype, "add", function (original) {
      return function (text) {
        return original.call(this, translate(text, "message", "Game_Message.add"));
      };
    });

    installed = true;
    return true;
  }

  // -- applying translations --------------------------------------------

  function messageWindow() {
    var windows = windowList();
    for (var i = 0; i < windows.length; i++) {
      var child = windows[i];
      if (child && child._textState !== undefined && typeof child.startMessage === "function") {
        return child;
      }
    }
    return null;
  }

  function rewriteMessageTexts() {
    var msg = window.$gameMessage;
    if (!msg || !msg._texts) return false;
    // Switching back to the source language restores the rows and then asks
    // for a redraw through restartMessage(); rewriting here would put the
    // translation straight back and the toggle would look dead.
    if (state.mode === "original") return false;
    var changed = false;
    if (!msg._gtOrig) msg._gtOrig = msg._texts.slice();
    for (var i = 0; i < msg._texts.length; i++) {
      var key = norm(msg._texts[i]);
      if (key in map && msg._texts[i] !== map[key]) {
        msg._texts[i] = map[key];
        changed = true;
      }
    }
    return changed;
  }

  function restoreMessageTexts() {
    var msg = window.$gameMessage;
    if (!msg || !msg._gtOrig) return false;
    var changed = msg._texts.join("\u0000") !== msg._gtOrig.join("\u0000");
    msg._texts = msg._gtOrig.slice();
    msg._gtOrig = null;
    return changed;
  }

  // Windows live in the scene's window layer, not in the scene itself: a
  // scene's own children are the background sprite and that layer, so
  // iterating scene.children finds nothing to refresh.
  function windowList() {
    var scene = window.SceneManager && SceneManager._scene;
    if (!scene) return [];
    if (scene._windowLayer && scene._windowLayer.children) return scene._windowLayer.children;
    return scene.children || [];
  }

  function refreshWindows() {
    var windows = windowList();
    if (!windows.length) return 0;
    var touched = 0;
    for (var i = 0; i < windows.length; i++) {
      var w = windows[i];
      if (!w) continue;
      // Message windows cannot be refreshed into new text: their progress
      // lives in _textState and refresh() only repaints the pixels already
      // drawn. They are restarted below instead.
      if (typeof w.startMessage === "function" && w._textState !== undefined) continue;
      try {
        if (typeof w.refresh === "function") {
          w.refresh();
        } else if (typeof w.setText === "function" && typeof w._text === "string") {
          // Window_Help and friends are drawn once by setText and have no
          // refresh of their own; re-setting the same string redraws it, and
          // the draw hook then picks up the translation.
          w.setText(w._text);
        } else {
          continue;
        }
        touched++;
      } catch (e) { /* window mid-transition */ }
    }
    return touched;
  }

  function restartMessage(force) {
    if (!RESTART_MESSAGE) return false;
    var win = messageWindow();
    if (!win) return false;
    // Restarting into a closing window leaves an empty box with a wait
    // arrow: startMessage() re-reads a message that has already been cleared.
    if (typeof win.isClosing === "function" && win.isClosing()) return false;
    // Once a page has finished drawing, _textState is null again while the
    // message is still on screen waiting for the player. A translation that
    // arrives at that point still has to land, so the state check cannot be
    // the gate. A choice window is the exception: reprinting the page under
    // an open choice desynchronises the selection, so leave it alone.
    if (!win._textState) {
      if (typeof win.isAnySubWindowActive === "function" && win.isAnySubWindowActive()) return false;
      if (!win.visible || !win.openness) return false;
    }
    var msg = window.$gameMessage;
    if (!msg || typeof msg.allText !== "function" || !msg.allText()) return false;
    if (!rewriteMessageTexts() && !force) return false;
    try {
      win._textState = null;
      win.startMessage();
      return true;
    } catch (e) {
      return false;
    }
  }

  // -- public surface ----------------------------------------------------

  window.__GT = {
    version: "1",

    // The host sends a JSON string, same as runtime.js; a parsed array is
    // accepted too so the surface is usable from a console.
    apply: function (payload) {
      var items = payload;
      if (typeof payload === "string") {
        try { items = JSON.parse(payload); } catch (e) { return 0; }
      }
      if (!items || !items.length) return 0;
      var n = 0;
      for (var i = 0; i < items.length; i++) {
        var item = items[i];
        if (!item || item.id == null || typeof item.text !== "string") continue;
        map[item.id] = item.text;
        n++;
      }
      stats.applied += n;
      restartMessage(false);
      refreshWindows();
      return n;
    },

    setMode: function (mode) {
      state.mode = mode === "original" ? "original" : "translated";
      if (state.mode === "original") {
        restoreMessageTexts();
      } else {
        rewriteMessageTexts();
      }
      restartMessage(true);
      refreshWindows();
      return state.mode;
    },

    restore: function () {
      state.mode = "original";
      if (installed) {
        restoreMessageTexts();
        restartMessage(true);
        refreshWindows();
      }
      return true;
    },

    stats: function () {
      return {
        mode: state.mode,
        installed: installed,
        keys: Object.keys(map).length,
        seen: stats.seen,
        reported: stats.reported,
        applied: stats.applied,
        lookups: stats.lookups,
        hits: stats.hits,
        messageRows: (window.$gameMessage && $gameMessage._texts && $gameMessage._texts.length) || 0
      };
    },

    rescan: function () {
      var scene = window.SceneManager && SceneManager._scene;
      if (scene && typeof scene.refresh === "function") {
        try { scene.refresh(); } catch (e) { /* ignore */ }
      }
      refreshWindows();
      return true;
    },

    dispose: function () {
      for (var i = 0; i < unhook.length; i++) {
        try { unhook[i](); } catch (e) { /* ignore */ }
      }
      unhook = [];
      installed = false;
      if (flushTimer) {
        clearTimeout(flushTimer);
        flushTimer = null;
      }
    }
  };

  // The game boots asynchronously; keep trying until the engine globals exist.
  if (!install()) {
    var tries = 0;
    var timer = setInterval(function () {
      tries++;
      if (install() || tries > 600) clearInterval(timer);
    }, 100);
  }
})();
