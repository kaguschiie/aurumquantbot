# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_submodules

hiddenimports = ['pystray._win32', 'bot', 'agent', 'chat', 'teknik', 'quant', 'telegram_bot', 'dersler', 'makro', 'portfoy', 'kontrolor', 'haberler', 'feedparser', 'zincir', 'guncelle', 'sunucu', 'gecmis', 'komite', 'hafiza', 'gunluk', 'montecarlo']
hiddenimports += collect_submodules('webview')
hiddenimports += collect_submodules('anthropic')
hiddenimports += collect_submodules('sklearn')


a = Analysis(
    ['app.py'],
    pathex=[],
    binaries=[],
    datas=[('ui.html', '.'), ('lw.js', '.'), ('surum.txt', '.'), ('portfoy_model.joblib', '.'), ('sunucu_adresi.txt', '.')],
    hiddenimports=hiddenimports,
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
    [],
    exclude_binaries=True,
    name='KriptoQuantBot',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['icon.ico'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='KriptoQuantBot',
)
