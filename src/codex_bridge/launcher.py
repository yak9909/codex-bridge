"""Small tkinter role chooser shown when the unified executable is double-clicked."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

Role = Literal["host", "agent"]
_ROLE_CHOICES: tuple[tuple[Role, str, str], ...] = (
    ("host", "使う側", "他の人の Codex を使う"),
    ("agent", "貸す側", "自分の Codex を他の人に使わせる"),
)


@dataclass
class _RoleSelection:
    """Selection and dismissal rules, independent of Tk and a display server."""

    selected: int = 0
    role: Role | None = None
    finished: bool = False

    def choose(self, index: int) -> None:
        self.selected = index
        self.role = _ROLE_CHOICES[index][0]
        self.finished = True

    def cancel(self) -> None:
        self.role = None
        self.finished = True

    def handle_key(self, key: str, *, shift: bool = False) -> bool:
        if key in ("Tab", "ISO_Left_Tab", "Up", "Down"):
            backwards = key in ("ISO_Left_Tab", "Up") or (key == "Tab" and shift)
            self.selected = (self.selected + (-1 if backwards else 1)) % len(_ROLE_CHOICES)
        elif key in ("Return", "KP_Enter", "space"):
            self.choose(self.selected)
        elif key == "Escape":
            self.cancel()
        else:
            return False
        return True


def _windows_default_icon(root) -> None:
    """Use the shared Windows application icon, never Tk's class/feather icon."""
    import ctypes
    from ctypes import wintypes

    user = ctypes.WinDLL("user32", use_last_error=True)
    user.LoadIconW.argtypes = [wintypes.HINSTANCE, ctypes.c_void_p]
    user.LoadIconW.restype = wintypes.HICON
    user.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user.SendMessageW.restype = ctypes.c_ssize_t
    icon = user.LoadIconW(None, ctypes.c_void_p(32512))  # IDI_APPLICATION; shared, do not destroy.
    window = int(root.wm_frame(), 0)
    for size in (0, 1):  # WM_SETICON: ICON_SMALL / ICON_BIG.
        user.SendMessageW(window, 0x0080, size, icon)


def _set_icon(root, icon: Path) -> None:
    import tkinter as tk

    if sys.platform == "win32":
        if icon.is_file():
            try:
                root.iconbitmap(default=str(icon))
                return
            except tk.TclError:
                pass
        _windows_default_icon(root)
    else:
        # The launcher targets Windows; hide the feather for source runs elsewhere.
        root.iconphoto(True, tk.PhotoImage(master=root, width=1, height=1))


class _RoleChooser:
    def __init__(self, root) -> None:
        import tkinter as tk
        from tkinter import font

        self.root = root
        self.selection = _RoleSelection()
        self.cards = []
        self.hovered: int | None = None
        self.pressed: int | None = None
        root.withdraw()
        root.title("codex-bridge")
        root.resizable(False, False)
        root.configure(bg="#f5f6f8")

        self.body_font = font.nametofont("TkDefaultFont", root=root).copy()
        self.title_font = self.body_font.copy()
        self.title_font.configure(size=round(self.body_font.actual("size") * 1.4), weight="bold")
        tk.Label(root, text="この PC での使い方を選んでください。", font=self.body_font,
                 bg="#f5f6f8", fg="#18212f").pack(anchor="w", padx="18p", pady=("16p", "12p"))

        for index, (_, title, description) in enumerate(_ROLE_CHOICES):
            card = tk.Frame(root, bd=0, highlightthickness="1.5p", takefocus=True, cursor="hand2")
            card.pack(fill="x", padx="18p", pady=(0, "12p"))
            tk.Label(card, text=title, font=self.title_font, fg="#18212f", anchor="w",
                     bd=0).pack(fill="x", padx="14p", pady=("11p", "3p"))
            tk.Label(card, text=description, font=self.body_font, fg="#626b78", anchor="w",
                     bd=0).pack(fill="x", padx="14p", pady=(0, "12p"))
            for widget in (card, *card.winfo_children()):
                widget.bind("<ButtonPress-1>", lambda event, i=index: self._press(i))
                widget.bind("<ButtonRelease-1>", lambda event, i=index: self._release(event, i))
                widget.bind("<Enter>", lambda event, i=index: self._hover(i))
                widget.bind("<Leave>", lambda event: self._hover(None))
            card.bind("<FocusIn>", lambda event, i=index: self._select(i))
            self.cards.append(card)

        root.bind("<KeyPress>", self._on_key)
        root.protocol("WM_DELETE_WINDOW", self._cancel)
        self._refresh()
        root.update_idletasks()
        # 255 points is about 340px at 100%; Tk scales it along with the padding.
        width = max(root.winfo_reqwidth(), root.winfo_pixels("255p"))
        height = root.winfo_reqheight()
        root.minsize(width, height)
        x = max(0, (root.winfo_screenwidth() - width) // 2)
        y = max(0, (root.winfo_screenheight() - height) // 2)
        root.geometry(f"+{x}+{y}")
        _set_icon(root, Path(__file__).with_name("assets") / "codex-bridge.ico")
        root.deiconify()
        self.cards[0].focus_set()

    def _refresh(self) -> None:
        for index, card in enumerate(self.cards):
            background = "#eef3fc" if index == self.hovered else "#ffffff"
            border = "#2563eb" if index == self.selection.selected else "#d4d9e1"
            card.configure(bg=background, highlightbackground=border, highlightcolor=border)
            for label in card.winfo_children():
                label.configure(bg=background)

    def _hover(self, index: int | None) -> None:
        self.hovered = index
        self._refresh()

    def _select(self, index: int) -> None:
        self.selection.selected = index
        self._refresh()

    def _press(self, index: int) -> None:
        self.pressed = index

    def _release(self, event, index: int) -> None:
        pressed = self.pressed
        self.pressed = None
        if pressed != index:
            return
        card = self.cards[index]
        x, y = event.x_root - card.winfo_rootx(), event.y_root - card.winfo_rooty()
        if 0 <= x < card.winfo_width() and 0 <= y < card.winfo_height():
            self._choose(index)

    def _choose(self, index: int) -> None:
        self.selection.choose(index)
        self.root.quit()

    def _cancel(self) -> None:
        self.selection.cancel()
        self.root.quit()

    def _on_key(self, event) -> str | None:
        if not self.selection.handle_key(event.keysym, shift=bool(event.state & 1)):
            return None
        if self.selection.finished:
            self.root.quit()
        else:
            self._refresh()
            self.cards[self.selection.selected].focus_set()
        return "break"


def _enable_dpi_awareness() -> None:
    """Make this process DPI aware before Tk creates a window (else 125%/150% screens blur it).

    Per-monitor first, then the older calls; any failure just keeps the default. It applies to the
    whole process and can only be set once, so later windows (folder picker) are sharp too."""
    if sys.platform != "win32":
        return
    import ctypes

    attempts = (
        lambda: ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)),  # per-monitor v2
        lambda: ctypes.windll.shcore.SetProcessDpiAwareness(2) == 0,  # per-monitor (Windows 8.1)
        lambda: ctypes.windll.user32.SetProcessDPIAware(),  # system aware (Vista)
    )
    for attempt in attempts:
        try:
            if attempt():
                return
        except (AttributeError, OSError):
            pass  # not on this Windows version, or already set: try the next one


def choose_role() -> Role | None:
    import tkinter as tk

    _enable_dpi_awareness()
    root = tk.Tk()
    try:
        chooser = _RoleChooser(root)
        root.mainloop()
        return chooser.selection.role
    finally:
        root.destroy()
