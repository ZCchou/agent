# PyInstaller entry point for the Windows Core sidecar.
# Keep the sidecar build deterministic and package the installed project
# dependencies only through the normal import graph.
from PyInstaller.utils.hooks import collect_submodules


hiddenimports = collect_submodules("agent")

a = Analysis(
    ["src/agent/main.py"],
    pathex=["src"],
    binaries=[],
    datas=[],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="agent-core",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
)
