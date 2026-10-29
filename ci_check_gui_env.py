# -*- coding: utf-8 -*-
"""CI 自检：GUI 版（console=False）运行环境下的健壮性。

打完包的 exe 里 sys.stdout / sys.stderr 都是 None。任何裸的
sys.stderr.write(...) 都会抛 AttributeError，把「口令错误」这种
正常失败变成未捕获异常 —— 用户看到的是一个莫名其妙的错误框。

这里把 stderr 置 None 后跑一遍加密文件解密失败路径，
断言抛的是预期的 SystemExit 而不是 AttributeError。

输出全 ASCII，避免 Windows 控制台 cp1252 编码报错。
"""

import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pdfstrip  # noqa: E402

MINI = (b"%PDF-1.4\n"
        b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
        b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 595 842]>>endobj\n"
        b"trailer<</Root 1 0 R/Size 4>>\nstartxref\n0\n%%EOF\n")


def main():
    qpdf = pdfstrip._find_qpdf()
    if not qpdf:
        raise SystemExit("FAIL: qpdf not found")

    tmp = tempfile.mkdtemp(prefix="ci_gui_env_")
    try:
        raw = os.path.join(tmp, "raw.pdf")
        with open(raw, "wb") as f:
            f.write(MINI)
        # 先用 qpdf 修成合法结构
        fixed = os.path.join(tmp, "fixed.pdf")
        subprocess.run([qpdf, "--warning-exit-0", "--object-streams=disable",
                        raw, fixed], check=True, capture_output=True)
        # 再造一个需要口令的加密件
        enc = os.path.join(tmp, "enc.pdf")
        subprocess.run([qpdf, "--encrypt", "123456", "123456", "256", "--",
                        "--linearize", fixed, enc],
                       check=True, capture_output=True)

        real_out, real_err = sys.stdout, sys.stderr
        # ---- 模拟 console=False 打包后的状态
        sys.stdout = None
        sys.stderr = None
        try:
            status = "unknown"
            try:
                pdfstrip.Strip(enc, "", clean_content=False)
                status = "unexpected-success"
            except SystemExit:
                status = "SystemExit"
            except AttributeError as e:
                status = "AttributeError: %s" % e
            except Exception as e:
                status = "%s: %s" % (type(e).__name__, e)
        finally:
            sys.stdout, sys.stderr = real_out, real_err

        print("stdlib streams set to None ->", status)
        if status != "SystemExit":
            raise SystemExit("FAIL: expected SystemExit, got %s" % status)

        # 正路也要能跑：普通文件在 stderr=None 下必须压缩成功
        sys.stdout = None
        sys.stderr = None
        try:
            out = os.path.join(tmp, "out.pdf")
            st = pdfstrip.Strip(fixed, "", clean_content=False)
            info = st.run(out)
            ok = os.path.getsize(out) > 0
        except Exception as e:
            ok = False
            print("normal path failed:", type(e).__name__, e)
        finally:
            sys.stdout, sys.stderr = real_out, real_err

        print("normal path under None streams:", "OK" if ok else "FAIL")
        if not ok:
            raise SystemExit("FAIL: normal compress broke with None streams")

        print("gui env robustness: OK")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
