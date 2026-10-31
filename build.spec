# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller 单文件打包配置

用法（在 Windows 上，于本目录执行）：
    pip install -r requirements.txt
    pyinstaller build.spec

产物：dist/PDFCompress.exe（可自行改名为 中文名.exe，不影响运行）

不再打包任何外部可执行文件：解密由 pdfdecrypt.py（纯 Python + pycryptodome）
完成，去线性化由引擎重写结构时顺带完成。
"""

import os

block_cipher = None

a = Analysis(
    ["gui.py"],
    pathex=["."],
    binaries=[],
    datas=[],
    hiddenimports=[
        "pdfstrip",
        "pdfdecrypt",
        "pypdf",
        # pycryptodome 的 AES 是 C 扩展（_raw_aes 等），静态分析有时扫不到
        "Crypto",
        "Crypto.Cipher",
        "Crypto.Cipher.AES",
        # gui.py 用 ctypes 接 Windows 拖放（WM_DROPFILES）。wintypes 是子模块，
        # 显式声明避免某些 PyInstaller 版本分析不到。
        "ctypes",
        "ctypes.wintypes",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # 砍掉用不到的大件。
    # 引擎只用 pypdf + zlib + pdfdecrypt(pycryptodome)。
    excludes=[
        "pymupdf", "fitz",
        "cryptography",          # 旧版 pypdf 解密路径才需要，解密已改走 pdfdecrypt
        "numpy", "matplotlib", "scipy", "PIL", "IPython", "pandas",
        "test", "unittest", "pydoc", "doctest",
        "tkinter.test", "tkinter.dnd", "turtle", "idlelib",
        "pypdfium2", "PyQt5", "PyQt6", "PySide2", "PySide6",
        "email", "http", "xmlrpc", "pdb", "cProfile", "profile",
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
    name="PDFCompress",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,          # UPX 压缩极易触发杀软误报，宁可不压
    runtime_tmpdir=None,
    console=False,      # 双击不弹黑窗
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="app.ico" if os.path.exists("app.ico") else None,
)
