# -*- coding: utf-8 -*-
"""模型下载与状态检查。

克隆仓库后不需要手动去找模型文件：`python main.py models --download all`
会把需要的模型从魔搭（ModelScope）拉到 `models/` 下。

如果某个仓库 ID 变了，改下面的 MAP 即可（`--list` 会显示每个模型的状态）。
"""
from __future__ import annotations

import os
from pathlib import Path

# key -> (ModelScope 仓库 ID, 本地目录, 用途, 大约体积)
MAP: dict[str, tuple[str, str, str, str]] = {
    "embed": (
        "Qwen/Qwen3-Embedding-0.6B",
        "models/Qwen3-Embedding-0.6B",
        "向量检索（本地 embedding；不用它会退回云端 embedding）",
        "1.1 GB",
    ),
    "vision": (
        "Qwen/Qwen2.5-VL-3B-Instruct",
        "models/Qwen2.5-VL-3B-Instruct",
        "图片理解与 OCR（缺省时群图片只存不认）",
        "7.0 GB",
    ),
    "asr": (
        "FireRedTeam/FireRedASR2-AED",
        "models/FireRedASR2-AED",
        "语音转写主干（录音转写必需）",
        "4.4 GB",
    ),
    "punc": (
        "FireRedTeam/FireRedPunc",
        "models/FireRedPunc",
        "转写标点恢复（缺省时输出无标点）",
        "1.2 GB",
    ),
    "vad": (
        "FireRedTeam/FireRedVAD",
        "models/FireRedVAD",
        "长音频静音切分（缺省时整段音频一次送入，长录音易失败）",
        "10 MB",
    ),
    "spk": (
        "iic/speech_campplus_sv_zh-cn_16k-common",
        "models/campplus",
        "说话人分离（把「主讲」和「发言者」分开；缺省时不分人）",
        "30 MB",
    ),
}

# 这些是「转写」这条链路的一整套
GROUPS = {
    "core": ["embed"],
    "asr": ["asr", "punc", "vad", "spk"],
    "all": ["embed", "asr", "punc", "vad", "spk", "vision"],
}


def _root() -> Path:
    return Path(__file__).resolve().parent.parent


def model_path(key: str) -> Path:
    return _root() / MAP[key][1]


def installed(key: str) -> bool:
    p = model_path(key)
    if not p.is_dir():
        return False
    try:
        return any(p.iterdir())
    except OSError:
        return False


def status() -> list[dict]:
    out = []
    for key, (repo, rel, desc, size) in MAP.items():
        out.append({
            "key": key, "repo": repo, "path": rel, "desc": desc,
            "size": size, "installed": installed(key),
        })
    return out


def print_status() -> None:
    print(f"{'键':6} {'状态':6} {'体积':8} 本地目录 / 仓库")
    for r in status():
        mark = "已就绪" if r["installed"] else "缺失"
        print(f"{r['key']:6} {mark:6} {r['size']:8} {r['path']}")
        print(f"{'':22}← {r['repo']}　（{r['desc']}）")


def download(keys: list[str]) -> int:
    """下载若干模型。返回失败的个数。"""
    try:
        from modelscope import snapshot_download  # type: ignore
    except ImportError:
        print("[错误] 缺少 modelscope：pip install modelscope")
        return len(keys)

    failed = 0
    for key in keys:
        if key not in MAP:
            print(f"[跳过] 未知模型 {key}（可选：{'/'.join(MAP)}）")
            failed += 1
            continue
        repo, rel, desc, size = MAP[key]
        target = _root() / rel
        if installed(key):
            print(f"[已存在] {key} → {rel}（如需重下先删目录）")
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        print(f"[下载] {key} ← {repo}（约 {size}，{desc}）")
        try:
            snapshot_download(repo, local_dir=str(target))
        except Exception as e:  # noqa: BLE001  下载失败要能继续下一个
            print(f"[失败] {key}: {e}")
            print("       可改用 HuggingFace 镜像：set HF_ENDPOINT=https://hf-mirror.com")
            failed += 1
            continue
        if not installed(key):
            print(f"[警告] {key} 下载命令执行完但目录仍为空：{target}")
            failed += 1
    return failed


def resolve(keys: list[str]) -> list[str]:
    """把 'all'/'asr' 这类组名展开成具体 key。"""
    out: list[str] = []
    for k in keys:
        for part in str(k).split(","):
            part = part.strip()
            if not part:
                continue
            if part in GROUPS:
                out.extend(GROUPS[part])
            elif part in MAP:
                out.append(part)
            else:
                out.append(part)      # 未知的留给 download() 报错
    seen, uniq = set(), []
    for k in out:
        if k not in seen:
            seen.add(k)
            uniq.append(k)
    return uniq


if __name__ == "__main__":  # pragma: no cover - 手动工具
    import sys

    if "--download" in sys.argv:
        i = sys.argv.index("--download")
        want = sys.argv[i + 1:] or ["all"]
        sys.exit(1 if download(resolve(want)) else 0)
    print_status()
