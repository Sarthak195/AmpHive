# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for AmpHiveFlasher.exe.

Onefile, console build: the app is an interactive text CLI, not a GUI, so it
needs a visible console window and a working stdin when double-clicked from
Explorer — that's what `console=True` gives it (PyInstaller allocates a
console automatically on launch).

This does NOT bundle a compiler or the ESP-IDF toolchain — only esptool +
pyserial + this package's own code. See tools/flasher/README.md for why the
tool never builds firmware on the end user's machine.

Build (from anywhere, this resolves its own directory via SPECPATH):
    pyinstaller tools/flasher/AmpHiveFlasher.spec --distpath dist --workpath build
Output: dist/AmpHiveFlasher.exe

Built automatically by .github/workflows/flasher-exe.yml on windows-latest.
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files

HERE = Path(SPECPATH)  # noqa: F821 - injected by PyInstaller at spec-exec time
ENTRY = HERE / "amphive_flasher" / "__main__.py"

# esptool ships its "stub flasher" protocol data as JSON files loaded at
# runtime (not importable Python), which PyInstaller's static import
# analysis can't discover on its own — collect them explicitly, or flashing
# fails at runtime with a missing-file error that never shows up in local
# `python -m amphive_flasher` testing (only in the frozen build).
datas = collect_data_files("esptool")

a = Analysis(  # noqa: F821
    [str(ENTRY)],
    pathex=[str(HERE)],
    binaries=[],
    datas=datas,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="AmpHiveFlasher",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)
