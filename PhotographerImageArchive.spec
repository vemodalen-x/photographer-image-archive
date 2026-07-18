# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['photo_archive_app.py'],
    pathex=[],
    binaries=[],
    datas=[('assets\\photo_archive_icons', 'assets\\photo_archive_icons')],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['setuptools', 'pkg_resources', 'packaging'],
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
    name='PhotographerImageArchive',
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
    icon=['assets\\photo_archive_icons\\photo_archive_app.ico'],
    version='assets\\photo_archive_version_info.txt',
)
