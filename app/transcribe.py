"""会议录屏/录音转写：ffmpeg 提取音频 + 中文 ASR + 写作 AI 可读 Markdown。

支持两类模型：
- funasr 传统模型：paraformer / paraformer-zh / SenseVoiceSmall（AutoModel 封装）
- GLM-ASR-Nano（智谱，专为低音量/会议/方言优化）：transformers 原生加载，
  device_map=auto + max_memory 自动分层，GPU 放不下自动 offload 到内存

设计要点：
- 模型懒加载（首次调用才 load，避免启动 WebUI 就占显存）
- 全程优雅降级：缺 ffmpeg / funasr / torch 时返回 None，不中断导入流程
- 长音频由 fsmn-vad 自动切分，控制显存占用
"""
from __future__ import annotations

import gc
import logging
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Callable

log = logging.getLogger("transcribe")

# 转写用全局串行锁：模型单例 + 显存有限，两个录音同时转写会互相卸载
# 模型（历史上表现为 KeyError: 'processor'），必须排队执行。
_TRANSCRIBE_LOCK = threading.Lock()

# 需要转写的媒体扩展名（与 meetings.RECORDING_EXTS 保持一致的子集）
AUDIO_VIDEO_EXTS = {
    ".mp4", ".m4v", ".mkv", ".webm", ".mov", ".avi", ".3gp", ".3gpp",
    ".m4a", ".m4b", ".mp3", ".mp2", ".wav", ".amr", ".aac", ".ogg",
    ".opus", ".flac", ".wma", ".aiff", ".aif", ".caf", ".wemv", ".wem",
}

# FunASR 要求 16kHz 单声道
_SAMPLE_RATE = 16000


class TranscribeUnavailable(Exception):
    """转写依赖缺失。"""


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def asr_available() -> bool:
    """ASR 是否可用（优先 FireRedASR2，其次 FunASR 旧后端）。"""
    try:
        from .asr_firered import available as fr_available
        if fr_available():
            return True
    except Exception:
        pass
    try:
        import funasr  # noqa: F401
        import torch  # noqa: F401
    except ImportError:
        return False
    return True


def extract_audio(src: Path, dst_wav: Path) -> Path:
    """用 ffmpeg 把视频/音频转成 16kHz 单声道 WAV（FunASR 输入格式）。

    缺 ffmpeg 或转换失败时抛 TranscribeUnavailable。
    """
    if not ffmpeg_available():
        raise TranscribeUnavailable("未找到 ffmpeg，无法从视频提取音频")
    dst_wav.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y", "-i", str(src),
        "-vn",                    # 丢弃视频流
        "-ac", "1",               # 单声道
        "-ar", str(_SAMPLE_RATE),  # 16kHz
        "-c:a", "pcm_s16le",      # 16bit PCM
        "-loglevel", "error",
        str(dst_wav),
    ]
    try:
        kwargs = {}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        r = subprocess.run(cmd, capture_output=True, timeout=3600, **kwargs)
    except subprocess.TimeoutExpired:
        raise TranscribeUnavailable(f"音频提取超时：{src.name}")
    if r.returncode != 0 or not dst_wav.is_file():
        err = r.stderr.decode("utf-8", "replace")[:300]
        raise TranscribeUnavailable(f"ffmpeg 提取失败：{err}")
    return dst_wav


class _Transcriber:
    """ASR 模型封装（单例，懒加载）。

    支持两类：
    - funasr 传统模型（paraformer 系 / SenseVoiceSmall）：AutoModel 一条龙
    - GLM-ASR（transformers 原生）：device_map=auto 分层，GPU 限 _glm_gpu_gb
    """

    _GLM_MARKER = "GLM-ASR"

    def __init__(self, device: str = "auto",
                 model_name: str = "paraformer",
                 batch_seconds: int = 120,
                 glm_gpu_gb: float = 4.0):
        self._model = None          # funasr AutoModel 实例
        self._glm = None            # GLM-ASR：{"model","processor","vad"}
        self._device = device
        self._model_name = model_name
        self._batch_seconds = int(batch_seconds) or 120
        self._glm_gpu_gb = float(glm_gpu_gb) or 4.0

    def _is_glm(self) -> bool:
        return self._GLM_MARKER in self._model_name

    @staticmethod
    def _free_vision_vram() -> None:
        """转写前把常驻显存的视觉模型移回内存，给 ASR 腾出显存。

        视觉模型 4bit 约 2.4GB，GLM-ASR 需要约 4GB，8GB 显卡上两者
        共存很容易 OOM。移回内存后下次图片理解会自动（0.6s）搬回来。
        """
        try:
            from .config import load_config
            from .vision import unload_vision
            if unload_vision(load_config()):
                log.info("已让视觉模型移回内存，为转写腾出显存")
        except Exception as e:
            log.debug("让出视觉模型显存失败（可忽略）：%s", e)

    def _resolve_device(self) -> str:
        if self._device != "auto":
            return self._device
        try:
            import torch
            return "cuda:0" if torch.cuda.is_available() else "cpu"
        except ImportError:
            return "cpu"

    def load(self):
        if self._is_glm():
            return self._load_glm()
        if self._model is not None:
            return self._model
        if not asr_available():
            raise TranscribeUnavailable(
                "未安装 FunASR（在运行本项目的 Python 环境里 pip install funasr 后重试）")
        from funasr import AutoModel
        self._free_vision_vram()   # 给 ASR 腾显存（视觉模型先回内存）
        dev = self._resolve_device()
        log.info("加载 FunASR 模型 %s（%s），首次较慢…", self._model_name, dev)
        kw = dict(
            model=self._model_name,  # paraformer（大模型，最准）/ paraformer-zh（seaco 大模型，带热词）/ SenseVoiceSmall（快）
            vad_model="fsmn-vad",    # 语音端点检测：自动切分长音频
            punc_model="ct-punc",    # 标点恢复：让转写结果可读
            device=dev,
        )
        if self._model_name == "SenseVoiceSmall":
            # SenseVoice 自带情感/事件标记、不做标点，标点统一交给 ct-punc
            kw["disable_punc"] = True
        self._model = AutoModel(**kw)
        log.info("FunASR 就绪（%s）", dev)
        return self._model

    def _load_glm(self):
        """transformers 原生加载 GLM-ASR（device_map=auto + max_memory 分层）。

        funasr 的 GLM-ASR 封装有 bug（AutoModel 拿到的是无 generate 的基座类），
        因此直接走 transformers：GPU 最多放 _glm_gpu_gb GB，其余层自动进内存。
        """
        if self._glm is not None:
            return self._glm
        if not asr_available():
            raise TranscribeUnavailable(
                "未安装 FunASR（在运行本项目的 Python 环境里 pip install funasr 后重试）")
        self._free_vision_vram()   # 给 ASR 腾显存（视觉模型先回内存）
        import torch
        from modelscope.hub.snapshot_download import snapshot_download
        from transformers import AutoProcessor
        from transformers.models.glmasr import GlmAsrForConditionalGeneration

        dev = self._resolve_device()
        gpu_gb = self._glm_gpu_gb if dev.startswith("cuda") else 0
        log.info("加载 GLM-ASR %s（device_map=auto，GPU 上限 %sGiB，其余进内存）…",
                 self._model_name, gpu_gb or "0")
        local = snapshot_download(self._model_name)   # 已缓存则秒回
        processor = AutoProcessor.from_pretrained(local, trust_remote_code=True)
        kwargs = dict(
            dtype=torch.bfloat16,
            device_map="auto",
            trust_remote_code=True,
        )
        if gpu_gb:
            kwargs["max_memory"] = {0: f"{int(gpu_gb)}GiB"}
        model = GlmAsrForConditionalGeneration.from_pretrained(local, **kwargs)
        model.eval()

        # 长音频切分仍用 funasr 的 fsmn-vad（很小，放 GPU 无压力）
        from funasr import AutoModel as FnAutoModel
        vad = FnAutoModel(model="fsmn-vad", device=dev)

        self._glm = {"model": model, "processor": processor, "vad": vad}
        log.info("GLM-ASR 就绪（%s）", dev)
        return self._glm

    def transcribe(self, wav: Path, hotwords: str = "",
                   progress: Callable[[str], None] | None = None):
        """转写单个音频，返回 ASR 原始结果列表。"""
        if self._is_glm():
            return self._transcribe_glm(wav, progress)
        m = self.load()
        if progress:
            progress("语音识别中")
        kwargs = {"input": str(wav), "batch_size_s": self._batch_seconds}
        if self._model_name == "SenseVoiceSmall":
            # 指定中文 + 数字规整 + 屏蔽 emo/unk 标签，输出更干净
            kwargs.update(language="zh", use_itn=True, ban_emo_unk=True)
        if hotwords and self._model_name in ("SenseVoiceSmall", "paraformer-zh"):
            # 仅这两类支持热词（SenseVoice 新格式「词 拼音」/ paraformer-zh 纯文本）
            kwargs["hotword"] = hotwords
        elif hotwords:
            # paraformer（speech_paraformer-large）等不支持热词，忽略避免报错
            log.warning("模型 %s 不支持热词，忽略 asr_hotwords", self._model_name)
        try:
            return m.generate(**kwargs)
        except TypeError as e:
            # 个别 FunASR 版本参数名有差异：去掉 SenseVoice 专属参数重试
            if self._model_name == "SenseVoiceSmall" and \
                    any(k in kwargs for k in ("language", "use_itn", "ban_emo_unk")):
                log.warning("SenseVoice 专属参数不被支持（%s），降级重试", e)
                for k in ("language", "use_itn", "ban_emo_unk"):
                    kwargs.pop(k, None)
                return m.generate(**kwargs)
            raise

    def _transcribe_glm(self, wav: Path,
                        progress: Callable[[str], None] | None = None):
        """GLM-ASR 转写：fsmn-vad 切分 → 逐段 numpy 推理 → 拼接。"""
        g = self._load_glm()
        if progress:
            progress("语音识别中")
        import soundfile as sf
        import torch

        # 1) VAD 切分长音频（返回毫秒级语音段）
        if progress:
            progress("VAD 语音端点切分中")
        log.info("GLM-ASR VAD 切分：%s", wav.name)
        vad_res = g["vad"].generate(input=str(wav))
        segs = vad_res[0]["value"] if vad_res else [[0, 0, 1]]
        log.info("VAD 完成：%d 段", len(segs))

        # 2) 读整段音频为 float32 numpy（16k 单声道，ffmpeg 已归一）
        audio, sr = sf.read(str(wav), dtype="float32")
        if audio.ndim > 1:
            audio = audio.mean(axis=1)

        # 3) 逐段推理
        texts: list[str] = []
        for i, seg in enumerate(segs):
            if progress:
                progress(f"语音识别中（第 {i + 1}/{len(segs)} 段）")
            log.info("GLM-ASR 推理 第 %d/%d 段", i + 1, len(segs))
            s = max(0, int(seg[0] * sr // 1000))
            e = min(len(audio), int(seg[1] * sr // 1000))
            if e <= s:
                continue
            chunk = audio[s:e]
            if chunk.size == 0 or float(chunk.max()) - float(chunk.min()) < 1e-6:
                continue  # 纯静音段跳过
            text = self._glm_infer(g, chunk)
            if text:
                texts.append(text)
        log.info("GLM-ASR 全部完成：%d 段 → %d 字", len(segs),
                 sum(len(t) for t in texts))
        return [{"text": " ".join(texts)}]

    def _glm_infer(self, g: dict, audio_np) -> str:
        """对一段 numpy 音频跑 GLM-ASR，返回识别文本。"""
        import torch
        proc = g["processor"]
        messages = [{
            "role": "user",
            "content": [
                {"type": "audio", "url": audio_np},
                {"type": "text", "text": "Please transcribe this audio into text"},
            ],
        }]
        inputs = proc.apply_chat_template(
            messages, tokenize=True, add_generation_prompt=True,
            return_dict=True, return_tensors="pt")
        dev = "cuda:0" if torch.cuda.is_available() else "cpu"
        inputs = {k: (v.to(dev) if hasattr(v, "to") else v)
                  for k, v in inputs.items()}
        for k, v in inputs.items():
            if hasattr(v, "dtype") and v.dtype == torch.float32:
                inputs[k] = v.to(torch.bfloat16)   # 模型 bf16，输入须对齐
        with torch.inference_mode():
            gen = g["model"].generate(**inputs, max_new_tokens=1024,
                                      do_sample=False)
        out = proc.batch_decode(
            gen[:, inputs["input_ids"].shape[1]:],
            skip_special_tokens=True)[0].strip()
        return out

    def unload(self) -> None:
        """卸载模型并清空 CUDA 缓存，归还显存（下次转写会重新加载）。"""
        if self._glm is not None:
            log.info("卸载 GLM-ASR 模型，释放显存/内存")
            for key in ("model", "processor", "vad"):
                try:
                    del self._glm[key]
                except KeyError:
                    pass
            self._glm = None
        if self._model is not None:
            log.info("卸载 FunASR 模型 %s，释放显存", self._model_name)
            del self._model
            self._model = None
        gc.collect()
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except ImportError:
            pass


_transcriber = _Transcriber()


def _clean_asr_text(text: str) -> str:
    """清洗 FunASR 输出：剥掉 SenseVoice 的 <|zh|> 等语言/特殊标签，压缩空白。"""
    text = re.sub(r"<\|[^|]*\|>", "", text or "")
    return re.sub(r"\s+", " ", text).strip()


def configure(device: str = "auto", model_name: str | None = None,
              batch_seconds: int | None = None,
              glm_gpu_gb: float | None = None) -> None:
    """设置推理设备/模型/批长（auto / cuda:0 / cpu）。

    模型为懒加载单例：首次转写后换模型名需重启进程才生效。
    """
    _transcriber._device = device
    if model_name:
        _transcriber._model_name = model_name
    if batch_seconds:
        _transcriber._batch_seconds = int(batch_seconds)
    if glm_gpu_gb:
        _transcriber._glm_gpu_gb = float(glm_gpu_gb)


def unload() -> None:
    """卸载已加载的 FunASR 模型并清空 CUDA 缓存（转写完成后释放显存）。"""
    _transcriber.unload()


def _transcribe_file_unlocked(src: Path, tmp_dir: Path, hotwords: str = "",
                              progress: Callable[[str], None] | None = None,
                              unload_after: bool = False) -> tuple[str | None, str]:
    """把视频/音频转成纯文本。

    返回 (text, reason)：
      text   识别出的文字，失败时为 None
      reason 失败原因（供页面展示，帮助定位问题）；成功时为空串
    """
    if not asr_available():
        reason = "未安装 FunASR（在运行本项目的 Python 环境里 pip install funasr 后重试）"
        log.info("%s，跳过转写：%s", reason, src.name)
        return None, reason
    from . import asr_firered
    if asr_firered.available():
        try:
            res = asr_firered.transcribe(src, tmp_dir, progress, unload_after)
            text = (res.get("text") or "").strip()
            if not text:
                return None, "音频中没有识别出语音（可能是空音频/纯音乐/无人声）"
            return text, ""
        except TranscribeUnavailable as e:
            log.warning("FireRed 转写跳过：%s", e)
            return None, str(e)
        except Exception as e:
            log.warning("FireRed 转写失败（%s）：%s", src.name, e)
            return None, f"{type(e).__name__}: {e}"
    wav = tmp_dir / f"{src.stem}_{src.stat().st_mtime_ns % 100000}.wav"
    try:
        extract_audio(src, wav)
        res = _transcriber.transcribe(wav, hotwords, progress)
        text = _clean_asr_text(res[0].get("text", "") if res else "")
        if not text:
            return None, "音频中没有识别出语音（可能是空音频/纯音乐/无人声）"
        return text, ""
    except TranscribeUnavailable as e:
        log.warning("转写跳过：%s", e)
        return None, str(e)
    except Exception as e:  # 模型/显存等异常不应中断整个导入
        log.warning("转写失败（%s）：%s", src.name, e)
        return None, f"{type(e).__name__}: {e}"
    finally:
        if unload_after:
            _transcriber.unload()   # 转写完成/失败后归还显存
        if wav.is_file():
            try:
                wav.unlink()
            except OSError:
                pass


def transcribe_file(src: Path, tmp_dir: Path, hotwords: str = "",
                    progress: Callable[[str], None] | None = None,
                    unload_after: bool = False) -> tuple[str | None, str]:
    """串行入口：同一时间只跑一个转写任务（模型/显存不能并发）。"""
    with _TRANSCRIBE_LOCK:
        return _transcribe_file_unlocked(src, tmp_dir, hotwords, progress, unload_after)


def _fmt_ts(ms: int) -> str:
    s = max(0, int(ms)) // 1000
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"


def to_markdown(text: str, title: str, src_name: str = "",
                with_timestamps: bool = False,
                segments: list | None = None) -> str:
    """把转写文本整理成 AI 可读 Markdown。

    - 按标点切分成段落，避免一大坨文字
    - 可选带时间戳小标题，便于回溯原始录音
    """
    out: list[str] = [f"# {title}", ""]
    if src_name:
        out.append(f"> 来源：`{src_name}`（自动语音转写）")
        out.append("")

    body = (text or "").strip()
    if not body:
        out.append("_（未识别出语音内容）_")
        return "\n".join(out) + "\n"

    if with_timestamps and segments:
        # 按时间戳分段：把句子归到最近的段落
        out.append("## 转写全文（带时间轴）")
        out.append("")
        for seg in segments:
            ts = _fmt_ts(seg[0])
            out.append(f"**[{ts}]** {seg[2]}")
        out.append("")
    else:
        # 按句号/问号/感叹号切段，长句保留
        parts = re.split(r"(?<=[。！？!?])", body)
        buf = ""
        for p in parts:
            buf += p
            if len(buf) >= 120:
                out.append(buf.strip())
                out.append("")
                buf = ""
        if buf.strip():
            out.append(buf.strip())
            out.append("")
    return "\n".join(out).rstrip() + "\n"
