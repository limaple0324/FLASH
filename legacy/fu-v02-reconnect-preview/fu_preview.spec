# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
import os
from PyInstaller.utils.hooks import collect_data_files

root = Path(SPECPATH)
windir = Path(os.environ.get("WINDIR", r"C:\Windows"))
mfc = windir / "System32" / "mfc140u.dll"

datas = [
    (str(root / "assets"), "assets"),
    (str(root / "manor_assets"), "manor_assets"),
    (str(root / "fishing_evidence"), "fishing_evidence"),
    (str(root / "templates"), "templates"),
    (str(root / "sync_plus_icon.ico"), "."),
    (str(root / "sync_plus_icon.png"), "."),
    (str(root / "role_id_templates.json"), "."),
    (str(root / "sync_launch_config.json"), "."),
    (str(root / "config.json"), "."),
    (str(root / "RUNTIME_ASSET_MANIFEST.json"), "."),
]
datas += collect_data_files("rapidocr_onnxruntime", include_py_files=False)

a = Analysis(
    [str(root / "flash_sync_v02.py")],
    pathex=[str(root)],
    binaries=[(str(mfc), ".")] if mfc.is_file() else [],
    datas=datas,
    hiddenimports=[
        "fu_reconnect_integration", "smart_reconnect", "manor_runtime", "fishing_profiles",
        "runtime_paths", "dpi_policy", "window_geometry", "user_activity_guard",
        "v02_faithful_game_time", "runtime_asset_manifest", "session_identity", "win32com.client",
        "manor_assistant.models", "manor_assistant.vision", "manor_assistant.win32_api",
        "manor_assistant.workflow", "cv2", "numpy", "onnxruntime", "rapidocr_onnxruntime",
        "win32api", "win32con", "win32gui", "win32ui",
    ],
    hookspath=[], hooksconfig={}, runtime_hooks=[], excludes=[], noarchive=False, optimize=0,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name="輔V0.2_遊戲時間忠實模式_候選版",
    debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
    console=False, disable_windowed_traceback=False, argv_emulation=False,
    target_arch="x86_64", icon=str(root / "sync_plus_icon.ico"),
    version=str(root / "version_info.txt"), manifest=str(root / "windows_manifest.xml"),
)
