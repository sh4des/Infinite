/*
 * errors.js — shared client-side error handling for infinite-jukebox.
 *
 *  - Every error (and structured debug event) is written to the browser
 *    console / developer tools with an [infinite-jukebox] prefix.
 *  - Uncaught errors and unhandled promise rejections are captured globally.
 *  - CRITICAL errors are ALSO shown on the page itself, in a dismissible banner,
 *    so you don't need devtools open to notice something broke.
 *
 * Public API (on window):
 *    ijLog(...args)            -> console.debug
 *    ijInfo(...args)           -> console.info
 *    ijWarn(...args)           -> console.warn
 *    ijError(msg, opts)        -> console.error; opts.critical shows the banner
 */
(function () {
  "use strict";
  var TAG = "[infinite-jukebox]";

  function stamp() {
    try { return new Date().toISOString().substr(11, 12); } catch (e) { return ""; }
  }

  window.ijLog = function () {
    console.debug.apply(console, [TAG, stamp()].concat([].slice.call(arguments)));
  };
  window.ijInfo = function () {
    console.info.apply(console, [TAG, stamp()].concat([].slice.call(arguments)));
  };
  window.ijWarn = function () {
    console.warn.apply(console, [TAG, stamp()].concat([].slice.call(arguments)));
  };

  function banner() {
    var b = document.getElementById("ij-errbar");
    if (!b) {
      b = document.createElement("div");
      b.id = "ij-errbar";
      b.className = "errbar";
      b.hidden = true;
      b.innerHTML = '<span class="errbar-msg"></span><button class="errbar-x" aria-label="dismiss">✕</button>';
      var attach = function () {
        document.body.appendChild(b);
        b.querySelector(".errbar-x").onclick = function () { b.hidden = true; };
      };
      if (document.body) attach();
      else document.addEventListener("DOMContentLoaded", attach);
    }
    return b;
  }

  function showBanner(text) {
    var b = banner();
    var show = function () {
      b.querySelector(".errbar-msg").textContent = "⚠ " + text;
      b.hidden = false;
    };
    if (document.body) show();
    else document.addEventListener("DOMContentLoaded", show);
  }

  window.ijError = function (msg, opts) {
    opts = opts || {};
    console.error.apply(console, [TAG, stamp(), "ERROR:", msg].concat(opts.detail ? [opts.detail] : []));
    if (opts.critical) showBanner(typeof msg === "string" ? msg : String(msg));
  };

  window.addEventListener("error", function (e) {
    var where = e.filename ? " (" + e.filename + ":" + e.lineno + ":" + e.colno + ")" : "";
    console.error(TAG, stamp(), "UNCAUGHT:", e.message + where, e.error || "");
    showBanner("Something broke: " + e.message);
  });

  window.addEventListener("unhandledrejection", function (e) {
    var reason = e.reason && e.reason.message ? e.reason.message : e.reason;
    console.error(TAG, stamp(), "UNHANDLED PROMISE REJECTION:", reason, e.reason || "");
    showBanner("Something broke: " + reason);
  });

  window.ijInfo("client error handling ready");
})();
