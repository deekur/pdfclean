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
    PDF压缩.exe                  # 双击，点区域选文件
    PDF压缩.exe 试卷.pdf 答案.pdf  # 把文件拖到 exe 图标上，直接开跑
"""

import os
import queue
import sys
import threading
import traceback

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

# 打包后 exe 所在目录要进 sys.path，才能 import 同目录的 pdfstrip
_here = os.path.dirname(os.path.abspath(
    sys.executable if getattr(sys, "frozen", False) else __file__))
if _here not in sys.path:
    sys.path.insert(0, _here)

from pdfstrip import Strip  # noqa: E402


APP_TITLE = "PDF 无损压缩"
DROP_TEXT = "把 PDF 拖到这里\n\n或点击选择文件"
OUT_HINT = "输出到源文件同目录，文件名后加 _cleaned，不覆盖原文件"


def human(n):
    """字节数转人话。"""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return ("%.0f %s" % (n, unit)) if unit == "B" else ("%.2f %s" % (n, unit))
        n /= 1024.0
    return "%.2f GB" % n


def out_name(src):
    """算输出路径：<原名>_cleaned.pdf，已在同目录且重名时退到 _2。"""
    d, base = os.path.split(os.path.abspath(src))
    stem = os.path.splitext(base)[0]
    if not stem.endswith("_cleaned"):
        stem += "_cleaned"
    dst = os.path.join(d, stem + ".pdf")
    if os.path.abspath(dst) == os.path.abspath(src):
        dst = os.path.join(d, stem + "_2.pdf")
    return dst


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
    def __init__(self, root, initial=(), title=None):
        self.root = root
        self.files = list(initial)
        self.busy = False
        self.q = queue.Queue()
        self.last_out = None

        root.title(title or APP_TITLE)
        root.geometry("560x430")
        root.minsize(480, 400)
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

        # ---- 选中文件列表
        self.lbl_files = tk.Label(body, text="未选择文件", anchor="w",
                                  fg="#5a6673", font=("Microsoft YaHei UI", 9))
        self.lbl_files.pack(fill="x", pady=(10, 4))

        # ---- 进度条
        self.bar = ttk.Progressbar(body, mode="determinate", maximum=100)
        self.bar.pack(fill="x", pady=(2, 6))

        self.lbl_state = tk.Label(body, text=OUT_HINT, anchor="w",
                                  fg="#8a939c", font=("Microsoft YaHei UI", 9))
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

    # ------------------------------------------------------------ 交互
    def pick(self):
        if self.busy:
            return
        paths = filedialog.askopenfilenames(
            title="选择 PDF（可多选）",
            filetypes=[("PDF 文件", "*.pdf"), ("所有文件", "*.*")])
        if paths:
            self.files = list(paths)
            self.refresh_files()

    def refresh_files(self):
        n = len(self.files)
        if n == 0:
            self.lbl_files.config(text="未选择文件")
            self.drop.config(text=DROP_TEXT)
        elif n == 1:
            p = self.files[0]
            self.lbl_files.config(
                text="已选择：%s  (%s)" % (os.path.basename(p),
                                         human(os.path.getsize(p))))
            self.drop.config(text="已选择 1 个文件\n\n点击可重新选择")
        else:
            total = sum(os.path.getsize(p) for p in self.files)
            self.lbl_files.config(
                text="已选择 %d 个文件，合计 %s" % (n, human(total)))
            self.drop.config(text="已选择 %d 个文件\n\n点击可重新选择" % n)

    def open_dir(self):
        if not self.last_out:
            return
        d = os.path.dirname(os.path.abspath(self.last_out))
        try:
            if sys.platform.startswith("win"):
                os.startfile(d)          # noqa: S606
            elif sys.platform == "darwin":
                os.system('open "%s"' % d)
            else:
                os.system('xdg-open "%s" 2>/dev/null &' % d)
        except Exception as e:
            messagebox.showwarning(APP_TITLE, "打不开目录：%s" % e)

    # ------------------------------------------------------------ 执行
    def start(self):
        if self.busy:
            return
        if not self.files:
            messagebox.showinfo(APP_TITLE, "请先选择 PDF 文件。")
            return
        self.busy = True
        self.btn_go.config(state="disabled", text="压缩中…")
        self.btn_open.config(state="disabled")
        self.bar.config(value=0)
        self.lbl_state.config(text="准备中…")
        t = threading.Thread(target=self.worker, args=(list(self.files),), daemon=True)
        t.start()

    def worker(self, files):
        """后台线程：逐个压缩，进度和结果通过队列回传主线程。"""
        total = len(files)
        results = []
        for i, src in enumerate(files, 1):
            self.q.put(("progress", i - 1, total, "正在处理 %d/%d：%s"
                        % (i, total, os.path.basename(src))))
            try:
                dst, info = compress_one(src)
                old = info.get("original") or os.path.getsize(src)
                new = info.get("bytes") or os.path.getsize(dst)
                saved = 0.0 if not old else (1.0 - float(new) / old) * 100.0
                results.append((True, src, dst, old, new, saved, info))
            except SystemExit as e:
                # 引擎里用 SystemExit 表达「前置条件不满足」，如找不到 qpdf
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
        self.root.after(120, self.pump)

    def finish(self, results):
        self.busy = False
        self.btn_go.config(state="normal", text="开始压缩")
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

        # 批量：汇总成一段
        lines = []
        tot_old = tot_new = 0
        for r in ok:
            _, src, dst, old, new, saved, info = r
            tot_old += old
            tot_new += new
            lines.append("  %s  %s → %s  (-%.1f%%)"
                         % (os.path.basename(src), human(old), human(new), saved))
        for r in bad:
            lines.append("  %s  失败：%s"
                         % (os.path.basename(r[1]), r[6].get("error", "未知错误")))
        summary = "成功 %d 个，失败 %d 个" % (len(ok), len(bad))
        if ok and tot_old:
            summary += "，合计 %s → %s（节省 %.1f%%）" % (
                human(tot_old), human(tot_new),
                (1.0 - float(tot_new) / tot_old) * 100.0)
        self.lbl_state.config(text=summary)
        text = summary + "\n\n" + "\n".join(lines)
        if bad:
            messagebox.showwarning(APP_TITLE, text)
        else:
            messagebox.showinfo(APP_TITLE, text)


def main():
    initial = []
    for a in sys.argv[1:]:
        if os.path.isfile(a) and a.lower().endswith(".pdf"):
            initial.append(os.path.abspath(a))
    root = tk.Tk()
    if not initial:
        App(root)
    else:
        # 一次拖/传多个文件：每个文件一个独立窗口并行压，互不排队。
        # 第一个用主窗口，其余用 Toplevel（共用同一个 mainloop 与 Tk 解释器）
        n = len(initial)
        for i, p in enumerate(initial):
            win = root if i == 0 else tk.Toplevel(root)
            ttl = "%s  %d/%d" % (APP_TITLE, i + 1, n) if n > 1 else APP_TITLE
            app = App(win, [p], title=ttl)
            # 拖到图标上启动的，直接开跑，省一次点击
            win.after(300, app.start)
    root.mainloop()


if __name__ == "__main__":
    main()
