"""aiohttp Web 看板路由。"""
from __future__ import annotations

import asyncio
import calendar as _calendar
import hmac
import json
import logging
import os
import re
import shutil
import time
import uuid
from datetime import date as _date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote, unquote

from aiohttp import web

from .. import thumbs
from ..cleaning import ingest_files
from ..config import Settings, load_config
from ..db import Database
from ..digest import md_to_html
from ..knowledge import (sync_chat_group, sync_inbox, sync_knowledge,
                         sync_media)
from . import templates as T

log = logging.getLogger("web")

routes = web.RouteTableDef()

_COOKIE_NAME = "qm_token"


def _safe_next(url: str | None) -> str:
    """只允许站内跳转，防止登录后被重定向到外站（open redirect）。"""
    url = (url or "/").strip()
    if not url.startswith("/") or url.startswith("//") or "\r" in url or "\n" in url:
        return "/"
    return url


@web.middleware
async def _csrf_middleware(request: web.Request, handler):
    """跨站写操作防御：POST/PUT/PATCH/DELETE 校验 Origin/Referer 与本站同源。

    本服务原来没有任何鉴权，攻击者可以从恶意网页表单直接 POST 到
    127.0.0.1:8088 触发同步/删除；同源校验能挡住浏览器发起的跨站表单。
    命令行/脚本请求通常不带 Origin/Referer，不受影响。
    """
    # /api/ 是给外部机器人用的独立 token 接口（见 app/kbapi.py），
    # 不走浏览器同源校验；没有浏览器会用它。
    if request.path.startswith("/api/"):
        return await handler(request)
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        origin = request.headers.get("Origin")
        referer = request.headers.get("Referer")
        # 反向代理（tailscale serve / nginx）会把 Host 改写成 127.0.0.1:8088，
        # 但浏览器 Origin 仍是外部域名；用 X-Forwarded-Host/Proto 还原真实站点。
        expected = {f"{request.scheme}://{request.host}"}
        xfh = (request.headers.get("X-Forwarded-Host") or "").split(",")[0].strip()
        xfp = (request.headers.get("X-Forwarded-Proto") or "").split(",")[0].strip()
        if xfp:
            expected.add(f"{xfp}://{request.host}")
        if xfh:
            expected.add(f"{xfp or 'https'}://{xfh}")
        if origin:
            if origin.rstrip("/") not in expected:
                log.warning("拒绝跨站请求：%s %s origin=%s（允许 %s）", request.method,
                            request.path, origin, "，".join(sorted(expected)))
                raise web.HTTPForbidden(text="跨站请求被拒绝")
        elif referer:
            if not any(referer == e or referer.startswith(e + "/") for e in expected):
                log.warning("拒绝跨站请求：%s %s referer=%s（允许 %s）", request.method,
                            request.path, referer, "，".join(sorted(expected)))
                raise web.HTTPForbidden(text="跨站请求被拒绝")
    return await handler(request)


@web.middleware
async def _auth_middleware(request: web.Request, handler):
    """可选的访问口令。

    `web.access_token` 为空 = 不鉴权（默认只绑 127.0.0.1）。
    设置后：页面跳 /login，接口返回 401；登录态存 HttpOnly Cookie（30 天）。
    """
    srv = request.app.get("server")
    if srv is not None:
        srv.refresh_config()
    cfg = request.app["cfg"]
    # 外部机器人接口用独立 api.token（app/kbapi.py 自己校验），与网页登录解耦
    if request.path.startswith("/api/") and (cfg.api or {}).get("enabled"):
        return await handler(request)
    token = str((cfg.web or {}).get("access_token") or "").strip()
    if not token:
        return await handler(request)
    path = request.path
    if path == "/login":
        return await handler(request)

    supplied = request.cookies.get(_COOKIE_NAME) or ""
    if not supplied:
        auth = request.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            supplied = auth[7:].strip()
    query_token = request.query.get("token") or ""
    if supplied and hmac.compare_digest(supplied, token):
        return await handler(request)
    # 首次带 ?token= 访问：种 Cookie 并重定向掉地址栏里的口令（仅 GET）
    if request.method in ("GET", "HEAD") and query_token and hmac.compare_digest(query_token, token):
        q = dict(request.query)
        q.pop("token", None)
        resp = web.HTTPFound(request.rel_url.with_query(q))
        resp.set_cookie(_COOKIE_NAME, token, max_age=30 * 86400,
                        httponly=True, samesite="Lax")
        return resp

    accept = request.headers.get("Accept", "")
    want_json = (path.startswith("/api/") or "application/json" in accept
                 or path.endswith("/stream"))
    if want_json:
        return web.json_response({"ok": False, "error": "未登录或口令失效"},
                                 status=401)
    raise web.HTTPFound("/login?next=" + quote(request.rel_url.path_qs))


@routes.get("/login")
async def login_page(request: web.Request) -> web.Response:
    cfg, *_ = await _ctx(request)
    token = str((cfg.web or {}).get("access_token") or "").strip()
    if not token:
        raise web.HTTPFound("/")
    err = "口令错误" if request.query.get("err") else ""
    html = T.login_page(_safe_next(request.query.get("next")), err)
    return web.Response(text=html, content_type="text/html", charset="utf-8")


@routes.post("/login")
async def login_submit(request: web.Request) -> web.Response:
    cfg, *_ = await _ctx(request)
    token = str((cfg.web or {}).get("access_token") or "").strip()
    form = await request.post()
    next_url = _safe_next(str(form.get("next") or "/"))
    supplied = str(form.get("token") or "").strip()
    if token and supplied and hmac.compare_digest(supplied, token):
        resp = web.HTTPFound(next_url)
        resp.set_cookie(_COOKIE_NAME, token, max_age=30 * 86400,
                        httponly=True, samesite="Lax")
        return resp
    raise web.HTTPFound("/login?err=1&next=" + quote(next_url))


@routes.post("/logout")
async def logout(request: web.Request) -> web.Response:
    resp = web.HTTPFound("/login")
    resp.del_cookie(_COOKIE_NAME)
    return resp


def lan_ips() -> list[str]:
    """枚举本机局域网 IPv4，供手机/其他电脑访问。

    优先用「连接外部地址」拿默认出口 IP（不会真的发包），再补充 hostname
    解析出的其他地址，覆盖多网卡/虚拟网卡的情况。全部失败返回空列表。
    """
    import socket as _s
    ips: list[str] = []
    try:
        s = _s.socket(_s.AF_INET, _s.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))   # 仅用于选路，不产生流量
            ip = s.getsockname()[0]
            if ip and not ip.startswith("127."):
                ips.append(ip)
        finally:
            s.close()
    except Exception:
        pass
    try:
        for ip in _s.gethostbyname_ex(_s.gethostname())[2]:
            if ip not in ips and not ip.startswith("127."):
                ips.append(ip)
    except Exception:
        pass
    return ips


class WebServer:
    def __init__(self, cfg: Settings, db: Database):
        self.cfg = cfg
        self.db = db
        # client_max_size: 上传上限（默认 2GB）。群文件与录音录像可能较大，
        # 超限 aiohttp 直接返回 413 且日志不明显，容易被误判为"上传失败没反应"。
        # 可用 config.yaml 的 web.max_upload_mb 调整（0=不限制）。
        max_mb = int((cfg.web or {}).get("max_upload_mb", 2048) or 0)
        # 注意：0 不能直接传给 aiohttp。请求体层（web_request.py）判的是
        # `if self._client_max_size:`，0 确实等于不限制；但 multipart 分片层
        # （multipart.py 的 MultipartReader）判的是
        # `if len(data) > self._client_max_size:`，没有真值保护 —— 传 0 会让
        # /kb/upload、/kb/ingest、/api/recordings/upload 对任何非空文件直接 413，
        # 与"0=不限制"的承诺相反。所以 0（含负数）统一换成 4GB 这个明确的大值，
        # 正数路径保持原样。
        self.max_upload_bytes = (max_mb * 1024 * 1024 if max_mb > 0
                                 else 4 * 1024 ** 3)
        self.app = web.Application(
            client_max_size=self.max_upload_bytes,
            middlewares=[_auth_middleware, _csrf_middleware])
        self.app.add_routes(routes)
        # 注入依赖
        self.app["cfg"] = cfg
        self.app["db"] = db
        self.app["server"] = self
        self._conn_ok = False
        self._runner: web.AppRunner | None = None
        # config.yaml 的 mtime：外部进程（自动同步守护进程、白名单自动规则）
        # 改过配置时，请求方按这个指纹决定是否重载
        self._cfg_mtime = self._config_mtime()
        # 拖拽导入后台任务：task_id → {status, total, done, results, error}
        self.ingest_tasks: dict[str, dict] = {}
        # 定时自动同步的后台任务句柄（取消用）
        self._auto_sync_task: asyncio.Task | None = None

    def _config_mtime(self) -> float:
        try:
            return self.cfg.path.stat().st_mtime
        except OSError:
            return 0.0

    def refresh_config(self) -> Settings:
        """config.yaml 被外部进程改过时重载内存配置。

        Web 进程启动时把配置读进内存；而白名单自动规则是在同步流程里
        改写 config.yaml 的（另一个进程或另一个线程）。不重载的话设置页
        永远显示旧名单，点「从本机 QQ 库同步」也会继续用旧名单。
        """
        mtime = self._config_mtime()
        if not mtime or mtime == self._cfg_mtime:
            return self.cfg
        try:
            fresh = load_config(self.cfg.path)
        except Exception as e:  # noqa: BLE001
            log.warning("重载配置失败，沿用内存配置：%s: %s",
                        type(e).__name__, e)
            return self.cfg
        self.cfg = fresh
        self.app["cfg"] = fresh
        self._cfg_mtime = mtime
        log.info("配置已从磁盘重载：%s", fresh.path)
        return fresh

    def set_connected(self, ok: bool) -> None:
        self._conn_ok = ok

    @property
    def connected(self) -> bool:
        return self._conn_ok

    async def start(self) -> None:
        self._runner = web.AppRunner(self.app)
        await self._runner.setup()
        host = self.cfg.web["host"]
        port = self.cfg.web["port"]
        site = web.TCPSite(self._runner, host, port)
        await site.start()
        token_on = bool(str((self.cfg.web or {}).get("access_token") or "").strip())
        if host in ("0.0.0.0", "::"):
            # 绑定全部网卡：本机与同局域网的手机/电脑都能访问
            log.info("看板已启动（局域网可访问）：")
            log.info("  本机    http://127.0.0.1:%s", port)
            for ip in lan_ips():
                log.info("  局域网  http://%s:%s", ip, port)
            if token_on:
                log.info("  已开启访问口令：首次访问会跳转 /login")
            else:
                log.warning("  ⚠ 绑定 0.0.0.0 且未设 web.access_token：同网段任何设备"
                            "都能直接查看聊天记录。请设置口令，或把 web.host 改回 127.0.0.1")
        else:
            log.info("看板已启动: http://%s:%s", host, port)
            if token_on:
                log.info("  已开启访问口令：首次访问会跳转 /login")
        self._start_auto_sync()

    async def stop(self) -> None:
        if self._auto_sync_task:
            self._auto_sync_task.cancel()
            try:
                await self._auto_sync_task
            except (asyncio.CancelledError, Exception):
                pass
            self._auto_sync_task = None
        if self._runner:
            await self._runner.cleanup()

    # ---------- 定时自动同步 ----------
    def _start_auto_sync(self) -> None:
        """按 knowledge.auto_sync_minutes 配置的间隔启动后台自动同步。

        0 或未配置 = 关闭。间隔 <1 分钟按 1 分钟处理，避免过度频繁撞 QQ 写库。
        """
        # 独立守护进程在跑时让位：它已负责定时同步，避免双进程撞 WAL
        from ..autosync_daemon import daemon_running
        if daemon_running(self.cfg):
            log.info("检测到独立自动同步守护进程在运行，WebUI 内置定时器让位")
            return
        try:
            minutes = float(self.cfg.knowledge.get("auto_sync_minutes") or 0)
        except (TypeError, ValueError):
            minutes = 0
        if minutes <= 0:
            log.info("自动同步未启用（knowledge.auto_sync_minutes=0）")
            return
        interval = max(60.0, minutes * 60.0)  # 下限 1 分钟
        self._auto_sync_task = asyncio.create_task(self._auto_sync_loop(interval))
        log.info("自动同步已启动：每 %.1f 分钟从本机 QQ 库同步一次", interval / 60)

    async def _auto_sync_loop(self, interval: float) -> None:
        """周期性触发同步。异常被捕获并记录，不中断循环。"""
        from ..pipeline import start_sync
        # 启动后先等一个周期再跑，避免与手动操作/启动初始化抢资源
        await asyncio.sleep(interval)
        while True:
            try:
                # 每轮重载配置：设置页的白名单修改、自动规则写回的新名单
                # 都要在下一轮生效，不能等重启 WebUI
                self.refresh_config()
                # 守护进程可能在 WebUI 启动之后才起来（或反之），每轮都要重新让位，
                # 否则两个进程会同时跑同步（审计批次 3 ⑲）
                from ..autosync_daemon import daemon_running
                if daemon_running(self.cfg):
                    log.info("检测到自动同步守护进程已启动，WebUI 内置定时器退出")
                    return
                tid = await asyncio.to_thread(start_sync, self.db, self.cfg)
                log.info("自动同步任务已启动: %s", tid)
                # 等这轮同步大致落库后，再从当天全量群聊抽取日程
                asyncio.create_task(self._chat_schedule_later())
            except RuntimeError as e:
                # 已有同步任务在跑（用户手动点了同步），本次跳过
                log.info("自动同步跳过：%s", e)
            except Exception as e:
                log.warning("自动同步触发失败：%s: %s", type(e).__name__, e)
            await asyncio.sleep(interval)

    async def _chat_schedule_later(self) -> None:
        """给同步任务留出落库时间，再执行群聊日程抽取（守护进程存在时跳过）。"""
        try:
            await asyncio.sleep(60)
            from ..autosync_daemon import daemon_running
            if daemon_running(self.cfg):
                return
            from ..chat_schedule import extract_today
            res = await asyncio.to_thread(extract_today, self.db, self.cfg)
            if not res.get("ok"):
                log.warning("群聊日程抽取未完成：%s", res.get("error"))
            from ..file_schedule import extract_files
            fs = await asyncio.to_thread(extract_files, self.db, self.cfg)
            if fs.get("inserted"):
                log.info("文件日程抽取：%s 个文件 → %s 条", fs.get("files"), fs.get("inserted"))
            elif fs.get("error") and "未启用" not in fs["error"]:
                log.warning("文件日程抽取未完成：%s", fs["error"])
        except Exception as e:  # noqa: BLE001
            log.warning("群聊日程抽取失败：%s: %s", type(e).__name__, e)


async def _ctx(request: web.Request) -> tuple[Settings, Database, None]:
    # 每个请求先按 config.yaml 的 mtime 判断是否需要重载（自动白名单由
    # 同步流程改写配置，不重载的话页面与手动同步都会用旧名单）
    srv = request.app.get("server")
    if srv is not None:
        srv.refresh_config()
    return request.app["cfg"], request.app["db"], None


async def _param(request: web.Request, key: str) -> str:
    """读参数：优先 query，其次 POST 表单。

    破坏性操作从 GET 改成 POST 后，页面上的 `?ocr=rebuild` 变成隐藏表单字段；
    保留 query 兼容书签/脚本。
    """
    val = request.query.get(key)
    if val is not None:
        return val
    if request.method == "POST":
        try:
            form = await request.post()
        except Exception:  # noqa: BLE001
            return ""
        val = form.get(key)
    return str(val) if val is not None else ""


async def _groups(db: Database) -> list[dict]:
    return db.get_groups()


def _index_now(db: Database, cfg: Settings) -> dict:
    """同步更新向量索引（阻塞，调用方用 to_thread 包起来）。

    失败要 note_index_result()，让后台同步下一轮退避重试（审计批次 2 ⑮）。
    """
    from ..llm.client import build_client
    from ..llm.indexer import Indexer
    from ..pipeline import note_index_result
    try:
        client = build_client(cfg, "embedding")
        client._require()
        res = Indexer(db, cfg, client).build(force=False)
        if res.get("errors"):
            note_index_result(cfg, False)
            return {"ok": False, **res,
                    "error": f"{res['errors']} 个文件向量化失败（其余已更新）"}
        note_index_result(cfg, True)
        return {"ok": True, **res}
    except Exception as e:  # noqa: BLE001
        note_index_result(cfg, False)
        log.warning("索引更新失败：%s", e)
        return {"ok": False, "error": str(e)[:200]}


def _to_int(s, default: int | None = None) -> int | None:
    """安全转 int：非法值返回 default，避免 int() 抛异常导致 500。"""
    try:
        return int(str(s).strip())
    except (TypeError, ValueError):
        return default


def _page(db: Database, title: str, content: str, active: str = "",
          groups: list | None = None, **kw) -> str:
    """包装 T.page：自动附带侧边栏会话列表。

    所有页面都应走这里，这样「AI 对话」区在任意页面都可见，
    无需每个路由重复传 conversations。
    """
    kw.setdefault("conversations", db.list_conversations(30))
    return T.page(title, content, active, groups or [], **kw)


def _to_date(s, default: str | None = None) -> str | None:
    """安全校验 YYYY-MM-DD：非法日期返回 default，避免 strptime 抛异常导致 500。"""
    s = (s or "").strip()
    if not s:
        return default
    try:
        return datetime.strptime(s, "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        return default


# ---------- 总览 ----------
@routes.get("/")
async def overview(request: web.Request) -> web.Response:
    cfg, db, _ = await _ctx(request)
    stats = db.get_stats()
    html = T.overview_page(stats, request.app["server"].connected, _media_names(cfg),
                           _autosync_status(cfg, request))
    return web.Response(text=_page(db, "总览", html, "overview", await _groups(db)),
                        content_type="text/html", charset="utf-8")


# ---------- 日历（录音日程） ----------
def _sched_date(raw, fallback: str | None) -> str | None:
    """把 AI 抽取的日期归一成 YYYY-MM-DD；容忍 M月D日 / MM-DD。"""
    s = str(raw or "").strip()
    if not s:
        return None
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", s)
    if m:
        return s
    year = None
    if fallback:
        try:
            year = int(str(fallback)[:4])
        except ValueError:
            year = None
    m = re.match(r"^(\d{1,2})月(\d{1,2})日$", s)
    if m and year:
        return f"{year:04d}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    m = re.match(r"^(\d{1,2})-(\d{1,2})$", s)
    if m and year:
        return f"{year:04d}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    return None


def _cal_conf(raw) -> float:
    """置信度转 float。坏值/缺列一律当 1.0，避免把老数据误标成「待确认」。"""
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 1.0


def _calendar_events(cfg: Settings, db: Database) -> list[dict]:
    out: list[dict] = []
    for row in db.list_schedule_items():
        day = _sched_date(row.get("date"), None)
        if not day:
            continue
        src = row.get("source_type") or ""
        link = ""
        if src == "chat":
            link = str(row.get("source_ref") or "")
        elif src == "recording":
            link = str(row.get("source_ref") or "")
        if not link:
            link = str(row.get("source_ref") or "")
        out.append({
            # id / confidence / manual 供手动编辑与「待确认」徽标使用（模板侧只读）
            "id": _to_int(row.get("id")),
            "confidence": _cal_conf(row.get("confidence")),
            "manual": bool(row.get("manual") or 0),
            "date": day,
            "time": str(row.get("time") or "").strip(),
            "title": str(row.get("title") or "").strip(),
            "detail": str(row.get("detail") or "").strip(),
            "quote": str(row.get("source_quote") or "").strip(),
            "source_label": str(row.get("source_label") or "").strip(),
            "source_type": src,
            "link": link,
        })
    out.sort(key=lambda x: (x["date"], x["time"] or "99:99"))
    return out


@routes.get("/calendar")
async def calendar_view(request: web.Request) -> web.Response:
    cfg, db, _ = await _ctx(request)
    today = _date.today()
    month = (request.query.get("month") or today.strftime("%Y-%m")).strip()
    if not re.match(r"^\d{4}-(0[1-9]|1[0-2])$", month):
        month = today.strftime("%Y-%m")
    year, mon = int(month[:4]), int(month[5:7])
    # day 是页面自己的参数；date 是兼容写法（手动编辑后的 303 回跳用 ?date=YYYY-MM-DD）
    day_q = (request.query.get("day") or request.query.get("date") or "").strip()
    try:
        selected = _date.fromisoformat(day_q)
        if selected.strftime("%Y-%m") != month:
            selected = None
    except ValueError:
        selected = None

    events = _calendar_events(cfg, db)
    by_day: dict[str, list[dict]] = {}
    for ev in events:
        by_day.setdefault(ev["date"], []).append(ev)

    if selected is None:
        if today.strftime("%Y-%m") == month:
            selected = today
        elif by_day:
            first = min(d for d in by_day if d[:7] == month) if any(
                d[:7] == month for d in by_day) else None
            selected = _date.fromisoformat(first) if first else _date(year, mon, 1)
        else:
            selected = _date(year, mon, 1)

    weeks = _calendar.Calendar(firstweekday=0).monthdatescalendar(year, mon)
    first = _date(year, mon, 1)
    prev_m = (first - timedelta(days=1)).strftime("%Y-%m")
    nxt = _date(year + (mon == 12), 1 if mon == 12 else mon + 1, 1)
    next_m = nxt.strftime("%Y-%m")
    html = T.calendar_page(
        month_label=month,
        prev_month=prev_m,
        next_month=next_m,
        weeks=weeks,
        events_by_day=by_day,
        selected_day=selected.isoformat(),
        selected_events=by_day.get(selected.isoformat(), []),
        today=today.isoformat(),
        notice=(request.query.get("cal_ok") or "").strip(),
        error=(request.query.get("cal_err") or "").strip(),
    )
    return web.Response(text=_page(db, "日历", html, "calendar", await _groups(db)),
                        content_type="text/html", charset="utf-8")


_CAL_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
_CAL_TITLE_MAX = 120
_CAL_DETAIL_MAX = 2000


def _cal_back_url(day: str, err: str = "", ok: str = "") -> str:
    """回日历页的 303 目标：固定 /calendar 路径 + 已校验日期，天然没有开放重定向。"""
    day = _to_date(day, "") or _date.today().isoformat()
    url = f"/calendar?month={day[:7]}&day={quote(day)}"
    if err:
        url += "&cal_err=" + quote(err, safe="")
    if ok:
        url += "&cal_ok=" + quote(ok, safe="")
    return url


async def _cal_form(request: web.Request) -> dict[str, str]:
    """读编辑表单。_param 先 query 后表单，所以 ?date= 这类写法也能用。"""
    return {
        "date": (await _param(request, "date") or "").strip(),
        "time": (await _param(request, "time") or "").strip(),
        "title": (await _param(request, "title") or "").strip(),
        "detail": (await _param(request, "detail") or "").strip(),
        "back": (await _param(request, "back") or "").strip(),
    }


def _cal_validate(fields: dict[str, str]) -> tuple[dict | None, str]:
    """校验并归一表单；返回 (干净字段, 错误文案)，错误文案为空表示通过。"""
    day = _to_date(fields.get("date"), "")
    if not day:
        return None, "日期要填 2026-10-31 这种格式。"
    title = fields.get("title") or ""
    if not title:
        return None, "「事项」不能为空。"
    if len(title) > _CAL_TITLE_MAX:
        title = title[:_CAL_TITLE_MAX]
    t = fields.get("time") or ""
    if t:
        m = _CAL_TIME_RE.match(t)
        if not m:
            return None, "时间要填 08:30 这种格式，或者留空。"
        t = f"{int(m.group(1)):02d}:{m.group(2)}"
    detail = fields.get("detail") or ""
    if len(detail) > _CAL_DETAIL_MAX:
        detail = detail[:_CAL_DETAIL_MAX]
    return {"date": day, "time": t, "title": title, "detail": detail}, ""


@routes.post("/calendar/item/new")
async def calendar_item_new(request: web.Request) -> web.Response:
    """手动新增一条日程（POST：写库必须走同源校验，见 _csrf_middleware）。"""
    cfg, db, _ = await _ctx(request)
    f = await _cal_form(request)
    back = _to_date(f["back"], "") or _to_date(f["date"], "") or \
        _date.today().isoformat()
    item, err = _cal_validate(f)
    if err:
        return web.HTTPSeeOther(_cal_back_url(back, err=err))
    try:
        db.add_schedule_manual(**item)
    except Exception as e:  # noqa: BLE001  存储层不可用时也不该 500
        log.warning("新增日程失败：%s", e)
        return web.HTTPSeeOther(_cal_back_url(back, err="保存失败，请稍后再试。"))
    return web.HTTPSeeOther(_cal_back_url(item["date"], ok="已添加日程。"))


@routes.post("/calendar/item/{item_id}/edit")
async def calendar_item_edit(request: web.Request) -> web.Response:
    """修改一条日程（手动条目直接改；AI 抽出的条目改后同样不再被重建覆盖）。"""
    cfg, db, _ = await _ctx(request)
    item_id = _to_int(request.match_info.get("item_id"))
    f = await _cal_form(request)
    back = _to_date(f["back"], "") or _to_date(f["date"], "") or \
        _date.today().isoformat()
    if item_id is None:
        return web.HTTPSeeOther(_cal_back_url(back, err="这条日程不存在（可能已被删除）。"))
    item, err = _cal_validate(f)
    if err:
        return web.HTTPSeeOther(_cal_back_url(back, err=err))
    try:
        ok = db.update_schedule_manual(item_id, **item)
    except Exception as e:  # noqa: BLE001
        log.warning("修改日程 #%s 失败：%s", item_id, e)
        return web.HTTPSeeOther(_cal_back_url(back, err="保存失败，请稍后再试。"))
    if not ok:
        return web.HTTPSeeOther(_cal_back_url(back, err="这条日程不存在（可能已被删除）。"))
    return web.HTTPSeeOther(_cal_back_url(item["date"], ok="已保存修改。"))


@routes.post("/calendar/item/{item_id}/delete")
async def calendar_item_delete(request: web.Request) -> web.Response:
    """删除一条日程。dismiss=True：同时记进「已忽略」表，自动抽取不会再加回来。"""
    cfg, db, _ = await _ctx(request)
    item_id = _to_int(request.match_info.get("item_id"))
    back = _to_date(await _param(request, "back"), "") or _date.today().isoformat()
    if item_id is None:
        return web.HTTPSeeOther(_cal_back_url(back, err="这条日程不存在（可能已被删除）。"))
    try:
        ok = db.delete_schedule_item(item_id, dismiss=True)
    except Exception as e:  # noqa: BLE001
        log.warning("删除日程 #%s 失败：%s", item_id, e)
        return web.HTTPSeeOther(_cal_back_url(back, err="删除失败，请稍后再试。"))
    if not ok:
        return web.HTTPSeeOther(_cal_back_url(back, err="这条日程不存在（可能已被删除）。"))
    return web.HTTPSeeOther(_cal_back_url(back, ok="已删除，且不会再被自动抽取加回来。"))


# ---------- 群消息 ----------
@routes.get("/messages")
async def messages(request: web.Request) -> web.Response:
    cfg, db, _ = await _ctx(request)
    groups = await _groups(db)
    q = request.query
    group_id = _to_int(q.get("group_id"))
    keyword = q.get("keyword", "").strip()
    user_id = _to_int(q.get("user_id"))
    important_only = bool(q.get("important_only"))
    date_from = _to_date(q.get("date_from"))
    date_to = _to_date(q.get("date_to"))
    limit = 50
    offset = max(0, _to_int(q.get("offset"), 0) or 0)

    messages_ = db.query_messages(group_id=group_id, keyword=keyword, user_id=user_id,
                                  important_only=important_only, date_from=date_from,
                                  date_to=date_to, limit=limit, offset=offset)
    total = db.query_messages(group_id=group_id, keyword=keyword, user_id=user_id,
                              important_only=important_only, date_from=date_from,
                              date_to=date_to, limit=100000, offset=0)
    senders = db.distinct_senders(group_id) if group_id else []
    filters = {"keyword": keyword, "user_id": user_id, "important_only": important_only,
               "date_from": date_from or "", "date_to": date_to or ""}
    html = T.messages_page(groups, group_id, messages_, senders, filters,
                           len(total), offset, limit, _media_names(cfg))
    return web.Response(text=_page(db, "群消息", html, "messages", groups,
                                    cur_group=group_id),
                        content_type="text/html", charset="utf-8")


@routes.get("/msg/{msg_id}")
async def message_detail(request: web.Request) -> web.Response:
    cfg, db, _ = await _ctx(request)
    mid = _to_int(request.match_info["msg_id"])
    if mid is None:
        raise web.HTTPNotFound()
    m = db.get_message(mid)
    if not m:
        raise web.HTTPNotFound()
    group = db.get_group(m["group_id"])
    html = T.message_detail_page(m, group, m["important_reason"] or "-",
                                 _media_names(cfg))
    return web.Response(text=_page(db, "消息详情", html, "messages", await _groups(db)),
                        content_type="text/html", charset="utf-8")


@routes.post("/mark/{msg_id}")
async def mark_important(request: web.Request) -> web.Response:
    """翻「重要」标记。

    必须是 POST：会写库，而 _csrf_middleware 只对 POST/PUT/PATCH/DELETE 做
    同源校验，GET 版本可以被任意跨站 <img>/预取触发。
    """
    cfg, db, _ = await _ctx(request)
    mid = _to_int(request.match_info["msg_id"])
    if mid is None:
        raise web.HTTPNotFound()
    # 页面用隐藏表单字段提交；_param 先 query 后表单，老书签 ?important=1 仍可用
    important = (await _param(request, "important")) == "1"
    db.set_important(mid, important)
    raise web.HTTPFound(f"/msg/{mid}")


# ---------- 知识库 ----------
def _kb_root(cfg: Settings) -> Path:
    return cfg.resolve(cfg.knowledge.get("dir", "data/knowledge"))


def _media_names(cfg: Settings) -> dict[int, set[str]]:
    """扫描 knowledge/media/，建立 群id → 已同步媒体文件名集合。

    供消息页判断 [图片:xxx] 的媒体是否已同步，决定渲染成图片还是提示文字。
    """
    idx: dict[int, set[str]] = {}
    mdir = _kb_root(cfg) / "media"
    if not mdir.is_dir():
        return idx
    for g in mdir.iterdir():
        if g.is_dir():
            try:
                gid = int(g.name)
            except ValueError:
                continue
            idx[gid] = {p.name for p in g.rglob("*") if p.is_file()}
    return idx


# 行数缓存：key=绝对路径, value=(size, mtime, 行数)。
# 避免每次打开 /kb 都把全部聊天 md 重读一遍（文件大时明显卡顿）。
_LINES_CACHE: dict[str, tuple[int, float, int]] = {}


def _count_lines(f: Path) -> int:
    """统计文本行数：正确关闭文件句柄，并按 (size, mtime) 缓存结果。"""
    try:
        st = f.stat()
    except OSError:
        return 0
    key = str(f)
    hit = _LINES_CACHE.get(key)
    if hit and hit[0] == st.st_size and hit[1] == st.st_mtime:
        return hit[2]
    try:
        # 必须用 with 关闭句柄：否则在 Windows 上会锁住文件导致无法删除，
        # 在非引用计数解释器上则直接句柄泄漏
        with f.open(encoding="utf-8", errors="replace") as fp:
            n = sum(1 for _ in fp)
    except OSError:
        n = 0
    if len(_LINES_CACHE) > 2000:  # 防止无限增长
        _LINES_CACHE.clear()
    _LINES_CACHE[key] = (st.st_size, st.st_mtime, n)
    return n


def _invalidate_lines_cache(path: Path) -> None:
    """文件被删除/改动后清掉对应缓存（含其子项，用于整目录删除）。"""
    key = str(path)
    for k in [k for k in _LINES_CACHE if k == key or k.startswith(key + "\\")
              or k.startswith(key + "/")]:
        _LINES_CACHE.pop(k, None)


def _kb_index(cfg: Settings) -> dict:
    """扫描知识库目录，返回各分区文件列表与统计。"""
    root = _kb_root(cfg)
    chat_files: list[dict] = []
    cdir = root / "chat"
    if cdir.is_dir():
        for f in sorted(cdir.glob("*.md")):
            try:
                gid = int(f.name.split("_", 1)[0])
            except ValueError:
                gid = 0
            chat_files.append({
                "name": f.name,
                "gid": gid,
                "size": f.stat().st_size,
                "lines": _count_lines(f),
            })
    media: dict[str, dict] = {}
    mroot = root / "media"
    media_total = 0
    if mroot.is_dir():
        for gdir in sorted(mroot.iterdir()):
            if not gdir.is_dir():
                continue
            files = [p for p in gdir.rglob("*") if p.is_file()]
            size = sum(p.stat().st_size for p in files)
            media_total += size
            media[gdir.name] = {
                "months": sorted({p.parent.name for p in files}),
                "files": len(files),
                "size": size,
            }
    documents = _dir_files(root / "documents")
    transcripts = _dir_files(root / "transcripts")
    doc_size = sum(f["size"] for f in documents)
    trs_size = sum(f["size"] for f in transcripts)
    chat_size = sum(f["size"] for f in chat_files)
    return {
        "root": str(root), "chat": chat_files, "media": media,
        "documents": documents, "transcripts": transcripts,
        "stats": {
            "chat": len(chat_files), "chat_size": chat_size,
            "documents": len(documents), "documents_size": doc_size,
            "transcripts": len(transcripts), "transcripts_size": trs_size,
            "media": sum(v["files"] for v in media.values()), "media_size": media_total,
            "total_size": chat_size + doc_size + trs_size + media_total,
        },
    }


def _dir_files(d: Path) -> list[dict]:
    """列出目录下文件：{name, size}，目录/隐藏文件跳过。"""
    if not d.is_dir():
        return []
    out = []
    for p in sorted(d.iterdir()):
        if p.is_file() and not p.name.startswith("."):
            out.append({"name": p.name, "size": p.stat().st_size, "mtime": p.stat().st_mtime})
    return out


def _safe_child(root: Path, name: str) -> Path | None:
    """把 name 解析到 root 之内，防止路径穿越。"""
    try:
        path = (root / name).resolve()
    except OSError:
        return None
    root = root.resolve()
    if os.path.commonpath([str(root).lower(), str(path).lower()]) != str(root).lower():
        return None
    return path


@routes.get("/kb")
async def kb_index(request: web.Request) -> web.Response:
    cfg, db, _ = await _ctx(request)
    q = request.query
    msg, tone = "", ""
    if q.get("uploaded"):
        msg = (f"已上传 {q['uploaded']} 个文件："
               f"{q.get('docs', 0)} 个分拣进 documents/，"
               f"{q.get('trs', 0)} 个分拣进 transcripts/")
        tone = "ok"
    elif q.get("synced"):
        # 这里原来有两份 `elif q.get("synced")`（旧的"重新同步完成"与带 OCR 统计
        # 的"同步完成"），后一份永远不可达 → OCR 统计提示从没显示过。
        # 合并成一段：/kb/sync 只回这一种 URL，参数为 chat/media/docs/trs/
        # ocr_total/ocr_text（见 kb_sync 的 HTTPFound）。
        msg = (f"同步完成：重建 {q.get('chat', 0)} 个聊天文件，"
               f"补同步媒体 {q.get('media', 0)} 个，"
               f"分拣文档 {q.get('docs', 0)} 个、文字稿 {q.get('trs', 0)} 个；"
               f"图片 OCR 共 {q.get('ocr_total', 0)} 张，"
               f"其中 {q.get('ocr_text', 0)} 张有文字")
        tone = "ok"
    elif q.get("msg") == "no":
        msg = "没有收到文件，请先选择要上传的文件"
        tone = "warn"
    elif q.get("deleted"):
        msg = f"已删除：{q['deleted']}"
        tone = "ok"
    elif q.get("group_deleted"):
        msg = f"已删除该群全部记录（含 {q.get('n', 0)} 条消息、聊天文件与该群媒体）"
        tone = "ok"
    elif q.get("url_ok"):
        msg = (f"网页已抓取为 Markdown：{unquote(q.get('name', ''))}"
               f"（{q.get('chars', 0)} 字，{q.get('imgs', 0)} 张图片已保存到 media/）")
        tone = "ok"
    elif q.get("url_err"):
        msg = f"网页抓取失败：{unquote(q['url_err'])}"
        tone = "warn"
    elif q.get("qqsynced"):
        n = q.get("n", "0")
        if n == "0":
            msg = f"本机 QQ 库已是最新，没有新消息。{q.get('d', '')}"
        else:
            msg = (f"从本机 QQ 库同步了 {n} 条新消息（{q.get('g', 0)} 个群），"
                   f"知识库已重建。{q.get('d', '')}")
        tone = "ok"
    elif q.get("qqsync_error"):
        msg = f"本机 QQ 库同步失败：{q.get('qqsync_error')}"
        tone = "warn"
    elif q.get("gf") is not None:
        gf = db.group_files_stats()
        msg = (f"群文件扫描完成：本轮检查 {q.get('gf', 0)} 个，"
               f"提取文字 {q.get('gf_ing', 0)} 个、仅归档 {q.get('gf_arch', 0)} 个、"
               f"本机未找到 {q.get('gf_miss', 0)} 个。"
               f"累计：已入库 {gf.get('ingested', 0)}、仅归档 {gf.get('archived', 0)}、"
               f"未下载 {gf.get('missing', 0)}。")
        if q.get("idx_err"):
            msg += f" ⚠ 索引更新失败：{q['idx_err']}"
        tone = "warn" if q.get("idx_err") else "ok"
    elif q.get("reparsed"):
        n = q.get("n", "0")
        msg = (f"重新解析完成：检查 {q.get('c', 0)} 条消息，"
               f"补全 {n} 条图片文件名"
               + (f"，已重建知识库生成图片链接与 OCR。" if n != "0"
                  else "，没有需要修复的旧消息。"))
        tone = "ok" if n != "0" else "info"
    elif q.get("deduped"):
        n = q.get("deduped", "0")
        msg = (f"清理重复消息 {n} 条，知识库已重建。" if n != "0"
               else "没有发现重复消息。")
        tone = "ok" if n != "0" else "info"
    elif q.get("group_synced"):
        msg = f"单群重同步完成：聊天文件已重建，补同步媒体 {q.get('media', 0)} 个"
        tone = "ok"
    elif q.get("blocked"):
        msg = (f"群 {q['blocked']} 不在白名单中，已跳过同步。"
               f"如需同步该群，请到设置页把它加进白名单（或清空白名单 = 不限群）。")
        tone = "warn"
    elif q.get("saved"):
        msg = f"已保存 {q['saved']}"
        tone = "ok"
    tab = (q.get("tab") or "overview").strip()
    if tab not in ("overview", "group-files", "recordings", "documents",
                   "media", "chat", "search"):
        tab = "overview"
    if q.get("gf") is not None or q.get("gf_saved") is not None:
        tab = "group-files"
    gf_status = (q.get("gf_status") or "").strip()
    if gf_status not in ("", "pending", "missing", "ingested", "archived",
                         "skipped", "duplicate", "error"):
        gf_status = ""
    gf_q = (q.get("gf_q") or "").strip()
    try:
        gf_page = max(1, int(q.get("gf_page") or 1))
    except ValueError:
        gf_page = 1
    gf_limit = 20
    gf_total = db.count_group_files(status=gf_status or None, q=gf_q)
    gf_pages = max(1, (gf_total + gf_limit - 1) // gf_limit)
    gf_page = min(gf_page, gf_pages)
    gf_rows = db.list_group_files(status=gf_status or None, q=gf_q,
                                  limit=gf_limit, offset=(gf_page - 1) * gf_limit)
    groups = await _groups(db)
    def _pg(key: str) -> int:
        try:
            return max(1, int(q.get(key) or 1))
        except ValueError:
            return 1

    html = T.kb_page(_kb_index(cfg), msg, tone, meta_all=db.kb_all_meta(),
                     sync_state=_autosync_status(cfg, request),
                     group_names={g["group_id"]: g["name"] for g in groups},
                     group_files=db.group_files_stats(),
                     group_file_rows=gf_rows, tab=tab,
                     recordings=db.list_recordings(limit=200),
                     rec_stats=db.recordings_stats(),
                     doc_page=_pg("dpage"), trs_page=_pg("tpage"),
                     gf_filters={"status": gf_status, "q": gf_q,
                                 "page": gf_page, "pages": gf_pages,
                                 "total": gf_total})
    return web.Response(text=_page(db, "知识库", html, "kb", groups),
                        content_type="text/html", charset="utf-8")


def _restore_filename(name: str) -> str:
    """恢复被 percent-encoding 过的文件名。

    部分客户端/工具（如 aiohttp 的 FormData 默认会 quote_fields）会把
    非 ASCII 文件名编码成 %E6%A0%A1... 形式；浏览器上传的是原始 UTF-8，
    不受影响。解码结果若引入路径分隔符/控制字符则放弃，保持原样。
    """
    if "%" not in name:
        return name
    decoded = unquote(name)
    if not any(ch in decoded for ch in "/\\\x00\x01"):
        return decoded
    return name


@routes.post("/kb/upload")
async def kb_upload(request: web.Request) -> web.Response:
    """接收文件 → 存 inbox → 自动分拣到 documents/ 或 transcripts/。"""
    cfg, db, _ = await _ctx(request)
    form = await request.post()
    inbox = cfg.resolve(cfg.storage["inbox_dir"])
    inbox.mkdir(parents=True, exist_ok=True)
    saved: list[str] = []
    for part in form.values():
        if not isinstance(part, web.FileField) or not part.filename:
            continue
        fname = _restore_filename(Path(part.filename).name)  # basename + 解码，防路径穿越
        if not fname:
            continue
        target = inbox / fname
        if target.exists():
            stem, ext = Path(fname).stem, Path(fname).suffix
            target = inbox / f"{stem}_{datetime.now().strftime('%H%M%S')}{ext}"
        with open(target, "wb") as out:
            while True:
                chunk = part.file.read(1 << 20)  # 1MB 分块，防大文件占内存
                if not chunk:
                    break
                out.write(chunk)
        saved.append(target.name)
    if not saved:
        raise web.HTTPFound("/kb?msg=no")
    result = sync_inbox(cfg, _kb_root(cfg))
    raise web.HTTPFound(
        f"/kb?uploaded={len(saved)}&docs={result['documents']}&trs={result['transcripts']}")


@routes.post("/kb/from-url")
async def kb_from_url(request: web.Request) -> web.Response:
    """抓取网页（支持微信公众号）→ 转成 Markdown 存知识库。

    产物：md 存 transcripts/，图片存 media/web_<标题>/（网页可直接显示）。
    """
    cfg, db, _ = await _ctx(request)
    form = await request.post()
    url = (form.get("url") or "").strip()
    if not url:
        raise web.HTTPFound("/kb?url_err=" + quote("请填写链接"))
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    from ..webpage import fetch_markdown, slugify

    kdir = _kb_root(cfg)
    (kdir / "transcripts").mkdir(parents=True, exist_ok=True)
    media_root = kdir / "media"

    payload = None
    error = ""
    try:
        # 先抓一次拿标题（图片下载放第二次，避免目录名未知）
        probe = await asyncio.to_thread(fetch_markdown, url, None, "", False)
        if not probe["ok"]:
            raise RuntimeError(probe["error"] or "抓取失败")

        title = probe["title"] or "网页"
        slug = slugify(title)
        # 同名标题用时间后缀隔离，避免两次抓取互相覆盖图片（审计 P0-10）
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        media_dir = media_root / f"web_{slug}_{stamp}"
        # 完整抓取（含图片下载）
        res = await asyncio.to_thread(fetch_markdown, url, media_dir, "../media", True)
        if not res["ok"]:
            raise RuntimeError(res["error"] or "抓取失败")

        body = res["markdown"]
        # 标题 + 来源，便于 AI 判断时效性
        md = (f"# {title}\n\n"
              f"> 来源：{url}（网页抓取，{datetime.now().strftime('%Y-%m-%d')}）\n\n"
              f"{body}")

        fname = f"web_{slug}_{stamp}.md"
        target = kdir / "transcripts" / fname
        target.write_text(md, encoding="utf-8")
        log.info("网页抓取入库：%s（%d 字，%d 张图）", target.name, len(md),
                 res["images"])
        payload = (target.name, len(md), res["images"])
    except Exception as e:
        log.warning("网页抓取失败 %s: %s", url, e)
        error = str(e)[:200]

    # 重定向必须放在 try 之外：web.HTTPFound 本身是异常，
    # 放在 try 里会被自己的 except 捕获成「抓取失败」（审计 P0-10）
    if payload:
        raise web.HTTPFound(
            f"/kb?url_ok=1&name={quote(payload[0])}"
            f"&chars={payload[1]}&imgs={payload[2]}")
    raise web.HTTPFound("/kb?url_err=" + quote(error or "抓取失败"))


@routes.post("/kb/dedup")
async def kb_dedup(request: web.Request) -> web.Response:
    """清理重复消息（解析格式升级后重导可能产生，见坑 #9），随后重建知识库。"""
    from ..importer import dedup_messages
    from ..knowledge import sync_knowledge

    cfg, db, _ = await _ctx(request)
    res = await asyncio.to_thread(dedup_messages, db)
    if res["removed"]:
        await asyncio.to_thread(sync_knowledge, db, cfg)
    raise web.HTTPFound(f"/kb?deduped={res['removed']}")


@routes.post("/kb/reparse")
async def kb_reparse(request: web.Request) -> web.Response:
    """重新解析旧消息：补全早期入库时丢失的图片文件名（裸 [图片] → [图片:xxx.jpg]）。

    早期提取器不支持 QQ 新版云 ID 格式，图片只存成裸占位符，没有链接也无法 OCR。
    用当前解析逻辑重跑一遍即可补全，随后需再点「重新同步」生成链接与 OCR。
    """
    cfg, db, _ = await _ctx(request)
    from ..qq_sync import reparse_messages
    from ..knowledge import sync_knowledge

    res = await asyncio.to_thread(reparse_messages, db, cfg)
    if not res["ok"]:
        raise web.HTTPFound(f"/kb?qqsync_error={quote(str(res.get('error', '')))}")
    # 补全文件名后重建一次，让链接与 OCR 立即生效
    if res["updated"]:
        await asyncio.to_thread(sync_knowledge, db, cfg)
    raise web.HTTPFound(
        f"/kb?reparsed=1&n={res['updated']}&c={res['checked']}")


def _guard_daemon(cfg: Settings) -> None:
    """跨进程互斥：独立自动同步守护进程在跑时让位。

    守护进程与 WebUI 可能同时开着。pipeline 的任务锁只在进程内生效，
    防不了另一个进程正在解密/写库/加载 7GB 模型，所以手动同步入口
    统一先检测守护进程，在跑就提示让位，避免双进程撞 SQLite WAL。
    """
    from ..autosync_daemon import daemon_running

    if daemon_running(cfg):
        raise web.HTTPFound("/kb?qqsync_error=" + quote(
            "自动同步守护进程正在后台运行，已接管定时同步，无需手动触发。"
            "如需手动同步，请先运行 sync-daemon-stop.bat 停止守护进程。"))


def _autosync_status(cfg: Settings, request: web.Request) -> dict:
    """汇总自动同步状态：守护进程（锁文件 + 状态文件）/ WebUI 内置定时器。"""
    import json

    from ..autosync_daemon import STATUS_NAME, daemon_running

    st: dict = {"daemon": daemon_running(cfg)}
    p = cfg.resolve(cfg.storage.get("db_path", "data/qqmonitor.db")).parent / STATUS_NAME
    if p.exists():
        try:
            st.update(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
    web_srv = request.app.get("server")
    st["webui_timer"] = bool(web_srv and getattr(web_srv, "_auto_sync_task", None))
    return st


@routes.post("/kb/sync-qq")
async def kb_sync_qq(request: web.Request) -> web.Response:
    """启动完整同步任务（后台执行）：解密提取 → 入库 → 媒体 → OCR → 渲染。

    返回任务 ID，前端轮询 /kb/sync-status 看进度。不再同步阻塞，
    几百张图 OCR 也不会卡界面。同时间只允许一个任务。
    """
    cfg, db, _ = await _ctx(request)
    _guard_daemon(cfg)
    from ..pipeline import start_sync

    try:
        tid = await asyncio.to_thread(start_sync, db, cfg)
    except RuntimeError as e:
        raise web.HTTPFound(f"/kb?qqsync_error={quote(str(e))}")
    raise web.HTTPFound(f"/kb?sync_task={tid}")


@routes.get("/kb/sync-status")
async def kb_sync_status(request: web.Request) -> web.Response:
    """轮询同步任务进度。返回当前阶段、日志、是否完成。"""
    tid = request.query.get("tid", "")
    from ..pipeline import get_sync, task_to_dict

    task = get_sync(tid)
    return web.json_response(task_to_dict(task))


@routes.post("/kb/sync")
async def kb_sync(request: web.Request) -> web.Response:
    """完整重新同步：重建聊天 Markdown（含图片 OCR）+ 补媒体 + inbox 分拣。"""
    cfg, db, _ = await _ctx(request)
    _guard_daemon(cfg)
    force_ocr = (await _param(request, "ocr")) == "rebuild"
    result = await asyncio.to_thread(sync_knowledge, db, cfg,
                                     ocr_force=force_ocr)
    # 知识库文件变了必须补索引：旧实现只重建 md，不更新向量库（审计批次 2 ⑮）
    idx = await asyncio.to_thread(_index_now, db, cfg)
    chat_n = len(result["chat"])
    ocr_s = result.get("ocr", {})
    extra = "" if idx.get("ok") else f"&idx_err={quote(str(idx.get('error', ''))[:120])}"
    raise web.HTTPFound(
        f"/kb?synced=1&chat={chat_n}&media={result['media']['copied']}"
        f"&docs={result['inbox']['documents']}&trs={result['inbox']['transcripts']}"
        f"&ocr_total={ocr_s.get('total', 0)}&ocr_text={ocr_s.get('with_text', 0)}"
        f"&idx={idx.get('added', 0)}{extra}")


@routes.post("/kb/group-files/scan")
async def kb_group_files_scan(request: web.Request) -> web.Response:
    """扫描群文件消息：把本机已有副本的文件复制进知识库并提取文字。

    只利用本机文件，不联网下载；没下载过的文件会标成「未下载」，
    在 QQ 里下载后再点一次即可。
    """
    cfg, db, _ = await _ctx(request)
    from ..group_files import process_pending

    res = await asyncio.to_thread(process_pending, db, cfg, limit=50)
    idx = await asyncio.to_thread(_index_now, db, cfg)
    extra = "" if idx.get("ok") else f"&idx_err={quote(str(idx.get('error', ''))[:120])}"
    raise web.HTTPFound(
        f"/kb?gf={res['scanned']}&gf_ing={res['ingested']}"
        f"&gf_arch={res['archived']}&gf_miss={res['missing']}"
        f"&gf_skip={res['skipped']}&gf_dup={res['duplicate']}{extra}")


@routes.post("/kb/sync/{gid}")
async def kb_sync_group(request: web.Request) -> web.Response:
    """单群重同步：只重建该群聊天 Markdown + 补同步该群媒体（不动其他群）。"""
    cfg, db, _ = await _ctx(request)
    _guard_daemon(cfg)
    try:
        gid = int(request.match_info["gid"])
    except ValueError:
        raise web.HTTPNotFound()
    # 白名单统一生效：不在名单里的群不做同步（避免静默无效果，回页面提示）
    from ..watchlist import allows_conversation
    if not allows_conversation(cfg, gid):
        raise web.HTTPFound(f"/kb?blocked={gid}")
    force_ocr = (await _param(request, "ocr")) == "rebuild"
    await asyncio.to_thread(sync_chat_group, db, _kb_root(cfg), gid,
                            cfg=cfg, ocr_force=force_ocr)
    media = await asyncio.to_thread(sync_media, db, cfg, _kb_root(cfg),
                                    group_ids=[gid])
    idx = await asyncio.to_thread(_index_now, db, cfg)
    extra = "" if idx.get("ok") else "&idx_err=1"
    raise web.HTTPFound(f"/kb?group_synced=1&media={media['copied']}{extra}")


@routes.post("/kb/meta/date")
async def kb_meta_date(request: web.Request) -> web.Response:
    """设定知识库文件的事件日期（内容发生时间，非文件修改时间）。

    该日期决定文件被纳入哪个周/月/年总结，也影响问答的时间筛选。
    留空 = 清除日期，文件将不参与周期总结。
    """
    _, db, _ = await _ctx(request)
    form = await request.post()
    source = str(form.get("source") or "").strip()
    date_val = str(form.get("event_date") or "").strip()

    if not source or "/" not in source:
        return web.json_response({"ok": False, "error": "参数不合法"})
    section, _, name = source.partition("/")
    if section not in ("documents", "transcripts", "chat") or not name:
        return web.json_response({"ok": False, "error": "分区不合法"})
    if date_val:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_val):
            return web.json_response({"ok": False,
                                      "error": "日期格式应为 YYYY-MM-DD"})
        # 格式对但日期不存在（如 2026-13-45）也要拒：否则它永远匹配不到
        # 任何时间段，用户却以为设置成功了
        try:
            datetime.strptime(date_val, "%Y-%m-%d")
        except ValueError:
            return web.json_response({"ok": False,
                                      "error": f"{date_val} 不是有效日期"})

    if date_val:
        db.kb_set_meta(source, date_val, note="", auto=False)  # 手动设定优先级最高
    else:
        db.kb_del_meta(source)
    # 同步更新已索引片段的 day，避免必须重建索引才生效
    db.kb_set_chunks_day(source, date_val)
    log.info("设定事件日期 %s = %s", source, date_val or "（清除）")
    return web.json_response({"ok": True})


@routes.post("/kb/delete")
async def kb_delete(request: web.Request) -> web.Response:
    """知识库删除（带确认由前端负责）。

    section: chat | media | documents | transcripts
    - chat:    name 为聊天文件，整群删除（数据库消息 + 聊天 md + 该群媒体目录）
    - media:   scope=group 删整个群媒体；scope=month 删某月；scope=file 删单文件
    - documents/transcripts: 单文件删除
    """
    cfg, db, _ = await _ctx(request)
    form = await request.post()
    section = (form.get("section") or "").strip()
    name = (form.get("name") or "").strip()
    scope = (form.get("scope") or "file").strip()
    root = _kb_root(cfg)

    if section in ("documents", "transcripts"):
        path = _safe_child(root / section, name)
        if not path or not path.is_file():
            raise web.HTTPFound("/kb?msg=no")
        path.unlink()
        _invalidate_lines_cache(path)
        # 派生数据要一起删：否则 AI 还能检索到已删文件（审计 P0-2）
        db.kb_delete_source(f"{section}/{path.name}")
        db.kb_del_meta(f"{section}/{path.name}")
        if section == "transcripts" and path.suffix == ".md":
            db.ocr_del(f"transcripts/{path.name}")
        from ..pipeline import mark_dirty
        mark_dirty(cfg, "index")
        log.info("知识库删除 %s/%s（已清索引与元数据）", section, name)
        return web.json_response({"ok": True})

    if section == "chat":
        # 文件名形如 {gid}_{群名}.md。群号只作为「定位」用，必须同时满足：
        # 文件真实存在、文件名前缀能解析出群号、该群确实在数据库里。
        # 否则伪造成 "<任意群号>_x.md" 就能在文件不存在时整群删除（审计 P0-1）。
        path = _safe_child(root / "chat", name)
        if not path or not path.is_file():
            raise web.HTTPFound("/kb?msg=no")
        m = re.match(r"^(-?\d+)_", path.name)
        if not m:
            raise web.HTTPNotFound()
        gid = int(m.group(1))
        if not db.get_group(gid):
            raise web.HTTPFound("/kb?msg=no")
        from ..knowledge import purge_conversation
        res = await asyncio.to_thread(purge_conversation, db, cfg, gid)
        log.info("删除群 %s 全部记录（消息 %d、索引 %d）",
                 gid, res["messages"], res["chunks"])
        return web.json_response({"ok": True, "n": res["messages"]})

    if section == "media":
        path = _safe_child(root / "media", name)
        if not path:
            raise web.HTTPFound("/kb?msg=no")
        if scope in ("group", "month") and path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
            db.ocr_del_prefix(f"media/{name.strip('/')}/")
        elif path.is_file():
            path.unlink()
            db.ocr_del(f"media/{name}")
        else:
            raise web.HTTPFound("/kb?msg=no")
        # 媒体删了，聊天 md 里的图片/OCR 也得重渲染
        from ..pipeline import mark_dirty
        mark_dirty(cfg, "chat", "index")
        log.info("删除媒体 %s (%s，已清 OCR 缓存)", name, scope)
        return web.json_response({"ok": True})

    raise web.HTTPNotFound()


def _chat_title(db: Database, name: str) -> str:
    """聊天记录页标题：优先「群名 · 聊天记录」，取不到就退回文件名。"""
    try:
        gid = int(name.split("_", 1)[0])
    except (ValueError, IndexError):
        return "聊天记录"
    g = db.get_group(gid)
    if g and g.get("name"):
        return f"{g['name']} · 聊天记录"
    return "聊天记录"

@routes.get("/kb/chat/{name}")
async def kb_chat(request: web.Request) -> web.Response:
    cfg, db, _ = await _ctx(request)
    path = _safe_child(_kb_root(cfg) / "chat", request.match_info["name"])
    if not path or not path.is_file():
        raise web.HTTPNotFound()
    md = path.read_text(encoding="utf-8")
    html = T.kb_chat_page(path.name, md_to_html(md), str(path))
    return web.Response(text=_page(db, _chat_title(db, path.name), html, "kb", await _groups(db)),
                        content_type="text/html", charset="utf-8")


@routes.get("/kb/media/{tail:.*}")
async def kb_media_file(request: web.Request) -> web.Response:
    cfg, *_ = await _ctx(request)
    path = _safe_child(_kb_root(cfg) / "media", request.match_info["tail"])
    if not path or not path.is_file():
        raise web.HTTPNotFound()
    return web.FileResponse(path)


_ASSET_DIR = Path(__file__).resolve().parent / "assets"


@routes.get("/manifest.webmanifest")
async def web_manifest(request: web.Request) -> web.Response:
    """PWA manifest：手机「添加到主屏幕」后全屏运行，不再显示浏览器地址栏。"""
    data = {
        "name": "QQMonitor · 群档案",
        "short_name": "QQMonitor",
        "description": "QQ 群消息归档、检索与 AI 助手",
        "lang": "zh-CN",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "orientation": "portrait-primary",
        "background_color": "#EDF0EC",
        "theme_color": "#141815",
        "icons": [
            {"src": "/pwa/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
            {"src": "/pwa/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
            {"src": "/pwa/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
        ],
    }
    return web.json_response(data, headers={"Cache-Control": "public, max-age=3600"})


@routes.get("/pwa/icon-{size}.png")
async def pwa_icon(request: web.Request) -> web.Response:
    size = request.match_info["size"]
    if not size.isdigit():
        raise web.HTTPNotFound()
    path = _ASSET_DIR / f"icon-{size}.png"
    if not path.is_file():
        raise web.HTTPNotFound()
    return web.FileResponse(path, headers={"Cache-Control": "public, max-age=604800"})


@routes.get("/sw.js")
async def service_worker(request: web.Request) -> web.Response:
    """最小 Service Worker：满足 Chrome「安装到主屏幕」的可安装条件（全屏无地址栏）。"""
    js = (
        "self.addEventListener('install', function (e) { self.skipWaiting(); });\n"
        "self.addEventListener('activate', function (e) { e.waitUntil(self.clients.claim()); });\n"
        "self.addEventListener('fetch', function (e) { /* 纯在线应用，交给网络 */ });\n"
    )
    return web.Response(text=js, content_type="application/javascript",
                        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})


@routes.get("/app.apk")
async def app_apk(request: web.Request) -> web.StreamResponse:
    """手机浏览器直接下载录音上传 App（调试签名，仅自用）。"""
    apk = Path(__file__).resolve().parents[2] / "android-uploader" / "qqmonitor-uploader.apk"
    if not apk.is_file():
        raise web.HTTPNotFound(text="APK 尚未构建")
    return web.FileResponse(apk, headers={
        "Content-Type": "application/vnd.android.package-archive",
        "Content-Disposition": 'attachment; filename="qqmonitor-uploader.apk"',
    })


@routes.get("/kb/media/find/{group_id}/{name}")
async def kb_media_find(request: web.Request) -> web.Response:
    """按文件名在媒体库中查找（跨月份），供消息页内联显示图片/音视频/文件。

    match_info 会自动做 URL 解码，中文文件名也能匹配；{name} 不含路径分隔符，
    rglob 精确比对文件名，天然防穿越。
    """
    cfg, *_ = await _ctx(request)
    gid, name = request.match_info["group_id"], request.match_info["name"]
    if name in (".", "..") or not name:
        raise web.HTTPNotFound()
    base = _kb_root(cfg) / "media" / gid
    if not base.is_dir():
        raise web.HTTPNotFound()
    for p in base.rglob("*"):
        if p.is_file() and p.name == name:
            return web.FileResponse(p)
    raise web.HTTPNotFound()


@routes.get("/kb/thumb/media/{group_id}/{name}")
async def kb_thumb_media(request: web.Request) -> web.Response:
    """消息页内联图片的缩略图（原图常常 5–15MB，直接铺会让页面卡死）。"""
    cfg, *_ = await _ctx(request)
    gid, name = request.match_info["group_id"], request.match_info["name"]
    if name in (".", "..") or not name:
        raise web.HTTPNotFound()
    base = _kb_root(cfg) / "media" / gid
    if not base.is_dir():
        raise web.HTTPNotFound()
    for p in base.rglob("*"):
        if p.is_file() and p.name == name:
            thumb = await asyncio.to_thread(thumbs.thumb_for, cfg, p, _thumb_width(request))
            if thumb is None:
                raise web.HTTPFound(f"/kb/media/find/{gid}/{quote(name)}")
            resp = web.FileResponse(thumb)
            resp.headers["Cache-Control"] = "public, max-age=604800"
            return resp
    raise web.HTTPNotFound()


def _thumb_width(request: web.Request, default: int = 360) -> int:
    try:
        return int(request.query.get("w", default))
    except (TypeError, ValueError):
        return default


@routes.get("/kb/thumb/{section}/{name}")
async def kb_thumb(request: web.Request) -> web.Response:
    """知识库文档/转写稿/聊天记录里的图片缩略图。"""
    cfg, *_ = await _ctx(request)
    section, name = request.match_info["section"], request.match_info["name"]
    sub = {"chat": "chat", "documents": "documents", "transcripts": "transcripts"}.get(section)
    if not sub or name in (".", ".."):
        raise web.HTTPNotFound()
    path = _safe_child(_kb_root(cfg) / sub, name)
    if not path or not path.is_file():
        raise web.HTTPNotFound()
    thumb = await asyncio.to_thread(thumbs.thumb_for, cfg, path, _thumb_width(request))
    if thumb is None:
        raise web.HTTPFound(f"/kb/file/{section}/{quote(name)}")
    resp = web.FileResponse(thumb)
    resp.headers["Cache-Control"] = "public, max-age=604800"
    return resp


# 可在线编辑/新建的文本扩展名
_EDITABLE_EXTS = {".md", ".txt", ".srt", ".vtt", ".ass", ".json", ".csv"}
_SEARCH_MAX_BYTES = 20 * 1024 * 1024  # 单文件超过 20MB 只按文件名匹配


def _kb_search(cfg: Settings, q: str) -> list[dict]:
    """知识库全文搜索：匹配文件名 + 文本文件内容（每文件最多 5 个命中片段）。"""
    ql = q.lower()
    hits: list[dict] = []
    for section, sub in (("chat", "chat"), ("documents", "documents"),
                         ("transcripts", "transcripts")):
        d = _kb_root(cfg) / sub
        if not d.is_dir():
            continue
        for p in sorted(d.iterdir()):
            if not p.is_file():
                continue
            size = p.stat().st_size
            if ql in p.name.lower():
                hits.append({"section": section, "name": p.name, "size": size,
                             "snippets": []})
                continue
            if p.suffix.lower() not in _EDITABLE_EXTS or size > _SEARCH_MAX_BYTES:
                continue
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            snips: list[dict] = []
            for i, line in enumerate(text.splitlines(), 1):
                if ql in line.lower():
                    snips.append({"line": i, "text": line.strip()[:300]})
                    if len(snips) >= 5:
                        break
            if snips:
                hits.append({"section": section, "name": p.name, "size": size,
                             "snippets": snips})
    # 内容命中优先，其次按名称排序
    hits.sort(key=lambda h: (-len(h["snippets"]), h["name"].lower()))
    return hits


@routes.get("/kb/search")
async def kb_search(request: web.Request) -> web.Response:
    cfg, db, _ = await _ctx(request)
    q = (request.query.get("q") or "").strip()
    results = _kb_search(cfg, q) if q else []
    html = T.kb_search_page(q, results)
    return web.Response(text=_page(db, "知识库搜索", html, "kb", await _groups(db)),
                        content_type="text/html", charset="utf-8")


@routes.get("/kb/media/browse/{gid}/{month}/")
async def kb_media_browse(request: web.Request) -> web.Response:
    """媒体月份浏览页：列出某群某月的媒体文件，支持单文件/整月删除。"""
    cfg, db, _ = await _ctx(request)
    gid, month = request.match_info["gid"], request.match_info["month"]
    if not (gid.isdigit() and re.fullmatch(r"\d{4}-\d{2}|unknown", month)):
        raise web.HTTPNotFound()
    base = _safe_child(_kb_root(cfg) / "media", f"{gid}/{month}")
    if not base or not base.is_dir():
        raise web.HTTPNotFound()
    files = [{"name": p.name, "size": p.stat().st_size}
             for p in sorted(base.iterdir()) if p.is_file()]
    html = T.kb_media_page(gid, month, files)
    return web.Response(text=_page(db, f"媒体 {gid}/{month}", html, "kb", await _groups(db)),
                        content_type="text/html", charset="utf-8")


@routes.get("/kb/new")
async def kb_new(request: web.Request) -> web.Response:
    cfg, db, _ = await _ctx(request)
    html = T.kb_new_page("")
    return web.Response(text=_page(db, "新建笔记", html, "kb", await _groups(db)),
                        content_type="text/html", charset="utf-8")


@routes.get("/kb/edit/{section}/{name}")
async def kb_edit(request: web.Request) -> web.Response:
    cfg, db, _ = await _ctx(request)
    section = request.match_info["section"]
    if section not in ("documents", "transcripts"):
        raise web.HTTPNotFound()
    path = _safe_child(_kb_root(cfg) / section, request.match_info["name"])
    if not path or not path.is_file() or path.suffix.lower() not in _EDITABLE_EXTS:
        raise web.HTTPNotFound()
    content = path.read_text(encoding="utf-8", errors="replace")
    html = T.kb_edit_page(section, path.name, content)
    return web.Response(text=_page(db, f"编辑 {path.name}", html, "kb", await _groups(db)),
                        content_type="text/html", charset="utf-8")


@routes.post("/kb/save")
async def kb_save(request: web.Request) -> web.Response:
    """保存笔记：新建或覆盖 documents/transcripts 下的文本文件。"""
    cfg, db, _ = await _ctx(request)
    form = await request.post()
    section = (form.get("section") or "").strip()
    name = (form.get("name") or "").strip()
    content = form.get("content") or ""
    groups = await _groups(db)

    def _bad(err: str) -> web.Response:
        html = T.kb_new_page(err, section, name, content)
        return web.Response(text=_page(db, "新建笔记", html, "kb", groups),
                            content_type="text/html", charset="utf-8")

    if section not in ("documents", "transcripts"):
        return _bad("目标分区不正确")
    if not name or "/" in name or "\\" in name or ":" in name:
        return _bad("文件名不能为空或包含路径分隔符")
    if Path(name).suffix.lower() not in _EDITABLE_EXTS:
        return _bad("仅支持文本文件（.md / .txt / .srt / .vtt / .ass / .json / .csv）")
    path = _safe_child(_kb_root(cfg) / section, name)
    if not path:
        return _bad("文件名不合法")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    log.info("保存笔记 %s/%s", section, name)
    raise web.HTTPFound(f"/kb?saved={name}")


@routes.get("/kb/file/{section}/{name}")
async def kb_doc_file(request: web.Request) -> web.Response:
    cfg, db, _ = await _ctx(request)
    section = request.match_info["section"]
    if section not in ("documents", "transcripts"):
        raise web.HTTPNotFound()
    path = _safe_child(_kb_root(cfg) / section, request.match_info["name"])
    if not path or not path.is_file():
        raise web.HTTPNotFound()
    # transcripts 下的 .md 直接渲染查看，其余文件下载
    if section == "transcripts" and path.suffix.lower() == ".md":
        md = path.read_text(encoding="utf-8", errors="replace")
        html = T.kb_chat_page(path.name, md_to_html(md), str(path))
        return web.Response(text=_page(db, _chat_title(db, path.name), html, "kb", await _groups(db)),
                            content_type="text/html", charset="utf-8")
    return web.FileResponse(path)


@routes.post("/kb/ingest")
async def kb_ingest(request: web.Request) -> web.Response:
    """拖拽智能导入：接收多文件 + 可选群号，后台异步分拣清洗，返回 task_id。"""
    cfg, db, _ = await _ctx(request)
    form = await request.post()
    group_id = 0
    try:
        group_id = int(form.get("group_id") or "0") or 0
    except ValueError:
        group_id = 0

    tmp_dir = cfg.resolve("data/inbox") / "_ingest_tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    files: list[Path] = []
    for part in form.values():
        if not isinstance(part, web.FileField) or not part.filename:
            continue
        # 中文文件名可能被编码为 filename*=UTF-8''%XX 形式，需 unquote
        fname = Path(unquote(part.filename)).name  # 去掉客户端路径
        if not fname:
            continue
        # 同名文件加序号后缀，避免同批上传时后者静默覆盖前者（会丢文件）
        target = tmp_dir / fname
        if target.exists():
            stem, suffix = Path(fname).stem, Path(fname).suffix
            for i in range(2, 100):
                cand = tmp_dir / f"{stem}({i}){suffix}"
                if not cand.exists():
                    target = cand
                    break
        try:
            with open(target, "wb") as out:
                while chunk := part.file.read(1 << 20):
                    out.write(chunk)
            files.append(target)
        except OSError as e:
            log.warning("保存上传文件失败 %s: %s", fname, e)
    if not files:
        return web.json_response({"error": "没有收到任何文件"}, status=400)

    tid = uuid.uuid4().hex
    task = {"status": "running", "total": len(files),
            "done": 0, "results": [], "error": "", "stage": ""}
    server: WebServer = request.app["server"]
    server.ingest_tasks[tid] = task

    async def _run() -> None:
        def _progress(n) -> None:
            if isinstance(n, str):
                task["stage"] = n   # 阶段文本（语音识别中/第 x/y 段）
            else:
                task["done"] = n    # 完成文件数

        try:
            results = await asyncio.to_thread(
                ingest_files, db, cfg, files, group_id, _progress)
            task["results"] = results
            task["status"] = "done"
            task["done"] = len(results)
        except Exception as e:
            log.exception("拖拽导入任务失败")
            task["status"] = "error"
            task["error"] = str(e)
        finally:
            for f in files:
                try:
                    f.unlink()
                except OSError:
                    pass
            try:
                tmp_dir.rmdir()
            except OSError:
                pass

    asyncio.create_task(_run())
    return web.json_response({"task_id": tid})


@routes.get("/kb/ingest/{task_id}")
async def kb_ingest_status(request: web.Request) -> web.Response:
    """拖拽导入任务状态轮询。"""
    task = request.app["server"].ingest_tasks.get(request.match_info["task_id"])
    if not task:
        raise web.HTTPNotFound()
    return web.json_response(task)


# ---------- 设置（白名单管理） ----------
def _vision_status(cfg: Settings) -> dict:
    """图片理解引擎状态（设置页展示）：是否就绪、显卡信息、不可用原因。

    注意：只做「轻量探测」——本地后端只检查依赖与配置，不真的加载模型
    （加载要占几 GB 显存且耗时，留到首次识别时懒加载）。
    """
    out: dict = {"ok": False, "why": "", "engine": "", "gpu": ""}
    try:
        import torch
        if torch.cuda.is_available():
            free = torch.cuda.get_device_properties(0).total_memory
            out["gpu"] = (f"{torch.cuda.get_device_name(0)}"
                          f"（{free / 1024 ** 3:.0f}GB）")
        else:
            out["gpu"] = "未检测到 CUDA 显卡（本地模型会跑在 CPU 上，很慢）"
    except Exception:
        out["gpu"] = "未安装 torch"

    v = cfg.vision or {}
    backend = v.get("backend", "local")
    if backend == "off":
        out["why"] = "已关闭（vision.backend=off）"
        return out
    if backend == "api":
        api = v.get("api") or {}
        if not (api.get("base_url") and api.get("model")):
            out["why"] = "未填写视觉 API 的接口地址或模型名"
            return out
        out["ok"] = True
        out["engine"] = f"api:{api['model']}"
        return out

    # local：检查依赖是否齐全（不加载模型）
    try:
        import transformers  # noqa: F401
        import qwen_vl_utils  # noqa: F401
        import torchvision  # noqa: F401
    except ImportError as e:
        out["why"] = f"缺少依赖 {e.name}（pip install accelerate qwen-vl-utils torchvision）"
        return out
    if v.get("quant", "4bit") == "4bit":
        try:
            import bitsandbytes  # noqa: F401
        except ImportError:
            out["why"] = "开启 4bit 量化需要 bitsandbytes（pip install bitsandbytes）"
            return out
    out["ok"] = True
    out["engine"] = f"local:{v.get('model', '')}"
    return out


@routes.get("/settings")
async def settings(request: web.Request) -> web.Response:
    """设置页：AI 模型配置 + 索引管理 + 监听白名单 + 已导入群概览。"""
    cfg, db, _ = await _ctx(request)
    q = request.query
    msg = ""
    if q.get("saved"):
        n = q.get("n", "0")
        what = q.get("what") or "groups"
        if what == "buddies":
            msg = ("好友白名单已更新并立即生效：%s 个好友（同时作用于私聊同步与知识库）" % n
                   if n != "0" else "好友白名单已清空（当前不同步任何好友）")
        else:
            msg = (f"群白名单已更新并立即生效：{n} 个群"
                   f"（同时作用于实时监听、文件导入与知识库同步）"
                   if n != "0" else "群白名单已清空（当前不限制群）")
    elif q.get("llm_saved"):
        msg = "模型配置已保存"
    elif q.get("index_done"):
        msg = (f"索引完成：新增 {q.get('added', 0)} 个片段，"
               f"跳过 {q.get('skipped', 0)} 个未变动文件，"
               f"当前共 {q.get('chunks', 0)} 个片段")
    elif q.get("index_empty"):
        msg = "知识库中没有可索引的文本文件"
    elif q.get("index_cleared"):
        msg = "索引已清空"
    elif q.get("err"):
        msg = unquote(q["err"])
    elif q.get("auto"):
        from ..watchlist import load_state
        st = load_state(cfg)
        if not st.get("ok"):
            msg = f"白名单自动规则：{st.get('error') or '尚未执行过'}"
        elif not st.get("enabled"):
            msg = "白名单自动规则已关闭，未改动名单"
        else:
            msg = (f"白名单自动规则已执行：命中群 {len(st.get('auto_groups') or [])} 个、"
                   f"好友 {len(st.get('auto_buddies') or [])} 个；"
                   f"新增群 {len(st.get('added_groups') or [])} 个、"
                   f"移出群 {len(st.get('removed_groups') or [])} 个")
    elif q.get("auto_saved"):
        msg = "白名单自动规则已保存（下一轮扫描生效，也可点「立即刷新」马上应用）"
    tone = "warn" if q.get("err") else ""
    from ..watchlist import auto_cfg, load_state
    auto_state = load_state(cfg)
    if q.get("auto") and not auto_state.get("ok"):
        tone = "warn"
    auto_groups = {int(x) for x in auto_state.get("auto_groups") or []
                   if str(x).lstrip("-").isdigit()}
    auto_buddies = {int(x) for x in auto_state.get("auto_buddies") or []
                    if str(x).lstrip("-").isdigit()}
    html = T.settings_page(
        groups=await _groups(db),
        watch=[int(g) for g in cfg.watch_groups],
        cfg_path=str(cfg.path),
        keywords=cfg.important.get("keywords", []),
        at_all=bool(cfg.important.get("at_all")),
        llm=cfg.llm,
        kb_stats=db.kb_stats(),
        knowledge=cfg.knowledge,
        ocr_stats=db.ocr_stats(),
        vision=cfg.vision,
        vision_status=_vision_status(cfg),
        watch_buddies=[int(b) for b in (cfg.watch_buddies or [])],
        manual_groups=[int(g) for g in (cfg.watch_groups_manual or [])],
        manual_buddies=[int(b) for b in (cfg.watch_buddies_manual or [])],
        auto=auto_cfg(cfg),
        auto_state=auto_state,
        auto_group_ids=auto_groups,
        auto_buddy_ids=auto_buddies,
        msg=msg,
    )
    return web.Response(text=_page(db, "设置", html, "settings", await _groups(db),
                                    tone=tone),
                        content_type="text/html", charset="utf-8")


@routes.post("/settings/llm")
async def settings_llm(request: web.Request) -> web.Response:
    """保存 AI 模型配置（含 api_key）到 data/llm.yaml。"""
    cfg, _, _ = await _ctx(request)
    form = await request.post()

    # 以现有配置为底：页面只管 chat/embedding/summary/rag/system_extra，
    # 其余键（config_version、将来新增的段）原样保留，避免保存一次就被降级/清掉
    new_llm: dict = dict(cfg.llm)
    for kind in ("chat", "embedding", "summary"):
        old = (cfg.llm.get(kind) or {})
        # 以旧配置为底：页面没暴露的键（local 的 device / max_length 等）原样保留
        conf: dict = dict(old)
        for name, _l, typ, _p, _t in T._LLM_FIELDS[kind]:
            raw = form.get(f"{kind}.{name}")
            if raw is None:
                continue
            v = str(raw).strip()
            # 数值字段转数字，便于 YAML 输出干净、模型参数生效
            if typ == "number":
                try:
                    conf[name] = int(v) if float(v).is_integer() else float(v)
                except (TypeError, ValueError):
                    continue
            else:
                conf[name] = v
        # 页面不再回显密钥：留空 = 保持原有密钥（清空请直接编辑 data/llm.yaml）
        if not conf.get("api_key") and old.get("api_key"):
            conf["api_key"] = old["api_key"]
        new_llm[kind] = conf

    from ..config import DEFAULTS

    rag: dict = {}
    old_rag = cfg.llm.get("rag") or {}
    rag_defaults = DEFAULTS["llm"]["rag"]
    for name in ("top_k", "chunk_size", "max_context", "max_rounds",
                 "tool_result_chars", "doc_read_chars", "context_budget_chars"):
        raw = form.get(f"rag.{name}")
        if not raw:
            continue
        dft = old_rag.get(name, rag_defaults.get(name, 6))
        rag[name] = _to_int(raw, dft) or dft
    raw_chat = str(form.get("rag.index_chat") or "").strip().lower()
    rag["index_chat"] = raw_chat not in ("false", "0", "no", "off")

    new_llm["rag"] = rag
    # 自定义系统提示词（追加式）：页面没提交该字段时保留原值
    raw_extra = form.get("llm.system_extra")
    if raw_extra is not None:
        new_llm["system_extra"] = str(raw_extra).strip()
    elif cfg.llm.get("system_extra"):
        new_llm["system_extra"] = cfg.llm["system_extra"]
    cfg.save_llm(new_llm)
    log.info("AI 模型配置已保存")
    raise web.HTTPFound("/settings?llm_saved=1")


@routes.post("/settings/llm/test")
async def settings_llm_test(request: web.Request) -> web.Response:
    """用当前表单值测试模型连通性（不保存、不落盘）。"""
    cfg, _, _ = await _ctx(request)
    form = await request.post()
    kind = str(form.get("kind") or "").strip()
    if kind not in ("chat", "embedding", "summary"):
        return web.json_response({"ok": False, "message": "未知模型类型"})

    old = (cfg.llm.get(kind) or {})
    conf: dict = dict(old)
    for name, _l, typ, _p, _t in T._LLM_FIELDS[kind]:
        raw = form.get(f"{kind}.{name}")
        if raw is None:
            continue
        v = str(raw).strip()
        if typ == "number":
            try:
                conf[name] = int(v) if float(v).is_integer() else float(v)
            except (TypeError, ValueError):
                continue
        else:
            conf[name] = v

    # 测试连接同样不再从页面拿密钥：留空时用已保存的密钥
    if not conf.get("api_key") and old.get("api_key"):
        conf["api_key"] = old["api_key"]

    # local 引擎不连网络，直接加载模型做一次冒烟
    if kind == "embedding" and str(conf.get("provider") or "api") == "local":
        from ..llm.local_embed import get_engine
        prev = cfg.llm.get("embedding")
        cfg.llm["embedding"] = conf
        try:
            result = await asyncio.to_thread(get_engine(cfg).test)
        except Exception as e:
            result = {"ok": False, "message": f"本地模型测试失败：{e}"}
        finally:
            if prev is not None:
                cfg.llm["embedding"] = prev
        return web.json_response(result)

    # 必须放进线程：test() 内部是同步 HTTP 请求，直接在协程里调用会
    # 阻塞整个事件循环，导致 WebUI 在测试期间完全无响应
    from ..llm.client import LLMClient
    result = await asyncio.to_thread(LLMClient(kind, conf).test)
    return web.json_response(result)


@routes.post("/settings/llm/index")
async def settings_llm_index(request: web.Request) -> web.Response:
    """构建 / 增量更新知识库索引。force=1 时全量重建。"""
    cfg, db, _ = await _ctx(request)
    form = await request.post()
    force = str(form.get("force") or "").strip() in ("1", "true", "yes")

    from ..llm.client import build_client
    from ..llm.indexer import Indexer
    try:
        client = build_client(cfg, "embedding")
        client._require()  # 配置不全时提前报错，给出明确提示
        idx = Indexer(db, cfg, client)
        res = await asyncio.to_thread(idx.build, force)
    except Exception as e:
        msg = str(e)
        log.warning("索引构建失败：%s", msg)
        raise web.HTTPFound(f"/settings?err={quote(msg[:200])}")

    if res["files"] == 0:
        raise web.HTTPFound("/settings?index_empty=1")
    raise web.HTTPFound(
        f"/settings?index_done=1&added={res['added']}"
        f"&skipped={res['skipped']}&chunks={res['chunks']}")


@routes.post("/settings/llm/index/clear")
async def settings_llm_index_clear(request: web.Request) -> web.Response:
    """清空全部索引。"""
    _, db, _ = await _ctx(request)
    db.kb_clear()
    log.info("知识库索引已清空")
    raise web.HTTPFound("/settings?index_cleared=1")


@routes.post("/settings/knowledge")
async def settings_knowledge(request: web.Request) -> web.Response:
    """保存知识库 OCR 开关。nt_db / key 敏感，不在此页改，需手编 config.yaml。"""
    cfg, db, _ = await _ctx(request)
    form = await request.post()
    cfg.knowledge["ocr_chat_images"] = "ocr_chat_images" in form
    cfg.knowledge["ocr_formula_wrap"] = "ocr_formula_wrap" in form
    cfg.save_section("knowledge", cfg.knowledge)
    raise web.HTTPFound("/settings?saved=知识库设置已保存")


@routes.post("/settings/autosync")
async def settings_autosync(request: web.Request) -> web.Response:
    """保存定时自动同步开关与间隔。写入 config.yaml，需重启服务生效。"""
    cfg, _, _ = await _ctx(request)
    form = await request.post()
    if "enabled" in form:
        try:
            minutes = max(1.0, float(form.get("minutes") or 3))
        except (TypeError, ValueError):
            minutes = 3.0
        # 整数分钟存 int，YAML 更干净
        cfg.knowledge["auto_sync_minutes"] = \
            int(minutes) if float(minutes).is_integer() else minutes
    else:
        cfg.knowledge["auto_sync_minutes"] = 0
    cfg.save_section("knowledge", cfg.knowledge)
    log.info("自动同步设置已保存：auto_sync_minutes=%s",
             cfg.knowledge["auto_sync_minutes"])
    raise web.HTTPFound(
        "/settings?saved=" + quote(
            "自动同步已开启，重启服务后生效"
            if cfg.knowledge["auto_sync_minutes"] else "自动同步已关闭"))


@routes.post("/settings/vision")
async def settings_vision(request: web.Request) -> web.Response:
    """保存图片理解引擎配置。切换后端/模型时释放旧引擎，下次调用重新加载。"""
    cfg, db, _ = await _ctx(request)
    form = await request.post()
    v = dict(cfg.vision or {})
    api = dict(v.get("api") or {})

    v["backend"] = (form.get("backend") or "local").strip()
    v["model"] = (form.get("model") or "").strip() or \
        "Qwen/Qwen2.5-VL-3B-Instruct"
    try:
        v["max_pixels"] = max(65536, int(form.get("max_pixels") or 1003520))
    except ValueError:
        pass
    v["quant"] = (form.get("quant") or "4bit").strip()

    base = (form.get("api_base_url") or "").strip()
    model = (form.get("api_model") or "").strip()
    key = (form.get("api_key") or "").strip()
    if base:
        api["base_url"] = base
    if model:
        api["model"] = model
    if key:          # 留空表示不修改已保存的密钥
        api["api_key"] = key
    v["api"] = api

    cfg.vision = v
    cfg.save_section("vision", v)

    # 切换后端/模型后必须释放旧引擎，否则显存里还是老模型
    from ..vision import reset_engine
    reset_engine()
    raise web.HTTPFound("/settings?saved=图片理解引擎设置已保存")


# ---------- AI 助手（知识库问答 + 日/周/月/年总结） ----------
# 简单会话历史：仅存内存，重启即失。够用且避免把聊天内容额外落盘。
_assistant_history: dict[str, list[dict]] = {"items": []}
_HISTORY_MAX = 20


def _summaries_dir(cfg: Settings) -> Path:
    return cfg.resolve(cfg.storage.get("digest_dir", "data/digests")).parent / "summaries"


# ---------- AI 对话（一期：会话 + 三栏布局，检索仍走固定流程） ----------
def _new_conv_id() -> str:
    return uuid.uuid4().hex[:12]


@routes.get("/assistant")
async def assistant_redirect(request: web.Request) -> web.Response:
    """旧入口重定向到会话列表（保留兼容）。"""
    raise web.HTTPFound("/chat")


@routes.get("/chat")
async def chat_list(request: web.Request) -> web.Response:
    """会话列表 / 搜索。"""
    _, db, _ = await _ctx(request)
    kw = (request.query.get("q") or "").strip()
    items = db.search_conversations(kw) if kw else db.list_conversations()
    html = T.conversation_list_page(items, kw)
    return web.Response(
        text=_page(db, "AI 对话", html, "assistant", await _groups(db),
                    cur_conv="",
                    conv_keyword=kw),
        content_type="text/html", charset="utf-8")


@routes.post("/chat/new")
async def chat_new(request: web.Request) -> web.Response:
    """新建会话并进入对话页。

    POST 而非 GET：建会话会写库，GET 版本既能被跨站预取触发（CSRF 中间件
    只拦写方法），也会在爬虫/预连接时留下空会话。
    """
    _, db, _ = await _ctx(request)
    gid = _to_int(await _param(request, "group_id"))
    cid = _new_conv_id()
    db.create_conversation(cid, group_id=gid, title="")
    raise web.HTTPFound(f"/chat/{cid}")


@routes.get("/chat/{cid}")
async def chat_view(request: web.Request) -> web.Response:
    """进入指定会话。"""
    cfg, db, _ = await _ctx(request)
    cid = request.match_info["cid"]
    conv = db.get_conversation(cid)
    if not conv:
        raise web.HTTPNotFound()
    from ..llm.client import llm_ready
    ready = {k: llm_ready(cfg, k) for k in ("chat", "embedding", "summary")}
    html = T.chat_page(conv=conv, messages=conv.get("messages") or [],
                       ready=ready, stats=db.kb_stats(),
                       groups=await _groups(db))
    return web.Response(
        text=_page(db, conv.get("title") or "新对话", html, "assistant",
                    await _groups(db),
                    cur_conv=cid),
        content_type="text/html", charset="utf-8")


@routes.get("/chat/{cid}/rename")
async def chat_rename_get(request: web.Request) -> web.Response:
    """GET 兜底：重定向回会话，避免直接访问报错。"""
    raise web.HTTPFound(f"/chat/{request.match_info['cid']}")


@routes.post("/chat/{cid}/rename")
async def chat_rename(request: web.Request) -> web.Response:
    _, db, _ = await _ctx(request)
    form = await request.post()
    title = str(form.get("title") or "").strip()[:80]
    db.rename_conversation(request.match_info["cid"], title)
    return web.json_response({"ok": True})


@routes.post("/chat/{cid}/delete")
async def chat_delete(request: web.Request) -> web.Response:
    _, db, _ = await _ctx(request)
    db.delete_conversation(request.match_info["cid"])
    raise web.HTTPFound("/chat")


@routes.post("/chat/{cid}/send")
async def chat_send(request: web.Request) -> web.Response:
    """发送消息（无 JS 时的兜底）：Agent 自主调用工具 → 保存会话 → 刷新页面。"""
    cfg, db, _ = await _ctx(request)
    cid = request.match_info["cid"]
    conv = db.get_conversation(cid)
    if not conv:
        raise web.HTTPNotFound()
    form = await request.post()
    content = str(form.get("content") or "").strip()
    if not content:
        raise web.HTTPFound(f"/chat/{cid}")

    from ..llm.agent import Agent
    from ..llm.client import LLMError, llm_ready

    async def _render(error: str = "") -> web.Response:
        ready = {k: llm_ready(cfg, k) for k in ("chat", "embedding", "summary")}
        conv2 = db.get_conversation(cid) or conv
        html = T.chat_page(conv=conv2, messages=conv2.get("messages") or [],
                           ready=ready, stats=db.kb_stats(),
                           groups=await _groups(db), error=error)
        return web.Response(
            text=_page(db, conv2.get("title") or "新对话", html, "assistant",
                        await _groups(db),
                        cur_conv=cid,
                        tone="warn" if error else ""),
            content_type="text/html", charset="utf-8")

    # 先把用户消息入库，保证即使后续失败也不丢输入
    msgs = list(conv.get("messages") or [])
    msgs.append({"role": "user", "content": content})
    db.save_conversation_messages(cid, msgs)

    if not llm_ready(cfg, "chat"):
        return await _render("对话模型未配置完整，请到设置页填写接口地址、API Key 与模型名。")

    # 由模型自己决定要不要检索、检索几次（Function Calling）。
    # 模型不支持工具时，Agent 内部自动退回「后端预判 + 固定检索」流程。
    agent = Agent(db, cfg)
    try:
        res = await asyncio.to_thread(agent.run, content, msgs[:-1])
    except LLMError as e:
        return await _render(str(e))
    except Exception as e:
        log.exception("问答失败")
        return await _render(f"调用对话模型失败：{type(e).__name__}: {e}")

    msgs.append({
        "role": "assistant", "content": res["text"],
        "model": res.get("model", ""),
        "tools": res["tools"],
        "sources": res["sources"],
    })
    db.save_conversation_messages(cid, msgs)

    # 首轮自动生成标题（取用户问题前 20 字）
    if not (conv.get("title") or "").strip() and len(msgs) >= 2:
        first = (msgs[0].get("content") or "").strip().replace("\n", " ")
        db.rename_conversation(cid, first[:20] or "新对话")

    raise web.HTTPFound(f"/chat/{cid}")



@routes.post("/assistant/summary")
async def assistant_summary(request: web.Request) -> web.Response:
    """生成日 / 周 / 月 / 年总结。"""
    cfg, db, _ = await _ctx(request)
    form = await request.post()
    period = str(form.get("period") or "week").strip()
    if period not in ("week", "month", "year"):  # 日总结用 digest.py
        period = "week"
    gid = _to_int(form.get("group_id"))
    base = None
    raw_base = str(form.get("base") or "").strip()
    if raw_base:
        base = _to_date(raw_base, raw_base)
        if base:
            try:
                base = datetime.strptime(base, "%Y-%m-%d").date()
            except ValueError:
                base = None

    from ..llm.client import LLMError, build_client, llm_ready
    from ..llm.summary import generate_summary, list_summaries

    # 总结结果作为一条助手消息落到会话里，复用对话页展示（不再有独立助手页）
    cid = str(form.get("cid") or "").strip()
    conv = db.get_conversation(cid) if cid else None
    if not conv:
        cid = _new_conv_id()
        db.create_conversation(cid, group_id=gid, title="")
        conv = db.get_conversation(cid) or {}

    async def _fail(msg: str) -> web.Response:
        conv2 = db.get_conversation(cid) or conv
        ready = {k: llm_ready(cfg, k) for k in ("chat", "embedding", "summary")}
        html = T.chat_page(conv=conv2, messages=conv2.get("messages") or [],
                           ready=ready, stats=db.kb_stats(),
                           groups=await _groups(db), error=msg)
        return web.Response(text=_page(db, "AI 对话", html, "assistant",
                                       await _groups(db), cur_conv=cid,
                                       tone="warn"),
                            content_type="text/html", charset="utf-8")

    if not llm_ready(cfg, "summary"):
        return await _fail("总结模型未配置完整，请到设置页填写接口地址、API Key 与模型名。")

    # 总结走 kb_chunks（统一分块 + 事件日期），因此必须先有索引
    if not db.kb_stats()["chunks"]:
        return await _fail(
            "尚未建立知识库索引，无法生成总结。请到设置页点击「增量更新索引」后重试。")

    try:
        client = build_client(cfg, "summary")
        res = await asyncio.to_thread(
            generate_summary, db, cfg, client, period, base, gid)
    except LLMError as e:
        return await _fail(str(e))
    except Exception as e:
        log.exception("总结生成失败")
        return await _fail(f"生成失败：{type(e).__name__}: {e}")

    if res.get("empty") or not res.get("content"):
        return await _fail(
            f"{res.get('range', '')} 范围内没有带日期的资料。\n"
            f"提示：文档和转写稿需要设定事件日期才会被纳入对应时间段，"
            f"可到知识库页检查；无日期的文件不会出现在周期总结中。")

    # 把总结作为助手消息存入会话，用户可在对话流里直接看到并继续追问
    label = {"week": "本周", "month": "本月", "year": "本年度"}.get(period, period)
    msgs = list(conv.get("messages") or [])
    msgs.append({"role": "user", "content": f"生成{label}总结"})
    msgs.append({
        "role": "assistant",
        "content": res["content"],
        "model": (cfg.llm.get("summary") or {}).get("model", ""),
        "tools": [f"summarize_{period}"],
    })
    db.save_conversation_messages(cid, msgs)
    if not (conv.get("title") or "").strip():
        db.rename_conversation(cid, f"{label}总结 {res.get('range', '')}")
    raise web.HTTPFound(f"/chat/{cid}")


@routes.post("/chat/{cid}/stream")
async def chat_stream(request: web.Request) -> web.StreamResponse:
    """流式问答（SSE）。

    事件类型：
        meta        首帧：模型名
        tool_start  模型请求调用某工具（含参数）
        tool_done   工具执行完毕（含结果摘要与命中来源）
        delta       文本增量
        fallback    该模型不支持工具调用，已切回固定检索流程
        done        完成：带完整文本、工具调用记录、来源
        error       失败：带中文错误说明
    """
    cfg, db, _ = await _ctx(request)
    cid = request.match_info["cid"]
    conv = db.get_conversation(cid)
    if not conv:
        raise web.HTTPNotFound()
    form = await request.post()
    content = str(form.get("content") or "").strip()
    if not content:
        raise web.HTTPBadRequest()

    resp = web.StreamResponse(
        status=200,
        headers={"Content-Type": "text/event-stream; charset=utf-8",
                 "Cache-Control": "no-cache",
                 "X-Accel-Buffering": "no"},  # 关闭 Nginx 缓冲
    )
    await resp.prepare(request)

    async def sse(event: str, data: dict) -> None:
        import json
        await resp.write(
            f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
            .encode("utf-8"))

    # 用户消息先入库，保证流式失败也不丢输入
    msgs = list(conv.get("messages") or [])
    msgs.append({"role": "user", "content": content})
    await asyncio.to_thread(db.save_conversation_messages, cid, msgs)

    from ..llm.agent import Agent
    from ..llm.client import LLMError, llm_ready

    if not llm_ready(cfg, "chat"):
        await sse("error", {"message": "对话模型未配置完整，请到设置页配置。"})
        await resp.write_eof()
        return resp

    await sse("meta", {"model": (cfg.llm.get("chat") or {}).get("model", "")})

    # Agent 在线程里跑（含多轮工具调用），事件经队列跨线程投递。
    # 用队列实时推送而非轮询 buffer——否则本地/快速响应会一次性吐出全部内容，
    # 前端看不到打字机效果
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    _SENTINEL = object()

    def _emit(ev: dict) -> None:
        loop.call_soon_threadsafe(queue.put_nowait, ev)

    agent = Agent(db, cfg, on_event=_emit)

    def _consume():
        """在线程里跑 Agent。返回 (结果字典, 错误信息)，二者必有其一为 None。"""
        try:
            return agent.run(content, msgs[:-1]), None
        except LLMError as e:
            return None, str(e)
        except Exception as e:
            log.exception("流式生成失败")
            return None, f"生成失败：{type(e).__name__}: {e}"
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, _SENTINEL)

    task = asyncio.create_task(asyncio.to_thread(_consume))

    def _drain_task(t: "asyncio.Task") -> None:
        """客户端断开后不再 await 任务，但必须取走异常，避免告警刷日志。"""
        def _done(t: "asyncio.Task") -> None:
            try:
                t.exception()
            except (asyncio.CancelledError, Exception):
                pass
        t.add_done_callback(_done)

    try:
        while True:
            item = await queue.get()
            if item is _SENTINEL:
                break
            await sse(item.get("type") or "delta", item)
        res, err = await task
        if err or not res:
            await sse("error", {"message": err or "生成失败：模型没有返回结果。"})
            await resp.write_eof()
            return resp
    except asyncio.CancelledError:
        _drain_task(task)
        raise
    except (ConnectionResetError, ConnectionError) as e:
        # 浏览器关页面/切走：Agent 线程无法强制中断，让它跑完自生自灭，
        # 但绝不能再往已断开的连接写 error 帧（旧实现会二次抛异常刷 500）。
        log.info("流式推送中断：客户端已断开（%s）：%s", cid, e)
        _drain_task(task)
        return resp
    except Exception as e:
        log.exception("流式推送失败")
        try:
            await sse("error", {"message": f"推送失败：{type(e).__name__}: {e}"})
            await resp.write_eof()
        except (ConnectionResetError, ConnectionError):
            log.info("流式推送中断：客户端已断开（%s）", cid)
        _drain_task(task)
        return resp

    answer = (res.get("text") or "").strip()
    msgs.append({
        "role": "assistant", "content": answer,
        "model": res.get("model", ""),
        "tools": res.get("tools") or [],
        "sources": res.get("sources") or [],
    })
    await asyncio.to_thread(db.save_conversation_messages, cid, msgs)
    if not (conv.get("title") or "").strip() and len(msgs) >= 2:
        first = (msgs[0].get("content") or "").strip().replace("\n", " ")
        await asyncio.to_thread(db.rename_conversation, cid,
                                first[:20] or "新对话")

    await sse("done", {"text": answer, "tools": res.get("tools") or [],
                       "sources": res.get("sources") or []})
    await resp.write_eof()
    return resp


@routes.get("/assistant/summary/{name}")
async def assistant_summary_view(request: web.Request) -> web.Response:
    """查看已生成的总结文件。"""
    cfg, db, _ = await _ctx(request)
    path = _safe_child(_summaries_dir(cfg), unquote(request.match_info["name"]))
    if not path or not path.is_file():
        raise web.HTTPNotFound()
    md = path.read_text(encoding="utf-8", errors="replace")
    html = T.summary_view_page(path.name, md_to_html(md))
    return web.Response(text=_page(db, path.name, html, "assistant", await _groups(db)),
                        content_type="text/html", charset="utf-8")


async def _save_watchlist(cfg: Settings, db: Database | None = None, *,
                          groups: set[int] | None = None,
                          buddies: set[int] | None = None) -> dict:
    """把表单勾选合并进三层名单（手工基线 / 生效名单 / ignore）并写盘。

    `groups` / `buddies` 传 None 表示这一侧不改（例如只保存好友白名单）。
    返回 {groups, buddies, auto_groups, auto_buddies}（都是列表，便于拼消息）。
    """
    from ..watchlist import auto_cfg, load_state

    ac = auto_cfg(cfg)
    state = load_state(cfg)
    auto_g = ({int(x) for x in state.get("auto_groups") or []}
              if state.get("ok") else set())
    auto_b = ({int(x) for x in state.get("auto_buddies") or []}
              if state.get("ok") else set())

    prev_g = {int(g) for g in (cfg.watch_groups or [])}
    prev_b = {int(b) for b in (cfg.watch_buddies or [])}
    prev_manual_g = {int(g) for g in (cfg.watch_groups_manual or [])}
    prev_manual_b = {int(b) for b in (cfg.watch_buddies_manual or [])}
    manual_g = (set(groups) if groups is not None else set(prev_manual_g))
    manual_b = (set(buddies) if buddies is not None else set(prev_manual_b))

    # 取消勾选的「自动项」要记进 ignore：否则下一轮自动规则又把它加回来，
    # 用户会发现怎么删都删不掉。取消勾选的手工项不必记（它本来就不在自动集里）。
    # 反方向：重新勾上的自动项从 ignore 里移除，让用户重新获得控制权。
    ignore_g = set(ac["ignore_groups"])
    ignore_b = set(ac["ignore_buddies"])
    if groups is not None:
        sel = set(groups)
        # 只把「用户新勾选的、非自动命中」的项记成手工基线；自动命中的项即使
        # 出现在表单里也保持自动身份，否则页面一存盘它们就永远是手工项了。
        manual_g = (sel & prev_manual_g) | (sel - auto_g)
        ignore_g -= sel
        ignore_g |= (prev_g - sel) & auto_g
    if buddies is not None:
        sel = set(buddies)
        manual_b = (sel & prev_manual_b) | (sel - auto_b)
        ignore_b -= sel
        ignore_b |= (prev_b - sel) & auto_b

    new_auto = dict(ac)
    new_auto["ignore_groups"] = sorted(ignore_g)
    new_auto["ignore_buddies"] = sorted(ignore_b)
    eff_g = (manual_g | auto_g) - ignore_g
    eff_b = (manual_b | auto_b) - ignore_b
    cfg.set_watchlists(sorted(eff_g), sorted(eff_b),
                       sorted(manual_g), sorted(manual_b), new_auto)

    # 被移出生效名单的会话：删掉 chat md / 媒体 / OCR / 向量块，
    # 否则 AI 仍能检索到已经「取消监听」的内容（审计 P0-2）
    removed = {int(g) for g in (prev_g - eff_g) if int(g) > 0}
    removed |= {-int(b) for b in (prev_b - eff_b)}
    if removed and db is not None:
        from ..knowledge import purge_conversation
        for gid in sorted(removed):
            try:
                await asyncio.to_thread(purge_conversation, db, cfg, gid,
                                        drop_messages=False, drop_media=False)
            except Exception as e:  # noqa: BLE001
                log.warning("清理移出白名单的会话 %s 失败：%s", gid, e)
        log.info("已清理 %d 个被移出白名单的会话：%s", len(removed),
                 ", ".join(str(g) for g in sorted(removed)))

    return {"groups": sorted(eff_g), "buddies": sorted(eff_b),
            "manual_groups": sorted(manual_g), "manual_buddies": sorted(manual_b),
            "auto_groups": sorted(auto_g), "auto_buddies": sorted(auto_b)}


@routes.post("/settings/watch-groups")
async def settings_watch_groups(request: web.Request) -> web.Response:
    """保存群白名单：内存立即生效（同步进程读 cfg.watch_groups）+ 写回 config.yaml。"""
    cfg, db, _ = await _ctx(request)
    form = await request.post()

    selected: set[int] = set()
    for v in form.getall("groups", []):
        try:
            selected.add(int(str(v).strip()))
        except ValueError:
            continue
    # 手动输入的群号：支持逗号/空格/顿号分隔，非法数字自动忽略
    manual = str(form.get("manual") or "").strip()
    if manual:
        for tok in re.split(r"[,\s，、;；]+", manual):
            tok = tok.strip()
            if tok.isdigit():
                selected.add(int(tok))

    res = await _save_watchlist(cfg, db, groups=selected)
    log.info("群白名单已保存：生效 %d（手工 %d + 自动 %d）",
             len(res["groups"]), len(res["manual_groups"]),
             len(res["auto_groups"]))
    raise web.HTTPFound(f"/settings?saved=1&what=groups&n={len(res['groups'])}")


@routes.post("/settings/watch-groups/clear")
async def settings_watch_groups_clear(request: web.Request) -> web.Response:
    """清空手工群白名单（自动命中的群仍保留，见设置页提示）。"""
    cfg, db, _ = await _ctx(request)
    res = await _save_watchlist(cfg, db, groups=set())
    log.info("手工群白名单已清空，生效名单仍有 %d 个群（自动规则）",
             len(res["groups"]))
    raise web.HTTPFound("/settings?saved=1&what=groups&n=0")


@routes.post("/settings/watch-buddies")
async def settings_watch_buddies(request: web.Request) -> web.Response:
    """保存好友白名单：内存立即生效 + 写回 config.yaml。

    表单传来的是负数 group_id（-好友QQ），存回 watch_buddies 时转为正数 QQ 号。
    """
    cfg, db, _ = await _ctx(request)
    form = await request.post()

    selected: set[int] = set()
    for v in form.getall("buddies", []):
        try:
            selected.add(abs(int(str(v).strip())))  # 负数 gid → 正数 QQ
        except ValueError:
            continue
    manual = str(form.get("manual") or "").strip()
    if manual:
        for tok in re.split(r"[,\s，、;；]+", manual):
            tok = tok.strip().lstrip("-")
            if tok.isdigit():
                selected.add(int(tok))

    res = await _save_watchlist(cfg, db, buddies=selected)
    log.info("好友白名单已保存：生效 %d（手工 %d + 自动 %d）",
             len(res["buddies"]), len(res["manual_buddies"]),
             len(res["auto_buddies"]))
    raise web.HTTPFound(f"/settings?saved=1&what=buddies&n={len(res['buddies'])}")


@routes.post("/settings/watch-buddies/clear")
async def settings_watch_buddies_clear(request: web.Request) -> web.Response:
    """清空手工好友白名单 = 默认不同步任何好友。"""
    cfg, db, _ = await _ctx(request)
    res = await _save_watchlist(cfg, db, buddies=set())
    log.info("手工好友白名单已清空，生效名单仍有 %d 个好友（自动规则）",
             len(res["buddies"]))
    raise web.HTTPFound("/settings?saved=1&what=buddies&n=0")


@routes.post("/settings/watch-auto")
async def settings_watch_auto(request: web.Request) -> web.Response:
    """保存自动规则（备注后缀 / 置顶 / 范围），保存后立即跑一轮。"""
    cfg, db, _ = await _ctx(request)
    form = await request.post()

    from ..watchlist import apply_from_nt, auto_cfg, _norm_suffixes

    ac = auto_cfg(cfg)
    new_auto = dict(ac)
    new_auto["enabled"] = bool(form.get("enabled"))
    suffixes = _norm_suffixes(form.get("suffixes") or "")
    new_auto["by_remark_suffix"] = suffixes
    new_auto["by_pin"] = bool(form.get("by_pin"))
    scope = str(form.get("scope") or "both").strip().lower()
    new_auto["scope"] = scope if scope in ("both", "groups", "friends") else "both"

    # 只改规则，不动三层名单的其余内容
    cfg.set_watchlists([int(g) for g in (cfg.watch_groups or [])],
                       [int(b) for b in (cfg.watch_buddies or [])],
                       [int(g) for g in (cfg.watch_groups_manual or [])],
                       [int(b) for b in (cfg.watch_buddies_manual or [])],
                       new_auto)
    log.info("白名单自动规则已保存：enabled=%s 后缀=%s 置顶=%s 范围=%s",
             new_auto["enabled"], new_auto["by_remark_suffix"],
             new_auto["by_pin"], new_auto["scope"])
    if not new_auto["enabled"]:
        raise web.HTTPFound("/settings?auto_saved=1")

    # 规则变了就马上重算一轮：用户立刻能在页面上看到命中了哪些会话
    summary = await asyncio.to_thread(apply_from_nt, cfg, db)
    if summary.get("ok"):
        raise web.HTTPFound("/settings?auto=1")
    raise web.HTTPFound(f"/settings?err={quote(str(summary.get('error') or '自动规则失败'))}")


@routes.post("/settings/watch-auto/refresh")
async def settings_watch_auto_refresh(request: web.Request) -> web.Response:
    """立即按当前规则扫一遍本机库（不等到下一轮自动同步）。"""
    cfg, db, _ = await _ctx(request)
    from ..watchlist import apply_from_nt

    summary = await asyncio.to_thread(apply_from_nt, cfg, db)
    log.info("白名单自动规则手动刷新：ok=%s 群+%s/-%s 好友+%s/-%s",
             summary.get("ok"), summary.get("added_groups"),
             summary.get("removed_groups"), summary.get("added_buddies"),
             summary.get("removed_buddies"))
    if summary.get("ok"):
        raise web.HTTPFound("/settings?auto=1")
    raise web.HTTPFound(f"/settings?err={quote(str(summary.get('error') or '自动规则失败'))}")


@routes.post("/settings/watch-auto/forget-ignored")
async def settings_watch_auto_forget(request: web.Request) -> web.Response:
    """清空「手动移除」黑名单：之前删掉的自动项重新纳入。"""
    cfg, db, _ = await _ctx(request)
    from ..watchlist import apply_from_nt, auto_cfg

    new_auto = dict(auto_cfg(cfg))
    new_auto["ignore_groups"] = []
    new_auto["ignore_buddies"] = []
    cfg.set_watchlists([int(g) for g in (cfg.watch_groups or [])],
                       [int(b) for b in (cfg.watch_buddies or [])],
                       [int(g) for g in (cfg.watch_groups_manual or [])],
                       [int(b) for b in (cfg.watch_buddies_manual or [])],
                       new_auto)
    summary = await asyncio.to_thread(apply_from_nt, cfg, db)
    if summary.get("ok"):
        raise web.HTTPFound("/settings?auto=1")
    raise web.HTTPFound(f"/settings?err={quote(str(summary.get('error') or '自动规则失败'))}")
# ---------------- 知识库对外只读 API（外部机器人，见 app/kbapi.py） ----------------

_API_MAX_BODY = 64 * 1024


async def _api_json(request: web.Request) -> dict:
    """读取 JSON 请求体（允许空体），限制 64KB。"""
    body = await request.read()
    if len(body) > _API_MAX_BODY:
        raise web.HTTPRequestEntityTooLarge(
            max_size=_API_MAX_BODY, actual_size=len(body),
            text='{"ok": false, "error": "请求体过大（上限 64KB）"}',
            content_type="application/json")
    if not body:
        return {}
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise web.HTTPBadRequest(
            text='{"ok": false, "error": "请求体不是合法 JSON"}',
            content_type="application/json")
    if not isinstance(data, dict):
        raise web.HTTPBadRequest(
            text='{"ok": false, "error": "请求体必须是 JSON 对象"}',
            content_type="application/json")
    return data


def _api_groups(value) -> list[int] | None:
    """群号列表：支持 JSON 数组或 "123,456" 字符串；空 = 不过滤。"""
    if value in (None, "", []):
        return None
    if isinstance(value, str):
        value = [v for v in re.split(r"[,\s，、;；]+", value) if v]
    items = value if isinstance(value, (list, tuple, set)) else [value]
    out: list[int] = []
    for v in items:
        try:
            out.append(int(str(v).strip()))
        except (TypeError, ValueError):
            continue
    return sorted(set(out)) or None


def _api_took(t0: float) -> int:
    return int((time.monotonic() - t0) * 1000)


@routes.get("/api/kb/health")
async def api_kb_health(request: web.Request) -> web.Response:
    t0 = time.monotonic()
    cfg, db, _ = await _ctx(request)
    from ..kbapi import require
    require(request, cfg)
    stats = db.kb_stats()
    return web.json_response({
        "ok": True, "time": int(time.time()),
        "chunks": stats["chunks"], "files": stats["files"],
        "with_vector": stats["with_vector"],
        "mode": "hybrid" if stats["with_vector"] else "keyword",
        "took_ms": _api_took(t0),
    })


@routes.get("/api/kb/meta")
async def api_kb_meta(request: web.Request) -> web.Response:
    t0 = time.monotonic()
    cfg, db, _ = await _ctx(request)
    from ..kbapi import meta, require
    require(request, cfg)
    data = await asyncio.to_thread(meta, db, cfg)
    return web.json_response({"ok": True, **data, "took_ms": _api_took(t0)})


@routes.post("/api/kb/search")
async def api_kb_search(request: web.Request) -> web.Response:
    t0 = time.monotonic()
    cfg, db, _ = await _ctx(request)
    from ..kbapi import require, search
    from ..llm.client import LLMError
    require(request, cfg)
    data = await _api_json(request)
    query = str(data.get("query") or "").strip()
    if not query:
        return web.json_response({"ok": False, "error": "缺少 query"}, status=400)
    try:
        res = await asyncio.to_thread(
            search, db, cfg, query,
            k=int(data.get("k") or 8),
            groups=_api_groups(data.get("groups")),
            date_from=str(data.get("date_from") or ""),
            date_to=str(data.get("date_to") or ""),
            include_documents=bool(data.get("include_documents", True)))
    except ValueError as e:
        return web.json_response({"ok": False, "error": str(e)}, status=400)
    except LLMError as e:
        return web.json_response({"ok": False, "error": f"检索失败：{e}"}, status=502)
    except Exception as e:  # noqa: BLE001
        log.exception("知识库 API 检索失败")
        return web.json_response({"ok": False, "error": f"检索失败：{e}"}, status=500)
    return web.json_response({"ok": True, **res, "took_ms": _api_took(t0)})


@routes.post("/api/kb/ask")
async def api_kb_ask(request: web.Request) -> web.Response:
    t0 = time.monotonic()
    cfg, db, _ = await _ctx(request)
    from ..kbapi import ask, ask_agent, require
    from ..llm.client import LLMError
    require(request, cfg)
    data = await _api_json(request)
    question = str(data.get("question") or "").strip()
    if not question:
        return web.json_response({"ok": False, "error": "缺少 question"}, status=400)
    # mode=agent（默认）：跑网页端同一个 Agent，会自己多轮调工具，带引用材料；
    # mode=fast：旧的「检索一次 + 总结一次」，快但只能看 top-k。
    mode = str(data.get("mode") or "agent").strip().lower()
    history = []
    raw_hist = data.get("history")
    if isinstance(raw_hist, list):
        for m in raw_hist:
            if not isinstance(m, dict):
                continue
            role = str(m.get("role") or "").strip().lower()
            content = str(m.get("content") or "").strip()
            if role in ("user", "assistant") and content:
                history.append({"role": role, "content": content[:4000]})
        history = history[-20:]
    try:
        if mode in ("fast", "simple", "search"):
            res = await asyncio.to_thread(
                ask, db, cfg, question,
                k=int(data.get("k") or 8),
                groups=_api_groups(data.get("groups")),
                date_from=str(data.get("date_from") or ""),
                date_to=str(data.get("date_to") or ""),
                include_documents=bool(data.get("include_documents", True)))
            res = {"mode": "fast", **res}
        else:
            res = await asyncio.to_thread(ask_agent, db, cfg, question,
                                          history=history)
            res["mode"] = "agent"
    except ValueError as e:
        return web.json_response({"ok": False, "error": str(e)}, status=400)
    except LLMError as e:
        return web.json_response({"ok": False, "error": f"回答失败：{e}"}, status=502)
    except Exception as e:  # noqa: BLE001
        log.exception("知识库 API 问答失败")
        return web.json_response({"ok": False, "error": f"回答失败：{e}"}, status=500)
    return web.json_response({"ok": True, **res, "took_ms": _api_took(t0)})


@routes.get("/api/kb/overview")
async def api_kb_overview(request: web.Request) -> web.Response:
    t0 = time.monotonic()
    cfg, db, _ = await _ctx(request)
    from ..kbapi import overview, require
    require(request, cfg)
    detail = request.query.get("detail", "brief")
    try:
        data = await asyncio.to_thread(overview, db, cfg, detail)
    except Exception as e:  # noqa: BLE001
        log.exception("知识库 API 概览失败")
        return web.json_response({"ok": False, "error": f"概览失败：{e}"}, status=500)
    return web.json_response({"ok": True, **data, "took_ms": _api_took(t0)})


@routes.get("/api/kb/files")
async def api_kb_files(request: web.Request) -> web.Response:
    t0 = time.monotonic()
    cfg, db, _ = await _ctx(request)
    from ..kbapi import files, require
    require(request, cfg)
    try:
        data = await asyncio.to_thread(
            files, db, cfg,
            section=request.query.get("section", ""),
            limit=request.query.get("limit", 20),
            offset=request.query.get("offset", 0))
    except Exception as e:  # noqa: BLE001
        log.exception("知识库 API 文件清单失败")
        return web.json_response({"ok": False, "error": f"读取清单失败：{e}"}, status=500)
    return web.json_response({"ok": True, **data, "took_ms": _api_took(t0)})


@routes.post("/api/kb/document")
async def api_kb_document(request: web.Request) -> web.Response:
    t0 = time.monotonic()
    cfg, db, _ = await _ctx(request)
    from ..kbapi import read_document, require
    require(request, cfg)
    data = await _api_json(request)
    source = str(data.get("source") or "").strip()
    if not source:
        return web.json_response({"ok": False, "error": "缺少 source"}, status=400)
    try:
        res = await asyncio.to_thread(
            read_document, db, cfg, source,
            keyword=str(data.get("keyword") or ""),
            offset=data.get("offset", 0),
            max_chars=data.get("max_chars", 20000))
    except ValueError as e:
        return web.json_response({"ok": False, "error": str(e)}, status=400)
    except Exception as e:  # noqa: BLE001
        log.exception("知识库 API 读文档失败")
        return web.json_response({"ok": False, "error": f"读取失败：{e}"}, status=500)
    return web.json_response({"ok": True, **res, "took_ms": _api_took(t0)})


def _chat_payload(data: dict) -> dict:
    return {
        "keyword": str(data.get("keyword") or "").strip(),
        "group": data.get("group"),
        "group_id": data.get("group_id"),
        "user_id": data.get("user_id"),
        "important_only": bool(data.get("important_only", False)),
        "date_from": str(data.get("date_from") or ""),
        "date_to": str(data.get("date_to") or ""),
        "limit": data.get("limit", 20),
        "offset": data.get("offset", 0),
    }


@routes.post("/api/kb/chat/search")
async def api_kb_chat_search(request: web.Request) -> web.Response:
    t0 = time.monotonic()
    cfg, db, _ = await _ctx(request)
    from ..kbapi import chat_messages, require
    require(request, cfg)
    data = await _api_json(request)
    payload = _chat_payload(data)
    try:
        res = await asyncio.to_thread(chat_messages, db, cfg, **payload)
    except ValueError as e:
        return web.json_response({"ok": False, "error": str(e)}, status=400)
    except Exception as e:  # noqa: BLE001
        log.exception("知识库 API 群消息检索失败")
        return web.json_response({"ok": False, "error": f"检索失败：{e}"}, status=500)
    return web.json_response({"ok": True, **res, "took_ms": _api_took(t0)})


@routes.post("/api/kb/chat/recent")
async def api_kb_chat_recent(request: web.Request) -> web.Response:
    t0 = time.monotonic()
    cfg, db, _ = await _ctx(request)
    from ..kbapi import chat_messages, require
    require(request, cfg)
    data = await _api_json(request)
    payload = _chat_payload(data)
    payload["keyword"] = ""
    if data.get("limit") in (None, ""):
        payload["limit"] = 40
    try:
        res = await asyncio.to_thread(chat_messages, db, cfg, **payload)
    except ValueError as e:
        return web.json_response({"ok": False, "error": str(e)}, status=400)
    except Exception as e:  # noqa: BLE001
        log.exception("知识库 API 最近消息失败")
        return web.json_response({"ok": False, "error": f"读取失败：{e}"}, status=500)
    return web.json_response({"ok": True, **res, "took_ms": _api_took(t0)})


@routes.post("/api/recordings/upload")
async def api_recording_upload(request: web.Request) -> web.Response:
    """手机 App 上传录音（multipart，字段名 file）。"""
    import tempfile
    cfg, db, _ = await _ctx(request)
    from ..kbapi import require
    from ..recordings import recording_cfg, save_upload

    require(request, cfg)
    rc = recording_cfg(cfg)
    if not rc["enabled"]:
        return web.json_response({"ok": False, "error": "录音上传未启用"}, status=503)

    reader = await request.multipart()
    field = await reader.next()
    while field is not None and field.name != "file":
        field = await reader.next()
    if field is None:
        return web.json_response({"ok": False, "error": "缺少 file 字段"}, status=400)
    filename = field.filename or ""
    limit = int(rc["max_mb"] or 0) * 1024 * 1024

    tmp_dir = cfg.resolve("data/_upload_tmp")
    tmp_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix="rec_", suffix=Path(filename).suffix, dir=tmp_dir)
    os.close(fd)  # mkstemp 返回的文件句柄不关掉，Windows 上无法移动/删除
    tmp = Path(tmp_name)
    size = 0
    try:
        with tmp.open("wb") as f:
            while True:
                chunk = await field.read_chunk(1 << 20)
                if not chunk:
                    break
                size += len(chunk)
                if limit and size > limit:
                    return web.json_response(
                        {"ok": False, "error": f"文件超过 {rc['max_mb']}MB 上限"}, status=413)
                f.write(chunk)
        res = await asyncio.to_thread(save_upload, tmp, filename, db, cfg)
    except Exception as e:  # noqa: BLE001
        log.exception("录音上传失败")
        return web.json_response({"ok": False, "error": f"上传失败：{e}"}, status=500)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            log.warning('上传临时文件暂无法删除：%s', tmp)
    status = 200 if res.get("ok") else int(res.get("status", 400))
    return web.json_response({**res, "ok": bool(res.get("ok"))}, status=status)


@routes.post("/api/recordings/retry")
async def api_recording_retry(request: web.Request) -> web.Response:
    """把归档/失败的录音重新排队转写（App 手动重试）。"""
    cfg, db, _ = await _ctx(request)
    from ..kbapi import require
    from ..recordings import retry_recording

    require(request, cfg)
    try:
        data = await request.json()
    except Exception:  # noqa: BLE001
        data = {}
    rec_id = data.get("id") if isinstance(data, dict) else None
    if rec_id in (None, ""):
        return web.json_response({"ok": False, "error": "缺少录音 id"}, status=400)
    try:
        res = await asyncio.to_thread(retry_recording, int(rec_id), db, cfg)
    except Exception as e:  # noqa: BLE001
        log.exception("录音重试失败 #%s", rec_id)
        return web.json_response({"ok": False, "error": f"重试失败：{e}"}, status=500)
    status = 200 if res.get("ok") else int(res.get("status", 400))
    return web.json_response(res, status=status)


@routes.get("/api/recordings/{rec_id}")
async def api_recording_detail(request: web.Request) -> web.Response:
    """读取单条录音：元数据 + 带置信度标记的转写 + 总结 + 日程。"""
    cfg, db, _ = await _ctx(request)
    from ..kbapi import require

    require(request, cfg)
    try:
        rec_id = int(request.match_info["rec_id"])
    except ValueError:
        return web.json_response({"ok": False, "error": "无效的录音 id"}, status=400)
    row = db.get_recording(rec_id)
    if not row:
        return web.json_response({"ok": False, "error": "录音不存在"}, status=404)
    kdir = cfg.resolve(cfg.knowledge.get("dir", "data/knowledge"))
    out = {"ok": True, "recording": row}
    for key, field in (("transcript", "md_path"),
                       ("summary", "summary_path"),
                       ("schedule", "schedule_path")):
        rel = str(row.get(field) or "")
        if not rel:
            out[key] = None
            continue
        p = (kdir / rel).resolve()
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            out[key] = None
            continue
        if key == "schedule":
            import json as _json
            try:
                out[key] = _json.loads(text)
            except ValueError:
                out[key] = text
        else:
            out[key] = text
    return web.json_response(out)


@routes.get("/api/schedules")
async def api_schedules(request: web.Request) -> web.Response:
    """日历日程（群聊/文件/录音统一表），供本地机器人/MCP 读取。"""
    cfg, db, _ = await _ctx(request)
    from ..kbapi import require

    require(request, cfg)
    start = (request.query.get("start") or "").strip()
    end = (request.query.get("end") or "").strip()
    try:
        limit = max(1, min(2000, int(request.query.get("limit", "500"))))
    except ValueError:
        limit = 500
    items = db.list_schedule_items(start=start, end=end, limit=limit)
    return web.json_response({"ok": True, "items": items})


@routes.get("/api/recordings")
async def api_recordings(request: web.Request) -> web.Response:
    """列出最近上传的录音及转写状态（App 轮询用）。"""
    cfg, db, _ = await _ctx(request)
    from ..kbapi import require
    from ..recordings import recording_cfg

    require(request, cfg)
    limit = request.query.get("limit", "50")
    try:
        limit_n = max(1, min(200, int(limit)))
    except ValueError:
        limit_n = 50
    rows = db.list_recordings(limit=limit_n)
    return web.json_response({
        "ok": True,
        "stats": db.recordings_stats(),
        "courses": recording_cfg(cfg)["courses"],
        "recordings": rows,
    })
