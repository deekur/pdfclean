# -*- coding: utf-8 -*-
"""CI 自检：GUI 版（console=False）运行环境下的健壮性。

打完包的 exe 里 sys.stdout / sys.stderr 都是 None。任何裸的 print /
sys.stderr.write 都会抛 AttributeError，把「口令错误」这种正常失败
变成未捕获异常 —— 用户看到的是一个莫名其妙的错误框。

这里把 stdout/stderr 置 None 后跑两条路径：
  1) 空密码打开有密码的 PDF —— 必须抛 SystemExit（中文提示），不是 AttributeError
  2) 普通 PDF —— 必须正常压完

输出全 ASCII，避免 Windows 控制台 cp1252 编码报错。
"""

import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
TESTS = os.path.join(HERE, "tests")
sys.path.insert(0, HERE)

import pdfstrip  # noqa: E402


def main():
    enc = os.path.join(TESTS, "enc_aes256.pdf")
    plain = os.path.join(TESTS, "sample.pdf")
    for p in (enc, plain):
        if not os.path.exists(p):
            raise SystemExit("FAIL: missing test sample %s" % p)

    tmp = tempfile.mkdtemp(prefix="ci_gui_env_")
    real_out, real_err = sys.stdout, sys.stderr
    try:
        sys.stdout = sys.stderr = None
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

            normal_ok = False
            err_note = ""
            try:
                out = os.path.join(tmp, "out.pdf")
                st = pdfstrip.Strip(plain, "", clean_content=False)
                st.run(out)
                normal_ok = os.path.getsize(out) > 0
            except Exception as e:
                err_note = "%s: %s" % (type(e).__name__, e)
        finally:
            sys.stdout, sys.stderr = real_out, real_err

        print("None streams -> password path:", status)
        print("None streams -> normal path:", "OK" if normal_ok else "FAIL " + err_note)
        if status != "SystemExit":
            raise SystemExit("FAIL: expected SystemExit, got %s" % status)
        if not normal_ok:
            raise SystemExit("FAIL: normal compress broke with None streams")
        print("gui env robustness: OK")
    finally:
        sys.stdout, sys.stderr = real_out, real_err
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
