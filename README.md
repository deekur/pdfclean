# PDF 无损压缩 —— Windows 打包说明

把一个 PDF 文件拖到这个程序上，几秒钟后同目录出现一个 `<原名>_cleaned.pdf`，
体积通常只剩 **5%–10%**，画面和文字一个像素、一个字都不变。加密 PDF 直接填打开密码即可。

> 本版**不再依赖 qpdf 或任何外部可执行文件**：解密由项目内的 `pdfdecrypt.py`
> 完成（纯 Python + pycryptodome），去线性化由引擎重写结构时顺带完成。
> 想要一个双击就能跑的 `.exe`，按下面路线 A 或 B 打一次包即可 ——
> 打包必须在一台 Windows 机器（或 GitHub 的 Windows 云机）上做，
> PyInstaller 不支持交叉编译，在 Linux/Mac 上打不出 Windows 程序。

---

## 一、拿到 exe 的两条路

### 路线 A：GitHub Actions 自动构建（推荐，不用装任何东西）

1. 新建一个 GitHub 仓库（公开/私有都行）。
2. 把本目录**整个**传上去，注意 `.github/` 也要一起传（隐藏目录，
   用 GitHub Desktop 或 `git add -A` 才会带上）。
3. 打开仓库的 **Actions** 页，左侧选 `build-windows`，点 **Run workflow**。
4. 等约 3 分钟，进这次运行页面，底部 **Artifacts** 下载
   `PDFCompress-windows-x64.zip`，解压得到 `PDFCompress.exe`。

上传后 push 到 `main` 也会自动触发，不必手动点。

### 路线 B：Windows 本机自己打包

需要一台 Windows 10/11 64 位电脑，装好 [Python 3.11](https://www.python.org/downloads/windows/)
（安装时务必勾选 **Add python.exe to PATH**）。

打开 PowerShell，进到本目录，逐行执行：

```powershell
python -m pip install --upgrade pip
pip install -r requirements.txt
pyinstaller build.spec --noconfirm
```

产物在 `dist\PDFCompress.exe`，单文件、双击即用，无需安装 Python。
文件名可以随意改成中文（如 `PDF无损压缩.exe`），不影响运行。

---

## 二、怎么用

| 场景 | 操作 |
| --- | --- |
| 压一个文件 | 双击 exe → 点虚线框选 PDF → 点「开始压缩」 |
| 压多个文件 | 选文件时按住 Ctrl 多选，界面会显示合计大小 |
| 图省事 | 把 PDF **拖到 exe 图标上**，启动即自动开压 |
| 批量拖 | 一次拖多个 PDF 到图标上，每个文件弹一个窗口并行压 |
| 加密 PDF | 在「打开密码」栏填密码，再点开始；权限加密（不需密码就能打开）留空即可 |

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
| 图片内嵌元数据 | 视照片而定 | JPEG 的 EXIF / GPS / IPTC / 注释段整段剥掉，码流原样保留 |
| 图像尾部私有流 | 可达图像体积的 1/3 | EOI 之后的厂商数据（如 vivo `streamdataDEGS`，内含一帧欠曝 HDR 原料图与其自带 EXIF）一并剪掉 |

字体、内容流字节、页面尺寸一律不动，所以渲染结果逐字节相同。JPEG 图片是个例外但同样无损：只删 APP 段（相机型号、拍摄时间、GPS 坐标）与 EOI 之后的尾巴，SOS 到 EOI 之间的熵编码数据一个字节都不改，像素不变。若图像流外面套了 ASCII85 编码，连那层 25% 的膨胀一起去掉。

---

## 四、验证过什么

用 2027 届某省化学试卷及其答案两份真实文件实测：

| 文件 | 原始 | 清理后 | 比例 | 页数 | 像素校验 | 文字校验 | 字体 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 化学试卷 | 18,181,598 B | 1,337,671 B | **7.36%** | 8 → 8 | 8/8 页逐字节相同（144 dpi） | 7,825 字 → 7,825 字 | 10 种全保留 |
| 参看文献及答案 | 4,838,415 B | 387,405 B | **8.01%** | 4 → 4 | 4/4 页逐字节相同（144 dpi） | 3,033 字 → 3,033 字 | 9 种全保留 |

加密路径另有一套回归（`ci_check_engine.py`，每次 CI 都跑）：

| 样本 | 结果 |
| --- | --- |
| RC4-40 / RC4-128 / AES-128 / AES-256（user 密码） | 输出未加密、页数不变、文字与渲染像素与原文件一致 |
| owner 密码 | 同样能解 |
| 权限加密（user 密码为空） | 不填密码即可解开 |
| 口令错误 | 退出码非 0，且不产出任何文件 |
| JPEG 图片清理（`photos.pdf`） | 2 张图（含一张 CMYK）EXIF/GPS 全消失，码流未重编码，输出体积 78.4% |

---

## 五、常见问题

**Q：提示「缺少 VCRUNTIME140.dll」？**
exe 是 PyInstaller 单文件包，正常情况下自带运行库。若被杀软隔离，把整个 exe
加白名单即可；本版不再需要任何 `tools/` 目录。

**Q：压完反而变大了？**
内容流本来就是 deflate 的、没有冗余结构时会发生。程序仍会写出文件（便于对比），
直接用原件就好。

**Q：加密 PDF？**
在窗口的「打开密码」栏填密码即可，留空则按无密码尝试（权限加密的文件能直接解开）。
命令行版同样支持：

```
PDFCompress.exe 加密.pdf -p 口令
python pdfstrip.py 加密.pdf -p 口令 -o 输出.pdf
```

口令错误会明确提示「当前填的密码不对」，不会产出损坏文件。
只支持 PDF 标准加密（`/Filter /Standard`，覆盖 RC4 40/128、AES-128、AES-256）；
用数字证书加密的文件不在支持范围内。

**Q：为什么比在线压缩网站慢一点、但效果好得多？**
在线站多是重编码图片（有损），本工具是**结构化去冗余**（无损）。
一份 18 MB 的 Word 试卷，在线站压到 3 MB 且字发虚；本工具压到 1.3 MB 且原样。

**Q：360/火绒报毒？**
PyInstaller 单文件包常被启发式引擎误报。已关闭 UPX 压缩以降低误报率。
介意的话走路线 B 本机打包，或直接分发「源码文件夹 + python」。

---

## 六、命令行版（可选）

不打包也能用：装好 Python 与依赖后，一句命令搞定。

```bash
pip install -r requirements.txt
python pdfstrip.py 试卷.pdf                       # 输出 试卷_cleaned.pdf
python pdfstrip.py 试卷.pdf -o out.pdf --report   # 顺带打印诊断 JSON
python pdfstrip.py 加密.pdf -p 口令 -o 输出.pdf     # 加密文件给口令
```

| 参数 | 作用 |
| --- | --- |
| `-o OUT` | 指定输出路径，默认 `<原名>_cleaned.pdf` |
| `--report` | 打印诊断 JSON：体积构成、对象数、解密/线性化标记 |
| `-p PASSWORD` | 加密 PDF 的打开密码 |
| `--clean-content` | 额外清空内容流里的注释与多余空白（更彻底，但会改动内容流字节） |

---

## 七、目录结构

```
pdfclean/
├─ pdfstrip.py              清理引擎（也可单独当命令行用）
├─ pdfdecrypt.py            加密 PDF 解密（RC4 / AES，纯 Python）
├─ jpegmeta.py              JPEG 元数据段剥离（EXIF/GPS/IPTC，码流不动）
├─ gui.py                   图形界面
├─ build.spec               PyInstaller 打包配置（无外部二进制）
├─ requirements.txt         打包环境依赖
├─ tests/                   回归样本（四种加密 + 权限加密 + 带 EXIF 的照片）
├─ ci_check_engine.py       引擎回归（CI 主入口）
├─ ci_check_gui_env.py      GUI 无控制台环境自检
├─ ci_check_recursive.py    文件夹递归收集自检
└─ .github/workflows/
   └─ build-win.yml         自动构建 Windows exe
```
