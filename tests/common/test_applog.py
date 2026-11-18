import json
import threading

from codex_bridge.common.applog import AppLog


def test_add_and_filter(tmp_path):
    log = AppLog(tmp_path / "app.log")
    log.add("use", "info", "Alpha started")
    log.add("lend", "warn", "disk low")
    log.add("use", "error", "boom")
    assert [e["text"] for e in log.entries()] == ["Alpha started", "disk low", "boom"]
    assert [e["text"] for e in log.entries(side="use")] == ["Alpha started", "boom"]
    assert [e["text"] for e in log.entries(min_level="warn")] == ["disk low", "boom"]
    assert [e["text"] for e in log.entries(q="ALPHA")] == ["Alpha started"]
    assert [e["text"] for e in log.entries(limit=2)] == ["disk low", "boom"]
    first = log.entries()[0]
    assert set(first) == {"t", "side", "level", "text"} and isinstance(first["t"], float)


def test_trim_on_open(tmp_path):
    path = tmp_path / "app.log"
    log = AppLog(path)
    for i in range(5):
        log.add("use", "info", f"line {i}")
    log = AppLog(path, keep=3)
    assert len(path.read_text(encoding="utf-8").splitlines()) == 3
    assert [e["text"] for e in log.entries()] == ["line 2", "line 3", "line 4"]


def test_concurrent_adds(tmp_path):
    log = AppLog(tmp_path / "app.log")

    def work(n):
        for i in range(50):
            log.add("lend", "info", f"{n}-{i}")

    threads = [threading.Thread(target=work, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    lines = (tmp_path / "app.log").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 400
    assert all(json.loads(line)["side"] == "lend" for line in lines)


def test_bad_line_skipped(tmp_path):
    path = tmp_path / "app.log"
    path.write_text('not json\n{"t": 1.0, "side": "use", "level": "info", "text": "ok"}\n[1]\n', encoding="utf-8")
    log = AppLog(path)
    assert [e["text"] for e in log.entries()] == ["ok"]
