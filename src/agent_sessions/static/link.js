/* QR cross-device sign-in — login-page panel (#650).
 *
 * Progressive enhancement: the password form works with JS off; this only lights up the
 * "sign in with your phone" panel. Served as an external file because the app's CSP is
 * `script-src 'self'` (no inline scripts). It POSTs /link/start, shows the server-rendered
 * QR (`/link/qr`), polls /link/status, and navigates to the app once the phone approves.
 */
(function () {
  "use strict";
  var panel = document.getElementById("qr-signin");
  if (!panel || !window.fetch) return;
  var img = document.getElementById("qr-img");
  var statusEl = document.getElementById("qr-status");
  var POLL_MS = 2000;
  var timers = [];

  function safeNext() {
    var n = panel.getAttribute("data-next") || "/";
    return n.charAt(0) === "/" && n.charAt(1) !== "/" ? n : "/";
  }

  function setStatus(text, live) {
    if (!statusEl) return;
    statusEl.textContent = text;
    statusEl.classList.toggle("live", !!live);
  }

  function mmss(secs) {
    secs = Math.max(0, secs | 0);
    var m = (secs / 60) | 0;
    var s = secs % 60;
    return m + ":" + (s < 10 ? "0" : "") + s;
  }

  function stop() {
    while (timers.length) clearInterval(timers.pop());
  }

  function terminal(text) {
    stop();
    panel.classList.add("qr-done");
    setStatus(text, false);
  }

  function poll(claim) {
    fetch("/link/status?t=" + encodeURIComponent(claim), { headers: { Accept: "application/json" } })
      .then(function (r) {
        return r.ok ? r.json() : { state: "error" };
      })
      .then(function (d) {
        if (d.state === "approved") {
          terminal("Approved — signing in…");
          window.location.assign(safeNext());
        } else if (d.state === "denied") {
          terminal("Request denied on your phone.");
        } else if (d.state === "expired" || d.state === "consumed" || d.state === "error") {
          terminal("Code expired — reload the page for a new one.");
        }
        // "pending" → keep waiting.
      })
      .catch(function () {
        /* transient network blip — keep polling */
      });
  }

  function start() {
    fetch("/link/start", { method: "POST", headers: { Accept: "application/json" } })
      .then(function (r) {
        if (!r.ok) throw new Error("start");
        return r.json();
      })
      .then(function (d) {
        if (img) img.src = d.qr_path;
        // Reveal the panel only once the server has actually issued a challenge — so with
        // JS off, or when the server declines (throttled / `none` mode 404s), nothing shows
        // and the password form stands alone.
        var extra = document.getElementById("qr-extra");
        if (extra) extra.hidden = false;
        var expiresMs = (Number(d.expires_at) || 0) * 1000;
        function tick() {
          var left = expiresMs ? Math.round((expiresMs - Date.now()) / 1000) : 1;
          if (left <= 0) {
            terminal("Code expired — reload the page for a new one.");
            return;
          }
          setStatus("Waiting for approval… " + mmss(left), true);
        }
        tick();
        timers.push(setInterval(tick, 1000));
        timers.push(
          setInterval(function () {
            poll(d.claim_token);
          }, POLL_MS),
        );
      })
      .catch(function () {
        /* server declined — leave the phone panel hidden; password login is always there */
      });
  }

  start();
})();
