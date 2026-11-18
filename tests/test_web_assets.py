"""Static checks of the page's scripts (no browser): what they export, and which server routes they name."""

import re
from pathlib import Path

from codex_bridge.host.web.app import admin_router

APP = Path(__file__).parents[1] / "src" / "codex_bridge" / "common" / "web" / "app"


def use_sources() -> dict[str, str]:
    return {p.name: p.read_text(encoding="utf-8") for p in sorted(APP.glob("use*.js"))}


def test_use_js_exports_renderUse():
    assert re.search(r"export\s+(async\s+)?function\s+renderUse\s*\(", (APP / "use.js").read_text(encoding="utf-8"))


def test_use_js_uses_only_existing_routes():
    routes = [r.path for r in admin_router(None).routes if r.path.startswith("/admin/api/")]
    assert routes
    patterns = [re.compile("^" + re.sub(r"\\\{[^}]*\\\}", "[^/]+", re.escape(p)) + "$") for p in routes]
    seen = set()
    for name, text in use_sources().items():
        for m in re.finditer(r"""["'`](/admin/api/[^"'`]*)["'`]""", text):
            path = re.sub(r"\$\{[^}]*\}", "x", m.group(1)).split("?")[0]
            seen.add(path)
            assert any(p.match(path) for p in patterns), f"{name}: no admin route for {m.group(1)}"
    assert seen, "the use tab names no /admin/api route"


def test_use_js_has_no_inline_handlers_or_scripts():
    for name, text in use_sources().items():
        assert not re.search(r"""\son[a-z]+\s*=""", text), f"{name}: inline event handler"
        assert "<script" not in text and "javascript:" not in text, f"{name}: inline script"
