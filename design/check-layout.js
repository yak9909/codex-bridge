// Layout self-check for the mock pages (spec 0.5). Load with ?check in the URL.
// Reports: page wider than the window; side-by-side blocks whose heights differ a lot
// (a low column leaves an empty area beside it). Result goes to <body data-check="...">.
(function () {
  if (!location.search.includes("check")) return;
  addEventListener("load", () => {
    const out = [];
    const W = document.documentElement.clientWidth;
    if (document.documentElement.scrollWidth > W + 1) out.push(`page wider than window (${document.documentElement.scrollWidth} > ${W})`);
    document.querySelectorAll(".split, .tasks, .lender-body").forEach((el) => {
      const kids = [...el.children].filter((c) => c.offsetParent !== null);
      if (kids.length < 2) return;
      // grid items stretch to the row height by default; measure their content instead
      const saved = el.style.alignItems;
      el.style.alignItems = "start";
      const rows = new Map();
      kids.forEach((k) => {
        const top = Math.round(k.getBoundingClientRect().top);
        rows.set(top, (rows.get(top) || []).concat(k));
      });
      rows.forEach((row) => {
        if (row.length < 2) return;
        const h = row.map((k) => k.getBoundingClientRect().height);
        const ratio = Math.min(...h) / Math.max(...h);
        if (ratio < 0.8) {
          const name = el.closest("section")?.querySelector("h2")?.textContent.trim() || el.className;
          out.push(`${name}: columns ${h.map(Math.round).join("/")}px (ratio ${ratio.toFixed(2)})`);
        }
      });
      el.style.alignItems = saved;
    });
    // type and space must come from the scale (spec 0.6)
    const TYPE = new Set([14, 16, 20]);
    const SPACE = new Set([0, 4, 8, 12, 16, 24, 32, 48]);
    const badType = new Map(), badSpace = new Map();
    document.querySelectorAll("main *, header *").forEach((e) => {
      if (e.closest(".mock-tab, svg") || e.offsetParent === null) return;
      const cs = getComputedStyle(e);
      const hasText = [...e.childNodes].some((n) => n.nodeType === 3 && n.textContent.trim());
      const fs = parseFloat(cs.fontSize);
      if (hasText && !TYPE.has(fs)) badType.set(fs, (badType.get(fs) || 0) + 1);
      const props = ["paddingTop", "paddingRight", "paddingBottom", "paddingLeft", "marginTop", "marginBottom", "marginLeft", "marginRight"];
      if (/flex|grid/.test(cs.display)) props.push("rowGap", "columnGap");
      const typed = e.computedStyleMap ? e.computedStyleMap() : null;
      props.forEach((p) => {
        // auto margins (centering, pushing to the right) are not spacing choices
        const css = p.replace(/[A-Z]/g, (c) => "-" + c.toLowerCase());
        if (p.startsWith("margin") && typed && String(typed.get(css)) === "auto") return;
        const v = parseFloat(cs[p]);
        if (!Number.isNaN(v) && !SPACE.has(Math.abs(v))) {
          const key = `${p}=${v}`;
          badSpace.set(key, (badSpace.get(key) || 0) + 1);
        }
      });
    });
    if (badType.size) out.push("type off-scale: " + [...badType].map(([k, n]) => `${k}px×${n}`).join(", "));
    if (badSpace.size) out.push("space off-scale: " + [...badSpace].map(([k, n]) => `${k}×${n}`).join(", "));
    document.body.setAttribute("data-check", out.length ? out.join(" | ") : "OK");
  });
})();
