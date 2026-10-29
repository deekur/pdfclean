#!/usr/bin/env python3
"""
pdfstrip —— 通用 PDF 深度清理器（零元数据 / 零压缩 / 按需过滤内容流）

设计目标（与旧版 pdfstrip.py 的关键差异）
------------------------------------------------
旧版：删 /Annots /Outlines /AcroForm /Lang 等结构 + 强制 ObjStm 重排 + zlib 重压。
      后果：阅读器里批注、书签、表单、语言属性都会变，而且重压过的流若带
      /DecodeParms 预测器会画错图。

本版：只做「阅读器不可见」的删除，其余一律原样字节搬移。
      1) 不解压、不重压任何流（除非显式 --clean-content 处理文字）
      2) 结构键按白名单保留：/Type /Subtype /Kids /Count /Parent /MediaBox
         /CropBox /Resources /Contents /BBox /Matrix /Width /Height /BitsPerComponent
         /ColorSpace /Length /Filter /DecodeParms ... 等，其余 Catalog/Page 键
         属于「阅读器不渲染」的可删项才删
      3) 字符串级精确删除：/ID /Metadata /PieceInfo /Producer /Creator /Info /Prev
         这些键整块剔除，不是子串替换
      4) --clean-content: 对页面内容流做空白/数字规整（操作码不动），
         绘制结果不变，文字仍可提取（可选，默认关闭）

用法:
    python3 pdfstrip.py 输入.pdf [-o 输出.pdf] [--report] [-p 口令]
    python3 pdfstrip.py 输入.pdf --clean-content [-o 输出.pdf]

保证:
    - 输出体积 <= 输入（只做减法；不可达对象直接被丢弃）
    - 布局按体积择优：same / objstm / table 三选一，三者都是合法结构
      （same 沿用原文件写法，objstm 用 ObjStm+XRef 流，table 用传统 xref 表）
    - 文件头保留原版本号，不写固定注释行（避免同工具二进制指纹）
"""

import argparse
import json
import os
import re
import subprocess
import shutil
import sys
import warnings
import zlib

warnings.filterwarnings("ignore")

from pypdf import PdfReader


# ---------------------------------------------------------------- 子进程静默
# Windows 下 GUI 程序（console=False 打包）调命令行工具时，系统会给每次调用
# 新建一个控制台窗口，于是「每压一个文件闪一次黑窗」。必须显式告诉系统不要建。
# 注意：光设 console=False 不够，那只是主程序不显示控制台；子进程另算。
#
# 两层保险一起上：
#   creationflags=CREATE_NO_WINDOW  —— 根本不创建控制台
#   startupinfo.dwFlags=STARTF_USESHOWWINDOW + wShowWindow=SW_HIDE —— 万一被创建也立刻隐藏
# 两者叠加最稳：某些 Python 版本在传递 pipe 时对单个 flag 的处理有差异。
if sys.platform.startswith("win"):
    _CREATE_NO_WINDOW = 0x08000000
    _SUBPROCESS_KW = {"creationflags": _CREATE_NO_WINDOW}
    try:
        _si = subprocess.STARTUPINFO()
        _si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        _si.wShowWindow = 0      # SW_HIDE
        _SUBPROCESS_KW["startupinfo"] = _si
    except Exception:
        pass
else:
    _SUBPROCESS_KW = {}

from pypdf.generic import (
    ArrayObject, BooleanObject, ByteStringObject, DictionaryObject,
    IndirectObject, NameObject, NullObject, NumberObject, StreamObject,
    TextStringObject,
)

# ---------------------------------------------------------------- 删除清单

# 全字典通用：元数据/隐私键（整块删除，不递归进去）
META_KEYS = frozenset((
    "/Info", "/Metadata", "/PieceInfo", "/LastModified", "/Private",
    "/Producer", "/Creator", "/CreationDate", "/ModDate",
    "/Author", "/Title", "/Subject", "/Keywords", "/Trapped", "/GTS_PDFXVersion",
    "/AAPL:Keywords", "/XMP", "/DocChecksum", "/SourceModified",
))

# Catalog 上可删的键（激进模式：含书签/表单/大纲/可选内容，阅读器里会看不到）
CATALOG_DROP = frozenset((
    "/Metadata", "/PieceInfo", "/Lang", "/ViewerPreferences",
    "/StructTreeRoot", "/MarkInfo", "/Outlines", "/PageLabels", "/Names",
    "/AcroForm", "/OpenAction", "/AA", "/Perms", "/Legal",
    "/Collection", "/Threads", "/PageMode", "/PageLayout",
    "/Dests", "/OutputIntents", "/OutputIntents2",
    "/SpiderInfo", "/NeedsRendering", "/Requirements", "/PieceInfo2",
    "/AF", "/Version", "/Extensions", "/URI", "/SubmitForm",
))
# /OCProperties 不能删：它记录每个 OCG（图层）默认是开还是关。
# 删掉之后所有带 /OC 的对象一律按「可见」渲染——ISO 32000-2 第 268 页
# 那张示意图本来就是默认关闭的图层，删了反而盖住底层图片，画面直接换了。

# Page 上可删的键（含 /Annots：批注/链接/表单域会一起消失）
PAGE_DROP = frozenset((
    "/Tabs", "/Dur", "/Trans", "/B", "/AA", "/Metadata", "/PieceInfo",
    "/StructParents", "/PieceInfo2", "/TemplateInstantiated", "/PresSteps",
    "/VP", "/Annots", "/Annots2", "/Lang", "/SeparationInfo",
    "/PieceInfo3",
))

# 资源字典冗余（/Pattern /Shading 保留：有内容的图案/渐变被删会画不出来）
# 资源字典冗余。/Properties 不能删：BDC/BMC 里的 /OC /Span 等属性名
# 靠它映射到属性字典（图层可见性、ActualText），删了引用会悬空。
RES_DROP = frozenset(("/ProcSet",))


def _sniff_layout(path):
    """判断原文件用的是 ObjStm 打包 / XRef 流 / 传统 xref table。
    只看文件首尾的关键字，不解压全文件（ObjStm 字典本身是明文的）。"""
    try:
        sz = os.path.getsize(path)
        with open(path, "rb") as f:
            head = f.read(min(sz, 1 << 20))
        if sz <= (1 << 20):
            blob = head
        else:
            with open(path, "rb") as f:
                f.seek(max(0, sz - 262144))
                blob = head + f.read()
        has_xrefstm = b"/Type /XRef" in blob or b"/Type/XRef" in blob
        has_objstm = b"/ObjStm" in blob
        return has_objstm, has_xrefstm
    except Exception:
        return False, False


# ---------------------------------------------------------------- 线性化检测

_LIN_RE = re.compile(rb"(\d+)\s+0\s+obj\s*<<\s*/Linearized\b")


def sniff_linearized(path):
    """检测是否为线性化（Fast Web View）PDF。

    线性化文件的特征：文件头之后紧跟一个对象，其字典以 /Linearized 开头，
    形如  << /Linearized 1 /L 总长 /H [提示表偏移 溢页偏移] /O 首页对象号
             /E 主文件结束 /N 页数 /T 第一份 xref 偏移 >>

    返回 dict：
        {"linearized": bool, "obj": 该对象号或 None, "L": int|None,
         "O": int|None, "E": int|None, "N": int|None, "T": int|None,
         "hint_offsets": [int, int] | []}
    只读文件头 4KB，不做全文扫描。"""
    info = {"linearized": False, "obj": None, "L": None, "O": None,
            "E": None, "N": None, "T": None, "hint_offsets": []}
    try:
        with open(path, "rb") as f:
            head = f.read(4096)
        m = _LIN_RE.search(head)
        if not m:
            return info
        info["linearized"] = True
        info["obj"] = int(m.group(1))
        seg = head[m.start():m.start() + 400]
        for key, field in ((rb"/L\s+(\d+)", "L"), (rb"/O\s+(\d+)", "O"),
                           (rb"/E\s+(\d+)", "E"), (rb"/N\s+(\d+)", "N"),
                           (rb"/T\s+(\d+)", "T")):
            mm = re.search(key, seg)
            if mm:
                info[field] = int(mm.group(1))
        mh = re.search(rb"/H\s*\[\s*(\d+)\s+(\d+)\s*\]", seg)
        if mh:
            info["hint_offsets"] = [int(mh.group(1)), int(mh.group(2))]
    except Exception:
        pass
    return info


def _find_qpdf():
    """按优先级找 qpdf：环境变量 → 程序同目录 → 同目录 tools/ → PATH。

    打包成单文件时，spec 里的 binaries 会把 tools/ 下的 qpdf 解压到
    sys._MEIPASS/tools/ 而不是 exe 所在目录，所以两个根都要找。
    带 .exe 的候选只在 Windows 上考虑：源码目录里可能放着 Windows 版 tools/，
    在 Linux/macOS 上跑源码时会先撞上 qpdf.exe 报 Exec format error。
    """
    env = os.environ.get("PDFSTRIP_QPDF")
    if env and os.path.exists(env):
        return env
    win = sys.platform.startswith("win")
    names = ["qpdf.exe", "qpdf"] if win else ["qpdf"]
    roots = []
    if getattr(sys, "frozen", False):
        roots.append(os.path.dirname(os.path.abspath(sys.executable)))
        meipass = getattr(sys, "_MEIPASS", None)     # 单文件模式解压出来的临时根
        if meipass:
            roots.append(meipass)
    else:
        roots.append(os.path.dirname(os.path.abspath(__file__)))
    for base in roots:
        for sub in ("", "tools"):
            for nm in names:
                p = os.path.join(base, sub, nm)
                if os.path.isfile(p):
                    return p
    return shutil.which("qpdf")


def _warn(msg):
    """往 stderr 写一行警告，没有 stderr 就丢弃。

    GUI 版是用 console=False 打包的，此时 sys.stderr 是 None，
    直接 .write() 会抛 AttributeError，把「口令错误」这类正常失败
    变成未捕获异常。诊断信息本来只是给命令行用户看的，丢掉无妨。
    """
    try:
        if sys.stderr is not None:
            sys.stderr.write(msg)
    except Exception:
        pass


def _qpdf_normalize(src, dst, password="", decrypt=False):
    """用 qpdf 把 PDF 转成普通明文结构，一次搞定两件事：

    1) 去线性化 —— 不带 --linearize 时输出天然是非线性化的普通结构
       （/Linearized 字典、提示表、双份 xref 全部消失）。
    2) 解密 —— 带 --decrypt 时同时移除 /Encrypt 和全部加密字典。
       qpdf 的 --decrypt 是一步到位，不像 pypdf 手动重写那样留下 /Encrypt 残键。

    两者合并成一次调用：输入既是线性化又加密时，不必产生两个临时文件。

    返回 (ok, reason)。找不到 qpdf 或执行失败都只返回 False，不抛异常：
    这一步是可选优化，拿不到就按原文件走常规清理，只是少省一点体积，
    不该让整次压缩失败（目标用户机器上可能根本没有 qpdf）。"""
    exe = _find_qpdf()
    if not exe:
        return False, "找不到 qpdf"
    cmd = [exe, "--warning-exit-0"]
    if password:
        cmd.append("--password=" + password)
    if decrypt:
        cmd.append("--decrypt")
    cmd += [src, dst]
    try:
        # **_SUBPROCESS_KW 在 Windows 上会带上 CREATE_NO_WINDOW，避免每压一个文件闪一次黑窗
        r = subprocess.run(cmd, capture_output=True, text=True, errors="replace",
                           **_SUBPROCESS_KW)
    except OSError as e:
        # 可执行文件与本机架构不匹配（如 Windows 版被放到 Linux 上）、权限不足等
        _warn("qpdf 无法执行：%s\n" % e)
        return False, "qpdf 无法执行：%s" % e
    if r.returncode != 0 or not os.path.exists(dst) or os.path.getsize(dst) == 0:
        err = (r.stderr or "").strip()
        _warn(err or "qpdf failed\n")
        last = err.splitlines()[-1] if err else "qpdf 返回码 %d" % r.returncode
        return False, last
    return True, "ok"


def _flate(data, level=9):
    c = zlib.compressobj(level)
    return c.compress(data) + c.flush()


def _mkstemp_path():
    import tempfile
    fd, p = tempfile.mkstemp(suffix=".pdf")
    os.close(fd)
    return p


def _quick_validate(path):
    """去线性化结果的快速自检：能不能打开、页数是否非零、xref 是否自洽。
    不做像素比对（那一步在最终 verify 里）。返回 (ok, reason)。"""
    if not os.path.exists(path) or os.path.getsize(path) < 32:
        return False, "输出为空"
    try:
        r = PdfReader(path, strict=False)
        n = len(r.pages)
        if n == 0:
            return False, "0 页"
        root = r.trailer["/Root"]
        if isinstance(root, IndirectObject):
            root.get_object()
        return True, "%d 页" % n
    except Exception as e:
        return False, "%s: %s" % (type(e).__name__, e)


class Strip:
    def __init__(self, src, password="", clean_content=False):
        self.src = src
        self.tmp = None
        self.clean_content = clean_content
        self.delinearized = False
        self.decrypted = False
        self.lin_info = sniff_linearized(src)
        self.path = src          # 实际读取的路径（可能被去线性化/解密替换）
        self.src_has_objstm, self.src_has_xrefstm = _sniff_layout(src)
        try:
            enc = PdfReader(src, strict=False).is_encrypted
        except Exception:
            enc = False

        # 加密和线性化都交给 qpdf 一次处理：--decrypt 顺手去掉 /Encrypt，
        # 不带 --linearize 的输出天然是普通结构。pypdf 手写重写会留下 /Encrypt
        # 残键，所以原先那条 PyMuPDF 路径整个不需要了（包里也不再带 pymupdf）。
        self.tmp_lin = None
        self.lin_reason = None
        if enc or self.lin_info["linearized"]:
            self.tmp_lin = _mkstemp_path()
            ok, why = _qpdf_normalize(src, self.tmp_lin, password, decrypt=enc)
            if ok:
                valid, vwhy = _quick_validate(self.tmp_lin)
                if valid:
                    self.path = self.tmp_lin
                    self.delinearized = bool(self.lin_info["linearized"])
                    self.decrypted = enc
                else:
                    self.lin_reason = vwhy
            else:
                self.lin_reason = why
            if enc and not self.decrypted:
                # 加密文件拿不到明文版本就没法继续，这里必须停。
                # qpdf 口令错误的报错含 "invalid password"，单独给更好懂的提示。
                low = (self.lin_reason or "").lower()
                if "password" in low or "invalid" in low:
                    raise SystemExit("文件已加密，且当前口令无法解开。请用 -p 提供口令。\n%s"
                                     % self.lin_reason)
                raise SystemExit("文件已加密，解密失败（依赖随包 qpdf）。\n%s"
                                 % (self.lin_reason or "未知原因"))

        self.reader = PdfReader(self.path, strict=False)
        self.header = self._sniff_header(self.path)
        self.keep = []      # 旧对象号（遍历顺序）
        self.objs = {}
        self.deleted = set()  # 明确删除的对象号：重走可达性时禁止复活
        self.newmap = {}
        self.stats = {"annot_dropped": 0, "outline_dropped": 0, "form_dropped": 0,
                      "media_kept": 0, "xmp_nuked": 0, "reencoded": 0}

    # ---------------------------------------------------------- 文件头
    @staticmethod
    def _sniff_header(path):
        """保留原文件版本与 % 注释行，避免所有输出共享同一二进制头。"""
        with open(path, "rb") as f:
            head = f.read(64)
        m = re.match(rb"(%PDF-\d\.\d)", head)
        ver = m.group(1) if m else b"%PDF-1.7"
        # 原 %xxx 注释行若有短行就沿用，否则不写（不写比写固定行更干净）
        tail = head[len(ver):]
        note = b""
        if tail.startswith(b"\r\n%"):
            tail = tail[1:]     # Windows 生成的文件是 \r\n%
        if tail.startswith(b"\n%"):
            line = tail[1:].split(b"\n", 1)
            if line and 4 <= len(line[0]) <= 20:
                note = line[0]
        return ver + b"\n" + (note + b"\n" if note else b"")

    # ---------------------------------------------------------- 可达性遍历
    def _walk(self, obj, seen, known=None):
        # 迭代式遍历，不用递归：深层嵌套的注释链/结构树会把递归栈打爆
        # （ISO 32000-2 那份 1002 页文件里有 8 个页面对象就因此被
        #  RecursionError 吞掉，页面树里留下 8 个 null）。
        # known：scrub 后的对象缓存。重走可达性时必须优先取已清理的对象，
        # 否则 cur.get_object() 会从 reader 拿回原始对象，覆盖 _nullify/
        # 删键的结果；known 里是 NullObject 的只记 seen，引用留空（ser 写 null）。
        stack = [obj]
        while stack:
            cur = stack.pop()
            if isinstance(cur, IndirectObject):
                n = cur.idnum
                if n in seen or n in self.deleted:
                    continue
                seen.add(n)
                if known is not None and n in known:
                    t = known[n]
                else:
                    try:
                        t = cur.get_object()
                    except Exception:
                        continue        # 坏引用：丢弃
                if isinstance(t, NullObject):
                    continue
                self.keep.append(n)
                self.objs[n] = t
                stack.append(t)
            elif isinstance(cur, StreamObject):
                # extend 后再 reverse，等于「按原顺序逐个递归」：
                # 栈是 LIFO，直接 extend 会把兄弟节点的访问顺序倒过来，
                # 输出对象编号跟着变，普通文件就不再和旧版逐字节一致了。
                vals = list(cur.values())
                vals.reverse()
                stack.extend(vals)
            elif isinstance(cur, dict):
                vals = list(cur.values())
                vals.reverse()
                stack.extend(vals)
            elif isinstance(cur, (list, tuple)):
                vals = list(cur)
                vals.reverse()
                stack.extend(vals)

    def collect(self):
        root = self.reader.trailer.raw_get("/Root")
        if not isinstance(root, IndirectObject):
            raise SystemExit("trailer 里没有可用的 /Root，文件可能已损坏。")
        self._walk(root, set())
        self.root_num = root.idnum
        self.root = self.objs[self.root_num]

    # ---------------------------------------------------------- 定向清理
    @staticmethod
    def _typ(o, key="/Type"):
        """取 /Type /Subtype 等名字键；规范允许间接引用，需解引用再比较"""
        v = o.get(key)
        if isinstance(v, IndirectObject):
            try:
                v = v.get_object()
            except Exception:
                return ""
        return str(v or "")

    def _drop(self, d, keys):
        for k in list(d.keys()):
            if str(k) in keys:
                del d[k]

    def scrub(self):
        # Catalog
        if "/Outlines" in self.root:
            self.stats["outline_dropped"] = 1
        if "/AcroForm" in self.root:
            self.stats["form_dropped"] = 1
        self._drop(self.root, CATALOG_DROP)

        for num, o in list(self.objs.items()):
            if isinstance(o, StreamObject):
                # 图像/字体/ICC/嵌入文件：只删元数据键，字节原样搬移
                if self._is_media(o):
                    self.stats["media_kept"] += 1
                # StreamObject 是 dict 子类，嵌套字典统一交给 _deep_drop
                self._deep_drop(o, 0)
                continue
            if not isinstance(o, dict):
                continue
            typ = self._typ(o)

            if typ == "/Page":
                if "/Annots" in o:
                    self.stats["annot_dropped"] += 1
                self._drop(o, PAGE_DROP)
                res = o.get("/Resources")
                if res is not None:
                    res = res.get_object() if isinstance(res, IndirectObject) else res
                    if isinstance(res, dict):
                        self._drop(res, RES_DROP)

            # 全字典通用元数据键（递归深度受限，防止环）
            self._deep_drop(o, 0)
            # 结构性 Metadata 对象整体置空
            if typ in ("/Metadata", "/XMP"):
                self._nullify(num)

        # 二次扫描：/Type /Metadata 独立对象（保持原脚本的修复）
        for num, o in list(self.objs.items()):
            if isinstance(o, dict) and self._typ(o) in ("/Metadata", "/XMP"):
                self._nullify(num)

        # 普通流里的 XMP 包：只对明确的 Metadata/XML 流做（保守策略，
        # 避免 Form XObject / CharProcs / 内容流被误杀成丢图或白页）
        self._xmp_pack_scan()

        if self.clean_content:
            self._clean_pages_content()

    def _xmp_pack_scan(self):
        for num, o in list(self.objs.items()):
            if not isinstance(o, StreamObject) or self._is_media(o):
                continue
            # 白名单：只有明说是 Metadata/XML 的流才允许整流置空。
            # XMP 藏在绘图流里无法与真实绘制数据区分，宁可不动。
            if self._typ(o) not in ("/Metadata", "/XMP") and \
               self._typ(o, "/Subtype") != "/XML":
                continue
            f = o.get("/Filter")
            if isinstance(f, IndirectObject):
                try:
                    f = f.get_object()
                except Exception:
                    f = None
            filt = str(f or "").upper()
            # /Filter 可能是数组形式 [ /ASCII85Decode /FlateDecode ]
            if "/FLATEDECODE" not in filt and filt != "":
                continue
            try:
                ln = o.get("/Length", 1 << 30)
                if isinstance(ln, IndirectObject):
                    ln = ln.get_object()
                if int(ln) > 1 << 22:   # 4 MB 上限
                    continue
                head = (o.get_data() or b"")[:600]
            except Exception:
                continue
            if b"<x:xmpmeta" in head or b"<rdf:RDF" in head:
                self._nullify(num)
                self.stats["xmp_nuked"] += 1

    def _nullify(self, num):
        self.deleted.add(num)
        if num in self.objs:
            self.objs[num] = NullObject()
            try:
                self.keep.remove(num)
            except ValueError:
                pass

    def _is_media(self, o):
        """图像 / 字体程序 / ICC / 嵌入文件：这些流的字节一旦变动，
        阅读器渲染结果或字体度量就会变，因此只删字典键、不动数据。"""
        typ = self._typ(o)
        sub = self._typ(o, "/Subtype")
        if typ == "/XObject" and sub == "/Image":
            return True
        if typ == "/Font" or typ in ("/FontDescriptor",):
            return True
        if any(str(k).startswith("/FontFile") for k in o.keys()):
            return True
        if sub in ("/Image", "/CIDFontType0C", "/OpenType", "/TrueType"):
            return True
        if typ == "/EmbeddedFile":
            return True
        return bool(o.get("/ImageMask") is not None)

    def _deep_drop(self, o, depth):
        if depth > 40:
            return
        if isinstance(o, dict):
            self._drop(o, META_KEYS)
            for v in o.values():
                if isinstance(v, IndirectObject):
                    continue        # 独立对象在 objs 里会单独处理
                self._deep_drop(v, depth + 1)
        elif isinstance(o, (list, tuple)):
            for v in o:
                if isinstance(v, IndirectObject):
                    continue
                self._deep_drop(v, depth + 1)

    # ---------------------------------------------------------- 内容流（可选）
    def _clean_pages_content(self):
        """BT/ET 块规整 + 操作符归一，绘制结果不变。
        按原始 /Contents 对象号分组：多个页面共享同一内容流时只重建一次。
        只更新页面引用、不删旧流——旧流若仍被处理失败的页面引用必须保留，
        全部改指新流后，run() 里的可达性重走会让它自然不可达而被丢弃。"""
        # 新对象号必须同时避开：① reader 里所有已有对象号（/Size 只是下界，
        # 有些坏文件 /Size 偏小）② 被 _nullify 删掉的旧号（悬空引用会错接）。
        # 从两者最大值之上开始，才保证不与任何旧号碰撞。
        sz = self.reader.trailer.get("/Size")
        if isinstance(sz, IndirectObject):      # /Size 也可能是间接引用
            sz = sz.get_object()
        nxt = max(int(sz or 0),
                  (max(self.objs) + 1) if self.objs else 1,
                  (max(self.deleted) + 1) if self.deleted else 1)
        groups = {}     # key: 旧对象号元组 -> [页面字典, ...]
        for o in list(self.objs.values()):
            if not isinstance(o, dict) or self._typ(o) != "/Page":
                continue
            ids = self._content_ids(o.get("/Contents"))
            key = tuple(sorted(ids))   # [a b] 与 [b a] 视为同一组
            if key:
                groups.setdefault(key, []).append(o)
        for key, pages in groups.items():
            try:
                raw = b"\n".join(
                    self._concat_contents(IndirectObject(n, 0, self.reader))
                    for n in key)
            except Exception:
                continue
            if not raw:
                continue
            # 安全闸：带内联图像(BT/ET 之外的二进制块)或字体内联的复杂流不做重排
            if b"BI" in raw and b"ID" in raw:
                continue
            new = _rebuild_content(raw)
            if new is None or new == raw:
                continue
            # 这组只分配一个新对象，所有共享页统一指向它
            newnum = max(nxt, max(self.keep) + 1 if self.keep else 1)
            nxt = newnum + 1
            newobj = StreamObject()
            newobj._data = new
            newobj[NameObject("/Length")] = NumberObject(len(new))
            # keep 会在 run() 里重置，这里只登记 objs，重走可达性自会收录
            self.objs[newnum] = newobj
            ref = IndirectObject(newnum, 0, self.reader)
            for pg in pages:
                pg[NameObject("/Contents")] = ref
            # 注意：不 pop 旧内容流、不加入 self.deleted。
            self.stats["content_groups"] = self.stats.get("content_groups", 0) + 1
            self.stats["content_pages"] = self.stats.get("content_pages", 0) + len(pages)

    def _content_ids(self, cont):
        """展开 /Contents 引用（含间接指向数组的形式），返回对象号列表。
        只取对象号，读数据时另行 IndirectObject(n,0,reader) 现取，避免缓存干扰。"""
        if isinstance(cont, IndirectObject):
            try:
                val = cont.get_object()
            except Exception:
                return []
            if isinstance(val, (list, ArrayObject)):
                # 间接指向数组：展开元素
                return [p.idnum for p in val if isinstance(p, IndirectObject)]
            # 间接指向单个流：保留原始对象号（最常见情况）
            return [cont.idnum]
        if isinstance(cont, (list, ArrayObject)):
            return [p.idnum for p in cont if isinstance(p, IndirectObject)]
        # 直接内联 StreamObject 不合法，忽略
        return []

    def _concat_contents(self, cont):
        pieces = []
        objs = cont if isinstance(cont, (list, ArrayObject)) else [cont]
        for p in objs:
            p = p.get_object() if isinstance(p, IndirectObject) else p
            if isinstance(p, StreamObject):
                pieces.append(p.get_data() or b"")
        return b"\n".join(pieces)

    # ---------------------------------------------------------- 序列化
    @staticmethod
    def _name(raw):
        """序列化 PDF 名字对象。先落成字节再逐字节转义——名字转义只允许
        两位十六进制（#XX），按码点转义遇到 CJK 会写出 #4E2D 这种四位数，
        解析器会把 4E 当一字节、2D 当字面字符，名字直接坏掉。"""
        if not raw.startswith("/"):
            raw = "/" + raw
        body = raw[1:].encode("latin-1", "replace")   # 非 Latin-1 字符落成 b"? "
        out = []
        for b in body:
            if b in b"#/()<>[]{}/% " or b < 33 or b > 126:
                out.append("#%02X" % b)
            else:
                out.append(chr(b))
        return ("/" + "".join(out)).encode("latin-1")

    def ser(self, o):
        if isinstance(o, IndirectObject):
            n = self.newmap.get(o.idnum)
            return b"null" if n is None else b"%d 0 R" % n
        if isinstance(o, NameObject):
            return self._name(str(o))
        if isinstance(o, BooleanObject):
            # 别用 if o：pypdf 的 BooleanObject 没定义 __bool__，空容器以外
            # 的对象真值恒为 True，BooleanObject(False) 会被写成 "true"。
            # 后果实打实：/Group 的 /K（是否击穿透明组）一翻转，整张图渲染
            # 就换了样子（ISO 32000-2 第 268 页的示例图就是这么被搞坏的）。
            return b"true" if o.value else b"false"
        if isinstance(o, bool):
            return b"true" if o else b"false"
        if isinstance(o, NullObject) or o is None:
            return b"null"
        if isinstance(o, int):
            return str(o).encode()
        if isinstance(o, float):
            # 对象字典里的浮点（/MediaBox /Matrix 等）做 round-trip 无损输出；
            # PDF 数字不接受指数写法，科学计数法时展开为定点
            if o != o or o in (float("inf"), float("-inf")):
                return b"0"     # 规范不允许 inf/nan，坏文件退化为 0
            if o == int(o) and abs(o) < 1e15:
                return str(int(o)).encode()
            s = repr(o)
            if "e" in s or "E" in s:
                # 从 repr 字符串构造 Decimal（不是 Decimal(o)）：float 构造
                # 会带出全部二进制精度噪声，1e-20 会膨胀成 40 位小数
                from decimal import Decimal
                s = format(Decimal(s), "f")
            return s.encode()
        if isinstance(o, (TextStringObject, ByteStringObject)):
            try:
                raw = o.original_bytes
            except Exception:
                # hex string 是字节串，latin-1 与 _name 的编码策略一致
                raw = str(o).encode("latin-1", "replace")
            return b"<" + raw.hex().encode() + b">"
        if isinstance(o, StreamObject):
            raise TypeError("stream 不能内联")
        if isinstance(o, dict):
            parts = [b"<<"]
            for k, v in o.items():
                # 键名同样走 _name() 转义：含空格/#/括号的非法名字
                # 直接 encode 会被解析器当成两个 token
                parts.append(self._name(str(k)) + b" " + self.ser(v) + b" ")
            parts.append(b">>")
            return b" ".join(parts)
        if isinstance(o, (list, tuple, ArrayObject)):
            return b"[" + b" ".join(self.ser(v) for v in o) + b"]"
        return b"null"

    # ---------------------------------------------------------- 构建
    def build(self, layout="same"):
        """layout:
        "same"    —— 尽量沿用原文件的存放方式（原用 ObjStm 就打包，原点 xref 流就写流）
        "table"   —— 强制传统 xref table + 不打包（体积会变大，兼容性最好）
        "objstm"  —— 强制 ObjStm 打包 + xref 流（体积最小）
        任何情况下都：不重压流、不重编码图像/字体。
        """
        self.newmap = {num: i + 1 for i, num in enumerate(self.keep)}
        n = len(self.keep)

        # 普通对象（可进 ObjStm）与流对象分流
        plain, streams = [], []
        for num in self.keep:
            o = self.objs[num]
            newno = self.newmap[num]
            if isinstance(o, StreamObject):
                # 编码后原始字节的存放位置随 pypdf 版本不同：
                #   3.x —— _data 就是编码字节，get_data() 才解码
                #   4.x —— _data_encoded 是编码字节；它为 None 时 _data 是解码结果
                # 按属性存在性分支才安全：4.x 下 _data_encoded 为 None 而 _data
                # 非空，说明已被解码，此时必须去掉 /Filter 否则阅读器会再解一次
                if hasattr(o, "_data_encoded"):
                    raw = o._data_encoded
                else:
                    raw = getattr(o, "_data", None)
                if raw is None:
                    self.stats["reencoded"] += 1
                    body = o.get_data()
                    drop = ("/Length", "/Filter", "/DecodeParms")
                else:
                    body = raw
                    drop = ("/Length",)
                d = DictionaryObject()
                for k, v in o.items():
                    if str(k) in drop:
                        continue
                    d[k] = v
                d[NameObject("/Length")] = NumberObject(len(body))
                streams.append((newno, self.ser(d), body))
            else:
                plain.append((newno, self.ser(o)))

        # plain 为空时没有可打包对象，ObjStm 不写，xref 流也不留悬空 type-2 条目
        use_objstm = ((layout == "objstm") or (layout == "same" and self.src_has_objstm)) and bool(plain)
        use_xrefstm = (layout == "objstm") or (layout == "same" and self.src_has_xrefstm)
        if use_objstm:
            # ObjStm 里的对象在 xref 流里以 type-2 条目索引，传统表写不了；
            # sniff 误判 src_has_xrefstm 时必须强制配对，否则结构非法
            use_xrefstm = True
        if not use_objstm and not use_xrefstm:
            return self._emit_table(plain, streams)

        # ObjStm 编号放在所有普通对象之后，XRef 流最后
        objstm_no = n + 1 if use_objstm else None
        xref_no = (n + 2) if use_objstm else (n + 1)

        out = bytearray(self.header)
        offsets = {}
        objstm_idx = {}

        if use_objstm and plain:
            hdr, bodies, cur = [], [], 0
            for newno, data in plain:
                # 单 ObjStm 索引字段 2 字节，超过 65535 个对象必须拆分
                objstm_idx[newno] = len(objstm_idx)
                hdr.append(b"%d %d" % (newno, cur))
                bodies.append(data + b"\n")
                cur += len(data) + 1
            header = b" ".join(hdr) + b"\n"
            stmd = header + b"".join(bodies)
            comp = _flate(stmd)
            offsets[objstm_no] = len(out)
            out += (b"%d 0 obj\n<</Type /ObjStm /N %d /First %d /Filter /FlateDecode /Length %d>>\n"
                    b"stream\n" % (objstm_no, len(plain), len(header), len(comp)))
            out += comp + b"\nendstream\nendobj\n"
        elif plain:
            for newno, data in plain:
                offsets[newno] = len(out)
                out += b"%d 0 obj\n" % newno + data + b"\nendobj\n"

        for newno, db, body in streams:
            offsets[newno] = len(out)
            out += b"%d 0 obj\n" % newno + db + b"\nstream\n" + body + b"\nendstream\nendobj\n"

        size = xref_no + 1
        offsets[xref_no] = len(out)
        if use_xrefstm:
            # /W 第三字段（流内序号）宽度不能写死 2 字节：对象数超过 65536
            # 时序号会溢出，整份重建失败（ISO 32000-2 那份 9 万对象就是）。
            # 按最大偏移量算需要几字节，最少 2。
            idx_max = max(objstm_idx.values()) if objstm_idx else 0
            off_max = max(offsets.values()) if offsets else 0
            w2 = max(2, (idx_max.bit_length() + 7) // 8)
            w1 = max(4, (off_max.bit_length() + 7) // 8)
            w = [1, w1, w2]
            rows = bytearray(bytes([0]) + (0).to_bytes(w1, "big") + (65535).to_bytes(w2, "big"))
            for i in range(1, size):
                if i in offsets:
                    rows += bytes([1]) + offsets[i].to_bytes(w1, "big") + (0).to_bytes(w2, "big")
                else:
                    rows += (bytes([2]) + (objstm_no or 0).to_bytes(w1, "big")
                             + objstm_idx.get(i, 0).to_bytes(w2, "big"))
            xc = _flate(bytes(rows))
            out += (b"%d 0 obj\n<</Type /XRef /Size %d /W [%d %d %d] /Root %d 0 R "
                    b"/Filter /FlateDecode /Length %d>>\nstream\n"
                    % (xref_no, size, w[0], w[1], w[2], self.newmap[self.root_num], len(xc)))
            out += xc + b"\nendstream\nendobj\n"
            out += b"startxref\n%d\n%%%%EOF\n" % offsets[xref_no]
        else:
            out += b"xref\n0 %d\n0000000000 65535 f \n" % size
            for i in range(1, size):
                out += b"%010d 00000 n \n" % offsets.get(i, 0)
            out += (b"trailer\n<</Size %d /Root %d 0 R>>\nstartxref\n%d\n%%%%EOF\n"
                    % (size, self.newmap[self.root_num], offsets[xref_no]))
        return bytes(out)

    def _emit_table(self, plain, streams):
        """传统 xref table，不打包 ObjStm（旧版写法，体积偏大）。"""
        out = bytearray(self.header)
        offsets = {}
        for newno, data in plain:
            offsets[newno] = len(out)
            out += b"%d 0 obj\n" % newno + data + b"\nendobj\n"
        for newno, db, body in streams:
            offsets[newno] = len(out)
            out += b"%d 0 obj\n" % newno + db + b"\nstream\n" + body + b"\nendstream\nendobj\n"
        size = len(self.keep) + 1
        xref_pos = len(out)
        out += b"xref\n0 %d\n0000000000 65535 f \n" % size
        for i in range(1, size):
            out += b"%010d 00000 n \n" % offsets.get(i, 0)
        out += (b"trailer\n<</Size %d /Root %d 0 R>>\nstartxref\n%d\n%%%%EOF\n"
                % (size, self.newmap[self.root_num], xref_pos))
        return bytes(out)

    # ---------------------------------------------------------- 主流程
    def run(self, dst):
        self.collect()
        self.scrub()
        # scrub 只删引用不删对象：bookmarks/表单/注释等子树仍留在 keep 里。
        # 以清理后的 self.objs 为缓存重走一遍可达性，把不可达子树真正丢掉。
        # old 是 scrub + 可选 clean_content 之后的快照；重走可达性只认它，
        # 不认 reader 里的原始对象，避免 _nullify 的结果被覆盖
        old = self.objs
        self.keep, self.objs = [], {}
        root = self.reader.trailer.raw_get("/Root")
        self._walk(root, set(), old)
        self.root_num = root.idnum
        if self.root_num not in self.objs:
            raise SystemExit("清理后 /Root 不可达，文件可能已损坏。")
        self.root = self.objs[self.root_num]
        # 基准体积取「真正被清理的那个文件」：去线性化后用的是临时普通 PDF，
        # 这样 ratio 反映的是清理本身的收益，而不是把去线性化的开销算进来。
        # path != src 说明当前处理的是解密/去线性化后的临时文件，基准也用它，
        # 否则分母是加密原文、分子是明文输出，ratio 口径不一致
        base_path = self.path if self.path != self.src else self.src
        base_size = os.path.getsize(base_path)
        src_size = os.path.getsize(self.src)

        # 三种布局全部试算取最小（代价只是多两次内存拼装，文件级开销可忽略）。
        # 不能「首个 <= 基准就 break」：去线性化后的基准本身很紧凑，
        # same 刚过线就停会错过小得多的 objstm（海南.pdf 相差 2.5%）。
        tries = ["same", "objstm", "table"]
        best = None
        for layout in tries:
            try:
                data = self.build(layout)
            except Exception:
                continue        # 单个布局失败不拖垮整个清理
            if best is None or len(data) < len(best[1]):
                best = (layout, data)
        if best is None:
            raise SystemExit("所有布局均构建失败")
        layout, data = best
        with open(dst, "wb") as f:
            f.write(data)
        info = {"bytes": len(data), "original": src_size,
                "ratio": round(len(data) / src_size * 100, 1),
                "objects": len(self.keep), "layout": layout,
                "compressed": False,
                "clean_content": self.clean_content, **self.stats}
        if self.decrypted:
            info["decrypted"] = True
        if self.lin_info["linearized"]:
            info["linearized_in"] = True
            info["delinearized"] = self.delinearized
            if self.delinearized:
                info["baseline"] = base_size
                info["ratio_vs_deflinear"] = round(len(data) / base_size * 100, 1)
                info["lin_saved"] = src_size - base_size
            elif self.lin_reason:
                info["lin_error"] = self.lin_reason
        return info


# ---------------------------------------------------------------- 内容流重建

_TOKEN = re.compile(
    rb"(?P<str>\((?:[^()\\]|\\.|\((?:[^()\\]|\\.)*\))*\))"
    rb"|(?P<hex><[0-9A-Fa-f\s]*>)"
    rb"|(?P<dict><<|>>)"
    rb"|(?P<arr>[\[\]])"
    rb"|(?P<name>/[^\s/\[\]()<>{}%]*)"
    rb"|(?P<num>[-+]?(?:\d+\.?\d*|\.\d+))"
    rb"|(?P<op>[A-Za-z'\"*][A-Za-z0-9'\"*]*)"
    rb"|(?P<comment>%[^\r\n]*)"
    rb"|(?P<ws>\s+)"
    rb"|(?P<other>.)",
    re.S,
)

_PAREN_NEST = re.compile(rb"\((?:[^()\\]|\\.)*\(")


def _has_nested_paren(data):
    """字符串字面量里嵌套括号（(a(b)c)）无法用单层正确定界，
    重建会破坏内容，直接放弃。"""
    return bool(_PAREN_NEST.search(data))


def _rebuild_content(data):
    """内容流的保守规整：统一空白、数字取最短表示、操作码后补单空格。
    不重排任何操作码，不跨块移动，绘制结果逐字节等价（仅文本量变化）。"""
    if b"BT" not in data or _has_nested_paren(data):
        return None
    out = bytearray()
    tokens = []
    for m in _TOKEN.finditer(data):
        kind = m.lastgroup
        # 空白丢弃；注释必须整段丢弃（否则注释后的换行被吞，
        # 下一条指令会被拼进注释行而失效）
        if kind == "ws" or kind == "comment":
            continue
        tokens.append((kind, m.group()))
    # 规整化：数字做无损压缩表示，操作码前补空格
    for kind, tok in tokens:
        if kind == "num":
            out += _norm_num(tok) + b" "
        else:
            out += tok + b" "
    return bytes(out).strip()


def _norm_num(tok):
    """无损压缩数字字面量的表示：去前导 +、去小数尾随零。
    不做浮点转换，也不动前导零——数值与原字面量严格等值。"""
    t = tok.decode("latin-1").lstrip("+")
    if "." in t:
        t = t.rstrip("0").rstrip(".")
        if t in ("", "-"):
            t = "0"
    return t.encode("latin-1")


# ---------------------------------------------------------------- 校验

def render_check(path, timeout=120):
    try:
        import pymupdf as fitz
    except ImportError:
        fitz = None
    if fitz is not None:
        try:
            doc = fitz.open(path)
            try:
                if doc.page_count == 0:
                    return None, "fitz", "0 pages"
                # 抽查首页和末页：只渲染第 1 页会漏掉后续页内容流被改坏
                idxs = [0] if doc.page_count == 1 else [0, doc.page_count - 1]
                details = []
                for i in idxs:
                    pix = doc[i].get_pixmap(dpi=72)
                    if pix.width <= 0 or pix.height <= 0:
                        return False, "fitz", "page %d 空白" % (i + 1)
                    details.append("%dx%d" % (pix.width, pix.height))
                return True, "fitz", ",".join(details)
            finally:
                doc.close()
        except Exception as e:
            return False, "fitz", "%s: %s" % (type(e).__name__, e)
    exe = shutil_which("mutool")
    if not exe:
        return None, "none", "no renderer (need PyMuPDF or mutool)"
    cmd = [exe, "draw", "-F", "png", "-o", os.devnull, "-r", "72", path, "1"]
    try:
        r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           timeout=timeout, **_SUBPROCESS_KW)
        if r.returncode == 0:
            return True, "mutool", "ok"
        err = (r.stderr or b"").decode("utf-8", "replace").strip()
        return False, "mutool", err[:300] or "exit %d" % r.returncode
    except Exception as e:
        return False, "mutool", "%s: %s" % (type(e).__name__, e)


def shutil_which(name):
    import shutil
    return shutil.which(name)


def verify(path):
    with open(path, "rb") as f:
        d = f.read()
    rep = {
        "size": len(d),
        "eof": d.count(b"%%EOF"),
        "header": d[:16].decode("latin-1", "replace").strip(),
        "xref": "table" if b"\nxref\n" in d else (
            "stream" if (b"/Type /XRef" in d or b"/Type/XRef" in d) else "none"),
    }
    counts = {}
    for pat in (b"/Info", b"/ID", b"/Prev", b"/Metadata", b"/XMP", b"/PieceInfo",
                b"Producer", b"Chromium", b"Skia", b"pypdf", b"WeasyPrint",
                b"Ghostscript", b"/ObjStm", b"/Creator", b"/Author", b"/CreationDate"):
        counts[pat.decode()] = d.count(pat)
    rep["bytes"] = counts
    try:
        r = PdfReader(path, strict=False)
        rep["pages"] = len(r.pages)
        rep["trailer_keys"] = sorted(str(k) for k in r.trailer.keys())
        rep["page_keys"] = sorted(str(k) for k in r.pages[0].keys()) if len(r.pages) else []
        root = r.trailer["/Root"]
        if isinstance(root, IndirectObject):
            root = root.get_object()
        rep["catalog_keys"] = sorted(str(k) for k in root.keys())
        try:
            rep["meta_empty"] = (r.metadata is None)
        except Exception:
            rep["meta_empty"] = True
        # 语义自检：不只是子串计数
        texts, annots = 0, 0
        for p in r.pages:
            try:
                texts += len(p.extract_text() or "")
            except Exception:
                pass
            try:
                a = p.get("/Annots")
                a = a.get_object() if isinstance(a, IndirectObject) else a
                annots += len(a or [])
            except Exception:
                pass
        rep["extract_text_chars"] = texts
        rep["annots"] = annots
    except Exception as e:
        rep["read_error"] = "%s: %s" % (type(e).__name__, e)
    try:
        ok, backend, detail = render_check(path)
        rep["render_ok"] = ok
        rep["render_backend"] = backend
        rep["render_detail"] = detail
    except Exception as e:
        rep["render_ok"] = False
        rep["render_error"] = str(e)
    return rep


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("-o", "--out")
    ap.add_argument("--report", action="store_true", help="打印校验 JSON")
    ap.add_argument("-p", "--password", default="", help="加密 PDF 的用户口令")
    ap.add_argument("--clean-content", action="store_true",
                    help="规整内容流空白/数字（视觉不变，文字仍可提取），默认关闭")
    a = ap.parse_args()
    if a.out:
        dst = a.out
    else:
        # 默认输出到原文件所在目录：<原名>_cleaned.pdf
        d, base = os.path.split(os.path.abspath(a.src))
        stem = os.path.splitext(base)[0]
        # 已经是 _cleaned 结尾的输入，避免叠成 _cleaned_cleaned
        if not stem.endswith("_cleaned"):
            stem += "_cleaned"
        dst = os.path.join(d, stem + ".pdf")
        if os.path.abspath(dst) == os.path.abspath(a.src):
            dst = os.path.join(d, stem + "_2.pdf")
    st = None
    try:
        st = Strip(a.src, a.password, clean_content=a.clean_content)
        info = st.run(dst)
    finally:
        if st is not None:
            for p in (st.tmp, st.tmp_lin):
                if p and os.path.exists(p):
                    try:
                        os.unlink(p)
                    except OSError:
                        pass
    info["output"] = dst
    print(json.dumps(info, ensure_ascii=False, indent=2))
    if a.report:
        print(json.dumps(verify(dst), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
