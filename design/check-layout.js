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
    document.body.setAttribute("data-check", out.length ? out.join(" | ") : "OK");
  });
})();
