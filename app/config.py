"""配置加载：优先读 config.yaml，缺字段用默认值。"""
from __future__ import annotations

import contextlib
import copy
import logging
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent

DEFAULTS: dict[str, Any] = {
    "storage": {
        "db_path": "data/qqmonitor.db",
        "meeting_dir": "data/meetings",
        "inbox_dir": "data/inbox",
        "digest_dir": "data/digests",
    },
    "knowledge": {
        "dir": "data/knowledge",
        "nt_db": "",
        "key": "",
        "ocr_chat_images": True,   # 聊天同步时把图片文字 OCR 出来内联进 Markdown
        "ocr_formula_wrap": False, # 疑似公式的行是否用 $...$ 包裹（启发式，有误判）
        # 定时自动同步：每隔 N 分钟从本机 QQ 库同步新消息并清洗。0=关闭，下限 1 分钟。
        "auto_sync_minutes": 0,
        # 每次同步（含定时自动同步）后自动增量更新 RAG 向量索引。
        # 索引本身是增量的（只处理变化的文件），有新消息时开销很小。
        # 关掉的话，AI 问答会一直用旧索引，新消息检索不到。
        "auto_index": True,
        # inbox 里的录音/录屏是否在每轮同步时自动转写（ffmpeg + 本地 ASR）。
        # 打开后长录音会明显拖慢当轮同步；只偶尔导入时可保持 False，
        # 手动运行 `python main.py ingest-audio` 即可。
        "audio_inbox_auto": False,
        # 群文件自动入库：扫描消息里的「文件」元素；只有本机已有副本
        # （你在 QQ 里点过下载，消息元素带本地路径）的文件才会被复制进知识库。
        # 不依赖 NapCat，也不会主动联网下载群文件。
        "group_files": {
            "enabled": True,
            "max_mb": 50,          # 超过这个大小的文件只记录、不入库
            "max_per_run": 10,     # 每轮同步最多处理几个文件，避免拖慢同步
            "scan_downloads": True  # 消息里没有本地路径时，按「文件名+大小」去 Downloads 里找
        },
    },
    # 图片理解（视觉语言模型，取代单纯 OCR）
    "vision": {
        "backend": "local",   # local=本地 GPU 推理 | api=云端视觉模型 | off=回退 RapidOCR
        "model": "Qwen/Qwen2.5-VL-3B-Instruct",
        "torch_dtype": "bfloat16",
        "attn": "sdpa",           # sdpa 省显存；flash_attention_2 更快但需额外安装
        "max_pixels": 1003520,    # 1280*28*28，限制单图分辨率以省显存
        "max_new_tokens": 2048,
        # 模型常驻策略：空闲 N 分钟后自动把模型移回内存，释放显存（下次用时
        # 约 0.6 秒搬回 GPU）。0=不自动卸载（常驻显存）。仅 4bit 整体加载时生效。
        "idle_unload_minutes": 5,
        # backend=api 时使用（图片会离开本机，可用 72B 大模型换取更好效果）
        "api": {
            "base_url": "",
            "api_key": "",
            "model": "",
            "timeout": 180,
            "max_tokens": 2048,
        },
    },
    # 拖拽导入ingest：图片自动 OCR；录屏/录音自动语音转写
    "ingest": {
        "ocr": True,
        "asr": True,             # 录屏/录音是否自动转写（依赖 funasr + ffmpeg）
        "asr_device": "auto",    # auto（有显卡用 cuda:0）/ cuda:0 / cpu
        # 中文语音识别模型：
        #   ZhipuAI/GLM-ASR-Nano-2512  智谱新一代，专为低音量/会议/方言优化，
        #                              官方宣称超越 Whisper V3，效果天花板
        #   paraformer                 speech_paraformer-large（最准的传统模型）
        #   paraformer-zh              seaco-paraformer-large（支持热词）
        #   SenseVoiceSmall            达摩院新一代，更快更省显存
        "asr_model": "ZhipuAI/GLM-ASR-Nano-2512",
        # 长音频按 N 秒切一个 batch：越大越快越吃显存，8GB 显卡建议 120 以内
        "asr_batch_seconds": 120,
        # GLM-ASR 在 GPU 上最多放多少 GiB，其余层自动 offload 到内存；
        # 32GB 内存 + 8GB 显存的机器建议 4，显存吃紧可调小到 2-3
        "asr_glm_gpu_gb": 4.0,
        # 热词，空格分隔，提升专有名词识别率（仅 SenseVoiceSmall / paraformer-zh 支持）
        "asr_hotwords": "",
        # 转写完成后自动卸载模型并清空显存（下次转写重新加载约 30 秒）；
        # 若一次批量导入多个音视频想避免反复加载，可改为 false
        "asr_unload_after": True,
    },
    # host 默认 127.0.0.1：只有本机能访问（最安全；本服务默认无登录鉴权，
    # 绑 0.0.0.0 会让同网段任何设备看到聊天记录与知识库）。
    # 需要手机/别的电脑访问时：把 host 改成 0.0.0.0，并务必设置 access_token，
    # 访问时先在 /login 页输入口令（或带 ?token=xxx）。
    "web": {"host": "127.0.0.1", "port": 8088,
            # 访问口令：留空 = 不鉴权（仅限 host=127.0.0.1 时安全）。
            # 设置后所有页面/接口都要先登录，登录态存在 HttpOnly Cookie（30 天）。
            "access_token": "",
            # 上传上限（MB）。会议录屏常达 1-2GB，超限返回 413。
            # 0 = 不限制（不建议，内存会被吃光）。
            "max_upload_mb": 2048},
    # 知识库对外只读 API（给外部机器人 / 自建助手用）。
    # 与网页登录完全独立：这里单独放一个 token，开 enabled 才会生效。
    # 建议只在 Tailscale 内网暴露（tailscale serve），不要直接绑 0.0.0.0。
    "api": {"enabled": False,
            "token": "",                 # 访问口令，留空则 API 一律拒绝
            "rate_limit_per_min": 30,    # 每个来源每分钟最多请求数
            "max_k": 12},                # 单次检索最多返回条数
    # 手机录音上传（自建 Android App；鉴权复用 api.token）。
    # 文件名规范：<分类名>YYYY年MM月DD日.mp3（分类就是下面的白名单，课程名/会议类型都行）
    "recordings": {
        "enabled": True,
        "max_mb": 500,
        "allow_meetings": True,   # 名字里含「会议」的也放行
        "courses": [
            "高等数学", "线性代数", "大学英语", "大学物理",
            "程序设计", "体育", "项目周会", "访谈",
        ],
    },
    "important": {
        "keywords": ["通知", "集合", "缴费", "开会", "报名", "截止", "紧急", "重要", "签到", "交表"],
        "at_all": True,
    },
    # 自动日程抽取（群聊 / 文件）；关闭则只保留录音自带日程。
    "schedule": {
        "enabled": True,
        "chat_enabled": True,
        "chat_max_messages": 6000,   # 单次喂给模型的当天消息上限
        "chat_max_chars": 90000,     # 单次上下文字符上限
        "file_enabled": True,
        "file_max_files": 5,         # 每轮最多处理的文档数（按修改时间倒序）
    },
    # ============ AI 助手（全部走 OpenAI 兼容协议的云端 API）============
    # 三类模型相互独立，可分别指向不同服务商（如对话用 DeepSeek、向量用硅基流动）
    "llm": {
        # 对话 / 知识库问答（RAG）
        "chat": {
            "base_url": "https://api.deepseek.com",
            "api_key": "",
            "model": "deepseek-chat",
            "temperature": 0.3,     # 问答求准，温度压低
            "max_tokens": 4096,     # 答案可能要列多项费用/日期，别卡输出
            "timeout": 120,
        },
        # 文本向量（用于知识库语义检索）
        "embedding": {
            "base_url": "https://api.siliconflow.cn/v1",
            "api_key": "",
            "model": "BAAI/bge-m3",     # provider=api 时是模型名；provider=local 时是项目内模型目录
            "dimensions": 1024,          # 换模型时需同步调整（local 时由模型决定，仅用于校验）
            "batch_size": 16,            # 单次请求/单批推理的文本条数上限
            "timeout": 60,
            "provider": "api",           # api = OpenAI 兼容服务；local = 项目 models/ 内模型（app/llm/local_embed.py）
            "device": "auto",            # local 时：cuda / cpu / auto
            "torch_dtype": "",           # local 时：空 = GPU bfloat16、CPU float32
            "max_length": 2048,          # local 时单条最大 token（超长块会先被切分）
            "max_batch_tokens": 8192,    # local 时单批总 token 预算（防止长文本撑爆显存）
            "idle_unload_minutes": 10,   # local 时 GPU 模型空闲多久自动卸载（0 = 不卸载）
        },
        # 日 / 周 / 月 / 年总结生成
        "summary": {
            "base_url": "https://api.deepseek.com",
            "api_key": "",
            "model": "deepseek-chat",
            "temperature": 0.5,     # 总结略放开，行文更自然
            "max_tokens": 4096,
            "timeout": 180,
        },
        # 知识库检索参数（默认给得宽松：人类文字再多也只是模型输出量的零头，
        # 卡上下文省下的 token 远不值一次答错的代价）
        "rag": {
            "index_chat": True,     # 是否把群聊记录纳入索引（内容会上传云端）
            "top_k": 12,            # 单次检索送入模型的片段数
            "chunk_size": 600,      # 文档分块字数（聊天按天切，不受此项影响）
            "max_context": 60000,   # 固定检索模式下上下文总字符上限
            "max_rounds": 6,        # Agent 最多允许几轮工具调用
            "tool_result_chars": 30000,   # 单次 search_knowledge 返回的最大字符
            "doc_read_chars": 60000,      # read_document 默认读取上限
            "context_budget_chars": 150000,  # 单轮问答里工具返回内容的总字符预算
        },
        # 自定义系统提示词：这里写的内容会**追加**到内置提示词末尾，不会替换它。
        # 想调语气、补充自己的背景或加个人规则时用；留空则只用内置的。
        "system_extra": "",
    },
    "watch_groups": [],
    "watch_buddies": [],   # 好友白名单（QQ号列表），空 = 同步全部好友
    # 手工基线：用户在设置页亲手勾选的群/好友。watch_groups / watch_buddies
    # 是「生效名单」= 手工基线 ∪ 自动规则命中的会话；自动规则只会增删自己
    # 加进去的项，绝不动手工基线（见 app/watchlist.py）。
    "watch_groups_manual": [],
    "watch_buddies_manual": [],
    # 白名单自动规则：QQ 备注名以约定后缀结尾（默认「_._」）的会话自动纳入。
    # 由 sync_from_ntqq 顺路执行（那时已解密好本机库），写回本文件。
    "auto_watch": {
        "enabled": True,
        "by_remark_suffix": [],
        "by_pin": False,          # 置顶会话也自动纳入（本机库 recent_contact_top_table）
        "scope": "both",          # both / groups / friends
        "ignore_groups": [],      # 用户手动移除过的自动项，不再自动加回
        "ignore_buddies": [],
    },
    # 用户本人身份卡：注入 AI 系统提示词，让 AI 知道聊天记录里哪个名字是你，
    # 回答「安排/待办」类问题时能主动关联到你本人的职责与前置准备
    "user": {
        "qq": 0,            # 本机 QQ 号
        "nickname": "",     # QQ 昵称
        "name": "",         # 真实姓名
        "roles": [],        # 固定职责（如 ["班长", "项目负责人"]），会额外提醒
        "context": "",      # 自由文本：你自己的处境（学校/班级/部门/项目），会拼进 AI 系统提示词
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


class Settings:
    def __init__(self, raw: dict, path: Path | None = None):
        self._raw = raw
        self.path: Path = Path(path) if path else BASE_DIR / "config.yaml"
        self.storage: dict = raw["storage"]
        self.knowledge: dict = raw["knowledge"]
        self.vision: dict = raw.get("vision") or {}
        self.ingest: dict = raw["ingest"]
        self.web: dict = raw["web"]
        self.api: dict = raw.get("api") or {}
        self.recordings: dict = raw.get("recordings") or {}
        self.schedule: dict = raw.get("schedule") or {}
        self.important: dict = raw["important"]
        self.watch_groups: list[int] = raw.get("watch_groups") or []
        self.watch_buddies: list[int] = raw.get("watch_buddies") or []
        self.watch_groups_manual: list[int] = raw.get("watch_groups_manual") or []
        self.watch_buddies_manual: list[int] = raw.get("watch_buddies_manual") or []
        self.auto_watch: dict = raw.get("auto_watch") or {}
        self.user: dict = raw.get("user") or {}
        # LLM 配置：config.yaml 提供默认值，data/llm.yaml（WebUI 保存）优先覆盖。
        # 分开放是因为含 api_key，而 data/ 已被 gitignore，避免密钥误入库。
        self.llm_path: Path = (Path(path).parent if path else BASE_DIR) / "data" / "llm.yaml"
        self.llm: dict = _load_llm_override(self.llm_path, raw["llm"])

    def resolve(self, path: str) -> Path:
        """把相对路径解析到项目根目录。"""
        p = Path(path)
        return p if p.is_absolute() else BASE_DIR / p

    def save_llm(self, llm: dict) -> None:
        """保存 LLM 配置到 data/llm.yaml 并更新内存。"""
        self.llm = llm
        if hasattr(self, "_raw"):
            self._raw["llm"] = llm
        save_llm_config(self.llm_path, llm)

    def set_watch_groups(self, groups: list[int]) -> None:
        """更新监听白名单：内存立即生效 + 写回 config.yaml 持久化。"""
        self.watch_groups = list(groups)
        if hasattr(self, "_raw"):
            self._raw["watch_groups"] = list(groups)
        save_watch_groups(self.path, groups)

    def set_watch_buddies(self, buddies: list[int]) -> None:
        """更新好友白名单：内存立即生效 + 写回 config.yaml 持久化。

        语义与群相反：为空 = 一个好友都不同步（私聊隐私更敏感，默认不采集）。
        """
        self.watch_buddies = list(buddies)
        if hasattr(self, "_raw"):
            self._raw["watch_buddies"] = list(buddies)
        save_watch_buddies(self.path, buddies)

    def set_watchlists(self, groups: list[int], buddies: list[int],
                       manual_groups: list[int] | None = None,
                       manual_buddies: list[int] | None = None,
                       auto_watch: dict | None = None) -> bool:
        """一次事务写回生效名单 + 手工基线 + 自动规则。

        内存立即更新（listener/导入/同步都读 cfg.watch_groups），文件用
        「锁 + 临时文件 + os.replace」原子替换，避免与自动同步守护进程
        并发写同一份 config.yaml 时互相截断。

        返回是否真的写盘了：内容没变就不写，防止每轮扫描都刷新 mtime
        导致 Web 端不停地重载配置。
        """
        manual_groups = list(self.watch_groups_manual if manual_groups is None
                             else manual_groups)
        manual_buddies = list(self.watch_buddies_manual if manual_buddies is None
                              else manual_buddies)
        auto_watch = dict(self.auto_watch if auto_watch is None else auto_watch)
        groups = sorted({int(g) for g in groups})
        buddies = sorted({int(b) for b in buddies})
        manual_groups = sorted({int(g) for g in manual_groups})
        manual_buddies = sorted({int(b) for b in manual_buddies})

        self.watch_groups = groups
        self.watch_buddies = buddies
        self.watch_groups_manual = manual_groups
        self.watch_buddies_manual = manual_buddies
        self.auto_watch = auto_watch
        if hasattr(self, "_raw"):
            self._raw.update({"watch_groups": groups, "watch_buddies": buddies,
                              "watch_groups_manual": manual_groups,
                              "watch_buddies_manual": manual_buddies,
                              "auto_watch": auto_watch})
        return save_watch_config(
            self.path, groups=groups, buddies=buddies,
            manual_groups=manual_groups, manual_buddies=manual_buddies,
            auto_watch=auto_watch)

    def save_section(self, section: str, value) -> None:
        """把某个顶层配置段（如 knowledge）写回 config.yaml。

        与 save_watch_groups 一样用正则按行替换，保留文件里所有中文注释。
        路由层改完内存后调用本方法持久化。
        """
        import re
        path = Path(self.path)
        if not path.exists():
            return
        text = path.read_text(encoding="utf-8")
        # 匹配 "  section:" 开头的那一行（顶格两空格缩进），整行替换
        pattern = re.compile(
            r"^(?P<indent>\s*)" + re.escape(section) + r"\s*:",
            re.MULTILINE)
        rendered = _render_yaml_value(value)
        if pattern.search(text):
            text = pattern.sub(
                lambda m: f"{m.group('indent')}{section}: {rendered}",
                text, count=1)
        else:
            text = text.rstrip() + f"\n{section}: {rendered}\n"
        path.write_text(text, encoding="utf-8")
        if hasattr(self, "_raw"):
            self._raw[section] = value


def _render_watch_groups(groups: list[int]) -> str:
    """渲染 watch_groups 的 YAML 行内列表字面量。"""
    if not groups:
        return "[]"
    return "[" + ", ".join(str(int(g)) for g in groups) + "]"


def _render_yaml_value(value) -> str:
    """把一个 Python 值渲染成单行 YAML 字面量（用于写回 config.yaml）。

    只支持标量与「单行列表/字典」：knowledge 段目前就是 bool + 字符串，
    简单渲染即可；复杂嵌套结构请走 yaml.safe_dump 到 data/ 下文件。
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if value is None:
        return '""'
    if isinstance(value, (list, tuple)):
        if not value:
            return "[]"
        return "[" + ", ".join(_render_yaml_value(v) for v in value) + "]"
    if isinstance(value, dict):
        # 顶层配置段（dict）整体写回时用 yaml.safe_dump 多行块，避免把
        # 多个字段挤在一行里导致可读性差
        import io
        buf = io.StringIO()
        yaml.safe_dump(dict(value), buf, allow_unicode=True,
                       sort_keys=False, default_flow_style=False)
        rendered = buf.getvalue().rstrip("\n")
        # 缩进 2 空格，使其落在 config.yaml 的顶层段下
        return "\n" + "\n".join("  " + ln if ln else ln
                                for ln in rendered.splitlines())
    s = str(value)
    if any(c in s for c in ":#{}[]&*!|>'\"%@`"):
        return '"' + s.replace('"', '\\"') + '"'
    return s


def _ensure_config(path: Path) -> None:
    """config.yaml 不存在时从模板复制一份，避免写回失败。"""
    if path.exists():
        return
    example = BASE_DIR / "config.example.yaml"
    if example.exists():
        shutil.copyfile(example, path)
        log.info("config.yaml 不存在，已从 config.example.yaml 复制")
    else:
        path.write_text("\n", encoding="utf-8")


@contextlib.contextmanager
def _config_lock(path: Path, timeout: float = 5.0):
    """跨进程写锁：独占创建 `config.yaml.lock`（Windows 没有 fcntl.flock）。

    自动同步守护进程与 Web 进程会同时改白名单，没有锁时后写的一方可能
    读到半截文件再写回（丢失对方刚保存的字段）。只包住毫秒级的读-改-写。
    超过 30 秒的锁文件视为上次崩溃残留，直接接管，避免永久卡住。
    """
    lock = path.with_name(path.name + ".lock")
    deadline = time.time() + timeout
    fd = None
    while fd is None:
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime > 30:
                    log.warning("发现陈旧的 %s，已删除后重试", lock.name)
                    lock.unlink()
                    continue
            except OSError:
                pass
            if time.time() > deadline:
                log.warning("等待 %s 写锁超时（%.0fs），强制继续", lock.name, timeout)
                break
            time.sleep(0.05)
    try:
        yield
    finally:
        if fd is not None:
            os.close(fd)
            try:
                lock.unlink()
            except OSError:
                pass


def _atomic_write(path: Path, text: str) -> None:
    """先写 .tmp 再 os.replace：断电/被杀时不会留下半截 config.yaml。"""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _upsert_line(text: str, key: str, line: str,
                 anchor: str | None = None) -> str:
    """把 `key: value` 那一行替换掉；没有该键就插到 anchor 行之后或文件末尾。"""
    pattern = re.compile(r"^" + re.escape(key) + r"[ \t]*:.*$", re.MULTILINE)
    if pattern.search(text):
        # 用 lambda 替换避免 repl 中的反斜杠转义问题
        return pattern.sub(lambda _: line, text, count=1)
    if anchor:
        apat = re.compile(r"^" + re.escape(anchor) + r"[ \t]*:.*$", re.MULTILINE)
        if apat.search(text):
            return apat.sub(lambda m: m.group(0) + "\n" + line, text, count=1)
    if not text.endswith("\n"):
        text += "\n"
    return text + line + "\n"


def _upsert_block(text: str, key: str, block: str,
                  anchor: str | None = None) -> str:
    """整段替换 `key:` 及其所有缩进子行（用于 auto_watch 这个嵌套字典）。"""
    pattern = re.compile(
        r"^" + re.escape(key) + r"[ \t]*:[ \t]*\n(?:[ \t]+\S[^\n]*\n)*",
        re.MULTILINE)
    if pattern.search(text):
        return pattern.sub(lambda _: block + "\n", text, count=1)
    if anchor:
        apat = re.compile(r"^" + re.escape(anchor) + r"[ \t]*:.*$", re.MULTILINE)
        if apat.search(text):
            return apat.sub(lambda m: m.group(0) + "\n" + block, text, count=1)
    if not text.endswith("\n"):
        text += "\n"
    return text + "\n" + block + "\n"


_AUTO_WATCH_DEFAULTS = {
    "enabled": True,
    "by_remark_suffix": ["_._"],
    "by_pin": False,
    "scope": "both",
    "ignore_groups": [],
    "ignore_buddies": [],
}


def _render_auto_watch(auto: dict | None) -> str:
    """把 auto_watch 字典渲染成 YAML 块（键顺序固定、列表行内，便于 diff）。"""
    merged = dict(_AUTO_WATCH_DEFAULTS)
    merged.update({k: v for k, v in (auto or {}).items() if v is not None})
    on = "true" if merged.get("enabled") else "false"
    pin = "true" if merged.get("by_pin") else "false"
    suffixes = _render_yaml_value(merged.get("by_remark_suffix") or [])
    ig = _render_yaml_value(merged.get("ignore_groups") or [])
    ib = _render_yaml_value(merged.get("ignore_buddies") or [])
    scope = str(merged.get("scope") or "both")
    return (f"auto_watch:\n"
            f"  enabled: {on}\n"
            f"  by_remark_suffix: {suffixes}\n"
            f"  by_pin: {pin}\n"
            f"  scope: {scope}\n"
            f"  ignore_groups: {ig}\n"
            f"  ignore_buddies: {ib}")


def _watch_line(key: str, items: list[int]) -> str:
    return f"{key}: {_render_watch_groups(items)}"


def save_watch_config(path: str | Path, *,
                      groups: list[int], buddies: list[int],
                      manual_groups: list[int], manual_buddies: list[int],
                      auto_watch: dict | None = None) -> bool:
    """一次写回白名单相关的全部字段（锁 + 原子替换，保留中文注释）。

    必须把这些字段放在同一个临界区里改：如果分开写，自动同步守护进程
    刚写完生效名单、Web 端又写回它内存里的旧值，用户的勾选就丢了。

    返回 True 表示文件内容真的变了；False = 与现有内容一致，免写盘。
    """
    path = Path(path)
    _ensure_config(path)
    with _config_lock(path):
        text = path.read_text(encoding="utf-8")
        new = _upsert_line(text, "watch_groups",
                           _watch_line("watch_groups", groups))
        new = _upsert_line(new, "watch_buddies",
                           _watch_line("watch_buddies", buddies),
                           anchor="watch_groups")
        new = _upsert_line(new, "watch_groups_manual",
                           _watch_line("watch_groups_manual", manual_groups),
                           anchor="watch_buddies")
        new = _upsert_line(new, "watch_buddies_manual",
                           _watch_line("watch_buddies_manual", manual_buddies),
                           anchor="watch_groups_manual")
        new = _upsert_block(new, "auto_watch", _render_auto_watch(auto_watch),
                            anchor="watch_buddies_manual")
        if new == text:
            return False
        _atomic_write(path, new)
    log.info("已保存监听白名单到 %s：群 %s，好友 %s（自动规则结果已合并）",
             path, groups, buddies)
    return True


def save_watch_groups(path: str | Path, groups: list[int]) -> None:
    """只更新 watch_groups（兼容旧调用）：文本行替换以保留中文注释。"""
    path = Path(path)
    _ensure_config(path)
    with _config_lock(path):
        text = path.read_text(encoding="utf-8")
        new = _upsert_line(text, "watch_groups",
                           _watch_line("watch_groups", groups))
        if new != text:
            _atomic_write(path, new)
    log.info("已保存群白名单：%s", groups)


def save_watch_buddies(path: str | Path, buddies: list[int]) -> None:
    """只更新 watch_buddies（兼容旧调用）。"""
    path = Path(path)
    _ensure_config(path)
    with _config_lock(path):
        text = path.read_text(encoding="utf-8")
        new = _upsert_line(text, "watch_buddies",
                           _watch_line("watch_buddies", buddies),
                           anchor="watch_groups")
        if new != text:
            _atomic_write(path, new)
    log.info("已保存好友白名单：%s", buddies)


# data/llm.yaml 一旦保存过，代码里的新默认值就不会自动生效（文件里的值优先）。
# 因此记录一个配置版本号：升级时只把「仍是旧默认值」的字段提升为新默认值，
# 用户手动改过的值保持不动。
_LLM_VERSION_KEY = "config_version"
_LLM_VERSION = 2
_LLM_UPGRADES: tuple[tuple[tuple[str, ...], object, object], ...] = (
    (("chat", "max_tokens"), 2048, 4096),
    (("rag", "top_k"), 6, 12),
    (("rag", "max_context"), 12000, 60000),
)


def _apply_upgrades(merged: dict, override: dict) -> bool:
    """把仍处于旧默认值的字段提升到新默认值。返回是否发生过改动。"""
    changed = False
    for path, old, new in _LLM_UPGRADES:
        node = merged
        for key in path[:-1]:
            node = node.get(key)
            if not isinstance(node, dict):
                node = None
                break
        if not isinstance(node, dict):
            continue
        if node.get(path[-1]) == old:
            node[path[-1]] = new
            changed = True
    return changed


def _load_llm_override(llm_path: Path, defaults: dict) -> dict:
    """用 data/llm.yaml（WebUI 保存的、含 key 的配置）覆盖 config.yaml 默认值。"""
    if not llm_path.exists():
        return copy.deepcopy(defaults)
    try:
        with open(llm_path, "r", encoding="utf-8") as f:
            override = yaml.safe_load(f) or {}
    except Exception as e:
        log.warning("读取 %s 失败（%s），回退到 config.yaml 的 llm 配置",
                    llm_path, e)
        return copy.deepcopy(defaults)
    if not isinstance(override, dict):
        return copy.deepcopy(defaults)
    merged = _deep_merge(copy.deepcopy(defaults), override)
    if int(override.get(_LLM_VERSION_KEY) or 0) < _LLM_VERSION:
        if _apply_upgrades(merged, override):
            log.info("LLM 配置已升级到 v%d", _LLM_VERSION)
        # 新增字段（如 max_rounds）已由 _deep_merge 补齐，一并写回文件
        merged[_LLM_VERSION_KEY] = _LLM_VERSION
        try:
            save_llm_config(llm_path, merged)
        except OSError as e:
            log.warning("写回 LLM 配置失败（%s），本次仅在内存中生效", e)
    return merged


def save_llm_config(llm_path: Path, llm: dict) -> None:
    """把 LLM 配置写入 data/llm.yaml（含 api_key，故不进 git）。"""
    llm_path.parent.mkdir(parents=True, exist_ok=True)
    with open(llm_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(llm, f, allow_unicode=True, sort_keys=False,
                       default_flow_style=False)
    log.info("已保存 LLM 配置到 %s", llm_path)


def load_config(path: str | Path | None = None) -> Settings:
    path = Path(path) if path else BASE_DIR / "config.yaml"
    raw: dict = {}
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
    merged = _deep_merge(DEFAULTS, raw)
    # 老配置没有「手工基线」字段：用当前生效名单初始化它。否则升级后第一次
    # 自动规则运行会把原有的手工勾选当成自动项，用户一取消就被自动移出。
    if "watch_groups_manual" not in raw:
        merged["watch_groups_manual"] = list(merged.get("watch_groups") or [])
    if "watch_buddies_manual" not in raw:
        merged["watch_buddies_manual"] = list(merged.get("watch_buddies") or [])
    return Settings(merged, path)
