# PDF 无损压缩 —— Windows 打包说明

把一个 PDF 文件拖到这个程序上，几秒钟后同目录出现一个 `<原名>_cleaned.pdf`，
体积通常只剩 **5%–10%**，画面和文字一个像素、一个字都不变。

> 手上这份 `pdfstrip-win-src.zip` 已内置 Windows 64 位 qpdf，解压即可用。
> 想要一个双击就能跑的 `.exe`，按下面路线 A 或 B 打一次包即可 ——
> 打包必须在一台 Windows 机器（或 GitHub 的 Windows 云机）上做，
> PyInstaller 不支持交叉编译，在 Linux/Mac 上打不出 Windows 程序。

---

## 一、拿到 exe 的两条路

### 路线 A：GitHub Actions 自动构建（推荐，不用装任何东西）

1. 新建一个 GitHub 仓库（公开/私有都行）。
2. 把本目录**整个** `pdfstrip/` 传上去，注意 `.github/` 也要一起传（隐藏目录，
   用 GitHub Desktop 或 `git add -A` 才会带上）。
3. 打开仓库的 **Actions** 页，左侧选 `build-windows`，点 **Run workflow**。
4. 等约 4 分钟，进这次运行页面，底部 **Artifacts** 下载
   `PDFCompress-windows-x64.zip`，解压得到 `PDFCompress.exe`。

上传后 push 到 `main` 也会自动触发，不必手动点。

### 路线 B：Windows 本机自己打包

需要一台 Windows 10/11 64 位电脑，装好 [Python 3.11](https://www.python.org/downloads/windows/)
（安装时务必勾选 **Add python.exe to PATH**）。

打开 PowerShell，进到本目录，逐行执行：

```powershell
python -m pip install --upgrade pip
pip install -r requirements.txt

# 下载 qpdf（Windows 64 位版，约 8 MB）
$url = "https://sourceforge.net/projects/qpdf/files/qpdf/12.4.2/qpdf-12.4.2-msvc64.zip/download"
Invoke-WebRequest -Uri $url -OutFile qpdf.zip -UseBasicParsing
Expand-Archive qpdf.zip -DestinationPath qpdf_tmp
New-Item -ItemType Directory -Force -Path tools | Out-Null
Copy-Item "qpdf_tmp\qpdf-12.4.2-msvc64\bin\*" tools\ -Recurse

# 打包
pyinstaller build.spec --noconfirm
```

产物在 `dist\PDFCompress.exe`，单文件、约 20 MB，双击即用，无需安装 Python。
文件名可以随意改成中文（如 `PDF无损压缩.exe`），不影响运行。

> 仓库里 `tools/` 已预置好 qpdf，若直接克隆了整个目录，上面下载 qpdf 那三行可以跳过。

---

## 二、怎么用

| 场景 | 操作 |
| --- | --- |
| 压一个文件 | 双击 exe → 点虚线框选 PDF → 点「开始压缩」 |
| 压多个文件 | 选文件时按住 Ctrl 多选，界面会显示合计大小 |
| 图省事 | 把 PDF **拖到 exe 图标上**，启动即自动开压 |
| 批量拖 | 一次拖多个 PDF 到图标上，每个文件弹一个窗口并行压 |

输出永远写在**源文件同目录**，文件名加 `_cleaned` 后缀，**绝不覆盖原件**。
同名已存在时自动退到 `_cleaned_2.pdf`。

---

## 三、它到底删了什么

不是重新编码、不是降采样、不是转成图片。删的是 PDF 里纯属冗余的结构：

| 冗余项 | 典型占比 | 说明 |
| --- | --- | --- |
| 裸存内容流（无 `/Filter`） | **70%–90%** | Word 导出的 PDF 常把内容流明文存着，重新 deflate 压缩 |
| 重复 / 孤儿对象 | 5%–15% | 多次编辑残留的废弃对象、从未被绘制的图片资源 |
| 线性化开销 | 视文件而定 | `/Linearized` 字典、提示表、双份 xref，清理后必然失效 |
| 元数据 | <1% | Word 标题、作者名、PScript5、Distiller 等指纹 |

字体、图片、内容流字节、页面尺寸一律不动，所以渲染结果逐字节相同。

---

## 四、验证过什么

用 2027 届某省化学试卷及其答案两份真实文件实测：

| 文件 | 原始 | 清理后 | 比例 | 页数 | 像素校验 | 文字校验 | 字体 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 化学试卷 | 18,181,598 B | 1,337,671 B | **7.36%** | 8 → 8 | 8/8 页逐字节相同（144 dpi） | 7,825 字 → 7,825 字 | 10 种全保留 |
| 参看文献及答案 | 4,838,415 B | 387,405 B | **8.01%** | 4 → 4 | 4/4 页逐字节相同（144 dpi） | 3,033 字 → 3,033 字 | 9 种全保留 |

---

## 五、常见问题

**Q：提示「缺少 VCRUNTIME140.dll」/ 提示找不到 qpdf？**
exe 里已内置 qpdf 与全套 VC 运行库。若被杀软隔离，把整个 exe 加白名单即可；
若你自行删过 `tools/`，用路线 B 里的下载命令补齐。

**Q：压完反而变大了？**
内容流本来就是 deflate 的、没有冗余结构时会发生。程序仍会写出文件（便于对比），
直接用原件就好。

**Q：加密 PDF？**
GUI 版不提供口令输入框，遇到加密文件会弹窗提示「文件已加密，需用命令行版加 -p 口令」，
然后跳过这一个文件，其余文件照常处理。命令行版支持（解密由随包 qpdf 完成）：

```
PDFCompress.exe 加密.pdf -p 口令
python pdfstrip.py 加密.pdf -p 口令 -o 输出.pdf
```

口令错误的提示是「文件已加密，且当前口令无法解开」，不会产出损坏文件。

**Q：为什么比在线压缩网站慢一点、但效果好得多？**
在线站多是重编码图片（有损），本工具是**结构化去冗余**（无损）。
一份 18 MB 的 Word 试卷，在线站压到 3 MB 且字发虚；本工具压到 1.3 MB 且原样。

**Q：360/火绒报毒？**
PyInstaller 单文件包常被启发式引擎误报。已关闭 UPX 压缩以降低误报率。
介意的话走路线 B 本机打包，或直接分发「源码文件夹 + python」。

---

## 五点半、命令行版（可选）

不打包也能用：装好 Python 后把 `pdfstrip.py` 和 `tools/` 拷给对方，一句命令搞定。
`tools/` 是 Windows 64 位的 qpdf；其他系统换成对应的 qpdf 放到同目录即可。

```bash
python pdfstrip.py 试卷.pdf                       # 输出 试卷_cleaned.pdf
python pdfstrip.py 试卷.pdf -o out.pdf --report   # 顺带打印诊断 JSON
python pdfstrip.py 加密.pdf -p 口令 -o 输出.pdf     # 加密文件需给口令
```

| 参数 | 作用 |
| --- | --- |
| `-o OUT` | 指定输出路径，默认 `<原名>_cleaned.pdf` |
| `--report` | 打印诊断 JSON：体积构成、对象数、去线性化省了多少 |
| `-p PASSWORD` | 加密 PDF 的口令 |
| `--clean-content` | 额外清空内容流里的注释与多余空白（更彻底，但会改动内容流字节） |

---

## 六、目录结构

```
pdfstrip/
├─ pdfstrip.py              清理引擎（也可单独当命令行用）
├─ gui.py                   图形界面
├─ build.spec               PyInstaller 打包配置
├─ requirements.txt         打包环境依赖
├─ tools/                   qpdf.exe + 9 个 DLL（Windows 64 位）
└─ .github/workflows/
   └─ build-win.yml         自动构建 Windows exe
```
