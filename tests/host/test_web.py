import httpx
import pytest
import yaml

from codex_bridge.common.invite import parse_invite
from codex_bridge.host.app import build_host, create_local_app, create_public_app
from codex_bridge.host.config import load_host_config
from codex_bridge.host.main import ensure_config

H = {"X-Codex-Bridge": "1"}
VALID_OFF = "default: read\nrules: []\nexec: {mode: \"off\"}\n"


@pytest.fixture
def host(tmp_path):
    share = tmp_path / "share"
    (share / "raw").mkdir(parents=True)
    cfg = tmp_path / "host.yaml"
    cfg.write_text(yaml.safe_dump({"share_root": str(share), "data_dir": str(tmp_path / "data"),
                                   "public_bind": "100.64.0.1:8765", "local_port": 8770,
                                   "rg_path": str(tmp_path / "rg.exe")}), encoding="utf-8")
    ensure_config(cfg)
    return build_host(load_host_config(cfg))


@pytest.fixture
async def admin(host):
    t = httpx.ASGITransport(app=create_local_app(host), client=("127.0.0.1", 5000))
    async with httpx.AsyncClient(transport=t, base_url="http://127.0.0.1:8770") as c:
        yield c


@pytest.fixture
async def public(host):
    t = httpx.ASGITransport(app=create_public_app(host), client=("100.64.0.2", 5000))
    async with httpx.AsyncClient(transport=t, base_url="http://100.64.0.1:8765") as c:
        yield c


async def test_admin_api_not_served_on_public_bind(public):
    assert (await public.get("/admin/api/policy")).status_code == 404
    assert (await public.get("/admin/")).status_code == 404


async def test_admin_page_served(admin):
    r = await admin.get("/admin/")
    assert r.status_code == 200 and "codex-bridge" in r.text


async def test_mutation_requires_custom_header(admin):
    r = await admin.put("/admin/api/policy", content=VALID_OFF)
    assert r.status_code == 403


async def test_foreign_host_header_rejected(admin):
    r = await admin.get("/admin/api/policy", headers={"Host": "evil.example:8770"})
    assert r.status_code == 403


async def test_policy_put_validates_and_applies(admin, host):
    assert (await admin.put("/admin/api/policy", content="rules: [", headers=H)).status_code == 400
    assert (await admin.put("/admin/api/policy", content=VALID_OFF, headers=H)).status_code == 200
    assert host.policy.current().exec_mode == "off"
    assert "mode" in (await admin.get("/admin/api/policy")).json()["text"]


async def test_approve_via_api(admin, host):
    a = host.approvals.request("command", "python x.py", task_id=None)
    listed = (await admin.get("/admin/api/approvals")).json()
    assert listed[0]["id"] == a.id
    assert (await admin.post(f"/admin/api/approvals/{a.id}", json={"approve": True}, headers=H)).status_code == 200
    assert host.approvals.get(a.id).status == "approved"
    assert (await admin.post(f"/admin/api/approvals/{a.id}", json={"approve": True}, headers=H)).status_code == 409


async def test_invite_issue_and_revoke(admin, host):
    r = await admin.post("/admin/api/invites", json={"name": "friend"}, headers=H)
    inv = parse_invite(r.json()["code"])
    assert inv.url == "http://100.64.0.1:8765" and len(host.tokens.hashes()) == 1
    assert (await admin.delete("/admin/api/invites/friend", headers=H)).status_code == 200
    assert host.tokens.hashes() == set()


async def test_pause_and_thresholds_and_visibility(admin, host):
    await admin.post("/admin/api/pause", json={"paused": True}, headers=H)
    assert host.policy.current().paused is True
    await admin.put("/admin/api/thresholds", json={"five_hour": 70}, headers=H)
    assert (await admin.get("/admin/api/usage")).json()["thresholds"]["five_hour"] == 70
    assert (await admin.put("/admin/api/log-visibility", json={"value": "all"}, headers=H)).status_code == 200
    assert (await admin.put("/admin/api/log-visibility", json={"value": "x"}, headers=H)).status_code == 400


async def test_overview(admin):
    o = (await admin.get("/admin/api/overview")).json()
    assert o["agent"]["connected"] is False and o["paused"] is False and "share_root" in o


def test_first_run_writes_sample_configs(tmp_path):
    cfg = tmp_path / "host.yaml"
    assert ensure_config(cfg) is True
    assert cfg.exists()
    data_dir = load_host_config(cfg).data_dir
    assert (data_dir / "policy.yaml").exists()
    assert ensure_config(cfg) is False
