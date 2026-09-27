// Small progressive enhancements. Every page works without this file; it only adds
// conveniences. There is no inline JavaScript anywhere, which is what lets the
// Content-Security-Policy forbid it.
(function () {
  "use strict";

  // Invite links and tokens: select the whole value on focus so it can be copied.
  document.querySelectorAll(".select-on-focus").forEach(function (el) {
    el.addEventListener("focus", function () { el.select(); });
  });

  // Deadline countdowns. The server enforces the deadline; this only tells you.
  function fmt(ms) {
    var s = Math.max(0, Math.floor(ms / 1000));
    var d = Math.floor(s / 86400); s -= d * 86400;
    var h = Math.floor(s / 3600); s -= h * 3600;
    var m = Math.floor(s / 60); s -= m * 60;
    var parts = [];
    if (d) parts.push(d + "d");
    if (d || h) parts.push(h + "h");
    parts.push(m + "m");
    if (!d) parts.push(s + "s");
    return parts.join(" ");
  }
  document.querySelectorAll(".countdown[data-deadline]").forEach(function (el) {
    var deadline = Date.parse(el.dataset.deadline);
    if (isNaN(deadline)) return;
    function tick() {
      var left = deadline - Date.now();
      el.textContent = left > 0 ? fmt(left) + " left" : "Deadline passed. Reload to see the locked page.";
      if (left > 0) setTimeout(tick, 1000);
    }
    tick();
  });

  // Confirm destructive buttons marked with data-confirm.
  document.querySelectorAll("[data-confirm]").forEach(function (el) {
    el.addEventListener("click", function (ev) {
      if (!window.confirm(el.dataset.confirm)) ev.preventDefault();
    });
  });

  // The scorecard: show the weighted total as the judge marks cells.
  document.querySelectorAll("form.scorecard-form").forEach(function (form) {
    var out = form.querySelector("output[data-total]");
    if (!out) return;
    function recompute() {
      var sum = 0, weights = 0, complete = true;
      form.querySelectorAll("[data-criterion]").forEach(function (row) {
        var w = parseFloat(row.dataset.weight);
        var checked = row.querySelector("input:checked");
        weights += w;
        if (checked) sum += w * parseFloat(checked.value); else complete = false;
      });
      out.textContent = complete && weights ? (sum / weights).toFixed(2) : "–";
    }
    form.addEventListener("change", recompute);
    recompute();
  });

  // Live panels: re-fetch a fragment every few seconds and swap it in.
  document.querySelectorAll("[data-live-url]").forEach(function (el) {
    var url = el.dataset.liveUrl;
    var every = parseInt(el.dataset.liveEvery || "5", 10) * 1000;
    var stamp = document.querySelector("[data-live-stamp]");
    function refresh() {
      if (document.hidden) return setTimeout(refresh, every);
      fetch(url, { headers: { "X-Requested-With": "fetch" }, credentials: "same-origin" })
        .then(function (r) { return r.ok ? r.text() : Promise.reject(r.status); })
        .then(function (html) {
          el.innerHTML = html;
          if (stamp) stamp.textContent = "updated " + new Date().toLocaleTimeString();
        })
        .catch(function () { if (stamp) stamp.textContent = "connection lost, retrying"; })
        .finally(function () { setTimeout(refresh, every); });
    }
    setTimeout(refresh, every);
  });
})();
