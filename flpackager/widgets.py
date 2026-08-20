"""Small reusable pieces of the Crate skin.

Nothing here knows what a project is -- these are pure widgets, so both the
Library and the Project page can share them.
"""

from __future__ import annotations

from typing import Callable, Optional

import customtkinter as ctk

from . import theme as T


def pill(master, text: str, color: str, *, on: str = T.BG_CARD) -> ctk.CTkLabel:
    """A rounded status chip: coloured text on a dimmed wash of the same hue."""
    return ctk.CTkLabel(
        master,
        text=text,
        font=T.F_SMALL,
        text_color=color,
        fg_color=T.blend(color, on, 0.16),
        corner_radius=9,
        height=20,
        padx=9,
    )


def meta_chip(master, label: str, value: str, *, mono: bool = False, sub: str = "") -> ctk.CTkFrame:
    """A bordered chip holding a small caption over a value (BPM, Key, ...)."""
    box = ctk.CTkFrame(
        master, fg_color=T.BG_INSET, corner_radius=8, border_width=1, border_color=T.BORDER
    )
    inner = ctk.CTkFrame(box, fg_color="transparent")
    inner.pack(padx=11, pady=(5, 6))
    ctk.CTkLabel(
        inner, text=label.upper(), font=T.F_TINY, text_color=T.TEXT_DIM, height=13
    ).pack(anchor="w")
    line = ctk.CTkFrame(inner, fg_color="transparent")
    line.pack(anchor="w")
    ctk.CTkLabel(
        line,
        text=value,
        font=T.F_NUM if mono else T.F_BODY_BOLD,
        text_color=T.TEXT,
        height=17,
    ).pack(side="left")
    if sub:
        ctk.CTkLabel(
            line, text=f"  {sub}", font=T.F_TINY, text_color=T.TEXT_DIM, height=17
        ).pack(side="left")
    return box


def dot(master, color: str, size: int = 8) -> ctk.CTkFrame:
    """A small round status dot."""
    return ctk.CTkFrame(
        master, width=size, height=size, corner_radius=size // 2, fg_color=color
    )


def badge(master, letter: str, *, size: int = 34, color: str = T.ACCENT,
          text_color: str = "#1a1205", font=None) -> ctk.CTkLabel:
    """The orange rounded square used for the logo and the project badge."""
    return ctk.CTkLabel(
        master,
        text=letter,
        width=size,
        height=size,
        corner_radius=max(6, size // 4),
        fg_color=color,
        text_color=text_color,
        font=font or (T.UI, max(11, size // 2), "bold"),
    )


def ghost_button(master, text: str, command: Callable[[], None], *, width: int = 0) -> ctk.CTkButton:
    """Neutral secondary button -- grey, bordered, never accent-coloured."""
    return ctk.CTkButton(
        master,
        text=text,
        command=command,
        font=T.F_SMALL,
        height=28,
        width=width or 0,
        corner_radius=7,
        fg_color=T.BG_CARD,
        hover_color=T.BG_CARD_HOVER,
        text_color=T.TEXT,
        border_width=1,
        border_color=T.BORDER,
    )


def accent_button(master, text: str, command: Callable[[], None]) -> ctk.CTkButton:
    """The one loud control on screen: Package & Send."""
    return ctk.CTkButton(
        master,
        text=text,
        command=command,
        font=(T.UI, 13, "bold"),
        height=36,
        corner_radius=8,
        fg_color=T.ACCENT,
        hover_color=T.ACCENT_HOVER,
        text_color="#20150a",
    )


def bind_hover(frame: ctk.CTkFrame, normal: str, hover: str,
               on_click: Optional[Callable[[], None]] = None) -> None:
    """Make a whole row light up as one, children included.

    Tk delivers <Leave> when the pointer crosses onto a child, so a naive bind
    flickers; we re-check what's actually under the cursor before reverting.
    """

    def descendants(widget):
        out = [widget]
        for child in widget.winfo_children():
            out.extend(descendants(child))
        return out

    def enter(_event=None):
        frame.configure(fg_color=hover)

    def leave(_event=None):
        try:
            pointer = frame.winfo_containing(
                frame.winfo_pointerx(), frame.winfo_pointery()
            )
        except Exception:
            pointer = None
        if pointer is None or not str(pointer).startswith(str(frame)):
            frame.configure(fg_color=normal)

    for widget in descendants(frame):
        widget.bind("<Enter>", enter, add="+")
        widget.bind("<Leave>", leave, add="+")
        if on_click is not None:
            widget.bind("<Button-1>", lambda _e: on_click(), add="+")
            try:
                widget.configure(cursor="hand2")
            except Exception:
                pass
