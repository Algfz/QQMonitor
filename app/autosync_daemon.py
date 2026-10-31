"""独立自动同步守护进程：不开 WebUI 也能定时从本机 QQ 库同步并清洗。

为什么单独一个进程
-----------------
原先定时器挂在 Web 服务里（WebServer._start_auto_sync），必须开着 WebUI 才转。
但「盯着群消息」是个 7×24 的后台活，不该依赖用户记得开网页。
本模块把它做成独立常驻进程：`python main.py sync-daemon`（或双击 sync-daemon.bat），
丢后台/开机自启即可，不用 Windows 计划任务。

与 WebUI 定时器的关系
---------------------
两者跑的是同一条 pipeline，但同一时刻只能有一个在跑，否则两个进程同时
解密/写库会撞 WAL。用「锁文件 + 进程存活检测」互斥：
- 本守护进程启动时**原子占用**锁（data/autosync.daemon.lock，O_CREAT|O_EXCL）；
- WebUI 定时器每轮都检测锁是否被活进程持有，持有就自动让位；
- 进程退出（含异常）时锁失效，WebUI 定时器可接管，保证总有一个在看家。

状态文件 data/autosync.status.json 每轮更新，WebUI 用它展示「上次同步 / 下轮时间」。
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path

from .config import Settings
from .db import Database

log = logging.getLogger("autosync_daemon")

LOCK_NAME = "autosync.daemon.lock"
STATUS_NAME = "autosync.status.json"


def _data_dir(cfg: Settings) -> Path:
    return cfg.resolve(cfg.storage.get("db_path", "data/qqmonitor.db")).parent


def _lock_path(cfg: Settings) -> Path:
    return _data_dir(cfg) / LOCK_NAME


def _pid_alive(pid: int) -> bool:
    """判断 PID 是否存活（Windows / POSIX 通用）。

    注意：Windows 上不能用 os.kill(pid, 0) 检测——Python 在 Windows 把
    任何非控制台信号（含 0）都映射成 TerminateProcess，会真的杀掉进程！
    所以 Windows 走 OpenProcess 句柄探测，POSIX 才用信号 0。
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        # PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            # 进程还在则 GetExitCodeProcess 返回 STILL_ACTIVE(259)
            code = ctypes.c_ulong()
            if not ctypes.windll.kernel32.GetExitCodeProcess(
                    handle, ctypes.byref(code)):
                return False
            return code.value == 259
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)  # POSIX：信号 0 只检测不发送
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # 存在但无权发信号，视为存活
    except OSError:
        return False
    return True


def _read_lock_pid(cfg: Settings) -> int:
    p = _lock_path(cfg)
    try:
        return int(p.read_text(encoding="utf-8").strip().split()[0])
    except (ValueError, OSError, IndexError):
        return -1


def daemon_running(cfg: Settings) -> bool:
    """是否已有守护进程在跑（WebUI 定时器据此让位）。"""
    if not _lock_path(cfg).exists():
        return False
    pid = _read_lock_pid(cfg)
    return pid > 0 and _pid_alive(pid)


def _acquire_lock(cfg: Settings) -> Path | None:
    """原子占用锁文件（O_CREAT|O_EXCL）。已有活进程持锁时返回 None。"""
    p = _lock_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    for _attempt in range(2):
        try:
            fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            pid = _read_lock_pid(cfg)
            if pid > 0 and _pid_alive(pid):
                log.warning("已有自动同步守护进程在运行（PID %s），本进程退出", pid)
                return None
            # 陈旧锁（持有进程已死）：删掉重试一次
            log.info("清理陈旧的守护进程锁（PID %s 已不存在）", pid)
            try:
                p.unlink()
            except OSError as e:
                log.warning("清理锁文件失败：%s", e)
                return None
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(f"{os.getpid()} {datetime.now().isoformat(timespec='seconds')}\n")
        return p
    return None


def _release_lock(p: Path | None) -> None:
    """只删除自己持有的锁，避免误删别的进程的锁。"""
    if not p or not p.exists():
        return
    try:
        holder = int(p.read_text(encoding="utf-8").split()[0])
    except (ValueError, OSError, IndexError):
        holder = -1
    if holder != os.getpid():
        log.warning("锁文件已被 PID %s 持有，跳过删除", holder)
        return
    try:
        p.unlink()
    except OSError:
        pass


def _read_interval(cfg: Settings) -> float:
    """从配置读同步间隔（秒）。0/未配置=默认 3 分钟，下限 1 分钟。"""
    try:
        minutes = float(cfg.knowledge.get("auto_sync_minutes") or 0)
    except (TypeError, ValueError):
        minutes = 0
    if minutes <= 0:
        minutes = 3.0  # 守护进程默认 3 分钟（它的使命就是定时转）
    return max(60.0, minutes * 60.0)


def _write_status(cfg: Settings, *, started: str, ok: bool | None,
                  detail: str = "", error: str = "",
                  elapsed: float | None = None,
                  next_run: str = "", running: bool | None = None,
                  keep_result: bool = False) -> None:
    """写状态文件（WebUI 知识库页展示上次同步 / 下轮时间）。

    ok=None + running=True 表示「正在跑」；keep_result=True 时保留上一轮的
    结果字段，只更新心跳/下轮时间（等待下一轮时用）。
    """
    p = _data_dir(cfg) / STATUS_NAME
    prev: dict = {}
    if keep_result:
        try:
            prev = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            prev = {}
    if keep_result:
        ok = prev.get("ok", ok)
        detail = prev.get("detail", detail)
        error = prev.get("error", error)
        elapsed = prev.get("elapsed_s", elapsed)
    data = {
        "pid": os.getpid(),
        "interval_min": round(_read_interval(cfg) / 60, 1),
        "started": started,
        "last_run": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "ok": ok,
        "running": bool(running) if running is not None else ok is None,
        "detail": detail,
        "error": error,
        "elapsed_s": round(elapsed, 1) if isinstance(elapsed, (int, float)) else None,
        "next_run": next_run,
    }
    try:
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, p)
    except OSError:
        pass


def run_daemon() -> int:
    """守护进程主循环。返回进程退出码。"""
    from .config import load_config
    from .pipeline import SyncTask

    cfg = load_config()
    db = Database(cfg.resolve(cfg.storage["db_path"]))

    lock = _acquire_lock(cfg)
    if lock is None:
        return 2  # 已有实例在跑

    started = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    interval = _read_interval(cfg)
    log.info("自动同步守护进程已启动（PID %d）：每 %.1f 分钟同步一次，Ctrl+C 停止",
             os.getpid(), interval / 60)
    print(f"[自动同步守护] PID {os.getpid()}，每 {interval/60:.1f} 分钟同步一次。"
          f"关闭本窗口或按 Ctrl+C 停止。")
    _write_status(cfg, started=started, ok=True, detail="守护进程已启动")

    def _tick() -> None:
        nonlocal cfg
        # 每轮重读 config.yaml：设置页保存的白名单、以及 sync 过程中由
        # 自动规则（备注后缀/置顶）改写的新名单，下一轮必须立刻可见；
        # 否则守护进程会一直用启动时的旧名单，改配置要重启才生效。
        try:
            fresh = load_config()
            if fresh.path == cfg.path:
                cfg = fresh
        except Exception as e:  # noqa: BLE001
            log.warning("重读配置失败，沿用上一轮配置：%s: %s",
                        type(e).__name__, e)
        t0 = time.time()
        _write_status(cfg, started=started, ok=None, detail="正在同步…")
        task = SyncTask(db, cfg)
        task.run()
        # 每轮同步后从「当天全部白名单群消息」抽取日程（全量上下文，允许模型查资料）
        try:
            from .chat_schedule import extract_today
            sch = extract_today(db, cfg)
            if sch.get("ok"):
                log.info("日程抽取：%s 条消息 → %s 条日程",
                         sch.get("messages", 0), sch.get("inserted", 0))
            elif sch.get("error") and "未启用" not in sch["error"]:
                log.warning("日程抽取未完成：%s", sch["error"])
        except Exception as e:  # noqa: BLE001
            log.warning("日程抽取异常：%s: %s", type(e).__name__, e)
        try:
            from .file_schedule import extract_files
            fs = extract_files(db, cfg)
            if fs.get("inserted"):
                log.info("文件日程：%s 个文件 → %s 条日程",
                         fs.get("files", 0), fs.get("inserted", 0))
            elif fs.get("error") and "未启用" not in fs["error"]:
                log.warning("文件日程未完成：%s", fs["error"])
        except Exception as e:  # noqa: BLE001
            log.warning("文件日程异常：%s: %s", type(e).__name__, e)
        elapsed = time.time() - t0
        if task.error:
            log.warning("本轮同步失败：%s", task.error)
            _write_status(cfg, started=started, ok=False,
                          detail=task.detail or "", error=task.error,
                          elapsed=elapsed)
        elif "没有新消息" in task.detail:
            log.info("本轮无新消息（%.1fs）", elapsed)
            _write_status(cfg, started=started, ok=True,
                          detail="没有新消息（已是最新）", elapsed=elapsed)
        else:
            log.info("本轮同步完成（%.1fs）：%s", elapsed, task.detail)
            _write_status(cfg, started=started, ok=True,
                          detail=task.detail, elapsed=elapsed)

    try:
        while True:
            interval = _read_interval(cfg)  # 每轮重读，改配置不用重启
            t0 = time.time()
            try:
                _tick()
            except Exception as e:  # noqa: BLE001
                log.exception("本轮同步异常：%s: %s", type(e).__name__, e)
                _write_status(cfg, started=started, ok=False,
                              error=f"{type(e).__name__}: {e}")
            # 按「间隔 - 本轮耗时」休眠，保证节奏稳定；同步耗时超过间隔则立即下一轮
            el = time.time() - t0
            sleep_for = max(5.0, interval - el)
            nxt = datetime.fromtimestamp(time.time() + sleep_for)
            _write_status(cfg, started=started, ok=None, detail="等待下一轮",
                          elapsed=el, running=False, keep_result=True,
                          next_run=nxt.strftime("%Y-%m-%d %H:%M:%S"))
            time.sleep(sleep_for)
    except KeyboardInterrupt:
        log.info("收到退出信号，守护进程停止")
        print("\n[自动同步守护] 已停止。")
        return 0
    finally:
        _release_lock(lock)
