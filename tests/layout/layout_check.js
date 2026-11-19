// Run in the page: every problem that hides content from the user, as a list of strings (empty = fine).
// 1. at 721px and wider the page itself must not scroll (only the panes do) and nothing is wider than the window;
// 2. no element clips its content without a way to scroll to it (one-line ellipsis and form controls are fine);
// 3. no scrolling region that has content is squeezed so small that it cannot be read.
// 4. approvals must leave room for the work below; scrolling the whole strip can hide the decision controls.
(() => {
  const out = [];
  const doc = document.scrollingElement;
  const name = (e) => e.tagName.toLowerCase() + (e.className && typeof e.className === "string" ? "." + e.className.trim().split(/\s+/).join(".") : "")
    + (e.dataset && e.dataset.slot ? `[${e.dataset.slot}]` : "");
  if (innerWidth >= 721 && doc.scrollHeight > innerHeight + 1) out.push(`page scrolls (${doc.scrollHeight} > ${innerHeight})`);
  if (doc.scrollWidth > innerWidth + 1) out.push(`page is wider than the window (${doc.scrollWidth} > ${innerWidth})`);
  for (const e of document.querySelectorAll("body *")) {
    if (e.closest("svg, select, textarea, input, [hidden]") || e.getClientRects().length === 0) continue;
    const cs = getComputedStyle(e);
    if (cs.visibility === "hidden" || cs.display === "contents") continue;
    const height = e.getBoundingClientRect().height;
    if (e.matches(".alerts") && height > innerHeight * 0.3 + 1) out.push(`approvals strip too tall: ${name(e)} (${height.toFixed(1)}px > 30% of ${innerHeight}px)`);
    const oneLine = cs.whiteSpace === "nowrap" || cs.textOverflow === "ellipsis";
    const clipY = cs.overflowY === "hidden" || cs.overflowY === "clip";
    const clipX = cs.overflowX === "hidden" || cs.overflowX === "clip";
    if (clipY && !oneLine && e.scrollHeight > e.clientHeight + 2) out.push(`clipped, cannot scroll: ${name(e)} (${e.scrollHeight} > ${e.clientHeight})`);
    if (clipX && !oneLine && e.scrollWidth > e.clientWidth + 2) out.push(`clipped sideways: ${name(e)} (${e.scrollWidth} > ${e.clientWidth})`);
    const scrolls = /(auto|scroll)/.test(cs.overflowY);
    if (scrolls && e.scrollHeight > e.clientHeight + 2 && e.clientHeight < 48) out.push(`scroll region squeezed: ${name(e)} (${e.clientHeight}px tall)`);
  }
  return [...new Set(out)];
})()
