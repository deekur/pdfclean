#!/usr/bin/env python3
"""纯 Python 还原加密 PDF（按 ISO 32000-2 §7.6 与 qpdf QPDF_encryption.cc 实现）

支持 V1/V2 (RC4 40/128)、V4 (AESV2 / RC4 / Identity)、V5 (AESV3, R5/R6)
不依赖 qpdf / pikepdf，只需要 pycryptodome。

用法: python3 pdfdecrypt.py in.pdf out.pdf [密码]
"""
import sys, re, hashlib, struct, os, zlib, io, contextlib
from Crypto.Cipher import AES

PAD = bytes([
    0x28, 0xBF, 0x4E, 0x5E, 0x4E, 0x75, 0x8A, 0x41, 0x64, 0x00, 0x4E, 0x56,
    0xFF, 0xFA, 0x01, 0x08, 0x2E, 0x2E, 0x00, 0xB6, 0xD0, 0x68, 0x3E, 0x80,
    0x2F, 0x0C, 0xA9, 0xFE, 0x64, 0x53, 0x69, 0x7A])

# ---------- 基础密码学 ----------

def rc4(key, data):
    S = list(range(256)); j = 0
    for i in range(256):
        j = (j + S[i] + key[i % len(key)]) & 0xFF
        S[i], S[j] = S[j], S[i]
    out = bytearray(); i = j = 0
    for c in data:
        i = (i + 1) & 0xFF
        j = (j + S[i]) & 0xFF
        S[i], S[j] = S[j], S[i]
        out.append(c ^ S[(S[i] + S[j]) & 0xFF])
    return bytes(out)

def aes_dec(key, data):
    """AES-CBC，前 16 字节为 IV，PKCS#7 填充"""
    if len(data) < 32:
        return b''
    iv, ct = data[:16], data[16:]
    ct = ct[:len(ct) // 16 * 16]
    if not ct:
        return b''
    pt = AES.new(key, AES.MODE_CBC, iv).decrypt(ct)
    n = pt[-1] if pt else 0
    return pt[:-n] if 0 < n <= 16 else pt

def aes_dec_nopad(key, data, iv=b'\x00' * 16):
    return AES.new(key, AES.MODE_CBC, iv).decrypt(data)

def pad(pwd):
    return (pwd + PAD)[:32] if len(pwd) <= 32 else pwd[:32]

def pad_to(s, n):
    """对应 qpdf 的 pad_short_parameter：只补不截"""
    return s + b'\x00' * (n - len(s)) if len(s) < n else s

def hex_bytes(h):
    """PDF hex string：允许奇数位，末位隐含补 0（ISO 32000-2 §7.3.4.3）"""
    h = re.sub(rb'\s', b'', h)
    if len(h) % 2:
        h += b'0'
    return bytes.fromhex(h.decode())

def iterate_rc4(data, okey, iterations, reverse=False):
    """qpdf iterate_rc4：每轮用 key^round 重新 RC4；reverse 用于从 /O 反解"""
    for i in range(iterations):
        xv = (iterations - 1 - i) if reverse else i
        data = rc4(bytes(b ^ xv for b in okey), data)
    return data

def iter_md5(d0, iterations, n):
    d = d0
    for _ in range(iterations):
        d = hashlib.md5(d[:n]).digest()
    return d[:n]

# ---------- 密钥推导（Alg 3.2 / 3.1 / 2.B / 3.2a） ----------

def file_key_legacy(pwd, enc):
    """Alg 3.2: V<5 的文件密钥由用户密码推导"""
    m = hashlib.md5()
    m.update(pad(pwd))
    m.update(enc['O'])
    m.update(struct.pack('<i', enc['P']))
    m.update(enc['ID0'])
    if enc['R'] >= 4 and not enc['EncryptMetadata']:
        m.update(b'\xff\xff\xff\xff')
    n = enc['Length']
    return iter_md5(m.digest(), 50 if enc['R'] >= 3 else 0, n)

def compute_o_key(pwd, enc):
    """Alg 3.3 的 key 部分；调用时把 provided 当 owner 密码"""
    return iter_md5(hashlib.md5(pad(pwd)).digest(),
                    50 if enc['R'] >= 3 else 0, enc['Length'])

def hash_v5(pwd, salt, udata, R):
    """Alg 2.B / hash_V5"""
    K = hashlib.sha256(pwd + salt + udata).digest()
    if R < 6:
        return K
    rnd = 0
    while True:
        rnd += 1
        K1 = pwd + K + udata
        buf = (K1 * 64)
        buf = buf[:len(buf) // 16 * 16]        # qpdf 用 disablePadding，丢弃不完整块
        E = AES.new(K[:16], AES.MODE_CBC, K[16:32]).encrypt(buf)
        mod = sum(E[:16]) % 3
        K = hashlib.new({0: 'sha256', 1: 'sha384', 2: 'sha512'}[mod], E).digest()
        if rnd >= 64 and E and E[-1] <= rnd - 32:
            break
    return K[:32]

def file_key_v5(pwd, enc):
    """Alg 3.2a: 文件密钥存在 UE/OE 里，用密码解开"""
    upwd = pwd[:127]
    U, O = enc['U'], enc['O']
    if hash_v5(upwd, O[32:40], U[:48], enc['R']) == O[:32]:       # owner（Alg 3.12，含 U[:48]）
        return aes_dec_nopad(hash_v5(upwd, O[40:48], U[:48], enc['R']), enc['OE'][:32])
    if hash_v5(upwd, U[32:40], b'', enc['R']) == U[:32]:          # user
        return aes_dec_nopad(hash_v5(upwd, U[40:48], b'', enc['R']), enc['UE'][:32])
    return None

def obj_key(fkey, num, gen, aes, enc):
    """Alg 3.1 / 3.1a: 每个对象独立派生"""
    if enc['V'] >= 5:
        return fkey
    d = fkey + bytes([num & 0xFF, (num >> 8) & 0xFF, (num >> 16) & 0xFF,
                      gen & 0xFF, (gen >> 8) & 0xFF])
    if aes:
        d += b'sAlT'
    return hashlib.md5(d).digest()[:min(len(fkey) + 5, 16)]

def check_user(fkey, enc):
    """Alg 6: 用推导出的文件密钥重算 /U，确认密码确实对。
    不加这层校验，错误密码会得到一堆乱码却"成功"输出。"""
    U = enc['U']
    if enc['R'] < 3:
        return rc4(fkey, PAD)[:32] == U[:32]
    x = hashlib.md5(PAD + enc['ID0']).digest()
    for i in range(20):
        x = rc4(bytes(b ^ i for b in fkey), x)
    return x[:16] == U[:16]

def recover_user_password(provided, enc):
    """Alg 3.7：把 provided 当 owner 密码，从 /O 反解出 user 密码。
    V<5 的文件密钥永远由 user 密码推导，所以 owner 密码必须先还原成 user。"""
    key = pad_to(compute_o_key(provided, enc), enc['Length'])
    up = iterate_rc4(enc['O'][:32], key, 20 if enc['R'] >= 3 else 1, reverse=True)
    return up if check_user(file_key_legacy(up, enc), enc) else None

# ---------- 极简 PDF 解析 ----------

def parse_dict(text, key, last=False):
    """取 /Key N G R 的对象号；last=True 时取最后一个（增量更新取最新版）"""
    ms = list(re.finditer(rb'/' + key + rb'\s*(\d+)\s+(\d+)\s+R', text))
    if not ms:
        return None
    return int(ms[-1].group(1)) if last else int(ms[0].group(1))

def num_val(text, key, default=0):
    m = re.search(rb'/' + key + rb'\s+(\d+)', text)
    return int(m.group(1)) if m else default

def name_val(text, key, default=None):
    m = re.search(rb'/' + key + rb'\s*/([A-Za-z0-9]+)', text)
    return m.group(1) if m else default

def pdf_string(text, key):
    """取字典里的字符串值，兼容 <hex> 与 (literal)，literal 需处理转义括号"""
    m = re.search(rb'/' + key + rb'\s*(\(|<)', text)
    if not m:
        return b''
    if m.group(1) == b'<':
        end = text.find(b'>', m.end())
        if end < 0: return b''
        try:
            return hex_bytes(text[m.end():end])
        except ValueError:
            return b''
    depth, i, out = 1, m.end(), bytearray()
    while i < len(text):
        c = text[i]
        if c == 0x5C:
            out += text[i:i + 2]; i += 2; continue
        if c == 0x28:
            depth += 1
        elif c == 0x29:
            depth -= 1
            if depth == 0:
                break
        out.append(c); i += 1
    return unescape(bytes(out))

def parse_encrypt_dict(text):
    e = {}
    e['V'] = num_val(text, b'V', 0)
    e['R'] = num_val(text, b'R', 0)
    e['P'] = int(re.search(rb'/P\s+(-?\d+)', text).group(1))

    # /Length 规则（initializeEncryption）：V1→40, V4→128, V5→256 强制；
    # 只有 V=2 才读字典值，且必须 8 的倍数、40..128 之间，否则回退 128
    V = e['V']
    if V <= 1:
        e['Length'] = 5
    elif V == 4:
        e['Length'] = 16
    elif V == 5:
        e['Length'] = 32
    else:
        vals = [int(x) for x in re.findall(rb'/Length\s+(\d+)(?!\s+\d+\s+R)', text)]
        L = vals[-1] if vals else 128
        if L % 8 or L < 40 or L > 128:
            L = 128
        e['Length'] = L // 8

    # O/U/OE/UE/Perms 定长，短了要补零（pad_short_parameter）
    kb = 48 if V >= 5 else 32
    # 必须在 pad 之前判存在性：/O 缺失时 pdf_string 返回 b''，pad 成零后
    # 长度检查就看不出来了，会一路用零 /O 算出错误密钥。qpdf 对所有 V 都
    # 无条件 getStringValue()，缺键或类型不对即 throw_damaged_pdf。
    miss = [k.decode() for k in (b'O', b'U')
            if not re.search(rb'/' + k + rb'\s*(\(|<)', text)]
    if miss:
        raise ValueError('V=%d 缺少 %s，文件已损坏'
                         % (V, '、'.join('/' + m for m in miss)))
    e['O'] = pad_to(pdf_string(text, b'O'), kb)
    e['U'] = pad_to(pdf_string(text, b'U'), kb)
    # qpdf 是先 pad 再校验：31 字节会被补零接受，只有超过 32 字节才 throw_damaged_pdf
    if V < 5 and (len(e['O']) != 32 or len(e['U']) != 32):
        raise ValueError('/O 或 /U 长度超过 32 字节（%d/%d），文件已损坏'
                         % (len(e['O']), len(e['U'])))
    e['EncryptMetadata'] = b'/EncryptMetadata false' not in text
    e['StmF'] = name_val(text, b'StmF', b'Identity')
    e['StrF'] = name_val(text, b'StrF', b'Identity')
    e['CF'] = {}
    for m in re.finditer(rb'/CFM\s*/(\w+)', text):
        pre = list(re.finditer(rb'/(\w+)\s*<<', text[:m.start()]))
        if pre:
            e['CF'][pre[-1].group(1).decode()] = m.group(1).decode()
    if V >= 5:
        # qpdf：/OE /UE /Perms 必须是 string（hex 或 literal 均可），缺失或类型不对即判损坏
        miss = [k.decode() for k in (b'OE', b'UE', b'Perms')
                if not re.search(rb'/' + k + rb'\s*(\(|<)', text)]
        if miss:
            raise ValueError('V=5 缺少 %s，文件已损坏' % '、'.join('/' + m for m in miss))
        e['OE'] = pad_to(pdf_string(text, b'OE'), 32)
        e['UE'] = pad_to(pdf_string(text, b'UE'), 32)
        e['Perms'] = pad_to(pdf_string(text, b'Perms'), 16)
    else:
        e['OE'] = e['UE'] = e['Perms'] = b''
    return e

def unescape(s):
    out = bytearray(); i = 0
    while i < len(s):
        c = s[i]
        if c == 0x5C and i + 1 < len(s):
            n = s[i + 1]
            mp = {0x6E: 10, 0x72: 13, 0x74: 9, 0x62: 8, 0x66: 12,
                  0x28: 40, 0x29: 41, 0x5C: 92}
            if n in mp:
                out.append(mp[n]); i += 2; continue
            if 0x30 <= n <= 0x37:
                oct_ = s[i + 1:i + 4]
                k = 0
                while k < 3 and k < len(oct_) and 0x30 <= oct_[k] <= 0x37:
                    k += 1
                out.append(int(oct_[:k], 8) & 0xFF); i += 1 + k; continue
            if n == 0x0A:                   # 反斜杠+LF 续行：两个字节都丢
                i += 2; continue
            if n == 0x0D:                   # 反斜杠+CRLF 续行：反斜杠与整个行尾标记（CR+LF）都要丢
                i += 3 if (i + 2 < len(s) and s[i + 2] == 0x0A) else 2
                continue
            out.append(n); i += 2; continue  # 其他未知转义：只忽略反斜杠，字符保留
        if c == 0x0D:                       # 裸 CR 或 CRLF → LF（转义的 \r 已在上面的分支处理，不受影响）
            out.append(0x0A); i += 1
            if i < len(s) and s[i] == 0x0A:
                i += 1
            continue
        out.append(c); i += 1
    return bytes(out)

STR_RE = re.compile(rb'(\((?:\\.|[^\\()]|\((?:\\.|[^\\()])*\))*\))|(<[0-9A-Fa-f\s]+>)')

def unescape_lit(b):
    return unescape(b[1:-1])

# ---------- 主流程 ----------

def main(inp, outp, pwd=b''):
    data = open(inp, 'rb').read()

    # trailer / Encrypt 引用
    tpos = data.rfind(b'trailer')
    tail = data[tpos:] if tpos >= 0 else data[-4096:]
    enc_num = parse_dict(tail, b'Encrypt', last=True)
    if enc_num is None:
        # xref stream 的情况：/Encrypt 在 xref 字典里，全文找最后一个
        mes = list(re.finditer(rb'/Encrypt\s+(\d+)\s+(\d+)\s+R', data))
        enc_num = int(mes[-1].group(1)) if mes else None
    if enc_num is None:
        print('未发现加密字典，文件可能未加密'); return 1

    # 取 /Encrypt 对象（增量更新时取最后一个同名对象）与 /ID
    matches = list(re.finditer(
        rb'(?<![0-9])' + str(enc_num).encode() + rb'\s+(\d+)\s+obj(.*?)endobj', data, re.S))
    if not matches:
        print('找不到 /Encrypt 对象'); return 1
    try:
        enc = parse_encrypt_dict(matches[-1].group(2))
    except ValueError as ex:
        print('加密字典解析失败：%s' % ex); return 1
    mid = re.search(rb'/ID\s*\[\s*(<[0-9A-Fa-f\s]+>)', data)
    if mid:
        enc['ID0'] = hex_bytes(mid.group(1)[1:-1])
    else:
        # 字面量形式 /ID [(...)]：从中括号后的 ( 起手工扫描，pdf_string 的 \s* 跨不过 [
        mid = re.search(rb'/ID\s*\[\s*\(', data)
        if mid:
            i, depth, start = mid.end(), 1, mid.end()
            while i < len(data):
                c = data[i]
                if c == 0x5C:
                    i += 2; continue
                if c == 0x28:
                    depth += 1
                elif c == 0x29:
                    depth -= 1
                    if depth == 0:
                        break
                i += 1
            enc['ID0'] = unescape(data[start:i])
        else:
            enc['ID0'] = b''
    if enc['V'] >= 5:
        fkey = file_key_v5(pwd, enc)
        if fkey is None:
            print('密码错误'); return 1
    else:
        # 先按 owner 密码恢复 user 密码，再按 user 密码直接算（顺序同 qpdf）
        upwd = recover_user_password(pwd, enc)
        if upwd is None:
            if not check_user(file_key_legacy(pwd, enc), enc):
                print('密码错误（/U 校验不通过）'); return 1
            upwd = pwd
        fkey = file_key_legacy(upwd, enc)

    # 判断某对象的 crypt 方法
    def method_of(name):
        if not name or name == b'Identity':
            return 'none'
        cf = enc['CF'].get(name.decode())
        if cf == 'V2':
            return 'rc4'
        if cf in ('AESV2', 'AESV3'):
            return 'aes'
        # qpdf 对未知 CFM 是"警告后按 AES 处理"，不是不解密
        print('警告：未知 CFM /%s，按 AES 处理' % name.decode(), file=sys.stderr)
        return 'aes'

    m_stm = method_of(enc['StmF']) if enc['V'] >= 4 else 'rc4'
    m_str = method_of(enc['StrF']) if enc['V'] >= 4 else 'rc4'

    # 扫描对象（按 /Length 精确切片，绝不能用 endstream 反推，加密流尾字节可能是 \r\n）
    spans = {}
    for m in re.finditer(rb'(?<![0-9])(\d+)\s+(\d+)\s+obj\b', data):
        num, gen = int(m.group(1)), int(m.group(2))
        end = data.find(b'endobj', m.end())
        if end < 0:
            continue
        spans[(num, gen)] = (m.end(), end)

    def stream_len(body):
        m = re.search(rb'/Length\s+(\d+)(\s+(\d+)\s+R)?', body)
        if not m:
            return None
        if m.group(2):                      # 间接引用：group(1)=num, group(3)=gen
            rn, rg = int(m.group(1)), int(m.group(3))
            if (rn, rg) in spans:
                bs2, be2 = spans[(rn, rg)]
            else:                            # gen 对不上时取同 num 的最大 gen
                cand = sorted(g for (n2, g) in spans if n2 == rn)
                if not cand:
                    return None
                bs2, be2 = spans[(rn, cand[-1])]
            mm = re.search(rb'\d+', data[bs2:be2])
            return int(mm.group(0)) if mm else None
        return int(m.group(1))

    objs = {}
    objpos = {}                              # 每个对象在文件中的物理起点，用于判定增量更新的新旧
    for (num, gen), (bs, be) in spans.items():
        objpos[(num, gen)] = bs
        sm = data.find(b'stream', bs, be)
        if sm < 0:
            objs[(num, gen)] = (data[bs:be], None, num, gen)
            continue
        sstart = sm + 6
        if data[sstart:sstart + 2] == b'\r\n': sstart += 2
        elif data[sstart:sstart + 1] in (b'\n', b'\r'): sstart += 1
        ln = stream_len(data[bs:sm])
        if ln is not None and 0 <= ln <= be - sstart:
            stm = data[sstart:sstart + ln]
        else:                                # 长度不可信时退回 endstream，且不去行尾
            es = data.find(b'endstream', sstart)
            stm = data[sstart:(es if es > 0 else be)]
        objs[(num, gen)] = (data[bs:sm], stm, num, gen)

    root = parse_dict(tail, b'Root', last=True)
    if root is None:
        mrs = list(re.finditer(rb'/Root\s+(\d+)\s+(\d+)\s+R', data))
        root = int(mrs[-1].group(1)) if mrs else None   # 增量更新时取最新的 trailer
    info = parse_dict(tail, b'Info', last=True)
    if info is None:
        mis = list(re.finditer(rb'/Info\s+(\d+)\s+(\d+)\s+R', data))
        info = int(mis[-1].group(1)) if mis else None
    if root is None:
        print('无法定位 /Root，放弃重建'); return 1

    # 阶段 1：解密所有流
    for k, (dct, stm, num, gen) in list(objs.items()):
        if stm is None or m_stm == 'none':
            continue
        key = obj_key(fkey, num, gen, m_stm == 'aes', enc)
        objs[k] = (dct, aes_dec(key, stm) if m_stm == 'aes' else rc4(key, stm), num, gen)

    # 阶段 2：展开对象流。ObjStm 里的对象必须提为顶层，否则重建的 xref 表无法表示
    packed, from_stm = set(), set()
    for k, (dct, stm, num, gen) in list(objs.items()):
        if stm is None or b'/ObjStm' not in dct:
            continue
        n_obj = num_val(dct, b'N', 0)
        first = num_val(dct, b'First', 0)
        body = stm
        if b'/FlateDecode' in dct:
            try:
                body = zlib.decompress(stm)
            except zlib.error:
                body = stm
        hdr = body[:first].split()
        pairs = []
        for i in range(0, min(len(hdr), 2 * n_obj), 2):
            try:
                pairs.append((int(hdr[i]), int(hdr[i + 1])))
            except ValueError:
                break
        pairs.sort(key=lambda x: x[1])
        for idx, (onum, off) in enumerate(pairs):
            s0 = first + off
            e0 = first + pairs[idx + 1][1] if idx + 1 < len(pairs) else len(body)
            key2 = (onum, 0)
            pos = objpos.get(k, 0)           # 对象流所在位置即这批对象的物理位置
            if key2 not in objs or pos > objpos.get(key2, -1):
                objs[key2] = (body[s0:e0], None, onum, 0)
                objpos[key2] = pos
                from_stm.add(key2)
        packed.add(k)

    # 阶段 3：重建（同一对象号若在增量更新中出现多次，只保留文件位置最靠后的那版）
    latest = {}
    for (num, gen) in sorted(objs, key=lambda k: objpos.get(k, 0)):
        latest[num] = gen
    out = bytearray(b'%PDF-1.7\n%\xe2\xe3\xcf\xd3\n')
    offsets = {}
    for (num, gen), (dct, stm, _, _) in sorted(objs.items()):
        if num == enc_num or (num, gen) in packed:
            continue                          # 丢掉 /Encrypt 对象与已展开的对象流
        if latest.get(num) != gen:
            continue                          # 旧版本对象
        if re.search(rb'/Type\s*/XRef\b', dct):
            continue                          # 只跳过真正的 xref 流，避免误杀正文里含 /XRef 字样的字典
        if stm is not None:
            dct = re.sub(rb'/Length\s+\d+(\s+\d+\s+R)?', b'/Length ' + str(len(stm)).encode(), dct)
        if m_str != 'none' and (num, gen) not in from_stm:
            skey = obj_key(fkey, num, gen, m_str == 'aes', enc)
            def rep(mo):
                if mo.group(1):
                    raw = unescape_lit(mo.group(1))
                else:
                    try:
                        raw = hex_bytes(mo.group(2)[1:-1])
                    except ValueError:
                        return mo.group(0)
                dv = aes_dec(skey, raw) if m_str == 'aes' else rc4(skey, raw)
                return b'<' + dv.hex().encode() + b'>'   # 统一转 hex，规避字面量转义
            dct = STR_RE.sub(rep, dct)
        offsets[num] = (len(out), gen)
        out += str(num).encode() + b' ' + str(gen).encode() + b' obj\n' + dct.strip() + b'\n'
        if stm is not None:
            out += b'stream\n' + stm + b'\nendstream\n'
        out += b'endobj\n'

    maxn = max(offsets) + 1
    xref_at = len(out)
    out += b'xref\n0 ' + str(maxn).encode() + b'\n'
    out += b'0000000000 65535 f \n'
    for i in range(1, maxn):
        if i in offsets:
            off, g = offsets[i]
            out += b'%010d %05d n \n' % (off, g)
        else:
            out += b'0000000000 65535 f \n'
    tr = b'trailer\n<< /Size ' + str(maxn).encode() + b' /Root ' + str(root).encode() + b' 0 R'
    if info:
        tr += b' /Info ' + str(info).encode() + b' 0 R'
    tr += b' >>\nstartxref\n' + str(xref_at).encode() + b'\n%%EOF\n'
    out += tr

    open(outp, 'wb').write(out)
    print('%s -> %s | V=%d R=%d 对象=%d %d -> %d 字节' % (
        os.path.basename(inp), os.path.basename(outp), enc['V'], enc['R'],
        len(objs), len(data), len(out)))
    return 0

def decrypt_file(inp, outp, pwd=b''):
    """供其他模块调用的封装：不打印、不退出，返回 (ok, 原因)。

    打包成 GUI 程序后 sys.stdout 是 None，main() 里的 print 会抛
    AttributeError，所以统一用 redirect_stdout 接住，再由返回值传递结论。
    """
    if isinstance(pwd, str):
        pwd = pwd.encode('utf-8')
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            rc = main(inp, outp, pwd)
    except Exception as e:
        return False, '%s: %s' % (type(e).__name__, e)
    if rc == 0:
        return True, ''
    lines = [l for l in (buf.getvalue() or '').strip().splitlines() if l.strip()]
    return False, lines[-1] if lines else '解密失败'


if __name__ == '__main__':
    p = sys.argv[3].encode() if len(sys.argv) > 3 else b''
    sys.exit(main(sys.argv[1], sys.argv[2], p))
