"""剥离 JPEG 里的元数据段，熵编码数据原样保留（不重解码、不重编码）。

PDF 的图像流（/Filter /DCTDecode）就是一段完整的 JPEG 字节，
EXIF、GPS、XMP、IPTC、机器型号、拍摄时间全部藏在它的 APP1/APP13/COM 段里。
这些段对渲染没有任何作用，PDF 阅读器只看段头参数和 SOS 之后的码流。
"""


def strip(data):
    """返回去掉元数据段后的 JPEG；没有可删段、或结构不可信时返回 None。"""
    if len(data) < 4 or data[:2] != b"\xff\xd8":
        return None
    out = bytearray(data[:2])
    i, n = 2, len(data)
    while i + 3 < n:
        if data[i] != 0xFF:
            return None                      # 段头对不上，不猜
        m = data[i + 1]
        if m == 0xFF:                        # 段间允许任意多个填充 FF
            i += 1
            continue
        if m in (0xD9, 0xDA):                # EOI / SOS：其后是码流，整段照搬
            out += data[i:]
            return bytes(out) if len(out) < len(data) else None
        if m == 0x01 or 0xD0 <= m <= 0xD7:   # 无长度字段的独立标记
            out += data[i:i + 2]
            i += 2
            continue
        ln = int.from_bytes(data[i + 2:i + 4], "big")
        end = i + 2 + ln
        if ln < 2 or end > n:                # 长度越界：文件本身有问题，放弃
            return None
        seg = data[i:end]
        if _keep(m, seg[4:]):
            out += seg
        i = end
    return None


def _keep(m, body):
    """非 APP 段全留；APP 段只留影响解码与色彩的那三个。

    删 APP2（ICC）会让 Adobe RGB / CMYK 图明显偏色，
    删 APP14（Adobe）会让 CMYK 图反相，这两个不是"元数据"而是渲染参数。
    """
    if m < 0xE0:
        return True
    if m == 0xE0:
        return body.startswith(b"JFIF\0")            # JFXX 缩略图段丢掉
    if m == 0xE2:
        return body.startswith(b"ICC_PROFILE\0")
    if m == 0xEE:
        return body.startswith(b"Adobe")
    return False
