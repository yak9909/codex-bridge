"""Friend-facing log page (http://127.0.0.1:8766/), filtered by the host's log_visibility."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse

from .settings import FEATURE_INFO, SettingsStore, defaults

VISIBLE = {
    "summary": {"task_prompt", "final_report"},
    "files": {"task_prompt", "final_report", "tool_call"},
    "all": {"task_prompt", "final_report", "tool_call", "agent_message", "status", "state", "error",
            "started", "turn_completed"},
}
LOGGED_ELSEWHERE = {"tool_started", "plan", "reasoning_summary", "token_usage", "progress"}
MAX_ENTRIES = 5000


def _normalize(m: dict) -> dict | None:
    """Turn a runner log message into {ts, task_id, kind, text}."""
    t = m.get("type")
    base = {"ts": time.time(), "task_id": m.get("task_id")}
    if t == "task_prompt":
        return {**base, "kind": "task_prompt", "text": m.get("prompt", "")}
    if t == "final_report":
        return {**base, "kind": "final_report", "text": m.get("text") or ""}
    if t == "event":
        d = m.get("data") or {}
        kind = m.get("kind")
        if kind in ("tool_call", "tool_finished"):  # v0.2 sends tool_finished where v0.1 sent tool_call
            args = d.get("arguments", "")
            args = args if isinstance(args, str) else json.dumps(args, ensure_ascii=False)
            return {**base, "kind": "tool_call", "text": f"{d.get('tool')} {args}"}
        if kind in LOGGED_ELSEWHERE:  # in the work log (agent/worklog.py), not in this list
            return None
        return {**base, "kind": m.get("kind"), "text": d.get("text") or d.get("status") or ""}
    if t == "task_state":
        return {**base, "kind": "state", "text": m.get("state", "")}
    if t == "error":
        return {**base, "kind": "error", "text": m.get("text", "")}
    return None


class LogStore:
    def __init__(self, directory: Path) -> None:
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "log.jsonl"
        self.state_path = self.dir / "visibility.txt"
        self.visibility = self.state_path.read_text(encoding="utf-8").strip() if self.state_path.exists() else "summary"
        self.entries: list[dict] = []
        if self.path.exists():
            self.entries = [json.loads(l) for l in self.path.read_text(encoding="utf-8").splitlines()][-MAX_ENTRIES:]
        self._lock = threading.Lock()

    def set_visibility(self, value: str) -> None:
        if value in VISIBLE:
            self.visibility = value
            self.state_path.write_text(value, encoding="utf-8")

    def add(self, m: dict) -> None:
        if m.get("type") == "settings":
            self.set_visibility(m.get("log_visibility", self.visibility))
            return
        e = _normalize(m)
        if e is None:
            return
        with self._lock:
            self.entries.append(e)
            del self.entries[:-MAX_ENTRIES]
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")

    def visible(self) -> list[dict]:
        allowed = VISIBLE[self.visibility]
        return [e for e in self.entries if e["kind"] in allowed]


PAGE = """<!DOCTYPE html><html lang="ja"><head><meta charset="utf-8"><title>codex-bridge ログ</title>
<style>body{font:14px/1.6 "Yu Gothic UI",Meiryo,sans-serif;margin:0;background:#f5f6f8;color:#1d2330}
header{background:#1d2330;color:#fff;padding:10px 20px;font-weight:700}main{max-width:960px;margin:0 auto;padding:16px}
.e{background:#fff;border:1px solid #e3e6ec;border-radius:8px;padding:8px 12px;margin:8px 0;white-space:pre-wrap;word-break:break-all}
.k{font-size:12px;color:#6b7385}.task_prompt{border-left:4px solid #2f6fde}.final_report{border-left:4px solid #1f9d55}
.note{color:#6b7385;font-size:12px}
.card{background:#fff;border:1px solid #e3e6ec;border-radius:10px;padding:14px 16px;margin:12px 0}
.card h2{font-size:15px;margin:0 0 6px}.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:8px 0}
input[type=number]{width:70px;font:inherit;border:1px solid #e3e6ec;border-radius:6px;padding:3px 6px}
button{font:inherit;border:1px solid #2f6fde;background:#2f6fde;color:#fff;padding:4px 14px;border-radius:6px;cursor:pointer}
label.ck{display:flex;gap:8px;align-items:flex-start;margin:6px 0}#msg{color:#1f9d55;font-size:13px}</style></head>
<body><header>codex-bridge（この PC の Codex を貸している作業の記録と設定）</header>
<main>
<section class="card"><h2>使用量の上限（あなたが決める）</h2>
<p class="note">依頼主の作業は、あなたの Codex の使用量を使います。ここで決めた使用率を超えると、依頼主の作業を止めます。
空欄なら依頼主の設定だけが使われます（低いほうが効きます）。</p>
<div class="row">5 時間枠 <input type="number" id="l5" min="1" max="100" placeholder="なし">% ／ 週間枠 <input type="number" id="lw" min="1" max="100" placeholder="なし">%</div>
<label class="ck"><input type="checkbox" id="lf"><span><b>超えたらすぐ強制的に止める</b><br>
<span class="note">オフのときは、Codex に「区切りのよいところで止めて報告して」と頼みます（少し使用量が増えることがあります）。</span></span></label>
<div class="row"><button id="lsave">保存</button><span id="msg"></span></div></section>
<section class="card"><h2>作業の記録</h2><p class="note" id="vis"></p><div id="list"></div></section>
</main><script>
const KIND={task_prompt:"依頼",final_report:"最終報告",tool_call:"道具",agent_message:"発言",state:"状態",status:"お知らせ",error:"エラー",started:"開始",turn_completed:"区切り"};
const VIS={summary:"表示範囲: 依頼文と最終報告のみ（依頼主が設定）",files:"表示範囲: 依頼・報告・道具の名前と対象（依頼主が設定）",all:"表示範囲: すべて（依頼主が設定）"};
const esc=s=>String(s??"").replace(/[&<>]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));
async function load(){const r=await (await fetch("/api/events")).json();document.getElementById("vis").textContent=VIS[r.visibility]||"";
document.getElementById("list").innerHTML=r.events.slice().reverse().map(e=>`<div class="e ${e.kind}"><div class="k">${new Date(e.ts*1000).toLocaleString()}・${KIND[e.kind]||e.kind}・${esc(e.task_id||"")}</div>${esc(e.text)}</div>`).join("")||"<p class=note>まだ記録はありません。</p>";}
const $=id=>document.getElementById(id);
async function loadLimits(){const l=await (await fetch("/api/limits")).json();$("l5").value=l.five_hour??"";$("lw").value=l.weekly??"";$("lf").checked=!!l.force;}
$("lsave").onclick=async()=>{const body={five_hour:$("l5").value||null,weekly:$("lw").value||null,force:$("lf").checked};
const r=await fetch("/api/limits",{method:"PUT",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
const d=await r.json();$("msg").style.color=r.ok?"#1f9d55":"#d14343";$("msg").textContent=r.ok?"保存しました（すぐに効きます）":("保存できません: "+(d.detail||""));};
loadLimits();load();setInterval(load,3000);</script></body></html>"""


def _limits_patch(body: dict) -> dict:
    """The limits page of v0.1 sends what its input boxes hold: text, and "" (or null) for an empty box."""
    if not isinstance(body, dict):
        raise ValueError("設定は JSON の object で指定してください")
    out: dict = {"force": body.get("force", False)}
    for k in ("five_hour", "weekly"):
        v = body.get(k)
        out[k] = None if v in (None, "") else float(v) if isinstance(v, str) else v
    return out


def create_logview_app(store: LogStore, settings: SettingsStore | None = None, on_settings=None,
                       on_move=None) -> FastAPI:
    """on_settings(new_settings) is awaited after a change is saved (it puts the change to work); on_move(path)
    moves the data to that folder (design 4.7) and raises ValueError / OSError when it cannot."""
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    def local_only(request: Request) -> None:
        if (request.headers.get("host") or "").rsplit(":", 1)[0] not in ("127.0.0.1", "localhost"):
            raise HTTPException(403, "forbidden host")

    def need_settings() -> SettingsStore:
        if settings is None:
            raise HTTPException(404, "not available")
        return settings

    async def save(patch: dict) -> dict:
        try:
            saved = need_settings().update(patch)
        except (TypeError, ValueError) as e:
            raise HTTPException(400, str(e))
        if on_settings:
            await on_settings(saved)
        return saved

    @app.get("/", response_class=HTMLResponse)
    async def index():
        return PAGE

    @app.get("/api/events")
    async def events():
        return {"visibility": store.visibility, "events": store.visible()[-1000:]}

    @app.get("/api/settings")
    async def get_settings(request: Request):
        local_only(request)
        return {"settings": (settings.get() if settings else defaults()), "feature_info": FEATURE_INFO}

    @app.put("/api/settings")
    async def put_settings(request: Request, body: dict):
        local_only(request)
        return await save(body)

    @app.get("/api/limits")
    async def get_limits(request: Request):
        local_only(request)
        return (settings.get() if settings else defaults())["limits"]

    @app.put("/api/limits")
    async def put_limits(request: Request, body: dict):
        local_only(request)
        need_settings()
        try:
            patch = _limits_patch(body)
        except (TypeError, ValueError) as e:
            raise HTTPException(400, f"上限は 1〜100 の数で指定してください: {e}")
        return (await save({"limits": patch}))["limits"]

    @app.put("/api/data-location")
    async def put_data_location(request: Request, body: dict):
        local_only(request)
        if on_move is None:
            raise HTTPException(404, "not available")
        path = body.get("path") if isinstance(body, dict) else None
        if not isinstance(path, str) or not path.strip():
            raise HTTPException(400, "保存場所のフォルダを指定してください")
        try:
            left = await on_move(Path(path.strip()))
        except (ValueError, OSError) as e:
            raise HTTPException(400, f"移せませんでした（元の場所のまま使います）: {e}" if isinstance(e, OSError) else str(e))
        return {"moved": True, "left": [str(p) for p in left or []]}

    return app
