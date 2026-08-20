"""Desktop GUI for flpackager -- the "Crate" skin.

A thin presentation layer over :mod:`flpackager.core`, exactly like the CLI --
no packaging logic lives here.

Design notes:
* CustomTkinter over Tk, so the dark FL-inspired palette in :mod:`.theme`
  renders on Windows without a native-widget fight. It still packages into a
  self-contained .exe (see the README build steps).
* Drag-and-drop is used when ``tkinterdnd2`` happens to be installed, and
  silently falls back to the file picker when it isn't.
* All disk work runs on a worker thread; progress is marshalled back to the UI
  thread through a queue, so the window never freezes on a big sample set.
  The Library scan, packaging and unpacking all go through that same pump.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
import traceback
from typing import List, Optional

import tkinter as tk
from tkinter import filedialog

import customtkinter as ctk

from . import __version__, core, theme as T
from . import widgets as W
from . import icons as I

APP_NAME = "flpackager"
CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".flpackager-gui.json")

# Library row column widths, shared by the header and every row so they line up.
COL_BPM = 76
COL_KEY = 122
COL_SAMPLES = 96
COL_STATUS = 116

# Building a Library row costs a dozen widgets, and a big folder can deliver
# hundreds of them in a burst. Draining the whole queue in one tick starves the
# Tk event loop, so each tick takes a bounded bite and leaves the rest queued.
ROWS_PER_TICK = 5

# A folder walk is capped so an accidental "scan my whole drive" stays sane.
MAX_LIBRARY_PROJECTS = 2000

# Each row costs ~50ms to draw, so a huge folder shows its most recent slice
# rather than spending half a minute building rows nobody scrolls to.
MAX_LIBRARY_ROWS = 200

# Breathing room between project reads, so the UI thread gets the GIL back.
SCAN_YIELD = 0.05


def _default_output_dir() -> str:
    desktop = os.path.join(os.path.expanduser("~"), "Desktop")
    return desktop if os.path.isdir(desktop) else os.path.expanduser("~")


def _open_in_file_manager(path: str) -> None:
    """Reveal a folder in Explorer / Finder / the desktop file manager."""
    try:
        if sys.platform == "win32":
            os.startfile(path)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception:
        pass


def _load_config() -> dict:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fp:
            data = json.load(fp)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_config(data: dict) -> None:
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as fp:
            json.dump(data, fp)
    except Exception:
        pass  # a preference that won't persist isn't worth interrupting anyone


def _edited_ago(path: str) -> str:
    """'edited 3 days ago' -- friendlier than a timestamp in a list."""
    try:
        delta = time.time() - os.path.getmtime(path)
    except OSError:
        return "edited recently"
    minutes = delta / 60
    if minutes < 60:
        return "edited just now" if minutes < 2 else "edited %d min ago" % int(minutes)
    hours = minutes / 60
    if hours < 24:
        return "edited %d hour%s ago" % (int(hours), "" if int(hours) == 1 else "s")
    days = int(hours / 24)
    if days < 30:
        return "edited %d day%s ago" % (days, "" if days == 1 else "s")
    months = days // 30
    return "edited %d month%s ago" % (months, "" if months == 1 else "s")


def _shorten(path: str, limit: int = 44) -> str:
    return path if len(path) <= limit else "..." + path[-(limit - 3):]


# ===========================================================================
# Library
# ===========================================================================


class LibraryView(ctk.CTkFrame):
    """Folder of projects, one row each: name, BPM, key, samples, status."""

    def __init__(self, master, *, on_open, on_change_folder, on_open_package,
                 on_open_file, on_search=None):
        super().__init__(master, fg_color=T.BG_MAIN, corner_radius=0)
        self.on_open = on_open
        self.on_change_folder = on_change_folder
        self.on_search = on_search
        self.rows: List[ctk.CTkFrame] = []
        self.row_paths: List[str] = []
        self.cells: dict = {}

        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)

        # --- header ---
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=28, pady=(24, 0))
        header.columnconfigure(0, weight=1)

        ctk.CTkLabel(header, text="Library", font=T.F_TITLE, text_color=T.TEXT).grid(
            row=0, column=0, sticky="w"
        )

        controls = ctk.CTkFrame(header, fg_color="transparent")
        controls.grid(row=1, column=0, sticky="w", pady=(10, 0))

        self.folder_chip = ctk.CTkFrame(
            controls, fg_color=T.BG_INSET, corner_radius=8,
            border_width=1, border_color=T.BORDER,
        )
        self.folder_chip.pack(side="left")
        chip_inner = ctk.CTkFrame(self.folder_chip, fg_color="transparent")
        chip_inner.pack(padx=10, pady=6)
        W.dot(chip_inner, T.GREEN).pack(side="left", pady=1)
        self.folder_label = ctk.CTkLabel(
            chip_inner, text="", font=T.F_SMALL, text_color=T.TEXT_MUTED, height=14
        )
        self.folder_label.pack(side="left", padx=(8, 0))

        W.ghost_button(controls, "Change folder", on_change_folder, icon="folder").pack(
            side="left", padx=(10, 0)
        )
        # Keeps the old click-to-browse route alive: a project doesn't have to
        # live in the library folder to be packaged.
        W.ghost_button(controls, "Open .flp...", on_open_file, icon="openfile").pack(side="left", padx=(8, 0))
        W.ghost_button(controls, "Open a package...", on_open_package, icon="download").pack(
            side="left", padx=(8, 0)
        )

        # Search: the way to work a big folder without scrolling hundreds of rows.
        self.search_var = tk.StringVar()
        self.search = ctk.CTkEntry(
            controls, textvariable=self.search_var, placeholder_text="Search projects...",
            font=T.F_SMALL, height=28, width=190, corner_radius=7,
            fg_color=T.BG_INSET, border_color=T.BORDER, text_color=T.TEXT,
        )
        self.search.pack(side="left", padx=(16, 0))
        if self.on_search is not None:
            self.search_var.trace_add("write", lambda *_: self.on_search(self.search_var.get()))

        self.scan_label = ctk.CTkLabel(
            header, text="", font=T.F_SMALL, text_color=T.TEXT_DIM, height=16, anchor="w"
        )
        self.scan_label.grid(row=2, column=0, sticky="w", pady=(10, 0))

        # Shown only once drag-and-drop is confirmed working, so we never
        # promise a gesture the build can't deliver.
        self.dnd_hint = ctk.CTkLabel(
            header, text="", font=T.F_TINY, text_color=T.TEXT_DIM, height=14, anchor="w"
        )
        self.dnd_hint.grid(row=3, column=0, sticky="w", pady=(2, 0))

        # --- column headings ---
        # The headings live outside the scroll area, so their columns can't be
        # the same grid as the rows'. They get pinned to the real cell
        # positions by _sync_header() instead of guessed at.
        self.head = ctk.CTkFrame(self, fg_color="transparent", height=20)
        self.head.grid(row=1, column=0, sticky="ew", padx=28, pady=(6, 2))
        self.head.grid_propagate(False)

        self.heads = {}
        ctk.CTkLabel(
            self.head, text="PROJECT", font=T.F_TINY, text_color=T.TEXT_DIM,
            anchor="w", height=14,
        ).place(x=0, y=3)
        for key, label, width in [
            ("bpm", "BPM", COL_BPM),
            ("key", "KEY", COL_KEY),
            ("samples", "SAMPLES", COL_SAMPLES),
            ("status_cell", "STATUS", COL_STATUS),
        ]:
            heading = ctk.CTkLabel(
                self.head, text=label, font=T.F_TINY, text_color=T.TEXT_DIM,
                anchor="w", width=width, height=14,
            )
            heading.place(x=0, y=3)
            self.heads[key] = heading

        # --- rows ---
        self.list = ctk.CTkScrollableFrame(
            self, fg_color="transparent", corner_radius=0,
            scrollbar_button_color=T.BORDER, scrollbar_button_hover_color=T.TEXT_DIM,
        )
        self.list.grid(row=2, column=0, sticky="nsew", padx=(22, 16), pady=(0, 18))
        self.list.columnconfigure(0, weight=1)

        # Empty state: a message plus a real call-to-action, so a first-run
        # user staring at an empty Desktop folder has an obvious next step
        # instead of hunting for the "Change folder" chip up in the header.
        self.empty = ctk.CTkFrame(self.list, fg_color="transparent")
        self.empty_label = ctk.CTkLabel(
            self.empty, text="", font=T.F_BODY, text_color=T.TEXT_DIM, justify="left"
        )
        self.empty_label.pack(anchor="w")
        self.empty_button = W.accent_button(
            self.empty, "Choose your projects folder", self.on_change_folder, icon="folder"
        )
        self.empty_button.pack(anchor="w", pady=(14, 0))

        # Re-pin the headings whenever the list is resized.
        self.list.bind("<Configure>", lambda _e: self.after_idle(self._sync_header), add="+")

    # -- content ------------------------------------------------------------

    def _sync_header(self) -> None:
        """Line the column headings up with the cells of the first row."""
        if not self.rows:
            return
        cells = self.cells.get(self.row_paths[0])
        if not cells:
            return
        try:
            origin = self.head.winfo_rootx()
            for key, heading in self.heads.items():
                heading.place(x=max(0, cells[key].winfo_rootx() - origin), y=3)
        except Exception:
            pass  # geometry not settled yet; the next row add will retry

    def visible_range(self):
        """Indices of the rows currently on screen (rows are a uniform height)."""
        count = len(self.rows)
        if not count:
            return 0, -1
        try:
            top, bottom = self.list._parent_canvas.yview()
        except Exception:
            return 0, min(count - 1, 12)
        return int(top * count), min(count - 1, int(bottom * count) + 1)

    def set_folder(self, folder: str) -> None:
        self.folder_label.configure(text=_shorten(folder))

    def set_scan_status(self, text: str) -> None:
        self.scan_label.configure(text=text)

    def show_dnd_hint(self) -> None:
        self.dnd_hint.configure(text="tip · drag an .flp anywhere in this window to open it")

    def clear(self) -> None:
        for row in self.rows:
            row.destroy()
        self.rows = []
        self.row_paths = []
        self.cells = {}
        self.empty.grid_forget()

    def show_empty(self, message: str) -> None:
        self.empty_label.configure(text=message)
        self.empty.grid(row=0, column=0, sticky="w", padx=6, pady=18)

    def add_row(self, path: str) -> None:
        """Create a row straight from the filesystem -- no parsing yet.

        Reading a .flp is expensive, so the list appears immediately and each
        row's numbers arrive later via :meth:`update_row`.
        """
        row = ctk.CTkFrame(self.list, fg_color=T.BG_CARD, corner_radius=10, height=58)
        row.grid(row=len(self.rows), column=0, sticky="ew", pady=3, padx=6)
        row.columnconfigure(0, weight=1)
        row.grid_propagate(False)

        left = ctk.CTkFrame(row, fg_color="transparent")
        left.grid(row=0, column=0, sticky="w", padx=(12, 10), pady=10)
        W.badge(
            left, "♪", size=32, color=T.BG_INSET,
            text_color=T.TEXT_MUTED, font=(T.UI, 15),
        ).pack(side="left")
        names = ctk.CTkFrame(left, fg_color="transparent")
        names.pack(side="left", padx=(11, 0))
        title = ctk.CTkLabel(
            names, text=os.path.splitext(os.path.basename(path))[0], font=T.F_BODY_BOLD,
            text_color=T.TEXT, anchor="w", height=17,
        )
        title.pack(anchor="w")
        ctk.CTkLabel(
            names, text=_edited_ago(path), font=T.F_SMALL,
            text_color=T.TEXT_DIM, anchor="w", height=15,
        ).pack(anchor="w")

        cells = {"title": title}
        for index, (key, width, font, color) in enumerate(
            [
                ("bpm", COL_BPM, T.F_NUM, T.TEXT),
                ("key", COL_KEY, T.F_BODY, T.TEXT_MUTED),
                ("samples", COL_SAMPLES, T.F_NUM, T.TEXT_MUTED),
            ],
            start=1,
        ):
            label = ctk.CTkLabel(
                row, text="--", font=font, text_color=T.TEXT_DIM,
                anchor="w", width=width, height=18,
            )
            label.grid(row=0, column=index, sticky="w", padx=(14, 0))
            cells[key] = label
            cells[key + "_color"] = color

        status_cell = ctk.CTkFrame(row, fg_color="transparent", width=COL_STATUS)
        status_cell.grid(row=0, column=4, sticky="w", padx=(14, 12))
        status = ctk.CTkLabel(
            status_cell, text="reading...", font=T.F_SMALL, text_color=T.TEXT_DIM, height=20
        )
        status.pack(anchor="w")
        cells["status_cell"] = status_cell
        cells["status"] = status

        W.bind_hover(row, T.BG_CARD, T.BG_CARD_HOVER, lambda p=path: self.on_open(p))

        self.rows.append(row)
        self.row_paths.append(path)
        self.cells[path] = cells

        # The headings follow the first row's cells, so re-pin them whenever
        # that row's geometry settles or changes.
        if len(self.rows) == 1:
            row.bind("<Configure>", lambda _e: self.after_idle(self._sync_header), add="+")
        self.after_idle(self._sync_header)

    def update_row(self, path: str, analysis: Optional[core.ProjectAnalysis],
                   error: str = "") -> None:
        """Fill in a row once its project has been read."""
        cells = self.cells.get(path)
        if cells is None:
            return

        if analysis is None:
            cells["status"].destroy()
            cells["status"] = W.pill(cells["status_cell"], error or "Unreadable", T.RED)
            cells["status"].pack(anchor="w")
            return

        if analysis.project_name:
            cells["title"].configure(text=analysis.project_name)
        cells["bpm"].configure(
            text=("%g" % analysis.tempo) if analysis.tempo is not None else "--",
            text_color=cells["bpm_color"],
        )
        cells["key"].configure(
            text=analysis.key.key if analysis.key else "--",
            text_color=cells["key_color"],
        )
        cells["samples"].configure(
            text=str(len(analysis.samples)), text_color=cells["samples_color"]
        )

        cells["status"].destroy()
        if analysis.missing:
            cells["status"] = W.pill(
                cells["status_cell"], "%d missing" % len(analysis.missing), T.AMBER
            )
        else:
            cells["status"] = ctk.CTkLabel(
                cells["status_cell"], text="Ready", font=T.F_SMALL,
                text_color=T.TEXT_MUTED, height=20,
            )
        cells["status"].pack(anchor="w")


# ===========================================================================
# Project page
# ===========================================================================


class ProjectView(ctk.CTkFrame):
    """One project in detail: meta chips, the package action, and its contents."""

    def __init__(self, master, *, on_back, on_package, on_change_output):
        super().__init__(master, fg_color=T.BG_MAIN, corner_radius=0)
        self.on_package = on_package
        self.analysis: Optional[core.ProjectAnalysis] = None
        self.active_tab = "samples"
        self.sample_rows: List[ctk.CTkFrame] = []
        self.plugin_rows: List[ctk.CTkFrame] = []

        self.columnconfigure(0, weight=1)
        self.rowconfigure(3, weight=1)

        # --- back link ---
        back = ctk.CTkLabel(
            self, text=" Library", font=T.F_SMALL, text_color=T.TEXT_MUTED,
            cursor="hand2", height=16, image=I.icon("back", 14, T.TEXT_MUTED), compound="left",
        )
        back.grid(row=0, column=0, sticky="w", padx=28, pady=(20, 0))
        back.bind("<Button-1>", lambda _e: on_back())
        back.bind("<Enter>", lambda _e: back.configure(text_color=T.TEXT))
        back.bind("<Leave>", lambda _e: back.configure(text_color=T.TEXT_MUTED))

        # --- header ---
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=1, column=0, sticky="ew", padx=28, pady=(14, 0))
        header.columnconfigure(0, weight=1)

        left = ctk.CTkFrame(header, fg_color="transparent")
        left.grid(row=0, column=0, sticky="w")

        title_line = ctk.CTkFrame(left, fg_color="transparent")
        title_line.pack(anchor="w")
        W.badge(title_line, "♪", size=38, font=(T.UI, 17, "bold")).pack(side="left")
        self.title = ctk.CTkLabel(
            title_line, text="", font=T.F_TITLE, text_color=T.TEXT, height=30
        )
        self.title.pack(side="left", padx=(12, 0))

        self.chips = ctk.CTkFrame(left, fg_color="transparent")
        self.chips.pack(anchor="w", pady=(12, 0))

        right = ctk.CTkFrame(header, fg_color="transparent")
        right.grid(row=0, column=1, sticky="e")
        self.pack_button = W.accent_button(right, "Package & Send", on_package, icon="upload")
        self.pack_button.configure(state="disabled")
        self.pack_button.pack(anchor="e")
        self.summary = ctk.CTkLabel(
            right, text="", font=T.F_SMALL, text_color=T.TEXT_DIM, height=15
        )
        self.summary.pack(anchor="e", pady=(7, 0))
        self.output_line = ctk.CTkLabel(
            right, text="", font=T.F_TINY, text_color=T.TEXT_DIM, height=14, cursor="hand2"
        )
        self.output_line.pack(anchor="e")
        self.output_line.bind("<Button-1>", lambda _e: on_change_output())

        # --- tabs ---
        tabbar = ctk.CTkFrame(self, fg_color="transparent", height=38)
        tabbar.grid(row=2, column=0, sticky="ew", padx=28, pady=(20, 0))
        tabbar.grid_propagate(False)
        rule = ctk.CTkFrame(self, fg_color=T.BORDER, height=1)
        rule.grid(row=2, column=0, sticky="sew", padx=28)

        self.tab_buttons = {}
        self.tab_marks = {}
        for index, (key, label) in enumerate([("samples", "Samples"), ("plugins", "Plugins")]):
            holder = ctk.CTkFrame(tabbar, fg_color="transparent")
            holder.grid(row=0, column=index, sticky="w", padx=(0, 26))
            button = ctk.CTkLabel(
                holder, text=label, font=T.F_BODY, text_color=T.TEXT_MUTED,
                cursor="hand2", height=26,
            )
            button.pack()
            mark = ctk.CTkFrame(holder, fg_color="transparent", height=2, width=58)
            mark.pack(fill="x")
            button.bind("<Button-1>", lambda _e, k=key: self.select_tab(k))
            self.tab_buttons[key] = button
            self.tab_marks[key] = mark

        # --- tab bodies ---
        self.body = ctk.CTkFrame(self, fg_color="transparent")
        self.body.grid(row=3, column=0, sticky="nsew", padx=(22, 16), pady=(10, 18))
        self.body.columnconfigure(0, weight=1)
        self.body.rowconfigure(0, weight=1)

        self.samples_list = ctk.CTkScrollableFrame(
            self.body, fg_color="transparent", corner_radius=0,
            scrollbar_button_color=T.BORDER, scrollbar_button_hover_color=T.TEXT_DIM,
        )
        self.samples_list.columnconfigure(0, weight=1)
        self.plugins_list = ctk.CTkScrollableFrame(
            self.body, fg_color="transparent", corner_radius=0,
            scrollbar_button_color=T.BORDER, scrollbar_button_hover_color=T.TEXT_DIM,
        )
        self.plugins_list.columnconfigure(0, weight=1)

        self.select_tab("samples")

    # -- tabs ---------------------------------------------------------------

    def select_tab(self, key: str) -> None:
        self.active_tab = key
        for name, button in self.tab_buttons.items():
            active = name == key
            button.configure(
                text_color=T.TEXT if active else T.TEXT_MUTED,
                font=T.F_BODY_BOLD if active else T.F_BODY,
            )
            self.tab_marks[name].configure(fg_color=T.ACCENT if active else "transparent")
        self.samples_list.grid_forget()
        self.plugins_list.grid_forget()
        target = self.samples_list if key == "samples" else self.plugins_list
        target.grid(row=0, column=0, sticky="nsew")

    # -- content ------------------------------------------------------------

    def set_output_dir(self, path: str) -> None:
        self.output_line.configure(text="Save to: %s  ·  change" % _shorten(path, 38))

    def show(self, analysis: core.ProjectAnalysis) -> None:
        self.analysis = analysis
        self.title.configure(text=analysis.project_name or "Untitled project")
        self.pack_button.configure(state="normal")

        for child in self.chips.winfo_children():
            child.destroy()
        bpm = ("%g" % analysis.tempo) if analysis.tempo is not None else "--"
        W.meta_chip(self.chips, "BPM", bpm, mono=True).pack(side="left", padx=(0, 8))
        if analysis.key:
            W.meta_chip(self.chips, "Key", analysis.key.key, sub="estimated").pack(
                side="left", padx=(0, 8)
            )
        else:
            W.meta_chip(self.chips, "Key", "unknown").pack(side="left", padx=(0, 8))
        W.meta_chip(self.chips, "Samples", str(len(analysis.samples)), mono=True).pack(
            side="left", padx=(0, 8)
        )
        W.meta_chip(self.chips, "Plugins", str(len(analysis.plugins)), mono=True).pack(
            side="left", padx=(0, 8)
        )

        size = core._human_bytes(analysis.total_bundle_bytes)
        self.summary.configure(
            text="Package will include: %d samples · %s"
            % (len(analysis.unique_bundled), size)
        )

        self._fill_samples(analysis)
        self._fill_plugins(analysis)
        self.select_tab("samples")

    def _notes(self, analysis: core.ProjectAnalysis) -> List[str]:
        """The old Notes tab, condensed into banners above the lists."""
        notes = []
        if analysis.missing:
            notes.append(
                "%d sample(s) could not be found on this machine. They'll be listed in "
                "manifest.txt so whoever opens the project knows what's absent and where "
                "it used to live. Packaging still works." % len(analysis.missing)
            )
        renamed = [ref for ref in analysis.samples if "renamed" in ref.note]
        if renamed:
            notes.append(
                "%d sample(s) were renamed to avoid a filename clash between different "
                "folders." % len(renamed)
            )
        if analysis.warnings:
            notes.append("Parser warnings: " + "; ".join(analysis.warnings))
        return notes

    def _banner(self, master, text: str, color: str, row: int) -> None:
        frame = ctk.CTkFrame(
            master, fg_color=T.blend(color, T.BG_MAIN, 0.10), corner_radius=9,
            border_width=1, border_color=T.blend(color, T.BG_MAIN, 0.30),
        )
        frame.grid(row=row, column=0, sticky="ew", pady=(0, 8), padx=6)
        ctk.CTkLabel(
            frame, text=text, font=T.F_SMALL, text_color=T.TEXT_MUTED,
            justify="left", wraplength=740, anchor="w",
        ).pack(anchor="w", padx=12, pady=9)

    def _fill_samples(self, analysis: core.ProjectAnalysis) -> None:
        for child in self.samples_list.winfo_children():
            child.destroy()
        self.sample_rows = []

        index = 0
        for note in self._notes(analysis):
            self._banner(self.samples_list, note, T.AMBER if analysis.missing else T.BLUE, index)
            index += 1

        if not analysis.samples:
            ctk.CTkLabel(
                self.samples_list, text="This project references no samples.",
                font=T.F_BODY, text_color=T.TEXT_DIM,
            ).grid(row=index, column=0, sticky="w", padx=6, pady=12)
            return

        for ref in analysis.samples:
            row = ctk.CTkFrame(self.samples_list, fg_color=T.BG_CARD, corner_radius=10, height=52)
            row.grid(row=index, column=0, sticky="ew", pady=3, padx=6)
            row.columnconfigure(0, weight=1)
            row.grid_propagate(False)

            names = ctk.CTkFrame(row, fg_color="transparent")
            names.grid(row=0, column=0, sticky="w", padx=(14, 10), pady=8)
            filename = (
                ref.bundled_name
                or os.path.basename(ref.original_path.replace("\\", "/"))
                or ref.original_path
            )
            ctk.CTkLabel(
                names, text=filename, font=T.F_BODY, text_color=T.TEXT, anchor="w", height=17
            ).pack(anchor="w")
            ctk.CTkLabel(
                names, text=_shorten(ref.original_path, 78), font=T.F_SMALL,
                text_color=T.TEXT_DIM, anchor="w", height=15,
            ).pack(anchor="w")

            ctk.CTkLabel(
                row,
                text=core._human_bytes(ref.size_bytes) if ref.size_bytes else "--",
                font=T.F_NUM_SMALL, text_color=T.TEXT_MUTED,
                anchor="e", width=78, height=18,
            ).grid(row=0, column=1, sticky="e", padx=(8, 14))

            if ref.status == core.FOUND:
                text, color = "Bundled", T.GREEN
            elif ref.status == core.BUILTIN:
                text, color = "Built-in", T.BLUE
            else:
                text, color = "Missing", T.RED
            cell = ctk.CTkFrame(row, fg_color="transparent", width=92)
            cell.grid(row=0, column=2, sticky="w", padx=(0, 14))
            W.pill(cell, text, color).pack(anchor="w")

            W.bind_hover(row, T.BG_CARD, T.BG_CARD_HOVER)
            self.sample_rows.append(row)
            index += 1

    def _fill_plugins(self, analysis: core.ProjectAnalysis) -> None:
        for child in self.plugins_list.winfo_children():
            child.destroy()
        self.plugin_rows = []

        self._banner(
            self.plugins_list,
            "Plugins aren't bundled -- they can't be. Whoever opens this project needs "
            "these installed, or the channels they sit on will come up empty. They're "
            "listed in manifest.txt too.",
            T.BLUE,
            0,
        )
        index = 1

        if not analysis.plugins:
            ctk.CTkLabel(
                self.plugins_list, text="No plugins in this project.",
                font=T.F_BODY, text_color=T.TEXT_DIM,
            ).grid(row=index, column=0, sticky="w", padx=6, pady=12)
            return

        for plugin in analysis.plugins:
            row = ctk.CTkFrame(self.plugins_list, fg_color=T.BG_CARD, corner_radius=10, height=52)
            row.grid(row=index, column=0, sticky="ew", pady=3, padx=6)
            row.columnconfigure(0, weight=1)
            row.grid_propagate(False)

            names = ctk.CTkFrame(row, fg_color="transparent")
            names.grid(row=0, column=0, sticky="w", padx=(14, 10), pady=8)
            ctk.CTkLabel(
                names, text=plugin.name, font=T.F_BODY, text_color=T.TEXT,
                anchor="w", height=17,
            ).pack(anchor="w")
            native = plugin.kind.lower() == "native"
            subtitle = plugin.vendor or ("ships with FL Studio" if native else "unknown maker")
            if plugin.used_in:
                subtitle += "  ·  " + ", ".join(plugin.used_in)
            ctk.CTkLabel(
                names, text=subtitle, font=T.F_SMALL, text_color=T.TEXT_DIM,
                anchor="w", height=15,
            ).pack(anchor="w")

            cell = ctk.CTkFrame(row, fg_color="transparent", width=104)
            cell.grid(row=0, column=1, sticky="e", padx=(0, 14))
            W.pill(
                cell, "Native" if native else "Third-party",
                T.GREEN if native else T.TEXT_MUTED,
            ).pack(anchor="e")

            W.bind_hover(row, T.BG_CARD, T.BG_CARD_HOVER)
            self.plugin_rows.append(row)
            index += 1


# ===========================================================================
# Package progress modal
# ===========================================================================


class ProgressModal(ctk.CTkToplevel):
    """Driven entirely by the existing progress(done, total, label) callback."""

    def __init__(self, parent, *, on_reveal):
        super().__init__(parent)
        self.on_reveal = on_reveal
        self.title("Packaging")
        self.configure(fg_color=T.BG_MAIN)
        self.resizable(False, False)
        self.protocol("WM_DELETE_WINDOW", self._maybe_close)
        self.done = False

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(padx=26, pady=24, fill="both", expand=True)

        self.icon = W.badge(body, "", size=40, font=(T.UI, 18, "bold"))
        _up = I.icon("upload", 20, T.ACCENT_INK)
        if _up is not None:
            self.icon.configure(image=_up)
        self.icon.pack(anchor="w")

        self.heading = ctk.CTkLabel(
            body, text="Packaging project", font=T.F_H2, text_color=T.TEXT, height=22
        )
        self.heading.pack(anchor="w", pady=(14, 0))

        self.detail = ctk.CTkLabel(
            body, text="Preparing...", font=T.F_SMALL, text_color=T.TEXT_MUTED,
            height=16, anchor="w", justify="left",
        )
        self.detail.pack(anchor="w", pady=(4, 0))

        self.bar = ctk.CTkProgressBar(
            body, width=380, height=6, corner_radius=3,
            fg_color=T.BG_INSET, progress_color=T.ACCENT,
        )
        self.bar.pack(anchor="w", pady=(16, 0))
        self.bar.set(0)
        self.bar.configure(mode="indeterminate")
        self.bar.start()

        self.count = ctk.CTkLabel(
            body, text="", font=T.F_NUM_SMALL, text_color=T.TEXT_DIM, height=15
        )
        self.count.pack(anchor="w", pady=(8, 0))

        self.actions = ctk.CTkFrame(body, fg_color="transparent")
        self.actions.pack(anchor="e", pady=(18, 0), fill="x")

        self._centre(parent)
        self.transient(parent)
        try:
            self.grab_set()
        except Exception:
            pass

    def _centre(self, parent) -> None:
        self.update_idletasks()
        try:
            x = parent.winfo_rootx() + (parent.winfo_width() - self.winfo_width()) // 2
            y = parent.winfo_rooty() + (parent.winfo_height() - self.winfo_height()) // 3
            self.geometry("+%d+%d" % (max(0, x), max(0, y)))
        except Exception:
            pass

    # -- state --------------------------------------------------------------

    def update_progress(self, done: int, total: int, label: str) -> None:
        if total:
            if str(self.bar.cget("mode")) == "indeterminate":
                self.bar.stop()
                self.bar.configure(mode="determinate")
            self.bar.set(min(1.0, done / float(total)))
            self.count.configure(text="%d / %d" % (done, total))
        self.detail.configure(text=_shorten(label, 56))

    def finish(self, message: str, sub: str) -> None:
        self.done = True
        self.bar.stop()
        self.bar.configure(mode="determinate", progress_color=T.GREEN)
        self.bar.set(1.0)
        self.icon.configure(text="", image=I.icon("check", 22, "#0f2410"), fg_color=T.GREEN)
        self.heading.configure(text=message)
        self.detail.configure(text=sub)
        self.count.configure(text="")
        W.ghost_button(self.actions, "Close", self._close).pack(side="right")
        W.ghost_button(self.actions, "Reveal in folder", self.on_reveal, icon="reveal").pack(
            side="right", padx=(0, 8)
        )

    def fail(self, message: str) -> None:
        self.done = True
        self.bar.stop()
        self.bar.configure(mode="determinate", progress_color=T.RED)
        self.bar.set(1.0)
        self.icon.configure(text="", image=I.icon("error", 22, "#2a0d0c"), fg_color=T.RED)
        self.heading.configure(text="Packaging failed")
        self.detail.configure(text=_shorten(message, 64))
        W.ghost_button(self.actions, "Close", self._close).pack(side="right")

    def _maybe_close(self) -> None:
        if self.done:
            self._close()

    def _close(self) -> None:
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()


# ===========================================================================
# Main window
# ===========================================================================


class PackagerApp:
    """Shell: title bar, sidebar, and the Library / Project views inside it."""

    def __init__(self, root, initial_file: Optional[str] = None) -> None:
        self.root = root
        self.analysis: Optional[core.ProjectAnalysis] = None
        self.flp_path: Optional[str] = None
        self.result: Optional[core.PackageResult] = None
        self.events: "queue.Queue[tuple]" = queue.Queue()
        self.busy = False
        self.modal: Optional[ProgressModal] = None
        self.scan_token = 0
        self.scan_results: dict = {}
        self.pending_rows: List[str] = []
        self.all_paths: List[str] = []      # every .flp found, for search
        self.search_text = ""
        self._search_job = None             # debounce handle
        self.analyze_requests: "queue.Queue[str]" = queue.Queue()
        self.requested: set = set()
        self.ticks = 0

        config = _load_config()
        self.output_var = tk.StringVar(value=config.get("output_dir") or _default_output_dir())
        self.library_dir = config.get("library_dir") or _default_output_dir()
        # First launch is anything before the welcome has been dismissed once.
        self.first_run = not config.get("seen_welcome", False)

        root.title("%s %s" % (APP_NAME, __version__))
        root.minsize(1060, 680)
        try:
            root.geometry("1180x760")
        except Exception:
            pass
        try:
            root.configure(fg_color=T.BG_MAIN)
        except Exception:
            root.configure(bg=T.BG_MAIN)

        self._build_ui()
        self._poll_events()

        if initial_file:
            self.library_dir = os.path.dirname(os.path.abspath(initial_file)) or self.library_dir
            self.load_project(initial_file)
        self.start_scan(self.library_dir)

        # Greet a brand-new user once the window has painted. Skipped when they
        # arrived by opening an .flp directly -- they already know what they want.
        if self.first_run and not initial_file:
            self.root.after(300, self._show_welcome)

    def _show_welcome(self) -> None:
        WelcomeModal(
            self.root,
            on_choose_folder=self.choose_library_folder,
            on_done=self._welcome_dismissed,
        )

    def _welcome_dismissed(self) -> None:
        self.first_run = False
        config = _load_config()
        config["seen_welcome"] = True
        _save_config(config)

    # -- layout -------------------------------------------------------------

    def _build_ui(self) -> None:
        root = self.root
        root.columnconfigure(0, weight=1)
        root.rowconfigure(1, weight=1)

        # --- title bar ---
        bar = ctk.CTkFrame(root, height=46, fg_color=T.BG_TITLE, corner_radius=0)
        bar.grid(row=0, column=0, sticky="ew")
        bar.grid_propagate(False)
        brand = ctk.CTkFrame(bar, fg_color="transparent")
        brand.place(x=16, rely=0.5, anchor="w")
        mark = I.logo(26)
        if mark is not None:
            ctk.CTkLabel(brand, text="", image=mark, width=26, height=26).pack(side="left")
        else:
            W.badge(brand, "f", size=24, font=(T.UI, 13, "bold")).pack(side="left")
        ctk.CTkLabel(brand, text=APP_NAME, font=T.F_LOGO, text_color=T.TEXT).pack(
            side="left", padx=(10, 0)
        )
        ctk.CTkLabel(
            bar, text="v%s" % __version__, font=T.F_TINY, text_color=T.TEXT_DIM
        ).place(relx=1.0, x=-16, rely=0.5, anchor="e")

        # One quiet status line for whatever the app last did, visible from both
        # views. Scan progress has its own line on the Library, so a scan
        # finishing in the background can't overwrite a result message here.
        self.status = ctk.CTkLabel(
            bar, text="", font=T.F_SMALL, text_color=T.TEXT_DIM, anchor="e", height=16
        )
        self.status.place(relx=1.0, x=-58, rely=0.5, anchor="e")

        # --- middle: sidebar + content ---
        middle = ctk.CTkFrame(root, fg_color="transparent", corner_radius=0)
        middle.grid(row=1, column=0, sticky="nsew")
        middle.columnconfigure(1, weight=1)
        middle.rowconfigure(0, weight=1)

        self._build_sidebar(middle)

        self.content = ctk.CTkFrame(middle, fg_color=T.BG_MAIN, corner_radius=0)
        self.content.grid(row=0, column=1, sticky="nsew")
        self.content.columnconfigure(0, weight=1)
        self.content.rowconfigure(0, weight=1)

        self.library = LibraryView(
            self.content,
            on_open=self.open_project,
            on_change_folder=self.choose_library_folder,
            on_open_package=self.open_unpack_window,
            on_open_file=self.choose_project,
            on_search=self.on_search,
        )
        self.library.set_folder(self.library_dir)
        self.project = ProjectView(
            self.content,
            on_back=self.show_library,
            on_package=self.start_packaging,
            on_change_output=self.choose_output,
        )
        self.project.set_output_dir(self.output_var.get())
        self.show_library()

        self.pack_button = self.project.pack_button
        self._enable_dnd()

    def _build_sidebar(self, master) -> None:
        side = ctk.CTkFrame(master, width=190, fg_color=T.BG_SIDE, corner_radius=0)
        side.grid(row=0, column=0, sticky="nsw")
        side.grid_propagate(False)
        side.rowconfigure(1, weight=1)

        nav = ctk.CTkFrame(side, fg_color="transparent", width=190)
        nav.grid(row=0, column=0, sticky="new", pady=(16, 0))

        # Sidebar destinations. Library shows the project list; Incoming opens
        # the receiving flow; Settings opens the preferences popover.
        self.nav_items = {}
        self.nav_bars = {}
        self.nav_labels = {}
        nav_actions = {
            "library": self.show_library,
            "incoming": self.open_unpack_window,
            "settings": self.open_settings,
        }
        for key, label, active in [
            ("library", "Library", True),
            ("incoming", "Incoming", False),
            ("settings", "Settings", False),
        ]:
            item = ctk.CTkFrame(
                nav, fg_color=T.BG_CARD if active else "transparent",
                corner_radius=8, height=34, width=166,
            )
            item.pack(padx=12, pady=2, fill="x")
            item.pack_propagate(False)
            bar = ctk.CTkFrame(
                item, width=3, height=18, corner_radius=2,
                fg_color=T.ACCENT if active else "transparent",
            )
            bar.place(x=0, rely=0.5, anchor="w")
            text = ctk.CTkLabel(
                item, text=label, font=T.F_NAV,
                text_color=T.TEXT if active else T.TEXT_DIM, anchor="w",
            )
            text.place(x=16, rely=0.5, anchor="w")
            self.nav_items[key] = item
            self.nav_bars[key] = bar
            self.nav_labels[key] = text

            # Hover + click on the whole row, children included.
            for widget in (item, text):
                widget.configure(cursor="hand2")
                widget.bind("<Button-1>", lambda _e, k=key: nav_actions[k](), add="+")
                widget.bind(
                    "<Enter>",
                    lambda _e, k=key: self._nav_hover(k, True), add="+",
                )
                widget.bind(
                    "<Leave>",
                    lambda _e, k=key: self._nav_hover(k, False), add="+",
                )

        ctk.CTkFrame(side, fg_color="transparent").grid(row=1, column=0, sticky="nsew")

        # Quiet brand + version footer, in place of the old (pointless) username
        # chip. Uses the real logo mark.
        chip = ctk.CTkFrame(side, fg_color="transparent", height=40, width=166)
        chip.grid(row=2, column=0, sticky="ew", padx=12, pady=14)
        chip.pack_propagate(False)
        mark = I.logo(22)
        if mark is not None:
            ctk.CTkLabel(chip, text="", image=mark, width=22, height=22).pack(
                side="left", padx=(4, 0)
            )
        else:
            W.badge(chip, "f", size=22, font=(T.UI, 12, "bold")).pack(side="left", padx=(4, 0))
        meta = ctk.CTkFrame(chip, fg_color="transparent")
        meta.pack(side="left", padx=(10, 0))
        ctk.CTkLabel(
            meta, text="flpackager", font=(T.UI, 12, "bold"), text_color=T.TEXT,
            anchor="w", height=15,
        ).pack(anchor="w")
        ctk.CTkLabel(
            meta, text="v%s" % __version__, font=T.F_TINY, text_color=T.TEXT_DIM,
            anchor="w", height=13,
        ).pack(anchor="w")

    def _enable_dnd(self) -> None:
        """Wire up drag-and-drop if tkinterdnd2 is available; ignore it if not."""
        try:
            from tkinterdnd2 import DND_FILES  # type: ignore

            self.content.drop_target_register(DND_FILES)  # type: ignore[attr-defined]
            self.content.dnd_bind("<<Drop>>", self._on_drop)  # type: ignore[attr-defined]
            self.library.show_dnd_hint()  # only advertise it once it works
        except Exception:
            pass  # picker-only; the Library's folder chip is the way in

    def _on_drop(self, event) -> None:
        raw = (event.data or "").strip()
        if raw.startswith("{") and raw.endswith("}"):
            raw = raw[1:-1]
        path = raw.split("} {")[0].strip()
        if path:
            self.load_project(path)

    # -- view switching -----------------------------------------------------

    def show_library(self) -> None:
        self.project.grid_forget()
        self.library.grid(row=0, column=0, sticky="nsew")

    def show_project(self) -> None:
        self.library.grid_forget()
        self.project.grid(row=0, column=0, sticky="nsew")

    # -- search / row population -------------------------------------------

    def on_search(self, text: str) -> None:
        """Debounced: re-render the list filtered by project name."""
        self.search_text = text
        if getattr(self, "_suppress_search", False):
            return  # programmatic reset (e.g. new folder scan), not a user query
        if self._search_job is not None:
            try:
                self.root.after_cancel(self._search_job)
            except Exception:
                pass
        self._search_job = self.root.after(180, self._populate_rows)

    def _populate_rows(self) -> None:
        """Rebuild the visible rows from the full list, honouring the search.

        Filtering the source list (rather than rendering all of it) is what
        keeps a 500-project folder responsive: type a few letters and only the
        matching rows are ever built.
        """
        self._search_job = None
        self.library.clear()
        self.requested = set()

        query = self.search_text.strip().lower()
        if query:
            paths = [
                p for p in self.all_paths
                if query in os.path.splitext(os.path.basename(p))[0].lower()
            ]
        else:
            paths = self.all_paths

        self.pending_rows = list(paths[:MAX_LIBRARY_ROWS])
        total = len(self.all_paths)

        if not total:
            self.library.set_scan_status("")
            self.library.show_empty(
                "No .flp files in this folder.\n"
                "Use “Change folder” to point flpackager at where you keep your projects."
            )
        elif not paths:
            self.library.set_scan_status('No projects match “%s”.' % self.search_text.strip())
        elif query:
            shown = min(len(paths), MAX_LIBRARY_ROWS)
            status = "%d of %d" % (shown, total) if len(paths) <= MAX_LIBRARY_ROWS \
                else "%d of %d matches · showing %d" % (len(paths), total, MAX_LIBRARY_ROWS)
            self.library.set_scan_status(status)
        else:
            found = "%d project%s" % (total, "" if total == 1 else "s")
            if total > MAX_LIBRARY_ROWS:
                found += " · showing the %d most recent · search to find the rest" % MAX_LIBRARY_ROWS
            self.library.set_scan_status(found)

    # -- sidebar ------------------------------------------------------------

    def _nav_hover(self, key: str, entering: bool) -> None:
        """Light a nav row on hover; the active one (Library) stays lit."""
        if key == "library":
            return  # persistently highlighted
        self.nav_items[key].configure(fg_color=T.BG_CARD if entering else "transparent")

    def open_settings(self) -> None:
        SettingsWindow(
            self.root,
            output_dir=self.output_var.get(),
            library_dir=self.library_dir,
            on_output=self._apply_output_dir,
            on_library=self._apply_library_dir,
        )

    def _apply_output_dir(self, path: str) -> None:
        self.output_var.set(path)
        config = _load_config()
        config["output_dir"] = path
        _save_config(config)
        self.project.set_output_dir(path)

    def _apply_library_dir(self, path: str) -> None:
        self.library_dir = path
        config = _load_config()
        config["library_dir"] = path
        _save_config(config)
        self.start_scan(path)
        self.show_library()

    # -- helpers ------------------------------------------------------------

    def set_status(self, text: str, error: bool = False) -> None:
        self.status.configure(text=text, text_color=T.RED if error else T.TEXT_DIM)

    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        self.pack_button.configure(state="disabled" if busy or not self.analysis else "normal")

    # -- library scan -------------------------------------------------------

    def choose_library_folder(self) -> None:
        path = filedialog.askdirectory(title="Where do you keep your projects?")
        if path:
            self.library_dir = path
            config = _load_config()
            config["library_dir"] = path
            _save_config(config)
            self.start_scan(path)

    def start_scan(self, folder: str) -> None:
        """Read every .flp in a folder, off the UI thread, rows appearing live."""
        self.scan_token += 1
        token = self.scan_token
        self.scan_results = {}
        self.pending_rows = []
        self.all_paths = []
        self.search_text = ""
        try:
            self._suppress_search = True
            self.library.search_var.set("")
        finally:
            self._suppress_search = False
        self.requested = set()
        self.analyze_requests = queue.Queue()
        self.library.clear()
        self.library.set_folder(folder)
        self.library.set_scan_status("Scanning...")
        threading.Thread(target=self._scan_worker, args=(folder, token), daemon=True).start()
        threading.Thread(target=self._meta_worker, args=(token,), daemon=True).start()

    def _scan_worker(self, folder: str, token: int) -> None:
        """List the folder. Reading the projects themselves is a separate job."""
        try:
            paths = []
            for root_dir, dirs, files in os.walk(folder):
                # Don't re-list projects that live inside packages we made.
                dirs[:] = [d for d in dirs if not d.endswith("_package")]
                for name in files:
                    if name.lower().endswith(".flp"):
                        paths.append(os.path.join(root_dir, name))
                if len(paths) >= MAX_LIBRARY_PROJECTS:
                    break

            def mtime(path):
                try:
                    return os.path.getmtime(path)
                except OSError:
                    return 0

            # Most recently worked-on first: the ones worth reading soonest.
            paths.sort(key=mtime, reverse=True)
            self.events.put(("scan_list", token, paths[:MAX_LIBRARY_PROJECTS]))
        except Exception as exc:
            self.events.put(("scan_error", token, str(exc)))

    def _meta_worker(self, token: int) -> None:
        """Read the projects the Library actually needs, one at a time.

        Parsing a .flp is CPU-bound and holds the GIL, so reading a whole
        folder up front would leave the window stuttering for minutes. Instead
        the UI asks for the rows on screen and this worker answers, sleeping
        between projects so the UI thread gets the interpreter back.
        """
        while token == self.scan_token:
            try:
                path = self.analyze_requests.get(timeout=0.25)
            except queue.Empty:
                continue
            if token != self.scan_token:
                return
            try:
                analysis = core.analyze_project(path)
                self.events.put(("scan_meta", token, path, analysis, ""))
            except Exception as exc:
                self.events.put(("scan_meta", token, path, None, str(exc)))
            time.sleep(SCAN_YIELD)

    def _request_visible(self) -> None:
        """Queue a read for any on-screen row whose numbers are still blank."""
        if not self.library.rows or self.analyze_requests.qsize() > 12:
            return
        first, last = self.library.visible_range()
        for path in self.library.row_paths[first:last + 2]:
            if path in self.scan_results or path in self.requested:
                continue
            self.requested.add(path)
            self.analyze_requests.put(path)

    # -- opening a project --------------------------------------------------

    def open_project(self, path: str) -> None:
        """Row click: the scan already analysed this one, so it opens instantly."""
        analysis = self.scan_results.get(path)
        if analysis is None:
            self.load_project(path)
            return
        self.flp_path = path
        self.analysis = analysis
        self.result = None
        self.project.show(analysis)
        self.project.set_output_dir(self.output_var.get())
        self._set_busy(False)
        self.show_project()

    def choose_project(self) -> None:
        if self.busy:
            return
        path = filedialog.askopenfilename(
            title="Choose an FL Studio project",
            filetypes=[("FL Studio project", "*.flp"), ("All files", "*.*")],
        )
        if path:
            self.load_project(path)

    def load_project(self, path: str) -> None:
        if self.busy:
            return
        if not os.path.isfile(path) or not path.lower().endswith(".flp"):
            self.set_status("That doesn't look like an .flp file.", error=True)
            return

        self.flp_path = path
        self.result = None
        self.set_status("Reading project...")
        self._set_busy(True)
        threading.Thread(target=self._analyze_worker, args=(path,), daemon=True).start()

    def _analyze_worker(self, path: str) -> None:
        try:
            analysis = core.analyze_project(path)
            self.events.put(("analyzed", analysis))
        except Exception as exc:
            self.events.put(
                ("error", "Couldn't read that project: %s" % exc, traceback.format_exc())
            )

    def _show_analysis(self, analysis: core.ProjectAnalysis) -> None:
        self.analysis = analysis
        self.scan_results[analysis.flp_path] = analysis
        self.project.show(analysis)
        self.project.set_output_dir(self.output_var.get())

        counts = "%d to bundle (%s)" % (
            len(analysis.unique_bundled),
            core._human_bytes(analysis.total_bundle_bytes),
        )
        if analysis.missing:
            counts += "  ·  %d missing" % len(analysis.missing)
        if analysis.builtin:
            counts += "  ·  %d built-in" % len(analysis.builtin)
        self.set_status(counts)
        self._set_busy(False)
        self.show_project()

    # -- packaging ----------------------------------------------------------

    def choose_output(self) -> None:
        path = filedialog.askdirectory(title="Where should the package go?")
        if path:
            self.output_var.set(path)
            config = _load_config()
            config["output_dir"] = path
            _save_config(config)
            self.project.set_output_dir(path)

    def start_packaging(self) -> None:
        if self.busy or not self.analysis:
            return
        output_dir = self.output_var.get().strip()
        if not output_dir:
            self.set_status("Pick a folder to save into.", error=True)
            return

        self._set_busy(True)
        self.result = None
        try:
            self.modal = ProgressModal(self.root, on_reveal=self.reveal_result)
        except Exception:
            self.modal = None  # headless / no display: packaging still runs
        threading.Thread(
            target=self._package_worker, args=(self.analysis, output_dir), daemon=True
        ).start()

    def _package_worker(self, analysis: core.ProjectAnalysis, output_dir: str) -> None:
        def progress(done: int, total: int, label: str) -> None:
            self.events.put(("progress", done, total, label))

        try:
            os.makedirs(output_dir, exist_ok=True)
            result = core.build_package(analysis, output_dir, progress=progress)
            self.events.put(("packaged", result))
        except Exception as exc:
            self.events.put(("error", "Packaging failed: %s" % exc, traceback.format_exc()))

    def _live_modal(self) -> Optional[ProgressModal]:
        if self.modal is None:
            return None
        try:
            return self.modal if self.modal.winfo_exists() else None
        except Exception:
            return None

    def _show_result(self, result: core.PackageResult) -> None:
        self.result = result
        size = ""
        if result.zip_path and os.path.isfile(result.zip_path):
            size = "  (%s)" % core._human_bytes(os.path.getsize(result.zip_path))
        name = os.path.basename(result.zip_path or result.bundle_dir or "")
        sub = "%s%s" % (name, size)
        if result.analysis.missing:
            sub += "  ·  %d missing, see manifest.txt" % len(result.analysis.missing)
        modal = self._live_modal()
        if modal is not None:
            modal.finish("Package ready", sub)
        self.set_status("Done -- %s%s is ready to send." % (name, size))

    def reveal_result(self) -> None:
        if not self.result:
            return
        target = self.result.zip_path or self.result.bundle_dir
        if target:
            _open_in_file_manager(os.path.dirname(target) if os.path.isfile(target) else target)

    # -- unpack window ------------------------------------------------------

    def open_unpack_window(self) -> None:
        UnpackWindow(self.root)

    # -- event pump ---------------------------------------------------------

    def _poll_events(self) -> None:
        rows_added = 0
        try:
            while rows_added < ROWS_PER_TICK:
                event = self.events.get_nowait()
                kind = event[0]

                if kind == "scan_list":
                    _, token, paths = event
                    if token != self.scan_token:
                        continue
                    self.all_paths = list(paths)
                    self._populate_rows()

                elif kind == "scan_meta":
                    _, token, path, analysis, error = event
                    if token != self.scan_token:
                        continue
                    if analysis is not None:
                        self.scan_results[path] = analysis
                    self.library.update_row(
                        path, analysis, error="Unreadable" if error else ""
                    )

                elif kind == "scan_error":
                    _, token, message = event
                    if token == self.scan_token:
                        self.library.set_scan_status("Couldn't read that folder")
                        self.set_status("Couldn't read that folder: %s" % message, error=True)

                elif kind == "analyzed":
                    self._set_busy(False)
                    self._show_analysis(event[1])

                elif kind == "progress":
                    _, done, total, label = event
                    modal = self._live_modal()
                    if modal is not None:
                        modal.update_progress(done, total, label)

                elif kind == "packaged":
                    self._set_busy(False)
                    self._show_result(event[1])

                elif kind == "error":
                    self._set_busy(False)
                    self.set_status(event[1], error=True)
                    modal = self._live_modal()
                    if modal is not None:
                        modal.fail(event[1])
        except queue.Empty:
            pass

        # Rows are cheap to make but not free; a bounded batch per tick keeps
        # a 500-project folder from locking the window while it populates.
        while self.pending_rows and rows_added < ROWS_PER_TICK:
            path = self.pending_rows.pop(0)
            self.library.add_row(path)
            # On a re-render (e.g. after a search) the analysis is already
            # cached, so fill the row now instead of waiting for a re-read.
            cached = self.scan_results.get(path)
            if cached is not None:
                self.library.update_row(path, cached)
            rows_added += 1

        # Twice a second, top up the read queue with whatever is on screen.
        self.ticks += 1
        if self.ticks % 8 == 0:
            self._request_visible()

        self.root.after(60, self._poll_events)


# ===========================================================================
# Receiving side
# ===========================================================================


class UnpackWindow:
    """Extract a package someone sent, and show its manifest."""

    def __init__(self, parent) -> None:
        self.window = ctk.CTkToplevel(parent)
        self.window.title("Open a package")
        self.window.minsize(720, 540)
        self.window.configure(fg_color=T.BG_MAIN)
        self.events: "queue.Queue[tuple]" = queue.Queue()
        self.result: Optional[core.UnpackResult] = None

        frame = ctk.CTkFrame(self.window, fg_color="transparent")
        frame.pack(fill="both", expand=True, padx=24, pady=22)
        frame.columnconfigure(1, weight=1)
        frame.rowconfigure(4, weight=1)

        head = ctk.CTkFrame(frame, fg_color="transparent")
        head.grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 16))
        _dl_badge = W.badge(
            head, "", size=32, color=T.BG_CARD, text_color=T.TEXT_MUTED,
            font=(T.UI, 15, "bold"),
        )
        _dl = I.icon("download", 17, T.TEXT_MUTED)
        if _dl is not None:
            _dl_badge.configure(image=_dl)
        _dl_badge.pack(side="left")
        ctk.CTkLabel(head, text="Open a package", font=T.F_H2, text_color=T.TEXT).pack(
            side="left", padx=(11, 0)
        )

        self.zip_var = tk.StringVar()
        self.dest_var = tk.StringVar(value=_default_output_dir())
        self._field(frame, 1, "Package", self.zip_var, self.choose_zip)
        self._field(frame, 2, "Extract to", self.dest_var, self.choose_dest)

        self.status = ctk.CTkLabel(
            frame, text="Choose a *_package.zip", font=T.F_SMALL,
            text_color=T.TEXT_DIM, anchor="w",
        )
        self.status.grid(row=3, column=0, columnspan=3, sticky="w", pady=(14, 8))

        self.text = ctk.CTkTextbox(
            frame, fg_color=T.BG_INSET, text_color=T.TEXT_MUTED, font=(T.MONO, 12),
            corner_radius=10, border_width=1, border_color=T.BORDER, wrap="word",
        )
        self.text.grid(row=4, column=0, columnspan=3, sticky="nsew")
        self.text.configure(state="disabled")

        actions = ctk.CTkFrame(frame, fg_color="transparent")
        actions.grid(row=5, column=0, columnspan=3, sticky="ew", pady=(14, 0))
        actions.columnconfigure(0, weight=1)
        self.reveal = W.ghost_button(actions, "Show me the folder", self.reveal_folder, icon="reveal")
        self.reveal.configure(state="disabled")
        self.reveal.grid(row=0, column=1, padx=(0, 8))
        # Neutral, not accent: the orange is reserved for the logo, the active
        # tab and Package & Send. This one just carries a heavier fill.
        self.unpack_button = ctk.CTkButton(
            actions, text="Unpack", command=self.start, font=(T.UI, 13, "bold"),
            height=36, width=110, corner_radius=8, fg_color=T.BG_CARD_HOVER,
            hover_color="#3d4048", text_color=T.TEXT,
            border_width=1, border_color=T.BORDER,
        )
        self.unpack_button.grid(row=0, column=2)

        self.progress = ctk.CTkProgressBar(
            frame, height=4, corner_radius=2, fg_color=T.BG_INSET, progress_color=T.ACCENT
        )
        self.progress.grid(row=6, column=0, columnspan=3, sticky="ew", pady=(12, 0))
        self.progress.set(0)

        self._poll()

    def _field(self, master, row: int, label: str, variable: tk.StringVar, command) -> None:
        ctk.CTkLabel(
            master, text=label, font=T.F_SMALL, text_color=T.TEXT_MUTED, anchor="w", width=76
        ).grid(row=row, column=0, sticky="w", pady=(0, 8))
        entry = ctk.CTkEntry(
            master, textvariable=variable, font=T.F_SMALL, height=32, corner_radius=8,
            fg_color=T.BG_INSET, border_color=T.BORDER, text_color=T.TEXT,
        )
        entry.grid(row=row, column=1, sticky="ew", padx=10, pady=(0, 8))
        W.ghost_button(master, "Browse...", command).grid(row=row, column=2, pady=(0, 8))

    def choose_zip(self) -> None:
        path = filedialog.askopenfilename(
            title="Choose a package", filetypes=[("Package", "*.zip"), ("All files", "*.*")]
        )
        if path:
            self.zip_var.set(path)

    def choose_dest(self) -> None:
        path = filedialog.askdirectory(title="Extract to")
        if path:
            self.dest_var.set(path)

    def start(self) -> None:
        zip_path = self.zip_var.get().strip()
        dest = self.dest_var.get().strip()
        if not os.path.isfile(zip_path):
            self.status.configure(text="Pick a package file first.", text_color=T.RED)
            return
        self.status.configure(text="Unpacking...", text_color=T.TEXT_MUTED)
        self.progress.configure(progress_color=T.ACCENT)
        self.progress.set(0)
        threading.Thread(target=self._worker, args=(zip_path, dest), daemon=True).start()

    def _worker(self, zip_path: str, dest: str) -> None:
        def progress(done: int, total: int, label: str) -> None:
            self.events.put(("progress", done, total, label))

        try:
            os.makedirs(dest, exist_ok=True)
            result = core.unpack_package(zip_path, dest, progress=progress)
            self.events.put(("done", result))
        except Exception as exc:
            self.events.put(("error", str(exc)))

    def reveal_folder(self) -> None:
        if self.result:
            _open_in_file_manager(self.result.output_dir)

    def _poll(self) -> None:
        try:
            while True:
                event = self.events.get_nowait()
                if event[0] == "progress":
                    _, done, total, label = event
                    if total:
                        self.progress.set(min(1.0, done / float(total)))
                    self.status.configure(text=_shorten(label, 70), text_color=T.TEXT_MUTED)

                elif event[0] == "done":
                    result: core.UnpackResult = event[1]
                    self.result = result
                    body = result.manifest_text or "(no manifest in this package)"
                    if result.flp_path:
                        body = (
                            "Open this in FL Studio:\n%s\n"
                            "Its samples are in the samples folder beside it.\n\n"
                            % result.flp_path
                            + "-" * 60
                            + "\n\n"
                            + body
                        )
                    self.text.configure(state="normal")
                    self.text.delete("1.0", "end")
                    self.text.insert("1.0", body)
                    self.text.configure(state="disabled")
                    self.progress.set(1.0)
                    self.progress.configure(progress_color=T.GREEN)
                    self.status.configure(
                        text="Unpacked %d sample(s) to %s"
                        % (result.sample_count, result.output_dir),
                        text_color=T.GREEN,
                    )
                    self.reveal.configure(state="normal")

                elif event[0] == "error":
                    self.progress.set(0)
                    self.status.configure(text=event[1], text_color=T.RED)
        except queue.Empty:
            pass
        self.window.after(60, self._poll)


# ===========================================================================
# Settings
# ===========================================================================


class SettingsWindow:
    """A small preferences popover: the two folders the app remembers."""

    def __init__(self, parent, *, output_dir, library_dir, on_output, on_library) -> None:
        self.on_output = on_output
        self.on_library = on_library

        self.window = ctk.CTkToplevel(parent)
        self.window.title("Settings")
        self.window.resizable(False, False)
        self.window.configure(fg_color=T.BG_MAIN)

        frame = ctk.CTkFrame(self.window, fg_color="transparent")
        frame.pack(fill="both", expand=True, padx=24, pady=22)
        frame.columnconfigure(1, weight=1)

        head = ctk.CTkFrame(frame, fg_color="transparent")
        head.grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 16))
        _lg = I.logo(30)
        if _lg is not None:
            ctk.CTkLabel(head, text="", image=_lg, width=30, height=30).pack(side="left")
        else:
            W.badge(head, "f", size=30, font=(T.UI, 14, "bold")).pack(side="left")
        titles = ctk.CTkFrame(head, fg_color="transparent")
        titles.pack(side="left", padx=(11, 0))
        ctk.CTkLabel(titles, text="Settings", font=T.F_H2, text_color=T.TEXT).pack(anchor="w")
        ctk.CTkLabel(
            titles, text="flpackager v%s" % __version__, font=T.F_TINY,
            text_color=T.TEXT_DIM,
        ).pack(anchor="w")

        self.library_var = tk.StringVar(value=library_dir)
        self.output_var = tk.StringVar(value=output_dir)
        self._field(frame, 1, "Projects folder", self.library_var, self._pick_library,
                    "Where flpackager looks for your .flp projects.")
        self._field(frame, 3, "Save packages to", self.output_var, self._pick_output,
                    "Where finished packages are written.")

        done = ctk.CTkFrame(frame, fg_color="transparent")
        done.grid(row=5, column=0, columnspan=3, sticky="e", pady=(18, 0))
        W.ghost_button(done, "Done", self.window.destroy).pack(side="right")

        self.window.transient(parent)
        try:
            self.window.grab_set()
        except Exception:
            pass

    def _field(self, master, row, label, variable, command, hint) -> None:
        ctk.CTkLabel(
            master, text=label, font=T.F_SMALL, text_color=T.TEXT_MUTED, anchor="w", width=118
        ).grid(row=row, column=0, sticky="w", pady=(0, 2))
        entry = ctk.CTkEntry(
            master, textvariable=variable, font=T.F_SMALL, height=32, width=320,
            corner_radius=8, fg_color=T.BG_INSET, border_color=T.BORDER, text_color=T.TEXT,
        )
        entry.grid(row=row, column=1, sticky="ew", padx=10, pady=(0, 2))
        W.ghost_button(master, "Browse...", command).grid(row=row, column=2, pady=(0, 2))
        ctk.CTkLabel(
            master, text=hint, font=T.F_TINY, text_color=T.TEXT_DIM, anchor="w"
        ).grid(row=row + 1, column=1, columnspan=2, sticky="w", padx=10, pady=(0, 12))

    def _pick_library(self) -> None:
        path = filedialog.askdirectory(title="Where do you keep your projects?")
        if path:
            self.library_var.set(path)
            self.on_library(path)

    def _pick_output(self) -> None:
        path = filedialog.askdirectory(title="Where should packages go?")
        if path:
            self.output_var.set(path)
            self.on_output(path)


# ===========================================================================
# First-run welcome
# ===========================================================================


class WelcomeModal(ctk.CTkToplevel):
    """Shown once, on the very first launch: what the app is, and step one."""

    STEPS = [
        ("1", "Point it at your projects",
         "Pick the folder where you keep your .flp files. flpackager lists them "
         "with their BPM, key, and how many samples each one needs."),
        ("2", "Open a project and check it",
         "Every sample shows as Bundled, Built-in, or Missing, so you know it'll "
         "open cleanly on the other person's machine before you send it."),
        ("3", "Package & Send",
         "One button bundles the project with its samples and a manifest into a "
         "single .zip. Send it however you like -- the receiver opens it here too."),
    ]

    def __init__(self, parent, *, on_choose_folder, on_done) -> None:
        super().__init__(parent)
        self.on_choose_folder = on_choose_folder
        self.on_done = on_done
        self._finished = False

        self.title("Welcome")
        self.configure(fg_color=T.BG_MAIN)
        self.resizable(False, False)
        self.protocol("WM_DELETE_WINDOW", self._close)

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(padx=30, pady=26, fill="both", expand=True)

        # --- header ---
        head = ctk.CTkFrame(body, fg_color="transparent")
        head.pack(anchor="w")
        _lg = I.logo(40)
        if _lg is not None:
            ctk.CTkLabel(head, text="", image=_lg, width=40, height=40).pack(side="left")
        else:
            W.badge(head, "f", size=40, font=(T.UI, 18, "bold")).pack(side="left")
        titles = ctk.CTkFrame(head, fg_color="transparent")
        titles.pack(side="left", padx=(13, 0))
        ctk.CTkLabel(
            titles, text="Welcome to flpackager", font=T.F_TITLE, text_color=T.TEXT, height=28
        ).pack(anchor="w")
        ctk.CTkLabel(
            titles,
            text="Send an FL Studio project that just opens -- samples and all.",
            font=T.F_SMALL, text_color=T.TEXT_MUTED, height=18,
        ).pack(anchor="w")

        # --- the problem, in one line ---
        self._banner(
            body,
            "FL saves sample paths from your machine, so a bare .flp opens with "
            "missing-audio errors on anyone else's. flpackager fixes that.",
        )

        # --- three steps ---
        for number, title, detail in self.STEPS:
            self._step(body, number, title, detail)

        # --- receiving note ---
        ctk.CTkLabel(
            body,
            text="Got sent a package? Use  Incoming  in the sidebar to open it.",
            font=T.F_SMALL, text_color=T.TEXT_DIM, anchor="w", justify="left",
        ).pack(anchor="w", pady=(6, 0))

        # --- actions ---
        actions = ctk.CTkFrame(body, fg_color="transparent")
        actions.pack(fill="x", pady=(22, 0))
        W.ghost_button(actions, "I'll look around first", self._close).pack(side="right")
        W.accent_button(actions, "Choose my projects folder", self._choose, icon="folder").pack(
            side="right", padx=(0, 10)
        )

        self._centre(parent)
        self.transient(parent)
        try:
            self.grab_set()
        except Exception:
            pass

    def _banner(self, master, text: str) -> None:
        frame = ctk.CTkFrame(
            master, fg_color=T.blend(T.ACCENT, T.BG_MAIN, 0.08), corner_radius=9,
            border_width=1, border_color=T.blend(T.ACCENT, T.BG_MAIN, 0.22),
        )
        frame.pack(fill="x", pady=(18, 8))
        ctk.CTkLabel(
            frame, text=text, font=T.F_SMALL, text_color=T.TEXT_MUTED,
            justify="left", wraplength=470, anchor="w",
        ).pack(anchor="w", padx=13, pady=10)

    def _step(self, master, number: str, title: str, detail: str) -> None:
        row = ctk.CTkFrame(master, fg_color="transparent")
        row.pack(fill="x", pady=7)
        W.badge(
            row, number, size=26, color=T.BG_CARD, text_color=T.ACCENT,
            font=(T.UI, 13, "bold"),
        ).pack(side="left", anchor="n")
        text = ctk.CTkFrame(row, fg_color="transparent")
        text.pack(side="left", padx=(13, 0), fill="x", expand=True)
        ctk.CTkLabel(
            text, text=title, font=T.F_BODY_BOLD, text_color=T.TEXT, anchor="w", height=18
        ).pack(anchor="w")
        ctk.CTkLabel(
            text, text=detail, font=T.F_SMALL, text_color=T.TEXT_MUTED,
            justify="left", wraplength=440, anchor="w",
        ).pack(anchor="w")

    def _centre(self, parent) -> None:
        self.update_idletasks()
        try:
            x = parent.winfo_rootx() + (parent.winfo_width() - self.winfo_width()) // 2
            y = parent.winfo_rooty() + (parent.winfo_height() - self.winfo_height()) // 4
            self.geometry("+%d+%d" % (max(0, x), max(0, y)))
        except Exception:
            pass

    def _finish(self) -> None:
        if not self._finished:
            self._finished = True
            try:
                self.on_done()
            except Exception:
                pass

    def _choose(self) -> None:
        self._finish()
        self._close()
        try:
            self.on_choose_folder()
        except Exception:
            pass

    def _close(self) -> None:
        self._finish()
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    initial = argv[0] if argv and argv[0].lower().endswith(".flp") else None

    ctk.set_appearance_mode("dark")

    # Use the drag-and-drop-aware root when tkinterdnd2 is present: CTk for the
    # look, DnDWrapper for the drop-target registration.
    try:
        from tkinterdnd2 import TkinterDnD  # type: ignore

        class _Root(ctk.CTk, TkinterDnD.DnDWrapper):  # type: ignore[misc]
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.TkdndVersion = TkinterDnD._require(self)

        root = _Root()
    except Exception:
        root = ctk.CTk()

    try:
        root.call("tk", "scaling", 1.2)
    except Exception:
        pass

    I.set_window_icon(root)  # real logo in the title bar / taskbar
    PackagerApp(root, initial_file=initial)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
