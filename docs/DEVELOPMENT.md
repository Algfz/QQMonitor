# 开发者指南

> 面向想改代码或接手维护的人。用户手册见根目录 `README.md`。
> 本文只讲**工程结构、数据流、以及改代码时最容易踩的坑**，不含任何个人或群数据。

---

## 1. 它是一条什么流水线

```
本机 QQ(NTQQ) 加密库 nt_msg.db
        │  ① 解密（拷贝主库+WAL → 剥 1024 字节头 → PRAGMA key → 解密）
        ▼
   qq_sync.sync_from_ntqq()         每 N 分钟一轮，全表扫描白名单会话
        │  ② 按 message_id 去重写入 data/qqmonitor.db
        ▼
   messages / conversations 表
        │  ③ 媒体文件落地 data/knowledge/media/<群号>/<年-月>/<md5>.<ext>
        │  ④ 图片 OCR、群文件下载入库
        │  ⑤ 渲染 Markdown：data/knowledge/chat/<群号>_<群名>.md（每群一份，全量重建）
        ▼
   knowledge/ + 向量索引（kb_chunks，1024 维）
        │  ⑥ indexer 增量建索引（文件 mtime 未变则跳过）
        ▼
   检索 / 问答 / MCP 只读 API / Web 看板 / 日程抽取

另外两条支线：
  录音（data/inbox/*.mp3）→ 转写(FireRedASR2+说话人) → 总结 → 日程 → 全部落在 knowledge/transcripts/
  群聊消息 → 日程抽取（chat_schedule，只看当天新消息）
```

一轮完整同步约 10–40 秒（视消息量与模型响应速度）。

---

## 2. 目录与模块

```
main.py                  CLI 入口（12 个子命令，见第 8 节）
config.yaml              本地配置（不入库；模板见 config.example.yaml）
data/                    运行时数据（不入库）：库、知识库、上传临时区、日志
models/                  本地模型权重（不入库）

app/
  db.py                  存储层：13 张表 DDL + 约 80 个方法，每线程一个连接 + WAL
  config.py              配置默认值 + deep merge + 正则回写（保留注释）
  qq_sync.py             NTQQ 解密与同步主流程
  watchlist.py           白名单计算（手工基线 ∪ 自动命中 − 忽略）
  importer.py            外部导入（JSON/CSV），含「同一秒同一人内容变化」自愈
  knowledge.py           知识库同步：媒体、图片 OCR、群文件、渲染 chat md
  pipeline.py            流水线脏标记与阶段编排
  recordings.py          录音入库、转写调度、原件归档、临时目录清理
  transcribe.py / asr_firered.py / recording_ai.py   ASR 与说话人分离、录音总结
  vision.py / ocr_layout.py                         视觉模型（图片理解 / 版式 OCR）
  chat_schedule.py       群聊日程抽取（当天增量 + 按日期区间补抽）
  file_schedule.py       文档（转写稿/通知/纪要）日程抽取
  autosync_daemon.py     后台守护进程（锁文件保证单实例）
  web/server.py          aiohttp 全部路由（80 条）
  web/templates.py       纯字符串拼 HTML（内联 CSS/JS，无前端构建链）
  kbapi.py               只读知识库 API + 问答（快路径 ask / Agent 路径 ask_agent）
  llm/                   client(prompt 缓存友好) / agent(工具调用循环) / tools(工具定义)
                         indexer / retriever / local_embed / summary / intent / prompts

tools/
  qqmonitor_mcp.py       stdio MCP 服务（11 个只读工具，仅标准库），给外部 AI 挂载
  ntqq_export.py         NTQQ 解密导出（唯一实现，被 qq_sync 复用）

android-uploader/        安卓上传端（Java + 手写 build.ps1，无 Gradle）
docs/                    设计决策、安全审计、对接文档
```

**硬性约定**：Windows PowerShell 默认 GBK，含中文的命令一律写成 UTF-8 的 `.py` 脚本执行；`.bat` 必须 CRLF + GBK，否则中文参数乱码并污染库（`upsert_group` 会覆盖群名）。

---

## 3. 数据模型（13 张表）

> 逐表字段、目录命名规则、备份与隐私分级见 [`DATA-MAP.md`](DATA-MAP.md)。

| 表 | 用途 | 关键点 |
|---|---|---|
| `groups` | 群/好友会话 | 好友会话 `group_id` 为**负数**（`-QQ号`），比较 gid 时注意正负号 |
| `members` | 群成员快照 | 昵称会变，做去重别用昵称 |
| `messages` | 消息正文 | `message_id` 全局唯一；`raw_json` 只存 `{"imported":true,"source":"qq"}`，不保留原始结构 |
| `conversations` | 会话聚合 | 计数供看板用 |
| `media_ocr` | 图片 OCR 缓存 | 以「文件 + 大小 + mtime」为键，图片不变就不重识别 |
| `group_files` | 群文件对账 | `missing` 表示本机没有副本，每轮轮转重试（不是 bug） |
| `recordings` | 录音记录 | `status`: pending→transcribing→transcribed/error；产物路径按 `rec_id` 唯一 |
| `schedule_items` | 日程 | 四来源 `chat/file/recording/manual`；`manual=1` 的行不会被自动抽取覆盖 |
| `schedule_dismissed` | 日程墓碑 | 手动删过的条目 `(date,title_norm)` 命中后不再被自动加回来 |
| `kb_chunks` | 向量索引 | `dim=1024`；`source` 形如 `chat/<文件名>.md` |
| `kb_meta` | 知识库元信息 | 事件日期、自动标记 |
| `meetings` | 会议记录 | 预留，未启用 |
| `meta` | 杂项键值 | 同步游标等 |

`messages.content` 里媒体用占位符表示：`[图片:<md5>.png] [文件:<名>] [语音] [视频] [表情] [回复] [群公告] [卡片]`。
带链接的分享卡会额外追加 ` <URL>`（设计：抽卡正文时用得上）。

---

## 4. NTQQ 解密要点（最容易做错的地方）

顺序不能变：

1. 拷贝 `nt_msg.db` **主库 + WAL**（连续拷贝，间隔 <0.5s，失败重试 3 次），只拷主库会 `malformed`；
2. 跳过文件头 1024 字节；
3. `PRAGMA page_size` → `PRAGMA key` → `PRAGMA kdf_iter` / `hmac` / `kdf`；
   **key 之后再改 kdf_iter 会重新派生密钥**，解出来是乱码；
4. 消息正文在 `group_msg_table` / `c2c_msg_table` 的 `[40800]` protobuf blob 里，元素按 `(field_id, write_type)` 存；
   图片/文件/引用/卡片各有自己的字段号，群昵称在 `[40093]` 而不是 `[40090]`；
5. 会话 id 用 `COALESCE(NULLIF([40030],0), NULLIF([40027],0), [40021])` 取，单用 `[40030]` 会漏掉新建群。

`content_type` 映射：1 文本 / 2 图片 / 3 文件 / 4 语音 / 5 视频 / 6 表情 / 7 引用 / 8 系统提示 / 10 结构化卡片 / 11 表情包 / 14 富文本 / 16 合并转发卡。

---

## 5. 知识库与检索

- 目录：`data/knowledge/{chat,media,documents,transcripts}`，**不递归**，只有这三个文本目录进索引。
- `chat/*.md` 每群一份、每轮全量重建；`transcripts/*.md` 是转写稿与总结稿（AI 产物）。
- 切片：chat 按 `## 日期` 分块；其余按 600 字 + 80 字重叠，超长行硬切。
- 检索：本地 embedding（或 API embedding）余弦 + 中文 2-gram 关键词，RRF 融合，`min_sim` 兜底。
- **`.json` 绝不进索引**：转写中间产物 `*_转写.json` 与 md 内容重复，曾实测知识库有 69% 的片段是重复 JSON，删掉后片段数直接降到 31%。
- 内容没变就不要重写文件（mtime 变了会触发全量重嵌）。

---

## 6. 日程抽取（三条链路 + 手动）

| 来源 | 触发 | 说明 |
|---|---|---|
| `recording` | 录音转写完成后 | 直接从转写稿抽 |
| `file` | 每轮处理若干文档 | 扫 `transcripts/*.md`，有总结稿就跳过同名的转写稿，避免同一件事两条 |
| `chat` | 每轮处理当天新消息 | 也可 `main.py schedule-backfill --from --to` 按日期区间补抽 |
| `manual` | 页面手动添加/编辑 | `manual=1`，自动抽取只合并来源、不改日期与标题 |

判据（三条 prompt 语义逐字一致）：只收「之后还需要本人去做」或「有明确时间地点、需本人到场」的事项；**当场做完的、纯告知的、纯提醒的一律不收**；日期不确定就不输出。

置信度：`<0.5` 不入库，`0.5–0.6` 页面标「待确认」（`≤0.6` 也会标，因为「模型没给置信度」按 0.6 记），`≥0.6` 正常。

去重三层：①`dedupe_key = 日期|时间|归一标题` 精确去重；②同日标题相似度 ≥0.72（含包含关系）视为同一件事，合并来源、取更长描述；③手动删除写墓碑，防止下一轮又冒出来。

**写日程里的日期基准**：文档链路必须用**文档自身记录的日期**（front matter `date:` → 文件名里的日期 → 最后才回退 mtime）；用 mtime 会把历史事项全部记到「今天」。群聊链路按**该条消息的发送日期**换算「明天/下周三」。两者都要做日期窗口校验（早不超 7 天、晚不超 120 天）。

---

## 7. Web 与 UI

- 单进程 aiohttp，默认只绑 `127.0.0.1:8088`；`web.access_token` 非空时全站要鉴权。
- 路由 80 条：页面 GET + 表单 POST（破坏性操作用 POST + Origin 校验，无 JS 也能用）。
- 模板是 Python 字符串拼接，CSS/JS 全内联在 `templates.py`；改样式只改那一处。
- 日历页做了原地切换（`fetch` 换 `#cal-main`），时间线按「最早前 1 小时 → 最晚后 1 小时」裁剪，页面只留一个滚动条。
- 只读 API 在 `/api/kb/*`（Bearer `api.token` + 每分钟限流），`POST /api/kb/ask` 有 `fast`（检索+一次总结）和 `agent`（工具调用，返回 `citations`）两种模式。

---

## 8. 常用命令

```bash
python main.py init                        # 初始化目录与数据库
python main.py run                         # 起 WebUI（默认 127.0.0.1:8088）
python main.py sync-daemon                 # 起后台同步守护（单实例，靠锁文件）
python main.py kb [--dry]                  # 同步知识库：聊天原文 / 媒体 / inbox 分拣（内含索引）
python main.py ingest-audio [--dry]        # 把 data/inbox 里的录音、录屏转写进知识库
python main.py schedule-backfill --from 2026-10-20 --to 2026-10-28 [--dry] [--limit N]
python main.py watchlist [--preview]       # 按备注后缀 / 置顶维护监听白名单
python main.py group-files [--scan-only] [--limit 50]   # 把本机已下载的群文件复制进知识库
python main.py import <文件> [--group 123456789] [--name 班级群] [--dry] [--replace] [--force]
python main.py export [--group 123456789] [--from 2026-09-01] [--to 2026-10-31] [--out out.csv]
python main.py digest [--date 2026-10-30]
python main.py archive [--dry]             # 归档会议录制文件
```

Windows 上双击 `sync-daemon.bat` / `webui-daemon.bat` 即可无窗口常驻，`*-stop.bat` 停止。

**没有自动化测试**，验收靠命令 + 观察日志（`data/qqmonitor.log`，轮转 10MB×5）。改代码后至少跑：`kb` 一轮不报错、页面 200、日志无 Traceback。

---

## 9. 已知限制

- Windows-only：依赖 NTQQ 本地库、Win32 进程管理与 `sqlcipher3`。
- 单用户、单机，没有多租户与权限模型。
- 视觉模型（Qwen2.5-VL 3B 4bit）与 ASR（FireRedASR2）约需 7–10GB 显存；显存不足时按配置降级或走 API。
- 公众号文章对程序普遍有风控（返回验证页），抓不到的卡片大概率永远抓不到；公共新闻网之类的公开页正常。
- 群聊日程只能「往后」抽：状态文件记的是最后处理到的消息 id，历史区间要靠 `schedule-backfill` 显式补。
- 语音转写置信度低（ASR 方言/噪声）时不要当事实引用。

---

## 10. 踩坑速查（通用，按记忆顺序）

1. PowerShell 命令行直接传中文 → GBK 转码乱码并写脏库；一律写 UTF-8 `.py` 脚本执行。
2. 拷 NTQQ 库只拷主库不拷 WAL → `malformed`；拷完再解。
3. 解密 PRAGMA 顺序错 → 全表乱码，且不会报错。
4. 重建历史消息前必须先停 `sync-daemon`，否则两边同时写。
5. 群昵称在 `[40093]`；`content_type` 10 是**通用卡片容器**，不是「群公告」；真群公告的标题/正文是 base64。
6. `_转写.json` 与 `_转写.md` 内容重复，`.json` 进索引会让索引膨胀两倍以上。
7. 导入去重键含 `content`，正文一变就会重复入库；改正文要用「同群同人同秒 + 高相似度」自愈（`importer._find_same_slot`）。
8. 日程日期基准用文件 mtime → 历史事项全记到今天（最典型的 bug）。
9. 同一件事会被「录音 / 总结稿 / 群聊」各抽一遍 → 必须做跨来源相似度合并。
10. `chat_schedule` 曾经把消息正文拼好了却没塞进 prompt，模型只能自己调工具去捞。
11. 卡片消息只有标题、没有正文；正文在链接里，要么抓页面（受风控），要么让用户手动补。
12. 移动端窄屏溢出别用 `chrome --headless --window-size=390 --screenshot` 判断（会给出被裁的假象），用 Playwright 量 `scrollWidth - clientWidth`。
13. 时间线/坐标类布局用百分比，别用固定像素，否则改容器高度就错位。
14. `urllib` 会继承 `HTTP_PROXY/HTTPS_PROXY`，对端挂了代理时访问内网域名会莫名超时；MCP 脚本要显式建 `ProxyHandler({})` 的 opener。
15. 提交中文 commit message 用 `git commit -F <utf8 文件>`；PowerShell 的 `Set-Content -Encoding utf8` 会加 BOM。
16. 文件被别的进程占用时，改名/删除会失败；Windows 上目录改名还可能把「当前目录」留成空壳，改名前先停服务、切换工作目录。
