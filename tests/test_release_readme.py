import importlib.util
import re
from pathlib import Path
from string import Formatter

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
    fields = [field for _, field, _, _ in Formatter().parse(mod.README) if field is not None]
    assert fields == ["ver"]
    rendered = mod.README.format(ver="0.3.0")
    assert "v0.3.0" in rendered
    assert "docs\\friend-guide.md" in rendered
    assert "docs\\operation-guide.md" in rendered
    assert "/hooks" in mod.README


def test_version_is_030(mod):
    assert codex_bridge.__version__ == "0.3.0"
    assert mod.VERSION == "0.3.0"


@pytest.mark.parametrize("path", ["README.md", "docs/friend-guide.md", "docs/operation-guide.md", None])
def test_guides_describe_the_resident_screen(mod, path):
    root = Path(__file__).resolve().parents[1]
    text = (root / path).read_text(encoding="utf-8") if path else mod.README
    for content in ("タスクトレイ", "左クリック", "画面を開く", "http://127.0.0.1:8766/",
                    "「使う」", "「貸す」", "「ログ」", "つなぐ", "カード"):
        assert content in text, (path, content)
    for obsolete in ("知人", "使わせる側", "「貸す側」を選", "「使う側」を選", "設定を保存", "保存して反映"):
        assert obsolete not in text, (path, obsolete)


@pytest.mark.parametrize("path", ["README.md", "docs/operation-guide.md", None])
def test_borrower_guides_keep_setup_and_console_instructions(mod, path):
    root = Path(__file__).resolve().parents[1]
    text = (root / path).read_text(encoding="utf-8") if path else mod.README
    for content in ("準備を始める", "招待コードを発行", "毎回承認", "http://127.0.0.1:8770",
                    "install-mcp.bat", "start.bat", "stop.bat", "host setup"):
        assert content in text, (path, content)
    for obsolete in ("set_thresholds", "全体と同じ", "こちらのしきい値"):
        assert obsolete not in text, (path, obsolete)


def test_guides_keep_the_windows_data_path_and_sign_in_shortcut(mod):
    root = Path(__file__).resolve().parents[1]
    paths = ["README.md", "docs/friend-guide.md", "docs/operation-guide.md"]
    texts = [(root / path).read_text(encoding="utf-8") for path in paths]
    for text in [*texts, mod.README]:
        assert not re.search(r"%LOCALAPPDATA%(?![\\/])", text)
    for text in texts[:2]:
        assert r"%LOCALAPPDATA%\codex-bridge-agent" in text
    assert "shell:startup" in texts[1] and "引数は要りません" in texts[1]


def test_notice_limits_and_status_guidance():
    root = Path(__file__).resolve().parents[1]
    guide = (root / "docs/operation-guide.md").read_text(encoding="utf-8")
    assert "Claude Code では、次に codex-bridge のどれかの道具を呼んだとき" in guide
    assert "Codex では、次のターンの終わり" in guide
    assert "24 時間より前の依頼" in guide
    assert "get_result か「使う」タブ" in guide
    assert "同じ PC の Claude Code の会話を 2 つ" in guide
    assert "タスク ID をフックが読み取れなかった依頼" in guide
    assert "ふつうは知らせで足ります。知らせが届かない環境のときだけ wait_sec で待ちます。" in (
        root / "README.md").read_text(encoding="utf-8")
