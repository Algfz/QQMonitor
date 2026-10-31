"""QQMonitor 入口。

用法:
    python main.py init                初始化目录与数据库
    python main.py run                 启动 QQ 监听 + Web 看板
    python main.py archive [--dry]     归档入站目录的会议录制文件
    python main.py digest [--date 2026-08-30]   生成每日摘要
    python main.py import 记录.txt --group 123456789 [--name 班级群] [--dry] [--force]
    python main.py export [--group 123] [--from 2026-08-01] [--to 2026-08-31] [--out a.csv]
    python main.py schedule-backfill --from 2026-10-01 --to 2026-10-31 [--dry] [--limit 6000]
                                      按日期区间重抽群聊日程（绕过 last_msg_id 游标，幂等）
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import shutil
import sys
from datetime import datetime
from pathlib import Path

from app.config import BASE_DIR, load_config
from app.db import Database
from app.digest import generate_digest
from app.exporter import export_csv
from app.importer import import_file
from app.knowledge import sync_audio_inbox, sync_knowledge
from app.meetings import archive_inbox, find_tencent_meeting_recordings
from app.web.server import WebServer

log = logging.getLogger("main")


def setup_logging(debug: bool = False) -> None:
    level = logging.DEBUG if debug else logging.INFO
    (BASE_DIR / "data").mkdir(exist_ok=True)
    handlers: list[logging.Handler] = []
    # pythonw.exe（后台静默运行）没有控制台，sys.stdout/stderr 均为 None。
    # 此时若仍挂 StreamHandler，每条日志都会触发 handleError 白白开销，
    # 所以只在有控制台时才加。
    if sys.stdout is not None:
        handlers.append(logging.StreamHandler(sys.stdout))
    # 按大小轮转：旧实现用 FileHandler，日志只增不减（实测涨到过 30MB），
    # Windows 上还占着文件无法手动删除。
    from logging.handlers import RotatingFileHandler
    handlers.append(RotatingFileHandler(
        BASE_DIR / "data" / "qqmonitor.log", maxBytes=10 * 1024 * 1024,
        backupCount=5, encoding="utf-8"))
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
    )


def _kb_subdirs() -> tuple[str, ...]:
    return ("meetings", "inbox", "digests", "exports",
            "knowledge/chat", "knowledge/media", "knowledge/documents",
            "knowledge/transcripts")


def cmd_init() -> None:
    (BASE_DIR / "data").mkdir(exist_ok=True)
    for sub in _kb_subdirs():
        (BASE_DIR / "data" / sub).mkdir(parents=True, exist_ok=True)
    cfg_path = BASE_DIR / "config.yaml"
    if not cfg_path.exists():
        example = BASE_DIR / "config.example.yaml"
        shutil.copy(example, cfg_path)
        print(f"已创建配置文件: {cfg_path}")
    cfg = load_config()
    Database(cfg.resolve(cfg.storage["db_path"]))
    print("数据库已初始化:", cfg.resolve(cfg.storage["db_path"]))
    print("目录结构:")
    for sub in _kb_subdirs():
        print(f"  data/{sub}/")
    tcm = find_tencent_meeting_recordings()
    if tcm:
        print("\n检测到腾讯会议录制目录:")
        for p in tcm:
            print(f"  {p}")
        print("可以把录制文件拷到 data/inbox/ 后运行: python main.py archive")
    else:
        print("\n未检测到腾讯会议录制目录（可忽略，录制文件手动拷到 data/inbox/ 即可）")


def _port_in_use(host: str, port: int) -> bool:
    """本机是否已有进程在监听该端口。

    用于防止 WebUI 双开（开机自启已拉起、又手动启动一次）。
    这里用端口而不是锁文件：进程退出端口即释放，不会留下残留锁。
    host 为 0.0.0.0 时探测 127.0.0.1（本机必然有回环）。
    """
    import socket
    probe = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1.0)
        return s.connect_ex((probe, int(port))) == 0


async def cmd_run(debug: bool = False) -> None:
    """启动 Web 看板（只读本机 QQ 库，不含任何 QQ 协议客户端）。

    2026-10-30 决策：NapCat/OneBot 等「额外登录一个 QQ 号」的取数方式
    永久移除——既有封号风险，也会漏消息。数据来源只有本机 NTQQ 库：
    sync-daemon 每 3 分钟全表扫描 + message_id 去重，可重复执行。
    """
    cfg = load_config()
    host, port = cfg.web["host"], cfg.web["port"]
    if _port_in_use(host, port):
        # 端口已被监听：要么已有 WebUI 实例，要么被别的程序占用。两种情况下
        # 本进程都无法正常启动，直接退出（避免双开写库/抢日志）。
        log.warning("端口 %s 已在监听，WebUI 应已在运行，本进程退出以免双开。"
                    "如需重启请先运行 webui-daemon-stop.bat", port)
        return
    db = Database(cfg.resolve(cfg.storage["db_path"]))
    try:
        from app.recordings import recover_pending
        recover_pending(db, cfg)
    except Exception:  # noqa: BLE001
        log.warning("恢复录音任务失败", exc_info=True)
    web = WebServer(cfg, db)

    tasks = [asyncio.create_task(web.start())]
    log.info("Web 看板已启动，按 Ctrl+C 退出")
    try:
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for r in results:
            if isinstance(r, BaseException) and not isinstance(r, asyncio.CancelledError):
                log.error("子任务异常退出：%s: %s", type(r).__name__, r)
        # web.start() 启动完就会返回；
        # 必须显式挂起，否则进程启动即退出（表现为 8088 端口没人监听）。
        await asyncio.Event().wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await web.stop()


def cmd_archive(dry_run: bool = False) -> None:
    cfg = load_config()
    inbox = cfg.resolve(cfg.storage["inbox_dir"])
    meeting_dir = cfg.resolve(cfg.storage["meeting_dir"])
    results = archive_inbox(inbox, meeting_dir, dry_run=dry_run)
    if not results:
        print(f"入站目录没有待归档文件: {inbox}")
        print("提示：把腾讯会议录屏/录音拷进来再运行本命令")
    else:
        print(f"共处理 {len(results)} 个文件，已归档到 {meeting_dir}")


def cmd_digest(day: str | None) -> None:
    cfg = load_config()
    db = Database(cfg.resolve(cfg.storage["db_path"]))
    day = day or datetime.now().strftime("%Y-%m-%d")
    result = generate_digest(db, cfg.resolve(cfg.storage["digest_dir"]), day)
    print(result["markdown"])


def _index_after(db, cfg) -> None:
    """导入 / 知识库同步后增量更新向量索引。

    失败不抛出：写入 pipeline.state.json 的脏标记，下次同步/`kb` 重试。
    """
    from app.llm.indexer import Indexer
    from app.pipeline import mark_dirty, note_index_result
    try:
        res = Indexer(db, cfg).build()
        print(f"  向量索引: 新增 {res.get('added', 0)} 块"
              f"（共 {res.get('chunks', 0)} 块）")
        if res.get("errors"):
            note_index_result(cfg, False)
            mark_dirty(cfg, "index")
            print(f"  有 {res['errors']} 个文件向量化失败（已记入待补索引，下轮重试）")
        else:
            note_index_result(cfg, True)
    except Exception as e:  # noqa: BLE001
        note_index_result(cfg, False)
        mark_dirty(cfg, "index")
        print(f"  向量索引更新失败（已记入待补索引，下轮重试）：{e}")


def cmd_import(file: str, group_id: int, group_name: str, dry_run: bool,
               replace: bool = False, force: bool = False) -> None:
    cfg = load_config()
    db = Database(cfg.resolve(cfg.storage["db_path"]))
    result = import_file(db, cfg, Path(file), group_id, group_name,
                         dry_run=dry_run, replace=replace, force=force)
    if not result["ok"]:
        print(f"导入失败：{result['error']}")
        return
    verb = "（试运行，未写入）" if dry_run else ""
    print(f"解析 {result['total']} 条，新增 {result['inserted']} 条，"
          f"跳过重复 {result['skipped']} 条 {verb}")
    if replace and not dry_run:
        print("已启用重导模式，旧导入消息已替换（旧数据已备份到 data/exports/）")
    if not dry_run:
        # 导入成功后自动同步知识库（聊天原文 Markdown + 媒体）
        print("同步知识库 ...")
        kb = sync_knowledge(db, cfg, dry_run=False)
        print(f"  聊天记录: {len(kb['chat'])} 个文件 -> {kb['dir']}")
        m = kb["media"]
        if m["groups"]:
            print(f"  媒体: 复制 {m['copied']} 个, 跳过 {m['skipped']} 个")
        ib = kb["inbox"]
        if ib["transcripts"] or ib["documents"]:
            print(f"  inbox 分拣: 文字稿 {ib['transcripts']}, 文档 {ib['documents']}")
        _index_after(db, cfg)
    print("提示：可运行 python main.py digest 生成摘要查看效果")


def cmd_kb(dry_run: bool) -> None:
    cfg = load_config()
    db = Database(cfg.resolve(cfg.storage["db_path"]))
    kb = sync_knowledge(db, cfg, dry_run=dry_run)
    verb = "（试运行）" if dry_run else ""
    print(f"知识库同步完成{verb}: {kb['dir']}")
    for name in kb["chat"]:
        print(f"  chat/{name}")
    m = kb["media"]
    print(f"  媒体: 复制 {m['copied']}, 跳过 {m['skipped']}（{m['groups']} 个群）")
    ib = kb["inbox"]
    print(f"  inbox: 文字稿 {ib['transcripts']}, 文档 {ib['documents']}")
    if not dry_run:
        _index_after(db, cfg)


def cmd_watchlist(preview_only: bool = False) -> None:
    """按自动规则（备注名后缀/置顶）扫描本机 QQ 库并维护白名单。"""
    cfg = load_config()
    from app.watchlist import apply_from_nt, auto_cfg, preview as wl_preview

    ac = auto_cfg(cfg)
    if preview_only:
        d = wl_preview(cfg)
        if not d.get("ok"):
            print(f"扫描失败：{d.get('error')}")
            return
        print(f"规则：后缀 {ac['by_remark_suffix'] or '（无）'}，"
              f"置顶={'是' if ac['by_pin'] else '否'}，范围={ac['scope']}")
        for item in d.get("groups") or []:
            print(f"  群 {item['id']}  {item['name']}  ← {'、'.join(item['reasons'])}")
        for item in d.get("buddies") or []:
            print(f"  好友 {item['id']}  {item['name']}  ← {'、'.join(item['reasons'])}")
        if not (d.get("groups") or d.get("buddies")):
            print("  （没有命中项）")
        return

    summary = apply_from_nt(cfg)
    if not summary.get("ok"):
        print(f"扫描失败：{summary.get('error')}")
        return
    print(f"命中群 {len(summary.get('auto_groups') or [])} 个、"
          f"好友 {len(summary.get('auto_buddies') or [])} 个")
    print(f"  新增群：{summary.get('added_groups') or '无'}")
    print(f"  移出群：{summary.get('removed_groups') or '无'}")
    print(f"  新增好友：{summary.get('added_buddies') or '无'}")
    print(f"  移出好友：{summary.get('removed_buddies') or '无'}")
    print(f"  是否写回配置：{'是' if summary.get('wrote') else '否（没有变化）'}")


def cmd_group_files(scan_only: bool = False, limit: int = 50) -> None:
    """把本机已有的群文件复制进知识库（不联网下载）。"""
    cfg = load_config()
    db = Database(cfg.resolve(cfg.storage["db_path"]))
    from app.group_files import process_pending

    if scan_only:
        rows = db.list_group_files(limit=limit)
        print(f"群文件记录 {len(rows)} 条（最近优先）：")
        for r in rows:
            print(f"  [{r['status']:9s}] {r['group_id']:>12} {r['size']/1024/1024:8.2f}MB "
                  f"{r['name']}  {r.get('detail') or ''}")
        print("统计：", db.group_files_stats())
        return

    res = process_pending(db, cfg, limit=limit)
    print(res.get("detail"))
    if res.get("ingested"):
        from app.llm.indexer import Indexer
        try:
            r = Indexer(db, cfg).build(force=False)
            print(f"  向量索引：新增 {r.get('added', 0)} 段（失败 {r.get('errors', 0)} 个文件）")
        except Exception as e:  # 索引失败不影响入库
            print(f"  ⚠ 向量索引更新失败（{e}），下轮同步会自动重试")


def cmd_ingest_audio(dry_run: bool = False) -> None:
    """把 data/inbox 里的录音/录屏转写进知识库。"""
    cfg = load_config()
    db = Database(cfg.resolve(cfg.storage["db_path"]))
    kdir = cfg.resolve(cfg.knowledge.get("dir", "data/knowledge"))
    res = sync_audio_inbox(db, cfg, kdir, dry_run=dry_run)
    verb = "（试运行）" if dry_run else ""
    print(f"录音入库完成{verb}：扫描 {res['scanned']} 个，"
          f"转写 {res['transcribed']} 个，仅归档 {res['archived']} 个，"
          f"失败 {res['failed']} 个")
    for item in res.get("files") or []:
        mark = "✓" if item.get("ok") else "✗"
        print(f"  {mark} {item['name']}：{item.get('detail') or ''}")
    if res.get("transcribed") and not dry_run:
        _index_after(db, cfg)


def cmd_transcribe_one(rec_id: int) -> None:
    """单独转写一条录音（由 Web 上传后 fork 出的独立进程调用）。"""
    from app.recordings import run_worker
    run_worker(rec_id)


def cmd_export(group_id: int | None, date_from: str, date_to: str, out: str) -> None:
    cfg = load_config()
    db = Database(cfg.resolve(cfg.storage["db_path"]))
    out_path = Path(out) if out else cfg.resolve("data/exports") / \
        f"export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    result = export_csv(db, out_path, group_id=group_id,
                        date_from=date_from, date_to=date_to)
    print(f"已导出 {result['rows']} 条消息 -> {result['path']}")


def cmd_schedule_backfill(date_from: str, date_to: str, dry: bool = False,
                          limit: int = 0) -> None:
    """按日期区间重抽群聊日程。

    与 `sync-daemon` 每轮的 `extract_today` 共用抽取/去重逻辑，但**不读也不写**
    `data/schedule_chat.state.json` 的 last_msg_id——游标只会往前走，历史消息里
    漏掉的日程只能靠这个命令补回来（跑几次都幂等：dedupe_key + 相似度合并 + 墓碑）。
    """
    cfg = load_config()
    db = Database(cfg.resolve(cfg.storage["db_path"]))
    from app.chat_schedule import backfill
    res = backfill(db, cfg, date_from, date_to, limit=limit, dry_run=dry)
    if not res.get("ok"):
        print(f"补抽失败：{res.get('error')}")
        raise SystemExit(1)
    verb = "（试运行，未写库）" if dry else ""
    print(f"补抽完成{verb}：{res['from']} ~ {res['to']}，{res['days']} 天中 "
          f"{res.get('days_with_messages', 0)} 天有消息，共 {res['messages']} 条消息")
    if dry:
        print(f"  将新增 {res.get('planned', 0)} 条，已存在 {res.get('exists', 0)} 条")
    else:
        print(f"  新增 {res.get('inserted', 0)} 条，"
              f"复用已有 {res.get('merged', 0)} 条")
    if res.get("dismissed"):
        print(f"  被墓碑丢弃 {res['dismissed']} 条（用户删过的日程不复活）")
    for e in res.get("errors") or []:
        print(f"  ! {e}")


def main() -> None:
    parser = argparse.ArgumentParser(description="QQMonitor - QQ 群信息整理工具")
    parser.add_argument("--debug", action="store_true", help="输出调试日志")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("init", help="初始化目录与数据库")
    p_run = sub.add_parser("run", help="启动监听与看板")
    p_run.add_argument("--debug", action="store_true")

    p_arc = sub.add_parser("archive", help="归档会议录制文件")
    p_arc.add_argument("--dry", action="store_true", help="试运行，不实际移动")

    p_dig = sub.add_parser("digest", help="生成每日摘要")
    p_dig.add_argument("--date", help="日期 YYYY-MM-DD，默认今天")

    p_imp = sub.add_parser(
        "import", help="导入聊天记录文件（TXT/CSV/JSON）")
    p_imp.add_argument("file", help="记录文件路径")
    p_imp.add_argument("--group", type=int, default=0,
                       help="目标群号（QQ 官方导出的 TXT 会自动识别，可省略）")
    p_imp.add_argument("--name", default="", help="群名（可选）")
    p_imp.add_argument("--dry", action="store_true", help="试运行，不写入")
    p_imp.add_argument("--replace", action="store_true",
                       help="重导：先删除该群旧导入消息再写入（更新导出后重导用）")
    p_imp.add_argument("--force", action="store_true",
                       help="忽略群白名单限制，强制导入（群不在 watch_groups 内时使用）")

    p_exp = sub.add_parser("export", help="导出 CSV")
    p_exp.add_argument("--group", type=int, help="群号")
    p_exp.add_argument("--from", dest="date_from", help="起始日期 YYYY-MM-DD")
    p_exp.add_argument("--to", dest="date_to", help="结束日期 YYYY-MM-DD")
    p_exp.add_argument("--out", help="输出文件路径")

    p_sb = sub.add_parser(
        "schedule-backfill",
        help="按日期区间重抽群聊日程（绕过 last_msg_id 游标，可重复跑）")
    p_sb.add_argument("--from", dest="date_from", required=True,
                      help="起始日期 YYYY-MM-DD（含当天）")
    p_sb.add_argument("--to", dest="date_to", required=True,
                      help="结束日期 YYYY-MM-DD（含当天）")
    p_sb.add_argument("--dry", action="store_true",
                      help="试运行：只打印会新增什么，不写库")
    p_sb.add_argument("--limit", type=int, default=0,
                      help="每天最多读多少条消息，0 = 用配置的 chat_max_messages")

    p_kb = sub.add_parser("kb", help="同步知识库（聊天原文/媒体/inbox 分拣）")
    p_kb.add_argument("--dry", action="store_true", help="试运行，不实际写入")

    sub.add_parser("sync-daemon",
                   help="独立自动同步守护进程（不开 WebUI 也定时同步）")

    p_wl = sub.add_parser("watchlist",
                          help="按备注名后缀/置顶自动维护监听白名单")
    p_wl.add_argument("--preview", action="store_true",
                      help="只显示会命中哪些群/好友，不写配置")

    p_gf = sub.add_parser("group-files",
                          help="把本机已有的群文件复制进知识库（不联网下载）")
    p_gf.add_argument("--scan-only", action="store_true",
                      help="只列出已识别的文件消息与状态，不入库")
    p_gf.add_argument("--limit", type=int, default=50,
                      help="本次最多处理/显示多少个文件")

    p_audio = sub.add_parser("ingest-audio",
                             help="把 data/inbox 里的录音/录屏转写进知识库")
    p_audio.add_argument("--dry", action="store_true", help="试运行，不转写")
    p_one = sub.add_parser("transcribe-one",
                           help="转写指定录音（内部用：Web 上传后在独立进程里跑）")
    p_one.add_argument("rec_id", type=int, help="recordings 表里的 id")
    p_models = sub.add_parser("models",
                              help="查看/下载本地模型（向量、语音识别、图片理解）")
    p_models.add_argument("--download", nargs="*", metavar="KEY",
                          help="下载指定模型：embed/vision/asr/punc/vad/spk，"
                               "或整组 asr / core / all（不带参数等于 all）")
    p_models.add_argument("--list", action="store_true",
                          help="只列出模型状态（默认行为）")

    args = parser.parse_args()
    setup_logging(args.debug)

    if args.command == "init":
        cmd_init()
    elif args.command == "run":
        asyncio.run(cmd_run(args.debug))
    elif args.command == "archive":
        cmd_archive(args.dry)
    elif args.command == "digest":
        cmd_digest(args.date)
    elif args.command == "import":
        cmd_import(args.file, args.group, args.name, args.dry, args.replace,
                   args.force)
    elif args.command == "export":
        cmd_export(args.group, args.date_from or "", args.date_to or "", args.out or "")
    elif args.command == "kb":
        cmd_kb(args.dry)
    elif args.command == "schedule-backfill":
        cmd_schedule_backfill(args.date_from, args.date_to, args.dry, args.limit)
    elif args.command == "sync-daemon":
        from app.autosync_daemon import run_daemon
        sys.exit(run_daemon())
    elif args.command == "watchlist":
        cmd_watchlist(args.preview)
    elif args.command == "group-files":
        cmd_group_files(args.scan_only, args.limit)
    elif args.command == "ingest-audio":
        cmd_ingest_audio(args.dry)
    elif args.command == "transcribe-one":
        cmd_transcribe_one(args.rec_id)
    elif args.command == "models":
        from app.models_download import download, print_status, resolve
        if args.download is None:
            print_status()
        else:
            keys = resolve(args.download or ["all"])
            sys.exit(1 if download(keys) else 0)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
