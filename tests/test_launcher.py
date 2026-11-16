"""Role chooser contracts; the selection model also runs without a display."""

import ast
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

from codex_bridge import launcher


def test_role_copy_is_exact_and_keeps_entry_values():
    assert launcher._ROLE_CHOICES == (
        ("host", "使う側", "他の人の Codex を使う"),
        ("agent", "貸す側", "自分の Codex を他の人に使わせる"),
    )


@pytest.mark.parametrize("index,role", [(0, "host"), (1, "agent")])
def test_click_selects_the_card_role(index, role):
    selection = launcher._RoleSelection()
    selection.choose(index)
    assert selection.role == role
    assert selection.finished


@pytest.mark.parametrize("key,shift", [("Tab", False), ("Tab", True), ("ISO_Left_Tab", False),
                                        ("Up", False), ("Down", False)])
def test_navigation_wraps_without_confirming(key, shift):
    selection = launcher._RoleSelection()
    assert selection.handle_key(key, shift=shift)
    assert selection.selected == 1
    assert selection.role is None
    assert not selection.finished
    selection.handle_key(key, shift=shift)
    assert selection.selected == 0


@pytest.mark.parametrize("key", ["Return", "KP_Enter", "space"])
@pytest.mark.parametrize("index,role", [(0, "host"), (1, "agent")])
def test_keyboard_confirms_selected_card(key, index, role):
    selection = launcher._RoleSelection()
    if index:
        selection.handle_key("Down")
    assert selection.handle_key(key)
    assert selection.role == role
    assert selection.finished


@pytest.mark.parametrize("via_key", [False, True])
def test_cancel_returns_none_without_launching(via_key):
    selection = launcher._RoleSelection()
    selection.handle_key("Down")
    if via_key:
        assert selection.handle_key("Escape")
    else:
        selection.cancel()
    assert selection.role is None
    assert selection.finished


def test_other_keys_do_not_change_selection():
    selection = launcher._RoleSelection()
    assert not selection.handle_key("a")
    assert selection.selected == 0
    assert selection.role is None
    assert not selection.finished


@pytest.mark.parametrize("missing", [False, True])
def test_icon_uses_packaged_asset_or_silent_windows_default(tmp_path, monkeypatch, missing):
    tk = pytest.importorskip("tkinter")
    root = Mock()
    fallback = Mock()
    monkeypatch.setattr(launcher, "_windows_default_icon", fallback)
    monkeypatch.setattr(launcher.sys, "platform", "win32")
    icon = tmp_path / "codex-bridge.ico"
    if not missing:
        icon.write_bytes(b"test icon")
    launcher._set_icon(root, icon)
    if missing:
        fallback.assert_called_once_with(root)
        root.iconbitmap.assert_not_called()
    else:
        root.iconbitmap.assert_called_once_with(default=str(icon))
        fallback.assert_not_called()
        root.iconbitmap.side_effect = tk.TclError("unreadable icon")
        launcher._set_icon(root, icon)
        fallback.assert_called_once_with(root)


def test_exe_and_dialog_share_a_small_valid_icon():
    from PIL import Image

    repo = Path(__file__).resolve().parents[1]
    icon = Path(launcher.__file__).with_name("assets") / "codex-bridge.ico"
    assert icon.stat().st_size < 16_384
    with Image.open(icon) as image:
        assert image.format == "ICO"
        assert {(16, 16), (32, 32), (48, 48)} <= image.ico.sizes()
    tree = ast.parse((repo / "codex-bridge.spec").read_text(encoding="utf-8"))
    exe = next(node for node in ast.walk(tree)
               if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "EXE")
    icon_option = next(option.value for option in exe.keywords if option.arg == "icon")
    assert repo / ast.literal_eval(icon_option) == icon
    datas = next(node.value for node in tree.body
                 if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "datas"
                                                        for t in node.targets))
    assert (icon.relative_to(repo).as_posix(), "codex_bridge/assets") in ast.literal_eval(datas)


@pytest.fixture
def tk_root():
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except tk.TclError as error:
        pytest.skip(f"Tk cannot be created: {error}")
    root.withdraw()
    destroy = root.destroy

    def clean_destroy():
        for callback in root.tk.call("after", "info"):
            root.after_cancel(callback)
        destroy()

    root.destroy = Mock(side_effect=clean_destroy)
    yield root
    try:
        root.destroy()
    except tk.TclError:
        pass


@pytest.mark.parametrize("index,role", [(0, "host"), (1, "agent")])
@pytest.mark.parametrize("part", ["frame", "title", "description"])
def test_choose_role_clicks_anywhere_on_card(tk_root, monkeypatch, index, role, part):
    import tkinter as tk

    monkeypatch.setattr(tk, "Tk", lambda: tk_root)

    def click():
        tk_root.update()
        cards = [w for w in tk_root.winfo_children() if w.winfo_class() == "Frame"]
        card = cards[index]
        target = card if part == "frame" else card.winfo_children()[part == "description"]
        target.event_generate("<Button-1>")

    tk_root.after(80, click)
    tk_root.after(1000, tk_root.quit)  # A broken binding must fail, never hang pytest.
    assert launcher.choose_role() == role
    tk_root.destroy.assert_called_once_with()


@pytest.mark.parametrize("events,role", [(["<Return>"], "host"),
                                         (["<Tab>", "<Return>"], "agent"),
                                         (["<Down>", "<space>"], "agent"),
                                         (["<Up>", "<Return>"], "agent"),
                                         (["<Escape>"], None), ([], None)])
def test_choose_role_keyboard_and_window_close(tk_root, monkeypatch, events, role):
    import tkinter as tk

    monkeypatch.setattr(tk, "Tk", lambda: tk_root)
    timed_out = []

    def timeout():
        timed_out.append(True)
        tk_root.quit()

    def drive():
        tk_root.focus_force()
        tk_root.update()
        if not events:
            tk_root.tk.call(tk_root.protocol("WM_DELETE_WINDOW"))
        for event in events:
            tk_root.event_generate(event)

    tk_root.after(80, drive)
    tk_root.after(1000, timeout)
    assert launcher.choose_role() == role
    assert not timed_out


@pytest.mark.parametrize("scaling", [96 / 72, 144 / 72])
def test_card_layout_fonts_hover_focus_and_center(tk_root, scaling):
    from tkinter import font

    tk_root.tk.call("tk", "scaling", scaling)
    chooser = launcher._RoleChooser(tk_root)
    tk_root.update()
    cards = chooser.cards
    assert len(cards) == 2
    assert [tuple(label.cget("text") for label in card.winfo_children()) for card in cards] == [
        ("使う側", "他の人の Codex を使う"),
        ("貸す側", "自分の Codex を他の人に使わせる"),
    ]
    assert any(w.cget("text") == "この PC での使い方を選んでください。"
               for w in tk_root.winfo_children() if w.winfo_class() == "Label")
    for card in cards:
        title, description = card.winfo_children()
        title_font = font.Font(root=tk_root, font=title.cget("font"))
        description_font = font.Font(root=tk_root, font=description.cget("font"))
        assert title_font.actual("family") == font.nametofont("TkDefaultFont", root=tk_root).actual("family")
        assert title_font.actual("weight") == "bold"
        assert title_font.actual("size") > description_font.actual("size")
        assert description.cget("fg") != title.cget("fg")
        for label in (title, description):
            assert label.winfo_width() >= label.winfo_reqwidth()
            assert label.winfo_height() >= label.winfo_reqheight()
        before = card.cget("bg")
        card.winfo_children()[1].event_generate("<Enter>")
        assert card.cget("bg") != before
        card.winfo_children()[1].event_generate("<Leave>")
        assert card.cget("bg") == before
    borders = [card.cget("highlightbackground") for card in cards]
    assert borders[0] != borders[1]
    tk_root.focus_force()
    tk_root.event_generate("<Down>")
    tk_root.update()
    assert [card.cget("highlightbackground") for card in cards] == borders[::-1]
    assert chooser.selection.selected == 1
    assert abs(tk_root.winfo_x() + tk_root.winfo_width() / 2 - tk_root.winfo_screenwidth() / 2) < 20
    assert abs(tk_root.winfo_y() + tk_root.winfo_height() / 2 - tk_root.winfo_screenheight() / 2) < 40


@pytest.mark.skipif(sys.platform != "win32", reason="Windows title-bar icon")
def test_missing_icon_replaces_title_bar_feather_with_windows_default(tk_root, tmp_path):
    import ctypes
    from ctypes import wintypes

    launcher._RoleChooser(tk_root)
    tk_root.update()
    user = ctypes.WinDLL("user32", use_last_error=True)
    user.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    user.GetAncestor.restype = wintypes.HWND
    user.LoadIconW.argtypes = [wintypes.HINSTANCE, ctypes.c_void_p]
    user.LoadIconW.restype = wintypes.HICON
    user.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user.SendMessageW.restype = ctypes.c_ssize_t
    window = user.GetAncestor(tk_root.winfo_id(), 2)  # GA_ROOT includes the title bar.
    expected = user.LoadIconW(None, ctypes.c_void_p(32512))  # IDI_APPLICATION.
    assert expected
    launcher._set_icon(tk_root, tmp_path / "missing.ico")
    for size in (0, 1):
        assert user.SendMessageW(window, 0x007F, size, 0) == expected  # WM_GETICON.
