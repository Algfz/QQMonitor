"""SQLite 存储层：群 / 成员 / 消息 / 会议。

多线程安全：每个线程独立连接（threading.local），写操作即时 commit。
"""
from __future__ import annotations

import difflib
import json
import logging
import re
import sqlite3
import threading
import time
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Iterable, Optional

log = logging.getLogger("db")


def _norm_title_for_match(s: str) -> str:
    """日程标题归一化：只留汉字/字母/数字，用于「同日标题近似」判定。"""
    return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", s or "").lower()


class Database:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._init()

    # ---------- 连接 ----------
    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            # 三个写者（Web 进程 / autosync_daemon 进程 / 转写线程）并发写同一个库；
            # WAL 只解决「读不挡写」，写-写仍然互斥，默认 5s 等待（pysqlite 的
            # connect(timeout=5.0) 等价于 busy_timeout=5000）容易被打破：
            # importer 整群导入是「一个 BEGIN IMMEDIATE 事务写完全部消息」
            # （app/importer.py:330-373），大群要几秒，落在这期间的手写就会抛
            # database is locked。这里显式放宽到 15s：等待只影响写者延迟，
            # 不会读到脏数据，也不会拖死读路径（读走 WAL 快照）。
            conn.execute("PRAGMA busy_timeout=15000")
            self._local.conn = conn
        return conn

    def _init(self) -> None:
        c = self._conn()
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS groups (
                group_id      INTEGER PRIMARY KEY,
                name          TEXT,
                member_count  INTEGER DEFAULT 0,
                updated_at    TEXT
            );
            CREATE TABLE IF NOT EXISTS members (
                group_id  INTEGER,
                user_id   INTEGER,
                nickname  TEXT,
                card      TEXT,
                role      TEXT,
                PRIMARY KEY (group_id, user_id)
            );
            CREATE TABLE IF NOT EXISTS messages (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id       INTEGER UNIQUE,
                group_id         INTEGER,
                user_id          INTEGER,
                nickname         TEXT,
                msg_type         TEXT DEFAULT 'text',
                content          TEXT,
                raw_json         TEXT,
                ts               INTEGER,
                important        INTEGER DEFAULT 0,
                important_reason TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_messages_group ON messages(group_id, ts);
            CREATE INDEX IF NOT EXISTS idx_messages_ts    ON messages(ts);
            CREATE INDEX IF NOT EXISTS idx_messages_imp   ON messages(group_id, important);
            CREATE TABLE IF NOT EXISTS meetings (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                title           TEXT,
                held_date       TEXT,
                notes           TEXT,
                recording_path  TEXT,
                created_at      TEXT
            );
            CREATE TABLE IF NOT EXISTS meta (
                key   TEXT PRIMARY KEY,
                value TEXT
            );
            CREATE TABLE IF NOT EXISTS kb_chunks (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                source     TEXT NOT NULL,   -- 相对知识库路径，如 chat/10001_班级群.md
                section    TEXT NOT NULL,   -- chat / documents / transcripts
                day        TEXT,            -- 聊天按天分块的日期，其余为空
                chunk_idx  INTEGER NOT NULL,
                text       TEXT NOT NULL,
                embedding  BLOB,            -- float32 序列化向量
                dim        INTEGER,         -- 向量维度，用于校验模型是否更换
                mtime      REAL,            -- 源文件修改时间，用于增量更新
                UNIQUE(source, chunk_idx)
            );
            CREATE TABLE IF NOT EXISTS kb_meta (
                source     TEXT PRIMARY KEY,  -- 相对知识库路径，如 documents/学费.md
                event_date TEXT,              -- 事件日期 YYYY-MM-DD（内容发生时间，非文件修改时间）
                end_date   TEXT,              -- 可选结束日期，用于跨天活动
                note       TEXT,              -- 备注
                auto       INTEGER DEFAULT 0, -- 1=自动推断，0=用户手动设定
                updated_at TEXT
            );
            CREATE TABLE IF NOT EXISTS media_ocr (
                rel_path  TEXT PRIMARY KEY,  -- 相对知识库路径，如 media/10001/2026-08/a.png
                size      INTEGER,           -- 文件字节数，与 mtime 一起判断是否需要重跑
                mtime     REAL,
                text      TEXT,              -- 还原后的 Markdown（表格/列表/公式行）
                updated_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_kb_chunks_day ON kb_chunks(day);
            CREATE TABLE IF NOT EXISTS conversations (
                id          TEXT PRIMARY KEY,
                title       TEXT DEFAULT "",
                group_id    INTEGER,          -- 会话绑定的群（可选）
                created_at  TEXT NOT NULL,
                updated_at  TEXT NOT NULL,
                msg_count   INTEGER DEFAULT 0,
                messages    TEXT DEFAULT "[]"  -- JSON 数组，含工具调用记录
            );
            CREATE INDEX IF NOT EXISTS idx_conv_updated
                ON conversations(updated_at DESC);
            CREATE TABLE IF NOT EXISTS group_files (
                group_id   INTEGER NOT NULL,
                msg_ts     INTEGER NOT NULL,
                name       TEXT NOT NULL,
                size       INTEGER DEFAULT 0,
                md5        TEXT DEFAULT '',
                file_id    TEXT DEFAULT '',
                busid      INTEGER DEFAULT 0,
                local_path TEXT DEFAULT '',
                status     TEXT DEFAULT 'pending',
                doc_path   TEXT DEFAULT '',
                md_path    TEXT DEFAULT '',
                detail     TEXT DEFAULT '',
                updated_at REAL DEFAULT 0,
                PRIMARY KEY (group_id, msg_ts, name)
            );
            CREATE INDEX IF NOT EXISTS idx_group_files_status
                ON group_files(status);
            CREATE TABLE IF NOT EXISTS recordings (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                filename    TEXT NOT NULL,
                course      TEXT DEFAULT '',
                rec_date    TEXT DEFAULT '',
                md5         TEXT UNIQUE,
                size        INTEGER DEFAULT 0,
                status      TEXT DEFAULT 'pending',
                stored_path TEXT DEFAULT '',
                md_path     TEXT DEFAULT '',
                summary_path TEXT DEFAULT '',
                schedule_path TEXT DEFAULT '',
                detail      TEXT DEFAULT '',
                created_at  REAL DEFAULT 0,
                updated_at  REAL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_recordings_status
                ON recordings(status);
            CREATE TABLE IF NOT EXISTS schedule_items (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                date         TEXT NOT NULL,
                time         TEXT DEFAULT '',
                title        TEXT NOT NULL,
                detail       TEXT DEFAULT '',
                source_type  TEXT DEFAULT '',
                source_id    TEXT DEFAULT '',
                source_ref   TEXT DEFAULT '',
                source_quote TEXT DEFAULT '',
                source_label TEXT DEFAULT '',
                sources      TEXT DEFAULT '',
                confidence   REAL DEFAULT 0,
                dedupe_key   TEXT UNIQUE,
                manual       INTEGER NOT NULL DEFAULT 0,
                created_at   REAL DEFAULT 0,
                updated_at   REAL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_schedule_date ON schedule_items(date);
            -- 日程墓碑：用户手动删掉的条目记在这里，自动抽取下一轮不许复活它
            CREATE TABLE IF NOT EXISTS schedule_dismissed (
                dedupe_key  TEXT PRIMARY KEY,
                date        TEXT,
                title_norm  TEXT,
                created_at  REAL
            );
            CREATE INDEX IF NOT EXISTS idx_schedule_dismissed_date
                ON schedule_dismissed(date, title_norm);
            """
        )
        c.commit()
        # 轻量迁移：为旧库补 recordings 的新列
        cols = {r["name"] for r in c.execute("PRAGMA table_info(recordings)")}
        for name in ("summary_path", "schedule_path"):
            if name not in cols:
                c.execute(f"ALTER TABLE recordings ADD COLUMN {name} TEXT DEFAULT ''")
        c.commit()
        # 轻量迁移：为旧库补 schedule_items.manual（老库平滑升级，可重复执行）
        sch_cols = {r["name"] for r in c.execute("PRAGMA table_info(schedule_items)")}
        if "manual" not in sch_cols:
            c.execute("ALTER TABLE schedule_items"
                      " ADD COLUMN manual INTEGER NOT NULL DEFAULT 0")
            c.commit()
            log.info("迁移：schedule_items 补 manual 列")

    def set_meta(self, key: str, value: str) -> None:
        c = self._conn()
        c.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        c.commit()

    def get_meta(self, key: str, default: str = "") -> str:
        row = self._conn().execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    # ---------- 群 ----------
    def upsert_group(self, group_id: int, name: str, member_count: int = 0,
                     commit: bool = True) -> None:
        c = self._conn()
        c.execute(
            "INSERT INTO groups(group_id, name, member_count, updated_at) VALUES(?,?,?,?) "
            "ON CONFLICT(group_id) DO UPDATE SET name=excluded.name, "
            "member_count=excluded.member_count, updated_at=excluded.updated_at",
            (group_id, name, member_count, datetime.now().isoformat(timespec="seconds")),
        )
        if commit:
            c.commit()

    def get_groups(self) -> list[dict]:
        rows = self._conn().execute(
            """
            SELECT g.*, COUNT(m.id) AS msg_count,
                   MAX(m.ts) AS last_ts
            FROM groups g
            LEFT JOIN messages m ON m.group_id = g.group_id
            GROUP BY g.group_id
            ORDER BY g.group_id
            """
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["last_time"] = _fmt_ts(d.pop("last_ts")) if d.get("last_ts") else None
            out.append(d)
        return out

    def get_group(self, group_id: int) -> Optional[dict]:
        row = self._conn().execute(
            "SELECT * FROM groups WHERE group_id=?", (group_id,)
        ).fetchone()
        return dict(row) if row else None

    # ---------- 成员 ----------
    def upsert_member(self, group_id: int, user_id: int, nickname: str,
                      card: str = "", role: str = "member",
                      commit: bool = True) -> None:
        c = self._conn()
        c.execute(
            "INSERT INTO members(group_id, user_id, nickname, card, role) VALUES(?,?,?,?,?) "
            "ON CONFLICT(group_id, user_id) DO UPDATE SET "
            "nickname=excluded.nickname, card=excluded.card, role=excluded.role",
            (group_id, user_id, nickname, card, role),
        )
        if commit:
            c.commit()

    def get_members(self, group_id: int) -> list[dict]:
        rows = self._conn().execute(
            "SELECT * FROM members WHERE group_id=? ORDER BY role, nickname", (group_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def member_display(self, group_id: int, user_id: int, fallback: str = "") -> str:
        row = self._conn().execute(
            "SELECT nickname, card FROM members WHERE group_id=? AND user_id=?",
            (group_id, user_id),
        ).fetchone()
        if row:
            return row["card"] or row["nickname"] or fallback
        return fallback

    # ---------- 消息 ----------
    def insert_message(self, message_id: int, group_id: int, user_id: int,
                       nickname: str, msg_type: str, content: str,
                       raw_json: dict, ts: int,
                       important: bool = False, important_reason: str = "",
                       commit: bool = True) -> bool:
        """返回 True 表示新插入（用于统计），重复消息忽略返回 False。

        注意：
        - 只把「message_id 唯一约束冲突」当重复；NOT NULL / 类型等
          其他完整性错误必须抛出，否则数据会被静默丢弃且无从排查。
        - commit=False 供批量导入使用：调用方自己 BEGIN/COMMIT，整批失败可回滚。
        """
        c = self._conn()
        try:
            c.execute(
                "INSERT INTO messages(message_id, group_id, user_id, nickname, msg_type, "
                "content, raw_json, ts, important, important_reason) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (message_id, group_id, user_id, nickname, msg_type, content,
                 json.dumps(raw_json, ensure_ascii=False), ts,
                 1 if important else 0, important_reason),
            )
            if commit:
                c.commit()
            return True
        except sqlite3.IntegrityError as e:
            msg = str(e).lower()
            if "unique" in msg and "message_id" in msg:
                return False
            log.error("消息入库失败（非重复，已抛出）：%s；message_id=%r group_id=%r",
                      e, message_id, group_id)
            raise

    def set_important(self, msg_id: int, important: bool, reason: str = "手动标记") -> None:
        c = self._conn()
        c.execute(
            "UPDATE messages SET important=?, important_reason=? WHERE id=?",
            (1 if important else 0, reason if important else "", msg_id),
        )
        c.commit()

    def delete_imported(self, group_id: int, source: str = "file",
                        commit: bool = True) -> int:
        """删除某群由「文件导入」写入的消息，返回删除条数。

        用于 --replace 重导：先把旧文件导入清掉再写入，避免占位符升级后重复。

        只删 raw_json 中 `"source": "file"` 的行：本机 QQ 库同步（source=qq）
        与实时监听的消息不会被误删。升级前导入的旧行没有 source 字段，
        也会被保留（宁可不删，不可错删）。
        """
        c = self._conn()
        # 兼容来源标记的别名，避免改过名字的历史数据漏删
        aliases = {"file": ("file", "import"), "qq": ("qq", "ntqq"),
                   "live": ("live",)}.get(source, (source,))
        likes = " OR ".join(["raw_json LIKE ?"] * len(aliases))
        args: list = [group_id] + [f'%"source": "{a}"%' for a in aliases]
        cur = c.execute(
            f"DELETE FROM messages WHERE group_id=? AND ({likes})", args)
        if commit:
            c.commit()
        return cur.rowcount

    def delete_group(self, group_id: int, commit: bool = True) -> dict:
        """彻底删除某群：消息 + 成员 + 群记录。返回删除的消息数。

        只做 DB 清理；聊天 md / 媒体目录 / 向量块等派生数据请用
        ``purge_conversation()``（否则会留下“幽灵知识”，审计 P0-2）。
        """
        c = self._conn()
        cur = c.execute("DELETE FROM messages WHERE group_id=?", (group_id,))
        n_msg = cur.rowcount
        c.execute("DELETE FROM members WHERE group_id=?", (group_id,))
        c.execute("DELETE FROM groups WHERE group_id=?", (group_id,))
        if commit:
            c.commit()
        return {"messages": n_msg}

    def purge_conversation(self, group_id: int, *, drop_messages: bool = True,
                           drop_ocr: bool = True, commit: bool = True,
                           chat_md_names: list[str] | None = None) -> dict:
        """删除一个会话（群或好友）的全部数据与派生索引。

        与 ``delete_group`` 的区别：这里连**知识库派生数据**一起清，
        避免删群/移出白名单后，旧的聊天 md、向量块、事件日期、OCR 缓存
        还能被检索到（审计 P0-2）。

        - drop_messages=False：只清派生数据（白名单移出时用），消息本体保留，
          以后重新加入白名单还能重新渲染。
        - chat_md_names：该会话当前聊天 md 文件名列表（如 ``123456789_班级群.md``）；
          不传则按 ``chat/<gid>_`` 前缀扫数据库里的 source。

        返回值各字段统一为「**实际删除的行数**」（不是命中的文件数 / source 数）：
        ``messages``/``conversations``/``chunks``/``meta``/``ocr`` 依次是
        messages、conversations、kb_chunks、kb_meta、media_ocr 的删除行数，可直接
        相加作为「这次清掉多少派生数据」上报。
        """
        c = self._conn()
        out = {"messages": 0, "chunks": 0, "meta": 0, "ocr": 0,
               "conversations": 0}
        if drop_messages:
            cur = c.execute("DELETE FROM messages WHERE group_id=?", (group_id,))
            out["messages"] = cur.rowcount or 0
            c.execute("DELETE FROM members WHERE group_id=?", (group_id,))
            c.execute("DELETE FROM groups WHERE group_id=?", (group_id,))
            cur = c.execute("DELETE FROM conversations WHERE group_id=?", (group_id,))
            out["conversations"] = cur.rowcount or 0
        # 聊天 md 对应的向量块 / 事件日期（source 形如 chat/<文件名>）
        names = set(chat_md_names or [])
        for r in c.execute("SELECT source FROM kb_chunks WHERE source LIKE 'chat/%'"
                           ).fetchall():
            src = r["source"]
            fname = src.split("/", 1)[1]
            if fname in names or fname.startswith(f"{group_id}_"):
                cur = c.execute("DELETE FROM kb_chunks WHERE source=?", (src,))
                out["chunks"] += cur.rowcount or 0
                cur = c.execute("DELETE FROM kb_meta WHERE source=?", (src,))
                out["meta"] += cur.rowcount or 0
        # 媒体派生：向量块（若曾索引）+ OCR 缓存
        cur = c.execute("DELETE FROM kb_chunks WHERE source LIKE ?",
                        (f"media/{group_id}/%",))
        out["chunks"] += cur.rowcount or 0
        for r in c.execute("SELECT source FROM kb_meta WHERE source LIKE ?",
                           (f"media/{group_id}/%",)).fetchall():
            c.execute("DELETE FROM kb_meta WHERE source=?", (r["source"],))
            out["meta"] += 1
        if drop_ocr:
            cur = c.execute("DELETE FROM media_ocr WHERE rel_path LIKE ?",
                            (f"media/{group_id}/%",))
            out["ocr"] = cur.rowcount or 0
        if commit:
            c.commit()
        return out

    # ---------- 知识库向量索引（kb_chunks） ----------
    def kb_indexed_sources(self) -> dict[str, float]:
        """已索引文件 → mtime。用于增量更新判断。"""
        c = self._conn()
        rows = c.execute("SELECT source, MAX(mtime) FROM kb_chunks GROUP BY source")
        return {r[0]: (r[1] or 0.0) for r in rows}

    def kb_replace_chunks(self, source: str, chunks: list[dict]) -> int:
        """整文件替换：先删该 source 的全部块，再插入新的。返回插入条数。

        显式事务：中途任何一条失败就回滚，不会留下「删一半 / 插一半」的索引
        （旧实现先 DELETE 再逐条 INSERT，异常后同线程下一次 commit 会把半成品落盘，审计 P0-9）。
        """
        c = self._conn()
        started = not c.in_transaction
        if started:
            c.execute("BEGIN IMMEDIATE")
        try:
            c.execute("DELETE FROM kb_chunks WHERE source=?", (source,))
            for ch in chunks:
                c.execute(
                    "INSERT INTO kb_chunks(source, section, day, chunk_idx, text,"
                    " embedding, dim, mtime) VALUES(?,?,?,?,?,?,?,?)",
                    (source, ch["section"], ch.get("day"), ch["chunk_idx"],
                     ch["text"], ch["embedding"], ch.get("dim"), ch.get("mtime")),
                )
            c.commit()
        except Exception:
            if started:
                c.rollback()
            raise
        return len(chunks)

    def kb_delete_source(self, source: str) -> None:
        c = self._conn()
        c.execute("DELETE FROM kb_chunks WHERE source=?", (source,))
        c.commit()

    def kb_all_chunks(self) -> list[dict]:
        """取出全部已索引块（含向量）。"""
        c = self._conn()
        rows = c.execute(
            "SELECT id, source, section, day, chunk_idx, text, embedding, dim"
            " FROM kb_chunks ORDER BY source, chunk_idx")
        return [dict(r) for r in rows]

    def kb_source_index(self) -> list[dict]:
        """按文件聚合的索引概览：块数、字符数、事件日期范围。

        供 list_knowledge 工具使用——模型要先知道知识库里有哪些文件，
        才能决定用 read_document 读哪一个。
        """
        c = self._conn()
        rows = c.execute(
            "SELECT source, section, COUNT(*) AS n, SUM(LENGTH(text)) AS chars,"
            " MIN(day) AS d1, MAX(day) AS d2"
            " FROM kb_chunks GROUP BY source ORDER BY source").fetchall()
        return [dict(r) for r in rows]

    def kb_chunks_of_source(self, source: str) -> list[dict]:
        """取某个文件的全部块（不含向量，避免搬运大 BLOB）。"""
        c = self._conn()
        rows = c.execute(
            "SELECT id, source, section, day, chunk_idx, text FROM kb_chunks"
            " WHERE source=? ORDER BY chunk_idx", (source,)).fetchall()
        return [dict(r) for r in rows]

    def kb_check_dim(self, dim: int) -> bool:
        """新向量维度是否与库中已有索引一致（空库视为一致）。

        换 embedding 模型后维度不同，检索侧会按维度跳过旧块，页面却仍显示
        "已向量化"——在写入前拦下来，提示用 force 重建。
        """
        rows = self._conn().execute(
            "SELECT DISTINCT dim FROM kb_chunks"
            " WHERE dim IS NOT NULL AND dim > 0").fetchall()
        dims = {int(r[0]) for r in rows}
        return not dims or dims == {int(dim)}

    def message_span(self) -> tuple[int, int]:
        """最早/最新一条消息的时间戳（秒）；空库返回 (0, 0)。"""
        row = self._conn().execute("SELECT MIN(ts), MAX(ts) FROM messages").fetchone()
        return (int(row[0] or 0), int(row[1] or 0))

    def latest_ts(self, group_id: int) -> int:
        """某个会话（群或好友负数 gid）在库里的最新消息时间戳；无消息返回 0。"""
        row = self._conn().execute(
            "SELECT MAX(ts) FROM messages WHERE group_id=?", (int(group_id),)).fetchone()
        return int(row[0] or 0)

    def kb_stats(self) -> dict:
        """索引统计：文件数、块数、各分区块数、是否有向量。"""
        c = self._conn()
        total = c.execute("SELECT COUNT(*) FROM kb_chunks").fetchone()[0]
        files = c.execute("SELECT COUNT(DISTINCT source) FROM kb_chunks").fetchone()[0]
        with_vec = c.execute(
            "SELECT COUNT(*) FROM kb_chunks WHERE embedding IS NOT NULL").fetchone()[0]
        by_sec = {r[0]: r[1] for r in c.execute(
            "SELECT section, COUNT(*) FROM kb_chunks GROUP BY section")}
        dims = sorted({int(r[0]) for r in c.execute(
            "SELECT DISTINCT dim FROM kb_chunks"
            " WHERE dim IS NOT NULL AND dim > 0")})
        return {"chunks": total, "files": files, "with_vector": with_vec,
                "by_section": by_sec, "dims": dims}

    # ---------- AI 会话（conversations） ----------
    def create_conversation(self, conv_id: str, group_id: int | None = None,
                            title: str = "") -> dict:
        now = datetime.now().isoformat(timespec="seconds")
        c = self._conn()
        c.execute(
            "INSERT INTO conversations(id, title, group_id, created_at,"
            " updated_at, msg_count, messages) VALUES(?,?,?,?,?,0,'[]')",
            (conv_id, title, group_id, now, now))
        c.commit()
        return self.get_conversation(conv_id) or {}

    def get_conversation(self, conv_id: str) -> dict | None:
        row = self._conn().execute(
            "SELECT * FROM conversations WHERE id=?", (conv_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["messages"] = json.loads(d.get("messages") or "[]")
        return d

    def list_conversations(self, limit: int = 100) -> list[dict]:
        """会话列表：按更新时间倒序。messages 正文字段不读出，避免传输大量数据。"""
        rows = self._conn().execute(
            "SELECT id, title, group_id, created_at, updated_at, msg_count"
            " FROM conversations ORDER BY updated_at DESC LIMIT ?",
            (limit,)).fetchall()
        return [dict(r) for r in rows]

    def save_conversation_messages(self, conv_id: str,
                                   messages: list[dict]) -> None:
        """整体覆盖保存消息数组（含工具调用记录）。"""
        c = self._conn()
        c.execute(
            "UPDATE conversations SET messages=?, msg_count=?, updated_at=? "
            "WHERE id=?",
            (json.dumps(messages, ensure_ascii=False), len(messages),
             datetime.now().isoformat(timespec="seconds"), conv_id))
        c.commit()

    def rename_conversation(self, conv_id: str, title: str) -> None:
        c = self._conn()
        c.execute("UPDATE conversations SET title=?, updated_at=? WHERE id=?",
                  (title, datetime.now().isoformat(timespec="seconds"), conv_id))
        c.commit()

    def delete_conversation(self, conv_id: str) -> None:
        c = self._conn()
        c.execute("DELETE FROM conversations WHERE id=?", (conv_id,))
        c.commit()

    def search_conversations(self, keyword: str, limit: int = 50) -> list[dict]:
        """搜索会话：标题 + 消息正文（JSON 字符串直接 LIKE 匹配）。"""
        kw = f"%{keyword}%"
        rows = self._conn().execute(
            "SELECT id, title, group_id, created_at, updated_at, msg_count"
            " FROM conversations WHERE title LIKE ? OR messages LIKE ?"
            " ORDER BY updated_at DESC LIMIT ?",
            (kw, kw, limit)).fetchall()
        return [dict(r) for r in rows]

    def kb_clear(self) -> None:
        c = self._conn()
        c.execute("DELETE FROM kb_chunks")
        c.commit()

    # ---------- 聊天图片 OCR 缓存（media_ocr） ----------
    def ocr_known(self) -> dict[str, tuple[int, float]]:
        """已 OCR 的图片 → (size, mtime)，用于增量跳过。"""
        rows = self._conn().execute(
            "SELECT rel_path, size, mtime FROM media_ocr").fetchall()
        return {r["rel_path"]: (r["size"] or 0, r["mtime"] or 0.0)
                for r in rows}

    def ocr_put(self, rel_path: str, size: int, mtime: float, text: str) -> None:
        """写入/更新一条 OCR 结果。text 为空表示识别无文字（也缓存，避免重复跑）。"""
        c = self._conn()
        c.execute(
            "INSERT INTO media_ocr(rel_path, size, mtime, text, updated_at)"
            " VALUES(?,?,?,?,datetime('now')) "
            "ON CONFLICT(rel_path) DO UPDATE SET size=excluded.size,"
            " mtime=excluded.mtime, text=excluded.text,"
            " updated_at=excluded.updated_at",
            (rel_path, size, mtime, text))
        c.commit()

    def ocr_group_map(self, group_id: int) -> dict[str, str]:
        """某群全部有文字的 OCR 结果：{键: Markdown}。

        同时提供两种键：
        - ``"<年月>/<文件名>"``：跨月同名文件不会互相覆盖（推荐，渲染时按消息
          所在月份精确匹配）
        - ``"<文件名>"``：兼容旧调用方 / 消息时间未知时的兜底
        """
        rows = self._conn().execute(
            "SELECT rel_path, text FROM media_ocr"
            " WHERE rel_path LIKE ? AND text IS NOT NULL AND text != ''",
            (f"media/{group_id}/%",)).fetchall()
        out: dict[str, str] = {}
        for r in rows:
            rel = r["rel_path"]
            name = rel.rsplit("/", 1)[-1]
            parts = rel.split("/")
            if len(parts) >= 4:
                out[f"{parts[-2]}/{name}"] = r["text"]
            out[name] = r["text"]
        return out

    def ocr_del(self, rel_path: str) -> None:
        c = self._conn()
        c.execute("DELETE FROM media_ocr WHERE rel_path=?", (rel_path,))
        c.commit()

    def ocr_del_group(self, group_id: int) -> int:
        """删群时清理该群的 OCR 缓存，返回删除条数。"""
        return self.ocr_del_prefix(f"media/{group_id}/")

    def ocr_del_prefix(self, prefix: str) -> int:
        """按相对路径前缀清理 OCR 缓存（删媒体文件/整月/整群时用）。"""
        c = self._conn()
        cur = c.execute("DELETE FROM media_ocr WHERE rel_path LIKE ?",
                        (prefix + "%",))
        c.commit()
        return cur.rowcount or 0

    def ocr_clear(self) -> int:
        """清空全部 OCR 缓存（重建前调用），返回删除条数。"""
        c = self._conn()
        cur = c.execute("DELETE FROM media_ocr")
        c.commit()
        return cur.rowcount or 0

    def ocr_stats(self) -> dict:
        c = self._conn()
        total = c.execute("SELECT COUNT(*) FROM media_ocr").fetchone()[0]
        with_text = c.execute(
            "SELECT COUNT(*) FROM media_ocr"
            " WHERE text IS NOT NULL AND text != ''").fetchone()[0]
        chars = c.execute(
            "SELECT COALESCE(SUM(LENGTH(text)), 0) FROM media_ocr").fetchone()[0]
        return {"total": total, "with_text": with_text, "chars": chars}

    # ---------- 知识库文件事件日期（kb_meta） ----------
    def kb_get_meta(self, source: str) -> dict:
        row = self._conn().execute(
            "SELECT * FROM kb_meta WHERE source=?", (source,)).fetchone()
        return dict(row) if row else {}

    def kb_all_meta(self) -> dict[str, dict]:
        return {r["source"]: dict(r) for r in
                self._conn().execute("SELECT * FROM kb_meta").fetchall()}

    def kb_set_meta(self, source: str, event_date: str, end_date: str = "",
                    note: str = "", auto: bool = False) -> None:
        """设定文件的事件日期。auto=False 表示用户手动设定（优先级高于自动推断）。"""
        c = self._conn()
        c.execute(
            "INSERT INTO kb_meta(source, event_date, end_date, note, auto,"
            " updated_at) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(source) DO UPDATE SET "
            "event_date=excluded.event_date, end_date=excluded.end_date, "
            "note=excluded.note, auto=excluded.auto, updated_at=excluded.updated_at",
            (source, event_date, end_date, note, 1 if auto else 0,
             datetime.now().isoformat(timespec="seconds")),
        )
        c.commit()

    def kb_del_meta(self, source: str) -> None:
        c = self._conn()
        c.execute("DELETE FROM kb_meta WHERE source=?", (source,))
        c.commit()

    def kb_set_chunks_day(self, source: str, day: str) -> None:
        """批量更新某文件所有片段的日期（改事件日期后立即生效，无需重建索引）。"""
        c = self._conn()
        c.execute("UPDATE kb_chunks SET day=? WHERE source=?", (day, source))
        c.commit()

    def kb_chunks_by_date(self, date_from: str, date_to: str) -> list[dict]:
        """取指定日期范围内的片段。day 为空的片段视为「无日期」，默认排除。"""
        c = self._conn()
        rows = c.execute(
            "SELECT source, section, day, chunk_idx, text FROM kb_chunks "
            "WHERE day IS NOT NULL AND day != '' AND day >= ? AND day <= ? "
            "ORDER BY day, source, chunk_idx",
            (date_from, date_to)).fetchall()
        return [dict(r) for r in rows]

    def kb_undated_sources(self, kdir) -> list[str]:
        """列出尚未设定事件日期的源文件（用于提示用户补录）。"""
        out = []
        for r in self._conn().execute(
                "SELECT DISTINCT source FROM kb_chunks").fetchall():
            src = r["source"]
            if src.startswith("chat/"):
                continue  # 聊天自带日期，无需补录
            meta = self.kb_get_meta(src)
            if not meta.get("event_date"):
                out.append(src)
        return sorted(out)

    def get_message(self, msg_id: int) -> Optional[dict]:
        row = self._conn().execute("SELECT * FROM messages WHERE id=?", (msg_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["time_str"] = _fmt_ts(d["ts"])  # 与 query_messages 保持一致，详情页模板需要
        return d

    def query_messages(self, group_id: Optional[int] = None, keyword: str = "",
                       user_id: Optional[int] = None, important_only: bool = False,
                       date_from: Optional[str] = None, date_to: Optional[str] = None,
                       limit: int = 200, offset: int = 0) -> list[dict]:
        sql = "SELECT * FROM messages WHERE 1=1"
        args: list = []
        if group_id is not None:
            sql += " AND group_id=?"
            args.append(group_id)
        if keyword:
            # 转义 LIKE 通配符，否则用户搜 "100%" 会把 % 当任意字符
            esc = keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            sql += " AND content LIKE ? ESCAPE '\\'"
            args.append(f"%{esc}%")
        if user_id is not None:
            sql += " AND user_id=?"
            args.append(user_id)
        if important_only:
            sql += " AND important=1"
        if date_from:
            sql += " AND ts >= ?"
            args.append(_date_to_ts(date_from, start=True))
        if date_to:
            sql += " AND ts < ?"
            args.append(_date_to_ts(date_to, start=False))
        sql += " ORDER BY ts DESC LIMIT ? OFFSET ?"
        args += [limit, offset]
        rows = self._conn().execute(sql, args).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["time_str"] = _fmt_ts(d["ts"])
            out.append(d)
        return out

    def count_messages(self, group_id: Optional[int] = None, keyword: str = "",
                       user_id: Optional[int] = None, important_only: bool = False,
                       date_from: Optional[str] = None, date_to: Optional[str] = None) -> int:
        """与 query_messages 的过滤条件保持一致，用于给「命中 N 条」提供真实总数。"""
        sql = "SELECT COUNT(*) AS n FROM messages WHERE 1=1"
        args: list = []
        if group_id is not None:
            sql += " AND group_id=?"
            args.append(group_id)
        if keyword:
            esc = keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            sql += " AND content LIKE ? ESCAPE '\\'"
            args.append(f"%{esc}%")
        if user_id is not None:
            sql += " AND user_id=?"
            args.append(user_id)
        if important_only:
            sql += " AND important=1"
        if date_from:
            sql += " AND ts >= ?"
            args.append(_date_to_ts(date_from, start=True))
        if date_to:
            sql += " AND ts < ?"
            args.append(_date_to_ts(date_to, start=False))
        return self._conn().execute(sql, args).fetchone()["n"]

    def get_stats(self) -> dict:
        c = self._conn()
        today_start = _date_to_ts(datetime.now().strftime("%Y-%m-%d"), start=True)
        yesterday_start = _date_to_ts(
            (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d"), start=True
        )
        def one(sql: str, args: tuple = ()) -> int:
            return c.execute(sql, args).fetchone()[0]

        stats = {
            "groups": one("SELECT COUNT(*) FROM groups"),
            "messages": one("SELECT COUNT(*) FROM messages"),
            "today_messages": one("SELECT COUNT(*) FROM messages WHERE ts>=?", (today_start,)),
            "yesterday_messages": one(
                "SELECT COUNT(*) FROM messages WHERE ts>=? AND ts<?",
                (yesterday_start, today_start),
            ),
            "members": one("SELECT COUNT(*) FROM members"),
            "meetings": one("SELECT COUNT(*) FROM meetings"),
            "important": one("SELECT COUNT(*) FROM messages WHERE important=1"),
        }
        # 各群消息排行
        stats["group_ranking"] = [
            dict(r)
            for r in c.execute(
                """
                SELECT g.group_id, g.name, COUNT(m.id) AS msg_count, MAX(m.ts) AS last_ts
                FROM messages m JOIN groups g ON g.group_id = m.group_id
                GROUP BY g.group_id ORDER BY msg_count DESC LIMIT 10
                """
            ).fetchall()
        ]
        # 最近活跃
        stats["recent"] = [
            dict(r)
            for r in c.execute(
                """
                SELECT * FROM messages
                WHERE ts >= ?
                ORDER BY ts DESC LIMIT 30
                """,
                (datetime.now().timestamp() - 3 * 86400,),
            ).fetchall()
        ]
        for m in stats["recent"]:
            m["time_str"] = _fmt_ts(m["ts"])
        return stats

    def distinct_senders(self, group_id: int) -> list[dict]:
        rows = self._conn().execute(
            """
            SELECT user_id, nickname, COUNT(*) AS n
            FROM messages WHERE group_id=? GROUP BY user_id
            ORDER BY n DESC
            """,
            (group_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ---------- 会议 ----------
    def add_meeting(self, title: str, held_date: str, notes: str = "",
                    recording_path: str = "") -> int:
        c = self._conn()
        cur = c.execute(
            "INSERT INTO meetings(title, held_date, notes, recording_path, created_at) "
            "VALUES(?,?,?,?,?)",
            (title, held_date, notes, recording_path,
             datetime.now().isoformat(timespec="seconds")),
        )
        c.commit()
        return cur.lastrowid

    def update_meeting(self, mid: int, **fields) -> None:
        allowed = {"title", "held_date", "notes", "recording_path"}
        sets = [f"{k}=?" for k in fields if k in allowed]
        if not sets:
            return
        c = self._conn()
        c.execute(
            f"UPDATE meetings SET {', '.join(sets)} WHERE id=?",
            [fields[k] for k in fields if k in allowed] + [mid],
        )
        c.commit()

    def delete_meeting(self, mid: int) -> None:
        c = self._conn()
        c.execute("DELETE FROM meetings WHERE id=?", (mid,))
        c.commit()

    def list_meetings(self, limit: int = 100) -> list[dict]:
        rows = self._conn().execute(
            "SELECT * FROM meetings ORDER BY held_date DESC, id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def get_meeting(self, mid: int) -> Optional[dict]:
        row = self._conn().execute("SELECT * FROM meetings WHERE id=?", (mid,)).fetchone()
        return dict(row) if row else None

    # ---------- 群文件（消息里的文件元素 → 本地文件 → 知识库） ----------
    # 记录本身只是「一条文件消息」，真正的下载/复制由 app/group_files.py 完成：
    # 只有本机已有副本（QQ 里点过下载，消息元素带 ::NTOSFull:: 路径）时才自动入库。
    def upsert_group_file(self, group_id: int, msg_ts: int, name: str, *,
                          size: int = 0, md5: str = "", file_id: str = "",
                          busid: int = 0, local_path: str = "") -> bool:
        """记录文件消息；新插入返回 True，已存在只补全信息（不覆盖已入库状态）。"""
        c = self._conn()
        now = time.time()
        cur = c.execute(
            "INSERT OR IGNORE INTO group_files"
            "(group_id, msg_ts, name, size, md5, file_id, busid, local_path,"
            " status, updated_at) VALUES(?,?,?,?,?,?,?,?, 'pending', ?)",
            (group_id, msg_ts, name, size, md5, file_id, busid, local_path, now),
        )
        new = cur.rowcount > 0
        if not new:
            c.execute(
                "UPDATE group_files SET"
                " local_path=COALESCE(NULLIF(?,''), local_path),"
                " size=COALESCE(NULLIF(?,0), size),"
                " md5=COALESCE(NULLIF(?,''), md5),"
                " file_id=COALESCE(NULLIF(?,''), file_id),"
                " busid=COALESCE(NULLIF(?,0), busid), updated_at=?"
                " WHERE group_id=? AND msg_ts=? AND name=?",
                (local_path, size, md5, file_id, busid, now,
                 group_id, msg_ts, name),
            )
        c.commit()
        return new

    def list_group_files(self, status: Optional[str] = None,
                         group_id: Optional[int] = None,
                         limit: int = 200, offset: int = 0,
                         q: str = "") -> list[dict]:
        sql = "SELECT * FROM group_files"
        args: list = []
        where = []
        if status:
            where.append("status=?")
            args.append(status)
        if group_id is not None:
            where.append("group_id=?")
            args.append(group_id)
        q = (q or "").strip()
        if q:
            like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            where.append("name LIKE ? ESCAPE '\\'")
            args.append(like)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY msg_ts DESC, name LIMIT ? OFFSET ?"
        args += [limit, offset]
        return [dict(r) for r in self._conn().execute(sql, args).fetchall()]

    def count_group_files(self, status: Optional[str] = None, q: str = "") -> int:
        """与 list_group_files 相同过滤条件下的总条数（供分页）。"""
        sql = "SELECT COUNT(*) FROM group_files"
        args: list = []
        where = []
        if status:
            where.append("status=?")
            args.append(status)
        q = (q or "").strip()
        if q:
            like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            where.append("name LIKE ? ESCAPE '\\'")
            args.append(like)
        if where:
            sql += " WHERE " + " AND ".join(where)
        return int(self._conn().execute(sql, args).fetchone()[0])

    def group_files_stats(self) -> dict:
        """按状态统计。返回 {total, pending, missing, ingested, archived, skipped, duplicate}。"""
        out = {"total": 0, "pending": 0, "missing": 0, "ingested": 0,
               "archived": 0, "skipped": 0, "duplicate": 0, "error": 0}
        rows = self._conn().execute(
            "SELECT status, COUNT(*) AS n FROM group_files GROUP BY status"
        ).fetchall()
        for r in rows:
            out[r["status"]] = r["n"]
            out["total"] += r["n"]
        return out

    def update_group_file(self, group_id: int, msg_ts: int, name: str, *,
                          status: str, doc_path: Optional[str] = None,
                          md_path: Optional[str] = None,
                          detail: Optional[str] = None,
                          local_path: Optional[str] = None) -> None:
        c = self._conn()
        c.execute(
            "UPDATE group_files SET status=?,"
            " doc_path=COALESCE(?, doc_path),"
            " md_path=COALESCE(?, md_path),"
            " detail=COALESCE(?, detail),"
            " local_path=COALESCE(?, local_path), updated_at=?"
            " WHERE group_id=? AND msg_ts=? AND name=?",
            (status, doc_path, md_path, detail, local_path, time.time(),
             group_id, msg_ts, name),
        )
        c.commit()

    # ---------- 录音上传 ----------
    def add_recording(self, *, filename: str, course: str, rec_date: str,
                      md5: str, size: int, status: str = "pending",
                      stored_path: str = "", detail: str = "") -> int:
        c = self._conn()
        now = time.time()
        cur = c.execute(
            "INSERT INTO recordings(filename, course, rec_date, md5, size,"
            " status, stored_path, detail, created_at, updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (filename, course, rec_date, md5, size, status, stored_path,
             detail, now, now),
        )
        c.commit()
        return int(cur.lastrowid)

    def recording_by_md5(self, md5: str) -> Optional[dict]:
        row = self._conn().execute(
            "SELECT * FROM recordings WHERE md5=? LIMIT 1", (md5,)
        ).fetchone()
        return dict(row) if row else None

    def get_recording(self, rec_id: int) -> Optional[dict]:
        row = self._conn().execute(
            "SELECT * FROM recordings WHERE id=?", (rec_id,)).fetchone()
        return dict(row) if row else None

    def update_recording(self, rec_id: int, *, status: Optional[str] = None,
                         detail: Optional[str] = None,
                         md_path: Optional[str] = None,
                         summary_path: Optional[str] = None,
                         schedule_path: Optional[str] = None,
                         stored_path: Optional[str] = None) -> None:
        """按需更新录音行；只更新传入的列，其余列原样保留（COALESCE）。

        stored_path 是「原件归档后的路径」：转写成功后原件会被移进
        knowledge/documents/（app/recordings.py 的 _archive_source），
        这里让调用方用公开 API 改指副本，不必再手写 UPDATE。
        """
        c = self._conn()
        c.execute(
            "UPDATE recordings SET status=COALESCE(?, status),"
            " detail=COALESCE(?, detail), md_path=COALESCE(?, md_path),"
            " summary_path=COALESCE(?, summary_path),"
            " schedule_path=COALESCE(?, schedule_path),"
            " stored_path=COALESCE(?, stored_path),"
            " updated_at=? WHERE id=?",
            (status, detail, md_path, summary_path, schedule_path,
             stored_path, time.time(), rec_id),
        )
        c.commit()

    def list_recordings(self, limit: int = 50) -> list[dict]:
        rows = self._conn().execute(
            "SELECT id, filename, course, rec_date, md5, size, status, md_path,"
            " summary_path, schedule_path, detail, created_at, updated_at"
            " FROM recordings ORDER BY id DESC LIMIT ?", (max(1, int(limit)),)).fetchall()
        return [dict(r) for r in rows]

    def recordings_stats(self) -> dict:
        out = {"total": 0}
        for r in self._conn().execute(
                "SELECT status, COUNT(*) AS n FROM recordings GROUP BY status"):
            out[r["status"]] = r["n"]
            out["total"] += r["n"]
        return out

    def group_file_done_md5(self, md5: str,
                            exclude: tuple[int, int, str] | None = None) -> bool:
        """同一文件（md5 相同）是否已经入库过，避免重复归档。

        exclude=(group_id, msg_ts, name)：重试「已归档」记录时排除它自己，
        否则会把自己当成重复项直接跳过。
        """
        if not md5:
            return False
        sql = ("SELECT 1 FROM group_files WHERE md5=? AND status IN"
               " ('ingested','archived')")
        params: list = [md5]
        if exclude:
            sql += " AND NOT (group_id=? AND msg_ts=? AND name=?)"
            params.extend(exclude)
        row = self._conn().execute(sql + " LIMIT 1", params).fetchone()
        return bool(row)

    # ---------- 日程 ----------
    def _similar_schedule_row(self, date: str, title: str) -> "sqlite3.Row | None":
        """同一天里标题近似的一条（跨来源去重）。

        同一件事会被三条链路抽出：录音（recording）、其总结文档（file）、
        群聊消息（chat）。标题措辞略异就不会命中 dedupe_key，日历里看着就是重复，
        所以这里再做一层「同日 + 标题近似」的判定。
        """
        want = _norm_title_for_match(title)
        if len(want) < 4:
            return None
        rows = self._conn().execute(
            "SELECT * FROM schedule_items WHERE date=?", (date,)).fetchall()
        best = None
        best_score = 0.0
        for r in rows:
            have = _norm_title_for_match(r["title"] or "")
            if len(have) < 4:
                continue
            if want == have:
                score = 1.0
            elif (want in have or have in want) and min(len(want), len(have)) >= 6:
                score = 0.9
            else:
                score = difflib.SequenceMatcher(None, want, have).ratio()
            if score > best_score:
                best_score = score
                best = r
        return best if best_score >= 0.72 else None

    def upsert_schedule_item(self, *, date: str, time: str = "", title: str,
                             detail: str = "", source: dict | None = None,
                             confidence: float = 0, dedupe_key: str = "") -> int:
        """写入一条日程；dedupe_key 或「同日标题近似」命中时合并来源而不是重复插入。

        返回值：写入/命中的行 id；**返回 0 表示被用户墓碑（schedule_dismissed）丢弃**，
        本次内容没有落库。调用方若把返回值当「新 id」用，需要容忍 0。
        两处保护（手动编辑过的条目 manual=1 归用户所有）：
        1. 命中 manual=1 的行时只并 sources / 提升 confidence，不改 title/time/detail/date；
        2. 用户删掉的条目有墓碑，命中墓碑直接丢弃，不复活。
        """
        c = self._conn()
        now = datetime.now().timestamp()
        src = dict(source or {})
        key = dedupe_key or f"{date}|{time}|{title.strip().lower()}"
        if self.is_schedule_dismissed(key, date, title):
            log.info("日程被墓碑丢弃：%s %s", date, title)
            return 0
        row = c.execute("SELECT * FROM schedule_items WHERE dedupe_key=?",
                        (key,)).fetchone()
        keep_title = False
        if row is None:
            row = self._similar_schedule_row(date, title)
            if row is not None:
                # 命中近似条目：沿用原有标题与 dedupe_key，只补充来源与详情
                key = row["dedupe_key"]
                keep_title = True
        if row:
            sources = []
            try:
                sources = json.loads(row["sources"] or "[]")
            except ValueError:
                sources = []
            if src and src.get("id") not in {s.get("id") for s in sources}:
                sources.append(src)
            if row["manual"]:
                # 手动编辑过的条目：内容归用户，只并来源与置信度
                c.execute(
                    "UPDATE schedule_items SET sources=?,"
                    " source_type=COALESCE(NULLIF(?, ''), source_type),"
                    " source_id=COALESCE(NULLIF(?, ''), source_id),"
                    " source_ref=COALESCE(NULLIF(?, ''), source_ref),"
                    " source_quote=COALESCE(NULLIF(?, ''), source_quote),"
                    " source_label=COALESCE(NULLIF(?, ''), source_label),"
                    " confidence=MAX(confidence, ?), updated_at=?"
                    " WHERE id=?",
                    (json.dumps(sources, ensure_ascii=False), src.get("type", ""),
                     str(src.get("id", "")) or "", src.get("ref", ""),
                     src.get("quote", ""), src.get("label", ""),
                     float(confidence), now, row["id"]),
                )
                c.commit()
                return int(row["id"])
            final_title = row["title"] if keep_title else title
            detail = detail if len(detail or "") > len(row["detail"] or "") else row["detail"]
            c.execute(
                "UPDATE schedule_items SET time=COALESCE(NULLIF(?, ''), time),"
                " title=?, detail=?, source_type=COALESCE(NULLIF(?, ''), source_type),"
                " source_id=COALESCE(NULLIF(?, ''), source_id),"
                " source_ref=COALESCE(NULLIF(?, ''), source_ref),"
                " source_quote=COALESCE(NULLIF(?, ''), source_quote),"
                " source_label=COALESCE(NULLIF(?, ''), source_label),"
                " sources=?, confidence=MAX(confidence, ?), updated_at=?"
                " WHERE id=?",
                (time, final_title, detail or "", src.get("type", ""),
                 str(src.get("id", "")) or "", src.get("ref", ""),
                 src.get("quote", ""), src.get("label", ""),
                 json.dumps(sources, ensure_ascii=False), float(confidence),
                 now, row["id"]),
            )
            c.commit()
            return int(row["id"])
        cur = c.execute(
            "INSERT INTO schedule_items(date, time, title, detail, source_type,"
            " source_id, source_ref, source_quote, source_label, sources,"
            " confidence, dedupe_key, created_at, updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (date, time, title, detail or "", src.get("type", ""),
             str(src.get("id", "")) or "", src.get("ref", ""),
             src.get("quote", ""), src.get("label", ""),
             json.dumps([src] if src else [], ensure_ascii=False),
             float(confidence), key, now, now),
        )
        c.commit()
        return int(cur.lastrowid or 0)

    def list_schedule_items(self, start: str = "", end: str = "",
                            limit: int = 2000) -> list[dict]:
        sql = "SELECT * FROM schedule_items WHERE 1=1"
        args: list = []
        if start:
            sql += " AND date>=?"
            args.append(start)
        if end:
            sql += " AND date<=?"
            args.append(end)
        sql += " ORDER BY date, time, id LIMIT ?"
        args.append(max(1, int(limit)))
        out = []
        for r in self._conn().execute(sql, args).fetchall():
            d = dict(r)
            try:
                d["sources"] = json.loads(d.get("sources") or "[]")
            except ValueError:
                d["sources"] = []
            out.append(d)
        return out

    # ---------- 日程：手动编辑 ----------
    def get_schedule_item(self, item_id: int) -> dict | None:
        """取单条日程（sources 已解析成 list）；不存在返回 None。"""
        r = self._conn().execute("SELECT * FROM schedule_items WHERE id=?",
                                 (int(item_id),)).fetchone()
        if r is None:
            return None
        d = dict(r)
        try:
            d["sources"] = json.loads(d.get("sources") or "[]")
        except ValueError:
            d["sources"] = []
        return d

    def update_schedule_manual(self, item_id: int, *, date: str, time: str,
                               title: str, detail: str) -> bool:
        """手动改写一条日程，并把 `manual` 置 1（此后自动抽取不再改它的内容）。

        dedupe_key 同时改成 `manual:{item_id}`：用户改过的内容与原来的自动 key
        已经不对应，继续占着旧 key 只会让下一轮抽取误以为「这条不存在」。
        返回是否命中行（False = 这条已被删掉）。不改 created_at。
        注意：改名后旧自动 key 就空出来了。下一轮抽取通常会被
        `_similar_schedule_row`（同日+标题相似度 ≥0.72）并回本条；只有当用户把
        标题改得和原措辞差太多（相似度不足）时，才可能再冒出一条重复日程。
        """
        c = self._conn()
        cur = c.execute(
            "UPDATE schedule_items SET date=?, time=?, title=?, detail=?,"
            " manual=1, dedupe_key=?, updated_at=? WHERE id=?",
            (date, time, title, detail or "", f"manual:{int(item_id)}",
             datetime.now().timestamp(), int(item_id)),
        )
        c.commit()
        return bool(cur.rowcount)

    def delete_schedule_item(self, item_id: int, *, dismiss: bool = True) -> bool:
        """删除一条日程；dismiss=True 时先写墓碑，挡住下一轮自动抽取复活它。

        墓碑同时留 dedupe_key 与 (date, title_norm) 两把钥匙：自动抽取换个
        时刻/措辞就会算出不同 dedupe_key，只靠 key 挡不住。
        返回是否真的删掉了行。
        """
        row = self._conn().execute(
            "SELECT dedupe_key, date, title FROM schedule_items WHERE id=?",
            (int(item_id),)).fetchone()
        if row is None:
            return False
        c = self._conn()
        if dismiss:
            self._add_schedule_dismissed(
                row["dedupe_key"] or f"manual:{int(item_id)}",
                row["date"] or "", row["title"] or "")
        cur = c.execute("DELETE FROM schedule_items WHERE id=?", (int(item_id),))
        c.commit()
        return bool(cur.rowcount)

    def _add_schedule_dismissed(self, dedupe_key: str, date: str,
                                title: str) -> None:
        """写（或刷新）一条墓碑。同一 key 重复删除只更新，不报 UNIQUE 冲突。"""
        c = self._conn()
        c.execute(
            "INSERT INTO schedule_dismissed(dedupe_key, date, title_norm, created_at)"
            " VALUES(?,?,?,?)"
            " ON CONFLICT(dedupe_key) DO UPDATE SET"
            " date=excluded.date, title_norm=excluded.title_norm,"
            " created_at=excluded.created_at",
            (dedupe_key, date, _norm_title_for_match(title),
             datetime.now().timestamp()),
        )
        c.commit()

    def is_schedule_dismissed(self, dedupe_key: str, date: str,
                              title: str) -> bool:
        """这条日程是不是被用户删过（墓碑命中）。

        两把钥匙任一命中就算：dedupe_key 完全相同，或同日 + 标题归一化后相同。
        后者的存在是因为自动抽取的 key 里含时刻与原始措辞，重跑一次就会变。
        """
        c = self._conn()
        if dedupe_key:
            row = c.execute(
                "SELECT 1 FROM schedule_dismissed WHERE dedupe_key=? LIMIT 1",
                (dedupe_key,)).fetchone()
            if row:
                return True
        norm = _norm_title_for_match(title)
        if date and norm:
            row = c.execute(
                "SELECT 1 FROM schedule_dismissed WHERE date=? AND title_norm=?"
                " LIMIT 1", (date, norm)).fetchone()
            return bool(row)
        return False

    def add_schedule_manual(self, *, date: str, time: str = "", title: str,
                            detail: str = "") -> int:
        """用户手工新增一条日程，返回新 id。

        先插入（dedupe_key 留 NULL，SQLite 的 UNIQUE 允许多个 NULL），再回填
        `manual:{id}`，这样 key 与 update_schedule_manual 里的规则一致。
        不查墓碑：用户亲手加的条目优先于「我以前删过类似的」。
        """
        c = self._conn()
        now = datetime.now().timestamp()
        cur = c.execute(
            "INSERT INTO schedule_items(date, time, title, detail, source_type,"
            " source_id, source_ref, source_quote, source_label, sources,"
            " confidence, dedupe_key, manual, created_at, updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (date, time, title, detail or "", "manual", "", "", "", "", "[]",
             1.0, None, 1, now, now),
        )
        new_id = int(cur.lastrowid or 0)
        if new_id:
            c.execute("UPDATE schedule_items SET dedupe_key=? WHERE id=?",
                      (f"manual:{new_id}", new_id))
        c.commit()
        return new_id

    def chat_messages_between(self, start: float, end: float,
                              group_ids: Iterable[int] | None = None,
                              limit: int = 6000) -> list[dict]:
        sql = ("SELECT m.id, m.message_id, m.group_id, m.nickname, m.content,"
               " m.ts, m.msg_type, g.name AS group_name"
               " FROM messages m LEFT JOIN groups g ON g.group_id=m.group_id"
               " WHERE m.ts>=? AND m.ts<? AND m.group_id>0")
        args: list = [float(start), float(end)]
        ids = sorted({int(g) for g in (group_ids or [])})
        if ids:
            sql += " AND m.group_id IN (" + ",".join("?" * len(ids)) + ")"
            args.extend(ids)
        sql += " ORDER BY m.ts DESC, m.id DESC LIMIT ?"
        args.append(max(1, int(limit)))
        rows = [dict(r) for r in self._conn().execute(sql, args).fetchall()]
        rows.reverse()  # 转回时间正序，便于模型按上下文阅读
        return rows

    # ---------- 摘要 ----------
    def day_summary(self, day: str, group_id: Optional[int] = None) -> list[dict]:
        """某天按群聚合：重要消息 + 统计。"""
        start = _date_to_ts(day, start=True)
        end = start + 86400
        sql = "SELECT * FROM messages WHERE ts>=? AND ts<?"
        args: list = [start, end]
        if group_id is not None:
            sql += " AND group_id=?"
            args.append(group_id)
        sql += " ORDER BY group_id, ts"
        rows = self._conn().execute(sql, args).fetchall()

        groups: dict[int, dict] = {}
        for r in rows:
            m = dict(r)
            m["time_str"] = _fmt_ts(m["ts"])
            g = groups.setdefault(
                m["group_id"],
                {"group_id": m["group_id"], "group_name": "", "messages": [], "users": {}},
            )
            g["messages"].append(m)
            g["users"][m["user_id"]] = (m["nickname"] or "未知") + f"({m['user_id']})"
        for gid in groups:
            g = groups[gid]
            grp = self.get_group(gid)
            g["group_name"] = grp["name"] if grp else f"群 {gid}"
        return sorted(groups.values(), key=lambda g: -len(g["messages"]))


def _fmt_ts(ts: int) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def _date_to_ts(day: str, start: bool = True) -> int:
    dt = datetime.strptime(day, "%Y-%m-%d")
    if not start:
        dt = dt + timedelta(days=1)
    return int(dt.timestamp())
