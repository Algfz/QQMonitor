"""项目内本地向量模型。

与 vision.py 相同的思路：模型文件放在 `models/` 下，用 transformers 在**本进程**
里加载，不依赖 Ollama / llama.cpp 之类的外部推理服务；懒加载、空闲自动卸载、
显存不足时与视觉模型互斥（先把它移回内存）。

对外接口与 `LLMClient` 的 embedding 用法一致：
    eng = LocalEmbedEngine(cfg)
    vecs = eng.embed(["文本1", "文本2"])   # list[list[float]]，已 L2 归一化
"""

from __future__ import annotations

import gc
import logging
import threading
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("llm.local_embed")

_ENGINES: dict[str, "LocalEmbedEngine"] = {}
_ENGINES_LOCK = threading.Lock()


def _abs_model_path(model: str) -> Path:
    """相对路径按项目根目录解析为绝对路径（transformers 不接受相对目录）。"""
    p = Path(model)
    if p.is_absolute():
        return p
    # app/llm/local_embed.py -> 项目根
    return (Path(__file__).resolve().parents[2] / p).resolve()


class LocalEmbedEngine:
    """进程内本地 embedding 模型（懒加载 + 空闲卸载 + 单例）。"""

    def __init__(self, cfg: Any):
        self.cfg = cfg
        self.conf: dict = (getattr(cfg, "llm", {}) or {}).get("embedding", {}) or {}
        self.model = str(self.conf.get("model") or "models/Qwen3-Embedding-0.6B")
        self.kind = "embedding"
        self._model_path = _abs_model_path(self.model)
        self._model = None
        self._tokenizer = None
        self._device = "none"
        self._lock = threading.RLock()
        self._last_used = 0.0
        self._wd: threading.Thread | None = None
        self._error = ""

    # ---------- 对外属性 ----------
    @property
    def device(self) -> str:
        return self._device

    def available(self) -> tuple[bool, str]:
        if not self._model_path.is_dir():
            return False, (f"本地模型目录不存在：{self._model_path}\n"
                           f"请先下载：python -m app.llm.local_embed --download")
        return True, ""

    # ---------- 生命周期 ----------
    def _target_device(self) -> str:
        dev = str(self.conf.get("device") or "auto").lower()
        if dev in ("cpu", "cuda"):
            return dev
        try:
            import torch
            return "cuda" if torch.cuda.is_available() else "cpu"
        except Exception:
            return "cpu"

    def _ensure(self) -> None:
        if self._model is not None:
            return
        with self._lock:
            if self._model is not None:
                return
            self._load()

    def _load(self) -> None:
        ok, why = self.available()
        if not ok:
            raise RuntimeError(why)

        import torch
        from transformers import AutoModel, AutoTokenizer

        device = self._target_device()
        if device == "cuda" and not torch.cuda.is_available():
            log.warning("CUDA 不可用，本地向量模型改用 CPU")
            device = "cpu"
        # 与视觉模型互斥：8GB 显存装不下两个模型，加载前先把视觉模型移回内存
        if device == "cuda":
            try:
                from ..vision import get_engine as _vision_engine
                veng = _vision_engine(self.cfg)
                if veng.device == "cuda":
                    log.info("本地向量模型加载前，先把视觉模型移回内存以腾出显存")
                    veng.unload_to_cpu()
            except Exception:
                log.debug("视觉模型显存协调跳过", exc_info=True)

        dtype_name = str(self.conf.get("torch_dtype") or
                         ("bfloat16" if device == "cuda" else "float32"))
        dtype = getattr(torch, dtype_name, torch.float32)
        if device == "cpu" and dtype_name in ("float16", "bfloat16"):
            dtype = torch.float32
        max_length = int(self.conf.get("max_length") or 2048)

        t0 = time.time()
        tok = AutoTokenizer.from_pretrained(
            str(self._model_path), padding_side="left", trust_remote_code=True)
        # transformers 5.x 把 torch_dtype 改名为 dtype，这里按版本自动选择，避免弃用警告
        try:
            from transformers import __version__ as _tf_version
            import re as _re
            _major = int(_re.match(r"(\d+)", _tf_version).group(1))
        except Exception:
            _major = 4
        dtype_key = "dtype" if _major >= 5 else "torch_dtype"
        kw: dict = {dtype_key: dtype, "trust_remote_code": True}
        try:
            kw["attn_implementation"] = "sdpa"
            model = AutoModel.from_pretrained(str(self._model_path), **kw)
        except Exception as e:  # 少数仓库只提供 CausalLM 结构
            log.warning("AutoModel 加载失败（%s），回退 AutoModelForCausalLM", e)
            from transformers import AutoModelForCausalLM
            kw.pop("attn_implementation", None)
            model = AutoModelForCausalLM.from_pretrained(str(self._model_path), **kw)
        model.eval()
        model.to(device)
        if device == "cuda":
            # 长文本注意力是主要瓶颈：优先用 flash / memory-efficient 内核，
            # 数学实现会按 O(n²) 分配，8GB 卡上几千 token 就开始走共享内存。
            try:
                torch.backends.cuda.enable_flash_sdp(True)
                torch.backends.cuda.enable_mem_efficient_sdp(True)
            except Exception:
                log.debug("SDPA 后端开关设置失败", exc_info=True)
        if hasattr(tok, "model_max_length") and tok.model_max_length:
            # 个别 tokenizer 的 model_max_length 是个巨大占位值，取配置上限
            max_length = min(max_length, int(tok.model_max_length))
        self._tokenizer = tok
        self._model = model
        self._device = device
        self._max_length = max(64, max_length)
        self._last_used = time.time()
        self._start_watchdog()
        log.info("本地向量模型已加载：%s（device=%s，dtype=%s，%.1fs）",
                 self._model_path.name, device, dtype_name, time.time() - t0)

    def unload(self, why: str = "空闲") -> None:
        """彻底释放模型（显存 + 内存），下次调用再重新加载。"""
        with self._lock:
            if self._model is None:
                return
            log.info("卸载本地向量模型（%s）", why)
            self._model = None
            self._tokenizer = None
            self._device = "none"
            gc.collect()
            try:
                import torch
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
            except Exception:
                pass

    def _start_watchdog(self) -> None:
        minutes = float(self.conf.get("idle_unload_minutes", 10) or 0)
        if minutes <= 0 or self._wd is not None:
            return

        def loop() -> None:
            while True:
                time.sleep(60)
                if (self._device == "cuda" and self._model is not None
                        and (time.time() - self._last_used) > minutes * 60):
                    try:
                        self.unload("空闲超时")
                    except Exception:
                        log.debug("空闲卸载失败", exc_info=True)

        self._wd = threading.Thread(target=loop, name="local-embed-wd", daemon=True)
        self._wd.start()

    # ---------- 推理 ----------
    def embed(self, texts: list[str]) -> list[list[float]]:
        from .client import LLMError

        if not texts:
            return []
        texts = [str(t) if t is not None else "" for t in texts]
        try:
            self._ensure()
        except RuntimeError as e:
            raise LLMError(f"文本向量：{e}") from e
        except Exception as e:
            raise LLMError(f"文本向量：本地模型加载失败（{type(e).__name__}: {e}）") from e

        import torch

        max_batch = max(1, int(self.conf.get("batch_size") or 16))
        # 按 token 预算组批：长文本（几千字）单独成批，避免「16 条 × 8k token」
        # 把注意力矩阵撑爆显存、退化成共享内存分页（实测会慢 20 倍以上）。
        token_budget = max(1024, int(self.conf.get("max_batch_tokens") or 8192))

        # 先按长度排序，批内长度接近、padding 浪费小；输出再按原顺序还原。
        lens = []
        for t in texts:
            try:
                lens.append(len(self._tokenizer(t, truncation=False)["input_ids"]))
            except Exception:
                lens.append(max(1, len(t)))
        order = sorted(range(len(texts)), key=lambda i: lens[i])
        batches: list[list[int]] = []
        cur: list[int] = []
        cur_tokens = 0
        for i in order:
            n = min(max(lens[i], 1), self._max_length)
            if cur and (len(cur) >= max_batch or cur_tokens + n > token_budget):
                batches.append(cur)
                cur, cur_tokens = [], 0
            cur.append(i)
            cur_tokens += n
        if cur:
            batches.append(cur)

        out: list[list[float] | None] = [None] * len(texts)
        with self._lock:
            self._last_used = time.time()
            for idxs in batches:
                chunk = [texts[i] for i in idxs]
                try:
                    enc = self._tokenizer(chunk, padding=True, truncation=True,
                                          max_length=self._max_length,
                                          return_tensors="pt")
                    enc = {k: v.to(self._device) for k, v in enc.items()}
                    with torch.inference_mode():
                        hidden = self._model(**enc).last_hidden_state
                        # 行长按 attention_mask 求和定位最后一个 token（左 padding）
                        pos = enc["attention_mask"].sum(dim=1) - 1
                        pooled = hidden[torch.arange(hidden.size(0)), pos]
                        # 先升到 float32 再归一化，bf16 下 L2 误差可达 1e-3 量级
                        pooled = torch.nn.functional.normalize(pooled.float(), p=2, dim=1)
                    for i, vec in zip(idxs, pooled.float().cpu().tolist()):
                        out[i] = vec
                except (torch.cuda.OutOfMemoryError, RuntimeError) as e:
                    if "out of memory" in str(e).lower():
                        torch.cuda.empty_cache()
                        raise LLMError("文本向量：显存不足（可把 llm.embedding.device 改成 cpu）") from e
                    raise LLMError(f"文本向量：本地推理失败（{e}）") from e
            self._last_used = time.time()
        return [v or [] for v in out]

    # ---------- 与 LLMClient 对齐的辅助接口 ----------
    def _require(self) -> None:
        """与 LLMClient._require 对齐：配置/模型缺失时抛 LLMError。"""
        from .client import LLMError

        ok, why = self.available()
        if not ok:
            raise LLMError(f"文本向量：{why}")

    def test(self) -> dict:
        try:
            vecs = self.embed(["连接测试", "第二条测试文本"])
            if not vecs or any(not v for v in vecs):
                return {"ok": False, "message": "文本向量：返回向量为空。"}
            dims = {len(v) for v in vecs}
            if len(dims) != 1:
                return {"ok": False,
                        "message": f"文本向量：同一批向量维度不一致 {sorted(dims)}。"}
            return {"ok": True,
                    "message": f"本地向量：加载成功，维度 {dims.pop()}，设备 {self._device}。"}
        except Exception as e:
            return {"ok": False, "message": str(e)}


def get_engine(cfg: Any) -> LocalEmbedEngine:
    """同一进程内同一模型只保留一个实例（含空闲卸载看门狗）。

    注意：Settings 对象会被 reload 重建，所以不能按对象身份判断复用；
    模型/设备/精度相同就复用同一个引擎，只把 cfg 指针更新到最新。
    """
    conf = (getattr(cfg, "llm", {}) or {}).get("embedding", {}) or {}
    key = f"{conf.get('model')}|{conf.get('device')}|{conf.get('torch_dtype')}"
    with _ENGINES_LOCK:
        eng = _ENGINES.get(key)
        if eng is None:
            eng = LocalEmbedEngine(cfg)
            _ENGINES[key] = eng
        else:
            eng.cfg = cfg
            if eng._model is None:
                eng.conf = conf
        return eng


def unload_all() -> None:
    with _ENGINES_LOCK:
        engines = list(_ENGINES.values())
    for eng in engines:
        try:
            eng.unload("主动释放")
        except Exception:
            pass


def download(model: str = "models/Qwen3-Embedding-0.6B",
             repo: str = "Qwen/Qwen3-Embedding-0.6B") -> str:
    """把模型下载到项目 models/ 下（走魔搭 ModelScope）。"""
    from modelscope import snapshot_download

    target = _abs_model_path(model)
    target.parent.mkdir(parents=True, exist_ok=True)
    path = snapshot_download(repo, local_dir=str(target))
    print(f"已下载到：{path}")
    return str(path)


if __name__ == "__main__":  # pragma: no cover - 手动工具
    import argparse

    ap = argparse.ArgumentParser(description="本地向量模型工具")
    ap.add_argument("--download", nargs="?", const="models/Qwen3-Embedding-0.6B",
                    help="下载模型到 models/（可传目标目录）")
    args = ap.parse_args()
    if args.download:
        download(args.download)
    else:
        print("用法：python -m app.llm.local_embed --download")
