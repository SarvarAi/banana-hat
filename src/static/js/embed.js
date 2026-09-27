// Banana Hat gallery widget.
//
//   <div data-bananahat-gallery="https://portal.example.org/embed/open-jam/?limit=6"></div>
//   <script src="https://portal.example.org/static/js/embed.js" async></script>
//
// Replaces each placeholder with a sandboxed iframe that resizes itself to fit. The
// frame runs on the portal's origin, so the host page gets no access to it and it gets
// none to the host page.
(function () {
  "use strict";
  var frames = [];
  document.querySelectorAll("[data-bananahat-gallery]").forEach(function (el) {
    var src = el.getAttribute("data-bananahat-gallery");
    if (!src || el.dataset.bananahatReady) return;
    var frame = document.createElement("iframe");
    frame.src = src;
    frame.title = el.getAttribute("data-title") || "Project gallery";
    frame.loading = "lazy";
    frame.setAttribute("sandbox", "allow-scripts allow-popups allow-popups-to-escape-sandbox allow-same-origin");
    frame.style.cssText = "width:100%;border:0;min-height:240px;display:block";
    el.appendChild(frame);
    el.dataset.bananahatReady = "1";
    frames.push(frame);
  });
  window.addEventListener("message", function (ev) {
    var data = ev.data || {};
    if (data.type !== "bananahat:height") return;
    frames.forEach(function (frame) {
      if (frame.contentWindow === ev.source) frame.style.height = Math.min(data.height, 20000) + "px";
    });
  });
})();
