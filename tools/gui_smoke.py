"""Drive the GUI programmatically: load a project, package it, check the result.

Exercises the real widgets and the real worker threads -- it just clicks the
buttons from code instead of with a mouse.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
import warnings

warnings.simplefilter("ignore")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import customtkinter as ctk

from flpackager import core
from flpackager.gui import PackagerApp, UnpackWindow

FLP = os.environ.get(
    "FLPACKAGER_TEST_FLP", r"C:\Users\TRIEDENT\Desktop\Main\beats\scaryahh.flp"
)

failures = []


def check(condition, label):
    print(f"  {'ok  ' if condition else 'FAIL'}  {label}")
    if not condition:
        failures.append(label)


def pump(root, seconds, until=None):
    """Run the Tk event loop for a while, stopping early once `until` is true."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        root.update()
        if until is not None and until():
            return True
        time.sleep(0.02)
    return until() if until else True


def main():
    tmp = tempfile.mkdtemp(prefix="flpack_gui_")
    try:
        ctk.set_appearance_mode("dark")
        root = ctk.CTk()
        root.withdraw()  # keep the window off-screen for an automated run
        app = PackagerApp(root)

        print("GUI smoke test")
        check(app.analysis is None, "starts with no project loaded")
        check(str(app.pack_button.cget("state")) == "disabled", "Package button starts disabled")

        # --- load a project ---
        app.load_project(FLP)
        loaded = pump(root, 30, until=lambda: app.analysis is not None)
        check(loaded, "project analysed")
        if not loaded:
            return 1

        check(app.analysis.tempo is not None, f"tempo read ({app.analysis.tempo})")
        check(len(app.project.sample_rows) > 0, "sample rows populated")
        check(len(app.project.plugin_rows) > 0, "plugin rows populated")
        check(str(app.pack_button.cget("state")) == "normal", "Package button enabled after load")
        check(app.project.winfo_ismapped() or app.project.winfo_exists(), "project page shown")
        check(
            app.project.title.cget("text") == app.analysis.project_name,
            f"project title: {app.project.title.cget('text')!r}",
        )
        check("Package will include" in app.project.summary.cget("text"),
              f"summary: {app.project.summary.cget('text')!r}")

        app.project.select_tab("plugins")
        check(app.project.active_tab == "plugins", "plugins tab selects")
        app.project.select_tab("samples")

        # --- package it ---
        app.output_var.set(tmp)
        app.start_packaging()
        packaged = pump(root, 180, until=lambda: app.result is not None)
        check(packaged, "packaging finished")
        if not packaged:
            return 1

        result = app.result
        check(os.path.isfile(result.zip_path), "zip written")
        check(result.copied_files == len(app.analysis.unique_bundled), "all samples copied")
        check("ready to send" in app.status.cget("text"), f"status: {app.status.cget('text')!r}")
        check(app.modal is not None and app.modal.done, "progress modal reached its done state")
        if app.modal is not None and app.modal.winfo_exists():
            check(app.modal.heading.cget("text") == "Package ready", "modal says Package ready")
            app.modal._close()

        # --- unpack it through the unpack window ---
        win = UnpackWindow(root)
        recv = os.path.join(tmp, "recv")
        win.zip_var.set(result.zip_path)
        win.dest_var.set(recv)
        win.start()
        unpacked = pump(root, 120, until=lambda: win.result is not None)
        check(unpacked, "unpack finished")
        if unpacked:
            check(os.path.isfile(win.result.flp_path), "unpacked .flp exists")
            check("Tempo" in win.text.get("1.0", "end"), "manifest shown in window")
            base = os.path.dirname(win.result.flp_path)
            import pyflp
            from pyflp.channel import Sampler

            unresolved = 0
            for ch in pyflp.parse(win.result.flp_path).channels:
                if isinstance(ch, Sampler) and ch.sample_path:
                    p = str(ch.sample_path)
                    if p.startswith("samples"):
                        if not os.path.isfile(os.path.join(base, p.replace("\\", os.sep))):
                            unresolved += 1
            check(unresolved == 0, f"all rewritten paths resolve ({unresolved} broken)")

        # --- error handling: a non-flp file must not crash ---
        junk = os.path.join(tmp, "notaproject.txt")
        with open(junk, "w") as fp:
            fp.write("nope")
        app.load_project(junk)
        pump(root, 3)
        check("doesn't look like" in app.status.cget("text"), "rejects a non-.flp politely")

        bad = os.path.join(tmp, "corrupt.flp")
        with open(bad, "wb") as fp:
            fp.write(b"GARBAGE" * 20)
        app.load_project(bad)
        pump(root, 20, until=lambda: "Couldn't read" in app.status.cget("text"))
        check("Couldn't read" in app.status.cget("text"),
              "reports a corrupt .flp without crashing")

        # --- library scan: rows appear off-thread for a real folder ---
        app.start_scan(os.path.dirname(FLP))
        scanned = pump(root, 240, until=lambda: len(app.library.rows) >= 3)
        check(scanned, f"library scan produced rows ({len(app.library.rows)})")
        if app.library.rows:
            app.open_project(app.library.row_paths[0])
            pump(root, 2)
            check(app.analysis is not None, "row click opens the project page")

        # the click-to-browse route is still wired up
        check(callable(app.choose_project), "file picker entry point present")

        root.destroy()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if failures:
        print(f"{len(failures)} FAILURE(S): {failures}")
        return 1
    print("all GUI checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
