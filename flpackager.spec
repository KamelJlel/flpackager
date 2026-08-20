# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec: builds a single-file, double-clickable flpackager.exe.

Build with:  pyinstaller flpackager.spec --noconfirm
Output:      dist/flpackager.exe
"""

from PyInstaller.utils.hooks import collect_data_files, collect_all

# CustomTkinter loads its theme JSON and fonts from disk at import time, so the
# package's data files have to travel with the binary.
ctk_datas = collect_data_files("customtkinter")

# tkinterdnd2 ships the native tkdnd tcl/binary libraries it loads at runtime;
# without them, drag-and-drop silently falls back to the file picker in the
# packaged build. collect_all grabs the data, binaries and submodules.
dnd_datas, dnd_binaries, dnd_hiddenimports = collect_all("tkinterdnd2")

block_cipher = None

a = Analysis(
    ["flpackager/__main__.py"],
    pathex=[],
    binaries=dnd_binaries,
    datas=ctk_datas + dnd_datas,
    # PyFLP resolves these lazily, so PyInstaller can't see them by itself.
    hiddenimports=[
        "pyflp",
        "pyflp.channel",
        "pyflp.plugin",
        "pyflp.project",
        "pyflp.mixer",
        "pyflp.pattern",
        "pyflp.arrangement",
        "pyflp.controller",
        "pyflp.timemarker",
        "customtkinter",
        "darkdetect",
        "construct",
        "fastenum",
        "sortedcontainers",
    ] + dnd_hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest", "numpy", "matplotlib"],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="flpackager",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,          # no terminal window when double-clicked
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
