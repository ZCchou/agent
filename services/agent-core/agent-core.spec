# PyInstaller entry point for the Windows Core sidecar.
# Keep the sidecar build deterministic and package the installed project
# dependencies only through the normal import graph.
from PyInstaller.utils.hooks import collect_submodules, collect_data_files
from pathlib import Path
import sys

source_root = Path(SPECPATH) / "src"
sys.path.insert(0, str(source_root))
sys.path.insert(0, str(Path(SPECPATH).parents[1] / "tools"))
from rag_runtime_assets import ssl_support_resources

hiddenimports = collect_submodules("agent")

a = Analysis(
    ["src/agent/main.py"],
    pathex=[str(source_root)],
    binaries=[],
    datas=collect_data_files("agent", includes=["builtin_skills/**/*"])
        + collect_data_files("sqlite_vec") + collect_data_files("pypdfium2_raw")
        + [(str(path), ".") for path in ssl_support_resources()],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["torch", "transformers", "onnxruntime", "tokenizers", "safetensors", "sentencepiece"],
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
