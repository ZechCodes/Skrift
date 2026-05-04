// Bot detection JS challenge.
//
// Collects a small set of automation indicators in the user's browser,
// posts them to /_bot/verify with a per-page-render signed token, and
// lets the server decide pass/fail. The server does not trust the
// values directly — it derives the verdict from them — so a tampered
// payload only earns the client a "fail" record.
//
// Read the data-* attributes on this script tag for the token. Fail
// closed: if anything throws, just bail rather than reporting bogus
// data.
(function () {
  "use strict";

  var script = document.currentScript;
  if (!script) {
    return;
  }
  var token = script.getAttribute("data-token") || "";
  var signature = script.getAttribute("data-signature") || "";
  if (!token || !signature) {
    return;
  }

  function safe(fn, fallback) {
    try {
      return fn();
    } catch (_e) {
      return fallback;
    }
  }

  var indicators = {
    token: token,
    signature: signature,
    webdriver: safe(function () { return navigator.webdriver === true; }, false),
    plugins: safe(function () { return navigator.plugins ? navigator.plugins.length : 0; }, 0),
    languages: safe(function () { return (navigator.languages || []).length; }, 0),
    chrome: safe(function () { return "chrome" in window; }, false),
    canvas: safe(function () {
      var c = document.createElement("canvas");
      var ctx = c.getContext("2d");
      if (!ctx) return -1;
      ctx.fillStyle = "#abc";
      ctx.fillText("bd", 1, 1);
      return c.toDataURL().length;
    }, -1),
    timing: safe(function () { return Math.round(performance.now()); }, 0),
  };

  fetch("/_bot/verify", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(indicators),
    credentials: "same-origin",
    mode: "same-origin",
  }).catch(function () { /* swallow */ });
})();
