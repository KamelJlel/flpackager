"""Visual constants for the Crate skin.

Pure presentation: no packaging logic, no imports from :mod:`flpackager.core`.
Keeping the palette in one place means the GUI modules never hardcode a hex
value, and a future light theme is a matter of swapping this table.
"""

from __future__ import annotations

# --- surfaces --------------------------------------------------------------
BG_TITLE = "#141518"
BG_MAIN = "#1f2024"
BG_SIDE = "#17181b"
BG_CARD = "#292b31"
BG_CARD_HOVER = "#33353c"
BG_INSET = "#1a1b1e"
BORDER = "#34363d"

# --- type ------------------------------------------------------------------
TEXT = "#e9e9ec"
TEXT_MUTED = "#9b9ca2"
TEXT_DIM = "#6a6b71"

# --- accent, used sparingly: logo, primary action, active tab --------------
# The mockup pairs #ff8a1e with #ff5c39 as a gradient; CustomTkinter fills are
# flat, so the solid orange stands in for it.
ACCENT = "#ff8a1e"
ACCENT_HOVER = "#ffa04a"
# Dark ink used for text/glyphs that sit on top of the orange accent.
ACCENT_INK = "#1c1206"

# --- status ----------------------------------------------------------------
GREEN = "#69cf5c"
BLUE = "#4aa3ff"
RED = "#ff5f57"
AMBER = "#d9a441"

# --- fonts -----------------------------------------------------------------
# Tuples rather than CTkFont objects: a CTkFont needs a live Tk root, and these
# get referenced at class-definition time in places.
UI = "Segoe UI"
MONO = "Consolas"

F_LOGO = (UI, 14, "bold")
F_TITLE = (UI, 22, "bold")
F_H2 = (UI, 15, "bold")
F_BODY = (UI, 13)
F_BODY_BOLD = (UI, 13, "bold")
F_SMALL = (UI, 11)
F_TINY = (UI, 10)
F_NAV = (UI, 13)

# Numbers read as instrument readouts, so they get the monospace face.
F_NUM = (MONO, 13)
F_NUM_SMALL = (MONO, 11)


def blend(fg: str, bg: str, t: float) -> str:
    """Mix ``fg`` over ``bg`` at ratio ``t`` (0..1) and return a #rrggbb string.

    CustomTkinter has no alpha, so translucent pills from the mockup are baked
    down to opaque colours against the surface they sit on.
    """
    fr, fgc, fb = int(fg[1:3], 16), int(fg[3:5], 16), int(fg[5:7], 16)
    br, bgc, bb = int(bg[1:3], 16), int(bg[3:5], 16), int(bg[5:7], 16)
    mix = (
        round(fr * t + br * (1 - t)),
        round(fgc * t + bgc * (1 - t)),
        round(fb * t + bb * (1 - t)),
    )
    return "#%02x%02x%02x" % mix
