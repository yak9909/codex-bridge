"""Real Chrome layout checks against extreme data, without touching the installed app.

Set CODEX_BRIDGE_CHROME to override Chrome, and CODEX_BRIDGE_LAYOUT_SHOTS to
save both tabs and expanded cards at desktop and narrow widths, at 2x.
"""

from __future__ import annotations

import asyncio
import base64
from concurrent.futures import Future
import json
import os
from pathlib import Path
import socket
import subprocess
import threading
import time
from urllib.request import urlopen

import pytest
import uvicorn
from websockets.asyncio.client import connect

from codex_bridge.resident import create_resident_app
from layout.stress_app import build

CHROME = Path(os.environ.get("CODEX_BRIDGE_CHROME", "C:/Program Files/Google/Chrome/Application/chrome.exe"))
if not CHROME.is_file():
    pytest.skip(f"Chrome is missing: {CHROME}; set CODEX_BRIDGE_CHROME to run browser layout checks", allow_module_level=True)

CHECK = (Path(__file__).parent / "layout" / "layout_check.js").read_text(encoding="utf-8")


class Page:
    """Only sequential CDP commands; unsolicited events are consumed, not awaited."""

    def __init__(self, ws):
        self.ws, self.seq = ws, 0

    async def call(self, method, **params):
        self.seq += 1
        await self.ws.send(json.dumps({"id": self.seq, "method": method, "params": params}))
        async with asyncio.timeout(15):
            while True:
                reply = json.loads(await self.ws.recv())
                if reply.get("id") == self.seq:
                    assert "error" not in reply, reply
                    return reply.get("result", {})

    async def js(self, expression):
        reply = await self.call("Runtime.evaluate", expression=expression, returnByValue=True, awaitPromise=True)
        assert "exceptionDetails" not in reply, reply
        return reply["result"].get("value")

    async def wait(self, expression):
        deadline = time.monotonic() + 10
        while not await self.js(f"Boolean({expression})"):
            if time.monotonic() >= deadline:
                text = await self.js("JSON.stringify({url:location.href, text:document.body.innerText.slice(0,4000), lenders:document.querySelectorAll('[data-slot=lenders] .ent').length, tasks:document.querySelectorAll('.items li').length, events:document.querySelectorAll('.d-log .tl li').length})")
                pytest.fail(f"page did not render: {expression}\n{text}")
            await asyncio.sleep(0.025)  # Real time: virtual time stalls the open SSE requests.
        await self.js("document.fonts.ready.then(() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r))))")

    async def click(self, selector):
        await self.wait(f"document.querySelector({json.dumps(selector)})")
        await self.js(f"document.querySelector({json.dumps(selector)}).click()")

    async def shot(self, side, selector=None, *, delay=0.1):
        if folder := os.environ.get("CODEX_BRIDGE_LAYOUT_SHOTS"):
            path = Path(folder)
            path.mkdir(parents=True, exist_ok=True)
            await self.js("document.querySelector('.d-req').scrollTop = 0; document.querySelector('.d-log').scrollTop = 0")
            if selector:
                await self.js(f"document.querySelector({json.dumps(selector)}).scrollIntoView({{block: \'start\'}})")
            else:
                await self.js("window.scrollTo(0, 0)")
            await asyncio.sleep(delay)
            result = await self.call("Page.captureScreenshot", format="png", captureBeyondViewport=False)
            (path / f"stress-{side}-{self.width}px-2x.png").write_bytes(base64.b64decode(result["data"]))


@pytest.fixture(scope="module")
def stress_server(tmp_path_factory):
    folder = tmp_path_factory.mktemp("layout-server")
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    ready = Future()

    async def serve():
        try:
            app = create_resident_app(await build(folder, port))
            server = uvicorn.Server(uvicorn.Config(app, log_level="error", timeout_graceful_shutdown=1))
            ready.set_result(server)
            await server.serve(sockets=[sock])
        except BaseException as exc:
            if not ready.done():
                ready.set_exception(exc)
            else:
                raise

    thread = threading.Thread(target=lambda: asyncio.run(serve()), name="layout-server", daemon=True)
    thread.start()
    server = None
    try:
        server = ready.result(timeout=60)
        deadline = time.monotonic() + 5
        while not server.started:
            assert thread.is_alive() and time.monotonic() < deadline, "layout server did not start"
            time.sleep(0.025)
        yield f"http://127.0.0.1:{port}/"
    finally:
        if server is not None:
            server.should_exit = True
        thread.join(timeout=5)
        sock.close()
        assert not thread.is_alive(), "layout server did not stop"


@pytest.fixture(scope="module")
def chrome(tmp_path_factory):
    profile = tmp_path_factory.mktemp("layout-chrome")
    proc = subprocess.Popen([
        str(CHROME), "--headless=new", "--disable-gpu", "--no-sandbox", "--no-first-run", "--no-default-browser-check",
        "--disable-background-networking", "--remote-debugging-port=0", f"--user-data-dir={profile}", "about:blank",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    browser_ws = None
    try:
        active = profile / "DevToolsActivePort"
        deadline = time.monotonic() + 8
        while True:
            assert proc.poll() is None and time.monotonic() < deadline, "Chrome did not start DevTools"
            try:
                lines = active.read_text(encoding="utf-8").splitlines()
                if len(lines) >= 2:
                    port, browser_path = lines[:2]
                    break
            except (OSError, ValueError):
                pass  # Chrome briefly holds this file open while publishing the endpoint on Windows.
            time.sleep(0.025)
        browser_ws = f"ws://127.0.0.1:{port}{browser_path}"
        with urlopen(f"http://127.0.0.1:{port}/json/list", timeout=3) as response:
            pages = json.load(response)
        yield next(p["webSocketDebuggerUrl"] for p in pages if p["type"] == "page")
    finally:
        if browser_ws and proc.poll() is None:
            async def close():
                async with connect(browser_ws, proxy=None) as ws:
                    await ws.send('{"id":1,"method":"Browser.close"}')
            try:
                asyncio.run(close())
            except (OSError, TimeoutError):
                pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.terminate()  # Only the Popen handle created above; never find processes by name.
            proc.wait(timeout=3)


@pytest.mark.parametrize("width,height", [(1280, 900), (480, 1000)])
async def test_extreme_layout(stress_server, chrome, width, height):
    problems = []
    async with connect(chrome, proxy=None, max_size=16 * 1024 * 1024) as ws:
        page = Page(ws)
        page.width = width
        await page.call("Emulation.setDeviceMetricsOverride", width=width, height=height, deviceScaleFactor=2, mobile=False)
        await page.call("Page.navigate", url=stress_server)
        # The host API returns the last 200 events of the 300+ event fixture.
        await page.wait("document.querySelectorAll('[data-slot=lenders] .ent').length === 33 && document.querySelectorAll('.items li').length >= 62")

        async def check(scenario):
            await page.wait("document.readyState === 'complete'")
            found = await page.js(CHECK)
            problems.extend(f"{scenario} ({width}px): {issue}" for issue in found)
            # A scrollable strip can pass a clipping check while hiding the reason field.
            # Only the request box may scroll; decisions and metadata must stay in the strip.
            approvals = await page.js("""(() => {
              const out = [];
              const lineHeight = parseFloat(getComputedStyle(document.body).lineHeight);
              for (const strip of document.querySelectorAll('.alerts:not([hidden])')) {
                const bounds = strip.getBoundingClientRect();
                if (!strip.getClientRects().length) continue;
                if (strip.scrollHeight > strip.clientHeight + 2) out.push('approval controls overflow the strip');
                for (const prompt of strip.querySelectorAll('.pending-prompt')) {
                  if (prompt.getBoundingClientRect().height > 5 * lineHeight + 1) out.push('approval request exceeds five body lines');
                }
                for (const control of strip.querySelectorAll('.cap, input, .decide')) {
                  const r = control.getBoundingClientRect();
                  if (r.top < bounds.top - 1 || r.bottom > bounds.bottom + 1 || r.left < bounds.left - 1 || r.right > bounds.right + 1)
                    out.push(`approval control outside strip: ${control.className || control.tagName}`);
                }
              }
              return out;
            })()""")
            problems.extend(f"{scenario} ({width}px): {issue}" for issue in approvals)
            errors = await page.js("[...document.querySelectorAll('.frame > .note-warn')].map(e => e.textContent)")
            problems.extend(f"{scenario} ({width}px): {error}" for error in errors)

        await page.wait("document.querySelector(\'[data-slot=lenders] .ent:first-child .cap\').textContent.includes(\'2 件作業中\')")
        # Select through the actual list; ready predicates verify the detail, not just the shell.
        await page.js("[...document.querySelectorAll('.items li')].find(e => e.textContent.includes('web のテスト')).click()")
        await page.wait("document.querySelector('.d-head').textContent.includes('web のテスト')")
        await page.wait("document.querySelectorAll(\'.d-log .tl li\').length >= 200")
        await check("使う・長い1行の依頼")
        await page.shot("use-multiple")
        await page.click('.task-exec > summary')
        await page.wait("document.querySelector('.task-exec[open] [data-act=task-exec-list]')?.value.length > 5000")
        assert await page.js("document.querySelector('.task-exec summary').textContent.includes('毎回承認')")
        assert await page.js("""(() => {
          const pane = document.querySelector('.task-exec').closest('.scroll');
          const bounds = pane.getBoundingClientRect();
          return [...pane.querySelectorAll('[data-act=task-exec-list]')].every(field => {
            const box = field.getBoundingClientRect();
            return box.top >= bounds.top && box.bottom <= bounds.bottom && box.right <= bounds.right;
          });
        })()""")
        await check("使う・この依頼のコマンド")
        await page.shot("use-task-command-rules", ".task-exec", delay=2)
        for mode, label in [("off", "実行しない"), ("ask", "毎回承認")]:
            await page.js(f"""(() => {{
              const select = document.querySelector('[data-act=task-exec-mode]');
              select.value = {json.dumps(mode)};
              select.dispatchEvent(new Event('change', {{bubbles: true}}));
            }})()""")
            await page.wait(f"document.querySelector('.task-exec summary').textContent.includes({json.dumps(label)})")
        await page.click('.task-exec > summary')
        await page.js("[...document.querySelectorAll('.items li')].find(e => e.textContent.includes('src/module_0/')).click()")
        await page.wait("document.querySelector('.d-head').textContent.includes('src/module_0/')")
        await check("使う・200行の依頼")
        await page.click('[data-slot=lenders] .ent:first-child [data-act=toggle]')
        await page.wait("document.querySelector('[data-slot=lenders] .ent:first-child .set-group details')")
        await page.click('[data-slot=lenders] .ent:first-child .set-group details summary')
        await page.wait("document.querySelector('[data-slot=lenders] .ent:first-child .set-group details[open] .pop')")
        await check("使う・貸す人の設定と使えるモデル")
        await page.shot("use-lender-card")
        await page.click('[data-slot=lenders] .ent:first-child .set-group details summary')
        await page.shot("use-lender-settings", "[data-slot=lenders] .ent:first-child .set-group")
        assert await page.js("[...document.querySelector('[data-act=exec-mode]').options].map(e => e.value)") == [
            "off", "allowlist", "ask", "all"]
        await page.shot("use-lender-exec-mode", "[data-slot=lenders] .ent:first-child", delay=2)
        await page.click('[data-slot=lenders] .ent:first-child [data-act=toggle]')
        for tab, label in [("folders", "共有フォルダ"), ("allow", "コマンドの規則")]:
            await page.click(f'.surface [data-tab={tab}]')
            await page.wait(f"!document.querySelector('[data-panel={tab}]').hidden && document.querySelector('[data-panel={tab}]').childElementCount > 0")
            await check(f"使う・{label}")
            if tab == "allow":
                assert await page.js("[...document.querySelectorAll('[data-act=rule-text]')].map(e => e.dataset.key)") == [
                    "deny", "ask", "allow"]
                assert await page.js("[...document.querySelectorAll('[data-act=rule-text]')].every(e => e.value.length > 5000)")
                await page.shot("use-command-rules", "[data-panel=allow]", delay=2)
        await page.click('.surface [data-tab=tasks]')
        await page.click('[data-act=invite-open]')
        await page.wait("document.querySelector('[data-slot=invite] input')")
        await check("使う・招待")

        await page.click('.topbar [data-tab=lend]')
        await page.wait("document.querySelectorAll('[data-slot=connections] .ent').length === 13 && document.querySelector('.pending-prompt')")
        await page.js("[...document.querySelectorAll('.items li')].find(e => e.textContent.includes('web のテスト')).click()")
        await page.wait("document.querySelector('.d-head').textContent.includes('web のテスト')")
        await page.wait("document.querySelectorAll(\'[data-pending]\').length === 2")
        await page.js("[...document.querySelectorAll(\'[data-pending] input\')].forEach((e, i) => e.value = \'理由 \' + i)")
        await page.wait("document.querySelectorAll(\'.d-log .tl li\').length >= 300")
        await check("貸す・長い依頼と追加の指示の許可待ち")
        await page.shot("lend-multiple-pending", "[data-slot=pending]")
        await page.click('[data-slot=connections] .ent:first-child [data-act=card]')
        await page.wait("document.querySelector('[data-slot=connections] .ent:first-child .ent-set select')")
        assert await page.js("[...document.querySelectorAll(\'[data-pending] input\')].map(e => e.value)") == ["理由 0", "理由 1"]
        await check("貸す・依頼主の設定")
        await page.shot("lend-requester-card", "[data-slot=connections] .ent:first-child")
        await page.click('.surface [data-tab=pc]')
        await page.wait("!document.querySelector('[data-panel=pc]').hidden")
        await check("貸す・この PC")
        await page.click('.topbar [data-tab=log]')
        await page.wait("document.querySelectorAll('.log-view .ln').length >= 300")
        await check("ログ")
    assert not problems, "\n" + "\n".join(problems)
