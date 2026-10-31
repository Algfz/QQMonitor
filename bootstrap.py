# -*- coding: utf-8 -*-
"""一键把运行环境装好。

克隆仓库后跑一次即可：`setup.cmd`（或 `python bootstrap.py`）。

它做四件事：
  1. 找 Python 3.11（系统里没有就告诉你装哪个）
  2. 在仓库里建 .venv
  3. 装依赖（有 N 卡自动装 CUDA 版 torch，没有就装 CPU 版）
  4. 生成 config.yaml，并按需下载模型

之后用 `webui.bat` / `sync-daemon.bat` 启动，它们会优先用 .venv。

常用参数：
  --cpu               强制装 CPU 版 torch（没有 N 卡时更小更快）
  --skip-deps         只建环境，不装依赖
  --models embed,asr  顺带下载模型（core / asr / all 也可以）
  --no-models         不下载模型（之后可用 python main.py models --download all）
  --dry-run           只打印将要做什么
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MIN_PY = (3, 11)
CUDA_INDEX = "https://download.pytorch.org/whl/cu128"


def say(msg: str = "") -> None:
    print(msg, flush=True)


def step(msg: str) -> None:
    print(f"\n==> {msg}", flush=True)


def run(cmd: list[str], *, quiet: bool = False) -> int:
    say("    " + " ".join(cmd))
    if quiet:
        r = subprocess.run(cmd, cwd=str(ROOT),
                           stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    else:
        r = subprocess.run(cmd, cwd=str(ROOT))
    return r.returncode


def find_python() -> str | None:
    """找一个能建虚拟环境的 Python 3.11。"""
    if sys.version_info >= MIN_PY:
        return sys.executable
    candidates = []
    if os.name == "nt":
        candidates += [
            ["py", "-3.11"],
            ["py", "-3"],
        ]
    candidates += [["python3.11"], ["python3"], ["python"]]
    for cand in candidates:
        exe = cand[0]
        if shutil.which(exe) is None:
            continue
        try:
            out = subprocess.run(cand + ["-c", "import sys;print('%d.%d' % sys.version_info[:2])"],
                                 capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.SubprocessError):
            continue
        ver = (out.stdout or "").strip()
        try:
            major, minor = (int(x) for x in ver.split("."))
        except ValueError:
            continue
        if (major, minor) >= MIN_PY:
            return " ".join(cand)
    return None


def has_nvidia() -> bool:
    if shutil.which("nvidia-smi") is None:
        return False
    try:
        r = subprocess.run(["nvidia-smi", "-L"], capture_output=True, text=True, timeout=20)
        return r.returncode == 0 and "GPU" in (r.stdout or "")
    except (OSError, subprocess.SubprocessError):
        return False


def venv_py(vdir: Path) -> Path:
    return vdir / ("Scripts\\python.exe" if os.name == "nt" else "bin/python")


def main() -> int:
    ap = argparse.ArgumentParser(description="一键安装 QQMonitor 运行环境")
    ap.add_argument("--cpu", action="store_true", help="强制 CPU 版 torch")
    ap.add_argument("--skip-deps", action="store_true", help="只建环境不装依赖")
    ap.add_argument("--models", default="", help="额外的模型：embed/asr/core/all 等")
    ap.add_argument("--no-models", action="store_true", help="不下载模型")
    ap.add_argument("--venv", default=str(ROOT / ".venv"), help="虚拟环境目录")
    ap.add_argument("--dry-run", action="store_true", help="只看计划，不执行")
    args = ap.parse_args()

    vdir = Path(args.venv)
    say(f"仓库目录：{ROOT}")
    say(f"虚拟环境：{vdir}")

    step("1/5 检查 Python")
    py = find_python()
    if py is None:
        say("   [错误] 没找到 Python 3.11+。")
        say("   Windows 可以这样装（任选一种）：")
        say("     winget install -e --id Python.Python.3.11")
        say("     或从 https://www.python.org/downloads/ 下载 3.11 安装包")
        return 2
    say(f"   用 {py}")
    if args.dry_run:
        say("   (dry-run 到此为止)")
        return 0

    step("2/5 建虚拟环境")
    if venv_py(vdir).exists():
        say("   已存在，跳过")
    else:
        if vdir.is_dir() and any(vdir.iterdir()):
            say(f"   [错误] {vdir} 已存在但不是可用的虚拟环境，请删掉它再重试")
            return 3
        venv.EnvBuilder(with_pip=True, clear=False, symlinks=False).create(str(vdir))
        say("   已创建")
    vpy = venv_py(vdir)
    if not vpy.exists():
        say(f"   [错误] 没生成解释器：{vpy}")
        return 3

    if args.skip_deps:
        say("\n==> 跳过依赖安装（--skip-deps）")
    else:
        step("3/5 装依赖")
        run([str(vpy), "-m", "pip", "install", "--upgrade", "pip", "setuptools", "wheel"], quiet=True)
        gpu = has_nvidia() and not args.cpu
        if gpu:
            say("   检测到 N 卡，装 CUDA 12.8 版 torch（约 3 GB，慢一点）")
            rc = run([str(vpy), "-m", "pip", "install", "torch", "torchaudio",
                      "--index-url", CUDA_INDEX])
            if rc != 0:
                say("   CUDA 版装失败，退回 CPU 版")
                gpu = False
        if not gpu:
            say("   装 CPU 版 torch（推理会慢 5–10 倍；有 N 卡可用 --cpu 之外的方式重装）")
        rc = run([str(vpy), "-m", "pip", "install", "-r", "requirements.txt"])
        if rc != 0:
            say("   [错误] 依赖安装失败，上面是 pip 的输出")
            return 4
        if shutil.which("ffmpeg") is None:
            say("   [提示] 没检测到 ffmpeg（视频抽音轨要用）：winget install -m Gyan.FFmpeg")

    step("4/5 生成配置")
    if args.skip_deps:
        say("   跳过（--skip-deps 只建环境，配置等装完依赖再生成）")
    elif (ROOT / "config.yaml").exists():
        say("   config.yaml 已存在，跳过（想重建就删掉它）")
    else:
        rc = run([str(vpy), "-X", "utf8", "main.py", "init"])
        if rc != 0:
            say("   [错误] main.py init 失败")
            return 5

    step("5/5 模型")
    if args.no_models or not args.models:
        say("   先不下载。之后随时可用：")
        say("     .venv\\python.exe main.py models --list")
        say("     .venv\\python.exe main.py models --download all")
    else:
        rc = run([str(vpy), "-X", "utf8", "main.py", "models", "--download"] + args.models.split(","))

    say("\n就绪。下一步：")
    say("   1) 打开 config.yaml 填 knowledge.nt_db / key、user.*")
    say("   2) 双击 webui.bat（或 .venv\\python.exe main.py run）")
    say("   3) 需要常驻同步就双击 sync-daemon.bat")
    return 0


if __name__ == "__main__":
    sys.exit(main())
