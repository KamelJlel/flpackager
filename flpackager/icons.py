"""Icons and the app mark, rendered with Pillow.

Two sources, no bundled art:

* Button glyphs come from **Segoe MDL2 Assets**, the icon font that ships on
  every Windows 10/11 machine -- so the icons are the same crisp, native marks
  the OS uses, and nothing has to travel with the binary.
* The app logo is drawn here: a rounded square with the flpackager orange
  gradient and an ``f`` monogram, reused for the title bar and the window icon.

Everything is cached and returned as :class:`customtkinter.CTkImage`, which
handles HiDPI scaling. On the rare machine without the font (or without a
display), the loaders fail soft and callers fall back to text.
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Optional

import customtkinter as ctk

from . import theme as T

try:
    from PIL import Image, ImageDraw, ImageFont, ImageTk  # type: ignore
    _PIL = True
except Exception:  # pragma: no cover - Pillow always present in the build
    _PIL = False

_MDL2 = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts", "segmdl2.ttf")

# Named MDL2 codepoints, verified to render as real glyphs on Windows 10.
GLYPHS = {
    "search": "\uE721",
    "settings": "\uE713",
    "back": "\uE72B",
    "upload": "\uE898",     # Package & Send
    "download": "\uE896",   # open a package
    "folder": "\uE8B7",     # change / choose folder
    "openfile": "\uE8E5",   # open an .flp
    "reveal": "\uE838",     # reveal in folder
    "refresh": "\uE72C",
    "check": "\uE73E",
    "error": "\uE783",
    "add": "\uE710",
}

# Render everything at 2x and let CTkImage scale it back down, so icons stay
# sharp on high-DPI displays.
_SS = 2


@lru_cache(maxsize=256)
def _mdl2_raster(glyph: str, px: int, color: str):
    font = ImageFont.truetype(_MDL2, px * _SS)
    img = Image.new("RGBA", (px * _SS, px * _SS), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    # Centre the glyph in its box; MDL2 metrics are already square-ish.
    bbox = draw.textbbox((0, 0), glyph, font=font)
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    x = (px * _SS - w) / 2 - bbox[0]
    y = (px * _SS - h) / 2 - bbox[1]
    draw.text((x, y), glyph, font=font, fill=color)
    return img


@lru_cache(maxsize=256)
def icon(name: str, size: int = 16, color: str = T.TEXT) -> Optional[ctk.CTkImage]:
    """A named MDL2 icon tinted ``color``, or ``None`` if unavailable."""
    if not _PIL:
        return None
    glyph = GLYPHS.get(name)
    if glyph is None:
        return None
    try:
        img = _mdl2_raster(glyph, size, color)
        return ctk.CTkImage(light_image=img, dark_image=img, size=(size, size))
    except Exception:
        return None


def _rounded_gradient(px: int, radius_frac: float = 0.28):
    """A rounded square filled with the flpackager orange gradient."""
    s = px * _SS
    grad = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    a = _hex(T.ACCENT)
    b = _hex("#ff4326")
    for y in range(s):
        for_x_t = y / max(1, s - 1)
        # diagonal-ish: blend by (x+y). Approximate per-row for speed.
        row = Image.new("RGBA", (s, 1))
        for x in range(s):
            t = (x + y) / (2 * (s - 1))
            row.putpixel((x, 0), _mix(a, b, t))
        grad.paste(row, (0, y))
    mask = Image.new("L", (s, s), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, s - 1, s - 1], radius=int(s * radius_frac), fill=255
    )
    grad.putalpha(mask)
    return grad


def _mark(px: int):
    """The logo: gradient rounded square with a bold ``f`` monogram."""
    s = px * _SS
    img = _rounded_gradient(px)
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype(
            os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts", "segoeuib.ttf"),
            int(s * 0.62),
        )
    except Exception:
        font = ImageFont.load_default()
    glyph = "f"
    bbox = draw.textbbox((0, 0), glyph, font=font)
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    x = (s - w) / 2 - bbox[0]
    y = (s - h) / 2 - bbox[1] - s * 0.02
    draw.text((x, y), glyph, font=font, fill=T.ACCENT_INK)
    return img


@lru_cache(maxsize=16)
def logo(size: int = 26) -> Optional[ctk.CTkImage]:
    """The app mark as a CTkImage for in-window use (title bar, dialogs)."""
    if not _PIL:
        return None
    try:
        img = _mark(size)
        return ctk.CTkImage(light_image=img, dark_image=img, size=(size, size))
    except Exception:
        return None


_icon_photo_ref = None  # keep a reference so Tk doesn't garbage-collect it


def set_window_icon(root) -> None:
    """Give the window and taskbar a real logo instead of the Tk feather."""
    global _icon_photo_ref
    if not _PIL:
        return
    try:
        img = _mark(64)
        _icon_photo_ref = ImageTk.PhotoImage(img)
        root.iconphoto(True, _icon_photo_ref)
    except Exception:
        pass


# --- tiny colour helpers ---------------------------------------------------

def _hex(h: str):
    h = h.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def _mix(a, b, t: float):
    return (
        round(a[0] * (1 - t) + b[0] * t),
        round(a[1] * (1 - t) + b[1] * t),
        round(a[2] * (1 - t) + b[2] * t),
        255,
    )
