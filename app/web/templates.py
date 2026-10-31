"""Web 看板模板：轻量字符串渲染，无第三方前端依赖。"""
from __future__ import annotations

import html
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from ..digest import md_to_html


def esc(s) -> str:
    # quote=True：必须同时转义引号，否则 " 会逃出 HTML 属性值造成属性注入 XSS。
    # 注意：不要用 esc 的输出直接拼进 <script> 或 on* 内联事件，JS 上下文要用
    # 数据属性（data-*）+ this.dataset 取值。
    return html.escape(str(s if s is not None else ""), quote=True)


CSS = """
:root{
  --bg:#FFF7F7; --card:#ffffff; --text:#3A2A2E; --muted:#8A7076;
  --primary:#FFD2D2; --primary-d:#FFBDBD; --primary-soft:#FFECEC;
  --primary-ink:#8B3A4A; --border:#F5D9D9;
  --green:#10b981; --amber:#f59e0b; --red:#ef4444; --radius:12px;
}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:"Segoe UI","Microsoft YaHei",system-ui,sans-serif;background:var(--bg);color:var(--text);font-size:14px}
a{color:var(--primary-ink);text-decoration:none}
a:hover{text-decoration:underline}
.layout{display:flex;min-height:100vh}
.sidebar{width:230px;background:#2B2126;color:#E7D3D6;padding:18px 12px;flex-shrink:0;position:sticky;top:0;height:100vh;overflow-y:auto}
.sidebar .logo{font-size:20px;font-weight:700;color:#fff;padding:6px 10px 16px;border-bottom:1px solid #45343A;margin-bottom:12px}
.sidebar .logo span{color:var(--primary)}
.sidebar .nav{margin:6px 0}
.sidebar .nav a{display:block;padding:8px 10px;border-radius:8px;color:#E7D3D6;font-size:14px;margin-bottom:2px}
.sidebar .nav a:hover{background:#45343A;color:#fff;text-decoration:none}
.sidebar .nav a.active{background:var(--primary);color:var(--primary-ink);font-weight:600}
.sidebar .nav .count,.sidebar .group-list a .count{float:right;font-size:11px;background:#45343A;border-radius:10px;padding:1px 8px;margin-top:1px}
.sidebar .nav a.active .count,.sidebar .group-list a.active .count{background:rgba(139,58,74,.16);color:var(--primary-ink)}
.main{flex:1;padding:22px 28px;max-width:1080px;margin:0 auto;width:100%}
.page-title{text-wrap:balance;font-size:20px;font-weight:700;margin-bottom:16px}
.card{background:var(--card);border:1px solid var(--border);border-radius:var(--radius);padding:18px;margin-bottom:16px}
.card h3{font-size:15px;margin-bottom:12px}
.stat-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:14px;margin-bottom:18px}
.stat{background:var(--card);border:1px solid var(--border);border-radius:var(--radius);padding:16px}
.stat .num{font-size:26px;font-weight:700;color:var(--primary-ink)}
.stat .label{color:var(--muted);font-size:13px;margin-top:4px}
table{width:100%;border-collapse:collapse}
th,td{padding:9px 10px;text-align:left;border-bottom:1px solid var(--border);vertical-align:top}
th{font-size:12px;color:var(--muted);font-weight:600;background:var(--primary-soft)}
tr:hover td{background:#FFF6F6}
.badge{display:inline-block;font-size:11px;padding:2px 8px;border-radius:10px;background:var(--primary-soft);color:var(--primary-ink)}
.badge.imp{background:#fef3c7;color:#92400e}
.badge.green{background:#d1fae5;color:#065f46}
.badge.red{background:#fee2e2;color:#991b1b}
.msg-list{list-style:none}
.msg-list li{display:flex;gap:12px;padding:12px 8px;border-bottom:1px solid var(--border)}
.msg-list li:last-child{border-bottom:none}
.avatar{width:38px;height:38px;border-radius:50%;display:flex;align-items:center;justify-content:center;color:#fff;font-weight:700;font-size:15px;flex-shrink:0}
.msg-body{flex:1;min-width:0}
.msg-head{display:flex;gap:8px;align-items:baseline;flex-wrap:wrap}
.msg-name{font-weight:600}
.msg-time{color:var(--muted);font-size:12px}
.msg-content{margin-top:4px;line-height:1.6;white-space:pre-wrap;word-break:break-word}
.msg-content .imp-flag{color:var(--amber)}
.toolbar{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:14px;align-items:center}
input,select,textarea,button{font-family:inherit;font-size:14px}
input[type=text],input[type=date],input[type=number],select,textarea{
  padding:8px 10px;border:1px solid var(--border);border-radius:8px;background:#fff;outline:none}
input:focus,select:focus,textarea:focus{border-color:var(--primary-d);box-shadow:0 0 0 3px var(--primary-soft)}
.btn{background:var(--primary);color:var(--primary-ink);border:none;border-radius:8px;padding:8px 16px;cursor:pointer;font-size:14px;font-weight:600}
.btn:hover{background:var(--primary-d);color:var(--primary-ink)}
.btn.ghost{background:#fff;color:var(--primary-ink);border:1px solid var(--primary-d);font-weight:500}
.btn.ghost:hover{background:var(--primary-soft)}
.btn.danger{background:#fff;color:var(--red);border:1px solid var(--red);font-weight:500}
.btn.sm{padding:4px 10px;font-size:12px}
.tip{color:var(--muted);font-size:13px}
.markdown{line-height:1.8}
.markdown h1{font-size:20px;margin:8px 0 12px}
.markdown h2{font-size:17px;margin:18px 0 8px;padding-bottom:6px;border-bottom:1px solid var(--border)}
.markdown h3{font-size:15px;margin:12px 0 6px}
.markdown ul{padding-left:22px;margin:6px 0}
.markdown li{margin:3px 0}
.markdown blockquote{border-left:3px solid var(--primary-d);padding:6px 12px;color:var(--muted);background:#FFF6F6;margin:8px 0;border-radius:0 8px 8px 0}
.markdown pre{background:#3A2A2E;color:#FFE8E8;padding:12px;border-radius:8px;overflow-x:auto;margin:8px 0}
.markdown p{margin:6px 0}
.markdown table{border-collapse:collapse;margin:10px 0;font-size:13px;display:table}
.markdown th,.markdown td{border:1px solid var(--primary-d);padding:7px 11px;text-align:left}
.markdown th{background:var(--primary-soft);color:var(--primary-ink);font-weight:600}
.markdown tbody tr:nth-child(even){background:#FFF8F8}
.empty{color:var(--muted);text-align:center;padding:36px 0}
.pager{display:flex;gap:10px;align-items:center;margin-top:12px;justify-content:center}
.digest-nav{display:flex;gap:10px;align-items:center;margin-bottom:14px}
.conn-status{font-size:12px;padding:2px 10px;border-radius:10px;margin-left:8px}
.conn-status.ok{background:#d1fae5;color:#065f46}
.conn-status.bad{background:#fee2e2;color:#991b1b}
.msg-img{max-width:320px;max-height:240px;border-radius:8px;margin-top:6px;display:block;border:1px solid var(--border);object-fit:contain}
.msg-video{max-width:320px;margin-top:6px;display:block;border-radius:8px}
.msg-audio{margin-top:6px;width:320px;max-width:100%}
.msg-file{font-size:13px}
.md-img{max-width:420px;border-radius:8px;margin:6px 0;border:1px solid var(--border)}
/* ---------- AI 对话（三栏布局） ---------- */
.chat-wrap{display:flex;gap:14px;height:calc(100vh - 116px);min-height:460px}
.chat-main{flex:1;display:flex;flex-direction:column;min-width:0;background:var(--card);
  border:1px solid var(--border);border-radius:var(--radius);overflow:hidden}
.chat-side{width:280px;flex-shrink:0;background:var(--card);border:1px solid var(--border);
  border-radius:var(--radius);padding:14px;overflow-y:auto}
.chat-side h4{font-size:13px;color:var(--muted);margin:0 0 8px;font-weight:600}
.chat-side .sec{margin-bottom:16px}
.chat-side .sec:last-child{margin-bottom:0}
.chat-msgs{flex:1;overflow-y:auto;padding:18px 20px;display:flex;flex-direction:column;gap:16px}
.bubble-row{display:flex;gap:10px;max-width:100%}
.bubble-row.user{flex-direction:row-reverse}
.bubble-avatar{width:30px;height:30px;border-radius:50%;flex-shrink:0;display:flex;
  align-items:center;justify-content:center;font-size:12px;font-weight:700;color:#fff;
  background:var(--primary-ink)}
.bubble-row.user .bubble-avatar{background:#B0899A}
.bubble{max-width:74%;padding:11px 14px;border-radius:14px;line-height:1.75;
  font-size:14px;word-break:break-word;background:#FFF6F6;border:1px solid var(--border);
  border-top-left-radius:4px}
.bubble-row.user .bubble{background:var(--primary);color:var(--text);
  border-color:var(--primary-d);border-top-left-radius:14px;border-top-right-radius:4px}
.bubble p{margin:0 0 6px}
.bubble p:last-child{margin:0}
.bubble.error{background:#FFF0F0;border-color:#F3C6C6;color:#8B3A4A}
.bubble-meta{margin-top:8px;padding-top:7px;border-top:1px solid var(--border);
  font-size:12px;color:var(--muted);display:flex;gap:10px;flex-wrap:wrap;align-items:center}
.bubble-meta a{color:var(--primary-ink)}
.tool-chip{display:inline-block;font-size:11px;padding:2px 8px;border-radius:10px;
  background:var(--primary-soft);color:var(--primary-ink);border:1px solid var(--primary-d);
  margin:3px 4px 0 0}
.src-list{list-style:none;padding:0;margin:6px 0 0;font-size:12px}
.src-list li{padding:3px 0;border-top:1px dashed var(--border)}
.src-list a{color:var(--primary-ink)}
/* 工具调用过程（Function Calling）：可折叠，默认展开最后一次 */
.tool-calls{margin:0 0 8px;border:1px solid var(--border);border-radius:10px;
  background:var(--primary-soft);overflow:hidden}
.tool-calls summary{cursor:pointer;font-size:12px;padding:6px 10px;
  color:var(--primary-ink);font-weight:600;user-select:none;list-style:none}
.tool-calls summary::-webkit-details-marker{display:none}
.tool-calls summary::before{content:'▸ ';font-size:11px}
.tool-calls[open] summary::before{content:'▾ '}
.tc-item{padding:6px 10px;border-top:1px dashed var(--border);font-size:12px;
  line-height:1.7;color:var(--muted)}
.tc-item b{color:var(--primary-ink);font-weight:600}
.tc-args{word-break:break-all}
.tc-run{display:inline-block;width:6px;height:6px;border-radius:50%;
  background:var(--primary-ink);opacity:.5;animation:blink 1.2s infinite;
  margin-right:5px;vertical-align:middle}
.tc-ok{color:#2E7D5B}
.tc-fail{color:#B23A48}
.tc-think{font-size:12px;color:var(--muted);white-space:pre-wrap;
  border-left:2px solid var(--border);padding-left:8px;margin:0 0 8px}
.chat-input{border-top:1px solid var(--border);padding:12px 14px;background:#FFFBFB}
.chat-input textarea{width:100%;border:1px solid var(--border);border-radius:10px;
  padding:10px 12px;resize:none;outline:none;font-family:inherit;font-size:14px;
  line-height:1.6;background:#fff;max-height:150px}
.chat-input textarea:focus{border-color:var(--primary-d);
  box-shadow:0 0 0 3px var(--primary-soft)}
.chat-input .row{display:flex;justify-content:space-between;align-items:center;margin-top:8px}
.typing{display:inline-block;width:7px;height:7px;border-radius:50%;
  background:var(--primary-ink);opacity:.4;animation:blink 1.2s infinite}
@keyframes blink{0%,100%{opacity:.25}50%{opacity:.9}}
.chat-empty{text-align:center;color:var(--muted);padding:40px 20px}
.chat-empty h3{font-size:17px;margin-bottom:8px;color:var(--text)}
.suggest{display:flex;flex-wrap:wrap;gap:8px;justify-content:center;margin-top:14px}
.suggest button{background:#fff;border:1px solid var(--primary-d);color:var(--primary-ink);
  border-radius:18px;padding:6px 14px;font-size:13px;cursor:pointer}
.suggest button:hover{background:var(--primary-soft)}
.side-item{font-size:12px;padding:7px 9px;border-radius:8px;background:#FFF8F8;
  border:1px solid var(--border);margin-bottom:7px;word-break:break-all}
.side-item .k{color:var(--muted);font-size:11px;display:block;margin-bottom:2px}
.side-item pre{margin:0;font-size:11px;white-space:pre-wrap;word-break:break-all;
  max-height:130px;overflow-y:auto}
/* 设置页「标签 + 输入框」行：桌面左右并排，窄屏改上下（见媒体查询） */
.field-row{display:flex;align-items:center;gap:8px;margin:6px 0}
.field-label{width:120px;font-size:13px;color:var(--muted);flex-shrink:0}
.field-tip{margin-left:128px;margin-top:-2px}
/* ---------- 移动端适配（手机 / 窄屏） ---------- */
/* 顶栏只在窄屏出现，桌面端不占位 */
.topbar{display:none}

/* 本块断点必须与 CSS_V5 里「侧栏收进抽屉」的阈值保持一致（900px）：
   ≤900px 时侧栏被 translateX(-100%) 移出屏幕，只能靠顶栏汉堡按钮叫出来。
   原来是 768px，于是 769–900px 之间侧栏已经消失、汉堡却还是 display:none，
   导航完全打不开。改这里的阈值前先看 CSS_V5 的 @media(max-width:900px)。 */
@media (max-width:900px){
  .layout{display:block}
  /* 顶栏：汉堡按钮 + 标题，吸顶 */
  .topbar{display:flex;align-items:center;gap:10px;position:sticky;top:0;z-index:30;
    background:#2B2126;padding:10px 12px}
  .topbar .logo{font-size:17px;font-weight:700;color:#fff;padding:0}
  .topbar .logo span{color:var(--primary)}
  .menu-btn{background:#45343A;border:1px solid #55434A;color:#E7D3D6;font-size:17px;
    line-height:1;padding:7px 12px;border-radius:10px;cursor:pointer}
  /* 侧边栏改抽屉：默认移出屏幕，点汉堡滑出（原来 230px 固定宽在手机上会吃掉 6 成屏幕） */
  .sidebar{position:fixed;top:0;left:0;bottom:0;width:266px;height:100vh;
    transform:translateX(-100%);transition:transform .22s ease;z-index:40}
  /* 阴影只在抽屉打开时给：关闭时整条已移出屏幕，但阴影仍会贴着左边缘透出一条 */
  body.nav-open .sidebar{transform:translateX(0);box-shadow:4px 0 20px rgba(0,0,0,.3)}
  /* 视觉遮罩统一由 CSS_V5 的 `body.nav-open .sidebar{box-shadow:0 0 0 100vmax …}`
     提供（见下方 CSS_V5 同名规则），这里不再画第二层 rgba 遮罩，否则两层叠加
     实际约 0.70，比设计值暗一倍。
     但这一层不能直接删：它是透明的全屏「命中层」。抽屉打开时，鼠标点在侧栏外的
     空白处，事件会落在 body 而不是底下的卡片/按钮上；否则关抽屉的同一次点击还会
     顺带触发底下的链接甚至「删除」按钮。点击关闭由 MOBILE_JS 的 document 级
     监听完成（判断 target 不在 .sidebar/.menu-btn 内即关闭），与本层无关。 */
  body.nav-open::after{content:"";position:fixed;inset:0;z-index:35}
  /* 正文占满宽度、收窄留白 */
  .main{padding:14px 12px;max-width:100%}
  .main-inner{padding-top:0}
  .page-title{text-wrap:balance;font-size:20px;margin-bottom:12px}
  /* 表格过宽时可横向滑动，不再撑破布局 */
  .card{padding:13px;overflow-x:auto;-webkit-overflow-scrolling:touch}
  .card h3{font-size:14px}
  th,td{padding:8px;font-size:13px}
  .stat-grid{grid-template-columns:repeat(2,minmax(0,1fr));gap:10px;margin-bottom:14px}
  .stat{padding:13px}
  .stat .num{font-size:22px}
  /* 图片/视频不超出屏幕 */
  .msg-img,.msg-video{max-width:100%;max-height:280px}
  .md-img{max-width:100%}
  /* 触控目标放大，方便手指点 */
  .sidebar .nav a,.sidebar .group-list a{padding:10px 12px;font-size:14px}
  .btn{padding:9px 15px}
  /* ≥16px 可避免 iOS 聚焦输入框时自动放大页面 */
  input[type=text],input[type=date],input[type=number],select,textarea{font-size:17px}
  /* AI 对话：三栏改上下堆叠 */
  .chat-wrap{display:block;height:auto;min-height:0}
  .chat-main{height:calc(100vh - 200px);min-height:360px}
  .chat-msgs{padding:14px 12px;gap:12px}
  .chat-side{width:100%;margin-top:12px;max-height:none}
  .bubble{max-width:88%}
  .bubble-meta{font-size:11px}
  .markdown table{display:block;overflow-x:auto;white-space:nowrap}
  .markdown pre{max-width:100%}
  /* 设置页表单行：标签独占一行、输入框占满宽度。
     （原来是 label 固定 120px 与 input 并排，手机上 input 只剩 200px 看不全内容）
     用 wrap + flex:1 而不是 block：密码框那行是「input + 显示按钮」，
     这样两者仍能并排，不会把按钮挤到下一行。 */
  .field-row{display:flex;flex-wrap:wrap;gap:8px;margin:9px 0}
  .field-label{display:block;width:100%}
  .field-row input,.field-row select{flex:1;min-width:0}
  .field-tip{margin-left:0}
  /* 输入类控件不超出屏幕宽度 */
  .main input,.main select,.main textarea{max-width:100%}
  /* 「输入框 + 按钮」横排的表单（粘贴网页链接 / 搜索 / 手动加白名单）：
     窄屏让输入框独占一行，按钮按自然宽度落到下一行；否则长按钮
     （如「抓取为 Markdown」）会被压到文字断开。内联样式的 flex/min-width
     优先级更高，得用 !important 才覆盖得动。 */
  .main form[style*="display:flex"] > input[type=text]{
    flex:1 1 100% !important;min-width:0 !important}
}

@media (max-width:400px){
  .stat-grid{grid-template-columns:1fr}
  .main{padding:12px 10px}
  .card{padding:11px}
  /* 超窄屏：内联 min-width（搜索框 200/260px 之类）会顶破布局，此时统一放开。
     只在这么窄的屏幕上才用 !important 压内联样式，避免影响正常的换行排版。 */
  .main [style*="min-width"]{min-width:0 !important}
}
"""

# 抽屉菜单的少量 JS。单独放常量而不是写进 f-string，
# 免得 JS 里的花括号要和 f-string 的插值语法打架。
CSS_V3 = """
:root{
  color-scheme:light;
  --bg:#F3F5F2; --card:#FFFFFF; --text:#171A18; --muted:#5B635E;
  --primary:#17352B; --primary-d:#10251E; --primary-soft:#E7EDE8;
  --primary-ink:#B23A2C; --border:#D7DBD5;
  --green:#2F6247; --amber:#8A6318; --red:#A9372C; --radius:4px;
  --ink:#171A18; --paper:#F3F5F2; --paper-2:#FFFFFF; --line:#D7DBD5;
  --sidebar:#141815; --sidebar-2:#1E2622;
  --sans:"Inter","Segoe UI","Microsoft YaHei",system-ui,sans-serif;
  --mono:"JetBrains Mono","Cascadia Mono","Consolas",monospace;
}
html{-webkit-font-smoothing:antialiased;accent-color:#17352B}
select,input,textarea,option{line-height:1.4}
select{appearance:none;-webkit-appearance:none;padding-right:26px;
  background-image:url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='10' height='6'><path d='M1 1l4 4 4-4' fill='none' stroke='%235b635e' stroke-width='1.5'/></svg>");
  background-repeat:no-repeat;background-position:right 9px center}
::selection{background:#17352B;color:#F3F6F1}
body{font-family:var(--sans);background:var(--paper);color:var(--text);
  font-size:14px;line-height:1.6}
a{color:var(--primary-ink);text-decoration:none;border-bottom:1px solid rgba(168,67,42,.25)}
a:hover{text-decoration:none;border-bottom-color:var(--primary-ink)}
/* ---------- 侧栏：深墨纸面 ---------- */
.sidebar{width:236px;background:var(--sidebar);color:#B9C0B6;padding:20px 12px 16px;
  border-right:1px solid #232A25}
.sidebar .logo{font-size:17px;font-weight:600;color:#F4F7F2;letter-spacing:-.01em;
  padding:0 8px 16px;border-bottom:1px solid #262E28;margin-bottom:12px}
.sidebar .logo span{color:#D08A57;font-weight:600}
.sidebar .nav a{display:block;padding:7px 10px;border-radius:8px;color:#B9C0B6;
  font-size:13px;margin-bottom:1px;border-bottom:0}
.sidebar .nav a:hover{background:var(--sidebar-2);color:#F4F7F2}
.sidebar .nav a.active{background:#223029;color:#F4F7F2;font-weight:600}
.sidebar .tip{color:#828B82}
.sidebar .nav .count,.sidebar .group-list a .count{float:right;font-family:var(--mono);
  font-size:11px;background:transparent;border:1px solid #2E3831;color:#97A096;
  border-radius:8px;padding:0 6px;margin-top:2px;line-height:1.45;font-variant-numeric:tabular-nums}
.sidebar .nav a.active .count{color:#D08A57;border-color:#5A4632}
.sidebar .group-list a{border-bottom:0;position:relative;display:block!important;
  padding:6px 56px 6px 10px!important;white-space:nowrap;overflow:hidden;
  text-overflow:ellipsis;color:#A9B2A9!important;font-size:13px!important}
.sidebar .group-list a .count{position:absolute;right:8px;top:50%;transform:translateY(-50%);
  float:none;margin:0}
.msg-list .avatar{background:#E7EBE5!important;color:#5B635E!important}
.toolbar{display:flex;gap:10px;flex-wrap:wrap;align-items:center;
  background:var(--paper-2);border:1px solid var(--line);padding:12px 14px;
  margin-bottom:16px}
.toolbar select,.toolbar input{height:32px;line-height:1.4}
.toolbar label{display:flex;align-items:center;gap:6px;font-size:13px}
.sidebar::-webkit-scrollbar{width:6px}
.sidebar::-webkit-scrollbar-thumb{background:#262E28}
/* ---------- 主区 ---------- */
.main{flex:1;height:100vh;overflow-y:auto;overscroll-behavior:contain;max-width:none;margin:0;padding:0 40px 64px;width:auto}
.main-inner{max-width:1120px;margin:0 auto;padding-top:30px}
.page-title{text-wrap:balance;font-size:26px;font-weight:600;letter-spacing:-.02em;
  margin:0 0 22px;padding:0 0 14px;border-bottom:1px solid var(--line);line-height:1.25}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;
  padding:20px 22px;margin-bottom:16px;box-shadow:none}
.card h3{font-size:15px;font-weight:600;margin:0 0 14px;letter-spacing:-.01em}
.card h3::before{content:none}
.stat-grid{gap:0;margin-bottom:22px;border-top:1px solid var(--line)}
.stat{background:transparent;border:0;border-right:1px solid var(--line);
  border-bottom:1px solid var(--line);border-radius:0;padding:18px 20px}
.stat:last-child{border-right:0}
.stat .num{font-size:30px;font-weight:600;color:var(--ink);letter-spacing:-.02em;
  font-variant-numeric:tabular-nums;
  line-height:1.1;font-variant-numeric:tabular-nums}
.stat .label{color:var(--muted);font-size:12px;letter-spacing:.08em;margin-top:6px}
/* ---------- 表格 ---------- */
table{width:100%;border-collapse:collapse;font-size:14px}
th,td{padding:10px 12px;text-align:left;border-bottom:1px solid #E6E8E2;
  vertical-align:top}
th{font-size:12px;color:var(--muted);font-weight:600;letter-spacing:.02em;
  background:transparent;border-bottom:1px solid var(--text);white-space:nowrap}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
tr:hover td{background:#EEF1EC}
td code{font-family:var(--mono);font-size:12px;color:#4C554F}
/* ---------- 标签 ---------- */
.badge{display:inline-block;font-size:11px;padding:1px 7px;border-radius:8px;
  background:transparent;color:var(--muted);border:1px solid var(--line);
  letter-spacing:.02em;font-family:var(--mono);font-variant-numeric:tabular-nums}
.badge.imp{background:transparent;color:#8A6318;border-color:#DCC79A}
.badge.green{background:transparent;color:#2F6247;border-color:#B9CFC0}
.badge.amber{background:transparent;color:#8A6318;border-color:#DCC79A}
.badge.red{background:transparent;color:#A9372C;border-color:#DDB4AE}
/* ---------- 按钮 / 表单 ---------- */
.btn{background:var(--primary);color:#F3F6F1;border:1px solid var(--primary);border-radius:8px;
  padding:8px 16px;font-size:13px;font-weight:500;letter-spacing:.02em}
.btn:hover{background:#22503F;border-color:#22503F;color:#F3F6F1}
.btn.ghost{background:transparent;color:var(--text);border:1px solid var(--line)}
.btn.ghost:hover{background:#EDE7DA;border-color:#C9BFA9}
.btn.danger{background:transparent;color:var(--red);border:1px solid #D8B7B0}
.btn.danger:hover{background:#F3DFDB}
/* 用 <button class="btn"> 做 POST 提交时，浏览器默认字体/行高/对齐与 <a class="btn">
   不一致（表单按钮不继承 body 字体），这里补齐基线，颜色与尺寸仍取自 .btn。 */
button.btn{font-family:inherit;line-height:1.5;text-align:center;cursor:pointer}
input[type=text],input[type=password],input[type=date],input[type=number],select,textarea{
  padding:8px 10px;border:1px solid var(--line);border-radius:8px;background:#FFFDF7;
  outline:none;color:var(--text)}
input:focus,select:focus,textarea:focus{border-color:#B23A2C;box-shadow:0 0 0 3px #EFE3D2}
::placeholder{color:#A9A192}
/* ---------- 消息 ---------- */
.msg-list li{padding:14px 2px;border-bottom:1px solid var(--line)}
.avatar{width:34px;height:34px;border-radius:8px;background:#E7EBE5;color:#5B635E;
  font-family:var(--serif);font-weight:600;font-size:14px}
.msg-name{font-weight:600}
.msg-time{color:var(--muted);font-size:12px;font-family:var(--mono)}
.msg-img,.md-img{border-radius:8px;border:1px solid var(--line)}
.msg-file{border-bottom:1px solid rgba(168,67,42,.25)}
/* ---------- 对话 ---------- */
.chat-wrap{gap:0;border:1px solid var(--line)}
.chat-main{border:0;border-radius:0;background:var(--paper-2)}
.chat-side{width:300px;border:0;border-left:1px solid var(--line);border-radius:0;
  background:#F3F5F2}
.chat-side h4{font-family:var(--mono);font-size:11px;letter-spacing:.14em;
  color:var(--muted)}
.chat-msgs{padding:22px 26px}
.bubble{max-width:76%;padding:12px 16px;border-radius:8px;line-height:1.75;
  font-size:14px;background:var(--paper-2);border:1px solid var(--line)}
.bubble-row.user .bubble{background:#1E2622;color:#F3F6F1;border-color:#1E2622}
.bubble-avatar{border-radius:8px;background:var(--primary);color:#F3F6F1;font-weight:600}
.bubble-row.user .bubble-avatar{background:#C08A5C}
.bubble-meta{border-top:1px dashed var(--line);font-family:var(--mono);font-size:11px;
  letter-spacing:.04em}
.tool-calls{background:#F3F5F2;border-radius:8px}
.tool-chip{background:transparent;border:1px solid var(--line);color:#6B6355;
  border-radius:8px;font-family:var(--mono);font-size:11px}
.src-list li{border-top:1px dashed var(--line)}
/* ---------- 知识库页签 / 设置锚点 ---------- */
.kf-tabs{border-bottom:1px solid var(--text);gap:0}
.kf-tab{border-radius:0;padding:9px 16px;font-size:14px;border-bottom:2px solid transparent}
.kf-tab.active{border-bottom-color:#B23A2C;color:var(--text);font-weight:600}
.kf-tile{background:transparent;border:0;border-left:2px solid var(--line);
  border-radius:0;padding:2px 0 2px 14px}
.kf-chip{border-radius:8px}
.kf-link{color:var(--primary-ink)}
.st-nav a{border-radius:8px;border-radius:8px;background:transparent;border:1px solid var(--line);
  font-family:var(--mono);font-size:12px;letter-spacing:.04em}
.st-nav a:hover{border-color:#B23A2C;color:#B23A2C}
.st-models fieldset{border:1px solid var(--line);border-radius:8px;background:#FDFCF8}
.st-adv summary{font-family:var(--mono);font-size:11px;letter-spacing:.06em;
  text-transform:uppercase;color:var(--muted)}
.st-list{border-radius:8px}
/* ---------- 其它 ---------- */
.tip{color:var(--muted);font-size:12px}
.markdown{font-size:14px;line-height:1.85}
.markdown h1,.markdown h2,.markdown h3{letter-spacing:-.01em;font-weight:600}
.markdown h2{border-bottom:1px solid var(--line)}
.markdown blockquote{border-left:2px solid #C08A5C;background:#F4EFE4;
  border-radius:0;color:#5C554A}
.markdown pre{background:#1E2622;color:#EFE7D7;border-radius:8px}
.markdown th,.markdown td{border:1px solid var(--line)}
.markdown th{background:#F3F5F2;color:#4A4438}
.markdown tbody tr:nth-child(even){background:#FAF7F0}
.empty{color:var(--muted);font-size:14px;padding:44px 0}
.conn-status{border-radius:8px;font-family:var(--mono);font-size:11px;
  letter-spacing:.06em;border:1px solid transparent}
.conn-status.ok{background:#E3EADF;color:#2F6247;border-color:#C6D5C4}
.conn-status.bad{background:#F3DFDB;color:#A9372C;border-color:#E1BEB7}
.conn-status.warn{background:#F7EFD8;color:#8A6318;border-color:#E8D8AE}
.field-label{font-size:12px;color:var(--muted)}
.topbar{background:#141815;border-bottom:1px solid #232A25}
.topbar .logo{color:#F4F7F2}
.topbar .logo span{color:#D08A57}
.menu-btn{background:#1E2622;border:1px solid #2E3831;color:#E8DFCC}
@media(max-width:720px){
  .kf-table th:nth-child(1){width:40%}
  .kf-table th:nth-child(2){width:13%}
  .kf-table th:nth-child(3){width:21%}
  .kf-table th:nth-child(4){width:26%}
  .kf-table td:nth-child(3){white-space:nowrap;padding-right:10px}
  .kf-table td:nth-child(4){padding-left:10px}
}
/* 滚动条 */
*::-webkit-scrollbar{width:10px;height:10px}
*::-webkit-scrollbar-thumb{background:#C6CCC4;border:3px solid var(--paper)}
*::-webkit-scrollbar-track{background:transparent}
/* 收尾：侧栏省略、筛选条、引用来源折叠 */
.layout{height:100vh;overflow:hidden}
.main{height:100vh;overflow-y:auto;overscroll-behavior:contain}
.doc-thumb{width:64px;height:64px;object-fit:cover;border:1px solid var(--line);
  border-radius:8px;display:inline-block;vertical-align:middle;margin-right:8px}
.doc-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(132px,1fr));
  gap:12px;margin:4px 0 10px}
.doc-tile{border:1px solid var(--line);padding:8px;background:var(--paper-2);
  display:flex;flex-direction:column;gap:6px}
.doc-tile img{width:100%;height:104px;object-fit:cover;display:block;
  border:1px solid var(--line)}
.doc-tile .doc-name{font-size:12px;line-height:1.4;max-height:2.9em;overflow:hidden;
  word-break:break-word;display:-webkit-box;-webkit-line-clamp:2;
  -webkit-box-orient:vertical}
.doc-tile .doc-meta{display:flex;flex-wrap:wrap;gap:5px;align-items:center;
  font-family:var(--mono);font-size:11px;color:var(--muted)}
.doc-tile .doc-meta button,.doc-tile .doc-meta .btn{font-size:11px;padding:1px 6px;
  min-height:0;line-height:1.6;height:auto}
.doc-tile .doc-meta form{display:inline;margin:0}
.sidebar{position:sticky;top:0;height:100vh;display:flex;flex-direction:column;
  overflow:hidden}
.sidebar .nav{flex:0 0 auto}
.sb-scroll{flex:1 1 auto;min-height:0;display:flex;flex-direction:column;
  overflow:hidden;margin-top:14px}
.sb-sec{display:flex;flex-direction:column;min-height:0}
.sb-sec:first-child{flex:0 0 194px;max-height:194px}
.sb-sec:last-child{flex:1 1 auto;min-height:130px}
.sb-h{display:flex;justify-content:space-between;align-items:baseline;
  padding:0 10px 6px;font-family:var(--mono);font-size:11px;
  letter-spacing:.14em;color:#828B82;text-transform:uppercase;flex:0 0 auto}
.sb-h span{color:#5B635E}
.sb-list{flex:1 1 auto;min-height:0;overflow-y:auto;padding-bottom:6px}
/* 侧栏里的「写操作」按钮（用 POST 表单提交，外观与链接一致；/chat/new 用它） */
.sb-list .pf-btn{border:0;background:none;margin:0;font:inherit;text-align:left;
  cursor:pointer;width:100%;appearance:none;-webkit-appearance:none}
.sb-list a,.sb-list .pf-btn{display:block;position:relative;padding:6px 54px 6px 10px;
  color:#A9B2A9;font-size:13px;white-space:nowrap;overflow:hidden;
  text-overflow:ellipsis;border-bottom:0}
.sb-list a:hover,.sb-list .pf-btn:hover{background:#1E2622;color:#F4F7F2}
.sb-list a .count{position:absolute;right:8px;top:50%;transform:translateY(-50%);
  font-family:var(--mono);font-size:11px;color:#828B82}
.sb-search{padding:4px 10px 6px}
.sb-search input{width:100%;padding:5px 8px;border-radius:8px;border:1px solid #2E3831;
  background:#1E2622;color:#E7D3D6;font-size:12px;outline:none}
.sb-search input:focus{border-color:#5A4632}
.sidebar .group-list a{white-space:nowrap!important;
  overflow:hidden!important;text-overflow:ellipsis!important;border-bottom:0!important}
.toolbar{gap:8px;position:sticky;top:0;z-index:6;background:var(--paper-2)}
.toolbar input[type=date]{width:126px}
.toolbar input[type=text]{width:150px!important}
.toolbar select{max-width:150px}
.msg-list .msg-img{max-height:96px;max-width:170px;width:auto;border-radius:8px;
  margin:6px 8px 2px 0;vertical-align:middle}
.md-img{max-height:420px;width:auto;max-width:100%}
.msg-content{max-height:none;overflow:visible}
.day-sep{list-style:none;padding:16px 2px 6px;font-family:var(--mono);font-size:11px;
  letter-spacing:.12em;color:var(--muted);border-bottom:1px solid var(--line)}
/* 侧栏/主区的窄屏改法：阈值与 CSS_V5 的抽屉断点对齐（900px），否则
   769–900px 区间会出现「侧栏已 fixed 但布局仍是桌面 overflow:hidden」的混合态 */
@media(max-width:900px){
  .layout{display:block;height:auto;overflow:visible}
  .main{height:auto;overflow:visible}
  .sidebar{position:fixed;height:100vh;overflow-y:auto}
  .sb-list{max-height:none}
}
.chat-side .src-list{max-height:320px;overflow:auto;padding-right:4px;
  border-top:1px solid var(--line);margin-top:6px}
.chat-side .src-list li{font-size:12px;line-height:1.5;padding:7px 0;
  border-top:0;border-bottom:1px dashed var(--line)}
.chat-side .src-list a{border-bottom:0;color:#5C554A}
.chat-side .src-list a:hover{color:var(--primary-ink)}
"""


MOTION_CSS = """
/* ---------- v4.1 动效层：克制、可关闭 ---------- */
@view-transition{navigation:auto}
::view-transition-old(root){animation:vt-out .16s ease both}
::view-transition-new(root){animation:vt-in .22s cubic-bezier(.22,.61,.36,1) both}
@keyframes vt-out{to{opacity:0}}
@keyframes vt-in{from{opacity:0;transform:translateY(4px)}}
@keyframes rise{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}
@keyframes fadein{from{opacity:0}to{opacity:1}}

.main-inner>*{animation:rise .42s cubic-bezier(.22,.61,.36,1) both}
.main-inner>*:nth-child(2){animation-delay:.04s}
.main-inner>*:nth-child(3){animation-delay:.08s}
.main-inner>*:nth-child(4){animation-delay:.12s}
.main-inner>*:nth-child(5){animation-delay:.16s}
.main-inner>*:nth-child(n+6){animation-delay:.2s}
.msg-list>li:nth-child(-n+8){animation:rise .4s cubic-bezier(.22,.61,.36,1) both}
.msg-list>li:nth-child(2){animation-delay:.03s}
.msg-list>li:nth-child(3){animation-delay:.06s}
.msg-list>li:nth-child(4){animation-delay:.09s}
.msg-list>li:nth-child(5){animation-delay:.12s}
.msg-list>li:nth-child(6){animation-delay:.15s}
.msg-list>li:nth-child(7){animation-delay:.18s}
.msg-list>li:nth-child(8){animation-delay:.21s}
.kf-tab,.st-nav a{animation:fadein .3s ease both}

.card{transition:border-color .18s ease,box-shadow .18s ease}
.card:hover{border-color:#C9CFC7;box-shadow:0 10px 30px -24px rgba(23,52,43,.55)}
.stat{transition:background .18s ease}
.stat:hover{background:#FBFCFA}
.stat .num{transition:color .18s ease}
.stat:hover .num{color:#17352B}
.msg-list>li{transition:background .16s ease}
.msg-list>li:hover{background:#FAFBF8}
.doc-tile{overflow:hidden;transition:border-color .18s ease}
.doc-tile:hover{border-color:#C9CFC7}
.doc-tile img{transition:transform .5s cubic-bezier(.22,.61,.36,1)}
.doc-tile:hover img{transform:scale(1.035)}
.btn{transition:background .16s ease,border-color .16s ease,color .16s ease,transform .12s ease}
.btn:active{transform:translateY(1px)}
a{transition:color .16s ease}
.sb-item{transition:background .16s ease,color .16s ease}
.topbar .logo span{display:inline-block;transition:transform .5s cubic-bezier(.22,.61,.36,1)}
.topbar .logo:hover span{transform:rotate(-8deg) scale(1.06)}
.toolbar{transition:box-shadow .2s ease,border-color .2s ease}
.toolbar.is-scrolled{box-shadow:0 8px 22px -18px rgba(23,52,43,.5);border-bottom-color:#E0E5DE}
.read-progress{position:fixed;top:0;left:236px;right:0;height:2px;z-index:40;pointer-events:none;
  background:linear-gradient(90deg,#17352B,#3C6B57);transform:scaleX(0);transform-origin:0 50%;
  transition:transform .1s linear}
/* 滚动进度条让出侧栏的左边距：阈值同样必须是 900px（侧栏收进抽屉的断点，
   见 CSS_V5）。原来写 768px，769–900px 时侧栏已不在屏幕上，进度条却仍从
   236px 处开始。 */
@media(max-width:900px){.read-progress{left:0}}
.markdown img{transition:transform .4s cubic-bezier(.22,.61,.36,1)}
.markdown img:hover{transform:scale(1.01)}
@media(prefers-reduced-motion:reduce){
  .main-inner>*, .msg-list>li, .kf-tab, .st-nav a{animation:none!important}
  .card,.stat,.stat .num,.btn,.doc-tile img,.toolbar,.read-progress,.topbar .logo span{transition:none!important}
  .doc-tile:hover img,.topbar .logo:hover span{transform:none!important}
}
"""

MOTION_JS = """
(function(){
  var rm = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  if(!rm){
    document.querySelectorAll('.stat .num').forEach(function(el){
      var raw = (el.textContent||'').trim();
      if(!/^[\\d,]+$/.test(raw)) return;
      var target = parseInt(raw.replace(/,/g,''),10);
      if(!isFinite(target) || target<=0 || target>10000000) return;
      var t0=null, dur=720;
      el.textContent='0';
      function step(ts){
        if(t0===null) t0=ts;
        var p=Math.min(1,(ts-t0)/dur), e=1-Math.pow(1-p,3);
        el.textContent=Math.round(target*e).toLocaleString('en-US');
        if(p<1) requestAnimationFrame(step);
      }
      requestAnimationFrame(step);
    });
  }
  var main=document.querySelector('.main'), bar=document.querySelector('.toolbar');
  if(main&&bar){
    var f=function(){bar.classList.toggle('is-scrolled', main.scrollTop>2);};
    main.addEventListener('scroll',f,{passive:true}); f();
  }
  var md=document.querySelector('.markdown');
  if(main&&md){
    var pb=document.createElement('div'); pb.className='read-progress';
    document.body.appendChild(pb);
    var upd=function(){
      var r=md.getBoundingClientRect(), total=md.offsetHeight-main.clientHeight;
      var p=total>0?(-r.top+main.getBoundingClientRect().top)/total:0;
      pb.style.transform='scaleX('+Math.max(0,Math.min(1,p)).toFixed(3)+')';
    };
    main.addEventListener('scroll',upd,{passive:true}); upd();
  }
})();
"""

CSS_V5 = """
/* ---------- v5 悬浮分组（iOS 设置式：内容浮在底上，分组内嵌） ---------- */
:root{--radius-lg:18px;--radius-md:14px;--shadow-card:0 1px 2px rgba(12,20,16,.04),
  0 18px 44px -34px rgba(12,20,16,.55);--shadow-bar:0 10px 30px -22px rgba(12,20,16,.5);
  --line-soft:#EDF0EB}
body{background:
  radial-gradient(1100px 620px at 106% -8%, #E1EAE2 0%, rgba(225,234,226,0) 62%),
  radial-gradient(900px 560px at -8% 108%, #E9E4D6 0%, rgba(233,228,214,0) 58%),
  #EDF0EC;
  background-attachment:fixed}
.layout{display:flex;height:100vh;min-height:0;padding:14px;gap:14px;overflow:hidden}
.sidebar{width:250px;flex:0 0 250px;height:calc(100vh - 28px);border-radius:22px;
  background:rgba(22,27,23,.9);backdrop-filter:blur(22px) saturate(1.05);
  -webkit-backdrop-filter:blur(22px) saturate(1.05);
  box-shadow:0 24px 60px -34px rgba(10,18,13,.85),0 2px 6px rgba(10,18,13,.18),inset 0 1px 0 rgba(255,255,255,.06);
  padding:16px 10px 10px;overflow:hidden;display:flex;flex-direction:column}
.sidebar .logo{margin:0 8px 12px;font-size:21px}
.sidebar .nav{display:flex;flex-direction:column;gap:2px;padding:0 2px;margin-bottom:12px}
.sidebar .nav a{padding:9px 12px;border-radius:10px;font-size:13.5px}
.sidebar .nav a.active{background:rgba(255,255,255,.11);color:#F4F7F2}
.sb-scroll{flex:1 1 auto;min-height:0;display:flex;flex-direction:column;gap:12px;padding:0 2px 2px}
.sb-sec,.sb-sec:first-child,.sb-sec:last-child{flex:1 1 0!important;min-height:0;max-height:none!important;
  background:rgba(255,255,255,.055);border:1px solid rgba(255,255,255,.07);border-radius:14px;
  padding:5px;display:flex;flex-direction:column;border-top:1px solid rgba(255,255,255,.07)!important}
.sb-h{padding:7px 9px 5px;color:#B7C0B7;font-size:11px;letter-spacing:.06em;font-family:var(--sans)}
.sb-h span{color:#8E998F}
.sb-list{flex:1 1 auto;min-height:0;overflow-y:auto;padding:0 1px 6px;scrollbar-width:thin;
  scrollbar-color:rgba(255,255,255,.22) transparent}
.sb-list::-webkit-scrollbar{width:6px}
.sb-list::-webkit-scrollbar-track{background:transparent}
.sb-list::-webkit-scrollbar-thumb{background:rgba(255,255,255,.2);border-radius:999px}
.sb-item{border-radius:10px;padding:8px 10px;margin:1px 1px}
.sb-item.active{background:rgba(255,255,255,.16);color:#fff;font-weight:600}
.sidebar .sb-list a.active{background:rgba(255,255,255,.20);color:#fff;font-weight:600;box-shadow:inset 0 0 0 1px rgba(255,255,255,.18)}
.sidebar .sb-list .count{background:transparent;border:0}
.main{flex:1 1 auto;height:calc(100vh - 28px);padding:0 6px;scrollbar-width:thin;
  scrollbar-color:rgba(18,30,23,.2) transparent}
.main::-webkit-scrollbar{width:10px}
.main::-webkit-scrollbar-track{background:transparent}
.main::-webkit-scrollbar-thumb{background:rgba(18,30,23,.16);border-radius:999px;
  border:3px solid transparent;background-clip:content-box}
.main:hover::-webkit-scrollbar-thumb{background:rgba(18,30,23,.26);background-clip:content-box}
.main-inner{max-width:1064px;padding:14px 8px 90px;display:flex;flex-direction:column;gap:16px}
.main-inner>*{margin-bottom:0;animation-fill-mode:both}
.page-title{text-wrap:balance;font-size:30px;letter-spacing:-.02em;margin:2px 4px 0}
.page-title::after{display:none}
.card,.stat-grid,.toolbar{background:rgba(255,255,255,.88);backdrop-filter:blur(16px) saturate(1.02);
  -webkit-backdrop-filter:blur(16px) saturate(1.02);
  border:1px solid rgba(255,255,255,.8);border-radius:var(--radius-lg);
  box-shadow:var(--shadow-card);padding:18px 20px}
.card:hover{border-color:rgba(255,255,255,.95);box-shadow:0 1px 2px rgba(12,20,16,.04),
  0 22px 50px -34px rgba(12,20,16,.62)}
.stat-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(148px,1fr));gap:0;padding:6px 6px}
.stat{padding:14px 18px;border-left:1px solid var(--line-soft)}
.stat:first-child{border-left:0}
.stat .num{font-size:29px}
.toolbar{display:flex;gap:10px;flex-wrap:wrap;align-items:center;padding:12px 14px;margin-bottom:0}
.toolbar.is-scrolled{box-shadow:var(--shadow-bar)}
.card>h3{margin:-2px 0 10px;font-size:15px}
table{border-collapse:separate;border-spacing:0}
th{background:transparent;border-bottom:1px solid var(--line-soft);padding:9px 8px;
  color:#6C746D;font-size:12px;font-weight:500}
td{border-bottom:1px solid #F2F4F0;padding:11px 8px}
tr:last-child td{border-bottom:0}
.msg-list>li{border-bottom:1px solid #F2F4F0}
.msg-list>li:last-child{border-bottom:0}
input,select,textarea{border-radius:10px;background:#F5F7F3;border:1px solid transparent}
input:focus,select:focus,textarea:focus{border-color:#B9C8BC;background:#fff;outline:none}
.btn{border-radius:10px}
.doc-tile{border-radius:14px;background:#F7F9F5;border:1px solid #EBEFE9}
.doc-tile img{border-radius:8px 8px 0 0}
.doc-name{padding:7px 8px 0}
.doc-meta{padding:5px 8px 8px}
.kf-tabs{border-bottom:1px solid var(--line-soft)}
.kf-tab{padding:10px 14px}
.kf-tablewrap{border-radius:14px;overflow:hidden}
.badge{border-radius:8px}
.topbar{background:rgba(22,27,23,.9);backdrop-filter:blur(18px);padding-top:env(safe-area-inset-top)}
@media(max-width:900px){
  html,body{height:auto;min-height:100%;overflow-x:clip}
  .layout{display:block;height:auto;min-height:100vh;padding:0;gap:0;overflow:visible}
  .sidebar{position:fixed;top:0;left:0;bottom:0;flex:none;width:266px;height:100vh;border-radius:0 22px 22px 0;
    transform:translateX(-100%);transition:transform .28s cubic-bezier(.25,1,.5,1);z-index:60;overflow-y:auto}
  body.nav-open .sidebar{transform:translateX(0)}
  .main{height:auto;max-width:none;margin:0;overflow:visible}
  .main-inner{height:auto;max-width:none;padding:12px 12px calc(72px + env(safe-area-inset-bottom))}
  /* 对话页：移动端改整页滚动，输入条 sticky 贴底，避免双层滚动 */
  .chat-page{height:auto;min-height:0;max-width:none}
  .chat-scroll{overflow:visible;max-height:none;mask-image:none;padding-bottom:8px}
  .chat-composer{position:fixed;left:0;right:0;bottom:0;z-index:45;padding:10px 12px calc(10px + env(safe-area-inset-bottom));
    background:linear-gradient(to top,#F3F5F2 62%,rgba(243,245,242,0))}
  .chat-page{padding-bottom:104px}
  /* 打开抽屉时用超大阴影做遮罩，点空白处由 MOBILE_JS 关闭 */
  body.nav-open .sidebar{box-shadow:0 0 0 100vmax rgba(10,16,12,.45)}
  /* 筛选工具栏：移动端改两列网格，日期并排、按钮整行 */
  form.toolbar{display:grid!important;grid-template-columns:1fr 1fr;gap:8px;align-items:center}
  form.toolbar select,form.toolbar input:not([type=checkbox]){width:100%!important;min-width:0}
  form.toolbar input[type=checkbox]{width:auto!important;margin:0}
  form.toolbar>span.tip{display:none}
  form.toolbar>label.tip{grid-column:1/-1;margin:0;display:flex;align-items:center;gap:6px;white-space:nowrap}
  form.toolbar>button,form.toolbar>a.btn{width:100%}
}
@media(prefers-reduced-motion:reduce){.card,.stat-grid,.toolbar{backdrop-filter:none}}
"""

CSS_V5B = """

.conv-title{font-weight:500;flex:1;color:var(--text);text-decoration:none}
.conv-title:hover{color:var(--primary)}
.conv-del{opacity:.5;border-color:transparent;background:transparent;color:var(--muted)}
.conv-del:hover{opacity:1;border-color:var(--accent);color:var(--accent)}


*,*::before,*::after{transition-timing-function:cubic-bezier(.25,1,.5,1)}
.page-title{text-wrap:balance}
@media(max-width:900px){.topbar .logo{min-height:44px;display:inline-flex;align-items:center}}

/* ---------- v5c 消息列表分组卡 ---------- */
.msg-list{background:rgba(255,255,255,.9);backdrop-filter:blur(16px);border:1px solid rgba(255,255,255,.82);
  border-radius:18px;box-shadow:var(--shadow-card);padding:6px 22px;margin:0;list-style:none}
.msg-list>li:first-child{padding-top:14px}
.msg-list>li:last-child{border-bottom:0}
.day-sep{padding:12px 0 8px;border-bottom:0}

/* ---------- v5b 设置页分组卡 ---------- */
.settings-page{display:flex;flex-direction:column;gap:16px}
.settings-page>.st-sec{background:rgba(255,255,255,.9);backdrop-filter:blur(16px) saturate(1.02);
  -webkit-backdrop-filter:blur(16px) saturate(1.02);
  border:1px solid rgba(255,255,255,.82);border-radius:18px;box-shadow:var(--shadow-card);
  padding:20px 22px;margin:0}
.settings-page .card{background:transparent;border:0;border-radius:0;padding:0;margin:0;box-shadow:none}
.settings-page .card+.card{margin-top:14px;padding-top:16px;border-top:1px solid var(--line-soft)}
.settings-page .st-sec>h3{margin:0 0 12px}
.settings-page fieldset{border:0;background:#F6F8F4;border-radius:14px;padding:14px 16px;margin:0}
.settings-page legend{font-size:13px;font-weight:600;padding:0 6px}
.st-nav{position:sticky;top:-1px;z-index:8;display:flex;gap:6px;flex-wrap:wrap;
  background:rgba(255,255,255,.9);backdrop-filter:blur(14px);
  border-radius:14px;padding:8px 10px;box-shadow:var(--shadow-card);margin-bottom:0;
  border:1px solid rgba(255,255,255,.8)}
.st-nav a{border:0;background:#F4F7F2;border-radius:10px;padding:6px 12px}
.st-nav a:hover{background:#E9EEE6;color:var(--primary)}
.st-nav a.active{background:#17352B;color:#F6F8F4}
.st-savebar{background:#F4F7F2;border-radius:10px;padding:9px 12px;margin-top:12px;border:1px solid var(--line-soft);
  box-shadow:none;position:sticky;bottom:10px}
.st-cols{gap:14px}
.st-cols .card{background:#F6F8F4;border-radius:14px;padding:14px 16px;border:0}
.st-list{border-radius:10px}
@media(max-width:900px){
  .settings-page>.st-sec{padding:16px}
  .st-nav{top:0;border-radius:0;margin:0 -8px 4px}
}
"""

CSS_V6 = """
/* ---------- v6 对话页 ---------- */
.chat-page{display:flex;flex-direction:column;height:calc(100vh - 158px);min-height:520px;animation:none!important;
  max-width:none;margin:0;gap:10px}
.chat-top{display:flex;align-items:center;justify-content:space-between;gap:12px;min-height:32px;
  max-width:780px;margin:0 auto;width:100%}
.chat-chips{display:flex;gap:6px;flex-wrap:wrap;align-items:center}
.chip{display:inline-flex;align-items:center;gap:5px;padding:3px 9px;border-radius:999px;
  border:1px solid var(--line);color:var(--muted);background:rgba(255,255,255,.62);
  font-size:12px;text-decoration:none;line-height:1.6}
.chip::before{content:"";width:5px;height:5px;border-radius:50%;background:#9AA39B}
.chip.ok::before{background:#2F7A57}
.chip.bad::before{background:#B23A2C}
.chip.warn{color:var(--accent);border-color:#E7C3BB}
.chat-menu{position:relative}
.menu-btn{list-style:none;cursor:pointer;width:32px;height:32px;display:flex;align-items:center;
  justify-content:center;border:1px solid var(--line);border-radius:10px;background:#fff;
  color:var(--text);font-size:16px;line-height:1}
.menu-btn::-webkit-details-marker{display:none}
.menu-btn:hover{border-color:var(--primary);color:var(--primary)}
.menu-panel{position:absolute;right:0;top:38px;z-index:30;min-width:238px;background:#fff;
  border:1px solid var(--line);border-radius:14px;box-shadow:0 18px 44px rgba(20,30,24,.14);padding:8px}
.menu-item{display:block;width:100%;text-align:left;padding:8px 10px;border:0;background:transparent;
  border-radius:8px;font:inherit;font-size:13px;color:var(--text);cursor:pointer;text-decoration:none}
.menu-item:hover{background:#F2F5F0}
.menu-item.danger{color:var(--accent)}
.menu-sep{height:1px;background:var(--line);margin:6px 4px}
.menu-label{font-size:11px;color:var(--muted);padding:6px 10px 2px}
.menu-row{display:flex;gap:6px;flex-wrap:wrap;padding:0 6px 6px}
.menu-select{font-size:12px;padding:4px 6px;border:1px solid var(--line);border-radius:8px;background:#fff}
.chat-scroll{flex:1;overflow-y:auto;padding:6px 4px 132px;display:flex;flex-direction:column;gap:26px;
  scroll-behavior:smooth;mask-image:linear-gradient(#000 calc(100% - 12px),transparent)}
.msg{width:100%;max-width:780px;margin:0 auto}
.msg.user{display:flex;justify-content:flex-end}
.user-bubble{max-width:82%;background:#17352B;color:#F3F7F2;padding:10px 14px;border-radius:14px;
  font-size:14.5px;line-height:1.72;white-space:pre-wrap;word-break:break-word}
.msg.assistant .msg-text{font-size:15.5px;line-height:1.82;color:var(--text)}
.msg.assistant .msg-text p{margin:0 0 .7em}
.msg-foot{display:flex;gap:10px;align-items:center;margin-top:6px;opacity:0;transition:opacity .15s}
.msg:hover .msg-foot,.msg:focus-within .msg-foot{opacity:1}
.msg-act{border:0;background:transparent;color:var(--muted);font-size:12px;cursor:pointer;
  padding:3px 5px;border-radius:8px}
.msg-act:hover{background:#EEF2EC;color:var(--primary)}
.msg-model{font-size:11px;color:var(--muted);font-family:var(--mono)}
.msg-sources{margin-top:8px;font-size:12px}
.msg-sources summary{cursor:pointer;color:var(--primary);width:fit-content}
.msg-sources ul{margin:6px 0 0;padding-left:18px;line-height:1.75}
.msg-sources li{color:var(--muted)}
.msg-sources .src-day{color:var(--muted);margin-left:6px;font-family:var(--mono);font-size:11px}
.msg-sources a{color:var(--primary);text-decoration:none}
.msg-sources a:hover{text-decoration:underline}
.chat-composer{position:absolute;left:0;right:0;bottom:0;padding:26px 0 10px;pointer-events:none;z-index:5;
  background:linear-gradient(to top,#F3F5F2 58%,rgba(243,245,242,0))}
.chat-composer form{pointer-events:auto;background:#fff;border:1px solid var(--line);border-radius:14px;max-width:780px;margin:0 auto;
  box-shadow:0 12px 30px rgba(20,30,24,.08);padding:10px 12px 8px}
.chat-composer textarea{width:100%;border:0;background:transparent;resize:none;font:inherit;
  font-size:15px;line-height:1.65;max-height:180px;outline:none;padding:3px 2px}
.chat-composer textarea::placeholder{color:#8A938C}
.composer-row{display:flex;justify-content:space-between;align-items:center;gap:8px;margin-top:4px}
.composer-actions{display:flex;gap:8px;align-items:center}
.chat-empty{max-width:560px;margin:54px auto;text-align:center;color:var(--muted)}
.chat-empty h3{font-size:20px;color:var(--text);margin:0 0 8px}
.chat-empty p{margin:0;font-size:13.5px}
.chat-empty .suggest{display:flex;gap:8px;flex-wrap:wrap;justify-content:center;margin-top:18px}
.chat-empty .suggest button{border:1px solid var(--line);background:#fff;border-radius:999px;
  padding:7px 13px;font-size:12.5px;color:var(--text);cursor:pointer}
.chat-empty .suggest button:hover{border-color:var(--primary);color:var(--primary)}

/* v6.1 对话页只保留一层滚动：让主区在对话页不滚，由 .chat-scroll 滚 */
@media(min-width:901px){
  .main:has(.chat-page){overflow-y:hidden}
  .main-inner:has(.chat-page){height:100%;display:flex;flex-direction:column;padding-bottom:12px;
    max-width:none;width:100%}
  .chat-page:has(.chat-scroll){height:100%;min-height:0;position:relative}
}

@media(max-width:900px){
  .page-title{font-size:26px}
  /* 移动端只保留整页滚动：对话页高度交给内容，内部不再单独滚动 */
  .chat-page{height:auto;min-height:0;padding-bottom:104px}
  .chat-scroll{overflow:visible;max-height:none;height:auto}
  /* 输入条固定在视口底部，滚动时始终可用 */
  .chat-composer{position:fixed;left:0;right:0;bottom:0;z-index:45;
    padding:10px 12px calc(10px + env(safe-area-inset-bottom));
    background:linear-gradient(to top,#F3F5F2 62%,rgba(243,245,242,0))}
  .user-bubble{max-width:88%}
  .msg.assistant .msg-text{font-size:15px}
}
"""

SB_JS = r"""
<script>
(function(){
  var KEY = 'qmsb-scroll';
  var lists = [].slice.call(document.querySelectorAll('.sidebar .sb-list'));
  function read(){
    try { return JSON.parse(sessionStorage.getItem(KEY) || '[]'); } catch(e) { return []; }
  }
  var saved = read();
  lists.forEach(function(l, i){
    if(saved[i]) l.scrollTop = saved[i];
    l.addEventListener('scroll', function(){
      var a = read(); a[i] = l.scrollTop;
      try { sessionStorage.setItem(KEY, JSON.stringify(a)); } catch(e) {}
    }, {passive:true});
  });
  window.addEventListener('pagehide', function(){
    try { sessionStorage.setItem(KEY, JSON.stringify(lists.map(function(l){ return l.scrollTop; }))); } catch(e) {}
  });
  var act = document.querySelector('.sidebar .sb-list a.active');
  if(act && act.closest){
    var l2 = act.closest('.sb-list');
    if(l2){
      var r = act.getBoundingClientRect(), lr = l2.getBoundingClientRect();
      if(r.top < lr.top || r.bottom > lr.bottom){
        l2.scrollTop += (r.top - lr.top) - (l2.clientHeight - r.height) / 2;
      }
    }
  }
})();
</script>"""

MOBILE_JS = """
(function(){
  if('serviceWorker' in navigator){
    try{navigator.serviceWorker.register('/sw.js',{scope:'/'});}catch(e){}
  }
  function closeNav(){document.body.classList.remove('nav-open');}
  document.addEventListener('click',function(e){
    if(!document.body.classList.contains('nav-open'))return;
    if(e.target.closest('.sidebar')||e.target.closest('.menu-btn'))return;
    closeNav();
  });
  document.addEventListener('keydown',function(e){
    if(e.key==='Escape')closeNav();
  });
  document.querySelectorAll('.sidebar a').forEach(function(a){
    a.addEventListener('click',closeNav);
  });
})();
"""


def _avatar_color(uid: int) -> str:
    # 暖色/粉色系，与主题一致；相邻色相拉开距离保证头像彼此可区分
    palette = ["#E8798A", "#C25E7E", "#D4A373", "#B56576", "#E39AA8",
               "#A85A6E", "#CB8E6B", "#D97B93", "#9E6070", "#EAA7B4"]
    return palette[abs(uid) % len(palette)]


def _avatar_char(name: str) -> str:
    return (name or "?")[:1]


# ---- 日历「每日时间线」：按需裁剪的竖轴 + 固定小时高度 + 当前时间线 ----
# 三条硬规则（改之前先读）：
# 1) 每小时高度恒定（_TL_HOUR_H），不随条目数压缩 —— 旧做法条目少时轴只有 220px，
#    一小时 10px，两条日程就叠成一团。
# 2) **不做嵌套滚动**：时间线不套内部滚动条（页面上只有一个滚动条）。
#    为了不让空白天占满屏，轴只画「真正用得到的那几个小时」：最早一件事的前 1 小时
#    到最晚一件事的后 1 小时（查看当天时把「现在」也算进去），最少 6 小时。
# 3) 块位置一律用百分比，且百分比是相对「裁剪后的窗口」算的
#    （top: (m - start)/span*100%），所以改 CSS 里的 --tl-h 不会让位置错乱。
_TL_HOUR_H = 52        # 每小时排版高度（px）；断点里用 --tl-h 覆盖
_TL_MIN_SPAN = 6       # 窗口最少显示多少小时（只有一件事时也不至于太窄）
_TL_PAD_BEFORE = 1     # 最早一件事之前留几小时
_TL_PAD_AFTER = 1      # 最晚一件事之后留几小时
_TL_BLOCK_H = 22       # 每块固定高（px）：块高不随轴缩放，文字才始终读得清
_TL_OVERLAP_MIN = 4    # 上下重叠超过 4px 才算「会盖住」；1px 级擦边不拆列（否则全天被切碎）
_TL_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")


def _tl_minutes(raw) -> int | None:
    """'8:05' / '08:05' → 485；解析不了（空、'待定'、'08:00-09:00'）→ None（归入「未定时间」）。"""
    m = _TL_TIME_RE.match(str(raw or "").strip())
    if not m:
        return None
    hh, mm = int(m.group(1)), int(m.group(2))
    if hh > 23 or mm > 59:
        return None
    return hh * 60 + mm


def _tl_pending(ev: dict) -> bool:
    """AI 抽取置信度 ≤ 0.6 且非手动添加 → 打「待确认」角标（手动的条目不该自我怀疑）。

    阈值写成 ≤0.6 而不是 <0.6：`app/recording_ai._confidence()` 把「模型没给置信度」
    一律记成 0.6 表示「不确定」，用严格小于会让这类条目看起来和确定的一样。
    """
    conf = ev.get("confidence")
    return (not ev.get("manual") and isinstance(conf, (int, float))
            and float(conf) <= 0.6)


def _timeline_html(events: list, day: str = "", today: str = "",
                   now: datetime | None = None) -> str:
    """把选中日的日程画成图形化竖轴（纯 Python 算位置，无 JS 布局、无图表库）。

    day/today/now：day == today 时在轴上画出「现在」红线和当前时刻。
    """
    timed: list[tuple[int, dict]] = []
    untimed: list[dict] = []
    for ev in events:
        mins = _tl_minutes(ev.get("time"))
        if mins is None:
            untimed.append(ev)
        else:
            timed.append((mins, ev))
    if not timed and not untimed:
        return ""

    parts: list[str] = []
    if timed:
        timed.sort(key=lambda p: (p[0], str(p[1].get("title") or "")))
        # 「现在」只在查看当天时参与计算（红线 + 保证现在落在可见窗口里）
        cur = (now or datetime.now()) if (today and day == today) else None
        lo = timed[0][0]
        hi = timed[-1][0]
        if cur is not None:
            now_min = cur.hour * 60 + cur.minute
            lo = min(lo, now_min)
            hi = max(hi, now_min)
        start_h = max(0, lo // 60 - _TL_PAD_BEFORE)
        end_h = min(24, hi // 60 + _TL_PAD_AFTER)
        # 太窄时按「向上多露几小时」补足最少跨度，仍不越出 0–24
        if end_h - start_h < _TL_MIN_SPAN:
            end_h = min(24, start_h + _TL_MIN_SPAN)
            start_h = max(0, end_h - _TL_MIN_SPAN)
        span = end_h - start_h
        span_min = span * 60
        off = start_h * 60
        h = span * _TL_HOUR_H

        def _pct(mins: int) -> float:
            """把「当天第几分钟」换算成裁剪窗口内的百分比。"""
            return (mins - off) / span_min * 100.0

        ys = [max(0, min(h - _TL_BLOCK_H, int(round((m - off) / span_min * h))))
              for m, _e in timed]

        placed: list[tuple[int, int, int]] = []   # (下标, 车道, 同簇列数)
        i = 0
        while i < len(timed):
            j, bottom = i, ys[i] + _TL_BLOCK_H
            # 连通区间：只要与「本簇当前底边」重叠够多就并入（链式重叠也算同一个簇）
            while j + 1 < len(timed) and bottom - ys[j + 1] > _TL_OVERLAP_MIN:
                j += 1
                bottom = max(bottom, ys[j] + _TL_BLOCK_H)
            lane_end: list[int] = []
            lane_of: dict[int, int] = {}
            for k in range(i, j + 1):
                lane = 0
                while lane < len(lane_end) and lane_end[lane] - ys[k] > _TL_OVERLAP_MIN:
                    lane += 1
                if lane == len(lane_end):
                    lane_end.append(0)
                lane_end[lane] = ys[k] + _TL_BLOCK_H
                lane_of[k] = lane
            cols = max(len(lane_end), 1)
            for k in range(i, j + 1):
                placed.append((k, lane_of[k], cols))
            i = j + 1

        blocks: list[str] = []
        for k, lane, cols in placed:
            _mins, ev = timed[k]
            raw_t = str(ev.get("time") or "").strip()
            title = esc(ev.get("title") or "未命名事项")
            tip = " · ".join(x for x in (raw_t, str(ev.get("title") or "").strip()) if x)
            pending = _tl_pending(ev)
            cls = "cal-tl-ev"
            if ev.get("manual"):
                cls += " manual"
            if pending:
                cls += " pending"
            # 列宽用百分比（任意宽度自适应）；每列右侧留缝 + 每多一列再右移 2px 拉开层次，
            # 宽度减去 (2*lane+6) 保证 left+width 永远 < 100%，窄屏也不会横向溢出。
            left = lane * (100.0 / cols)
            style = (f"top:{_pct(_mins):.4f}%;left:calc({left:.4f}% + {2 * lane}px);"
                     f"width:calc({100.0 / cols:.4f}% - {2 * lane + 6}px);"
                     f"z-index:{lane + 1}")
            badge = '<span class="cal-badge">待确认</span>' if pending else ""
            blocks.append(
                f'<div class="{cls}" style="{style}" title="{esc(tip)}">'
                f'<span class="cal-tl-t">{esc(raw_t)}</span>'
                f'<span class="cal-tl-n">{title}</span>{badge}</div>')

        # 刻度：每小时一条淡色横线 + 左侧整点数字（独立列，避免横线穿过文字）
        labels = "".join(
            f'<span style="top:{_pct(hh * 60):.4f}%">{hh}</span>'
            for hh in range(start_h, end_h + 1))
        lines = "".join(
            f'<div style="top:{_pct(hh * 60):.4f}%"></div>'
            for hh in range(start_h, end_h + 1))
        # 「现在」红线：只在查看当天时出现
        now_html = ""
        if cur is not None:
            now_label = cur.strftime("%H:%M")
            now_html = (
                f'<div class="cal-tl-now" style="top:{_pct(now_min):.4f}%" '
                f'title="当前时间 {now_label}">'
                f'<span>现在 {now_label}</span></div>')
        parts.append(
            f'<div class="cal-tl-box">'
            f'<div class="cal-tl-body" style="--tl-span:{span}">'
            f'<div class="cal-tl-gut">{labels}</div>'
            f'<div class="cal-tl-lines">{lines}</div>'
            f'{now_html}'
            f'<div class="cal-tl-track">{"".join(blocks)}</div></div></div>')

    if untimed:
        chips: list[str] = []
        for ev in untimed:
            pending = _tl_pending(ev)
            cls = "cal-tl-chip" + (" manual" if ev.get("manual") else "")
            raw_title = str(ev.get("title") or "").strip() or "未命名事项"
            badge = '<span class="cal-badge">待确认</span>' if pending else ""
            chips.append(
                f'<span class="{cls}" title="{esc(raw_title)}">'
                f'<span class="cal-tl-n">{esc(raw_title)}</span>{badge}</span>')
        parts.append(
            f'<div class="cal-tl-notime">'
            f'<div class="cal-tl-notime-h">未定时间 · {len(untimed)} 项</div>'
            f'<div class="cal-tl-chips">{"".join(chips)}</div></div>')
    return "".join(parts)


# 日历页的原地切换脚本：点日期/切月不再整页刷新（只换 #cal-main 里的内容 + 改地址栏）。
# 纯增强：脚本不跑（JS 关闭、fetch 失败）时链接本身就是普通 GET，功能不受影响。
# 不用 f-string（内含大量花括号），由 calendar_page 以 {_CAL_AJAX_JS} 插值。
_CAL_AJAX_JS = """
<script>
(function () {
  var main = document.getElementById('cal-main');
  if (!main || !window.fetch || !window.DOMParser) return;
  var busy = false;

  function load(url, push, keepScroll) {
    if (busy) return;
    busy = true;
    main.classList.add('is-loading');
    var top = keepScroll ? window.scrollY : null;
    fetch(url, {headers: {'X-Requested-With': 'fetch'}, credentials: 'same-origin'})
      .then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.text(); })
      .then(function (html) {
        var doc = new DOMParser().parseFromString(html, 'text/html');
        var next = doc.getElementById('cal-main');
        if (!next) throw new Error('missing #cal-main');
        main.innerHTML = next.innerHTML;
        if (push !== false) history.pushState({cal: 1}, '', url);
        if (top !== null) window.scrollTo(0, top);
      })
      .catch(function () { location.href = url; })
      .then(function () { busy = false; main.classList.remove('is-loading'); });
  }

  main.addEventListener('click', function (e) {
    if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    var a = e.target.closest('a[href]');
    if (!a) return;
    var href = a.getAttribute('href') || '';
    if (href.indexOf('/calendar') !== 0) return;      // 只接管日历内部跳转（来源链接等照常打开）
    if (a.target && a.target !== '_self') return;
    e.preventDefault();
    load(href, true, true);
  });

  window.addEventListener('popstate', function () {
    load(location.pathname + location.search, false, false);
  });
})();
</script>
"""


def calendar_page(month_label: str, prev_month: str, next_month: str,
                  weeks: list, events_by_day: dict, selected_day: str,
                  selected_events: list, today: str,
                  notice: str = "", error: str = "") -> str:
    """日历页：月视图 + 选中日期的图形化时间线 + 日程列表（AI 抽取 + 手动增删改）。

    时间线在上、文字列表在下；列表里的 <details> 编辑/删除表单保持原样。
    notice/error：手动增删改后的 303 回跳提示（空串表示不显示）。
    """
    week_names = ["一", "二", "三", "四", "五", "六", "日"]
    cells = []
    for week in weeks:
        for d in week:
            key = d.isoformat()
            evs = events_by_day.get(key) or []
            cls = "cal-day"
            if d.month != int(month_label.split("-")[1]):
                cls += " other"
            if key == today:
                cls += " today"
            if key == selected_day:
                cls += " sel"
            badge = f'<span class="cal-count">{len(evs)}</span>' if evs else ""
            cells.append(
                f'<a class="{cls}" href="/calendar?month={key[:7]}&day={key}" '
                f'title="{len(evs)} 项">{d.day}{badge}</a>')
    grid = "".join(f'<div class="cal-wd">{w}</div>' for w in week_names) + "".join(cells)

    if selected_events:
        rows = []
        for ev in selected_events:
            t = esc(ev.get("time") or "")
            title = esc(ev.get("title") or "未命名事项")
            course = esc(ev.get("course") or ev.get("source_label") or "")
            detail = esc(ev.get("detail") or "")
            q = esc(ev.get("quote") or "")
            src = esc(ev.get("source_time") or "")
            link_url = str(ev.get("link") or "")
            if not link_url and ev.get("transcript_name"):
                link_url = "/kb/file/transcripts/" + quote(str(ev["transcript_name"]))
            link = (f'<a class="cal-link" href="{esc(link_url)}" '
                    f'target="_blank">查看来源 →</a>') if link_url else ""
            meta = " · ".join(x for x in (course, f"原句 {src}" if src else "") if x)

            # 「待确认」：AI 抽取置信度 ≤ 0.6 的条目。手动添加的条目不算（不该自我怀疑）
            # 阈值必须与 _tl_pending 一致，否则时间线和列表会一个打标一个不打
            conf = ev.get("confidence")
            pending = (not ev.get("manual")
                       and isinstance(conf, (int, float)) and float(conf) <= 0.6)
            badge = ('<span class="cal-badge" title="AI 抽取置信度低，建议核对来源">'
                     '待确认</span>') if pending else ""

            # 手动编辑入口：<details> 展开即表单，无 JS 也能用（表单本身是普通 POST）
            ev_id = ev.get("id")
            edit = ""
            if ev_id:
                ev_date = esc(ev.get("date") or selected_day)
                ev_time = esc(ev.get("time") or "")
                ev_title = esc(ev.get("title") or "")
                ev_detail = esc(ev.get("detail") or "")
                sid = int(ev_id)
                edit = f"""
          <div class="cal-actions">
            <details class="cal-edit">
              <summary>编辑</summary>
              <form method="post" action="/calendar/item/{sid}/edit" class="cal-form">
                <input type="hidden" name="back" value="{esc(selected_day)}">
                <label>日期<input type="date" name="date" value="{ev_date}" required></label>
                <label>时间<input type="time" name="time" value="{ev_time}"></label>
                <label class="wide">事项<input type="text" name="title" value="{ev_title}"
                  maxlength="120" required></label>
                <label class="wide">备注<textarea name="detail" rows="2">{ev_detail}</textarea></label>
                <div class="cal-form-actions">
                  <button class="btn sm" type="submit">保存</button>
                  <button class="btn ghost sm" type="button"
                    onclick="this.closest('details').open=false">取消</button>
                </div>
              </form>
              <form method="post" action="/calendar/item/{sid}/delete" class="cal-del"
                onsubmit="return confirm('删除这条日程？删除后不会自动再加回来')">
                <input type="hidden" name="back" value="{esc(selected_day)}">
                <button class="btn danger sm" type="submit">删除这条日程</button>
              </form>
            </details>
          </div>"""
            rows.append(
                '<div class="cal-ev">'
                f'<div class="cal-time">{t or "--:--"}</div>'
                '<div class="cal-body">'
                f'<div class="cal-title">{title}{badge}</div>'
                + (f'<div class="cal-meta">{meta}</div>' if meta else "")
                + (f'<div class="cal-detail">{detail}</div>' if detail else "")
                + (f'<div class="cal-quote">“{q}”</div>' if q else "")
                + link + edit +
                '</div></div>')
        ev_html = "".join(rows)
    else:
        ev_html = ('<div class="cal-empty">这一天没有日程。日历来自群聊、文件和录音的 '
                   'AI 抽取，处理完会自动出现；也可以点上面的「＋ 添加日程」自己加。</div>')

    # 图形化时间线（文字列表上方；列表里的 <details> 编辑/删除表单原样保留）
    timeline_html = _timeline_html(selected_events, day=selected_day, today=today)

    error_html = f'<div class="cal-alert">{esc(error)}</div>' if error else ""
    notice_html = f'<div class="cal-ok">{esc(notice)}</div>' if notice else ""
    # 添加表单：日期默认就是当前查看的那天；整个块是 <details>，无 JS 也能展开提交
    add_html = f"""
<details class="cal-add">
  <summary>＋ 添加日程</summary>
  <form method="post" action="/calendar/item/new" class="cal-form">
    <input type="hidden" name="back" value="{esc(selected_day)}">
    <label>日期<input type="date" name="date" value="{esc(selected_day)}" required></label>
    <label>时间<input type="time" name="time"></label>
    <label class="wide">事项<input type="text" name="title" maxlength="120"
      placeholder="例如 数学单元测验" required></label>
    <label class="wide">备注<textarea name="detail" rows="2"
      placeholder="可选：地点、范围、要带的东西"></textarea></label>
    <div class="cal-form-actions">
      <button class="btn sm" type="submit">保存</button>
      <button class="btn ghost sm" type="button"
        onclick="this.closest('details').open=false">取消</button>
      <span class="tip">手动添加的日程不会被自动同步覆盖。</span>
    </div>
  </form>
</details>"""

    return f"""
<style>
.cal-top{{display:flex;align-items:center;gap:10px;margin-bottom:14px}}
.cal-month{{font-size:22px;font-weight:600;letter-spacing:-.01em}}
.cal-nav{{display:inline-flex;gap:6px;margin-left:auto}}
.cal-nav a{{border:1px solid rgba(23,53,43,.18);border-radius:8px;padding:6px 12px;
  font-size:13px;color:#17352B;background:rgba(255,255,255,.6)}}
.cal-nav a:hover{{background:#fff}}
.cal-card{{background:#fff;border-radius:18px;padding:18px 20px;
  box-shadow:0 10px 30px rgba(23,53,43,.08);border:1px solid rgba(23,53,43,.06)}}
.cal-grid{{display:grid;grid-template-columns:repeat(7,minmax(0,1fr));gap:6px}}
.cal-wd{{font-size:11px;color:#8A918B;text-align:center;padding:4px 0;letter-spacing:.08em}}
.cal-day{{position:relative;display:flex;align-items:flex-start;justify-content:flex-end;
  min-height:58px;padding:8px;border-radius:10px;color:#2A332F;font-size:13px;
  background:#F7F9F6;border:1px solid transparent}}
.cal-day:hover{{background:#EEF3EE;text-decoration:none}}
.cal-day.other{{opacity:.35}}
.cal-day.today{{border-color:#17352B;font-weight:700}}
.cal-day.sel{{background:#17352B;color:#fff}}
.cal-count{{position:absolute;left:8px;bottom:8px;background:#B23A2C;color:#fff;
  border-radius:999px;font-size:10px;line-height:16px;min-width:16px;text-align:center;padding:0 4px}}
.cal-day.sel .cal-count{{background:#FBE3DE;color:#8E2E24}}
.cal-list{{margin-top:18px}}
.cal-list-h{{font-size:13px;color:#5B635E;margin:0 0 10px;letter-spacing:.02em}}
.cal-ev{{display:flex;gap:16px;background:#fff;border-radius:14px;padding:14px 18px;
  margin-bottom:10px;border:1px solid rgba(23,53,43,.06);box-shadow:0 6px 18px rgba(23,53,43,.05)}}
.cal-time{{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:14px;font-weight:600;
  color:#B23A2C;min-width:64px}}
.cal-body{{flex:1;min-width:0}}
.cal-title{{font-size:15px;font-weight:600;color:#171A18;margin-bottom:4px}}
.cal-meta{{font-size:12px;color:#6B736D;margin-bottom:4px}}
.cal-detail{{font-size:13px;color:#3C4540;line-height:1.65}}
.cal-quote{{font-size:12px;color:#8A918B;margin-top:6px;line-height:1.6}}
.cal-link{{display:inline-block;margin-top:8px;font-size:12px;color:#17352B;
  border-bottom:1px solid rgba(23,53,43,.3);padding-bottom:1px}}
.cal-empty{{color:#8A918B;font-size:13px;line-height:1.8;background:#fff;border-radius:14px;
  padding:22px;border:1px dashed rgba(23,53,43,.15)}}
/* 手动增删改：提示条、添加按钮、行内表单、待确认徽标 */
.cal-alert{{margin:0 0 12px;padding:9px 13px;border-radius:8px;font-size:13px;
  color:#B23A2C;background:#FBE3DE;border:1px solid rgba(178,58,44,.3)}}
.cal-ok{{margin:0 0 12px;padding:9px 13px;border-radius:8px;font-size:13px;
  color:#17352B;background:#E4EFE7;border:1px solid rgba(23,53,43,.18)}}
.cal-add{{margin:0 0 14px}}
.cal-add>summary{{display:inline-flex;align-items:center;gap:6px;list-style:none;
  cursor:pointer;border:1px solid rgba(23,53,43,.18);border-radius:8px;padding:6px 12px;
  font-size:13px;color:#17352B;background:rgba(255,255,255,.6)}}
.cal-add>summary:hover{{background:#fff}}
.cal-add>summary::-webkit-details-marker{{display:none}}
.cal-form{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px;
  margin-top:10px;padding:12px 14px;background:#fff;border-radius:12px;
  border:1px solid rgba(23,53,43,.08)}}
.cal-form label{{display:flex;flex-direction:column;gap:4px;font-size:12px;color:#6B736D}}
.cal-form .wide{{grid-column:1 / -1}}
.cal-form input,.cal-form textarea{{font:inherit;font-size:13px;color:#171A18;
  border:1px solid rgba(23,53,43,.18);border-radius:8px;padding:6px 9px;background:#fff}}
.cal-form textarea{{resize:vertical;min-height:52px}}
.cal-form-actions{{grid-column:1 / -1;display:flex;align-items:center;gap:10px;margin-top:2px}}
.cal-badge{{display:inline-block;margin-left:8px;font-size:11px;line-height:18px;
  padding:0 7px;border-radius:999px;color:#B23A2C;background:#FBE3DE;font-weight:400;
  border:1px solid rgba(178,58,44,.35);vertical-align:2px}}
.cal-actions{{margin-top:10px}}
.cal-edit>summary{{display:inline-block;list-style:none;cursor:pointer;font-size:12px;
  color:#17352B;border-bottom:1px solid rgba(23,53,43,.3);padding-bottom:1px}}
.cal-edit>summary::-webkit-details-marker{{display:none}}
.cal-del{{margin-top:10px}}
/* 每日时间线：按需裁剪的小时窗口 + 固定 --tl-h（不做嵌套滚动，整页只有一个滚动条） */
.cal-tl-box{{position:relative;margin:2px 0 16px;border-radius:12px;background:rgba(255,255,255,.55);
  border:1px solid rgba(23,53,43,.07);padding:8px 0 10px}}
.cal-tl-body{{position:relative;--tl-h:{_TL_HOUR_H}px;--tl-span:8;height:calc(var(--tl-span) * var(--tl-h))}}
.cal-tl-gut{{position:absolute;left:0;top:0;bottom:0;width:52px;pointer-events:none}}
.cal-tl-gut span{{position:absolute;right:6px;transform:translateY(-50%);
  font-family:ui-monospace,Menlo,Consolas,monospace;font-size:10.5px;line-height:1;
  color:#8A918B;letter-spacing:.02em}}
.cal-tl-lines{{position:absolute;left:52px;right:0;top:0;bottom:0;pointer-events:none}}
.cal-tl-lines div{{position:absolute;left:0;right:0;border-top:1px solid rgba(23,53,43,.07)}}
.cal-tl-track{{position:absolute;left:52px;right:0;top:0;bottom:0}}
.cal-tl-now{{position:absolute;left:52px;right:0;height:0;border-top:2px solid #B23A2C;
  z-index:9;pointer-events:none}}
.cal-tl-now::before{{content:"";position:absolute;left:-4px;top:-5px;width:8px;height:8px;
  border-radius:50%;background:#B23A2C}}
.cal-tl-now span{{position:absolute;right:6px;top:-17px;font-size:10.5px;font-weight:600;
  color:#8E2E24;background:#FBE3DE;border:1px solid rgba(178,58,44,.35);border-radius:999px;
  line-height:15px;padding:0 7px;white-space:nowrap}}
.cal-tl-ev{{position:absolute;box-sizing:border-box;height:{_TL_BLOCK_H}px;display:flex;
  align-items:center;gap:5px;padding:0 7px;border-radius:8px;font-size:12.5px;line-height:1;
  background:#F7F9F6;border:1px solid rgba(23,53,43,.12);overflow:hidden;
  white-space:nowrap;color:#171A18}}
.cal-tl-ev.manual{{border-left:3px solid #B23A2C;padding-left:5px}}
.cal-tl-ev.pending{{background:#FBE3DE;border-color:rgba(178,58,44,.35)}}
.cal-tl-t{{flex:0 0 auto;font-family:ui-monospace,Menlo,Consolas,monospace;font-size:11px;
  font-weight:600;color:#B23A2C}}
.cal-tl-n{{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
  font-weight:500;color:#171A18}}
.cal-tl-ev .cal-tl-n{{flex:1 1 auto}}
.cal-tl-ev .cal-badge,.cal-tl-chip .cal-badge{{flex:0 0 auto;margin-left:2px;font-size:9.5px;
  line-height:14px;padding:0 4px;vertical-align:baseline;border-radius:999px}}
.cal-tl-notime{{margin-top:2px;padding:10px 12px;background:rgba(255,255,255,.55);
  border:1px dashed rgba(23,53,43,.15);border-radius:12px}}
.cal-tl-notime-h{{font-size:11.5px;color:#8A918B;margin-bottom:8px;letter-spacing:.04em}}
.cal-tl-chips{{display:flex;flex-wrap:wrap;gap:6px}}
.cal-tl-chip{{display:inline-flex;align-items:center;gap:4px;max-width:100%;min-width:0;
  padding:4px 9px;border-radius:999px;background:#F7F9F6;border:1px solid rgba(23,53,43,.12);
  font-size:12px;line-height:1.4;color:#171A18}}
.cal-tl-chip.manual{{border-left:3px solid #B23A2C;padding-left:7px}}
.cal-tl-chip .cal-tl-n{{max-width:280px}}
@media(max-width:850px){{
  .cal-tl-body{{--tl-h:44px}}
  .cal-tl-gut{{width:40px}}
  .cal-tl-lines,.cal-tl-track,.cal-tl-now{{left:40px}}
  .cal-tl-gut span{{font-size:10px;right:4px}}
  .cal-tl-ev{{font-size:12px;height:20px;padding:0 5px;gap:4px}}
  .cal-tl-t{{font-size:10.5px}}
  .cal-tl-chip .cal-tl-n{{max-width:200px}}
}}
@media(max-width:640px){{
  .cal-top{{flex-wrap:wrap;gap:8px;margin-bottom:12px}}
  .cal-month{{width:100%;font-size:19px}}
  .cal-nav{{width:100%;margin-left:0;justify-content:space-between;gap:4px}}
  .cal-nav a{{padding:5px 9px;font-size:12px}}
  .cal-card{{padding:10px;overflow:hidden}}
  .cal-grid{{gap:3px}}
  .cal-wd{{font-size:10px;padding:2px 0}}
  .cal-day{{min-height:38px;padding:4px;font-size:11px;border-radius:7px}}
  .cal-count{{left:4px;bottom:4px;font-size:9px;line-height:14px;min-width:14px}}
  .cal-ev{{padding:12px 14px;gap:4px;flex-direction:column}}
  .cal-time{{min-width:0;font-size:13px}}
  .cal-list-h{{font-size:12px}}
  .cal-form{{grid-template-columns:1fr}}
  /* 390px 下时间线不横向溢出：刻度区再收窄，块内字号与徽标同步缩小 */
  .cal-tl-body{{--tl-h:40px}}
  .cal-tl-gut{{width:34px}}
  .cal-tl-lines,.cal-tl-track,.cal-tl-now{{left:34px}}
  .cal-tl-ev{{font-size:11.5px;height:19px;padding:0 4px;gap:3px;border-radius:6px}}
  .cal-tl-now span{{font-size:9.5px;right:2px}}
  .cal-tl-ev .cal-badge,.cal-tl-chip .cal-badge{{font-size:9px;line-height:13px;padding:0 3px}}
  .cal-tl-notime{{padding:8px 10px}}
  .cal-tl-chip{{padding:3px 8px;font-size:11.5px}}
}}
#cal-main.is-loading{{opacity:.55}}
</style>
<div id="cal-main">
{error_html}{notice_html}
<div class="cal-top">
  <div class="cal-month">{month_label.replace("-", " 年 ")} 月</div>
  <div class="cal-nav">
    <a href="/calendar?month={prev_month}">‹ 上月</a>
    <a href="/calendar?month={today[:7]}&day={today}">今天</a>
    <a href="/calendar?month={next_month}">下月 ›</a>
  </div>
</div>
{add_html}
<div class="cal-card"><div class="cal-grid">{grid}</div></div>
<div class="cal-list">
  <div class="cal-list-h">{selected_day} · {len(selected_events)} 项日程</div>
  {timeline_html}
  {ev_html}
</div>
</div>
{_CAL_AJAX_JS}
"""


def page(title: str, content: str, active: str = "", groups: list | None = None,
         extra_head: str = "", tone: str = "", conversations: list | None = None,
         cur_conv: str = "", conv_keyword: str = "",
         cur_group: int | None = None) -> str:
    """整页骨架。

    conversations: 会话列表（传空列表也会渲染 AI 对话区，传 None 则不渲染）。
                   为避免逐个路由改调用，server 侧可用 conv_provider 延迟注入。
    cur_conv:      当前会话 id，用于高亮
    conv_provider: 可调用对象，返回会话列表；未提供时用 conversations
    """
    nav_items = [
        ("overview", "/", "总览"),
        ("messages", "/messages", "群消息"),
        ("kb", "/kb", "知识库"),
        ("calendar", "/calendar", "日历"),
        ("settings", "/settings", "设置"),
    ]
    nav = ""
    for key, url, label in nav_items:
        cls = "active" if key == active else ""
        nav += f'<a class="{cls}" href="{url}">{label}</a>'
    # tone=warn 时加醒目提示条（API 报错等需要用户注意的情况）
    tone_html = ""
    if tone == "warn":
        tone_html = ('<div class="badge amber" style="margin-bottom:12px;'
                     'display:inline-block">发生错误，详见下方提示</div>')
    groups_html = ""
    if groups:
        groups_html = '<div class="sb-sec"><div class="tip sb-h">群列表<span>%d</span></div><div class="sb-list">' % len(groups)
        for g in groups:
            url = f"/messages?group_id={g['group_id']}"
            cls = "active" if (cur_group is not None
                               and str(g.get("group_id")) == str(cur_group)) else ""
            groups_html += (
                f'<a class="sb-item {cls}" href="{url}" title="{esc(g["name"])}">'
                f'{esc(g["name"])}<span class="count">{g["msg_count"] or 0}</span></a>'
            )
        groups_html += '</div></div>'

    # AI 对话区：新建 / 搜索 / 历史会话列表
    conv_html = ""
    if conversations is not None:
        search_val = f'value="{esc(conv_keyword)}"' if conv_keyword else ""
        items = ""
        for c in conversations:
            cls = "active" if c["id"] == cur_conv else ""
            t = (c.get("title") or "新对话").strip() or "新对话"
            if len(t) > 22:
                t = t[:22] + "…"
            # 显示时间：今天只给时分，否则给日期
            upd = c.get("updated_at") or ""
            short = upd[11:16] if upd.startswith(
                datetime.now().strftime("%Y-%m-%d")) else upd[5:10]
            items += (
                f'<a class="sb-item {cls}" href="/chat/{c["id"]}" '
                f'title="{esc(t)}">{esc(t)}'
                f'<span class="count">{esc(short)}</span></a>')
        empty = ('<div class="tip" style="padding:4px 10px 8px">暂无对话</div>'
                 if not items else "")
        conv_html = f"""
<div class="sb-sec">
<div class="tip sb-h">AI 对话<span>{len(conversations)}</span></div>
<div class="sb-list">
<form method="post" action="/chat/new"><button class="pf-btn" type="submit">＋ 新建对话</button></form>
<form method="get" action="/chat" class="sb-search">
<input type="text" name="q" placeholder="搜索对话…" {search_val}>
</form>
{empty}{items}
</div></div>"""
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#141815">
<meta name="color-scheme" content="light">
<link rel="manifest" href="/manifest.webmanifest">
<link rel="apple-touch-icon" href="/pwa/icon-192.png">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="QQMonitor">
<title>{esc(title)} · QQMonitor</title><link rel="icon" href="data:image/svg+xml,%3Csvg xmlns=%27http://www.w3.org/2000/svg%27 viewBox=%270 0 32 32%27%3E%3Crect width=%2732%27 height=%2732%27 rx=%276%27 fill=%27%23141815%27/%3E%3Ctext x=%2716%27 y=%2722%27 font-family=%27Georgia,serif%27 font-size=%2720%27 fill=%27%23D9A05B%27 text-anchor=%27middle%27%3EQ%3C/text%3E%3C/svg%3E"><style>{CSS}{CSS_V3}{MOTION_CSS}{CSS_V5}{CSS_V5B}{CSS_V6}</style></head>
<body><div class="layout">
<header class="topbar"><button class="menu-btn" type="button" aria-label="菜单"
 onclick="document.body.classList.toggle('nav-open')">☰</button>
<div class="logo">QQ<span>Monitor</span></div></header>
<aside class="sidebar">
<div class="logo">QQ<span>Monitor</span></div>
<div class="nav">{nav}</div>
<div class="sb-scroll">{conv_html}{groups_html}</div>
</aside>
<main class="main"><div class="main-inner"><h1 class="page-title">{esc(title)}</h1>{tone_html}{content}</div></main>
</div><script>{MOTION_JS}{MOBILE_JS}</script>{SB_JS}</body></html>"""


# ---------- 总览 ----------
def overview_page(stats: dict, connected: bool,
                  media_names: dict[int, set[str]] | None = None,
                  sync: dict | None = None) -> str:
    cards = [
        ("群数量", stats["groups"]),
        ("消息总数", stats["messages"]),
        ("今日消息", stats["today_messages"]),
        ("昨日消息", stats["yesterday_messages"]),
        ("群成员", stats["members"]),
    ]
    stat_html = "".join(
        f'<div class="stat"><div class="num">{v}</div><div class="label">{k}</div></div>'
        for k, v in cards
    )
    s = sync or {}
    last = str(s.get("last_run") or "")
    last_hm = last[11:16] if len(last) >= 16 else last
    if s.get("daemon") or last:
        cls = "ok" if (s.get("daemon") or s.get("ok")) else "warn"
        label = "本机库同步运行中" if s.get("daemon") else "本机库同步"
        conn = (f'<span class="conn-status {cls}">● {label}</span>'
                + (f'<span class="tip">上次 {last_hm}</span>' if last_hm else "")
                + '<span class="tip">每 3 分钟自动拉取本机 QQ 库</span>')
        note = "本机库同步"
    else:
        conn = ('<span class="conn-status warn">● 同步未运行</span>'
                '<span class="tip">可在「知识库 → 立即同步一次」手动触发</span>')
        note = "同步未运行"
    ranking = "".join(
        f"<tr><td class='num'>{i+1}</td><td>{esc(g['name'])}</td><td class='num'>{g['msg_count']}</td>"
        f"<td>{g['last_ts'] and datetime.fromtimestamp(g['last_ts']).strftime('%m-%d %H:%M') or '-'}</td></tr>"
        for i, g in enumerate(stats["group_ranking"])
    ) or '<tr><td colspan="4" class="empty">暂无数据</td></tr>'
    recent = ""
    gmap = {g["group_id"]: g["name"] for g in stats.get("group_ranking") or []}
    for m in stats["recent"]:
        recent += _msg_item(m, media_names, gmap)
    return f"""
<div class="toolbar"><span class="tip">数据同步</span>{conn}</div>
<div class="stat-grid">{stat_html}</div>
<div class="card"><h3>群消息排行</h3>
<table><tr><th class="num">#</th><th>群</th><th class="num">消息数</th><th>最近活跃</th></tr>{ranking}</table></div>
<div class="card"><h3>最近 3 天消息</h3>
<ul class="msg-list">{recent or f'<li class="empty">暂无消息（当前状态：{esc(note)}）</li>'}</ul></div>"""


# 消息媒体占位符：[图片:xxx.png] / [文件:报告.pdf] / [语音:a.amr] / [视频:b.mp4]
_MSG_MEDIA_RE = re.compile(r"\[(图片|视频|语音|文件):([^\]]+)\]")
_IMG_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")


def _render_msg_content(content: str, group_id: int,
                        media_names: dict[int, set[str]] | None = None) -> str:
    """消息正文转 HTML：把 [图片:xxx] 之类的占位符渲染成内联图片/音视频/文件链接。

    media_names: 群id → 该群已同步的媒体文件名集合；None 表示不校验一律渲染。
    """
    s = esc(content)

    def rep(m: re.Match) -> str:
        kind, name = m.group(1), m.group(2).strip()
        ename = esc(name)
        if media_names is not None and name not in media_names.get(group_id, set()):
            return f'<span class="tip">[{kind}：{ename}（媒体未同步）]</span>'
        url = f"/kb/media/find/{group_id}/{quote(name)}"
        if kind == "图片":
            small = f"/kb/thumb/media/{group_id}/{quote(name)}?w=360"
            return (f'<a href="{url}" target="_blank" rel="noopener">'
                    f'<img class="msg-img" src="{small}" alt="{ename}" '
                    f'loading="lazy" decoding="async"></a>')
        if kind == "视频":
            return f'<video class="msg-video" src="{url}" controls preload="metadata"></video>'
        if kind == "语音":
            return f'<audio class="msg-audio" src="{url}" controls preload="metadata"></audio>'
        return f'<a class="msg-file" href="{url}">[文件] {ename}</a>'

    return _MSG_MEDIA_RE.sub(rep, s)


def _msg_item(m: dict, media_names: dict[int, set[str]] | None = None,
              gmap: dict | None = None) -> str:
    color = _avatar_color(m["user_id"])
    ch = _avatar_char(m["nickname"])
    imp = '<span class="badge imp">⭐重要</span> ' if m["important"] else ""
    link = f'<a href="/msg/{m["id"]}">查看</a>'
    gid = m.get("group_id")
    gname = (gmap or {}).get(gid)
    if not gname:
        gname = "好友" if isinstance(gid, int) and gid < 0 else f"群 {gid}"
    return f"""<li>
<div class="avatar" style="background:{color}">{esc(ch)}</div>
<div class="msg-body">
<div class="msg-head"><span class="msg-name">{esc(m['nickname'])}</span>
<span class="msg-time">{esc(m['time_str'])}</span>{imp}</div>
<div class="msg-content">{_render_msg_content(m['content'], m['group_id'], media_names)}</div>
<div class="msg-head"><span class="tip">{link} · {esc(gname)}</span></div>
</div></li>"""


# ---------- 群消息 ----------
def messages_page(groups: list, group_id: int | None, messages: list,
                  senders: list, filters: dict, total: int, offset: int,
                  limit: int, media_names: dict[int, set[str]] | None = None) -> str:
    opts = ['<option value="">全部群</option>'] + [
        f'<option value="{g["group_id"]}" {"selected" if g["group_id"]==group_id else ""}>'
        f'{esc(g["name"])}（{g["msg_count"]}条）</option>'
        for g in groups
    ]
    sender_opts = ['<option value="">全部发送者</option>'] + [
        f'<option value="{s["user_id"]}" {"selected" if filters.get("user_id")==s["user_id"] else ""}>'
        f'{esc(s["nickname"])}（{s["n"]}条）</option>'
        for s in senders
    ]
    q = filters.get("keyword", "")
    date_from = filters.get("date_from", "")
    date_to = filters.get("date_to", "")
    imp_only = 'checked' if filters.get("important_only") else ''
    gmap_all = {g["group_id"]: g["name"] for g in groups}
    parts = []
    last_day = ""
    for m in messages:
        day = (m.get("time_str") or "")[:10]
        if day and day != last_day:
            last_day = day
            parts.append(f'<li class="day-sep">{esc(day)}</li>')
        parts.append(_msg_item(m, media_names, gmap_all))
    items = "".join(parts) or '<li class="empty">没有符合条件的消息</li>'
    prev = f'<a class="btn ghost sm" href="?group_id={group_id or ""}&offset={max(0,offset-limit)}">上一页</a>' if offset > 0 else ""
    next_p = f'<a class="btn ghost sm" href="?group_id={group_id or ""}&offset={offset+limit}">下一页</a>' if offset + limit < total else ""
    return f"""
<form class="toolbar" method="get">
<select name="group_id">{opts}</select>
<input type="text" name="keyword" value="{esc(q)}" placeholder="搜索关键词…" style="width:180px">
<input type="date" name="date_from" value="{esc(date_from)}">
<span class="tip">至</span>
<input type="date" name="date_to" value="{esc(date_to)}">
<select name="user_id">{sender_opts}</select>
<label class="tip"><input type="checkbox" name="important_only" value="1" {imp_only}> 只看重要</label>
<button class="btn sm" type="submit">筛选</button>
<a class="btn ghost sm" href="/messages">重置</a>
</form>
<div class="tip" style="margin-bottom:10px">共 {total} 条结果，每页 {limit} 条</div>
<ul class="msg-list">{items}</ul>
<div class="pager">{prev}{next_p}</div>"""


# ---------- 消息详情 ----------
def message_detail_page(m: dict, group: dict | None, reason: str,
                        media_names: dict[int, set[str]] | None = None) -> str:
    imp_badge = '<span class="badge imp">⭐ 重要</span>' if m["important"] else ""
    # 标记重要会写库 → 用 POST 表单提交（GET 改状态能绕过 _csrf_middleware 的同源校验）
    toggle_imp = "0" if m["important"] else "1"
    toggle_txt = "取消重要" if m["important"] else "标记重要"
    gname = group["name"] if group else f"群 {m['group_id']}"
    gmap_detail = {m["group_id"]: gname} if gname else {}
    return f"""
<div class="card"><h3>{esc(gname)} 消息详情</h3>
{_msg_item(m, media_names, gmap_detail)}
<p class="tip" style="margin-top:12px">标记原因：{esc(reason)}</p>
<div class="toolbar" style="margin-top:10px">
<form method="post" action="/mark/{m['id']}" style="display:inline">
<input type="hidden" name="important" value="{toggle_imp}">
<button class="btn sm" type="submit">{toggle_txt}</button></form>
<a class="btn ghost sm" href="/messages?group_id={m['group_id']}">返回群消息</a>
</div></div>"""


# ---------- 知识库 ----------
def _fmt_size(n: int) -> str:
    if n >= 1 << 20:
        return f"{n / (1 << 20):.1f} MB"
    if n >= 1 << 10:
        return f"{n / (1 << 10):.1f} KB"
    return f"{n} B"


# 可在线编辑/新建的文本扩展名（与 server.py 的 _EDITABLE_EXTS 保持一致）
_EDIT_EXTS = (".md", ".txt", ".srt", ".vtt", ".ass", ".json", ".csv")


def _post_btn(label: str, action: str, *, confirm_text: str = "",
              cls: str = "btn ghost", title: str = "",
              hidden: dict | None = None, sm: bool = False) -> str:
    """通用 POST 按钮（破坏性/有副作用的操作不再用 GET 链接）。

    confirm_text 走 data-confirm，避免把用户可控内容拼进内联 JS。
    """
    fields = "".join(
        f'<input type="hidden" name="{esc(k)}" value="{esc(v)}">'
        for k, v in (hidden or {}).items())
    attrs = f' data-confirm="{esc(confirm_text)}"' if confirm_text else ""
    onsubmit = ' onsubmit="return confirm(this.dataset.confirm)"' if confirm_text else ""
    title_attr = f' title="{esc(title)}"' if title else ""
    klass = cls + (" sm" if sm else "")
    return (f'<form method="post" action="{esc(action)}" style="display:inline"'
            f'{attrs}{onsubmit}>'
            f'{fields}<button class="{klass}" type="submit"{title_attr}>{esc(label)}</button>'
            f'</form>')


def _del_form(section: str, name: str, scope: str, confirm_text: str) -> str:
    """知识库删除按钮（POST /kb/delete，带 JS 确认）。

    确认文案走 data-confirm 属性（esc 已转义引号），不拼进内联 JS 字符串：
    文件名是用户可控的，拼进 '...' 里可用 " 逃出属性造成 XSS。
    """
    return (f'<form method="post" action="/kb/delete" style="display:inline" '
            f'data-confirm="{esc(confirm_text)}" '
            f'onsubmit="return confirm(this.dataset.confirm)">'
            f'<input type="hidden" name="section" value="{esc(section)}">'
            f'<input type="hidden" name="name" value="{esc(name)}">'
            f'<input type="hidden" name="scope" value="{esc(scope)}">'
            f'<button class="btn danger sm" type="submit">删除</button></form>')


def _edit_link(section: str, name: str) -> str:
    return f'<a class="btn ghost sm" href="/kb/edit/{section}/{quote(name)}">编辑</a>'


def _hl(text: str, q: str) -> str:
    """片段高亮：大小写不敏感地把关键词包上 <mark>。"""
    qe = esc(q)
    return re.sub(re.escape(qe), f"<mark>{qe}</mark>", esc(text), flags=re.IGNORECASE)


def _date_cell(section: str, name: str, meta: dict | None) -> str:
    """事件日期单元格：直接可编辑，失焦即保存。

    日期决定该文件会被纳入哪个周/月/年总结，也影响问答的时间筛选。
    """
    meta = meta or {}
    src = f"{section}/{name}"
    val = meta.get("event_date") or ""
    auto = bool(meta.get("auto"))
    # 自动推断的值用琥珀色提示用户核对；手动设定的用绿色
    cls = "badge" if auto else "badge green"
    title = "由文件名/正文/文件时间自动推断，请核对" if auto else "已手动设定"
    if not val:
        cls, title = "badge red", "未设定日期，不会纳入周期总结"
        shown = "无日期"
    else:
        shown = val
    return (
        f'<span class="{cls}" title="{esc(title)}" style="cursor:pointer" '
        f'data-src="{esc(src)}" data-val="{esc(val)}" '
        f'onclick="editDate(this.dataset.src, this.dataset.val)">{esc(shown)} ✎</span>')


# 事件日期编辑（知识库页）：点徽章 → 弹输入框 → 保存 → 局部刷新
_DATE_JS = r"""
<script>
function editDate(src, cur){
  var v = prompt('设定「' + src + '」的事件日期\n\n'
    + '格式 YYYY-MM-DD，例如 2026-08-30\n'
    + '该日期决定文件被纳入哪个周/月/年总结。\n'
    + '留空并确定 = 清除日期（该文件将不参与周期总结）。', cur || '');
  if(v === null) return;            // 取消
  v = v.trim();
  if(v && !/^\d{4}-\d{2}-\d{2}$/.test(v)){
    alert('日期格式不正确，应为 YYYY-MM-DD，例如 2026-08-30');
    return;
  }
  var data = new FormData();
  data.append('source', src);
  data.append('event_date', v);
  fetch('/kb/meta/date', {method:'POST', body:data})
    .then(function(r){ return r.json(); })
    .then(function(d){
      if(d.ok){ location.reload(); }
      else { alert('保存失败：' + (d.error || '未知错误')); }
    })
    .catch(function(e){ alert('保存失败：' + e.message); });
}
</script>"""


# AI 对话页：Enter 发送、SSE 流式渲染、重命名会话
_CHAT_JS = r"""
<script>
var sending = false;
var currentCtrl = null;

function esc(s){
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

function ask(text){
  var el = document.getElementById('input');
  if(!el) return;
  el.value = text;
  el.focus();
}

function scrollBottom(){
  var box = document.getElementById('msgs');
  if(box) box.scrollTop = box.scrollHeight;
}

function renameConv(){
  var t = prompt('输入会话标题：', '');
  if(t === null) return;
  var cid = location.pathname.split('/')[2] || '';
  if(!cid) return;
  var data = new FormData();
  data.append('title', t);
  fetch('/chat/' + cid + '/rename', {method:'POST', body:data})
    .then(function(){ location.reload(); });
}

function removeEmpty(){
  var e = document.querySelector('.chat-empty');
  if(e) e.remove();
}

// 追加一条消息，返回内容容器
function addBubble(role){
  var box = document.getElementById('msgs');
  var row = document.createElement('div');
  row.className = 'msg ' + (role === 'user' ? 'user' : 'assistant');
  var inner = document.createElement('div');
  if(role === 'user'){ inner.className = 'user-bubble'; }
  else { inner.className = 'markdown msg-text'; }
  inner.textContent = '';
  row.appendChild(inner);
  box.appendChild(row);
  scrollBottom();
  return {row: row, bubble: row, inner: inner};
}

// 工具调用过程容器：插在助手气泡里、正文之上。
// 模型在调用工具前可能先输出一段「思考」文本，这段不算最终答案，
// 遇到 tool_start 时把它折叠成灰色引用块，正文区留给最终答案。
function makeTrace(a){
  var wrap = document.createElement('div');
  a.bubble.insertBefore(wrap, a.inner);
  var box = null, list = null, pending = [], live = '';

  function ensure(){
    if(box) return;
    box = document.createElement('details');
    box.className = 'tool-calls';
    box.open = true;
    var s = document.createElement('summary');
    s.textContent = '工具调用';
    box.appendChild(s);
    list = document.createElement('div');
    box.appendChild(list);
    wrap.insertBefore(box, wrap.firstChild);
  }

  return {
    addDelta: function(t){ live += t; },
    liveText: function(){ return live; },
    // 把已输出的文本收进思考区（本轮不是最终答案）
    flushThink: function(){
      var s = live;
      live = '';
      if(!s.trim()) return;
      var d = document.createElement('div');
      d.className = 'tc-think';
      d.textContent = s.replace(/^\s+|\s+$/g, '');
      wrap.insertBefore(d, wrap.firstChild);
    },
    start: function(d){
      ensure();
      var it = document.createElement('div');
      it.className = 'tc-item';
      it.innerHTML = '<span class="tc-run"></span><b>' + esc(d.label || d.name)
        + '</b>' + (d.args_text ? ' <span class="tc-args">'
        + esc(d.args_text) + '</span>' : '');
      list.appendChild(it);
      pending.push(it);
      scrollBottom();
    },
    done: function(d){
      ensure();
      var it = pending.shift();
      if(!it){
        it = document.createElement('div');
        it.className = 'tc-item';
        list.appendChild(it);
      }
      it.innerHTML = '<b>' + esc(d.label || d.name) + '</b> '
        + (d.ok === false ? '<span class="tc-fail">✗ 失败</span>'
                          : '<span class="tc-ok">✓</span>')
        + ' ' + esc(d.summary || '完成')
        + (d.args_text ? '<div class="tc-args">' + esc(d.args_text) + '</div>' : '');
      scrollBottom();
    }
  };
}

function sendMessage(text){
  var cid = location.pathname.split('/')[2] || '';
  if(!cid) return;
  sending = true;
  var btn = document.getElementById('send-btn');
  var st = document.getElementById('status');
  currentCtrl = new AbortController();
  if(btn){ btn.disabled = false; btn.textContent = '停止'; btn.classList.add('stop'); }
  if(st) st.textContent = '思考中…';

  removeEmpty();
  var u = addBubble('user');
  u.inner.textContent = text;
  var a = addBubble('assistant');
  a.inner.innerHTML = '<span class="typing"></span>';
  var trace = makeTrace(a);

  var data = new FormData();
  data.append('content', text);

  fetch('/chat/' + cid + '/stream', {method:'POST', body:data, signal: currentCtrl.signal})
    .then(function(r){
      if(!r.ok){
        return r.text().then(function(t){
          a.bubble.className = 'bubble error';
          a.inner.textContent = '请求失败（HTTP ' + r.status + '）：' + (t || '').slice(0, 240);
        });
      }
      var reader = r.body.getReader();
      var dec = new TextDecoder('utf-8');
      var buf = '';
      var meta = null;
      function pump(){
        return reader.read().then(function(res){
          if(res.done){
            if(trace.liveText()) a.inner.innerHTML = renderMd(trace.liveText());
            finish();
            return;
          }
          buf += dec.decode(res.value, {stream:true});
          var parts = buf.split('\n\n');
          buf = parts.pop();
          for(var i=0;i<parts.length;i++){
            var ev = null, dstr = null;
            var lines = parts[i].split('\n');
            for(var j=0;j<lines.length;j++){
              if(lines[j].indexOf('event: ') === 0) ev = lines[j].slice(7);
              if(lines[j].indexOf('data: ') === 0) dstr = lines[j].slice(6);
            }
            if(!ev || !dstr) continue;
            var d = {};
            try { d = JSON.parse(dstr); } catch(e){ continue; }
            if(ev === 'meta'){
              meta = d;
              if(st) st.textContent = '思考中…';
            } else if(ev === 'tool_start'){
              trace.flushThink();
              trace.start(d);
              if(st) st.textContent = '调用 ' + (d.label || d.name) + '…';
            } else if(ev === 'tool_done'){
              trace.done(d);
              if(st) st.textContent = (d.label || d.name) + '：' + (d.summary || '完成');
            } else if(ev === 'fallback'){
              if(st) st.textContent = '该模型不支持工具调用，已切回固定检索';
            } else if(ev === 'delta'){
              trace.addDelta(d.text);
              a.inner.innerHTML = renderMd(trace.liveText())
                + '<span class="typing"></span>';
              scrollBottom();
            } else if(ev === 'done'){
              a.inner.innerHTML = renderMd(d.text || trace.liveText());
              addMeta(a.row, d.tools, d.sources);
            } else if(ev === 'error'){
              a.bubble.className = 'bubble error';
              a.inner.textContent = d.message || '发生错误';
            }
          }
          return pump();
        });
      }
      return pump();
    })
    .catch(function(e){
      if(e && e.name === 'AbortError'){
        a.inner.innerHTML = renderMd(trace.liveText() || '已停止生成。');
      } else {
        a.row.className = 'msg assistant';
        a.inner.textContent = '请求失败：' + e.message;
      }
    })
    .then(finish);

  function finish(){
    sending = false;
    currentCtrl = null;
    if(btn){ btn.disabled = false; btn.textContent = '发送'; btn.classList.remove('stop'); }
    if(st) st.textContent = '';
    var ta = document.getElementById('input');
    if(ta) ta.focus();
    scrollBottom();
  }
}

function addMeta(row, tools, sources){
  var foot = document.createElement('div');
  foot.className = 'msg-foot';
  var copy = document.createElement('button');
  copy.type = 'button'; copy.className = 'msg-act'; copy.setAttribute('data-copy', '1');
  copy.textContent = '复制';
  foot.appendChild(copy);
  (tools || []).forEach(function(t){
    var name = '', tip = '';
    if(t && typeof t === 'object'){ name = t.label || t.name || ''; tip = t.args_text || ''; }
    else { name = (t === 'search_knowledge' ? '检索知识库' : String(t || '')); }
    if(!name) return;
    var chip = document.createElement('span');
    chip.className = 'msg-model';
    chip.textContent = name;
    if(tip) chip.title = tip;
    foot.appendChild(chip);
  });
  if(sources && sources.length){
    var det = document.createElement('details');
    det.className = 'msg-sources';
    var sum = document.createElement('summary');
    sum.textContent = '引用 ' + sources.length + ' 条';
    det.appendChild(sum);
    var ul = document.createElement('ul');
    sources.forEach(function(x){
      if(!x || typeof x !== 'object') return;
      var src = typeof x.source === 'string' ? x.source : '';
      var parts = src ? src.split('/') : [];
      var nm = parts.length ? parts[parts.length - 1] : '';
      var kind = x.kind || '';
      var label = x.title || src || x.group_name || ('引用 ' + (x.id || ''));
      if(!label) return;
      var href = '';
      if(typeof x.ref === 'string' && x.ref) href = x.ref;
      else if(x.msg_id) href = '/msg/' + x.msg_id;
      else if(src && (kind === 'chatfile' || parts[0] === 'chat')) href = '/kb/chat/' + encodeURIComponent(nm);
      else if(src && (kind === 'documents' || kind === 'transcripts')) href = '/kb/file/' + kind + '/' + encodeURIComponent(nm);
      else if(src && (x.section === 'documents' || x.section === 'transcripts')) href = '/kb/file/' + x.section + '/' + encodeURIComponent(nm);
      var li = document.createElement('li');
      var el;
      if(href){
        el = document.createElement('a');
        el.href = href; el.target = '_blank';
      } else {
        el = document.createElement('span');   // 媒体等没有可打开的页面，只做说明
      }
      el.textContent = label;
      if(x.quote) el.title = x.quote;
      li.appendChild(el);
      if(x.day){
        var d = document.createElement('span');
        d.className = 'src-day'; d.textContent = x.day;
        li.appendChild(d);
      }
      ul.appendChild(li);
    });
    det.appendChild(ul);
    row.appendChild(det);
  }
  row.appendChild(foot);
}

// 极简 Markdown 渲染（与后端 md_to_html 保持一致的常用子集）
function renderMd(s){
  var t = esc(s || '');
  t = t.replace(/```([\s\S]*?)```/g, function(m, p){
    return '<pre>' + p + '</pre>';
  });
  t = t.replace(/`([^`\n]+)`/g, '<code>$1</code>');
  t = t.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
  t = t.replace(/^### (.*)$/gm, '<h3>$1</h3>');
  t = t.replace(/^## (.*)$/gm, '<h2>$1</h2>');
  t = t.replace(/^# (.*)$/gm, '<h1>$1</h1>');
  t = t.replace(/^&gt; (.*)$/gm, '<blockquote>$1</blockquote>');
  t = t.replace(/^\s*[-*] (.*)$/gm, '<li>$1</li>');
  t = t.replace(/(<li>[\s\S]*?<\/li>)/g, '<ul>$1</ul>');
  t = t.replace(/\[([^\]]+)\]\(([^)]+)\)/g,
    function(m, label, href){
      if(/^\s*(javascript|data|vbscript):/i.test(href)) return label;
      return '<a href="' + href + '" target="_blank" rel="noopener">' + label + '</a>';
    });
  t = t.replace(/\n/g, '<br>');
  return t;
}

document.addEventListener('DOMContentLoaded', function(){
  var ta = document.getElementById('input');
  var form = document.getElementById('send-form');
  if(!ta || !form) return;

  ta.addEventListener('input', function(){
    ta.style.height = 'auto';
    ta.style.height = Math.min(ta.scrollHeight, 150) + 'px';
  });

  ta.addEventListener('keydown', function(e){
    if(e.key === 'Enter' && !e.shiftKey && !e.isComposing){
      e.preventDefault();
      var v = ta.value.trim();
      if(!v || sending) return;
      ta.value = '';
      ta.style.height = 'auto';
      sendMessage(v);
    }
  });

  var btn = document.getElementById('send-btn');
  if(btn) btn.addEventListener('click', function(e){
    if(sending && currentCtrl){ e.preventDefault(); currentCtrl.abort(); }
  });

  // 复制按钮（事件委托）
  document.addEventListener('click', function(e){
    var b = e.target.closest ? e.target.closest('[data-copy]') : null;
    if(!b) return;
    var m = b.closest('.msg');
    var t = m ? m.querySelector('.msg-text') : null;
    if(!t) return;
    var text = t.innerText || t.textContent || '';
    if(navigator.clipboard && navigator.clipboard.writeText){
      navigator.clipboard.writeText(text).then(function(){
        b.textContent = '已复制';
        setTimeout(function(){ b.textContent = '复制'; }, 1200);
      });
    }
  });

  // 走 JS 流式发送；无 JS 时回退到表单 POST
  form.addEventListener('submit', function(e){
    e.preventDefault();
    var v = ta.value.trim();
    if(!v || sending) return;
    ta.value = '';
    ta.style.height = 'auto';
    sendMessage(v);
  });

  scrollBottom();
});
</script>"""


# AI 模型连接测试（设置页）：用当前表单值测试，不需要先保存
_LLM_TEST_JS = r"""
<script>
async function testLLM(kind, btn){
  var box = document.getElementById('test-' + kind);
  var fs = document.querySelector('fieldset[data-kind="' + kind + '"]');
  box.textContent = '测试中…';
  box.style.color = 'var(--muted)';
  btn.disabled = true;
  var data = new FormData();
  data.append('kind', kind);
  fs.querySelectorAll('input,select').forEach(function(el){
    if(el.name) data.append(el.name, el.value);
  });
  try{
    var r = await fetch('/settings/llm/test', {method:'POST', body:data});
    var d = await r.json();
    box.textContent = d.message || ('HTTP ' + r.status);
    box.style.color = d.ok ? 'var(--green)' : 'var(--red)';
  }catch(e){
    box.textContent = '请求失败：' + e.message;
    box.style.color = 'var(--red)';
  }
  btn.disabled = false;
}
</script>"""


# 同步任务进度：轮询 /kb/sync-status，分阶段展示
_SYNC_TASK_JS = r"""
<script>
(function() {
  var card = document.getElementById('sync-task-card');
  var stagesEl = document.getElementById('sync-stages');
  var logsEl = document.getElementById('sync-logs');
  var resultEl = document.getElementById('sync-result');
  var urlParams = new URLSearchParams(location.search);
  var tid = urlParams.get('sync_task');
  if (!tid) return;
  card.style.display = 'block';

  function renderStage(stages, current, done) {
    stagesEl.innerHTML = stages.map(function(s) {
      var isCurrent = s.id === current && !done;
      var isDone = stages.findIndex(function(x) { return x.id === s.id; }) <
                   stages.findIndex(function(x) { return x.id === current; });
      var mark = done ? '✓' : (isDone ? '✓' : (isCurrent ? '⟳' : '○'));
      var color = isDone ? '#2E7D5B' : (isCurrent ? 'var(--primary-ink)' : 'var(--muted)');
      return '<span style="color:' + color + ';margin-right:16px;font-weight:600">'
             + mark + ' ' + s.label + '</span>';
    }).join('');
  }

  function poll() {
    fetch('/kb/sync-status?tid=' + tid)
      .then(function(r) { return r.json(); })
      .then(function(d) {
        renderStage(d.stages, d.stage, d.done);
        logsEl.innerHTML = (d.logs || []).map(function(l) {
          return '<div>' + l + '</div>';
        }).join('');
        logsEl.scrollTop = logsEl.scrollHeight;
        if (d.done) {
          resultEl.innerHTML = d.error
            ? '<span class="badge amber">失败：' + d.error + '</span>'
            : '<span class="badge green">' + (d.detail || '完成') + '</span>';
          return;
        }
        setTimeout(poll, 1200);
      })
      .catch(function(e) {
        setTimeout(poll, 3000);
      });
  }
  poll();
})();
</script>"""


# 拖拽导入：上传 → POST /kb/ingest → 轮询 /kb/ingest/{task_id} → 展示结果
_INGEST_JS = r"""
<script>
(function(){
  var dz=document.getElementById('dropzone');
  var fi=document.getElementById('ingest-files');
  var st=document.getElementById('ingest-status');
  function esc(s){return (s==null?'':String(s)).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}
  function setMsg(html){if(st)st.innerHTML=html;}
  function openPicker(){if(fi)fi.click();}
  if(dz){
    dz.addEventListener('dragover',function(e){e.preventDefault();dz.style.borderColor='var(--primary-ink)';dz.style.background='var(--primary-soft)';});
    dz.addEventListener('dragleave',function(){dz.style.borderColor='var(--border)';dz.style.background='';});
    dz.addEventListener('drop',function(e){
      e.preventDefault();dz.style.borderColor='var(--border)';dz.style.background='';
      if(e.dataTransfer&&e.dataTransfer.files&&e.dataTransfer.files.length){
        fi.files=e.dataTransfer.files;upload();
      }
    });
    dz.addEventListener('click',openPicker);
  }
  if(fi)fi.addEventListener('change',upload);
  function upload(){
    if(!fi||!fi.files||!fi.files.length)return;
    var fd=new FormData();
    for(var i=0;i<fi.files.length;i++)fd.append('files',fi.files[i]);
    setMsg('<div class="tip">上传中…</div>');
    fetch('/kb/ingest',{method:'POST',body:fd}).then(function(r){
      if(!r.ok){return r.json().catch(function(){return {error:'HTTP '+r.status};}).then(function(d){throw new Error(d.error||('HTTP '+r.status));});}
      return r.json();
    }).then(function(data){
      if(data.error)throw new Error(data.error);
      poll(data.task_id);
    }).catch(function(e){setMsg('<span class="badge red">上传失败：'+esc(e.message||e)+'</span>');});
  }
  function poll(tid){
    fetch('/kb/ingest/'+tid).then(function(r){return r.json();}).then(function(d){
      if(d.status==='running'){
        var pct=d.total?Math.round(d.done*100/d.total):0;
        var stage=d.stage?('<div class="tip" style="margin-top:6px;color:var(--primary-ink)">'+esc(d.stage)+'</div>'):'';
        setMsg('<div class="tip">处理中… '+d.done+'/'+d.total+'</div>'
          +stage
          +'<div style="background:var(--border);border-radius:8px;height:8px;margin-top:8px;max-width:420px">'
          +'<div style="background:var(--primary-ink);border-radius:8px;height:8px;width:'+pct+'%"></div></div>');
        setTimeout(function(){poll(tid);},800);
      }else if(d.status==='done'){
        showResult(d.results);
      }else{
        setMsg('<span class="badge red">处理出错：'+esc(d.error||'未知')+'</span>');
      }
    }).catch(function(e){setMsg('<span class="badge red">查询失败：'+esc(e.message||e)+'</span>');});
  }
  function showResult(results){
    var kindMap={'chat':'聊天记录','image':'图片 OCR','document':'文档提取','text':'文字稿','archive':'原始存档'};
    var rows='';
    (results||[]).forEach(function(r){
      var badge=r.ok?'<span class="badge green">成功</span>':'<span class="badge red">失败</span>';
      var det=esc(r.ok?(r.detail||''):(r.error||''));
      rows+='<tr><td>'+esc(r.name)+'</td><td>'+(kindMap[r.kind]||esc(r.kind))+'</td><td>'+badge+'</td><td>'+det+'</td></tr>';
    });
    if(!rows)rows='<tr><td colspan="4" class="empty">没有返回结果</td></tr>';
    setMsg('<div class="card" style="margin-top:12px"><h3>导入结果</h3>'
      +'<table><tr><th>文件</th><th>类型</th><th>状态</th><th>详情</th></tr>'+rows+'</table>'
      +'<a class="btn ghost sm" href="/kb" style="margin-top:10px">刷新知识库页</a></div>');
    if(st)st.scrollIntoView({behavior:'smooth'});
  }
})();
</script>
"""


_KF_CSS = """
<style>
.kf-page{width:100%;max-width:100vw;min-width:0;overflow-x:hidden}
.kf-tabs{display:flex;gap:4px;border-bottom:1px solid var(--border);margin:0 0 16px;
  flex-wrap:wrap}
.kf-tab{padding:9px 14px;font-size:14px;color:var(--muted);border-bottom:2px solid transparent;
  margin-bottom:-1px;text-decoration:none;white-space:nowrap}
.kf-tab.active{color:var(--accent,#0f766e);border-bottom-color:var(--accent,#0f766e);font-weight:600}
.kf-tabn{font-size:11px;color:#5b635e;margin-left:6px;background:transparent;border-radius:8px;
  padding:1px 7px}
.kf-tab.active .kf-tabn{background:transparent;color:#17352b;font-weight:600}
.kf-body{display:flex;flex-direction:column;gap:14px;min-width:0;max-width:100%;overflow-x:hidden}
.kf-tiles{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:0;
  border-top:1px solid var(--line);margin-bottom:6px}
.kf-tile{background:transparent;border:0;border-right:1px solid var(--line);
  border-bottom:1px solid var(--line);border-radius:0;padding:16px 18px}
.kf-tile:last-child{border-right:0}
.kf-tile .k{color:var(--muted);font-size:12px;letter-spacing:.08em}
.kf-tile .v{font-family:var(--serif);font-size:30px;font-weight:600;margin-top:4px;line-height:1.1}
.kf-tile .d{color:var(--muted);font-size:12px;margin-top:6px}
.kf-tile.kf-warn .v{color:#B23A2C}
.kf-quick{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.kf-tools{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:10px}
.kf-chips{display:flex;gap:6px;flex-wrap:wrap}
.kf-chip{display:inline-flex;align-items:center;gap:6px;padding:4px 10px;border-radius:999px;
  border:1px solid var(--border);background:#fff;font-size:13px;color:var(--muted);
  text-decoration:none;white-space:nowrap}
.kf-chip b{font-weight:700;color:var(--text)}
.kf-chip.active{background:#e6f4f1;border-color:#bfe3dd;color:#0b5f57;font-weight:600}
.kf-chip.active b{color:#0b5f57}
.kf-chip.warn{color:#b45309}
.kf-chip.warn.active{background:#fef3c7;border-color:#fcd34d;color:#92400e}
.kf-search{display:flex;gap:6px;align-items:center;margin-left:auto}
.kf-search input[type=search]{border:1px solid var(--border);border-radius:8px;padding:6px 10px;
  font-size:13px;min-width:200px;outline:none}
.kf-search input[type=search]:focus{border-color:#99c7c0;box-shadow:0 0 0 3px rgba(15,118,110,.12)}
.kf-actionbar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:10px}
.kf-tablewrap{overflow-x:auto;max-width:100%}
.kf-table{width:100%;border-collapse:collapse;font-size:13px}
.kf-table.wide{table-layout:fixed}
.kf-table.wide th:nth-child(1){width:42%}
.kf-table.wide th:nth-child(2){width:14%}
.kf-table.wide th:nth-child(3){width:8%}
.kf-table.wide th:nth-child(4){width:10%}
.kf-table.wide th:nth-child(5){width:12%}
.kf-table.wide th:nth-child(6){width:14%}
@media(max-width:720px){
  .kf-table.wide th:nth-child(2),.kf-table.wide td:nth-child(2),
  .kf-table.wide th:nth-child(6),.kf-table.wide td:nth-child(6){display:none}
  .kf-table.wide th:nth-child(1){width:40%}
  .kf-table.wide th:nth-child(3){width:13%}
  .kf-table.wide th:nth-child(4){width:22%}
  .kf-table.wide th:nth-child(5){width:25%}
  .kf-table.wide td:nth-child(4){white-space:nowrap;padding-right:10px}
}
.kf-table.wide .kf-name{max-width:100%}
.kf-src{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.kf-table th{color:var(--muted);font-weight:600;text-align:left;padding:8px 10px;
  border-bottom:1px solid var(--border);font-size:12px;white-space:nowrap}
.kf-table td{padding:9px 10px;border-bottom:1px solid #f1f2f6;vertical-align:middle}
.kf-table td:nth-child(3),.kf-table td:nth-child(4){white-space:nowrap}
.kf-table td:nth-child(5){white-space:nowrap}
.kf-table td:nth-child(6){min-width:170px}
.kf-table.wide td:nth-child(6){min-width:0}
.kf-table .badge{white-space:nowrap}
.kf-table tr:last-child td{border-bottom:0}
.kf-table tr:hover td{background:#fafbfc}
.kf-file{display:flex;align-items:center;gap:8px;min-width:0}
.kf-file .kf-name{min-width:0}
.kf-icon{flex:0 0 30px;height:24px;border-radius:8px;background:#e6f4f1;color:#0b5f57;
  display:grid;place-items:center;font-size:11px;font-weight:700;letter-spacing:.3px}
.kf-name{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:520px;display:block}
.kf-num{font-variant-numeric:tabular-nums;white-space:nowrap;color:var(--muted)}
.kf-pager{display:flex;align-items:center;gap:8px;padding:10px 2px;color:var(--muted);font-size:13px}
.kf-spacer{flex:1}
.kf-link{color:#0f766e;text-decoration:none;margin-right:8px;font-size:13px}
.kf-link:hover{text-decoration:underline}
@media(max-width:900px){.kf-tiles{grid-template-columns:repeat(2,minmax(0,1fr))}
  .kf-name{max-width:220px}.kf-search{margin-left:0;width:100%}
  .kf-search input[type=search]{flex:1;min-width:0}}
@media(max-width:520px){
  /* 窄屏只留「文件 / 状态 / 结果」，否则结果列会被挤出屏幕 */
  .kf-table.wide th:nth-child(3),.kf-table.wide td:nth-child(3){display:none}
  .kf-table.wide th:nth-child(1){width:52%}
  .kf-table.wide th:nth-child(4){width:24%}
  .kf-table.wide th:nth-child(5){width:24%}
  .kf-table.wide th,.kf-table.wide td{padding:7px 6px}
  .kf-table.wide td:nth-child(5){font-size:12px;white-space:nowrap}
  .kf-table.wide .kf-link{margin-right:4px}
}
</style>
"""


def _pager(tab: str, key: str, page: int, pages: int, total: int) -> str:
    if pages <= 1:
        return f'<span class="tip">共 {total} 个</span>'
    prev_p = max(1, page - 1)
    next_p = min(pages, page + 1)
    return (
        f'<span class="tip">共 {total} 个 · 第 {page}/{pages} 页</span>'
        f'<a class="kf-link" href="/kb?tab={tab}&{key}={prev_p}">上一页</a>'
        f'<a class="kf-link" href="/kb?tab={tab}&{key}={next_p}">下一页</a>')


def kb_page(idx: dict, msg: str = "", tone: str = "",
            meta_all: dict | None = None,
            sync_state: dict | None = None,
            group_names: dict | None = None,
            group_files: dict | None = None,
            group_file_rows: list | None = None,
            recordings: list | None = None,
            rec_stats: dict | None = None,
            tab: str = "overview",
            gf_filters: dict | None = None,
            doc_page: int = 1, trs_page: int = 1,
            page_size: int = 24) -> str:
    meta_all = meta_all or {}
    group_names = group_names or {}
    gf_filters = gf_filters or {}
    recordings = recordings or []
    rec_stats = rec_stats or {}
    chat_rows = ""
    for f in idx["chat"]:
        ops = []
        if f.get("gid"):
            ops.append(_post_btn("重生成", f'/kb/sync/{f["gid"]}', sm=True))
            ops.append(_del_form(
                "chat", f["name"], "group",
                f'确定删除群 {f["gid"]} 的全部记录？包括数据库消息、聊天文件与该群媒体，不可恢复！'))
        chat_rows += (f'<tr><td><a href="/kb/chat/{quote(f["name"])}">{esc(f["name"])}</a></td>'
                      f'<td>{f["lines"]} 行</td><td>{_fmt_size(f["size"])}</td>'
                      f'<td>{" ".join(ops)}</td></tr>')
    chat_rows = chat_rows or \
        '<tr><td colspan="4" class="empty">暂无聊天记录（导入后自动生成）</td></tr>'

    media_html = ""
    if idx["media"]:
        for gid, info in idx["media"].items():
            month_links = "".join(
                f'<a class="badge" href="/kb/media/browse/{esc(gid)}/{esc(m)}/">{esc(m)}</a> '
                for m in info["months"])
            gname = group_names.get(int(gid), "") if gid.isdigit() else ""
            gid_label = (f'<b>{esc(gname)}</b> <code class="tip">{esc(gid)}</code>'
                         if gname else f'<b>{esc(gid)}</b>')
            media_html += (
                f'<div style="display:flex;align-items:center;gap:10px;'
                f'margin-bottom:10px;flex-wrap:wrap">{gid_label} '
                f'<span class="tip">（{info["files"]} 个文件 · {_fmt_size(info["size"])}）</span>'
                f'{month_links}{_del_form("media", gid, "group", f"确定删除群 {gid} 的全部媒体文件？")}</div>')
    else:
        media_html = '<div class="empty">暂无媒体文件</div>'

    def _file_row(section: str, name: str, size: int, view_html: str) -> str:
        ops = ""
        if name.lower().endswith(_EDIT_EXTS):
            ops += _edit_link(section, name)
        ops += _del_form(section, name, "file", f"确定删除 {name} ？")
        date_html = _date_cell(section, name, meta_all.get(f"{section}/{name}"))
        return (f'<div style="display:flex;align-items:center;gap:10px;margin:6px 0;'
                f'flex-wrap:wrap">{view_html}'
                f'<span class="tip">{_fmt_size(size)}</span>'
                f'{date_html}'
                f'<span style="margin-left:auto;display:flex;gap:6px">{ops}</span></div>')

    doc_total = len(idx["documents"])
    doc_pages = max(1, (doc_total + page_size - 1) // page_size)
    doc_page = min(doc_page, doc_pages)
    doc_rows = []
    doc_tiles = []
    for d in idx["documents"][(doc_page - 1) * page_size: doc_page * page_size]:
        name = d["name"]
        if name.lower().endswith(_IMG_EXTS):
            url = f"/kb/file/documents/{quote(name)}"
            small = f"/kb/thumb/documents/{quote(name)}?w=320&v={int(d.get('mtime') or 0)}"
            date_html = _date_cell("documents", name, meta_all.get(f"documents/{name}"))
            doc_tiles.append(
                f'<div class="doc-tile"><a href="{url}" title="{esc(name)}">'
                f'<img src="{small}" loading="lazy" decoding="async" alt="{esc(name)}"></a>'
                f'<div class="doc-name"><a href="{url}">{esc(name)}</a></div>'
                f'<div class="doc-meta">{_fmt_size(d["size"])}{date_html}'
                f'{_del_form("documents", name, "file", f"确定删除 {name} ？")}</div></div>')
        else:
            view = (f'<a class="badge green" '
                    f'href="/kb/file/documents/{quote(name)}">{esc(name)}</a>')
            doc_rows.append(_file_row("documents", name, d["size"], view))
    docs = ""
    if doc_tiles:
        docs += f'<div class="doc-grid">{"".join(doc_tiles)}</div>'
    docs += ("".join(doc_rows) if doc_rows else "")
    docs = docs or '<span class="tip">（空）</span>'
    doc_pager = _pager("documents", "dpage", doc_page, doc_pages, doc_total)

    trs_total = len(idx["transcripts"])
    trs_pages = max(1, (trs_total + page_size - 1) // page_size)
    trs_page = min(trs_page, trs_pages)
    trs_rows = []
    for t in idx["transcripts"][(trs_page - 1) * page_size: trs_page * page_size]:
        name = t["name"]
        view = (f'<a class="badge" '
                f'href="/kb/file/transcripts/{quote(name)}">{esc(name)}</a>')
        trs_rows.append(_file_row("transcripts", name, t["size"], view))
    trs = "".join(trs_rows) or '<span class="tip">（空）</span>'
    trs_pager = _pager("documents", "tpage", trs_page, trs_pages, trs_total)

    s = idx["stats"]
    stats_badges = (
        f'<span class="badge">聊天 {s["chat"]} 个 · {_fmt_size(s["chat_size"])}</span> '
        f'<span class="badge green">文档 {s["documents"]} 个 · {_fmt_size(s["documents_size"])}</span> '
        f'<span class="badge">文字稿 {s["transcripts"]} 个 · {_fmt_size(s["transcripts_size"])}</span> '
        f'<span class="badge imp">媒体 {s["media"]} 个 · {_fmt_size(s["media_size"])}</span> '
        f'<span class="badge red">共 {_fmt_size(s["total_size"])}</span>')

    # ---- 同步状态卡片（守护进程 / WebUI 定时器接管 / 未运行） ----
    st = sync_state or {}
    interval_txt = f'每 {st.get("interval_min", "?")} 分钟同步一次'
    if st.get("daemon"):
        sync_badge = '<span class="badge green">守护进程运行中</span>'
        sync_desc = interval_txt
    elif st.get("webui_timer"):
        sync_badge = '<span class="badge green">WebUI 定时器运行中</span>'
        sync_desc = f'{interval_txt}（守护进程未启动，本页面服务接管）'
    else:
        sync_badge = '<span class="badge amber">自动同步未运行</span>'
        sync_desc = '双击 sync-daemon.bat 启动守护进程，或到设置页开启定时自动同步'
    last = st.get("last_run") or "—"
    if st.get("ok") is False:
        last_detail = f'失败：{st.get("error", "")}'
    else:
        last_detail = st.get("detail") or ""
    last_html = (f'<span class="tip">上次同步 {esc(last)}'
                 + (f' · {esc(last_detail)}' if last_detail else "") + '</span>')
    if st.get("daemon"):
        sync_btn = _post_btn(
            "立即同步一次", "/kb/sync-qq",
            title="守护进程正在负责自动同步，点击会提示让位")
    else:
        sync_btn = _post_btn(
            "立即同步一次", "/kb/sync-qq", cls="btn",
            title="一键完成：解密提取 → 入库 → 复制媒体 → 识别图片文字 → "
                  "渲染聊天 Markdown。后台执行，页面实时显示进度")
    sync_html = f"""
<div class="card"><h3>同步状态</h3>
<div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:8px">
{sync_badge}<span class="tip">{esc(sync_desc)}</span>{sync_btn}</div>
<div style="margin-bottom:10px">{last_html}</div>
<div>{stats_badges}</div></div>
"""

    maintain_html = f"""
<div class="card"><details>
<summary style="cursor:pointer;font-weight:600">维护工具（日常不需要）</summary>
<p class="tip" style="margin:10px 0">以下操作耗时较长或有副作用，仅在修复问题时使用：
重新同步 = 重建聊天 Markdown（含图片 OCR）+ 补同步媒体 + inbox 分拣；
「重建图片 OCR」会清空缓存从头识别，用于补漏。</p>
<div style="display:flex;gap:8px;flex-wrap:wrap">
{_post_btn("重新同步", "/kb/sync")}
{_post_btn("修复旧图片", "/kb/reparse",
           title="早期入库的图片缺少文件名（显示成裸 [图片]），用当前解析器重跑补全")}
{_post_btn("去重", "/kb/dedup",
           title="清理重复消息（解析格式升级后重导可能产生）")}
{_post_btn("重建图片 OCR", "/kb/sync", confirm_text="将清空全部图片 OCR 缓存并重新识别所有图片文字。图片较多时可能要跑几分钟到十几分钟，期间页面无响应属正常。继续？",
           title="清空 OCR 缓存后重跑，补回可能漏掉的图片文字",
           hidden={"ocr": "rebuild"})}
</div></details></div>
"""

    # ---- 群文件自动入库（tab=group-files） ----
    gf = (group_files or {}) if group_files is not None else {}
    _STATUS_LABEL = {
        "pending": ("待处理", ""), "missing": ("未下载", "amber"),
        "ingested": ("已入库", "green"), "archived": ("仅归档", ""),
        "duplicate": ("重复", ""), "skipped": ("跳过", ""),
        "error": ("失败", "red"),
    }
    gf_status = str(gf_filters.get("status") or "")
    gf_q = str(gf_filters.get("q") or "")
    gf_page = int(gf_filters.get("page") or 1)
    gf_pages = int(gf_filters.get("pages") or 1)
    gf_total = int(gf_filters.get("total") or 0)

    def _u(params: dict) -> str:
        return "?" + "&".join(f"{k}={quote(str(v))}"
                              for k, v in params.items() if v not in (None, ""))

    def _gf_url(**kw) -> str:
        p = {"tab": "group-files"}
        st = kw.get("status", gf_status)
        qq = kw.get("q", gf_q)
        pg = int(kw.get("page", 1) or 1)
        if st:
            p["gf_status"] = st
        if qq:
            p["gf_q"] = qq
        if pg > 1:
            p["gf_page"] = str(pg)
        return "/kb" + _u(p)

    def _gf_row(r: dict) -> str:
        status = r.get("status") or "pending"
        label, cls = _STATUS_LABEL.get(status, (status, ""))
        gid = r.get("group_id")
        gname = group_names.get(gid, "") if isinstance(gid, int) else ""
        target = ""
        if r.get("md_path"):
            target += f'<a class="kf-link" href="/kb/file/{quote(r["md_path"])}">文字稿</a>'
        if r.get("doc_path"):
            target += f'<a class="kf-link" href="/kb/file/{quote(r["doc_path"])}">原件</a>'
        detail = esc(r.get("detail") or "")
        icon = (r.get("name") or "").rsplit(".", 1)[-1].upper()[:4] if "." in (r.get("name") or "") else "FILE"
        return (
            f'<tr><td><div class="kf-file"><span class="kf-icon">{esc(icon)}</span>'
            f'<span class="kf-name">{esc(r.get("name") or "")}</span></div></td>'
            f'<td class="tip kf-src">{esc(gname or str(gid or ""))}</td>'
            f'<td class="kf-num">{_fmt_size(r.get("size") or 0)}</td>'
            f'<td><span class="badge {cls}">{label}</span></td>'
            f'<td>{target}</td>'
            f'<td class="tip">{detail}</td></tr>')

    rows_html = "".join(_gf_row(r) for r in (group_file_rows or []))
    rows_html = rows_html or '<tr><td colspan="6" class="empty">没有符合条件的文件</td></tr>'
    chips = ""
    for _st, _label, _n, _warn in [
        ("", "全部", gf.get("total", 0), False),
        ("ingested", "已入库", gf.get("ingested", 0), False),
        ("missing", "未下载", gf.get("missing", 0), True),
        ("pending", "待处理", gf.get("pending", 0), True),
        ("duplicate", "重复", gf.get("duplicate", 0), False),
        ("skipped", "跳过", gf.get("skipped", 0), False),
        ("archived", "仅归档", gf.get("archived", 0), False),
        ("error", "失败", gf.get("error", 0), False),
    ]:
        _active = " active" if _st == gf_status else ""
        _w = " warn" if _warn and _n else ""
        chips += (f'<a class="kf-chip{_w}{_active}" '
                  f'href="{_gf_url(status=_st)}">{_label} <b>{_n}</b></a>')
    prev_link = (f'<a class="btn ghost sm" href="{_gf_url(page=gf_page - 1)}">上一页</a>'
                 if gf_page > 1 else '<span class="btn ghost sm" aria-disabled="true">上一页</span>')
    next_link = (f'<a class="btn ghost sm" href="{_gf_url(page=gf_page + 1)}">下一页</a>'
                 if gf_page < gf_pages else '<span class="btn ghost sm" aria-disabled="true">下一页</span>')
    gf_card = f"""
<div class="card"><h3>群文件自动入库</h3>
<p class="tip" style="margin-bottom:10px">只处理<b>本机已有副本</b>的文件（你在 QQ 里点过下载）：
复制进 <code>documents/</code>，PDF / Word / PPT 自动提取文字、纯文本直接读，清洗后存入
<code>transcripts/</code> 并被 AI 检索；扫描版 PDF 用本地 OCR，旧版 <code>.doc</code> 借用本机 Word。
<b>不联网下载</b>，没下载过的显示「未下载」，在 QQ 下载后再扫一次即可。</p>
<div class="kf-tools">
  <div class="kf-chips">{chips}</div>
  <form method="get" action="/kb" class="kf-search">
    <input type="hidden" name="tab" value="group-files">
    <input type="hidden" name="gf_status" value="{esc(gf_status)}">
    <input type="search" name="gf_q" value="{esc(gf_q)}" placeholder="搜索文件名…">
    <button class="btn sm" type="submit">搜索</button>
  </form>
</div>
<div class="kf-actionbar">
  {_post_btn("扫描群文件并入库", "/kb/group-files/scan", cls="btn",
             title="把本机已有的群文件复制进知识库并提取文字，随后更新向量索引")}
  {sync_btn}
  <span class="tip">每轮最多 10 个，刚下载的优先处理</span>
</div>
<div class="kf-tablewrap"><table class="kf-table wide">
<thead><tr><th>文件</th><th>来源</th><th>大小</th><th>状态</th><th>结果</th><th>说明</th></tr></thead>
<tbody>{rows_html}</tbody></table></div>
<div class="kf-pager"><span>第 {gf_page} / {gf_pages} 页 · 共 {gf_total} 条</span>
<span class="kf-spacer"></span>{prev_link}{next_link}</div>
</div>"""

    def _gf_recent(n: int = 5) -> str:
        if not group_file_rows:
            return '<tr><td colspan="4" class="empty">还没有发现群文件消息</td></tr>'
        out = ""
        for r in (group_file_rows or [])[:n]:
            status = r.get("status") or "pending"
            label, cls = _STATUS_LABEL.get(status, (status, ""))
            target = ""
            if r.get("md_path"):
                target = f'<a class="kf-link" href="/kb/file/{quote(r["md_path"])}">文字稿</a>'
            elif r.get("doc_path"):
                target = f'<a class="kf-link" href="/kb/file/{quote(r["doc_path"])}">原件</a>'
            out += (f'<tr><td><span class="kf-name">{esc(r.get("name") or "")}</span></td>'
                    f'<td><span class="badge {cls}">{label}</span></td>'
                    f'<td class="kf-num">{_fmt_size(r.get("size") or 0)}</td>'
                    f'<td>{target}</td></tr>')
        return out

    st_ = idx["stats"]
    text_files = len(idx["chat"]) + len(idx["documents"]) + len(idx["transcripts"])
    _rec_badge = {"pending": ("amber", "待处理"), "transcribing": ("amber", "转写中"),
                  "transcribed": ("green", "已转写"), "archived": ("", "仅归档"),
                  "error": ("amber", "失败"), "duplicate": ("", "重复")}
    rec_total = rec_stats.get("total", len(recordings))
    rec_done = rec_stats.get("transcribed", 0)
    rec_archived = rec_stats.get("archived", 0)
    _by_course: dict[str, list[dict]] = {}
    for _r in recordings:
        _by_course.setdefault(_r.get("course") or "未分类", []).append(_r)
    rec_rows = ""
    for _course in sorted(_by_course,
                          key=lambda c: max((x.get("rec_date") or "") for x in _by_course[c]),
                          reverse=True):
        for _r in sorted(_by_course[_course], key=lambda x: x.get("rec_date") or "", reverse=True):
            _cls, _label = _rec_badge.get(_r.get("status") or "", ("", _r.get("status") or ""))
            _md = (_r.get("md_path") or "").strip()
            if _md:
                _md_cell = f'<a href="/kb/file/transcripts/{quote(Path(_md).name)}">文字稿</a>'
            else:
                _md_cell = '<span class="tip">—</span>'
            _sum = (_r.get("summary_path") or "").strip()
            _sum_cell = (f'<a href="/kb/file/transcripts/{quote(Path(_sum).name)}">总结</a>'
                         if _sum else '<span class="tip">—</span>')
            _sched = (_r.get("schedule_path") or "").strip()
            _sched_cell = (f'<a href="/kb/file/transcripts/{quote(Path(_sched).name)}">日程</a>'
                           if _sched else '<span class="tip">—</span>')
            _course_cell = esc(_course)
            if _course == "未识别":
                _course_cell = '<span class="badge amber">未识别</span>'
            rec_rows += (f'<tr><td>{_course_cell}</td>'
                         f'<td class="kf-name" title="{esc(_r.get("detail") or _r.get("filename") or "")}">{esc(_r.get("filename") or "")}</td>'
                         f'<td>{esc(_r.get("rec_date") or "")}</td>'
                         f'<td>{_fmt_size(int(_r.get("size") or 0))}</td>'
                         f'<td><span class="badge {_cls}">{esc(_label)}</span></td>'
                         f'<td>{_md_cell}</td><td>{_sum_cell}</td><td>{_sched_cell}</td></tr>')
    rec_rows = rec_rows or '<tr><td colspan="8" class="empty">还没有上传过录音</td></tr>'

    kf_tiles = f"""
<div class="kf-tiles">
  <div class="kf-tile"><div class="k">知识库文本文件</div><div class="v">{text_files}</div>
    <div class="d">{_fmt_size(st_["total_size"])} · 含聊天/文档/文字稿</div></div>
  <div class="kf-tile"><div class="k">聊天记录</div><div class="v">{len(idx["chat"])}</div>
    <div class="d">{_fmt_size(st_["chat_size"])}</div></div>
  <div class="kf-tile"><div class="k">文档 / 文字稿</div><div class="v">{len(idx["documents"])} / {len(idx["transcripts"])}</div>
    <div class="d">{_fmt_size(st_["documents_size"] + st_["transcripts_size"])}</div></div>
  <div class="kf-tile kf-warn"><div class="k">群文件待下载</div><div class="v">{gf.get("missing", 0)}</div>
    <div class="d">已入库 {gf.get("ingested", 0)} · 重复 {gf.get("duplicate", 0)}</div></div>
  <div class="kf-tile"><div class="k">录音</div><div class="v">{rec_total}</div>
    <div class="d">已转写 {rec_done} · 仅归档 {rec_archived} · 转写中 {rec_stats.get("transcribing", 0)}</div></div>
</div>"""

    tab_counts = {
        "group-files": gf.get("total", 0),
        "documents": len(idx["documents"]) + len(idx["transcripts"]),
        "media": st_["media"],
        "chat": len(idx["chat"]),
        "recordings": rec_total,
    }
    tabs_def = [("overview", "概览", None), ("group-files", "群文件", tab_counts["group-files"]),
                ("recordings", "录音", tab_counts["recordings"]),
                ("documents", "文档与文字稿", tab_counts["documents"]),
                ("chat", "聊天记录", tab_counts["chat"]),
                ("media", "媒体", tab_counts["media"]),
                ("search", "搜索", None)]
    tabs_html = '<nav class="kf-tabs">'
    for _key, _label, _n in tabs_def:
        _cls = " active" if _key == tab else ""
        _cnt = f'<span class="kf-tabn">{_n}</span>' if _n is not None else ""
        tabs_html += f'<a class="kf-tab{_cls}" href="/kb?tab={_key}">{_label}{_cnt}</a>'
    tabs_html += "</nav>"

    upload_card = f"""
<div class="card"><h3>导入文件 · 自动分拣清洗</h3>
<div id="dropzone" style="border:1px dashed var(--line);border-radius:8px;padding:26px;text-align:center;cursor:pointer;color:var(--muted);transition:border-color .15s ease-out, background-color .15s ease-out">
把文件拖到这里，或点击选择文件（可多选）</div>
<input type="file" id="ingest-files" multiple hidden>
<div id="ingest-status" style="margin-top:8px"></div>
<p class="tip" style="margin-top:10px"><b>图片</b> → 图片理解识别文字；<b>PDF / Word / PPT</b> → 提取清洗成 AI 可读 Markdown；<b>会议录屏 / 录音</b>（MP4 / MKV / M4A / MP3 …）→ 语音转写（较慢，上传后可关闭页面，后台继续处理）。聊天记录由自动同步负责，无需拖文件。</p>
<form method="post" action="/kb/from-url" style="margin-top:14px;display:flex;gap:8px;align-items:center;flex-wrap:wrap;border-top:1px solid var(--border);padding-top:14px">
<input type="text" name="url" placeholder="粘贴网页链接（支持微信公众号文章）" style="flex:1;min-width:260px">
<button class="btn" type="submit">抓取为 Markdown</button>
</form>
<p class="tip" style="margin-top:6px">抓取后正文（含文字、表格、图片）会转成 AI 可读 Markdown 存进 <code>transcripts/</code>，图片保存在 <code>media/web_&lt;标题&gt;/</code>。</p>
</div>"""

    search_card = f"""
<div class="card"><h3>全文搜索</h3>
<p class="tip" style="margin-bottom:10px">覆盖聊天 Markdown / 文档 / 文字稿内容与文件名；新建笔记会保存到对应分区。</p>
<form method="get" action="/kb/search" style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
<input type="text" name="q" placeholder="搜索聊天记录 / 文档 / 文字稿内容…" style="flex:1;min-width:200px">
<button class="btn" type="submit">搜索</button>
<a class="btn ghost" href="/kb/new">＋ 新建笔记</a>
</form></div>"""

    def _quick_actions() -> str:
        return f"""<div class="kf-quick">{sync_btn}
{_post_btn("扫描群文件", "/kb/group-files/scan", cls="btn ghost sm", title="扫描并入库本机已下载的群文件")}
<a class="btn ghost sm" href="/kb?tab=search">搜索知识库</a>
<a class="btn ghost sm" href="/kb?tab=documents">文档与文字稿</a></div>"""

    sec_overview = f"""
{sync_html}
{kf_tiles}
<div class="card"><h3>最近群文件</h3>
<div class="kf-tablewrap"><table class="kf-table">
<thead><tr><th>文件</th><th>状态</th><th>大小</th><th>结果</th></tr></thead>
<tbody>{_gf_recent(5)}</tbody></table></div>
<div class="kf-pager"><span class="tip">共 {gf.get("total", 0)} 个文件，已入库 {gf.get("ingested", 0)}</span>
<span class="kf-spacer"></span><a class="kf-link" href="/kb?tab=group-files">查看全部 →</a></div></div>
{upload_card}
{maintain_html}
<p class="tip">知识库根目录：<code>{esc(idx["root"])}</code></p>"""

    sec_group_files = gf_card
    sec_recordings = f"""
<div class="card"><h3>录音上传（手机 App / 会议记录）</h3>
<p class="tip" style="margin-bottom:10px">手机 App 上传的录音先落到 <code>data/inbox</code>，后台用 FireRedASR2-AED 转写（带说话人与高/中/低置信标记），随后自动生成总结与日程（<code>transcripts/*_总结.md</code>、<code>*_日程.json</code>）。文件名匹配课程与日期的按课程分组；匹配不上的记为 <span class="badge amber">未识别</span>，同样会转写。<b>电脑端原件会在转写完成后先归档到知识库 <code>documents/</code></b>（归档成功才把 <code>data/inbox</code> 里那份移走，归档失败则保留原件以便重试）；手机端上传的原件始终保留。</p>
<div class="kf-tablewrap"><table class="kf-table"><thead><tr><th>分类</th><th>录音</th><th>日期</th><th>大小</th><th>状态</th><th>文字稿</th><th>总结</th><th>日程</th></tr></thead>
<tbody>{rec_rows}</tbody></table></div>
<p class="tip" style="margin-top:8px">状态：待处理 / 转写中 / 已转写 / 仅归档 / 失败。<b>转写中的文件不要重启 WebUI</b>（转写在 WebUI 进程的后台线程里）。</p></div>"""
    sec_documents = _quick_actions() + f"""
<div class="card"><h3>文档（documents/）</h3>
<p class="tip" style="margin-bottom:10px">日期徽章是<b>事件日期</b>（内容发生在什么时候），决定该文件被纳入哪个周 / 月 / 年总结。点击徽章可修改。</p>
{docs}<div class="kf-pager" style="margin-top:10px">{doc_pager}</div></div>
<div class="card"><h3>会议文字稿（transcripts/）</h3>
<p class="tip" style="margin-bottom:10px">会议的发生时间请点日期徽章确认——自动推断不出来的会标红，这类文件不会进入周期总结。</p>
{trs}<div class="kf-pager" style="margin-top:10px">{trs_pager}</div>
<p class="tip" style="margin-top:8px">点开 <code>.md</code> 可直接在浏览器查看渲染后的内容，也可「编辑」修正 OCR 错字。</p></div>"""
    sec_chat = _quick_actions() + f"""
<div class="card"><h3>聊天记录原文（AI 可读）</h3>
<p class="tip" style="margin-bottom:10px">Markdown 按天分节；消息里的 [图片]/[文件] 已链接到原始文件。删除会同时清除数据库消息与该群媒体。</p>
<div class="kf-tablewrap"><table class="kf-table"><thead><tr><th>文件</th><th>行数</th><th>大小</th><th>操作</th></tr></thead>
<tbody>{chat_rows}</tbody></table></div></div>"""
    sec_media = _quick_actions() + f"""
<div class="card"><h3>原始媒体（media/）</h3>
<p class="tip" style="margin-bottom:10px">点月份徽章可浏览该月文件；删除按钮清除整个群的媒体。</p>{media_html}</div>"""
    sec_search = search_card + f"""
<div class="card"><h3>搜索技巧</h3>
<p class="tip" style="margin-bottom:0">搜索按关键词跨文件匹配，结果按命中次数排序。
若想按群、日期、发言人筛选原始消息，请到左侧「群消息」页；知识库搜索只覆盖可读文本
（已提取的文档、转写稿、聊天记录），不支持图片/音视频内容本身。</p></div>"""

    bodies = {"overview": sec_overview, "group-files": sec_group_files,
              "recordings": sec_recordings,
              "documents": sec_documents, "chat": sec_chat,
              "media": sec_media, "search": sec_search}
    body = bodies.get(tab, sec_overview)

    tone_cls = "ok" if tone == "ok" else ("warn" if tone == "warn" else "")
    msg_html = ""
    if msg:
        _cls = "green" if tone_cls == "ok" else ("amber" if tone_cls == "warn" else "")
        msg_html = (f'<div class="badge {_cls}" style="margin-bottom:12px;'
                    f'display:inline-block">{esc(msg)}</div>')
    return f"""
{_KF_CSS}
<div class="kf-page">
{msg_html}
{tabs_html}
<div class="kf-body">{body}</div>
</div>
<div class="card" id="sync-task-card" style="display:none"><h3>同步进度</h3>
<div id="sync-stages" style="margin-bottom:10px"></div>
<div id="sync-logs" style="font-size:12px;line-height:1.8;color:var(--muted);max-height:200px;overflow-y:auto;border-top:1px dashed var(--border);padding-top:8px"></div>
<div id="sync-result" style="margin-top:10px;font-weight:600"></div>
</div>
{_SYNC_TASK_JS}
{_INGEST_JS}
{_DATE_JS}"""


def kb_chat_page(name: str, md_html: str, path_str: str) -> str:
    return f"""
<div class="toolbar">
<a class="btn ghost sm" href="/kb">返回知识库</a>
<span class="tip">{esc(name)}</span>
</div>
<div class="card"><div class="markdown">{md_html or '<div class="empty">（空）</div>'}</div>
<p class="tip" style="margin-top:12px">保存路径：<code>{esc(path_str)}</code></p></div>"""


def kb_search_page(q: str, results: list[dict]) -> str:
    """知识库全文搜索页。results 来自 server 的 _kb_search。"""
    rows = ""
    for r in results:
        section = r["section"]
        name = r["name"]
        if section == "chat":
            link = f'/kb/chat/{quote(name)}'
        else:
            link = f'/kb/file/{section}/{quote(name)}'
        snips = ""
        for s in r["snippets"]:
            snips += (f'<div class="tip" style="margin:2px 0"><span style="color:var(--muted)">'
                      f'第 {s["line"]} 行</span>：<code>{_hl(s["text"], q)}</code></div>')
        if not snips:
            snips = '<div class="tip">仅文件名匹配</div>'
        rows += (f'<div style="margin-bottom:14px"><div style="display:flex;align-items:center;'
                 f'gap:8px;flex-wrap:wrap">'
                 f'<a class="badge green" href="{link}">{esc(name)}</a>'
                 f'<span class="badge">{section}</span>'
                 f'<span class="tip">{_fmt_size(r["size"])}</span>'
                 f'<span class="tip">命中 {len(r["snippets"])} 段</span></div>{snips}</div>')
    body = (f'<div class="tip" style="margin-bottom:10px">'
            f'共找到 <b>{len(results)}</b> 个文件包含「{esc(q)}」</div>{rows}'
            if q and results else
            ('<div class="empty">没有找到包含「' + esc(q) + '」的内容</div>'
             if q else '<div class="empty">请输入关键词</div>'))
    return f"""
<div class="toolbar">
<a class="btn ghost sm" href="/kb">返回知识库</a>
<span class="tip">搜索：{esc(q) or "（空）"}</span>
</div>
<form method="get" action="/kb/search" style="display:flex;gap:8px;align-items:center;margin-bottom:12px">
<input type="text" name="q" value="{esc(q)}" placeholder="搜索聊天记录 / 文档 / 文字稿内容…" style="flex:1;min-width:200px">
<button class="btn" type="submit">搜索</button>
</form>
<div class="card">{body}</div>"""


def kb_media_page(gid: str, month: str, files: list[dict]) -> str:
    """媒体月份浏览页：列出该月文件，支持单文件/整月删除。"""
    rows = ""
    for f in files:
        fname = f["name"]
        rel = f"{gid}/{month}/{fname}"
        del_btn = _del_form("media", rel, "file", f"确定删除 {fname} ？")
        rows += (f'<div style="display:flex;align-items:center;gap:10px;margin:6px 0">'
                 f'<a class="badge" href="/kb/media/{esc(gid)}/{esc(month)}/{quote(fname)}">'
                 f'{esc(fname)}</a>'
                 f'<span class="tip">{_fmt_size(f["size"])}</span>'
                 f'<span style="margin-left:auto">{del_btn}</span>'
                 f'</div>')
    rows = rows or '<div class="empty">该月没有文件</div>'
    return f"""
<div class="toolbar">
<a class="btn ghost sm" href="/kb">返回知识库</a>
<span class="tip">媒体 {esc(gid)} / {esc(month)}</span>
{_del_form("media", f"{gid}/{month}", "month", f"确定删除群 {gid} 在 {month} 的全部媒体？")}
</div>
<div class="card"><h3>文件（{len(files)} 个）</h3>{rows}</div>"""


def kb_edit_page(section: str, name: str, content: str) -> str:
    """在线编辑 documents/transcripts 下的文本文件。"""
    return f"""
<div class="toolbar">
<a class="btn ghost sm" href="/kb">返回知识库</a>
<a class="btn ghost sm" href="/kb/file/{section}/{quote(name)}">查看</a>
<span class="tip">编辑 {esc(section)}/{esc(name)}</span>
</div>
<form method="post" action="/kb/save">
<input type="hidden" name="section" value="{esc(section)}">
<input type="hidden" name="name" value="{esc(name)}">
<textarea name="content" rows="18" style="width:100%;font-family:ui-monospace,Consolas,monospace;font-size:13px"
placeholder="在此编辑内容…">{esc(content)}</textarea>
<div style="margin-top:10px;display:flex;gap:8px">
<button class="btn" type="submit">保存</button>
<span class="tip" style="align-self:center">保存后覆盖同名文件，操作不可撤销</span>
</div>
</form>"""


def kb_new_page(error: str = "", section: str = "",
                name: str = "", content: str = "") -> str:
    """新建笔记（documents/ 或 transcripts/ 下的文本文件）。"""
    sel = {section: " selected" if section else ""}
    err = (f'<p class="tip" style="color:var(--red)">{esc(error)}</p>') if error else ""
    return f"""
<div class="toolbar">
<a class="btn ghost sm" href="/kb">返回知识库</a>
<span class="tip">新建笔记</span>
</div>
{err}
<form method="post" action="/kb/save">
<div style="display:flex;gap:10px;align-items:center;margin-bottom:10px;flex-wrap:wrap">
<select name="section">
<option value="transcripts"{sel.get("transcripts", "")}>transcripts/（会议文字稿）</option>
<option value="documents"{sel.get("documents", "")}>documents/（文档）</option>
</select>
<input type="text" name="name" value="{esc(name)}" placeholder="文件名，如 读书笔记.md" style="flex:1;min-width:200px">
</div>
<textarea name="content" rows="18" style="width:100%;font-family:ui-monospace,Consolas,monospace;font-size:13px"
placeholder="在此输入内容…">{esc(content)}</textarea>
<div style="margin-top:10px;display:flex;gap:8px">
<button class="btn" type="submit">保存</button>
<span class="tip" style="align-self:center">支持 .md / .txt / .srt / .vtt / .ass / .json / .csv</span>
</div>
</form>"""


# AI 模型字段定义：(字段名, 标签, 输入类型, 占位符, 说明)
_LLM_FIELDS = {
    "chat": [
        ("base_url", "接口地址", "text", "https://api.deepseek.com", ""),
        ("api_key", "API Key", "password", "sk-...",
         "密钥仅存本地 data/llm.yaml，已排除出 git"),
        ("model", "模型名", "text", "deepseek-chat", ""),
        ("temperature", "温度", "number", "0.3", "越小越严谨，问答建议 0.1~0.4"),
        ("max_tokens", "最大输出", "number", "2048", ""),
        ("timeout", "超时(秒)", "number", "120", ""),
    ],
    "embedding": [
        ("provider", "运行方式", "text", "api 或 local",
         "api = 云端接口；local = 用项目 models/ 里的本地模型（离线，见 README）"),
        ("base_url", "接口地址", "text", "https://api.siliconflow.cn/v1", ""),
        ("api_key", "API Key", "password", "sk-...", "硅基流动 bge 系列免费；local 时可留空"),
        ("model", "模型名", "text", "BAAI/bge-m3",
         "local 时填项目内目录，如 models/Qwen3-Embedding-0.6B"),
        ("dimensions", "向量维度", "number", "1024",
         "bge-m3/Qwen3-Embedding-0.6B 均为 1024；换模型后会自动全量重建索引"),
        ("batch_size", "批处理大小", "number", "16", ""),
        ("timeout", "超时(秒)", "number", "60", ""),
    ],
    "summary": [
        ("base_url", "接口地址", "text", "https://api.deepseek.com", ""),
        ("api_key", "API Key", "password", "sk-...", ""),
        ("model", "模型名", "text", "deepseek-chat", ""),
        ("temperature", "温度", "number", "0.5", "总结可略高，行文更自然"),
        ("max_tokens", "最大输出", "number", "4096", ""),
        ("timeout", "超时(秒)", "number", "180", "长文本耗时长，建议 180 以上"),
    ],
}


def _llm_field(kind: str, name: str, label: str, value, ph: str = "",
               typ: str = "text", tip: str = "") -> str:
    """单个配置字段。密码框带显示/隐藏切换，便于核对长密钥。

    密钥类字段一律不回显真实值（页面源码里能直接读到就是泄漏）：
    已设置时只显示占位提示「已保存，留空不修改」，提交空值由服务端保留原值。
    """
    fid = f"{kind}_{name}"
    val = esc(str(value) if value is not None else "")
    if typ == "password" and val:
        val = ""
        ph = "已保存（留空不修改）"
        tip = tip or "密钥不回显；如需更换直接粘贴新密钥"
    if typ == "password":
        inp = ('<div style="display:flex;gap:6px;width:100%">'
               f'<input type="password" id="{fid}" name="{kind}.{name}" '
               f'value="{val}" placeholder="{esc(ph)}" style="flex:1;min-width:0">'
               f'<button type="button" class="btn ghost sm" onclick="'
               f"var e=document.getElementById('{fid}');"
               f"e.type=(e.type==='password'?'text':'password')\">显示</button>"
               '</div>')
    else:
        # number 类型必须声明 step="any"，否则浏览器默认 step=1 只接受整数，
        # 用户输入 0.3 会报「请输入有效值」
        step_attr = ' step="any"' if typ == "number" else ""
        inp = (f'<input type="{typ}" id="{fid}" name="{kind}.{name}"'
               f'{step_attr} value="{val}" placeholder="{esc(ph)}" '
               f'style="flex:1">')
    tip_html = (f'<div class="tip field-tip">{esc(tip)}</div>') if tip else ""
    return (f'<div class="field-row">'
            f'<label class="field-label" for="{fid}">{esc(label)}</label>'
            f'{inp}</div>{tip_html}')


_ST_CSS = """
<style>
.settings-page .card{border:0;border-top:1px solid var(--line);border-radius:0;background:transparent;padding:22px 0 26px;margin:0}
.settings-page .card:first-child{border-top:0;padding-top:0}
.st-nav{position:sticky;top:0;z-index:8;display:flex;gap:6px;flex-wrap:wrap;
  background:var(--bg);padding:8px 0 10px;margin:0 0 14px;border-bottom:1px solid var(--border)}
.st-nav a{border-radius:8px;font-size:13px;color:var(--muted);text-decoration:none;padding:5px 11px;
  border:1px solid var(--border);border-radius:18px;background:var(--card);white-space:nowrap}
.st-nav a:hover{color:var(--accent);border-color:var(--accent)}
.st-sec{scroll-margin-top:64px;margin-bottom:6px}
.st-models{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;align-items:start}
@media(max-width:1100px){.st-models{grid-template-columns:1fr}}
.st-models fieldset{margin-bottom:0;min-width:0}
.st-models .field-row{flex-direction:column;align-items:stretch;gap:4px;margin:6px 0}
.st-models .field-label{font-size:12px;color:var(--muted)}
.st-models .field-tip{margin:2px 0 6px}
.st-adv{margin:12px 0 4px;border-top:1px dashed var(--border);padding-top:10px}
.st-adv summary{cursor:pointer;font-size:13px;color:var(--muted);padding:4px 0}
.st-adv[open] summary{color:var(--text);font-weight:600}
.st-savebar{position:sticky;bottom:0;display:flex;gap:10px;align-items:center;flex-wrap:wrap;
  background:var(--card);border-top:1px solid var(--border);padding:10px 0 2px;margin-top:12px}
.st-cols{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px;align-items:start}
@media(max-width:1000px){.st-cols{grid-template-columns:1fr}}
.st-cols .card{margin-bottom:0}
.st-list{max-height:360px;overflow-y:auto;border:1px solid var(--border);border-radius:8px;padding:0 10px}
.st-list .empty{border:0}
.st-sub{font-size:12px;color:var(--muted);margin:14px 0 6px;font-weight:600}
.st-h2{font-size:15px;margin:6px 0 10px;color:var(--text)}
.st-sec .card{margin-bottom:14px}
</style>"""


def _llm_fieldset(kind: str, title: str, hint: str, conf: dict,
                  advanced: tuple[str, ...] = ()) -> str:
    """一个模型的配置区块，含「测试连接」（用当前表单值，无需先保存）。

    advanced 里的字段收进「高级选项」，页面默认只显示三四个关键字段。
    """
    conf = conf or {}
    core_html, adv_html = "", ""
    for name, label, typ, ph, tip in _LLM_FIELDS[kind]:
        block = _llm_field(kind, name, label, conf.get(name, ""), ph, typ, tip)
        if name in advanced:
            adv_html += block
        else:
            core_html += block
    adv = (f'<details class="st-adv"><summary>高级选项（{len(advanced)} 项）</summary>'
           f'<div style="margin-top:6px">{adv_html}</div></details>') if adv_html else ""
    return f"""
<fieldset data-kind="{kind}" style="border:1px solid var(--border);border-radius:10px;
padding:12px 14px">
<legend style="font-size:13px;font-weight:600;padding:0 6px">{esc(title)}</legend>
<div class="tip" style="margin-bottom:8px">{esc(hint)}</div>
{core_html}
{adv}
<div style="margin-top:10px;display:flex;gap:8px;align-items:center">
<button type="button" class="btn ghost sm"
onclick="testLLM('{kind}', this)">测试连接</button>
<span class="tip" id="test-{kind}"></span>
</div>
</fieldset>"""


_NOT_IN_WATCH = ' <span class="badge amber">不在白名单</span>'


def settings_page(groups: list[dict], watch: list[int], cfg_path: str,
                  keywords: list[str], at_all: bool, llm: dict | None = None,
                  kb_stats: dict | None = None, msg: str = "",
                  knowledge: dict | None = None,
                  ocr_stats: dict | None = None,
                  vision: dict | None = None,
                  vision_status: dict | None = None,
                  watch_buddies: list[int] | None = None,
                  manual_groups: list[int] | None = None,
                  manual_buddies: list[int] | None = None,
                  auto: dict | None = None,
                  auto_state: dict | None = None,
                  auto_group_ids: set[int] | None = None,
                  auto_buddy_ids: set[int] | None = None) -> str:
    """设置页：AI 模型配置 + 索引管理 + 监听白名单 + 已导入群概览。"""
    watch_set = set(watch)
    manual_group_set = set(manual_groups if manual_groups is not None else watch)
    # 好友：数据库里存负数 gid（-QQ），白名单里存正数 QQ，展示时统一成 gid。
    # 以前这里直接拿正数 QQ 与负数 gid 比较，导致好友勾选状态永远是空的。
    watch_buddy_set = {-abs(int(b)) for b in (watch_buddies or [])}
    manual_buddy_set = {-abs(int(b)) for b in (manual_buddies or [])}
    auto_g = set(auto_group_ids or ())
    auto_b = {-abs(int(b)) for b in (auto_buddy_ids or ())}
    auto = auto or {}
    auto_state = auto_state or {}
    # 群（group_id >= 0）与好友（< 0）分组显示
    only_groups = [g for g in groups if g["group_id"] >= 0]
    only_buddies = [g for g in groups if g["group_id"] < 0]

    def _check_row(g: dict, checked_set: set, name_field: str,
                   auto_set: set | None = None) -> str:
        gid = g["group_id"]
        checked = " checked" if gid in checked_set else ""
        badge = (' <span class="badge green">自动</span>'
                 if auto_set and gid in auto_set else "")
        return (
            f'<label style="display:flex;align-items:center;gap:10px;padding:8px 0;'
            f'border-bottom:1px solid var(--border);cursor:pointer">'
            f'<input type="checkbox" name="{name_field}" value="{gid}"{checked}>'
            f'<span style="min-width:120px">{esc(g["name"])}{badge}</span>'
            f'<code class="tip">{gid}</code>'
            f'<span class="tip" style="margin-left:auto">'
            f'{g["msg_count"] or 0} 条消息</span></label>')

    rows = "".join(_check_row(g, watch_set, "groups", auto_g) for g in only_groups)
    empty = ('<div class="empty">数据库还没有任何群。可在知识库页点「从本机 QQ 库同步」'
             '把本机 QQ 里已有的群消息拉进来（无需任何机器人）。</div>') if not rows else ""
    # 白名单里填了但数据库还没导入的群（手动手输的群号）。
    # 必须带 checkbox：以前只是纯文本 chip，保存时表单里没有它 → 被静默丢弃。
    extra = sorted(watch_set - {g["group_id"] for g in only_groups})
    extra_html = ""
    if extra:
        chips = " ".join(
            f'<label class="badge amber" style="cursor:pointer">'
            f'<input type="checkbox" name="groups" value="{gid}" checked '
            f'style="margin-right:4px">{gid}</label>' for gid in extra)
        extra_html = (f'<div style="margin-top:10px" class="tip">白名单中尚未导入的群号'
                      f'（取消勾选即移除）：<br>{chips}<br>'
                      f'下次点「从本机 QQ 库同步」时会作为新群自动入库。</div>')
    # 已入库但不在白名单的群：同步会被静默跳过，必须显式提示，
    # 否则用户点「重生成」看不到任何变化会以为坏了
    excluded_html = ""
    if watch_set:
        out_list = [g for g in only_groups if g["group_id"] not in watch_set]
        if out_list:
            chips2 = "、".join(
                f'{esc(g["name"])}（{g["group_id"]}）' for g in out_list)
            excluded_html = (
                '<div class="badge amber" style="margin-top:10px;'
                'display:inline-block;line-height:1.9">已导入但不在白名单：'
                f'{chips2}<br>这些群不会参与同步（聊天 Markdown / 媒体），'
                '导入新记录也会被拒绝。</div>')

    # ============ 好友白名单 ============
    buddy_rows = "".join(_check_row(g, watch_buddy_set, "buddies", auto_b)
                         for g in only_buddies)
    buddy_empty = ('<div class="empty">数据库还没有任何好友。把好友 QQ 号加入下方白名单后，'
                   '点「从本机 QQ 库同步」自动入库。</div>') if not buddy_rows else ""
    buddy_extra = sorted(watch_buddy_set - {g["group_id"] for g in only_buddies})
    buddy_extra_html = ""
    if buddy_extra:
        chips = " ".join(
            f'<label class="badge amber" style="cursor:pointer">'
            f'<input type="checkbox" name="buddies" value="{gid}" checked '
            f'style="margin-right:4px">{abs(gid)}</label>' for gid in buddy_extra)
        buddy_extra_html = (f'<div style="margin-top:10px" class="tip">白名单中尚未导入的好友'
                            f'（取消勾选即移除）：<br>{chips}<br>'
                            f'下次点「从本机 QQ 库同步」时会自动入库。</div>')
    buddy_status = (f'<span class="badge green">白名单 {len(watch_buddy_set)} 个好友</span>'
                    if watch_buddy_set else
                    '<span class="badge amber">未设置：不同步任何好友</span>')

    auto_n = len(auto_g)
    status = (f'<span class="badge green">白名单 {len(watch)} 个群</span>'
              + (f' <span class="badge green">其中自动 {auto_n} 个</span>'
                 if auto_n else "")
              if watch else
              '<span class="badge amber">未设置：所有群都处理</span>')
    msg_html = (f'<div class="badge green" style="margin-bottom:12px;'
                f'display:inline-block">{esc(msg)}</div>') if msg else ""

    kw = " ".join(f'<span class="badge">{esc(k)}</span>' for k in keywords) \
        or '<span class="tip">（未设置）</span>'

    llm = llm or {}
    rag = llm.get("rag") or {}
    llm_html = (
        '<div class="card"><h3>模型与密钥</h3>'
        '<p class="tip" style="margin-bottom:12px">全部走 OpenAI 兼容协议，'
        '三类模型<b>可分别指向不同服务商</b>。配置保存在 '
        '<code>data/llm.yaml</code>（含密钥，已排除出 git）。</p>'
        '<form method="post" action="/settings/llm" id="llm-form">'
        '<div class="st-models">'
        + _llm_fieldset("chat", "对话 / 知识库问答",
                        "用于 AI 助手页面，基于知识库回答问题。", llm.get("chat", {}),
                        advanced=("temperature", "max_tokens", "timeout"))
        + _llm_fieldset("embedding", "文本向量（知识库检索）",
                        "把知识库内容转成向量以支持语义检索；provider=local 时用项目内模型。",
                        llm.get("embedding", {}),
                        advanced=("base_url", "api_key", "dimensions",
                                  "batch_size", "timeout"))
        + _llm_fieldset("summary", "总结生成",
                        "用于生成日 / 周 / 月 / 年总结。", llm.get("summary", {}),
                        advanced=("temperature", "max_tokens", "timeout"))
        + '</div>'
        + '<details class="st-adv"><summary>高级：知识库检索参数（一般不用改）</summary>'
          '<div style="margin-top:6px">'
        + _llm_field("rag", "index_chat", "索引群聊",
                     "true" if rag.get("index_chat", True) else "false",
                     "true / false", "text",
                     "关闭则不把群聊内容上传云端；群文件与录音转写仍会索引")
        + _llm_field("rag", "top_k", "单次召回片段", rag.get("top_k", 12),
                     "12", "number",
                     "一次检索送入模型的片段数。宁多勿少：漏召回会直接答错")
        + _llm_field("rag", "max_context", "上下文上限(字)",
                     rag.get("max_context", 60000), "60000", "number",
                     "仅在模型不支持工具调用、走固定检索时生效")
        + _llm_field("rag", "max_rounds", "工具调用轮数",
                     rag.get("max_rounds", 6), "6", "number",
                     "Agent 最多连续调用几轮工具（多主题问题常需 3~6 轮）")
        + _llm_field("rag", "tool_result_chars", "检索返回上限(字)",
                     rag.get("tool_result_chars", 30000), "30000", "number",
                     "单次 search_knowledge 结果的最大字符数")
        + _llm_field("rag", "doc_read_chars", "读文档上限(字)",
                     rag.get("doc_read_chars", 60000), "60000", "number",
                     "read_document 一次最多返回多少字")
        + _llm_field("rag", "context_budget_chars", "单轮资料预算(字)",
                     rag.get("context_budget_chars", 150000), "150000", "number",
                     "一次问答里所有工具返回的总字符上限，到顶后模型必须收尾作答")
        + _llm_field("rag", "chunk_size", "分块字数", rag.get("chunk_size", 600),
                     "600", "number", "修改后需「全量重建索引」才生效")
        + '</div></details>'
        + '<details class="st-adv" open><summary>自定义系统提示词（追加在内置提示词之后）</summary>'
          '<div style="margin-top:6px">'
          '<div class="tip field-tip" style="margin-bottom:6px">'
          '内置提示词里写着工具清单、必须查证、引用要标来源这些约束，删掉会开始编造，'
          '所以这里只能<b>追加</b>。写你想让它遵守的额外规则或背景，留空则完全使用内置。</div>'
          f'<div class="field-row"><label class="field-label" for="llm_system_extra">'
          '附加规则</label>'
          f'<textarea id="llm_system_extra" name="llm.system_extra" rows="5" '
          f'style="flex:1;font-family:inherit" '
          f'placeholder="例：回答时先给结论再给依据；引用通知要写明是谁在哪天说的">'
          f'{esc(str(llm.get("system_extra") or ""))}</textarea></div>'
          '</div></details>'
        + '<div class="st-savebar">'
          '<button class="btn" type="submit">保存模型配置</button>'
          ' <span class="tip">测试连接使用当前表单值，无需先保存</span></div>'
        + '</form></div>')

    kb_stats = kb_stats or {}
    if kb_stats.get("chunks"):
        idx_info = (f'<span class="badge green">已索引 {kb_stats["chunks"]} 个片段</span> '
                    f'<span class="badge">来自 {kb_stats["files"]} 个文件</span> '
                    f'<span class="badge">向量化 {kb_stats["with_vector"]}</span>')
        for sec, n in (kb_stats.get("by_section") or {}).items():
            idx_info += f' <span class="badge">{esc(str(sec))} {n}</span>'
        if kb_stats["with_vector"] < kb_stats["chunks"]:
            idx_info += ('<div class="tip" style="margin-top:8px">⚠ 存在无向量的片段，'
                         '语义检索会失效，建议全量重建索引</div>')
    else:
        idx_info = '<span class="badge amber">尚未建立索引</span>'
    idx_html = f"""
<div class="card"><h3>知识库索引</h3>
<p class="tip" style="margin-bottom:10px">把知识库内容切片并生成向量，供 AI 助手语义检索。
{idx_info}</p>
<div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
<form method="post" action="/settings/llm/index" style="display:inline">
<button class="btn" type="submit">增量更新索引</button></form>
<form method="post" action="/settings/llm/index" style="display:inline"
onsubmit="return confirm('全量重建会重新生成所有向量，耗时较长且产生 API 调用，确定吗？')">
<input type="hidden" name="force" value="1">
<button class="btn ghost" type="submit">全量重建</button></form>
<form method="post" action="/settings/llm/index/clear" style="display:inline"
onsubmit="return confirm('确定清空全部索引？清空后 AI 助手将无法检索知识库。')">
<button class="btn danger" type="submit">清空索引</button></form>
</div>
<p class="tip" style="margin-top:10px">知识库新增 / 删除文件后做一次「增量更新」即可，
只处理变动过的文件。更换 embedding 模型后必须用「全量重建」。</p>
</div>"""

    # ============ 自动规则卡片 ============
    auto_suffixes = ", ".join(auto.get("by_remark_suffix") or ["_._"])
    scope = str(auto.get("scope") or "both")
    scope_opts = "".join(
        f'<option value="{v}"{" selected" if scope == v else ""}>{label}</option>'
        for v, label in (("both", "群聊 + 私聊"), ("groups", "仅群聊"),
                         ("friends", "仅私聊")))
    auto_on_chk = " checked" if auto.get("enabled", True) else ""
    pin_chk = " checked" if auto.get("by_pin") else ""
    st = auto_state or {}
    st_time = esc(str(st.get("time") or "尚未执行"))
    if st.get("ok"):
        st_badge = '<span class="badge green">上次扫描成功</span>'
    elif st:
        st_badge = '<span class="badge amber">上次扫描失败</span>'
    else:
        st_badge = '<span class="badge amber">尚未扫描</span>'
    st_err = (f'<div class="badge amber" style="margin-top:8px">'
              f'{esc(str(st.get("error")))}</div>' if st.get("error") else "")

    def _hit_chips(items: list[dict]) -> str:
        if not items:
            return '<span class="tip">（无）</span>'
        return " ".join(
            f'<span class="badge">{esc(str(x.get("name") or x.get("id")))}'
            f' <code class="tip">{x.get("id")}</code></span>' for x in items)

    hits = (f'<div class="tip" style="margin-top:10px">命中群：<br>'
            f'{_hit_chips(st.get("named_groups") or [])}<br>'
            f'命中好友：<br>{_hit_chips(st.get("named_buddies") or [])}')
    add_g, rem_g = st.get("added_groups") or [], st.get("removed_groups") or []
    add_b, rem_b = st.get("added_buddies") or [], st.get("removed_buddies") or []
    if add_g or rem_g or add_b or rem_b:
        hits += (f'<br>上轮变动：群 +{len(add_g)} / -{len(rem_g)}，'
                 f'好友 +{len(add_b)} / -{len(rem_b)}')
    hits += "</div>"
    ignore_n = (len(auto.get("ignore_groups") or [])
                + len(auto.get("ignore_buddies") or []))
    ignore_tip = (f' <span class="tip">（已记下 {ignore_n} 个手动移除项）</span>'
                  if ignore_n else "")

    auto_html = f"""
<div class="card"><h3>白名单自动规则（备注名后缀 / 置顶）</h3>
<p class="tip" style="margin-bottom:10px">在 QQ 里给群或好友设一个<b>备注名</b>，让它以约定后缀结尾
（默认 <code>_._</code>），自动同步每轮扫到后就会把它加进白名单。<br>
去掉后缀后：<b>自动加入的项</b>会被移出，<b>手工勾选的项</b>永远保留——所以自动规则不会误删你自己的选择。</p>
<form method="post" action="/settings/watch-auto" id="auto-form">
<label style="display:flex;align-items:center;gap:8px;margin-bottom:8px">
<input type="checkbox" name="enabled"{auto_on_chk}>
<b>启用自动规则</b>（每轮自动同步时维护白名单）</label>
<label style="display:flex;align-items:center;gap:8px;margin-bottom:8px;flex-wrap:wrap">
<b>备注名后缀</b>
<input type="text" name="suffixes" value="{esc(auto_suffixes)}"
placeholder="_._" style="flex:1;min-width:160px">
<span class="tip">多个用逗号分隔；只匹配结尾</span></label>
<label style="display:flex;align-items:center;gap:8px;margin-bottom:8px">
<input type="checkbox" name="by_pin"{pin_chk}>
同时纳入<b>置顶会话</b>（可选：置顶变动频繁，容易误收）</label>
<label style="display:flex;align-items:center;gap:8px;margin-bottom:10px">
<b>生效范围</b>
<select name="scope" style="padding:4px 6px;border:1px solid var(--line);border-radius:8px">
{scope_opts}</select></label>
<button class="btn" type="submit">保存并立即刷新</button>
</form>
<div style="display:flex;gap:8px;margin-top:10px;flex-wrap:wrap">
<form method="post" action="/settings/watch-auto/refresh" style="display:inline">
<button class="btn ghost" type="submit">立即刷新（只扫描，不改规则）</button></form>
<form method="post" action="/settings/watch-auto/forget-ignored" style="display:inline"
onsubmit="return confirm('清空后，之前手动移除的自动项会重新加入白名单，确定吗？')">
<button class="btn ghost" type="submit">清空「手动移除」记录{ignore_tip}</button></form>
</div>
<div class="tip" style="margin-top:12px">上次扫描：{st_time} {st_badge}</div>
{st_err}{hits}
</div>"""

    groups_card = f"""
<div class="card"><h3>群白名单</h3>
<p class="tip" style="margin-bottom:10px">决定哪些群进入知识库。当前：{status}</p>
<details class="st-adv" style="margin-top:0;border-top:0;padding-top:0">
<summary>规则说明</summary>
<div class="tip" style="margin-top:6px">· <b>本机库同步</b>：白名单非空时同步名单内全部群（<b>新群自动入库</b>）；留空时只更新已入库的群<br>
· <b>知识库同步</b>（聊天 Markdown、媒体、OCR）：只处理名单内且已入库的群<br>
· <b>全部不勾选</b> + 手动框留空 = <b>不限制</b>，所有群照常处理<br>
· <b>自动规则</b>：备注名带约定后缀的群会被自动纳入（列表里标「自动」）；取消勾选自动项会记入「手动移除」，不会被下一轮加回<br>
· <b>「清空」只清手工项</b>：自动命中的群仍保留，要全部清掉请先关掉自动规则<br>
· 保存后<b>立即生效</b>，无需重启；同时写回配置文件持久保存</div>
</details>
<form method="post" action="/settings/watch-groups" id="wg-form">
<label style="display:flex;align-items:center;gap:8px;margin:8px 0">
<input type="checkbox" id="chk-all" onchange="document.querySelectorAll('input[name=groups]').forEach(c=>c.checked=this.checked)">
<b>全选 / 全不选</b></label>
<div class="st-list">{rows}{empty}</div>
{extra_html}
</form>
<div style="margin-top:12px;display:flex;gap:8px;align-items:center;flex-wrap:wrap">
<input type="text" name="manual" form="wg-form" placeholder="手动添加群号，支持逗号/空格分隔" style="flex:1;min-width:200px">
<button class="btn" type="submit" form="wg-form">保存白名单</button>
<form method="post" action="/settings/watch-groups/clear" style="display:inline"
onsubmit="return confirm('清空手工勾选后 = 不限制群（自动规则命中的群仍保留），确定吗？')">
<button class="btn ghost" type="submit">清空</button></form>
</div>
{excluded_html}
</div>"""

    buddies_card = f"""
<div class="card"><h3>好友白名单</h3>
<p class="tip" style="margin-bottom:10px">私聊隐私敏感度高于群聊，语义与群相反。当前：{buddy_status}</p>
<details class="st-adv" style="margin-top:0;border-top:0;padding-top:0">
<summary>规则说明</summary>
<div class="tip" style="margin-top:6px">· <b>全部不勾选</b> + 手动框留空 = <b>不同步任何好友</b>（默认，保护私聊隐私）<br>
· 勾选或填入好友 QQ 号 → 点「从本机 QQ 库同步」，该好友的私聊消息才会入库<br>
· 好友在数据库中以负数编号存储（如 <code>-123456789</code>），与群（正数）区分<br>
· 保存后<b>立即生效</b>；从白名单移除后，已入库的好友消息<b>不会自动删除</b>，可到知识库页手动删除</div>
</details>
<form method="post" action="/settings/watch-buddies" id="wb-form">
<label style="display:flex;align-items:center;gap:8px;margin:8px 0">
<input type="checkbox" id="chk-all-buddy" onchange="document.querySelectorAll('input[name=buddies]').forEach(c=>c.checked=this.checked)">
<b>全选 / 全不选</b></label>
<div class="st-list">{buddy_rows}{buddy_empty}</div>
{buddy_extra_html}
</form>
<div style="margin-top:12px;display:flex;gap:8px;align-items:center;flex-wrap:wrap">
<input type="text" name="manual" form="wb-form" placeholder="手动添加好友 QQ 号" style="flex:1;min-width:200px">
<button class="btn" type="submit" form="wb-form">保存好友白名单</button>
<form method="post" action="/settings/watch-buddies/clear" style="display:inline"
onsubmit="return confirm('清空后 = 不同步任何好友，确定吗？')">
<button class="btn ghost" type="submit">清空</button></form>
</div>
</div>"""

    imported_card = f"""
<div class="card"><h3>已导入的群（数据库）</h3>
<p class="tip" style="margin-bottom:10px">数据库里已有消息的群，也就是知识库实际能收录的范围。增删方式：白名单勾选/填群号后「从本机 QQ 库同步」加入；在<a href="/kb">知识库页</a>「聊天记录」里删除。</p>
<table><tr><th>群</th><th>群号</th><th class="num">消息数</th><th>最近消息</th></tr>
{"".join(f'<tr><td>{esc(g["name"])}'
         f'{_NOT_IN_WATCH if (watch_set and g["group_id"] not in watch_set) else ""}'
         f'</td><td><code>{g["group_id"]}</code></td>'
         f'<td class="num">{g["msg_count"] or 0}</td>'
         f'<td>{esc(str(g.get("last_time") or "-")[:19])}</td></tr>' for g in groups)
 or '<tr><td colspan="4" class="empty">暂无群</td></tr>'}
</table></div>"""

    misc_card = f"""
<div class="card"><h3>重要消息关键词</h3>
<p class="tip" style="margin-bottom:10px">命中这些词的消息自动标记为重要（含 @全体成员：{'是' if at_all else '否'}）。如需修改，请编辑配置文件 <code>important.keywords</code> 后重启。</p>
<div>{kw}</div></div>
<p class="tip">配置文件：<code>{esc(cfg_path)}</code>　模型配置：<code>data/llm.yaml</code></p>"""

    kb_card = _knowledge_html(knowledge or {}, ocr_stats or {},
                              vision or {}, vision_status or {})

    return f"""<div class="settings-page">
{_ST_CSS}
{msg_html}
<nav class="st-nav">
<a href="#sec-models">模型与密钥</a>
<a href="#sec-index">索引</a>
<a href="#sec-watch">白名单</a>
<a href="#sec-knowledge">知识库处理</a>
<a href="#sec-imported">已导入群</a>
<a href="#sec-misc">其它</a>
</nav>
<section class="st-sec" id="sec-models">{llm_html}</section>
<section class="st-sec" id="sec-index">{idx_html}</section>
<section class="st-sec" id="sec-watch">
<h2 class="st-h2">白名单</h2>
{auto_html}
<div class="st-cols">{groups_card}{buddies_card}</div>
</section>
<section class="st-sec" id="sec-knowledge">
<h2 class="st-h2">知识库处理</h2>
{kb_card}
</section>
<section class="st-sec" id="sec-imported">{imported_card}</section>
<section class="st-sec" id="sec-misc">
<h2 class="st-h2">其它</h2>
{misc_card}
</section>
{_LLM_TEST_JS}</div>
"""


def _knowledge_html(k: dict, ocr_s: dict,
                    vision: dict | None = None,
                    vision_status: dict | None = None) -> str:
    """知识库设置卡片：OCR 开关、自动同步、NTQQ 库路径只读提示、OCR 缓存统计、图片理解引擎。"""
    ocr_on = "checked" if k.get("ocr_chat_images", True) else ""
    wrap_on = "checked" if k.get("ocr_formula_wrap", False) else ""
    nt = k.get("nt_db") or ""
    nt_html = (f'<code>{esc(nt)}</code>' if nt
               else '<span class="badge amber">未配置</span>（聊天媒体不会被同步）')
    try:
        auto_min = float(k.get("auto_sync_minutes") or 0)
    except (TypeError, ValueError):
        auto_min = 0
    auto_on = "checked" if auto_min > 0 else ""
    auto_val = auto_min if auto_min > 0 else 3
    return f"""
<div class="card"><h3>知识库 · 图片 OCR</h3>
<p class="tip" style="margin-bottom:10px">聊天同步时把图片里的文字 OCR 出来，内联进聊天 Markdown——
让 AI 能看到群里发的通知截图内容。表格会还原成 Markdown 表格，公式行会被保护不被段落合并拆散。</p>
<form method="post" action="/settings/knowledge" style="margin-bottom:12px">
<label style="display:flex;align-items:center;gap:8px;margin-bottom:8px">
<input type="checkbox" name="ocr_chat_images" {ocr_on}>
<b>聊天同步时识别图片文字</b>（依赖 rapidocr-onnxruntime）</label>
<label style="display:flex;align-items:center;gap:8px;margin-bottom:10px">
<input type="checkbox" name="ocr_formula_wrap" {wrap_on}>
<b>疑似公式行用 $...$ 包裹</b>（启发式判断，有误判；真正的 LaTeX 公式识别需另装 pix2tex）</label>
<button class="btn" type="submit">保存</button>
</form>

<h3 style="margin-top:18px">知识库 · 自动同步</h3>
<p class="tip" style="margin-bottom:10px">开启后服务端每隔设定分钟数自动从本机 QQ 库同步新消息并清洗
（复制媒体 → 识别图片 → 渲染聊天 Markdown）。<b>没有新消息时本轮秒级结束</b>，不会白白加载识别模型。</p>
<form method="post" action="/settings/autosync" style="margin-bottom:12px">
<label style="display:flex;align-items:center;gap:8px;margin-bottom:8px">
<input type="checkbox" name="enabled" {auto_on}>
<b>启用定时自动同步</b></label>
<label style="display:flex;align-items:center;gap:8px;margin-bottom:10px">
间隔 <input type="number" name="minutes" value="{auto_val}" min="1" step="1"
style="width:70px;padding:4px 6px;border:1px solid var(--line);border-radius:8px"> 分钟
<span class="tip">（下限 1 分钟；需重启服务后生效）</span></label>
<button class="btn" type="submit">保存</button>
</form>
<p class="tip" style="margin-bottom:6px">NTQQ 原始库（媒体同步用，敏感，只读展示）：{nt_html}</p>
<p class="tip" style="margin-bottom:6px">图片 OCR 缓存：共 {ocr_s.get('total', 0)} 张，其中 {ocr_s.get('with_text', 0)} 张有文字，
合计 {ocr_s.get('chars', 0)} 字。漏识别可到<a href="/kb">知识库页</a>点「重建图片 OCR」全量重跑。</p>
<p class="tip">NTQQ 库路径与密钥请直接编辑 <code>config.yaml</code> 的 <code>knowledge.nt_db</code> / <code>knowledge.key</code>（敏感信息不在此页保存）。</p>
</div>

{_vision_html(vision or {}, vision_status or {})}"""


def _vision_html(v: dict, st: dict) -> str:
    """图片理解引擎设置卡片：本地 VLM / 云端 API / 关闭。"""
    backend = v.get("backend", "local")
    ok = st.get("ok")
    why = st.get("why", "")
    gpu = st.get("gpu", "")
    if ok:
        badge = f'<span class="badge green">已就绪（{st.get("engine", backend)}）</span>'
    elif backend == "off":
        badge = '<span class="badge amber">已关闭（回退 RapidOCR）</span>'
    else:
        badge = f'<span class="badge amber">不可用</span>'

    api = v.get("api") or {}
    # 注意：f-string 里不能嵌带同类引号的字典字面量（Python 3.11 限制），
    # 所以先把标签映射提到外面
    labels = {
        "local": "本地 GPU 推理（推荐，隐私不出本机）",
        "api": "云端视觉模型（可上大模型，图片会离开本机）",
        "off": "关闭（回退 RapidOCR）",
    }
    sel = ""
    for b in ("local", "api", "off"):
        picked = "selected" if backend == b else ""
        sel += f'<option value="{b}" {picked}>{labels[b]}</option>'

    gpu_html = f'<p class="tip">显卡：{esc(gpu)}</p>' if gpu else ""
    why_html = f'<p class="tip" style="color:#B23A48">原因：{esc(why)}</p>' if why else ""

    return f"""
<div class="card"><h3>图片理解引擎（视觉语言模型）</h3>
<p class="tip" style="margin-bottom:10px">用视觉语言模型取代单纯 OCR：非文字图片能<b>描述画面内容</b>，
纯文字图片能<b>精确提取表格（Markdown）、公式（LaTeX）、分栏排版</b>。
当前状态：{badge}</p>
{gpu_html}{why_html}
<form method="post" action="/settings/vision" style="margin-bottom:10px">
<label style="display:block;margin-bottom:6px"><b>识别后端</b></label>
<select name="backend" style="margin-bottom:10px;padding:6px;width:100%;max-width:420px">{sel}</select>

<label style="display:block;margin-bottom:6px"><b>本地模型</b>（backend=local 时用，
首次调用会自动下载约 6GB）</label>
<input name="model" value="{esc(v.get('model', 'Qwen/Qwen2.5-VL-3B-Instruct'))}"
 style="width:100%;margin-bottom:10px;padding:6px">

<label style="display:block;margin-bottom:6px"><b>单图最大像素</b>（越大越清晰但越占显存，
8GB 显卡建议保持默认）</label>
<input name="max_pixels" type="number" value="{v.get('max_pixels', 1003520)}"
 style="margin-bottom:10px;padding:6px">

<label style="display:block;margin-bottom:6px"><b>量化</b></label>
<select name="quant" style="margin-bottom:12px;padding:6px">
<option value="4bit" {"selected" if v.get("quant", "4bit") == "4bit" else ""}>4bit（推荐：显存 6GB→2.4GB，速度约快一倍）</option>
<option value="none" {"selected" if v.get("quant", "4bit") == "none" else ""}>不量化 bf16（显存充裕时质量最佳）</option>
</select>

<hr style="margin:12px 0">
<p class="tip" style="margin-bottom:8px"><b>云端视觉 API</b>（backend=api 时填写，
可用 72B 大模型换取更好效果，但图片会上传到服务商）</p>
<label style="display:block;margin-bottom:4px">接口地址</label>
<input name="api_base_url" value="{esc(api.get('base_url', ''))}"
 placeholder="https://api.siliconflow.cn/v1" style="width:100%;margin-bottom:8px;padding:6px">
<label style="display:block;margin-bottom:4px">模型名</label>
<input name="api_model" value="{esc(api.get('model', ''))}"
 placeholder="Qwen/Qwen2.5-VL-72B-Instruct" style="width:100%;margin-bottom:8px;padding:6px">
<label style="display:block;margin-bottom:4px">API Key</label>
<input name="api_key" type="password" value="" placeholder="留空则不修改已保存的密钥"
 style="width:100%;margin-bottom:12px;padding:6px">

<button class="btn" type="submit">保存</button>
</form>
<p class="tip">模型权重下载（国内推荐 ModelScope，比 HuggingFace 快）：</p>
<p class="tip" style="margin-top:4px"><code>pip install modelscope</code> 然后
<code>python -c "from modelscope import snapshot_download; snapshot_download('Qwen/Qwen2.5-VL-3B-Instruct', local_dir='models/Qwen2.5-VL-3B-Instruct')"</code>，
再把上面的「本地模型」填成该目录路径。</p>
</div>"""


# ---------- AI 助手（问答 + 总结） ----------
def _tool_trace_html(tools) -> str:
    """把持久化的工具调用记录渲染成可折叠的过程块。

    tools 元素可能是 dict（Agent 记录：含 label / summary / args_text / ok），
    也可能是早期版本的字符串（如 "search_knowledge"），两种都兼容。
    """
    items = ""
    n = 0
    for t in tools or []:
        if isinstance(t, dict):
            name = t.get("label") or t.get("name") or ""
            summary = t.get("summary") or ""
            args = t.get("args_text") or ""
            ok = t.get("ok", True)
        else:
            name = "检索知识库" if t == "search_knowledge" else str(t or "")
            summary = args = ""
            ok = True
        if not name:
            continue
        n += 1
        flag = ('<span class="tc-ok">✓</span>' if ok
                else '<span class="tc-fail">✗ 失败</span>')
        items += f'<div class="tc-item">{flag} <b>{esc(name)}</b> {esc(summary)}'
        if args:
            items += f'<div class="tc-args">{esc(args)}</div>'
        items += '</div>'
    if not n:
        return ""
    return (f'<details class="tool-calls"><summary>工具调用 · {n} 次</summary>'
            f'{items}</details>')


def chat_page(conv: dict | None, messages: list[dict] | None = None,
              ready: dict | None = None, stats: dict | None = None,
              groups: list[dict] | None = None, error: str = "",
              periods: tuple = ("week", "month", "year")) -> str:
    """AI 对话页：三栏布局（会话列表在侧边栏，中间对话流，右侧工具与引用）。

    一期未接工具调用，检索由后端固定执行，右栏展示检索到的来源，
    为二期 Function Calling 预留展示位置。
    """
    messages = messages or []
    ready = ready or {}
    stats = stats or {}
    groups = groups or []
    conv = conv or {}

    # ---- 消息流 ----
    if messages:
        msgs_html = ""
        for m in messages:
            role = m.get("role", "")
            content = m.get("content") or ""
            if role == "user":
                msgs_html += (
                    f'<div class="msg user"><div class="user-bubble">'
                    f'{esc(content)}</div></div>')
            elif role == "assistant":
                src_items = ""
                for src in (m.get("sources") or []):
                    if not isinstance(src, dict):
                        continue
                    loc = str(src.get("source") or "")
                    sec = str(src.get("section") or "")
                    kind = str(src.get("kind") or "")
                    day = src.get("day") or ""
                    name = Path(loc).name
                    label = src.get("title") or loc or src.get("group_name") or f"引用 {src.get('id', '')}"
                    url = ""
                    if src.get("ref"):
                        url = str(src["ref"])
                    elif src.get("msg_id"):
                        url = f"/msg/{src['msg_id']}"
                    elif loc and (kind == "chatfile" or name and loc.split("/", 1)[0] == "chat"):
                        gid = name.rsplit(".md", 1)[0].split("_", 1)[0]
                        url = f"/kb/chat/{quote(name)}"
                        if day and gid.lstrip("-").isdigit():
                            url = (f"/messages?group_id={gid}"
                                   f"&date_from={quote(day)}&date_to={quote(day)}")
                    elif loc and (kind in ("documents", "transcripts") or sec in ("documents", "transcripts")):
                        url = f"/kb/file/{kind or sec}/{quote(name)}"
                    if url:
                        src_items += (
                            f'<li><a href="{esc(url)}" target="_blank">{esc(label)}</a>'
                            + (f'<span class="src-day">{esc(day)}</span>' if day else '')
                            + '</li>')
                    else:
                        src_items += (
                            f'<li><span>{esc(label)}</span>'
                            + (f'<span class="src-day">{esc(day)}</span>' if day else '')
                            + '</li>')
                n_src = len(m.get("sources") or [])
                src_html = ""
                if n_src:
                    src_html = (f'<details class="msg-sources">'
                                f'<summary>引用 {n_src} 条</summary>'
                                f'<ul>{src_items}</ul></details>')
                model = m.get("model") or ""
                model_html = (f'<span class="msg-model">{esc(model)}</span>'
                              if model else "")
                msgs_html += (
                    f'<div class="msg assistant">'
                    f'{_tool_trace_html(m.get("tools"))}'
                    f'<div class="markdown msg-text">{md_to_html(content)}</div>'
                    f'{src_html}'
                    f'<div class="msg-foot"><button class="msg-act" type="button" '
                    f'data-copy>复制</button>{model_html}</div></div>')
    else:
        msgs_html = f"""
<div class="chat-empty">
<h3>有什么可以帮你的？</h3>
<p>可以问我群里说过的事、文件里的内容，或让我帮你整理资料。</p>
<div class="suggest">
<button type="button" onclick="ask('最近有哪些重要通知？')">最近有哪些重要通知？</button>
<button type="button" onclick="ask('最近群里说了什么重要的事？')">最近群里说了什么？</button>
<button type="button" onclick="ask('帮我整理一下待办事项')">帮我整理待办事项</button>
</div></div>"""

    err_html = ""
    if error:
        err_html = (f'<div style="padding:0 20px 12px">'
                    f'<div class="bubble error" style="max-width:none">'
                    f'{esc(error)}</div></div>')
        msgs_html += err_html

    # ---- 顶部信息条 + 菜单 ----
    def _badge(kind: str, label: str) -> str:
        cls = "ok" if ready.get(kind) else "bad"
        txt = "已配置" if ready.get(kind) else "未配置"
        return f'<span class="chip {cls}">{label}·{txt}</span>'

    status = (_badge("chat", "对话") + _badge("embedding", "向量")
              + _badge("summary", "总结"))
    idx_txt = (f'索引 {stats["chunks"]} 片段' if stats.get("chunks")
               else "尚未建立索引")
    if not all(ready.get(k) for k in ("chat", "embedding", "summary")):
        status += '<a class="chip warn" href="/settings">去设置</a>'

    tabs = ""
    for pr, label in (("week", "周"), ("month", "月"), ("year", "年")):
        tabs += (f'<button class="menu-item" type="submit" name="period" '
                 f'value="{pr}">{label}总结</button>')
    g_opts = "".join(f'<option value="{g["group_id"]}">{esc(g["name"])}</option>'
                     for g in groups)
    g_sel = ('<select name="group_id" class="menu-select">'
             '<option value="">全部群</option>' + g_opts + '</select>') if g_opts else ""

    cid = conv.get("id") or ""
    return f"""
<div class="chat-page">
  <div class="chat-top">
    <div class="chat-chips">{status}<span class="tip">{esc(idx_txt)}</span></div>
    <details class="chat-menu">
      <summary class="menu-btn" title="更多操作">⋯</summary>
      <div class="menu-panel">
        <button class="menu-item" type="button" onclick="renameConv()">重命名会话</button>
        <form method="post" action="/chat/new"><button class="menu-item" type="submit">新建会话</button></form>
        <div class="menu-sep"></div>
        <form method="post" action="/assistant/summary" class="menu-form">
          <input type="hidden" name="cid" value="{esc(cid)}">
          <div class="menu-label">生成总结</div>
          <div class="menu-row">{tabs}{g_sel}</div>
        </form>
        <div class="menu-sep"></div>
        <form method="post" action="/chat/{quote(cid)}/delete"
              onsubmit="return confirm('确定删除这个会话？')">
          <button class="menu-item danger" type="submit">删除会话</button>
        </form>
      </div>
    </details>
  </div>

  <div class="chat-scroll" id="msgs">{msgs_html}{err_html}</div>

  <div class="chat-composer">
    <form method="post" action="/chat/{quote(cid)}/send" id="send-form">
      <textarea name="content" id="input" rows="1"
        placeholder="问我群里的事、文件内容，或让我整理资料…"></textarea>
      <div class="composer-row">
        <span class="tip">Enter 发送 · Shift+Enter 换行</span>
        <div class="composer-actions">
          <span class="tip" id="status"></span>
          <button class="btn" type="submit" id="send-btn">发送</button>
        </div>
      </div>
    </form>
  </div>
</div>
{_CHAT_JS}"""


def conversation_list_page(items: list[dict], keyword: str = "") -> str:
    """会话列表页（搜索结果也用这个）。"""
    rows = ""
    for c in items:
        title = (c.get("title") or "新对话").strip() or "新对话"
        rows += (
            f'<div style="display:flex;align-items:center;gap:12px;padding:11px 0;'
            f'border-bottom:1px solid var(--border)">'
            f'<a class="conv-title" href="/chat/{c["id"]}">'
            f'{esc(title)}</a>'
            f'<span class="tip">{c.get("msg_count", 0)} 条</span>'
            f'<span class="tip">{esc((c.get("updated_at") or "")[:16])}</span>'
            f'<form method="post" action="/chat/{c["id"]}/delete" '
            f'style="display:inline" onsubmit="return confirm(\'删除这个会话？\')">'
            f'<button class="btn ghost sm conv-del" type="submit">删除</button></form></div>')
    empty = ""
    if not rows:
        empty = ('<div class="empty">没有找到对话。'
                 + ('试试其他关键词。' if keyword else
                    '点击「新建对话」开始。') + '</div>')
    return f"""
<div class="toolbar">
<form method="post" action="/chat/new" style="display:inline"><button class="btn" type="submit">＋ 新建对话</button></form>
<form method="get" action="/chat" style="display:flex;gap:8px">
<input type="text" name="q" value="{esc(keyword)}" placeholder="搜索对话内容…"
style="width:240px">
<button class="btn ghost" type="submit">搜索</button>
</form>
</div>
<div class="card">{empty}{rows}</div>"""


def summary_view_page(name: str, md_html: str) -> str:
    """查看单个已生成的总结文件。"""
    return f"""
<div class="toolbar">
<a class="btn ghost sm" href="/chat">返回 AI 对话</a>
<span class="tip">{esc(name)}</span>
</div>
<div class="card"><div class="markdown">{md_html}</div></div>"""


# ---------- 登录 ----------
def login_page(next_url: str = "/", error: str = "") -> str:
    """最简单的口令登录页（独立页面，不套看板导航）。"""
    err_html = (f'<p style="color:#c0392b;margin:0 0 12px">{esc(error)}</p>'
                if error else "")
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>登录 · QQ 群信息整理</title>
<style>
body{{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
background:linear-gradient(135deg,#0f172a,#1e293b);font-family:system-ui,'Microsoft YaHei',sans-serif;color:#e2e8f0}}
.card{{background:#fff;color:#1e293b;border-radius:14px;padding:28px 30px;width:320px;
box-shadow:0 18px 50px rgba(0,0,0,.35)}}
h1{{font-size:17px;margin:0 0 6px}} p.tip{{color:#64748b;font-size:13px;margin:0 0 16px}}
input{{width:100%;box-sizing:border-box;padding:10px 12px;border:1px solid #cbd5e1;border-radius:10px;
font-size:14px;margin-bottom:14px}}
button{{width:100%;padding:10px;border:0;border-radius:10px;background:#2563eb;color:#fff;
font-size:14px;cursor:pointer}} button:hover{{background:#1d4ed8}}
</style></head><body>
<form class="card" method="post" action="/login">
<h1>QQ 群信息整理</h1>
<p class="tip">此实例开启了访问口令，请输入后继续。</p>
{err_html}
<input type="hidden" name="next" value="{esc(next_url)}">
<input type="password" name="token" placeholder="访问口令" autofocus autocomplete="current-password" required>
<button type="submit">进入看板</button>
</form></body></html>"""
