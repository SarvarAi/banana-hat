// Inside the widget iframe: tell the host page how tall we are, so it can size the frame.
(function () {
  "use strict";
  function report() {
    var height = document.documentElement.scrollHeight;
    window.parent.postMessage({ type: "bananahat:height", height: height, href: location.href }, "*");
  }
  window.addEventListener("load", report);
  window.addEventListener("resize", report);
  if (window.ResizeObserver) new ResizeObserver(report).observe(document.body);
})();
