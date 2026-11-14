"""Friend-facing log page (http://127.0.0.1:8766/), filtered by the host's log_visibility."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse

VISIBLE = {
    "summary": {"task_prompt", "final_report"},
    "files": {"task_prompt", "final_report", "tool_call"},
    "all": {"task_prompt", "final_report", "tool_call", "agent_message", "status", "state", "error",
            "started", "turn_completed"},
}
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
        if m.get("kind") == "tool_call":
            return {**base, "kind": "tool_call", "text": f"{d.get('tool')} {d.get('arguments', '')}"}
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


class LimitsStore:
    """The friend's own usage limits for the borrower (percent per window) and the force-stop choice."""

    DEFAULT = {"five_hour": None, "weekly": None, "force": False}

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def get(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return dict(self.DEFAULT)
        return {**self.DEFAULT, **{k: data.get(k) for k in self.DEFAULT}}

    def set(self, data: dict) -> dict:
        out = {"force": bool(data.get("force", False))}
        for k in ("five_hour", "weekly"):
            v = data.get(k)
            if v in (None, ""):
                out[k] = None
                continue
            v = float(v)
            if not 0 < v <= 100:
                raise ValueError("上限は 1〜100 の数で指定してください")
            out[k] = v if v != int(v) else int(v)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(out), encoding="utf-8")
        return out


def create_logview_app(store: LogStore, limits: LimitsStore | None = None, on_limits=None) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    def local_only(request: Request) -> None:
        if (request.headers.get("host") or "").rsplit(":", 1)[0] not in ("127.0.0.1", "localhost"):
            raise HTTPException(403, "forbidden host")

    @app.get("/", response_class=HTMLResponse)
    async def index():
        return PAGE

    @app.get("/api/events")
    async def events():
        return {"visibility": store.visibility, "events": store.visible()[-1000:]}

    @app.get("/api/limits")
    async def get_limits(request: Request):
        local_only(request)
        return limits.get() if limits else dict(LimitsStore.DEFAULT)

    @app.put("/api/limits")
    async def put_limits(request: Request, body: dict):
        local_only(request)
        if limits is None:
            raise HTTPException(404, "not available")
        try:
            saved = limits.set(body)
        except (TypeError, ValueError) as e:
            raise HTTPException(400, str(e))
        if on_limits:
            on_limits(saved)
        return saved

    return app
