// Layout self-check for the mock pages (spec 0.4 / 0.5). Load with ?check in the URL.
// Result goes to <body data-check="...">. With ?gaps it also lists the measured gaps per component (data-gaps),
// so that "the same role has the same space" can be compared as numbers, next to looking at the screenshot.
(function () {
  const q = new URLSearchParams(location.search);
  const sc = q.get("scroll");
  if (sc) addEventListener("load", () => document.querySelectorAll(".pane-body").forEach((e) => (e.scrollTop = +sc)));
  if (!q.has("check")) return;
  addEventListener("load", () => {
    const out = [];
    const W = document.documentElement.clientWidth;
    if (document.documentElement.scrollWidth > W + 1) out.push(`page wider than window (${document.documentElement.scrollWidth} > ${W})`);

    // a scrolling pane must scroll only vertically: report what is wider than it
    document.querySelectorAll("body *").forEach((el) => {
      const cs = getComputedStyle(el);
      if (!/(auto|scroll)/.test(cs.overflowX + cs.overflowY) || el.scrollWidth <= el.clientWidth + 1) return;
      if (el.matches("textarea, .log-view, .pop, code")) return;
      const r = el.getBoundingClientRect();
      const wide = [...el.querySelectorAll("*")].filter((c) => c.getBoundingClientRect().right > r.right + 1 && c.offsetParent !== null);
      const leaf = wide.filter((c) => !wide.some((d) => d !== c && c.contains(d)))[0];
      out.push(`wider than its pane: ${el.className} by ${el.scrollWidth - el.clientWidth}px (${leaf ? leaf.tagName + " in " + (leaf.closest(".set, .ent-head, li, .row")?.textContent.trim().slice(0, 20) || "?") : "?"})`);
    });

    // type and space must come from the scale (spec 0.4)
    const TYPE = new Set([12, 14, 16]);
    const SPACE = new Set([0, 1, 2, 3, 4, 8, 12, 16, 24]); // 1-3: borders/insets of small marks only
    const badType = new Map(), badSpace = new Map();
    document.querySelectorAll("body *").forEach((e) => {
      if (e.closest(".mock-tab, svg") || e.offsetParent === null) return;
      const cs = getComputedStyle(e);
      const hasText = [...e.childNodes].some((n) => n.nodeType === 3 && n.textContent.trim());
      const fs = parseFloat(cs.fontSize);
      if (hasText && !TYPE.has(fs)) badType.set(fs, (badType.get(fs) || 0) + 1);
      const props = ["paddingTop", "paddingRight", "paddingBottom", "paddingLeft", "marginTop", "marginBottom"];
      if (/flex|grid/.test(cs.display)) props.push("rowGap", "columnGap");
      props.forEach((p) => {
        const v = parseFloat(cs[p]);
        if (!Number.isNaN(v) && !SPACE.has(Math.abs(v))) badSpace.set(`${p}=${v}`, (badSpace.get(`${p}=${v}`) || 0) + 1);
      });
    });
    if (badType.size) out.push("type off-scale: " + [...badType].map(([k, n]) => `${k}px×${n}`).join(", "));
    if (badSpace.size) out.push("space off-scale: " + [...badSpace].map(([k, n]) => `${k}×${n}`).join(", "));
    document.body.setAttribute("data-check", out.length ? out.join(" | ") : "OK");

    if (q.has("gaps")) {
      // vertical gaps between consecutive visible children, per component kind
      const kinds = [".ent-head", ".ent-sec", ".set", ".d-head", ".items li", ".block", ".pane-body"];
      const rep = kinds.map((k) => {
        const vals = new Set();
        document.querySelectorAll(k).forEach((el) => {
          const kids = [...el.children].filter((c) => c.offsetParent !== null && getComputedStyle(c).position !== "absolute");
          for (let i = 1; i < kids.length; i++) {
            const a = kids[i - 1].getBoundingClientRect(), b = kids[i].getBoundingClientRect();
            if (b.top >= a.bottom - 1) vals.add(Math.round(b.top - a.bottom));
          }
          const r = el.getBoundingClientRect(), f = kids[0]?.getBoundingClientRect(), l = kids[kids.length - 1]?.getBoundingClientRect();
          if (f) vals.add(`top${Math.round(f.top - r.top)}`);
          if (l) vals.add(`bottom${Math.round(r.bottom - l.bottom)}`);
        });
        return `${k}: ${[...vals].join(",")}`;
      });
      document.body.setAttribute("data-gaps", rep.join(" | "));
    }
  });
})();
