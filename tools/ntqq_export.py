"""NTQQ 群聊记录导出工具（QQMonitor importer 兼容 CSV）。

原理：
1. NTQQ 数据库文件前 1024 字节是自定义明文头（"SQLite header 3" + QQ_NT DB 段），
   剥掉后是标准 SQLCipher 4 库（4096 页 / kdf_iter 4000 / HMAC-SHA1 / PBKDF2-HMAC-SHA512）。
2. 用 sqlcipher3（官方 SQLCipher 库绑定）以 passphrase 打开。
3. 群消息在 group_msg_table：40030=群号, 40033=发送者QQ, 40050=时间戳, 40011=msg_type, 40800=消息体 protobuf。
4. 40800 为 MsgBody{ repeated MsgContent content=40800 }，文本在 MsgContent.text=45101。

用法：
    python tools/ntqq_export.py --db <nt_msg.db> --key <passphrase> --out <dir>
      [--group <群号>]      # 只导出指定群；不指定则导出全部群
      [--list-groups]       # 只列出群号与消息数
"""
from __future__ import annotations

import argparse
import base64
import csv
import html
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import sqlcipher3.dbapi2 as sc

DB_HEADER_SIZE = 1024

# SQLCipher 参数（NTQQ 固定）
SC_PRAGMAS = [
    "PRAGMA cipher_page_size = 4096;",
    "PRAGMA kdf_iter = 4000;",
    "PRAGMA cipher_hmac_algorithm = HMAC_SHA1;",
    "PRAGMA cipher_kdf_algorithm = PBKDF2_HMAC_SHA512;",
]

# content_type (45002) → 占位文本（实测语义：7=引用、8=系统提示，不是视频/通话）
PLACEHOLDER = {
    0: "[消息]", 1: "", 2: "[图片]", 3: "[文件]", 4: "[语音]", 5: "[视频]",
    6: "[表情]", 7: "", 8: "", 9: "[语音通话]", 10: "[群公告]", 11: "[表情]",
    12: "[转发]", 14: "", 16: "[卡片]", 17: "[消息]", 20: "[系统消息]", 21: "[红包]",
}
_MEDIA_TYPE = {2: "图片", 3: "文件", 4: "语音", 5: "视频"}

# 媒体消息里的本地文件绝对路径（::NTOSFull::C:\...），用于带出文件名
_NTOSFULL_RE = re.compile(rb"::NTOSFull::([A-Za-z]:\\[\x20-\x7e]{5,})")

# QQ NT 新版本消息体里不再带 NTOSFull 原图路径，只给云文件 ID：
#   $DA4B22650D44A28870C9A3DD2240077D.jpg
# 本地缓存目录（Pic/<年月>/Thumb/<id>_720.jpg 或 _0.jpg）按这个 ID 命名。
_CLOUD_PIC_RE = re.compile(rb"\$([A-Fa-f0-9]{32})\.(jpg|jpeg|png|gif|webp)", re.IGNORECASE)




def read_varint(data: bytes, pos: int) -> tuple[int, int]:
    result, shift = 0, 0
    while pos < len(data):
        b = data[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, pos
        shift += 7
    return result, pos


def parse_fields(data: bytes) -> dict[int, list]:
    """通用 protobuf wire 解析：{field: [(wire_type, value), ...]}"""
    fields: dict[int, list] = defaultdict(list)
    pos = 0
    while pos < len(data):
        key, pos = read_varint(data, pos)
        field, wt = key >> 3, key & 7
        if wt == 0:
            v, pos = read_varint(data, pos)
            fields[field].append((0, v))
        elif wt == 1:
            if pos + 8 > len(data):
                break
            fields[field].append((1, data[pos:pos + 8]))
            pos += 8
        elif wt == 2:
            ln, pos = read_varint(data, pos)
            if pos + ln > len(data):
                break
            fields[field].append((2, data[pos:pos + ln]))
            pos += ln
        elif wt == 5:
            if pos + 4 > len(data):
                break
            fields[field].append((5, data[pos:pos + 4]))
            pos += 4
        else:
            break
    return fields


def _field_bytes(seg_fields: dict, key: int) -> str:
    for wt, v in seg_fields.get(key, []):
        if wt == 2 and isinstance(v, bytes) and v:
            return v.decode("utf-8", "replace")
    return ""


def _system_names(seg_fields: dict) -> dict[str, str]:
    """48210 是若干段「uid + 显示名」，用来把 gtip 里的 uin 换成昵称。"""
    names: dict[str, str] = {}
    for wt, raw in seg_fields.get(48210, []):
        if wt != 2 or not (isinstance(raw, bytes) and raw):
            continue
        s = raw.decode("utf-8", "ignore")
        m = re.search(r"u_[A-Za-z0-9_-]{6,}", s)
        if not m:
            continue
        uid = m.group(0)
        rest = "".join(ch for ch in s[m.end():] if ch.isprintable()).strip()
        # 名字前会带一个长度/分隔字符（如 ">"），只去掉它，保留 emoji 昵称
        rest = rest.lstrip(">").strip()
        if rest:
            names[uid] = rest
    return names


def _gtip_text(seg_fields: dict) -> str:
    """解析群系统提示的 gtip XML（48214），拼成「谁 邀请 谁 加入了群聊」这类文本。"""
    xml = _field_bytes(seg_fields, 48214)
    if not xml:
        return ""
    names = _system_names(seg_fields)
    parts: list[str] = []
    for m in re.finditer(r"<(qq|nor)\b([^>]*?)/?>", xml):
        kind, attrs = m.group(1), m.group(2)
        if kind == "qq":
            uid = re.search(r'uin="([^"]+)"', attrs)
            jp = re.search(r'jp="([^"]*)"', attrs)
            val = names.get(uid.group(1), "") if uid else ""
            parts.append(val or (f"QQ{jp.group(1)}" if jp and jp.group(1) else ""))
        else:
            txt = re.search(r'txt="([^"]*)"', attrs)
            parts.append(txt.group(1) if txt else "")
    text = " ".join(p for p in parts if p)
    return re.sub(r"\s+", " ", text).strip()


def _card_title(seg_fields: dict) -> str:
    xml = _field_bytes(seg_fields, 48602)
    if not xml:
        return ""
    m = re.search(r'title="([^"]*)"', xml)
    if m and m.group(1).strip():
        return m.group(1).strip()
    # 合并转发等卡片的标题是 <title>…</title> 的**元素文本**，不是属性（旧版只看属性，
    # 导致 852 条「聊天记录」卡片只剩 [卡片]）。
    m = re.search(r"<title[^>]*>([^<]{1,120})</title>", xml)
    return m.group(1).strip() if m else ""


def _card_brief(seg_fields: dict) -> str:
    """卡片 XML 的 brief 属性（如 [聊天记录]），标题取不到时的兜底。"""
    xml = _field_bytes(seg_fields, 48602)
    if not xml:
        return ""
    m = re.search(r'<msg[^>]*\bbrief="([^"]{1,80})"', xml)
    return m.group(1).strip() if m else ""


# --- content_type=10：通用 ARK 卡片容器 -------------------------------------
# 10 不是「群公告」专用类型，而是 ARK 卡片容器（小程序/公众号分享/位置/公告…），
# 正文不在 45101，而在元素字段 47901 的一段 JSON：
#   {"app": "com.tencent.mannounce",
#    "prompt": "...",
#    "meta": {"mannounce": {"title": <base64>, "text": <base64>}}}
# 真群公告 app=com.tencent.mannounce，title/text 是 base64(UTF-8)；
# 其它 app 用顶层 prompt 作可读文本（自带 [分享]/[位置] 之类前缀）。
ARK_MAX_CHARS = 2000
ARK_TRUNC_MARK = "…（已截断）"
_B64_ONLY_RE = re.compile(r"^[A-Za-z0-9+/]+={0,2}$")
_B64_INLINE_RE = re.compile(r"[A-Za-z0-9+/]{200,}={0,2}")


def _decode_b64_text(v: object) -> str:
    """mannounce 的 title/text 是 base64(UTF-8)；不是合法 base64 就原样当明文。

    等长明文（如 "12345678"）解码后不是合法 UTF-8，会被下面的校验挡掉。
    """
    if not isinstance(v, str):
        return ""
    s = v.strip()
    if len(s) < 8 or len(s) % 4 or not _B64_ONLY_RE.match(s):
        return s
    try:
        raw = base64.b64decode(s, validate=True)
        txt = raw.decode("utf-8").strip()
    except Exception:
        return s
    if not txt or any(ord(ch) < 32 and ch not in "\n\t" for ch in txt):
        return s
    return txt


def _is_noise_line(t: str) -> bool:
    """整行是 XML/HTML/JSON 结构残留时丢弃。"""
    if t.startswith("<") and ">" in t:
        return True
    return t.startswith("{") or t.startswith("[{")


def _clean_ark_text(s: str) -> str:
    """去 HTML 实体/结构残留、首尾空白、连续空行，并截断到 ARK_MAX_CHARS。"""
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = html.unescape(s)
    lines: list[str] = []
    for line in s.split("\n"):
        t = line.strip()
        if not t:
            lines.append("")
            continue
        if _is_noise_line(t):
            continue
        t = _B64_INLINE_RE.sub("", t).strip()
        if t:
            lines.append(t)
    out: list[str] = []
    for t in lines:
        if not t and (not out or not out[-1]):
            continue
        out.append(t)
    txt = "\n".join(out).strip()
    if len(txt) > ARK_MAX_CHARS:
        txt = txt[:ARK_MAX_CHARS].rstrip() + ARK_TRUNC_MARK
    return txt


def _ark_meta(seg_fields: dict) -> dict:
    """47901 是一段 ARK JSON，解析失败统一返回 {}。"""
    raw = _field_bytes(seg_fields, 47901)
    if not raw:
        return {}
    try:
        obj = json.loads(raw)
    except Exception:
        return {}
    return obj if isinstance(obj, dict) else {}


def _ark_text(seg_fields: dict) -> str:
    """ARK 卡片的可读正文：mannounce（base64 标题+正文）→ 顶层 prompt → meta.title。"""
    obj = _ark_meta(seg_fields)
    if not obj:
        return ""
    app = obj.get("app") if isinstance(obj.get("app"), str) else ""
    meta = obj.get("meta") if isinstance(obj.get("meta"), dict) else {}
    if app == "com.tencent.mannounce":
        man = meta.get("mannounce") if isinstance(meta.get("mannounce"), dict) else {}
        title = _decode_b64_text(man.get("title"))
        body = _decode_b64_text(man.get("text"))
        lines: list[str] = []
        if title and title not in ("群公告",):
            lines.append(title)
        if body:
            lines.append(body)
        if lines:
            return _clean_ark_text("[群公告] " + "\n".join(lines))
    prompt = obj.get("prompt")
    if isinstance(prompt, str) and prompt.strip():
        return _clean_ark_text(prompt)
    for v in meta.values():
        if isinstance(v, dict):
            t = _decode_b64_text(v.get("title"))
            if t.strip():
                return _clean_ark_text(f"[卡片:{t.strip()}]")
    return ""


_URL_KEYS = ("jumpUrl", "url", "qqdocurl")
CARD_URL_MAX = 500
_URL_OK_RE = re.compile(r"^https?://[^\s\"'<>]+$", re.IGNORECASE)

# QQ 分享卡片会给链接挂一长串分享/埋点参数（b23.tv 最典型：?share_medium=android
# &share_source=qq&bbid=…&ts=…，一条能带 100+ 字符）。这些参数与内容无关，剥掉能让
# content 干净、也让同一视频的不同分享不再看起来像不同链接。
# 只删下面这些 key（大小写不敏感，另加 `share_` 前缀），**其它参数一律原样保留**：
# 有些站点靠查询参数取内容，误删就是把链接弄坏。
_TRACK_KEYS = frozenset({
    "share_medium", "share_source", "share_plat", "share_tag", "share_times",
    "share_from", "bbid", "ts", "unique_k", "spm_id_from", "vd_source",
    "from_source", "from_spmid", "buvid", "is_story_h5", "timestamp",
    "plat_id", "mid", "up_id",
})


def _strip_track(u: str) -> str:
    """剥掉已知分享/埋点查询参数；按原文按 `&` 切分再拼回，不动其它参数的编码。"""
    try:
        parts = urlsplit(u)
    except ValueError:
        return u
    if not parts.query:
        return u
    kept: list[str] = []
    dropped = False
    for pair in parts.query.split("&"):
        if not pair:
            continue
        key = pair.split("=", 1)[0].lower()
        if key in _TRACK_KEYS or key.startswith("share_"):
            dropped = True
            continue
        kept.append(pair)
    if not dropped:
        return u
    return urlunsplit((parts.scheme, parts.netloc, parts.path,
                       "&".join(kept), parts.fragment))


def _norm_url(v: object) -> str:
    """规整卡片里的链接字段：剥追踪参数 → 只放行 http/https、无空白/引号、长度受限。"""
    if not isinstance(v, str):
        return ""
    u = _strip_track(v.strip())
    if not u or len(u) > CARD_URL_MAX:
        return ""
    if not _URL_OK_RE.match(u):
        return ""
    return u


def card_url(seg_fields: dict) -> str:
    """取 ARK 卡片里的跳转链接（公众号文章/小程序落地页/文档）。

    实测 47901 的 JSON 形如
    ``{"app":"com.tencent.structmsg","prompt":"[分享]…","meta":{"news":{…,
    "desc":"https://mp.weixin.qq.com/s/…","jumpUrl":"https://mp.weixin.qq.com/s/…"}}}``
    —— 正文（时间/地点）在链接指向的页面里，content 不带链接下游就无从跟进。

    取值顺序：每个 ``meta.<k>`` 内 ``jumpUrl`` → ``url`` → ``qqdocurl`` → ``desc``
    （部分卡片 desc 本身就是纯 URL），最后回退到 JSON 顶层的同名字段。
    只接受 http/https、不含空白与引号、≤ ``CARD_URL_MAX`` 字符，否则返回 ""。
    """
    obj = _ark_meta(seg_fields)
    if not obj:
        return ""
    meta = obj.get("meta") if isinstance(obj.get("meta"), dict) else {}
    for v in meta.values():
        if not isinstance(v, dict):
            continue
        for key in _URL_KEYS + ("desc",):
            url = _norm_url(v.get(key))
            if url:
                return url
    for key in _URL_KEYS:
        url = _norm_url(obj.get(key))
        if url:
            return url
    return ""


def _with_url(text: str, seg_fields: dict) -> str:
    """在卡片正文末尾追加 ``" <URL>"``；正文里已有 http(s) 则不重复追加。"""
    if not text:
        return text
    if "http://" in text or "https://" in text:
        return text
    url = card_url(seg_fields)
    if not url:
        return text
    return f"{text} {url}"


def seg_text(content_type: int, seg_fields: dict, media_name: str = "") -> str:
    """从单个 MsgContent 段提取可显示文本。

    media_name: 从 NTOSFull 路径提取的本地文件名（媒体消息用），
    用于让导出内容携带媒体文件名，便于知识库与原始文件对应。

    实测 content_type：1 文本 / 2 图片 / 3 文件 / 4 语音 / 5 视频 / 6 表情 /
    7 引用（只带被引用消息元数据）/ 8 系统提示（gtip XML 或 485xx 人名）/
    10 ARK 卡片（群公告/小程序/分享/位置，正文在元素 47901 的 JSON）/
    11 表情包 / 14 富文本 / 16 卡片（合并转发等，XML 在 48602）/ 17 未知。
    """
    text_vals = [v for wt, v in seg_fields.get(45101, []) if wt == 2 and v]
    if text_vals:
        return "".join(v.decode("utf-8", "replace") for v in text_vals)
    if content_type in (2, 3, 4, 5):
        fn = _field_bytes(seg_fields, 45402) or media_name
        label = _MEDIA_TYPE[content_type]
        return f"[{label}:{fn}]" if fn else f"[{label}]"
    if content_type == 6:
        return "[表情]"
    if content_type == 7:
        # 引用消息段本身不含正文（正文在后续文本段），以前误标成 [视频]。
        # 用 [回复] 保住只有引用段的消息，同时避免假的「视频」占位。
        return "[回复] "
    if content_type == 8:
        text = _gtip_text(seg_fields)
        if text:
            return text
        a = _field_bytes(seg_fields, 48504) or _field_bytes(seg_fields, 48505)
        b = _field_bytes(seg_fields, 48507) or _field_bytes(seg_fields, 48508)
        if a and b:
            return f"{a} → {b}"
        return "[系统提示]"
    if content_type == 10:
        # 通用 ARK 卡片（群公告/小程序/分享/位置…）：正文在元素 47901 的 JSON 里，
        # 解析不出可读文本时才退回占位「[群公告]」。卡片指向的链接（正文往往在
        # 那页上）追加在末尾，否则下游（日程抽取/知识库）看不到时间地点。
        return _with_url(_ark_text(seg_fields) or PLACEHOLDER[10], seg_fields)
    if content_type == 11:
        return _field_bytes(seg_fields, 80900) or "[表情]"
    if content_type == 14:
        return _field_bytes(seg_fields, 48701)
    if content_type == 16:
        title = _card_title(seg_fields)
        if title:
            text = f"[卡片:{title}]"
        else:
            text = _card_brief(seg_fields) or PLACEHOLDER[16]
        # 合并转发卡片自身不带链接时 card_url 返回 ""，拼接结果逐字节不变。
        return _with_url(text, seg_fields)
    return PLACEHOLDER.get(content_type, "[消息]")


def _extract_cloud_id(seg: bytes) -> str:
    """从消息体中提取云文件 ID（新格式）。"""
    m = _CLOUD_PIC_RE.search(seg)
    if not m:
        return ""
    # 统一小写，与本地 Thumb 缓存文件名对齐
    return f"{m.group(1).decode('ascii').lower()}.{m.group(2).decode('ascii').lower()}"


def extract_content(blob: bytes | None) -> str:
    """解析 40800 protobuf，提取整条消息文本。"""
    if not blob:
        return ""
    try:
        body = parse_fields(blob)
    except Exception:
        return ""
    parts: list[str] = []
    for wt, seg in body.get(40800, []):
        if wt != 2 or not seg:
            continue
        try:
            seg_fields = parse_fields(seg)
        except Exception:
            continue
        ct_vals = [v for wt, v in seg_fields.get(45002, []) if wt == 0]
        content_type = ct_vals[0] if ct_vals else 0
        # 提取媒体文件名：优先本地原图路径（老格式/部分新格式），
        # 没有则用云文件 ID（新格式，本地缓存按此 ID 命名）
        media_name = ""
        for m in _NTOSFULL_RE.finditer(seg):
            raw = m.group(1).decode("utf-8", "replace")
            if "\\Thumb\\" in raw or "/Thumb/" in raw:
                continue
            nm = os.path.basename(raw.replace("\\", "/"))
            if nm:
                media_name = nm
                break
        if not media_name and content_type in (2, 4, 5):
            media_name = _extract_cloud_id(seg)
        t = seg_text(content_type, seg_fields, media_name)
        if t:
            parts.append(t)
    # 文本段之间不能插换行：QQ 的 @提及 是「@A」「 」「@B」多个独立段，
    # 用 "\n" 拼会把一行 @ 拆成多行（2026-10-30 实测）。
    return "".join(parts)


def extract_file_elements(blob: bytes | None) -> list[dict]:
    """解析 40800 里的「文件」元素（content_type=3）。

    返回 [{msg_id,name,size,md5,file_id,busid,local}]：
    - name      文件名（45402）
    - size      字节数（45405）
    - md5       文件 md5（45406），用于跨群/重复发送去重
    - file_id   群文件 ID（字段 45503），想自己拼文件直链时会用到
    - busid     业务 ID（45501，实测群文件 102）
    - local     本机已有副本路径（45403 的 ::NTOSFull:: 段，未下载时为空）
    """
    if not blob:
        return []
    try:
        body = parse_fields(blob)
    except Exception:
        return []

    def _s(fields: dict, key: int) -> str:
        for wt, v in fields.get(key, []):
            if wt == 2 and isinstance(v, bytes) and v:
                return v.decode("utf-8", "replace")
        return ""

    def _i(fields: dict, key: int) -> int:
        for wt, v in fields.get(key, []):
            if wt == 0:
                return int(v)
        return 0

    out: list[dict] = []
    for wt, seg in body.get(40800, []):
        if wt != 2 or not seg:
            continue
        try:
            sf = parse_fields(seg)
        except Exception:
            continue
        if _i(sf, 45002) != 3:
            continue
        name = _s(sf, 45402)
        if not name:
            continue
        local = _s(sf, 45403)
        if local.startswith("::NTOSFull::"):
            local = local[len("::NTOSFull::"):]
        out.append({
            "msg_id": _i(sf, 45001),
            "name": name,
            "size": _i(sf, 45405),
            "md5": _s(sf, 45406),
            "file_id": _s(sf, 45503),
            "busid": _i(sf, 45501),
            "local": local,
        })
    return out


def open_db(db_path: Path, key: str) -> sc.Connection:
    # 剥 1024 字节头 → 临时明文文件（避免改动原文件）
    clear = db_path.with_suffix(db_path.suffix + ".clear.tmp")
    with open(db_path, "rb") as f:
        f.seek(DB_HEADER_SIZE)
        with open(clear, "wb") as out:
            while chunk := f.read(64 << 20):
                out.write(chunk)
    conn = sc.connect(str(clear), isolation_level=None)
    # 注意：sqlcipher3 中 key 之后的参数 PRAGMA 会触发重新派生，
    # 顺序必须为 page_size → key → 其他参数（官方 1.decrypt.py 同款顺序）
    conn.execute("PRAGMA cipher_page_size = 4096;")
    safe = key.replace("'", "''")
    conn.execute(f"PRAGMA key = '{safe}';")
    for pragma in SC_PRAGMAS[1:]:
        conn.execute(pragma)
    conn.execute("SELECT count(*) FROM sqlite_master;").fetchone()
    return conn, clear


# 群号字段：新创建群/小号群的 group_id [40030] 可能为 0，
# 此时用 [40027]（消息序列号，实测与群号一致）再退 [40021]。
# 否则刚建的群会因 40030=0 永远同步不到（坑 #21）。
_GID_SQL = "COALESCE(NULLIF([40030],0), NULLIF([40027],0), [40021])"

# 好友消息表字段（c2c_msg_table，2026-09-02 探查确认）：
#   [40030] = 对方QQ（会话对象，好友QQ）
#   [40033] = 发送者QQ（你或对方）
#   [40050] = 时间戳
#   [40093] = 发送者昵称（好友消息里通常为空，昵称需从其他地方拿）
#   [40800] = 消息体 protobuf（与群表同款，extract_content 可直接复用）
_BUDDY_QQ_SQL = "[40030]"  # 对方QQ字段


def list_groups(conn: sc.Connection) -> list[tuple[int, int]]:
    rows = conn.execute(
        f"SELECT {_GID_SQL} AS gid, count(*) FROM group_msg_table "
        "WHERE [40800] IS NOT NULL GROUP BY gid ORDER BY count(*) DESC;"
    ).fetchall()
    return [(int(r[0]), r[1]) for r in rows if r[0]]


def list_buddies(conn: sc.Connection) -> list[tuple[int, int]]:
    """列出所有好友会话（对方QQ, 消息数）。

    c2c_msg_table: [40030]=对方QQ, [40800]=消息体。
    """
    rows = conn.execute(
        f"SELECT {_BUDDY_QQ_SQL} AS qq, count(*) FROM c2c_msg_table "
        "WHERE [40800] IS NOT NULL AND [40030] > 0 "
        "GROUP BY qq ORDER BY count(*) DESC;"
    ).fetchall()
    return [(int(r[0]), r[1]) for r in rows if r[0]]


def export_group(conn: sc.Connection, group_id: int, out_csv: Path) -> int:
    rows = conn.execute(
        f"SELECT [40033],[40050],[40090],[40800] FROM group_msg_table "
        f"WHERE {_GID_SQL} = ? ORDER BY [40050];", (group_id,)
    ).fetchall()
    n = 0
    with open(out_csv, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["时间", "发送者QQ", "昵称", "内容"])
        for sender, ts, nick, blob in rows:
            content = extract_content(blob)
            if not content:
                continue
            ts_str = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S") if ts else ""
            w.writerow([ts_str, int(sender) if sender else 0,
                        (nick or "").strip(), content])
            n += 1
    return n


def export_buddy(conn: sc.Connection, buddy_qq: int, out_csv: Path) -> int:
    """导出指定好友的聊天记录（对方QQ → CSV）。

    与 export_group 同构，仅数据源换成 c2c_msg_table。
    """
    rows = conn.execute(
        f"SELECT [40033],[40050],[40093],[40800] FROM c2c_msg_table "
        f"WHERE {_BUDDY_QQ_SQL} = ? ORDER BY [40050];", (buddy_qq,)
    ).fetchall()
    n = 0
    with open(out_csv, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["时间", "发送者QQ", "昵称", "内容"])
        for sender, ts, nick, blob in rows:
            content = extract_content(blob)
            if not content:
                continue
            ts_str = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S") if ts else ""
            w.writerow([ts_str, int(sender) if sender else 0,
                        (nick or "").strip(), content])
            n += 1
    return n


def main() -> int:
    ap = argparse.ArgumentParser(description="NTQQ 聊天记录导出（群/好友）")
    ap.add_argument("--db", required=True, help="nt_msg.db 原始文件路径")
    ap.add_argument("--key", required=True, help="16 位解密密钥（qq-win-db-key 提取）")
    ap.add_argument("--out", default="exports", help="输出目录")
    ap.add_argument("--group", type=int, default=0, help="只导出指定群号（0=全部）")
    ap.add_argument("--buddy", type=int, default=0, help="只导出指定好友QQ（0=全部）")
    ap.add_argument("--list-groups", action="store_true", help="只列出群号")
    ap.add_argument("--list-buddies", action="store_true", help="只列出好友QQ")
    args = ap.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"错误: 文件不存在 {db_path}")
        return 1

    conn, clear = open_db(db_path, args.key)
    try:
        # 好友导出
        if args.list_buddies or args.buddy:
            buddies = list_buddies(conn)
            if not buddies:
                print("c2c_msg_table 中没有任何好友消息")
                return 1
            print(f"发现 {len(buddies)} 个好友会话:")
            for qq, cnt in buddies:
                print(f"  {qq}: {cnt} 条")
            if args.list_buddies:
                return 0

            out_dir = Path(args.out)
            out_dir.mkdir(parents=True, exist_ok=True)
            targets = [(args.buddy, 0)] if args.buddy else buddies
            total = 0
            for qq, cnt in targets:
                found = next((c for q, c in buddies if q == qq), 0)
                if not found:
                    print(f"警告: 好友 {qq} 不在数据库中，跳过")
                    continue
                out_csv = out_dir / f"buddy_{qq}.csv"
                n = export_buddy(conn, qq, out_csv)
                print(f"  好友 {qq}: 导出 {n}/{found} 条文本消息 → {out_csv}")
                total += n
            print(f"完成，共导出 {total} 条消息")
            return 0

        # 群导出（原有逻辑）
        groups = list_groups(conn)
        if not groups:
            print("group_msg_table 中没有任何群消息")
            return 1
        print(f"发现 {len(groups)} 个群:")
        for gid, cnt in groups:
            print(f"  {gid}: {cnt} 条")

        if args.list_groups:
            return 0

        out_dir = Path(args.out)
        out_dir.mkdir(parents=True, exist_ok=True)
        targets = [(args.group, 0)] if args.group else groups
        total = 0
        for gid, cnt in targets:
            found = next((c for g, c in groups if g == gid), 0)
            if not found:
                print(f"警告: 群 {gid} 不在数据库中，跳过")
                continue
            out_csv = out_dir / f"group_{gid}.csv"
            n = export_group(conn, gid, out_csv)
            print(f"  群 {gid}: 导出 {n}/{found} 条文本消息 → {out_csv}")
            total += n
        print(f"完成，共导出 {total} 条消息")
        return 0
    finally:
        conn.close()
        try:
            clear.unlink()
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main())
