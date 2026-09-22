# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec — CLI 版 (main.py 全部子命令)

用法 (在项目根目录):
    pip install pyinstaller>=6.3.0
    python packaging/build.py --cli-only

产物:
    dist/wecom-voice-blaster/wecom-voice-blaster.exe   (onedir)
    附带 config.yaml / samples/ / vendor/ 等运行时资源.
"""

import os

from PyInstaller.utils.hooks import collect_submodules

block_cipher = None
spec_dir = SPECPATH
root = os.path.abspath(os.path.join(spec_dir, os.pardir))

hidden = []
hidden += collect_submodules("app")
hidden += collect_submodules("uiautomator2")
hidden += collect_submodules("adbutils")
hidden += collect_submodules("PySide6")
hidden += [
    "loguru",
    "pydantic",
    "yaml",
    "PIL",
    "sounddevice",
    "soundfile",
]

datas = [
    (os.path.join(root, "config.yaml"), "."),
    (os.path.join(root, "samples"), "samples"),
    (os.path.join(root, "testdata"), "testdata"),
    (os.path.join(root, "vendor"), "vendor"),
    (os.path.join(root, "contacts.example.csv"), "."),
]

a = Analysis(
    [os.path.join(root, "main.py")],
    pathex=[root],
    binaries=[],
    datas=datas,
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[os.path.join(spec_dir, "runtime_hook_frozen.py")],
    excludes=[
        "tkinter",
        "matplotlib",
        "IPython",
        "notebook",
        "pytest",
        "pytestqt",
    ],
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
    name="wecom-voice-blaster",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="wecom-voice-blaster",
)
