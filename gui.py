#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PDF 无损压缩 —— 图形界面

设计约束（都是为了能打成单文件 exe 后稳定运行）
------------------------------------------------
1. 只依赖标准库 tkinter + 本目录的 pdfstrip，不引入任何 GUI 第三方包
2. 直接 import pdfstrip 调引擎，不起子进程（exe 内没有独立的 python.exe）
3. 压缩在后台线程跑，主线程只负责界面刷新，避免「未响应」
4. 全程 try/except，出错弹窗说人话，绝不静默失败

用法
    PDF压缩.exe                   # 双击，点区域选文件，或点「选择文件夹」
    PDF压缩.exe 试卷.pdf 答案.pdf   # 把文件拖到 exe 图标上
    PDF压缩.exe D:\\扫描件           # 把文件夹拖到 exe 图标上，递归处理其中所有 PDF
    程序开着时也可以直接把文件/文件夹拖进窗口

关于递归
--------
拖进来的文件夹会递归扫出里面所有 .pdf（含多层子目录），按需要排队处理。
两类文件会被自动跳过，免得白跑或越压越多：
    1. 本工具自己产出的 *_cleaned.pdf（再压一遍没有意义）
    2. 输出目标就是自己的文件（同名情况下 out_name 已退到 _2，这里只做兜底）

关于「拖进窗口」
----------------
tkinter 本身不支持系统拖放，收到的是普通鼠标事件，拿不到文件路径。
这里用 ctypes 直接接 Windows 的 DragAcceptFiles + WM_DROPFILES：
不引入 tkinterdnd2 之类的第三方包，exe 体积零增长。
非 Windows 平台自动降级为「点按钮选择」，不会报错。
"""

import os
import queue
import subprocess
import sys
import threading
import traceback

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

# Windows 拖放支持（可选；失败就降级，绝不影响主功能）
_DND_OK = False
if sys.platform.startswith("win"):
    try:
        import ctypes
        from ctypes import wintypes

        WM_DROPFILES = 0x0233
        GWL_WNDPROC = -4

        _user32 = ctypes.WinDLL("user32", use_last_error=True)
        _shell32 = ctypes.WinDLL("shell32", use_last_error=True)

        _WNDPROC = ctypes.WINFUNCTYPE(
            ctypes.c_void_p, wintypes.HWND, wintypes.UINT,
            wintypes.WPARAM, wintypes.LPARAM)

        _user32.CallWindowProcW.argtypes = [
            ctypes.c_void_p, wintypes.HWND, wintypes.UINT,
            wintypes.WPARAM, wintypes.LPARAM]
        _user32.CallWindowProcW.restype = ctypes.c_void_p
        # SetWindowLongPtrW 只在 64 位导出；32 位的 user32 里叫 SetWindowLongW。
        # 不处理这个差异，32 位 Python 打包的 exe 会在这一步抛 AttributeError。
        if hasattr(_user32, "SetWindowLongPtrW"):
            _set_long, _get_long = _user32.SetWindowLongPtrW, _user32.GetWindowLongPtrW
        else:
            _set_long, _get_long = _user32.SetWindowLongW, _user32.GetWindowLongW
        _set_long.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
        _set_long.restype = ctypes.c_void_p
        _get_long.argtypes = [wintypes.HWND, ctypes.c_int]
        _get_long.restype = ctypes.c_void_p
        _shell32.DragAcceptFiles.argtypes = [wintypes.HWND, wintypes.BOOL]
        _shell32.DragQueryFileW.argtypes = [
            ctypes.c_void_p, wintypes.UINT, wintypes.LPWSTR, wintypes.UINT]
        _shell32.DragQueryFileW.restype = wintypes.UINT
        _shell32.DragFinish.argtypes = [ctypes.c_void_p]
        _user32.DefWindowProcW.argtypes = [
            wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        _user32.DefWindowProcW.restype = ctypes.c_void_p
        _user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
        _user32.GetWindow.restype = wintypes.HWND

        def _dropped_paths(hdrop):
            """从 HDROP 取出全部路径。"""
            n = _shell32.DragQueryFileW(hdrop, 0xFFFFFFFF, None, 0)
            out = []
            for i in range(n):
                ln = _shell32.DragQueryFileW(hdrop, i, None, 0)
                buf = ctypes.create_unicode_buffer(ln + 1)
                _shell32.DragQueryFileW(hdrop, i, buf, ln + 1)
                if buf.value:
                    out.append(buf.value)
            return out

        _DND_OK = True
    except Exception:      # pragma: no cover - 非 Windows 或 API 缺失
        _DND_OK = False


def _spawn_quiet(argv):
    """起一个不弹控制台的子进程（用于打开目录这种可有可无的辅助动作）。

    os.system 在 Windows 上会闪一下控制台窗口；虽然这里只有非 Windows 分支用，
    但统一走 subprocess + 隐藏窗口参数更稳，避免以后改了平台判断又漏掉。
    """
    try:
        kw = {}
        if sys.platform.startswith("win"):
            kw["creationflags"] = 0x08000000          # CREATE_NO_WINDOW
        subprocess.Popen(argv, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, **kw)
    except Exception:
        pass


# 打包后 exe 所在目录要进 sys.path，才能 import 同目录的 pdfstrip
_here = os.path.dirname(os.path.abspath(
    sys.executable if getattr(sys, "frozen", False) else __file__))
if _here not in sys.path:
    sys.path.insert(0, _here)

from pdfstrip import Strip  # noqa: E402


APP_TITLE = "PDF 无损压缩"
DROP_TEXT = "把 PDF 或文件夹拖到这里\n\n或点击选择"
DROP_TEXT_OFF = "把 PDF 或文件夹拖到这里\n\n或点击选择"
OUT_HINT = "输出到源文件同目录，文件名后加 _cleaned，不覆盖原文件"
CLEANED_SUFFIX = "_cleaned"

# 递归时一律不进的目录：隐藏目录（.git/.cache 之类）+ 常见系统回收站
_SKIP_DIRS = {"__MACOSX", "$RECYCLE.BIN", "System Volume Information"}


def human(n):
    """字节数转人话。"""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return ("%.0f %s" % (n, unit)) if unit == "B" else ("%.2f %s" % (n, unit))
        n /= 1024.0
    return "%.2f GB" % n


def is_cleaned(path):
    """是不是本工具已经压过的产物。"""
    stem = os.path.splitext(os.path.basename(path))[0]
    return stem.lower().endswith(CLEANED_SUFFIX)


def out_name(src):
    """算输出路径：<原名>_cleaned.pdf，已在同目录且重名时退到 _2。"""
    d, base = os.path.split(os.path.abspath(src))
    stem, ext = os.path.splitext(base)
    if not stem.lower().endswith(CLEANED_SUFFIX):
        stem += CLEANED_SUFFIX
    dst = os.path.join(d, stem + (ext or ".pdf"))
    if os.path.abspath(dst) == os.path.abspath(src):
        dst = os.path.join(d, stem + "_2" + (ext or ".pdf"))
    return dst


def collect_pdfs(paths, skips=None):
    """把「文件和文件夹混着的入参」展开成一份 PDF 绝对路径清单。

    - 文件：扩展名是 .pdf 就收
    - 目录：os.walk 递归，跳过隐藏目录与系统垃圾目录
    - 排序：结果按路径排序，保证每次跑顺序一致（用户看着不跳）
    - skips：可选 list，用来回收被跳过的文件及原因，界面要如实告诉用户
    """
    found = []
    seen = set()
    skipped = skips if skips is not None else []

    for raw in paths:
        if not raw:
            continue
        p = os.path.abspath(os.path.expanduser(raw))
        if os.path.isfile(p):
            if p.lower().endswith(".pdf"):
                if p not in seen:
                    seen.add(p)
                    found.append(p)
            continue
        if not os.path.isdir(p):
            skipped.append((p, "路径不存在"))
            continue

        # 目录：递归。onerror 吃掉权限错误，别让整个扫描崩掉
        def _onerror(err, path=p):
            skipped.append((getattr(err, "filename", path),
                            "无法读取目录（权限不足）"))

        for dirpath, dirnames, filenames in os.walk(p, onerror=_onerror):
            # 就地改 dirnames 就能阻止 os.walk 继续往下走
            dirnames[:] = sorted(
                d for d in dirnames
                if not d.startswith(".") and d not in _SKIP_DIRS)
            for fn in sorted(filenames):
                if fn.startswith(".") or not fn.lower().endswith(".pdf"):
                    continue
                fp = os.path.join(dirpath, fn)
                if is_cleaned(fp):
                    skipped.append((fp, "已是压缩产物"))
                    continue
                if fp in seen:
                    continue
                seen.add(fp)
                found.append(fp)

    found.sort()
    return found


def compress_one(src):
    """压一个文件，返回 (输出路径, 摘要dict)。异常原样抛出，由调用方处理。"""
    dst = out_name(src)
    st = None
    try:
        st = Strip(src, "", clean_content=False)
        info = st.run(dst)
    finally:
        # 加密临时文件、去线性化/解密临时文件都要清掉，Windows 下不清理会占着句柄。
        # tmp 已不再由解密流程产生（解密改走 qpdf，落在 tmp_lin），但保留兼容。
        if st is not None:
            for p in (st.tmp, st.tmp_lin):
                if p and os.path.exists(p):
                    try:
                        os.unlink(p)
                    except OSError:
                        pass
    return dst, info


class App(object):
    def __init__(self, root, initial=(), title=None, autorun=False):
        self.root = root
        self.files = list(initial)
        self.skipped = []
        self.busy = False
        self.q = queue.Queue()
        self.last_out = None
        self._autorun = autorun
        self._dropped = []          # 窗口拖放收到的路径（主线程写入）
        self._dnd_hooks = []        # 保活：WNDPROC 回调和原过程地址
        self._dnd_hooks_old = {}    # hwnd -> 原窗口过程地址
        self._dnd_ready = False

        root.title(title or APP_TITLE)
        root.geometry("620x500")
        root.minsize(520, 440)
        try:
            root.iconbitmap(default="")
        except Exception:
            pass

        # ---- 顶部标题
        head = tk.Frame(root, bg="#2b579a", height=56)
        head.pack(fill="x")
        tk.Label(head, text=APP_TITLE, bg="#2b579a", fg="white",
                 font=("Microsoft YaHei UI", 14, "bold")).pack(pady=12)

        # ---- 中间：投放区
        body = tk.Frame(root, padx=18, pady=14)
        body.pack(fill="both", expand=True)

        self.drop = tk.Label(body, text=DROP_TEXT, relief="groove", bd=2,
                             bg="#f7f9fc", fg="#5a6673", cursor="hand2",
                             font=("Microsoft YaHei UI", 11), justify="center")
        self.drop.pack(fill="both", expand=True)
        self.drop.bind("<Button-1>", lambda e: self.pick())

        # ---- 选择按钮行（文件 / 文件夹两种入口）
        pickbar = tk.Frame(body)
        pickbar.pack(fill="x", pady=(10, 0))
        self.btn_pick = tk.Button(pickbar, text="选择文件…", width=12,
                                  command=self.pick)
        self.btn_pick.pack(side="left")
        self.btn_pickdir = tk.Button(pickbar, text="选择文件夹…", width=12,
                                     command=self.pick_dir)
        self.btn_pickdir.pack(side="left", padx=(8, 0))
        self.btn_clear = tk.Button(pickbar, text="清空", width=8,
                                   command=self.clear)
        self.btn_clear.pack(side="right")

        # ---- 选中文件列表
        self.lbl_files = tk.Label(body, text="未选择文件", anchor="w", justify="left",
                                  fg="#5a6673", font=("Microsoft YaHei UI", 9),
                                  wraplength=560)
        self.lbl_files.pack(fill="x", pady=(8, 4))

        # ---- 进度条
        self.bar = ttk.Progressbar(body, mode="determinate", maximum=100)
        self.bar.pack(fill="x", pady=(2, 6))

        self.lbl_state = tk.Label(body, text=OUT_HINT, anchor="w", justify="left",
                                  fg="#8a939c", font=("Microsoft YaHei UI", 9),
                                  wraplength=560)
        self.lbl_state.pack(fill="x")

        # ---- 底部按钮
        foot = tk.Frame(root, padx=18, pady=12)
        foot.pack(fill="x")
        self.btn_open = tk.Button(foot, text="打开输出目录", command=self.open_dir,
                                  state="disabled", width=14)
        self.btn_open.pack(side="left")
        self.btn_go = tk.Button(foot, text="开始压缩", command=self.start,
                                bg="#2b579a", fg="white", width=14,
                                font=("Microsoft YaHei UI", 10, "bold"))
        self.btn_go.pack(side="right")

        self.refresh_files()
        self.root.after(120, self.pump)
        if self._autorun and self.files:
            # 拖到图标上启动的，直接开跑，省一次点击
            self.root.after(300, self.start)
        # 挂上系统拖放。必须等窗口真正创建出来（after_idle）再挂
        self.root.after_idle(self._install_dnd)

    # ------------------------------------------------------------ 拖放
    def _install_dnd(self):
        """给窗口接上 WM_DROPFILES，让程序运行中也能拖文件/文件夹进来。

        tkinter 没有拖放 API，这里用 ctypes 子类化窗口过程：
        收到 WM_DROPFILES 就取出路径写进 self._dropped，再让 pump 处理，
        保证所有界面操作都在主线程做。失败只降级，不弹错。
        """
        if not _DND_OK:
            return
        try:
            self.root.update_idletasks()
            # Tk 的 toplevel 结构：wm_frame 是外层（带边框）窗口，真正接收鼠标的是
            # 它下面的子窗口。逐层往下把能拿到的都注册一遍，命中哪层都能收到。
            hwnds = []
            try:
                base = int(self.root.wm_frame(), 16)
            except Exception:
                base = 0
            if not base:
                self._dnd_ready = False
                return
            hwnds.append(base)
            child = _user32.GetWindow(base, 5)   # GW_CHILD
            depth = 0
            while child and depth < 5:
                hwnds.append(child)
                child = _user32.GetWindow(child, 5)
                depth += 1

            app = self

            def _proc(hwnd, msg, wparam, lparam):
                if msg == WM_DROPFILES:
                    try:
                        paths = _dropped_paths(wparam)
                        if paths:
                            app._dropped.extend(paths)
                    except Exception:
                        pass
                    finally:
                        try:
                            _shell32.DragFinish(wparam)
                        except Exception:
                            pass
                    return 0
                old = app._dnd_hooks_old.get(hwnd)
                if old:
                    return _user32.CallWindowProcW(old, hwnd, msg, wparam, lparam)
                return _user32.DefWindowProcW(hwnd, msg, wparam, lparam)

            cb = _WNDPROC(_proc)
            self._dnd_hooks.append(cb)          # 防 GC 回收导致崩溃
            self._dnd_hooks_old = {}
            for h in hwnds:
                try:
                    old = _set_long(h, GWL_WNDPROC, ctypes.cast(cb, ctypes.c_void_p))
                    if old:
                        self._dnd_hooks_old[h] = old
                    _shell32.DragAcceptFiles(h, True)
                except Exception:
                    continue
            self._dnd_ready = bool(self._dnd_hooks_old)
        except Exception:
            self._dnd_ready = False

    def _take_dropped(self):
        """把拖放进来的路径取出来处理（主线程）。"""
        if not self._dropped:
            return
        paths = list(self._dropped)
        self._dropped = []
        if self.busy:
            # 正在压的时候拖进来，不打断当前批次，明确告知
            messagebox.showinfo(APP_TITLE, "正在压缩中，请等这一批跑完再拖入新文件。")
            return
        self._set_paths(paths)

    # ------------------------------------------------------------ 交互
    def _set_paths(self, paths):
        """统一入口：把文件/文件夹混着的选择展开，然后刷新界面。"""
        skips = []
        files = collect_pdfs(paths, skips)
        self.skipped = skips
        if files:
            self.files = files
        elif paths:
            # 选了东西但一个 PDF 都没找到 —— 明确告诉用户，别装作没事。
            # 区分两种原因：真的没有 PDF，还是里面全是已压过的产物。
            self.files = []
            if skips and all("已是压缩产物" in w for _, w in skips):
                messagebox.showinfo(
                    APP_TITLE,
                    "选中的位置里只有已压缩过的文件（*_cleaned.pdf），没有需要处理的 PDF。")
            else:
                messagebox.showinfo(
                    APP_TITLE,
                    "选中的位置里没有找到 PDF 文件。\n\n"
                    "如果那里确实有 PDF，可能是文件名后缀不是 .pdf。")
        self.refresh_files()

    def pick(self):
        if self.busy:
            return
        paths = filedialog.askopenfilenames(
            title="选择 PDF（可多选）",
            filetypes=[("PDF 文件", "*.pdf"), ("所有文件", "*.*")])
        if paths:
            self._set_paths(paths)

    def pick_dir(self):
        if self.busy:
            return
        d = filedialog.askdirectory(title="选择文件夹（含子目录里的所有 PDF）")
        if d:
            self._set_paths([d])

    def clear(self):
        if self.busy:
            return
        self.files = []
        self.skipped = []
        self.last_out = None
        self.btn_open.config(state="disabled")
        self.bar.config(value=0)
        self.lbl_state.config(text=OUT_HINT)
        self.refresh_files()

    def refresh_files(self):
        n = len(self.files)
        skip_note = ""
        if self.skipped:
            skip_note = "（另有 %d 个已跳过）" % len(self.skipped)

        if n == 0:
            self.lbl_files.config(text="未选择文件")
            self.drop.config(text=DROP_TEXT)
            return

        # 只对真存在且是文件的算体积，防止中途被删导致 getize 抛错
        total = 0
        for p in self.files:
            try:
                if os.path.isfile(p):
                    total += os.path.getsize(p)
            except OSError:
                pass

        if n == 1:
            p = self.files[0]
            self.lbl_files.config(
                text="已选择：%s  (%s)%s"
                     % (os.path.basename(p), human(total), skip_note))
        else:
            # 多个文件时给出目录数，让用户确认递归范围对不对
            dirs = set(os.path.dirname(p) for p in self.files)
            self.lbl_files.config(
                text="已选择 %d 个 PDF，来自 %d 个目录，合计 %s%s"
                     % (n, len(dirs), human(total), skip_note))
        self.drop.config(text="已选 %d 个 PDF\n\n点击可重新选择" % n)

    def open_dir(self):
        if not self.last_out:
            return
        d = os.path.dirname(os.path.abspath(self.last_out))
        if not os.path.isdir(d):
            d = os.path.expanduser("~")
        try:
            if sys.platform.startswith("win"):
                os.startfile(d)          # noqa: S606
            elif sys.platform == "darwin":
                _spawn_quiet(["open", d])
            else:
                _spawn_quiet(["xdg-open", d])
        except Exception as e:
            messagebox.showwarning(APP_TITLE, "打不开目录：%s" % e)

    # ------------------------------------------------------------ 执行
    def _lock(self, on):
        """压缩期间锁住所有能改选择的入口，防止后台线程读的列表被中途换掉。"""
        state = "disabled" if on else "normal"
        for b in (self.btn_pick, self.btn_pickdir, self.btn_clear, self.btn_go):
            try:
                b.config(state=state)
            except Exception:
                pass

    def start(self):
        if self.busy:
            return
        if not self.files:
            messagebox.showinfo(APP_TITLE, "请先选择 PDF 文件或文件夹。")
            return
        self.busy = True
        self._lock(True)
        self.btn_go.config(text="压缩中…")
        self.btn_open.config(state="disabled")
        self.bar.config(value=0)
        self.lbl_state.config(text="准备中…共 %d 个文件" % len(self.files))
        t = threading.Thread(target=self.worker, args=(list(self.files),), daemon=True)
        t.start()

    def worker(self, files):
        """后台线程：逐个压缩，进度和结果通过队列回传主线程。"""
        total = len(files)
        results = []
        for i, src in enumerate(files, 1):
            self.q.put(("progress", i - 1, total, "第 %d/%d 个：%s"
                        % (i, total, os.path.basename(src))))
            try:
                dst, info = compress_one(src)
                old = info.get("original") or os.path.getsize(src)
                new = info.get("bytes") or os.path.getsize(dst)
                saved = 0.0 if not old else (1.0 - float(new) / old) * 100.0
                results.append((True, src, dst, old, new, saved, info))
            except SystemExit as e:
                # 引擎里用 SystemExit 表达「前置条件不满足」，如找不到 qpdf、口令错误
                results.append((False, src, None, 0, 0, 0,
                                {"error": str(e)}))
            except Exception as e:
                results.append((False, src, None, 0, 0, 0,
                                {"error": "%s: %s" % (type(e).__name__, e),
                                 "trace": traceback.format_exc()}))
            self.q.put(("progress", i, total, ""))
        self.q.put(("done", results))

    def pump(self):
        """主线程定时取队列，刷新界面。"""
        try:
            while True:
                msg = self.q.get_nowait()
                if msg[0] == "progress":
                    _, done, total, text = msg
                    self.bar.config(value=0 if not total else done * 100.0 / total)
                    if text:
                        self.lbl_state.config(text=text)
                elif msg[0] == "done":
                    self.finish(msg[1])
        except queue.Empty:
            pass
        # 拖放进来的路径在这里统一处理，保证走主线程
        try:
            self._take_dropped()
        except Exception:
            pass
        self.root.after(120, self.pump)

    def finish(self, results):
        self.busy = False
        self._lock(False)
        self.btn_go.config(text="开始压缩")
        self.bar.config(value=100)

        ok = [r for r in results if r[0]]
        bad = [r for r in results if not r[0]]
        if ok:
            self.last_out = ok[-1][2]
            self.btn_open.config(state="normal")

        if len(results) == 1:
            r = results[0]
            if r[0]:
                _, src, dst, old, new, saved, info = r
                self.lbl_state.config(text="完成：%s → %s，节省 %.1f%%"
                                      % (human(old), human(new), saved))
                messagebox.showinfo(
                    APP_TITLE,
                    "压缩完成\n\n原文件：%s\n压缩后：%s\n节省：%.1f%%\n输出：%s"
                    % (human(old), human(new), saved, dst))
            else:
                self.lbl_state.config(text="失败")
                messagebox.showerror(APP_TITLE, "压缩失败：%s\n\n%s"
                                     % (os.path.basename(r[1]),
                                        r[6].get("error", "未知错误")))
            return

        # 批量：汇总成一段。文件多时只列前若干条明细，免得弹窗长到看不完
        tot_old = tot_new = 0
        for r in ok:
            tot_old += r[3]
            tot_new += r[4]

        summary = "成功 %d 个，失败 %d 个" % (len(ok), len(bad))
        if ok and tot_old:
            summary += "，合计 %s → %s（节省 %.1f%%）" % (
                human(tot_old), human(tot_new),
                (1.0 - float(tot_new) / tot_old) * 100.0)
        self.lbl_state.config(text=summary)

        LIMIT = 25
        lines = []
        for r in ok[:LIMIT]:
            lines.append("  %s  %s → %s  (-%.1f%%)"
                         % (os.path.basename(r[1]), human(r[3]), human(r[4]), r[5]))
        if len(ok) > LIMIT:
            lines.append("  … 还有 %d 个成功项未列出" % (len(ok) - LIMIT))
        for r in bad[:LIMIT]:
            lines.append("  %s  失败：%s"
                         % (os.path.basename(r[1]),
                            r[6].get("error", "未知错误").splitlines()[0]
                            if r[6].get("error") else "未知错误"))
        if len(bad) > LIMIT:
            lines.append("  … 还有 %d 个失败项未列出" % (len(bad) - LIMIT))

        text = summary + "\n\n" + "\n".join(lines)
        if bad:
            messagebox.showwarning(APP_TITLE, text)
        else:
            messagebox.showinfo(APP_TITLE, text)


def _install_excepthook(root):
    """把未捕获异常变成正常对话框。

    console=False 打包后没有终端可以看堆栈，PyInstaller 的默认行为是弹一个
    原始 traceback 窗口，用户看不懂也复制不了。这里接管掉，只显示一句人话，
    同时把完整堆栈写进同目录的 pdfcompress_error.log，方便排查。
    """
    def hook(exc_type, exc_value, exc_tb):
        if issubclass(exc_type, KeyboardInterrupt):
            return
        detail = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        try:
            if getattr(sys, "frozen", False):
                logdir = os.path.dirname(os.path.abspath(sys.executable))
            else:
                logdir = os.path.dirname(os.path.abspath(__file__))
            with open(os.path.join(logdir, "pdfcompress_error.log"), "a",
                      encoding="utf-8") as f:
                f.write(detail)
                f.write("\n" + "-" * 60 + "\n")
        except Exception:
            pass
        try:
            messagebox.showerror(
                APP_TITLE,
                "程序遇到一个未处理的问题：\n\n%s: %s\n\n"
                "完整记录已写入程序同目录的 pdfcompress_error.log。"
                % (exc_type.__name__, exc_value))
        except Exception:
            pass

    sys.excepthook = hook

    # Tk 回调里抛的异常不走 sys.excepthook，单独接一层
    def tk_hook(exc_type, exc_value, exc_tb):
        hook(exc_type, exc_value, exc_tb)

    try:
        root.report_callback_exception = tk_hook
    except Exception:
        pass


def main():
    # argv 可以是文件，也可以是文件夹；统一交给 collect_pdfs 展开
    skips = []
    initial = collect_pdfs(sys.argv[1:], skips) if len(sys.argv) > 1 else []

    root = tk.Tk()
    _install_excepthook(root)
    # 不再「一个文件一个窗口」：拖文件夹进来可能有上百个 PDF，
    # 开上百个窗口会直接把系统拖死。统一单窗口内排队，进度条走一遍。
    app = App(root, initial, autorun=bool(initial))
    if skips:
        app.skipped = skips
        app.refresh_files()
    root.mainloop()


if __name__ == "__main__":
    main()
