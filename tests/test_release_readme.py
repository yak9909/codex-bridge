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
