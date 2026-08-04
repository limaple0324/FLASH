# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import (
    collect_data_files,
    collect_dynamic_libs,
    collect_submodules,
)


rawpy_binaries = collect_dynamic_libs('rawpy')
pillow_heif_binaries = collect_dynamic_libs('pillow_heif')
pillow_heif_hiddenimports = collect_submodules('pillow_heif')
rapidocr_binaries = (
    collect_dynamic_libs('rapidocr_onnxruntime')
    + collect_dynamic_libs('onnxruntime')
)
rapidocr_datas = collect_data_files('rapidocr_onnxruntime')
rapidocr_hiddenimports = collect_submodules('rapidocr_onnxruntime')
obsidian_reference_datas = [
    (
        f'assets/game_data_reference/obsidian/page_{page:02d}.png',
        'assets/game_data_reference/obsidian',
    )
    for page in range(1, 11)
]

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=rawpy_binaries + pillow_heif_binaries + rapidocr_binaries,
    datas=[
        ('assets/flash_icon.png', 'assets'),
        ('assets/flash_icon.ico', 'assets'),
        ('assets/reconnect_reference', 'assets/reconnect_reference'),
        ('assets/reconnect_reference/auto_battle', 'assets/reconnect_reference/auto_battle'),
        ('assets/role_id_ocr', 'assets/role_id_ocr'),
        ('assets/ui_fonts', 'assets/ui_fonts'),
    ] + obsidian_reference_datas + rapidocr_datas,
    hiddenimports=[
        'tkinter',
        'rawpy',
        'rawpy._rawpy',
        *pillow_heif_hiddenimports,
        *rapidocr_hiddenimports,
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

splash = Splash(
    'assets/flash_icon.png',
    binaries=a.binaries,
    datas=a.datas,
    text_pos=(48, 224),
    text_size=12,
    text_color='white',
    text_default='輔正在啟動，請稍候…',
)

exe = EXE(
    pyz,
    a.scripts,
    splash,
    splash.binaries,
    a.binaries,
    a.datas,
    [],
    name='FLASH',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='assets/flash_icon.ico',
)
