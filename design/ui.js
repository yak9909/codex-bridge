// Puts the width of a thin classic scrollbar into --sbw, so that a scrolling area can take the bar out of its right inset
// and keep the same inset on both sides (ui.css ".scroll"; spec 0.4). Overlay scrollbars measure 0 and change nothing.
(function () {
  const probe = document.createElement("div");
  probe.className = "scroll";
  probe.style.cssText = "position:absolute;top:-999px;width:100px;height:50px;overflow:scroll;";
  document.documentElement.appendChild(probe);
  document.documentElement.style.setProperty("--sbw", probe.offsetWidth - probe.clientWidth + "px");
  probe.remove();
})();
