# -*- mode: python ; coding: utf-8 -*-
import os, sys
from PyInstaller.utils.hooks import collect_all, collect_data_files

block_cipher = None

# Collect litellm data files
litellm_datas, litellm_binaries, litellm_hiddenimports = collect_all('litellm')
flask_dance_datas = collect_data_files('flask_dance')

a = Analysis(
    ['launcher.py'],
    pathex=['.'],
    binaries=litellm_binaries,
    datas=[
        ('../templates', 'templates'),
        ('../static',    'static'),
        ('version.json', 'desktop'),
        ('../utils',     'utils'),
        ('../extractor', 'extractor'),
    ] + litellm_datas + flask_dance_datas,
    hiddenimports=[
        'litellm', 'litellm.utils', 'litellm.main',
        'flask', 'flask_login', 'flask_dance',
        'flask_dance.contrib.google',
        'werkzeug', 'jinja2', 'markupsafe',
        'pdfminer', 'pdfminer.high_level', 'pdfminer.layout',
        'pdfplumber',
        'pandas', 'pandas.core', 'numpy',
        'requests', 'certifi', 'urllib3',
        'openpyxl', 'xlrd',
        'desktop.launcher', 'desktop.license_client',
        'desktop.device_id', 'desktop.app_state',
    ] + litellm_hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=['pymongo', 'motor', 'tkinter', 'matplotlib'],
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
    upx=False,
    console=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name='EasyDebitSummary',
)

app = BUNDLE(
    coll,
    name='EasyDebitSummary.app',
    icon=None,
    bundle_identifier='com.easydebitsummary.desktop',
    info_plist={
        'NSHighResolutionCapable': True,
        'LSBackgroundOnly': False,
        'CFBundleShortVersionString': '1.0.0',
        'CFBundleName': 'Easy Debit Summary',
        'CFBundleDisplayName': 'Easy Debit Summary',
        'NSRequiresAquaSystemAppearance': False,
    },
)
