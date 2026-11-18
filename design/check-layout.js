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
    const TYPE = new Set([10, 12, 14, 16]); // 10: meter helper only (spec 0.4)
    document.querySelectorAll("body *").forEach((e) => {
      if (parseFloat(getComputedStyle(e).fontSize) === 10 && !e.closest(".m-help, .mock-tab, svg") && e.offsetParent !== null
        && [...e.childNodes].some((n) => n.nodeType === 3 && n.textContent.trim())) out.push(`10px outside a meter helper: ${e.className}`);
    });
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
      // the right inset of a scrolling area is the inset minus the scrollbar width (ui.css .scroll), so it is off the scale on purpose
      if (e.matches(".scroll, .scroll > li")) props.splice(props.indexOf("paddingRight"), 1);
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
      const kinds = [".ent-head", ".ent-sec", ".set", ".d-head", ".items li", ".block", ".pane-body", ".composer", ".limits"];
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
      // meter helper: from its own bar (bar bottom) and to the next meter (next label top)
      const own = new Set(), next = new Set();
      document.querySelectorAll(".m-help").forEach((h) => {
        const bar = h.previousElementSibling?.previousElementSibling, r = h.getBoundingClientRect();
        if (bar?.classList.contains("m-bar")) own.add(Math.round(r.top - bar.getBoundingClientRect().bottom));
        const nl = h.nextElementSibling;
        if (nl?.classList.contains("m-label")) next.add(Math.round(nl.getBoundingClientRect().top - (r.top + 14)));
      });
      rep.push(`meter helper: bar-to-text ${[...own].join(",")} / text-to-next ${[...next].join(",")}`);
      // scrolling areas: left and right space between the area's border box and its content (including the scrollbar)
      const lr = new Set();
      document.querySelectorAll(".scroll").forEach((s) => {
        const kids = [...s.children].filter((c) => c.offsetParent !== null);
        if (!kids.length || s.clientHeight === 0) return;
        const r = s.getBoundingClientRect(), L = Math.min(...kids.map((c) => c.getBoundingClientRect().left)), R = Math.max(...kids.map((c) => c.getBoundingClientRect().right));
        lr.add(`${s.className.split(" ")[0]} ${Math.round(L - r.left)}/${Math.round(r.right - R)}${s.scrollHeight > s.clientHeight ? "(bar)" : ""}`);
      });
      rep.push(`scroll L/R: ${[...lr].join(", ")}`);
      document.body.setAttribute("data-gaps", rep.join(" | "));
    }
  });
})();
