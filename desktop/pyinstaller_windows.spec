# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for Windows — build with:
#   pyinstaller desktop/pyinstaller_windows.spec

import os
block_cipher = None

a = Analysis(
    ['desktop/launcher.py'],
    pathex=['.'],
    binaries=[],
    datas=[
        ('templates',           'templates'),
        ('static',              'static'),
        ('desktop/version.json','desktop'),
        ('utils',               'utils'),
        ('extractor',           'extractor'),
    ],
    hiddenimports=[
        'litellm', 'flask', 'flask_login', 'flask_dance',
        'werkzeug', 'jinja2', 'pdfminer', 'pdfplumber',
        'pandas', 'requests',
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=['pymongo', 'motor'],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='EasyDebitSummary',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    icon='static/favicon.ico' if os.path.exists('static/favicon.ico') else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='EasyDebitSummary',
)
