# -*- coding: utf-8 -*-
"""CI 用的递归收集自检。

造一棵固定结构的测试树，断言 gui.collect_pdfs 的行为：
    应收集   a.pdf / sub/b.pdf / sub/deep/c.pdf          -> 3 个
    应跳过   sub/a_cleaned.pdf（已是产物）、skip.txt（非 PDF）、
            .hidden/（隐藏目录）、__MACOSX/（系统垃圾目录）

输出全用 ASCII，避免 Windows 控制台 cp1252 编码报错。
"""

import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import gui  # noqa: E402

PDF = b"%PDF-1.4\n"


def touch(path):
    d = os.path.dirname(path)
    if d and not os.path.isdir(d):
        os.makedirs(d)
    with open(path, "wb") as f:
        f.write(PDF)


def main():
    root = tempfile.mkdtemp(prefix="ci_recursive_")
    try:
        touch(os.path.join(root, "a.pdf"))
        touch(os.path.join(root, "skip.txt"))
        touch(os.path.join(root, "sub", "b.pdf"))
        touch(os.path.join(root, "sub", "a_cleaned.pdf"))      # 产物，应跳过
        touch(os.path.join(root, "sub", "deep", "c.pdf"))
        touch(os.path.join(root, ".hidden", "h.pdf"))          # 隐藏目录
        touch(os.path.join(root, "__MACOSX", "m.pdf"))         # 系统垃圾目录

        skips = []
        got = gui.collect_pdfs([root], skips)
        names = sorted(os.path.basename(p) for p in got)

        print("collected:", names)
        print("skipped  :", sorted(os.path.basename(p) for p, _ in skips))

        expect = ["a.pdf", "b.pdf", "c.pdf"]
        if names != expect:
            raise SystemExit("FAIL: expected %s, got %s" % (expect, names))

        # 跳过原因里必须包含那个产物，且不能把隐藏目录的文件收进来
        assert not any("h.pdf" in p for p in got), "hidden dir must be skipped"
        assert not any("m.pdf" in p for p in got), "__MACOSX must be skipped"
        assert any("a_cleaned.pdf" in p for p, _ in skips), "cleaned file must be reported"

        # 幂等：第二次扫，结果不变（不会因为产物而变多或变少）
        got2 = gui.collect_pdfs([root], [])
        assert sorted(os.path.basename(p) for p in got2) == expect, "second scan must match"

        print("recursive collect: OK")
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()
