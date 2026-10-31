"""图片理解引擎：用视觉语言模型（VLM）取代单纯 OCR。

为什么必须换掉纯 OCR
-------------------
RapidOCR 只能给出「文字 + 坐标」，靠几何聚类猜版面，实际效果：
- QQ 聊天截图里 UI 元素、头像、气泡、文字混在一起，输出成一坨乱序文本
- 分栏排版、复杂表格、数学公式基本还原不出来
- 完全无法描述「这张图是什么」（如通知截图、活动照片、课件页面）

VLM（视觉语言模型）能真正做到用户要的两件事：
- **非纯文字图片** → 描述画面内容
- **纯文字图片** → 精确提取文字，表格还原成 Markdown 表格，
  公式转成 LaTeX，分栏按正确阅读顺序输出

三种后端（配置 vision.backend）
----------------------------
- ``local``（默认，推荐）：本地 GPU 推理 Qwen2.5-VL，隐私不出本机。
  8GB 显存跑 3B 模型（bf16）是上限，需要时可调小 max_pixels 省显存。
- ``api``：调用 OpenAI 兼容的视觉模型接口（可上 72B 大模型，效果更好，
  但图片会离开本机）。适合本地显存不足或追求最佳效果时。
- ``off``：关闭 VLM，回退到 RapidOCR（app/ocr_layout.py）。

模型下载（国内建议用 ModelScope 镜像）
----------------------------------
```powershell
# 方式一：ModelScope（国内快）
pip install modelscope
python -c "from modelscope import snapshot_download; snapshot_download('Qwen/Qwen2.5-VL-3B-Instruct', local_dir='models/Qwen2.5-VL-3B-Instruct')"

# 方式二：HuggingFace 镜像
$env:HF_ENDPOINT = "https://hf-mirror.com"
# 然后把 vision.model 设为 Qwen/Qwen2.5-VL-3B-Instruct，首次调用自动下载
```
"""
from __future__ import annotations

import base64
import gc
import logging
import mimetypes
import threading
import time
from pathlib import Path

from .config import Settings
from .ocr_layout import DEGENERATE_NOTE, ocr_degenerate_reason

log = logging.getLogger("vision")

# 模型所在设备的三种状态：
#   cuda  - 整体在显存（4bit 整体加载），可整体搬到内存
#   cpu   - 整体在内存，用时约 0.6s 搬回显存
#   mixed - 分层/跨设备（如 bf16 + device_map=auto），不支持整体搬运，跳过自动卸载
_DEV_CUDA, _DEV_CPU, _DEV_MIXED = "cuda", "cpu", "mixed"

# 需要识别的图片后缀
IMG_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp"}

# 提示词：一次调用同时给出「描述 + 结构化内容」。
# 关键在于明确要求 Markdown 表格 / LaTeX 公式 / 分栏阅读顺序，
# 否则模型会偷懒只给一段话。
_PROMPT_ANALYZE = """请仔细分析这张图片，并按下面两部分输出结果。

【第一部分：图片描述】
用一两句话说明这张图片是什么（如：QQ 群聊界面截图、通知公告、课件/PPT 页面、活动照片、表格截图等），
如果是聊天记录，说明群名/参与者与大致话题；如果是照片，说明画面主体与场景。

【第二部分：内容提取】
- 如果图片主要是文字（通知、文档、表格、公式、聊天记录）：
  完整、准确地提取全部文字内容，要求：
  * **表格必须还原成 Markdown 表格**（| 列 | 列 | 格式）
  * **数学公式必须转成 LaTeX**（行内用 $...$，独立成行用 $$...$$）
  * **多栏/分栏排版按正确阅读顺序输出**（先左栏从上到下，再右栏），
    并可用标题标明栏位
  * 保留原有的层级结构（标题、列表、加粗等 Markdown 语法）
  * 不要遗漏任何文字，包括页眉页脚、水印、小字注释
- 如果图片不是文字为主（照片、截图、图表、界面）：
  描述画面内容与关键信息，图表则说明其趋势与数据要点。
- 如果图片里同时有文字和图形，两部分都给出。

只输出上述两部分内容，不要添加"好的""以下是"之类的客套话。
用 Markdown 格式输出。"""


class VisionEngine:
    """图片理解引擎。模型懒加载（首次调用才载入显存），进程内单例。

    显存策略（vision.idle_unload_minutes > 0 时）：
    - 首次调用：加载模型到显存
    - 批量处理：连续调用期间保持驻留（不反复搬运）
    - 空闲超时：后台看门狗把模型移回内存，显存归零
    - 再次使用：约 0.6s 搬回显存（实测 4bit 模型 CPU<->GPU 往返可靠）
    """

    def __init__(self, cfg: Settings):
        self.cfg = cfg
        self._model = None
        self._processor = None
        self._backend = (cfg.vision or {}).get("backend", "local")
        self._ready = False
        self._err = ""
        self._device = None        # 模型当前所在设备（cuda/cpu/mixed）
        self._last_used = 0.0      # 最后使用时间戳
        self._lock = threading.RLock()   # 模型不可并发使用，且搬运要串行
        self._wd: threading.Thread | None = None   # 空闲看门狗线程

    # ---------- 对外 ----------
    @property
    def backend(self) -> str:
        return self._backend

    def available(self) -> tuple[bool, str]:
        """引擎是否可用，返回 (bool, 原因)。"""
        if self._backend == "off":
            return False, "视觉模型已关闭（vision.backend=off），回退 OCR"
        if self._backend == "api":
            api = (self.cfg.vision or {}).get("api") or {}
            if not (api.get("base_url") and api.get("model")):
                return False, "未配置视觉 API（vision.api）"
            return True, ""
        # local
        if self._ready:
            return True, ""
        return self._load()

    def analyze(self, path: Path) -> tuple[str, str]:
        """分析图片，返回 (markdown 结果, 错误信息)。失败时返回 ("", 原因)。

        退化输出（刷空单元格 / 复读同一张表 / 版式塌陷）统一在这一层拦掉：
        cleaning 写 *_OCR.md 和 knowledge.build_media_ocr 缓存媒体 OCR 都走这个入口，
        把质检放在这里，两条链路（以及以后的调用方）都自动受保护。
        命中时丢弃模型输出、换成一行说明返回，err 留空——否则调用方会拼出
        「视觉模型不可用，已降级 OCR」的错误提示。
        """
        text, err = self._analyze_raw(Path(path))
        if err or not text:
            return text, err
        why = ocr_degenerate_reason(text)
        if why:
            log.warning("视觉模型输出退化，已丢弃（%s）：%s", Path(path).name, why)
            return DEGENERATE_NOTE, ""
        return text, ""

    def _analyze_raw(self, path: Path) -> tuple[str, str]:
        """真正调用后端推理（不含退化质检）。"""
        ok, why = self.available()
        if not ok:
            return "", why
        if self._backend == "api":
            try:
                return self._analyze_api(path), ""
            except Exception as e:
                log.warning("图片理解失败（API）%s: %s", path.name, e)
                return "", f"{type(e).__name__}: {e}"
        # local：推理前确保模型在显存（空闲后可能已移回内存）
        ok, why = self.ensure_gpu()
        if not ok:
            return "", why
        try:
            with self._lock:
                self._last_used = time.time()
                return self._analyze_local(path), ""
        except Exception as e:
            log.warning("图片理解失败 %s: %s", path.name, e)
            return "", f"{type(e).__name__}: {e}"

    # ---------- 显存调度：用时载入 / 空闲移回内存 ----------
    def ensure_gpu(self) -> tuple[bool, str]:
        """确保模型在显存中。不在则加载或从内存搬回。

        返回 (ok, 原因)。批量连续调用时模型保持驻留，不会反复搬运。
        """
        with self._lock:
            if not self._ready:
                ok, why = self._load()
                if not ok:
                    return False, why
            if self._device == _DEV_CUDA:
                self._last_used = time.time()
                return True, ""
            if self._device == _DEV_MIXED:
                # 分层/跨设备模型不做整体搬运（bf16 + device_map=auto）
                self._last_used = time.time()
                return True, ""
            # 从内存搬回显存（实测约 0.6s）
            try:
                import torch
                t0 = time.time()
                self._model.to("cuda")
                self._device = _DEV_CUDA
                self._last_used = time.time()
                log.info("视觉模型已从内存载入显存（%.1fs）", time.time() - t0)
                return True, ""
            except Exception as e:
                # 显存不足时回退：保持模型可用（部分层在 CPU 会慢，但不至于失败）
                self._device = _DEV_MIXED
                log.warning("模型载入显存失败（%s），按分层模式继续", e)
                return True, ""

    def unload_to_cpu(self) -> bool:
        """把模型移回内存，释放显存。返回是否真的释放了。

        空闲超时由看门狗自动调用；也可由外部主动调用（如转写前给 ASR 让显存）。
        """
        with self._lock:
            if self._model is None or self._device != _DEV_CUDA:
                return False
            try:
                import torch
                t0 = time.time()
                self._model.to("cpu")
                self._device = _DEV_CPU
                gc.collect()
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
                log.info("视觉模型已移回内存，显存已释放（%.1fs）",
                         time.time() - t0)
                return True
            except Exception as e:
                log.warning("视觉模型移回内存失败：%s", e)
                return False

    @property
    def device(self) -> str:
        """模型当前所在设备：cuda / cpu / mixed / none。"""
        return self._device or "none"

    def _start_watchdog(self) -> None:
        """启动后台线程：空闲超过 N 分钟自动把模型移回内存。"""
        minutes = float((self.cfg.vision or {}).get("idle_unload_minutes", 5) or 0)
        if minutes <= 0 or self._wd is not None:
            return

        def _loop() -> None:
            while True:
                time.sleep(30)   # 每 30 秒检查一次
                try:
                    if (self._device == _DEV_CUDA and self._last_used
                            and time.time() - self._last_used > minutes * 60):
                        self.unload_to_cpu()
                except Exception:
                    pass   # 看门狗绝不能影响主流程

        self._wd = threading.Thread(target=_loop, daemon=True,
                                    name="vision-idle-watchdog")
        self._wd.start()
        log.info("视觉模型空闲看门狗已启动（空闲 %.0f 分钟后释放显存）", minutes)

    # ---------- 本地 VLM ----------
    def _load(self) -> tuple[bool, str]:
        cfg_v = self.cfg.vision or {}
        model_id = cfg_v.get("model") or "Qwen/Qwen2.5-VL-3B-Instruct"
        # 本地目录必须是绝对路径：相对路径（如 models/Qwen2.5-VL-3B-Instruct）
        # 会被 transformers 当成 HuggingFace repo id 去联网查找，在国内网络下
        # 会卡在连接超时上。这里统一解析到项目根目录。
        local_dir = self.cfg.resolve(model_id)
        if local_dir.exists():
            model_id = str(local_dir)
        try:
            import torch
            from transformers import AutoProcessor, Qwen2_5_VLForConditionalGeneration
        except ImportError as e:
            self._err = f"缺少依赖：{e}"
            return False, self._err

        try:
            log.info("加载视觉模型 %s（首次会从网络下载，约 6GB，请耐心）", model_id)
            dtype = cfg_v.get("torch_dtype", "bfloat16")
            device = cfg_v.get("device", "auto")
            # 与本地向量模型互斥：8GB 显存优先给视觉（OCR 更吃显存），
            # 向量模型很便宜（0.6B / ~1.1GB、加载约 10s），用完再按需重载。
            try:
                from .llm.local_embed import unload_all as _unload_embed
                _unload_embed()
            except Exception:
                log.debug("本地向量模型显存协调跳过", exc_info=True)
            # 默认 4bit：8GB 显卡上 device_map=auto 会把最后几层卸到 CPU，
            # 实测单图推理从 ~29s 劣化到 ~51s，且输出还会被压短。
            # 4bit 后显存 6.0GB→2.4GB、速度 51s→29s，表格/公式/分栏质量无损。
            quant = cfg_v.get("quant", "4bit")   # 4bit | none

            kw = {
                "torch_dtype": getattr(torch, dtype, torch.bfloat16),
                "attn_implementation": cfg_v.get("attn", "sdpa"),
            }
            if quant == "4bit":
                # 4bit 量化：权重 6GB → 约 2GB，能全部装进 8GB 显卡，
                # 避免 device_map=auto 把最后几层卸到 CPU（实测会拖慢 5 倍）。
                # transformers 5.x 用 quantization_config，不再支持 load_in_4bit
                try:
                    from transformers import BitsAndBytesConfig
                    kw["quantization_config"] = BitsAndBytesConfig(
                        load_in_4bit=True,
                        bnb_4bit_compute_dtype=getattr(torch, dtype,
                                                       torch.bfloat16),
                        bnb_4bit_quant_type="nf4",
                    )
                    kw["device_map"] = {"": 0}
                except ImportError:
                    log.warning("未安装 bitsandbytes，跳过 4bit 量化")
                    kw["device_map"] = "auto"
            elif device and device != "auto":
                kw["device_map"] = {"": device}   # 强制全部层放同一设备
            else:
                kw["device_map"] = "auto"

            whole_on_gpu = False   # 是否整体放在单一 GPU（可整体搬运）
            try:
                self._model = Qwen2_5_VLForConditionalGeneration.from_pretrained(
                    model_id, **kw)
                whole_on_gpu = kw.get("device_map") == {"": 0}
            except (torch.cuda.OutOfMemoryError, RuntimeError, ValueError) as e:
                log.warning("按 %s 加载失败（%s），回退 device_map=auto",
                            quant if quant != "none" else device, e)
                torch.cuda.empty_cache()
                kw.pop("quantization_config", None)
                kw["device_map"] = "auto"
                self._model = Qwen2_5_VLForConditionalGeneration.from_pretrained(
                    model_id, **kw)
                whole_on_gpu = False   # 回退后是分层，不做整体搬运
            self._processor = AutoProcessor.from_pretrained(model_id)
            self._ready = True
            # 只有「整体在一个 GPU」时才能安全地整体搬到内存再搬回来；
            # 分层/跨设备模型（bf16 + device_map=auto）跳过自动卸载，
            # 避免搬运时显存峰值翻倍导致 OOM。
            self._device = _DEV_CUDA if whole_on_gpu else _DEV_MIXED
            self._last_used = time.time()
            self._start_watchdog()
            log.info("视觉模型加载完成（设备：%s）", self._device)
            return True, ""
        except Exception as e:
            self._err = f"模型加载失败（{type(e).__name__}: {e}）"
            log.warning(self._err)
            return False, self._err

    def _analyze_local(self, path: Path) -> str:
        import torch
        from qwen_vl_utils import process_vision_info

        max_pixels = int((self.cfg.vision or {}).get("max_pixels", 1280 * 28 * 28))
        messages = [{
            "role": "user",
            "content": [
                {"type": "image", "image": str(path),
                 "max_pixels": max_pixels},
                {"type": "text", "text": _PROMPT_ANALYZE},
            ],
        }]
        text = self._processor.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=True)
        image_inputs, video_inputs = process_vision_info(messages)
        inputs = self._processor(
            text=[text], images=image_inputs, videos=video_inputs,
            padding=True, return_tensors="pt",
        ).to(self._model.device)

        gen_kw = {
            "max_new_tokens": int((self.cfg.vision or {}).get("max_new_tokens", 2048)),
            "do_sample": False,      # 提取任务要确定性输出
        }
        with torch.inference_mode():
            out = self._model.generate(**inputs, **gen_kw)
        trimmed = [o[len(i):] for i, o in zip(inputs.input_ids, out)]
        return self._processor.batch_decode(
            trimmed, skip_special_tokens=True,
            clean_up_tokenization_spaces=False)[0].strip()

    # ---------- 云端 API ----------
    def _analyze_api(self, path: Path) -> str:
        """调用 OpenAI 兼容的视觉模型接口（图片会离开本机）。"""
        from openai import OpenAI

        api = (self.cfg.vision or {}).get("api") or {}
        client = OpenAI(base_url=api.get("base_url"),
                        api_key=api.get("api_key") or "EMPTY",
                        timeout=int(api.get("timeout", 180)))
        mime = mimetypes.guess_type(str(path))[0] or "image/png"
        b64 = base64.b64encode(path.read_bytes()).decode("ascii")
        r = client.chat.completions.create(
            model=api["model"],
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image_url",
                     "image_url": {"url": f"data:{mime};base64,{b64}"}},
                    {"type": "text", "text": _PROMPT_ANALYZE},
                ],
            }],
            max_tokens=int(api.get("max_tokens", 2048)),
            temperature=0,
        )
        return (r.choices[0].message.content or "").strip()


_ENGINE: VisionEngine | None = None


def get_engine(cfg: Settings) -> VisionEngine:
    """进程内共享同一个引擎（模型只加载一次，避免占多份显存）。"""
    global _ENGINE
    if _ENGINE is None or _ENGINE.cfg is not cfg:
        _ENGINE = VisionEngine(cfg)
    return _ENGINE


def reset_engine() -> None:
    """释放引擎（切换后端或模型时用）。"""
    global _ENGINE
    if _ENGINE is not None:
        try:
            del _ENGINE._model
        except Exception:
            pass
        import gc
        gc.collect()
        try:
            import torch
            torch.cuda.empty_cache()
        except Exception:
            pass
    _ENGINE = None


def unload_vision(cfg: Settings) -> bool:
    """主动把视觉模型移回内存，释放显存（供转写等重显存任务前调用）。

    返回是否真的释放了（模型未加载/不在显存时返回 False）。
    """
    eng = _ENGINE
    if eng is None or eng.cfg is not cfg:
        return False
    return eng.unload_to_cpu()


def is_image(path: Path) -> bool:
    return Path(path).suffix.lower() in IMG_EXTS


def analyze_image(cfg: Settings, path: Path) -> tuple[str, str]:
    """理解一张图片。返回 (markdown, 错误信息)。

    退化质检在 ``VisionEngine.analyze`` 里做（这类畸形 Markdown 一旦写进
    knowledge/ 就会被切块进向量库），这里只是薄壳。
    """
    return get_engine(cfg).analyze(Path(path))
