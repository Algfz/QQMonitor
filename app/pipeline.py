"""同步任务流水线：把「解密提取 → 入库 → 媒体 → OCR → 聊天渲染 → 索引」串成一次操作。

为什么单独立一个模块
-------------------
原来的流程散在 qq_sync / knowledge 里，且是同步阻塞：点一下「同步」要等
全部跑完才返回，OCR 几百张图会卡界面几分钟；而且 OCR 被埋在
sync_chat_group 里，用户不知道还要不要再点第二次。

本模块把它改成「任务化」：
- **分阶段**：提取 → 入库 → 媒体 → OCR → 聊天渲染 → 索引，每步独立可见
- **后台执行**：WebUI 点一下立即返回任务 ID，前端轮询进度，不卡界面
- **阶段式脏标记**：导入/删除/移出白名单后会 mark_dirty()，下一轮自动补齐，
  不再「没有新消息就永远不补索引/不补渲染」（审计 P0-2 / 批次 2 ⑮）
- **一次到底**：同步后自动做完全部清洗，不需要再点「重新同步」
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path

from .config import Settings
from .db import Database

log = logging.getLogger("pipeline")

# 阶段定义（顺序即执行顺序）
STAGES = ("extract", "media", "ocr", "chat", "index")
DIRTY_NAMES = ("media", "ocr", "chat", "index")

_STAGE_LABEL = {
    "extract": "解密提取消息",
    "media": "复制媒体文件",
    "ocr": "识别图片文字",
    "chat": "渲染聊天 Markdown",
    "index": "更新 RAG 向量索引",
}

_STATE_LOCK = threading.Lock()


# ---------- 阶段脏标记（跨进程持久化在 data/pipeline.state.json） ----------
def _state_path(cfg: Settings) -> Path:
    return cfg.resolve(cfg.storage["db_path"]).parent / "pipeline.state.json"


def _new_state() -> dict:
    return {"dirty": {}, "index_fail": {"count": 0, "time": 0.0}}


def load_state(cfg: Settings) -> dict:
    path = _state_path(cfg)
    if not path.is_file():
        return _new_state()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        log.warning("读取流水线状态失败（按全脏处理）：%s", e)
        return _new_state()
    if not isinstance(data, dict):
        return _new_state()
    data.setdefault("dirty", {})
    data.setdefault("index_fail", {"count": 0, "time": 0.0})
    return data


def _save_state(cfg: Settings, state: dict) -> None:
    path = _state_path(cfg)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, path)
    except OSError as e:
        log.warning("写入流水线状态失败：%s", e)


def mark_dirty(cfg: Settings, *names: str) -> None:
    """把阶段标记为脏，下一轮同步会补跑（导入/删除/移出白名单后调用）。"""
    with _STATE_LOCK:
        state = load_state(cfg)
        now = round(time.time(), 3)
        for n in names:
            if n in DIRTY_NAMES:
                state["dirty"][n] = now
        _save_state(cfg, state)


def clear_dirty(cfg: Settings, *names: str) -> None:
    with _STATE_LOCK:
        state = load_state(cfg)
        for n in names:
            state["dirty"].pop(n, None)
        _save_state(cfg, state)


def dirty_names(cfg: Settings) -> set[str]:
    return set(load_state(cfg)["dirty"].keys())


# ---------- 索引失败的指数退避（避免每轮都去撞一个挂掉的 embedding 服务） ----------
def index_backoff_left(cfg: Settings) -> int:
    """距离允许再次尝试索引还有多少秒（0=可以尝试）。"""
    info = load_state(cfg).get("index_fail") or {}
    count = int(info.get("count") or 0)
    last = float(info.get("time") or 0)
    if count <= 0:
        return 0
    wait = min(60 * (2 ** min(count - 1, 5)), 1800)
    return max(0, int(last + wait - time.time()))


def note_index_result(cfg: Settings, ok: bool) -> None:
    with _STATE_LOCK:
        state = load_state(cfg)
        if ok:
            state["index_fail"] = {"count": 0, "time": 0.0}
        else:
            old = state.get("index_fail") or {}
            state["index_fail"] = {"count": int(old.get("count") or 0) + 1,
                                   "time": time.time()}
        _save_state(cfg, state)


class _NoNewMessages(Exception):
    """内部信号：本次同步没有新消息（保留给旧调用方，主流程已不抛出）。"""


class SyncTask:
    """一次完整的同步任务。由 WebUI 后台线程驱动。"""

    def __init__(self, db: Database, cfg: Settings, force_index: bool = False):
        self.db = db
        self.cfg = cfg
        self.force_index = force_index
        self.stage = "extract"
        self.done = False
        self.error = ""
        self.detail = ""
        self.logs: list[str] = []
        self.skipped: list[str] = []
        # 各阶段结果
        self.extracted: dict = {}
        self.media: dict = {}
        self.ocr: dict = {}
        self.chat: list = []
        self.index: dict = {}

    def _say(self, msg: str) -> None:
        self.logs.append(msg)
        log.info("sync: %s", msg)

    def _set_stage(self, s: str) -> None:
        self.stage = s
        self._say(f"▶ {_STAGE_LABEL[s]}…")

    def _skip(self, stage: str, why: str) -> None:
        self.skipped.append(stage)
        self._say(f"  ⏭ {_STAGE_LABEL[stage]}：{why}")

    # ---------- 各阶段 ----------
    def _do_extract(self) -> None:
        from .qq_sync import sync_from_ntqq
        res = sync_from_ntqq(self.db, self.cfg)
        if not res["ok"]:
            raise RuntimeError(res.get("error", "解密提取失败"))
        self.extracted = res
        n = res.get("inserted", 0)
        missing = res.get("missing", [])
        self._say(f"  新增 {n} 条消息"
                  + (f"；{len(missing)} 个白名单群在本机库没有记录" if missing else ""))
        if missing:
            self._say(f"  ⚠ 本机库没有记录：{', '.join(str(g) for g in missing[:5])}")

    def _do_media(self) -> None:
        from .knowledge import sync_media
        kdir = self.cfg.resolve(self.cfg.knowledge.get("dir", "data/knowledge"))
        self.media = sync_media(self.db, self.cfg, kdir)
        self._say(f"  复制 {self.media['copied']} 个文件，跳过 {self.media['skipped']} 个")

    def _do_ocr(self) -> None:
        from .knowledge import build_media_ocr
        kdir = self.cfg.resolve(self.cfg.knowledge.get("dir", "data/knowledge"))
        if not self.cfg.knowledge.get("ocr_chat_images", True):
            self._say("  （OCR 已在设置中关闭，跳过）")
            self.ocr = {"total": 0, "ocr": 0, "skipped": 0}
            return

        def on_progress(done: int, total: int, name: str) -> None:
            # 每 10 张报一次，避免日志刷屏
            if done % 10 == 0 or done == total:
                self.logs.append(f"  OCR {done}/{total} {name}")

        self.ocr = build_media_ocr(self.db, kdir, cfg=self.cfg,
                                   on_progress=on_progress)
        self._say(f"  识别 {self.ocr['ocr']} 张有文字"
                  + (f"，跳过 {self.ocr['skipped']} 张已缓存" if self.ocr["skipped"] else ""))
        if self.ocr.get("error"):
            self._say(f"  ⚠ {self.ocr['error']}")

    def _do_chat(self) -> None:
        from .knowledge import sync_chat
        kdir = self.cfg.resolve(self.cfg.knowledge.get("dir", "data/knowledge"))
        # OCR 已在上一阶段完成，这里只做渲染（不重复跑 OCR）
        self.chat = sync_chat(self.db, kdir, cfg=self.cfg)
        self._say(f"  重建 {len(self.chat)} 个聊天文件")

    def _do_index(self) -> None:
        """增量更新 RAG 向量索引。失败不中断同步，但会退避重试。"""
        if not self.cfg.knowledge.get("auto_index", True):
            self._say("  （auto_index 已关闭，跳过）")
            return
        left = 0 if self.force_index else index_backoff_left(self.cfg)
        if left > 0:
            self._say(f"  （上次索引失败，{left}s 后重试；本次跳过）")
            self.index = {"error": f"退避中（{left}s）", "backoff": left}
            return
        try:
            from .llm.client import build_client, _embedding_provider
            from .llm.indexer import Indexer
            client = build_client(self.cfg, "embedding")
            client._require()  # 配置不全时提前报错
            # 换 embedding 模型/维度后必须全量重建，否则新旧向量空间混在一起
            emb = (self.cfg.llm.get("embedding") or {})
            sig = (f"{_embedding_provider(self.cfg, 'embedding')}:"
                   f"{emb.get('model')}:{emb.get('dimensions')}")
            old_sig = self.db.get_meta("index_model_sig")
            force = bool(old_sig) and old_sig != sig
            if force:
                self._say(f"  （向量模型已变更：{old_sig} → {sig}，全量重建）")
                log.warning("向量模型签名变化 %s → %s，本次强制重建索引", old_sig, sig)
            res = Indexer(self.db, self.cfg, client).build(force=force)
            self.index = res
            if not res.get("errors"):
                self.db.set_meta("index_model_sig", sig)
            self._say(f"  索引新增 {res['added']} 段，跳过 {res['skipped']} 个未变文件"
                      f"（知识库现共 {res['chunks']} 段）")
            if res.get("errors"):
                # 部分文件失败：保留脏标记，下轮退避后重试（成功的那部分已落库）
                mark_dirty(self.cfg, "index")
                note_index_result(self.cfg, False)
                self._say(f"  ⚠ {res['errors']} 个文件向量化失败（已记入待补索引）")
                log.warning("索引部分失败：%s 个文件", res["errors"])
            else:
                note_index_result(self.cfg, True)
        except Exception as e:
            # 索引失败不影响同步主流程（消息已入库，索引下次再补）
            self.index = {"error": str(e)}
            note_index_result(self.cfg, False)
            self._say(f"  ⚠ 索引更新失败（不影响消息同步）：{e}")
            log.warning("自动索引失败：%s", e, exc_info=True)

    # ---------- 驱动 ----------
    def run(self) -> "SyncTask":
        """在线程里执行全部阶段。异常被捕获进 error 字段，不抛出。"""
        try:
            self._set_stage("extract")
            self._do_extract()
            new = int(self.extracted.get("inserted") or 0)
            dirty = dirty_names(self.cfg)

            # 媒体：只有新消息或显式脏标记才重新扫（每轮要整份解密本机库，开销大）
            if new > 0 or "media" in dirty:
                self._set_stage("media")
                self._do_media()
                clear_dirty(self.cfg, "media")
            else:
                self._skip("media", "没有新消息且未标记为需要补跑")
            media_copied = int(self.media.get("copied") or 0)

            # OCR：有新增图片/新消息或脏标记才跑；无待识别图片时不加载模型
            if new > 0 or media_copied > 0 or "ocr" in dirty:
                self._set_stage("ocr")
                self._do_ocr()
                clear_dirty(self.cfg, "ocr")
            else:
                self._skip("ocr", "没有新图片且未标记为需要补跑")
            ocr_done = int(self.ocr.get("ocr") or 0) + int(self.ocr.get("empty") or 0)

            # 聊天渲染：消息/媒体/OCR 有变化或脏标记才重建
            if new > 0 or media_copied > 0 or ocr_done > 0 or "chat" in dirty:
                self._set_stage("chat")
                self._do_chat()
                clear_dirty(self.cfg, "chat")
            else:
                self._skip("chat", "内容没有变化且未标记为需要补跑")

            # 索引：聊天 md 有产出、或脏标记、或手动强制，才更新向量库
            if self.force_index or "index" in dirty or self.chat:
                self._set_stage("index")
                self._do_index()
                # 只有「真成功」才清脏标记；部分文件失败时 _do_index 已重新标脏
                if not self.index.get("error") and not self.index.get("errors"):
                    clear_dirty(self.cfg, "index")
            else:
                self._skip("index", "聊天内容没有变化")

            self.stage = "done"
            self._say("✅ 全部完成")
            self.detail = self._summary()
        except _NoNewMessages:
            self.detail = "没有新消息（已是最新）"
        except Exception as e:
            self.error = f"{type(e).__name__}: {e}"
            self._say(f"❌ {self.error}")
            log.exception("同步任务失败")
        self.done = True
        return self

    def _summary(self) -> str:
        parts = []
        if self.extracted.get("inserted"):
            parts.append(f"新增 {self.extracted['inserted']} 条消息")
        if self.media.get("copied"):
            parts.append(f"复制 {self.media['copied']} 个媒体文件")
        if self.ocr.get("ocr"):
            parts.append(f"识别 {self.ocr['ocr']} 张图片文字")
        if self.chat:
            parts.append(f"重建 {len(self.chat)} 个聊天文件")
        if self.index.get("added"):
            parts.append(f"索引 +{self.index['added']} 段")
        elif self.index.get("errors"):
            parts.append(f"⚠ {self.index['errors']} 个文件索引失败")
        elif self.index.get("error"):
            parts.append("⚠ 索引更新失败")
        if self.extracted.get("missing"):
            m = "、".join(str(g) for g in self.extracted["missing"][:3])
            parts.append(f"⚠ 本机库没有 {m} 的记录")
        if self.skipped:
            parts.append("跳过 " + "、".join(self.skipped))
        return "，".join(parts) if parts else "没有新变化（已是最新）"


# ---------- WebUI 用的任务管理 ----------
_tasks: dict[str, SyncTask] = {}
# 检查 + 创建必须原子：旧实现两个并发请求可能同时通过检查（审计 P0-9/⑭）
_start_lock = threading.Lock()


def start_sync(db: Database, cfg: Settings, force_index: bool = False) -> str:
    """启动一个后台同步任务，返回任务 ID。同时间只允许一个。"""
    import uuid

    with _start_lock:
        if _tasks and not all(t.done for t in _tasks.values()):
            raise RuntimeError("已有同步任务在运行，请等它完成")
        tid = uuid.uuid4().hex[:12]
        task = SyncTask(db, cfg, force_index=force_index)
        _tasks[tid] = task

    def _runner() -> None:
        try:
            task.run()
        finally:
            # 完成后保留最近 5 个任务的结果供查询
            done_ids = [k for k, t in _tasks.items() if t.done]
            for k in done_ids[:-5]:
                _tasks.pop(k, None)

    threading.Thread(target=_runner, daemon=True, name=f"sync-{tid}").start()
    return tid


def get_sync(tid: str) -> SyncTask | None:
    """查询任务状态（WebUI 前端轮询用）。"""
    return _tasks.get(tid)


def task_to_dict(t: SyncTask | None) -> dict:
    """把任务序列化成前端可消费的 JSON。"""
    if t is None:
        return {"ok": False, "error": "任务不存在"}
    return {
        "ok": not t.error,
        "stage": t.stage,
        "stage_label": _STAGE_LABEL.get(t.stage, t.stage),
        "done": t.done,
        "error": t.error,
        "detail": t.detail,
        "logs": t.logs[-20:],  # 只给最近 20 条，避免前端被刷爆
        "stages": [{"id": s, "label": _STAGE_LABEL[s]} for s in STAGES],
    }
