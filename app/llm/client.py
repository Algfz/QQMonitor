"""LLM 客户端：OpenAI 兼容协议封装（对话 / 向量 / 连通性测试）。

所有模型服务商（DeepSeek、硅基流动、OpenAI、月之暗面…）均走同一套协议，
因此这里只需一个通用客户端，靠 base_url 区分。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Iterable

log = logging.getLogger("llm.client")

# 三种用途，对应 Settings.llm 下的子配置
KINDS = ("chat", "embedding", "summary")

KIND_LABEL = {
    "chat": "对话 / 问答",
    "embedding": "文本向量",
    "summary": "总结生成",
}


class LLMError(Exception):
    """LLM 调用失败。message 面向用户，可直接显示在页面上。"""


def _friendly_error(e: Exception, kind: str) -> str:
    """把 SDK 异常翻译成用户能看懂的中文提示。"""
    label = KIND_LABEL.get(kind, kind)
    name = type(e).__name__
    raw = str(e)

    import openai
    if isinstance(e, openai.AuthenticationError):
        return f"{label}：API Key 无效或已过期（401）。请检查设置页填写的 Key。"
    if isinstance(e, openai.RateLimitError):
        return f"{label}：请求过于频繁或额度已用尽（429）。请稍后再试或检查账户余额。"
    if isinstance(e, openai.APITimeoutError):
        return f"{label}：请求超时。可适当调大该模型的 timeout，或稍后重试。"
    if isinstance(e, openai.APIConnectionError):
        return (f"{label}：无法连接到服务器。请检查网络，"
                f"以及 base_url 是否正确（常见问题：多写了 /v1 或路径）。")
    if isinstance(e, openai.NotFoundError):
        return (f"{label}：接口或模型不存在（404）。请检查 base_url 与模型名是否匹配"
                f"（换服务商时这两项要一起改）。")
    if isinstance(e, openai.BadRequestError):
        # 常见：模型名写错、dimensions 与模型不符、上下文超长
        return f"{label}：请求被拒绝（400）。{raw[:200]}"
    if isinstance(e, openai.APIError):
        return f"{label}：服务端错误。{raw[:200]}"
    return f"{label}：调用失败（{name}）。{raw[:200]}"


class LLMClient:
    """单个模型配置的客户端。轻量对象，按需创建。"""

    def __init__(self, kind: str, conf: dict):
        self.kind = kind
        self.conf = conf or {}
        self.base_url = (self.conf.get("base_url") or "").strip()
        self.api_key = (self.conf.get("api_key") or "").strip()
        self.model = (self.conf.get("model") or "").strip()
        self.timeout = int(self.conf.get("timeout") or 120)

    def _require(self) -> None:
        """调用前校验必要配置，缺什么直接说清楚。"""
        label = KIND_LABEL.get(self.kind, self.kind)
        if not self.base_url:
            raise LLMError(f"{label}：未配置 base_url，请在设置页填写服务商接口地址。")
        if not self.api_key:
            raise LLMError(f"{label}：未配置 API Key，请在设置页填写。")
        if not self.model:
            raise LLMError(f"{label}：未配置模型名，请在设置页填写。")

    def _sdk(self):
        import openai
        return openai.OpenAI(
            base_url=self.base_url,
            api_key=self.api_key,
            timeout=self.timeout,
        )

    # ---------- 对话 ----------
    def chat(self, messages: list[dict], temperature: float | None = None,
             max_tokens: int | None = None) -> str:
        """发送对话请求，返回回复文本。失败抛 LLMError（含中文说明）。"""
        self._require()
        temp = self.conf.get("temperature") if temperature is None else temperature
        mtok = self.conf.get("max_tokens") if max_tokens is None else max_tokens
        try:
            r = self._sdk().chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=float(temp) if temp is not None else 0.3,
                max_tokens=int(mtok) if mtok else 4096,
            )
        except Exception as e:
            raise LLMError(_friendly_error(e, self.kind)) from e
        if not r.choices:
            raise LLMError(f"{KIND_LABEL.get(self.kind)}：模型返回空结果。")
        return (r.choices[0].message.content or "").strip()

    # ---------- 流式对话 ----------
    def chat_stream(self, messages: list[dict], temperature: float | None = None,
                    max_tokens: int | None = None):
        """流式对话，逐 chunk 产出文本增量。

        返回生成器，每次 yield 一段新增文本。调用方负责迭代与拼接。
        """
        self._require()
        temp = self.conf.get("temperature") if temperature is None else temperature
        mtok = self.conf.get("max_tokens") if max_tokens is None else max_tokens
        try:
            stream = self._sdk().chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=float(temp) if temp is not None else 0.3,
                max_tokens=int(mtok) if mtok else 2048,
                stream=True,
            )
        except Exception as e:
            raise LLMError(_friendly_error(e, self.kind)) from e
        for chunk in stream:
            try:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                piece = getattr(delta, "content", None)
            except Exception as e:  # 单个 chunk 解析异常不应中断整条流
                log.warning("流式 chunk 解析失败：%s", e)
                continue
            if piece:
                yield piece

    # ---------- 流式对话（含工具调用） ----------
    def chat_stream_events(self, messages: list[dict], tools: list[dict] | None = None,
                           temperature: float | None = None,
                           max_tokens: int | None = None):
        """流式对话，产出事件字典（供 Agent 循环消费）：

            {"type": "delta", "text": "..."}                    文本增量
            {"type": "tool_calls", "calls": [{"id","name","arguments"}]}  工具调用请求

        工具调用的 id / name / arguments 在流里是分片下发的，这里按 index 累积，
        到流结束后一次性产出（调用方需要完整的参数才能执行）。
        """
        self._require()
        temp = self.conf.get("temperature") if temperature is None else temperature
        mtok = self.conf.get("max_tokens") if max_tokens is None else max_tokens
        kw: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": float(temp) if temp is not None else 0.3,
            "max_tokens": int(mtok) if mtok else 4096,
            "stream": True,
        }
        if tools:
            kw["tools"] = tools
        try:
            stream = self._sdk().chat.completions.create(**kw)
        except Exception as e:
            raise LLMError(_friendly_error(e, self.kind)) from e

        tc_buf: dict[int, dict] = {}
        for chunk in stream:
            try:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                piece = getattr(delta, "content", None)
                if piece:
                    yield {"type": "delta", "text": piece}
                for tc in getattr(delta, "tool_calls", None) or []:
                    idx = getattr(tc, "index", 0) or 0
                    item = tc_buf.setdefault(
                        idx, {"id": "", "name": "", "arguments": ""})
                    if getattr(tc, "id", None):
                        item["id"] = tc.id
                    fn = getattr(tc, "function", None)
                    if fn is not None:
                        if getattr(fn, "name", None):
                            item["name"] += fn.name
                        if getattr(fn, "arguments", None):
                            item["arguments"] += fn.arguments
            except Exception as e:  # 单个 chunk 解析异常不应中断整条流
                log.warning("流式 chunk 解析失败：%s", e)
                continue

        if tc_buf:
            calls = []
            for idx in sorted(tc_buf):
                item = tc_buf[idx]
                try:
                    args = json.loads(item["arguments"] or "{}")
                except ValueError:
                    # 参数 JSON 被截断/非法：给空对象，让工具自己报错并提示模型重试
                    log.warning("工具参数解析失败：%s", item["arguments"][:200])
                    args = {}
                if not isinstance(args, dict):
                    args = {"_raw": args}
                calls.append({
                    "id": item["id"] or f"call_{idx}",
                    "name": item["name"],
                    "arguments": args,
                })
            yield {"type": "tool_calls", "calls": calls}

    # ---------- 向量 ----------
    def embed(self, texts: list[str]) -> list[list[float]]:
        """批量生成向量。texts 已在内部按 batch_size 切分。"""
        self._require()
        if not texts:
            return []
        texts = [t.strip() for t in texts]
        if not any(texts):
            return [[] for _ in texts]

        batch = max(1, int(self.conf.get("batch_size") or 16))
        dims = self.conf.get("dimensions")
        out: list[list[float]] = []
        sdk = self._sdk()
        for i in range(0, len(texts), batch):
            chunk = texts[i:i + batch]
            try:
                kw: dict[str, Any] = {"model": self.model, "input": chunk}
                if dims:
                    kw["dimensions"] = int(dims)
                r = sdk.embeddings.create(**kw)
            except Exception as e:
                raise LLMError(_friendly_error(e, self.kind)) from e
            # 按 index 归位：部分服务（如 OpenAI 兼容网关）不保证返回顺序，
            # 旧实现直接 append 会把向量配错文本，导致检索结果与内容错位。
            slots: list[list[float] | None] = [None] * len(chunk)
            unordered: list[list[float]] = []
            for item in r.data:
                idx = getattr(item, "index", None)
                emb = getattr(item, "embedding", None) or []
                if isinstance(idx, int) and 0 <= idx < len(chunk) and slots[idx] is None:
                    slots[idx] = emb
                else:
                    unordered.append(emb)
            for emb in unordered:
                for j in range(len(slots)):
                    if slots[j] is None:
                        slots[j] = emb
                        break
            if any(s is None or not s for s in slots):
                raise LLMError(
                    f"文本向量：返回条数与输入不一致（输入 {len(chunk)} 条，"
                    f"返回 {len(r.data)} 条）")
            out.extend(s for s in slots if s is not None)
        return out

    # ---------- 连通性测试 ----------
    def test(self) -> dict:
        """实测配置是否可用。返回 {ok, message, detail}。"""
        self._require()
        label = KIND_LABEL.get(self.kind, self.kind)
        try:
            if self.kind == "embedding":
                vec = self.embed(["连接测试", "第二条测试文本"])
                if not vec or any(not v for v in vec):
                    return {"ok": False, "message": f"{label}：返回向量为空。"}
                dims = {len(v) for v in vec}
                if len(dims) != 1:
                    return {"ok": False,
                            "message": f"{label}：同一批向量维度不一致 {sorted(dims)}，"
                                       "请检查服务端模型/维度配置。"}
                return {"ok": True,
                        "message": f"{label}：连接成功，向量维度 {dims.pop()}。"}
            r = self.chat([{"role": "user", "content": "回复“ok”两个字。"}],
                          max_tokens=16)
            return {"ok": True,
                    "message": f"{label}：连接成功，模型回复“{r[:20]}”。"}
        except LLMError as e:
            return {"ok": False, "message": str(e)}
        except Exception as e:
            return {"ok": False, "message": _friendly_error(e, self.kind)}


def _embedding_provider(cfg, kind: str) -> str:
    """embedding 的运行方式：api（OpenAI 兼容服务）/ local（项目内模型）。"""
    if kind != "embedding":
        return "api"
    conf = (getattr(cfg, "llm", {}) or {}).get(kind, {}) or {}
    return str(conf.get("provider") or "api").strip().lower()


def build_client(cfg, kind: str):
    """从 Settings 构建指定用途的客户端。

    embedding 支持两种 provider：
      - api：走 OpenAI 兼容的 /v1/embeddings（默认，行为不变）
      - local：用项目 models/ 下的模型在本进程内推理（app/llm/local_embed.py）
    """
    if kind not in KINDS:
        raise ValueError(f"未知模型类型：{kind}，可选 {KINDS}")
    conf = (getattr(cfg, "llm", {}) or {}).get(kind, {})
    if _embedding_provider(cfg, kind) == "local":
        from .local_embed import get_engine
        return get_engine(cfg)
    return LLMClient(kind, conf)


def llm_ready(cfg, kind: str) -> bool:
    """该用途的模型是否已配置完整（不实际联网）。"""
    conf = (getattr(cfg, "llm", {}) or {}).get(kind, {}) or {}
    if _embedding_provider(cfg, kind) == "local":
        try:
            from .local_embed import _abs_model_path
            model = str(conf.get("model") or "")
            return bool(model) and _abs_model_path(model).is_dir()
        except Exception:
            return False
    return bool(conf.get("base_url") and conf.get("api_key")
                and conf.get("model"))

