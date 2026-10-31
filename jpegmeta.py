"""剥离 JPEG 里的元数据段，熵编码数据原样保留（不重解码、不重编码）。

PDF 的图像流（/Filter /DCTDecode）就是一段完整的 JPEG 字节，
EXIF、GPS、XMP、IPTC、机器型号、拍摄时间全部藏在它的 APP1/APP13/COM 段里。
这些段对渲染没有任何作用，PDF 阅读器只看段头参数和 SOS 之后的码流。

EOI 之后的字节同样要剪：不少手机厂商会在图像结束后追加私有数据流，
vivo 的 streamdataDEGS 可达 490 KB，里头是一帧欠曝的 HDR 原料图，
而且自带一份完整 EXIF 和 GPS——只删 APP 段等于把隐私从后门又留了一份。
"""


def _eoi(data, start):
    """从 SOS 标记处向后定位真正的 EOI（FFD9）偏移，找不到返回 -1。

    熵编码段内 FF 后面只可能是 00（字节填充）或 D0-D7（RST），
    所以扫到的第一个 FFD9 必是图像结束，不会误判成码流里的巧合字节。
    """
    n = len(data)
    k = start + 2
    while k + 1 < n:
        if data[k] != 0xFF:
            k += 1
            continue
        m = data[k + 1]
        if m == 0x00 or 0xD0 <= m <= 0xD7:
            k += 2
            continue
        if m == 0xD9:
            return k
        k += 2                               # 多扫描 JPEG 的后续 SOS，接着找
    return -1


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
        if m == 0x00:
            return None                      # FF 00 是码流内的字节填充，出现在段外即结构错
        if m == 0xDA:
            # SOS：码流照搬，但只抄到 EOI 为止。
            # EOI 之后常跟厂商私有尾巴，vivo 的 streamdataDEGS 能有 490KB，
            # 里面是一帧欠曝的 HDR 原料图，还自带一份完整 EXIF 和 GPS。
            # PDF 的 DCTDecode 读到 EOI 就停，这些字节既不参与渲染又占体积。
            eoi = _eoi(data, i)
            out += data[i:eoi + 2] if eoi > 0 else data[i:]
            return bytes(out) if len(out) < len(data) else None
        if m == 0xD9:                        # EOI：图像到此为止，其后字节不属这张图
            out += data[i:i + 2]
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
