# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec — GUI 版（onefile）。

产物：
    dist/企微客户自动化管理工具.exe
"""

import os

from PyInstaller.utils.hooks import collect_submodules

block_cipher = None
spec_dir = SPECPATH
root = os.path.abspath(os.path.join(spec_dir, os.pardir))

hidden = []
hidden += collect_submodules("app")
# 关键：runtime.wework_re_android 里的 probe_account_identity / list_all_contacts /
# probe_fta_wxa_pipeline / send_engine 等模块由 GUI 通过 frozen_dispatch 的
# __runmod__ 动态 importlib.import_module 起来；不显式收集的话打包时 PyInstaller
# 追不到依赖，运行时子命令直接 ModuleNotFoundError → 联系人/发送/采集全崩。
hidden += collect_submodules("runtime")
hidden += [
    "loguru", "pydantic", "yaml", "PIL",
    "PySide6",
    "PySide6.QtCore",
    "PySide6.QtGui",
    "PySide6.QtWidgets",
    "PySide6.QtNetwork",
    "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets",
]

datas = [
    (os.path.join(root, "config.yaml"), "."),
]

a = Analysis(
    [os.path.join(spec_dir, "gui_launcher.py")],
    pathex=[root, spec_dir],
    binaries=[],
    datas=datas,
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[os.path.join(spec_dir, "runtime_hook_frozen.py")],
    excludes=[
        "tkinter", "matplotlib", "IPython", "notebook",
        "pytest", "pytestqt",
        "PySide6.Qt3DAnimation", "PySide6.Qt3DCore", "PySide6.Qt3DExtras",
        "PySide6.Qt3DInput", "PySide6.Qt3DLogic", "PySide6.Qt3DRender",
        "PySide6.QtQuick", "PySide6.QtQuick3D", "PySide6.QtQml",
        "PySide6.QtWebEngine", "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets",
        "PySide6.QtCharts", "PySide6.QtDataVisualization", "PySide6.QtGraphs",
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
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    exclude_binaries=False,
    name="企微客户自动化管理工具",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
