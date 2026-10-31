# 只读 API 与 MCP 接入

> 给**另一个 AI / 机器人 / 自建助手**接进来的说明：怎么开只读接口、11 个 MCP 工具各自干什么、
> 什么时候用哪个、有哪些已知坑。读这份 + [`DEVELOPMENT.md`](DEVELOPMENT.md) 就能接上。

```text
本机 NTQQ 数据库（只读）
  → 消息 / 群文件 / 图片 OCR / 语音转写
  → 清洗成 Markdown，建向量索引
  → HTTP API + MCP（stdio）
  → 另一台机器上的 AI 查询
```

- 不登录 QQ、不扫码、不占号，所以不会掉线、也不会因为协议被风控。
- 数据留在本机；只有需要总结/问答的文本会发到你配置的模型服务商。
- QQMonitor 只负责"查资料"，QQ 的收发仍由你原来的机器人负责。

---

## 1. 开启

1. `config.yaml` 里 `api.enabled: true`，把 `api.token` 设成一个随机串（建议 32 位以上）。
2. 重启 WebUI（`webui-daemon.bat` 或 `python main.py run`）。
3. 本机自测：

```bash
curl -s -H "Authorization: Bearer <TOKEN>" http://127.0.0.1:8088/api/kb/health
# {"ok": true, "chunks": <片段数>, "files": <文件数>, "mode": "hybrid"}
```

跨机器时**不要直接把 `:8088` 暴露到公网**，用 Tailscale / WireGuard / frp 之类把它包在内网里，
再把 `<你的服务器地址>:8444` 这类内网地址给对端。默认还有 60 次/分钟的限流。

---

## 2. MCP（stdio）接入

对端只要一份 `tools/qqmonitor_mcp.py`（单文件、纯标准库、无第三方依赖）和一个 Python 3.10+ 解释器。

```json
{
  "mcpServers": {
    "qqmonitor": {
      "command": "python",
      "args": ["<path>/qqmonitor_mcp.py"],
      "env": {
        "QQMONITOR_API_URL": "https://<你的服务器地址>:8444",
        "QQMONITOR_API_TOKEN": "<TOKEN>"
      }
    }
  }
}
```

要点：

- **超时调到 ≥ 300 秒**。`kb_ask` 的 agent 模式要 15–60 秒，客户端默认 60 秒会把它掐断。
- 脚本自己禁用系统代理直连（`_OPENER` 不读 `HTTP_PROXY`），所以对端设了代理也不影响；
  但如果你的内网地址只能经代理访问，请改用 HTTP 直调。
- `tools/list` 应返回 **11 个工具**；只有 8 个说明用的是旧脚本。
- 自检顺序：`kb_overview` → `rec_list(limit=3)` → `schedule_list(limit=3)` → `kb_ask("……")`。

---

## 3. 工具清单（11 个，全部只读）

### 知识库（8 个）

| 工具 | 用途 | 关键参数 |
|---|---|---|
| `kb_search` | 语义/混合检索 | `query, group, scope, include_documents, top_k` |
| `kb_ask` | 用知识库自带的 AI 回答，**并返回引用材料** | `question, mode(agent\|fast), history` |
| `kb_meta` | 索引/模型/规模 | 无 |
| `kb_overview` | 有哪些群/文件/最近内容 | 无 |
| `kb_list_files` | 列某个会话的文件 | `group_id, kind, limit` |
| `kb_read_document` | 读某份文档/转写 | `path, offset, limit` |
| `kb_search_chat` | 关键词查聊天 | `query, group, limit` |
| `kb_recent_chat` | 最近聊天 | `group_id, limit` |

### 录音 / 日程（3 个）

| 工具 | 用途 | 关键参数 |
|---|---|---|
| `rec_list` | 列录音及状态 | `limit`（1–200） |
| `rec_get` | 读一条录音的转写/总结/日程 | `id, kind=summary\|transcript\|all` |
| `schedule_list` | 查已抽取的日程 | `start, end, limit` |

### `kb_ask` 的两种模式

```text
kb_ask(question, mode="agent")   # 默认：网页端同一个 Agent，多轮查群聊/翻原文，15–60s
kb_ask(question, mode="fast")    # 检索一次 + 总结一次，3–8s，只给来源文件列表
```

agent 模式返回多出 **【引用材料】**：每条含 `kind`（`chat` 具体消息 / `chatfile` 群聊记录 /
`documents` / `transcripts` / `media`）、`title`、`day`、`原句`（≤200 字）；群消息还有
`/msg/<id>` 与群号可直接回查原文。`[图片]`/`[表情]`/`[系统提示]` 这类纯占位消息不进引用列表；
同一文件 ≤3 条、同一个群 ≤6 条；具体消息排最前。

回答正文里的 `[n]` 与引用编号不一一对应，按内容对应即可。
没有引用时说明这次回答没查到材料，可信度有限，别当结论用。

---

## 4. 录音状态机

```text
pending → transcribing → transcribed
                    ↘ error（可 retry）
```

- `transcribing` 时 `rec_get` 的 `transcript/summary/schedule` 为 `null`，不要当成失败。
- `error` 带 `detail`（如"源文件不存在"），可 `POST /api/recordings/retry`。
- 90 分钟音频约 15–25 分钟；队列串行，先看 `rec_list` 再决定等不等。

## 5. 日程的已知坑

1. **可能重复**：同一事件可能从"转写 md"和"总结 md"各抽一条，按 `date+title` 合并。
2. **相对日期**：抽取时会按消息/文档日期锚定；"下周三"这类仍可能有偏差，引用时保留原句。
3. **来源**：`source_type=chat/file/recording/manual`，可回查原文。
4. **置信度**：`confidence < 0.6` 的在页面上标"待确认"，引用时也要这么说。
5. **未识别分类**：不在白名单的录音标"未识别"，但照常转写，可人工改分类名。

---

## 6. HTTP API（MCP 不可用时兜底）

```bash
BASE=https://<你的服务器地址>:8444
TOKEN=<TOKEN>

curl -s -H "Authorization: Bearer $TOKEN" "$BASE/api/kb/health"
curl -s -H "Authorization: Bearer $TOKEN" "$BASE/api/recordings?limit=10"
curl -s -H "Authorization: Bearer $TOKEN" "$BASE/api/schedules?limit=20"

# 知识库问答（agent 模式，带引用材料）
curl -s -X POST "$BASE/api/kb/ask" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"question":"这周有什么要交的","mode":"agent"}'
```

完整路由见 [`DEVELOPMENT.md`](DEVELOPMENT.md) 的「Web / 路由」一节。

## 7. 凭据与安全

- Token 只放对端的环境变量里，不要写进代码、不要提交、不要发群。
- 服务端 token 在 `config.yaml` 的 `api.token`；轮换时两边一起改。
- 对外只暴露内网地址；不要直接映射公网端口。
- 所有 MCP 工具只读，不会修改 QQ 数据。
