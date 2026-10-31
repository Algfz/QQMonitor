"""本地录音转写（会议 / 课堂 / 访谈）：FireRedASR2-AED + FireRedVAD/Punc + CAM++ 说话人聚类。

对外只暴露 ``transcribe(src, tmp_dir, progress=None)``，返回结构化结果：
{
  "text": 全文,
  "sentences": [{start_ms, end_ms, text, speaker, confidence, tier}],
  "speakers": ["主讲", "发言者A"],
  "stats": {high, mid, low},
}
"""
from __future__ import annotations

import json
import logging
import re
import sys
import threading
import time
from pathlib import Path
from typing import Callable

import numpy as np

from .transcribe import extract_audio, TranscribeUnavailable

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]
MODELS = ROOT / "models"
FR_ROOT = MODELS / "FireRedASR2S"

_LOCK = threading.Lock()
_system = None
_campplus = None

# 已观察到的同音/近音误识别（只改确定的词，不改公式）
FIX = [
    ("单叶双重叠法", "单叶双曲面"),
    ("双重凹陷", "单叶双曲面"),
    ("单页双曲面", "单叶双曲面"),
    ("双曲抛物线", "双曲抛物面"),
    ("双数抛物线", "双曲抛物面"),
    ("双曲剖面", "双曲抛物面"),
    ("双曲刨面", "双曲抛物面"),
    ("双曲凹面", "双曲抛物面"),
    ("中方无面", "双曲抛物面"),
    ("持物线", "直母线"),
    ("子母线", "直母线"),
    ("文线", "母线"),
    ("弩线", "母线"),
    ("往里补", "方程组"),
    ("方程租", "方程组"),
    ("莱姆达", "λ"),
    ("兰姆达", "λ"),
    ("拉姆达", "λ"),
    ("阿尔法", "α"),
    ("阿发", "α"),
    ("贝塔", "β"),
    ("伽马", "γ"),
    ("依次是以三元一次方程", "它也是一个三元一次方程"),
]

HIGH = 0.80
MID = 0.65


def available() -> bool:
    need = [
        FR_ROOT / "fireredasr2s",
        MODELS / "FireRedASR2-AED",
        MODELS / "FireRedVAD" / "VAD",
        MODELS / "FireRedPunc",
        MODELS / "campplus",
    ]
    return all(p.exists() for p in need)


def _get_system():
    global _system
    if _system is not None:
        return _system
    if not available():
        raise TranscribeUnavailable("FireRed 模型不完整，请检查 models/FireRedASR2*")
    if str(FR_ROOT) not in sys.path:
        sys.path.insert(0, str(FR_ROOT))
    from fireredasr2s import FireRedAsr2System, FireRedAsr2SystemConfig
    from fireredasr2s.fireredasr2 import FireRedAsr2Config
    from fireredasr2s.fireredpunc import FireRedPuncConfig
    from fireredasr2s.fireredvad import FireRedVadConfig

    cfg = FireRedAsr2SystemConfig(
        vad_model_dir=str(MODELS / "FireRedVAD" / "VAD"),
        enable_lid=False,
        asr_model_dir=str(MODELS / "FireRedASR2-AED"),
        punc_model_dir=str(MODELS / "FireRedPunc"),
        asr_config=FireRedAsr2Config(use_gpu=True, use_half=True, beam_size=3,
                                     return_timestamp=False),
        vad_config=FireRedVadConfig(use_gpu=False),
        punc_config=FireRedPuncConfig(use_gpu=False),
        asr_batch_size=1,
        punc_batch_size=1,
    )
    t0 = time.time()
    _system = FireRedAsr2System(cfg)
    log.info("FireRedASR2 加载完成 %.1fs", time.time() - t0)
    return _system


def _get_campplus():
    global _campplus
    if _campplus is not None:
        return _campplus
    from funasr import AutoModel

    t0 = time.time()
    _campplus = AutoModel(model=str(MODELS / "campplus"), device="cuda:0",
                          disable_update=True)
    log.info("CAM++ 加载完成 %.1fs", time.time() - t0)
    return _campplus


def unload() -> None:
    global _system, _campplus
    _system = None
    _campplus = None
    try:
        import torch
        torch.cuda.empty_cache()
    except Exception:  # noqa: BLE001
        pass


def fmt_ts(ms: int) -> str:
    s = max(0, int(ms)) // 1000
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"


def _fix(text: str) -> str:
    for a, b in FIX:
        text = text.replace(a, b)
    return text


def _merge_chunks(sentences: list[dict]) -> list[dict]:
    chunks: list[dict] = []
    cur = None
    for s in sentences:
        if cur is None:
            cur = {"start_ms": s["start_ms"], "end_ms": s["end_ms"], "items": [s]}
            continue
        gap = s["start_ms"] - cur["end_ms"]
        span = s["end_ms"] - cur["start_ms"]
        if gap <= 1800 and span <= 12000:
            cur["end_ms"] = s["end_ms"]
            cur["items"].append(s)
        else:
            chunks.append(cur)
            cur = {"start_ms": s["start_ms"], "end_ms": s["end_ms"], "items": [s]}
    if cur:
        chunks.append(cur)
    # 短碎片并入前块，避免声纹样本过短
    merged: list[dict] = []
    for c in chunks:
        if (merged and c["end_ms"] - c["start_ms"] < 1500
                and c["end_ms"] - merged[-1]["start_ms"] <= 14000):
            merged[-1]["end_ms"] = c["end_ms"]
            merged[-1]["items"].extend(c["items"])
        else:
            merged.append(c)
    return merged


def _diarize(wav: Path, sentences: list[dict], tmp_dir: Path) -> tuple[list[dict], list[str]]:
    """给每句分配说话人；失败时全部标“主讲”。"""
    try:
        import soundfile as sf
        from sklearn.cluster import AgglomerativeClustering
        from sklearn.metrics import silhouette_score

        audio, sr = sf.read(wav, dtype="float32")
        if audio.ndim > 1:
            audio = audio.mean(axis=1)
        chunks = _merge_chunks(sentences)
        if len(chunks) < 4:
            return sentences, ["老师"]
        cam = _get_campplus()
        emb = []
        paths = []
        tmp_dir = Path(tmp_dir)
        tmp_dir.mkdir(parents=True, exist_ok=True)
        for i, c in enumerate(chunks):
            a = max(0, int(c["start_ms"] * sr / 1000))
            b = min(len(audio), int(c["end_ms"] * sr / 1000))
            if b - a < int(0.3 * sr):
                emb.append(None)
                paths.append(None)
                continue
            p = tmp_dir / f"spk_{i:05d}.wav"
            sf.write(p, audio[a:b], sr)
            paths.append(p)
            r = cam.generate(input=str(p))
            emb.append(r[0]["spk_embedding"].detach().cpu().numpy().reshape(-1))

        idx = [i for i, e in enumerate(emb) if e is not None]
        if len(idx) < 4:
            return sentences, ["老师"]
        E = np.stack([emb[i] for i in idx])
        E = E / (np.linalg.norm(E, axis=1, keepdims=True) + 1e-9)
        D = 1.0 - E @ E.T
        np.fill_diagonal(D, 0.0)

        best_k, best_score, best_labels = 1, 0.0, np.zeros(len(E), dtype=int)
        for k in range(2, min(4, len(E) - 1) + 1):
            labels = AgglomerativeClustering(n_clusters=k, metric="cosine",
                                             linkage="average").fit_predict(E)
            if len(set(labels)) < 2:
                continue
            try:
                score = silhouette_score(D, labels, metric="precomputed")
            except ValueError:
                continue
            counts = np.bincount(labels)
            if (score > 0.30 and counts.min() / counts.sum() >= 0.01
                    and score > best_score):
                best_k, best_score, best_labels = k, score, labels

        labels = [-1] * len(chunks)
        for pos, i in enumerate(idx):
            labels[i] = int(best_labels[pos])
        totals = np.zeros(max(best_k, 1))
        for c, lab in zip(chunks, labels):
            if lab >= 0:
                totals[lab] += c["end_ms"] - c["start_ms"]
        order = np.argsort(-totals)
        names = {}
        for rank, lab in enumerate(order):
            names[int(lab)] = "主讲" if rank == 0 else f"发言者{chr(65 + rank - 1)}"

        by_sentence = {}
        for c, lab in zip(chunks, labels):
            name = names.get(lab, "老师")
            for s in c["items"]:
                by_sentence[id(s)] = name
        for s in sentences:
            s["speaker"] = by_sentence.get(id(s), "老师")
        return sentences, sorted(set(s.get("speaker") for s in sentences))
    except Exception as e:  # noqa: BLE001
        log.warning("说话人聚类失败（转写继续）：%s", e)
        for s in sentences:
            s.setdefault("speaker", "老师")
        return sentences, ["老师"]


def transcribe(src: Path, tmp_dir: Path,
               progress: Callable[[str], None] | None = None,
               unload_after: bool = False) -> dict:
    """串行入口：模型/显存一次只能跑一个任务。"""
    with _LOCK:
        return _transcribe(src, tmp_dir, progress, unload_after)


def _transcribe(src: Path, tmp_dir: Path,
                progress: Callable[[str], None] | None = None,
                unload_after: bool = False) -> dict:
    """转写单个音频/视频，返回结构化结果；失败抛 TranscribeUnavailable。"""
    if not available():
        raise TranscribeUnavailable("FireRed 模型不完整（models/FireRedASR2*）")
    src = Path(src)
    tmp_dir = Path(tmp_dir)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    wav = tmp_dir / f"{src.stem}_{src.stat().st_mtime_ns % 100000}.wav"
    t0 = time.time()
    try:
        if progress:
            progress("提取音频…")
        extract_audio(src, wav)
        system = _get_system()
        if progress:
            progress("语音识别中…")
        res = system.process(str(wav), src.stem[:30])
        sentences = []
        for s in res.get("sentences") or []:
            text = _fix(str(s.get("text") or "").strip())
            if not text:
                continue
            conf = float(s.get("asr_confidence") or 0)
            sentences.append({
                "start_ms": int(s.get("start_ms") or 0),
                "end_ms": int(s.get("end_ms") or 0),
                "text": text,
                "speaker": "老师",
                "confidence": conf,
                "tier": "high" if conf >= HIGH else ("mid" if conf >= MID else "low"),
            })
        if not sentences:
            raise TranscribeUnavailable("音频中没有识别出语音")
        if progress:
            progress("说话人聚类中…")
        sentences, speakers = _diarize(wav, sentences, tmp_dir / "spk")
        stats = {
            "high": sum(1 for s in sentences if s["tier"] == "high"),
            "mid": sum(1 for s in sentences if s["tier"] == "mid"),
            "low": sum(1 for s in sentences if s["tier"] == "low"),
            "chars": sum(len(s["text"]) for s in sentences),
        }
        return {
            "text": "".join(s["text"] for s in sentences),
            "sentences": sentences,
            "speakers": speakers,
            "stats": stats,
            "elapsed_s": round(time.time() - t0, 1),
            "model": "FireRedASR2-AED + CAM++",
        }
    finally:
        try:
            wav.unlink(missing_ok=True)
        except OSError:
            pass
        if unload_after:
            unload()
