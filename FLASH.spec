# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_dynamic_libs, collect_submodules


rawpy_binaries = collect_dynamic_libs('rawpy')
pillow_heif_binaries = collect_dynamic_libs('pillow_heif')
pillow_heif_hiddenimports = collect_submodules('pillow_heif')

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=rawpy_binaries + pillow_heif_binaries,
    datas=[
        ('assets/flash_icon.png', 'assets'),
        ('assets/flash_icon.ico', 'assets'),
        ('assets/reconnect_reference', 'assets/reconnect_reference'),
    ],
    hiddenimports=[
        'tkinter',
        'rawpy',
        'rawpy._rawpy',
        *pillow_heif_hiddenimports,
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
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
