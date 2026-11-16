"""Small tkinter role chooser shown when the unified executable is double-clicked."""

from typing import Literal


def choose_role() -> Literal["host", "agent"] | None:
    import tkinter as tk

    role: Literal["host", "agent"] | None = None
    root = tk.Tk()
    root.title("codex-bridge")
    root.resizable(False, False)
    tk.Label(root, text="この PC での使い方を選んでください。", padx=20, pady=16).pack(anchor="w")

    def choose(value: Literal["host", "agent"]) -> None:
        nonlocal role
        role = value
        root.destroy()

    tk.Button(root, text="使う側 — 自分の PC のフォルダを、ほかの人の Codex に使ってもらう",
              command=lambda: choose("host"), padx=16, pady=18).pack(fill="x", padx=20, pady=(0, 12))
    tk.Button(root, text="貸す側 — 自分の Codex を、ほかの人に使ってもらう",
              command=lambda: choose("agent"), padx=16, pady=18).pack(fill="x", padx=20, pady=(0, 20))
    root.protocol("WM_DELETE_WINDOW", root.destroy)
    root.bind("<Escape>", lambda event: root.destroy())
    root.mainloop()
    return role
