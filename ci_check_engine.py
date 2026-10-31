#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""引擎回归：不依赖 qpdf，全部用仓库内 tests/ 的样本。

CI 与本地都能跑：python ci_check_engine.py
输出全 ASCII，避免 Windows 控制台 cp1252 编码错误。
"""

import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
TESTS = os.path.join(HERE, "tests")
ENV = dict(os.environ, PYTHONIOENCODING="utf-8")


def run(src, out, pwd=None):
    cmd = [sys.executable, os.path.join(HERE, "pdfstrip.py"), src, "-o", out]
    if pwd is not None:
        cmd += ["-p", pwd]
    r = subprocess.run(cmd, capture_output=True, text=True, errors="replace",
                       cwd=HERE, env=ENV)
    return r


def pages(path):
    from pypdf import PdfReader
    rd = PdfReader(path)
    return len(rd.pages), rd.is_encrypted


def dct_images(path):
    """页面资源里所有 JPEG 图像流的字节，按对象号索引。"""
    from pypdf import PdfReader
    rd = PdfReader(path)
    out = {}
    for p in rd.pages:
        res = p.get("/Resources")
        if not res:
            continue
        xo = res.get_object().get("/XObject")
        if not xo:
            continue
        for _name, ref in xo.get_object().items():
            if ref.idnum in out:
                continue
            o = ref.get_object()
            if str(o.get("/Subtype", "")) != "/Image":
                continue
            if "DCTDecode" not in str(o.get("/Filter") or ""):
                continue
            raw = getattr(o, "_data", None)
            if raw is None:
                raw = getattr(o, "_data_encoded", b"")
            out[ref.idnum] = raw
    return out


def main():
    fails = []
    tmp = tempfile.mkdtemp(prefix="pdfclean_ci_")

    # 1. 确认 qpdf 已经彻底退出项目
    assert not os.path.isdir(os.path.join(HERE, "tools")), "tools/ should be gone"
    sys.path.insert(0, HERE)
    import pdfstrip
    assert not hasattr(pdfstrip, "_find_qpdf"), "qpdf code path should be removed"
    print("no qpdf: ok")

    # 2. 未加密文件
    out = os.path.join(tmp, "plain.pdf")
    r = run(os.path.join(TESTS, "sample.pdf"), out)
    assert r.returncode == 0 and os.path.exists(out), r.stderr[-400:]
    n, enc = pages(out)
    assert n == 3 and not enc, (n, enc)
    print("plain: ok (%d pages)" % n)

    # 3. 四种加密算法，user 密码 user123
    cases = [("enc_rc4_40.pdf", "user123"), ("enc_rc4_128.pdf", "user123"),
             ("enc_aes128.pdf", "user123"), ("enc_aes256.pdf", "user123")]
    for name, pwd in cases:
        out = os.path.join(tmp, name)
        r = run(os.path.join(TESTS, name), out, pwd)
        assert r.returncode == 0 and os.path.exists(out), (name, r.stderr[-300:])
        n, enc = pages(out)
        assert n == 3 and not enc, (name, n, enc)
        print("%-18s ok (decrypted, %d pages)" % (name, n))

    # 4. owner 密码同样能解
    out = os.path.join(tmp, "owner.pdf")
    r = run(os.path.join(TESTS, "enc_aes256.pdf"), out, "owner123")
    assert r.returncode == 0 and not pages(out)[1], r.stderr[-300:]
    print("owner password: ok")

    # 5. 权限加密（user 密码为空）
    out = os.path.join(tmp, "perm.pdf")
    r = run(os.path.join(TESTS, "enc_perm_aes256.pdf"), out)
    assert r.returncode == 0 and os.path.exists(out), r.stderr[-300:]
    n, enc = pages(out)
    assert n == 3 and not enc, (n, enc)
    print("perm-only encrypt: ok")

    # 6. 错口令必须失败且不产出文件
    out = os.path.join(tmp, "bad.pdf")
    r = run(os.path.join(TESTS, "enc_aes256.pdf"), out, "wrong")
    assert r.returncode != 0, "wrong password must exit non-zero"
    assert not os.path.exists(out), "wrong password must not produce output"
    print("wrong password: ok (exit %d, no output)" % r.returncode)

    # 7. JPEG 元数据剥离：EXIF 段必须消失，码流不能被重编码
    src = os.path.join(TESTS, "photos.pdf")
    out = os.path.join(tmp, "photos.pdf")
    r = run(src, out)
    assert r.returncode == 0 and os.path.exists(out), r.stderr[-300:]
    info = json.loads(r.stdout[r.stdout.index("{"):])
    assert info.get("img_slimmed", 0) >= 2, info
    before, after = dct_images(src), dct_images(out)
    assert len(after) == len(before), (len(before), len(after))
    for raw in after.values():
        assert raw[:2] == b"\xff\xd8", "image stream is not a bare JPEG"
        assert b"Exif\x00" not in raw[:8192], "EXIF segment survived"
    assert sum(map(len, after.values())) < sum(map(len, before.values()))
    print("jpeg meta: ok (%d images, -%d bytes)" % (len(after), info["img_saved"]))

    print("ALL ENGINE CHECKS PASSED")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError as e:
        print("FAILED:", e)
        sys.exit(1)
