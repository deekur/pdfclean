# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller 单文件打包配置

用法（在 Windows 上，于本目录执行）：
    pip install -r requirements.txt
    pyinstaller build.spec

产物：dist/PDFCompress.exe（可自行改名为 中文名.exe，不影响运行）
"""

import os

block_cipher = None

# qpdf 全套文件（Windows 版是 qpdf.exe + 8 个 DLL；其他系统是一个 qpdf 可执行）
# 全部放进包内的 tools/ 子目录，运行时 pdfstrip._find_qpdf() 会去
# <exe目录>/tools/ 找。这里不能只收 .exe/.dll —— Linux/macOS 版 qpdf 没有扩展名，
# 只收 Windows 后缀会让其它平台打出「找不到 qpdf」的包。
QPDF_DIR = "tools"
qpdf_files = []
if os.path.isdir(QPDF_DIR):
    for name in os.listdir(QPDF_DIR):
        full = os.path.join(QPDF_DIR, name)
        if not os.path.isfile(full):
            continue
        low = name.lower()
        if low.endswith((".exe", ".dll", ".so", ".dylib", ".txt", ".license")) \
                or low.startswith("qpdf"):
            qpdf_files.append((full, "tools"))
    if not qpdf_files:
        print("WARNING: %s 目录存在但没有可打包的文件" % QPDF_DIR)
else:
    print("WARNING: %s 目录不存在，打出来的包将无法去线性化" % QPDF_DIR)

a = Analysis(
    ["gui.py"],
    pathex=["."],
    binaries=qpdf_files,
    datas=[],
    hiddenimports=[
        "pdfstrip",
        "pypdf",
        # pypdf 的加密后端是按需动态导入的，静态分析扫不到，必须显式声明，
        # 否则处理加密 PDF 时会在包里抛 ImportError。
        # 只声明 _cryptography：_bcrypt 是可选后端，没装 bcrypt 时声明它会让
        # PyInstaller 直接报 "Hidden import not found" 失败。
        "pypdf._crypt_providers",
        "pypdf._crypt_providers._cryptography",
        # cryptography 是纯 Rust 扩展，pypdf 通过 aes_cbc_encrypt 这类函数间接调用，
        # PyInstaller 的静态分析扫不到 Rust 侧的 .pyd，必须显式声明，
        # 否则处理加密 PDF 时会在包里抛 DependencyError。
        "cryptography",
        "cryptography.hazmat.primitives.ciphers",
        "cryptography.hazmat.primitives.ciphers.algorithms",
        "cryptography.hazmat.primitives.ciphers.modes",
        "cryptography.hazmat.primitives.hashes",
        "cryptography.hazmat.primitives.padding",
        "cryptography.hazmat.backends.openssl",
        # gui.py 用 ctypes 接 Windows 拖放（WM_DROPFILES）。wintypes 是子模块，
        # 显式声明避免某些 PyInstaller 版本分析不到。
        "ctypes",
        "ctypes.wintypes",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # 砍掉用不到的大件。
    # 重点：pymupdf 完全不打包。它只在两条支路上被用到 ——
    #   (a) 解密加密 PDF，(b) --report 的渲染自检。
    # 引擎主体（解析/重排/写回）只用 pypdf + zlib + qpdf。
    # 拿掉后 libmupdf(32.7MB) + _mupdf(13.5MB) + 头文件(1.6MB) 全部不进包，
    # exe 从 35MB 降到约 15MB。代价：加密 PDF 会报「需要 PyMuPDF」并跳过，
    # 其余文件不受影响（代码里本来就有 ImportError 兜底）。
    excludes=[
        "pymupdf", "fitz",
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
