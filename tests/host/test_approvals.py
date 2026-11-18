import asyncio

from codex_bridge.host.approvals import make_approve_fn


async def test_request_and_decide(share):
    a = share.approvals.request("command", "python x.py", task_id="t1")
    assert a.status == "pending" and [p.id for p in share.approvals.pending()] == [a.id]
    share.approvals.decide(a.id, True)
    assert share.approvals.get(a.id).status == "approved" and share.approvals.pending() == []


def test_request_records_cwd(share):
    a = share.approvals.request("command", "npm test", "t1", cwd="web")
    assert a.cwd == "web" and a.to_dict()["cwd"] == "web"
    default = share.approvals.request("command", "npm test", "t2")
    assert default.cwd is None and default.to_dict()["cwd"] is None


async def test_wait_wakes_on_decision(share):
    a = share.approvals.request("command", "x", task_id=None)
    asyncio.get_running_loop().call_later(0.1, share.approvals.decide, a.id, False)
    assert await share.approvals.wait(a.id, timeout=5) == "denied"


async def test_wait_times_out_as_pending(share):
    a = share.approvals.request("command", "x", task_id=None)
    assert await share.approvals.wait(a.id, timeout=0.1) == "pending"


async def test_expiry(share):
    share.set_policy(limits={"approval_timeout_sec": 1})
    a = share.approvals.request("command", "x", task_id=None)
    assert await share.approvals.wait(a.id, timeout=3) == "expired"
    assert share.approvals.pending() == []


async def test_on_change_called(share):
    seen = []
    share.approvals.on_change.append(lambda: seen.append(1))
    a = share.approvals.request("command", "x", task_id=None)
    share.approvals.decide(a.id, True)
    assert len(seen) == 2


async def test_approve_fn_reuses_pending_request_for_same_action(share):
    fn = make_approve_fn(share.approvals, lambda: "t1", wait_sec=0.05)
    assert await fn("delete", "削除: raw/b.txt") == "pending"
    assert await fn("delete", "削除: raw/b.txt") == "pending"
    assert len(share.approvals.pending()) == 1
    share.approvals.decide(share.approvals.pending()[0].id, True)
    assert await fn("delete", "削除: raw/b.txt") == "approved"
    # consumed: a later identical action needs a new approval
    assert await fn("delete", "削除: raw/b.txt") == "pending"
