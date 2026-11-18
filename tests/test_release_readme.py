import importlib.util
from pathlib import Path

import codex_bridge
import pytest


@pytest.fixture(scope="module")
def mod():
    path = Path(__file__).resolve().parents[1] / "scripts" / "build_release.py"
    spec = importlib.util.spec_from_file_location("build_release", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_readme_has_only_printable_text(mod):
    assert all(ch == "\n" or ch.isprintable() for ch in mod.README)


def test_readme_formats(mod):
    assert "v0.3.0" in mod.README.format(ver="0.3.0")
    assert "/hooks" in mod.README


def test_version_is_030(mod):
    assert codex_bridge.__version__ == "0.3.0"
    assert mod.VERSION == "0.3.0"


def test_notice_limits_and_status_guidance():
    root = Path(__file__).resolve().parents[1]
    guide = (root / "docs/operation-guide.md").read_text(encoding="utf-8")
    assert "Claude Code では、次に codex-bridge のどれかの道具を呼んだとき" in guide
    assert "Codex では、次のターンの終わり" in guide
    assert "24 時間より前の依頼" in guide
    assert "get_result か管理画面" in guide
    assert "同じ PC の Claude Code の会話を 2 つ" in guide
    assert "タスク ID をフックが読み取れなかった依頼" in guide
    assert "ふつうは知らせで足ります。知らせが届かない環境のときだけ wait_sec で待ちます。" in (
        root / "README.md").read_text(encoding="utf-8")
