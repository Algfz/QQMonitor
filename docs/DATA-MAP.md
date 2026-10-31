# 数据地图（DATA-MAP）

这份文档回答三个问题：**数据存在哪、怎么组织的、哪些丢了还能重建**。

> 约定：本文件只写结构与命名规则，**不写任何真实路径、群号、统计数字**。你本机的实际值在 `config.yaml` 和数据库里。

---

## 1. 数据的三层

```
① 原始层（不属于本项目，只读）
   QQ 客户端的加密库 nt_msg.db
        │  解密 → 白名单过滤 → 解析 → 去重
        ▼
② 本地库（唯一的权威副本）
   data/qqmonitor.db            ← 消息、成员、文件、录音、日程、向量全在这一个文件
        │  渲染 / 切分 / 嵌入 / 抽取
        ▼
③ 派生物（可删除、可重建）
   data/knowledge/…             用户可读的 Markdown 与媒体
   data/*.state.json            增量游标
```

设计上**只往 ②③ 写，永远不写 ①**。删掉 ③ 只会损失时间，删掉 ② 才会丢消息正文。

---

## 2. `data/` 目录约定

| 路径 | 内容 | 可否重建 |
|---|---|---|
| `qqmonitor.db`（+ `-wal`/`-shm`） | SQLite 主库，13 张表见下 | ❌ 备份它就等于备份了全部消息 |
| `knowledge/chat/<群号>_<群名>.md` | 每个会话一份**全量重建**的 Markdown | ✅ 重新同步即重建 |
| `knowledge/media/<群号>/<YYYY-MM>/<md5>.<ext>` | 群聊里出现过的图片/视频/语音 | ⚠️ 只有 QQ 缓存还在才能找回 |
| `knowledge/documents/<原文件名>` | 群文件、通知附件、**录音原件归档** | ⚠️ 同上，原件删了就没了 |
| `knowledge/transcripts/<课>_<日期>_r<id>_{转写,总结,日程}.{md,json}` | 录音转写、AI 总结、抽取出的日程 | ❌ 要重跑 ASR（慢且吃 GPU） |
| `inbox/` | 待处理的录音/录屏/文件（转写归档后移走） | ❌ 原始素材 |
| `cache/` | 网页正文、缩略图等缓存 | ✅ 可随时清 |
| `exports/` `digests/` `meetings/` | CSV 导出、每日摘要、会议归档 | ✅ 重新生成 |
| `*_tmp/`（`_upload_tmp`/`_ingest_tmp`/`_debug_tmp`） | 上传与转写的临时目录，超过 6 小时会被自动清理 | ✅ |
| `*.state.json` | 增量游标（见 §6） | ✅ 删了只会重扫 |

`data/` 整个目录在 `.gitignore` 里，**永远不会进仓库**。

---

## 3. 数据库：13 张表

按职责分四组。列名照抄 schema，省略类型与索引。

### 3.1 内容（真正的数据）

| 表 | 用途 | 关键列 |
|---|---|---|
| `messages` | 唯一的消息正文表 | `id`(本地自增)、`message_id`(平台 id，可为负=导入)、`group_id`(好友为负)、`user_id`、`nickname`、`msg_type`、`content`、`raw_json`、`ts`、`important`、`important_reason` |
| `groups` | 会话列表（群为正、好友为负） | `group_id`、`name`、`member_count`、`updated_at` |
| `members` | 群成员快照 | `group_id`、`user_id`、`nickname`、`card`（群昵称）、`role` |
| `conversations` | **Web 端 AI 对话历史** | `id`(uuid)、`title`、`group_id`(可选绑定)、`msg_count`、`messages`(JSON 数组，含工具调用记录) |

`content` 是渲染后的纯文本：图片/文件/语音写成 `[图片:<文件名>]` 之类占位符，卡片类消息会尽量解析出标题、正文与链接。`raw_json` 只是溯源标记（`{"imported":true,"source":"qq"}`），**不是原始结构**。

### 3.2 文件与录音

| 表 | 用途 | 关键列 |
|---|---|---|
| `group_files` | 群文件索引（不联网下载，只登记本机已有副本） | `group_id`、`msg_ts`、`name`、`size`、`md5`、`file_id`、`busid`、`local_path`、`status`、`doc_path`、`md_path` |
| `recordings` | 录音台账 | `id`、`filename`、`course`、`rec_date`、`md5`、`size`、`status`、`stored_path`、`md_path`、`summary_path`、`schedule_path`、`detail` |

`status` 取值：群文件为 `ingested`/`archived`/`duplicate`/`skipped`/`missing`（missing = 本机没下载过，等用户下载后自动重试）；录音为 `pending`/`transcribing`/`transcribed`/`error`。

### 3.3 派生数据（知识库与日程）

| 表 | 用途 | 关键列 |
|---|---|---|
| `kb_chunks` | 向量索引主表，一行一个切片 | `id`、`source`（相对路径）、`section`、`day`、`chunk_idx`、`text`、`embedding`（BLOB float32）、`dim`、`mtime` |
| `kb_meta` | 知识库元信息 | `source`、`event_date`、`end_date`、`note`、`auto`、`updated_at` |
| `media_ocr` | 图片 OCR 结果缓存（按文件 size+mtime 判失效） | `rel_path`、`size`、`mtime`、`text`、`updated_at` |
| `schedule_items` | 日程条目 | `id`、`date`、`time`、`title`、`detail`、`source_type`、`source_id`、`source_ref`、`source_quote`、`sources`(JSON)、`source_label`、`confidence`、`dedupe_key`、`manual` |
| `schedule_dismissed` | 删除墓碑：用户删掉的日程不会再被自动抽取加回来 | `dedupe_key`、`date`、`title_norm`、`created_at` |

`schedule_items` 的三类来源：`chat`（群聊消息）、`file`（转写稿/通知文档）、`recording`（录音链路），外加 `manual`（用户手填，`confidence` 固定 1.0，同步时只并来源不覆盖内容）。

`kb_chunks.source` 用的是 `knowledge/` 下的相对路径，例如 `chat/<群号>_<群名>.md`、`transcripts/<课>_<日期>_r<id>_转写.md`。**扩建索引前先看 `INDEXABLE_EXTS`**——只索引 Markdown/文本类，`.json` 不参与（会让同一堂课被重复切两遍）。

### 3.4 杂项

| 表 | 用途 | 关键列 |
|---|---|---|
| `meta` | 键值对（如库版本、初始化时间） | `key`、`value` |
| `meetings` | 会议归档（功能保留，默认几乎不用） | `id`、`title`、`held_date`、`notes`、`recording_path` |

---

## 4. 知识库文件的命名规则

| 目录 | 文件名格式 | 说明 |
|---|---|---|
| `chat/` | `<群号>_<群名>.md` | 好友是负数群号。每轮**整份重写**，内容不变则不落盘（避免全量重嵌入） |
| `media/` | `<群号>/<YYYY-MM>/<内容md5>.<ext>` | 按内容和月份分桶，同名不同内容不会互相覆盖 |
| `documents/` | `<原文件名>` | 重名会加序号；录音原件归档到这里（`<课>_<日期>_r<id>.mp3`） |
| `transcripts/` | `<课>_<日期>_r<录音id>_转写.md` | 同一录音的总结稿是 `_总结.md`、日程中间产物是 `_日程.json` |

聊天 Markdown 的固定骨架：

```markdown
# <群名> (<群号>)

## 2026-01-01

- **09:30** 昵称(QQ号): 消息正文
- **09:31** 昵称(QQ号): [图片:abc123.png]
```

`[图片:…]` 会被渲染成指向 `../media/` 的相对链接；文件不在时保留占位符而不是生成死链。

---

## 5. 配置文件（哪个含密钥）

| 文件 | 内容 | 敏感度 |
|---|---|---|
| `config.yaml` | 主配置：NTQQ 库路径与**解密密钥**、Web/API 端口与 **access token**、监听白名单、录音分类白名单、用户身份 | 🔴 高（已 gitignore） |
| `config.example.yaml` | 上面那份的模板，所有值都是占位符 | 🟢 可公开 |
| `data/llm.yaml` | AI 后端：base_url、模型名、**api_key**、embedding 与 RAG 参数 | 🔴 高 |
| `data/glossary.yaml` | ASR 术语表：机构名、分类术语、同音误识别纠错表 | 🟡 中（含机构/分类线索） |

`config.yaml` 里的白名单是「**计算结果**」：`watch_groups` / `watch_buddies` 由「手工基线 ∪ 自动命中 − 忽略」算出，真正要手改的是 `watch_groups_manual` / `watch_buddies_manual`。

---

## 6. 状态文件与增量语义

| 文件 | 作用 | 删掉的后果 |
|---|---|---|
| `autosync.status.json` | 守护进程心跳（pid、上次耗时、下次时间） | 无（下轮重写） |
| `autosync.daemon.lock` | 单实例锁（内容为 `<pid> <时间>`） | 无（但进程还在时删掉会导致双开） |
| `pipeline.state.json` | 流水线脏标记与索引失败计数 | 无（下轮全量比对） |
| `schedule_chat.state.json` | 群聊日程抽取游标 `last_msg_id` | 会重扫最近窗口，可能补出新条目 |
| `schedule_files.json` | 每个文档的 mtime 游标（文件名 → mtime） | 会重抽所有文档（慢，且可能重复，但相似度合并会兜住） |
| `watchlist.state.json` | 白名单扫描结果（上次扫到哪些群/是否变化） | 无（重扫即可） |
| `link_cache.json` | 卡片链接正文抓取缓存（成功 24h / 失败 30min） | 无（会重新抓） |

---

## 7. 什么能重建、什么不能

| 数据 | 重建方式 | 代价 |
|---|---|---|
| 聊天 Markdown | 同步一轮 | 分钟级，免费 |
| 向量索引 `kb_chunks` | 同步一轮（内容未变则跳过重嵌入） | 首次全量重建需要跑本地 embedding，几分钟到几十分钟（看显卡） |
| 图片 OCR | 重跑 OCR | 每张几秒，会重新加载视觉模型 |
| 日程条目 | 重跑抽取（`schedule-backfill` 可按日期区间补） | 每条一次 LLM 调用；顺带会因「日期不确定就不输出」而丢掉没人负责的猜测项 |
| 消息正文 | **只要 QQ 还在本地且白名单没变**，重新同步即可 | 分钟级 |
| 录音转写 | 只能拿原件重跑 ASR | 90 分钟音频约 15–25 分钟 GPU 时间 |
| 录音原件 / 群文件 / 群图片 | **不能重建**（取决于 QQ 本地缓存是否还在） | — |

所以真正需要备份的是两样：**`data/qqmonitor.db`** 和 **`data/knowledge/documents/` + `data/inbox/`**。

---

## 8. 备份要点

1. 先停守护进程（`sync-daemon-stop.bat`），否则可能拷到不一致的库。
2. 数据库是 WAL 模式，**主库和 `-wal` 要一起拷**，或者直接用 SQLite 备份接口：
   ```python
   import sqlite3
   src = sqlite3.connect("data/qqmonitor.db"); dst = sqlite3.connect("backup.db")
   src.backup(dst); dst.close(); src.close()
   ```
3. `knowledge/` 里占空间最大的是 `documents/` 与 `media/`，可按需只备份 `documents/`。

---

## 9. 隐私分级（分享、提 issue、录屏演示时看这张表）

| 等级 | 内容 | 处理建议 |
|---|---|---|
| 🔴 不可外传 | `config.yaml`（解密密钥、API token）、`data/llm.yaml`（api_key）、`qqmonitor.db`、`knowledge/chat/*.md`、`knowledge/transcripts/*.md` | 提 issue 前先换掉；演示用假数据重建库 |
| 🟠 谨慎 | `knowledge/media/`、`knowledge/documents/`、`inbox/`、`qqmonitor.log*`（含群名与文件名） | 打码或裁剪后再发 |
| 🟡 低 | `*.state.json`（含群号/文件名的摘要信息）、`data/glossary.yaml` | 可脱敏后参考 |
| 🟢 可公开 | `models/`（公开权重，13GB 级，不进仓库） | — |

另外：AI 问答会把「你的问题 + 检索到的片段」发给你在 `data/llm.yaml` 里配置的服务商；不想外发就把 chat/embedding 都切成本地模型。

---

## 10. 清理建议

`data/` 下有几类**可以安全删除**的残留（本项目历史上调试留下的）：

- `_bak_*`（旧库/旧转写备份）、`_verify_*`、`_bugcheck*`、`_regress`、`_test_audio` 等临时验证目录；
- `messages.sqlite3` 这类 0 字节的历史遗留文件；
- `*_tmp/` 下的内容（有自动清理，但手动清也行）；
- `cache/`（随时可清，会重新抓）。

删除前确认：**只删 `data/` 下的临时目录，不要碰 `qqmonitor.db`、`knowledge/documents/`、`knowledge/transcripts/`、`inbox/`。**
