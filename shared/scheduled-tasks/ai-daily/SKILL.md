---
name: ai-daily
description: 每天 09:00 按 1. AI 产业日报 → 2. X 热点 & AI Builder（纯 X List）→ 3. GitHub Trending & Hacker News 顺序三合一串行抓取、跨 Bot 账本去重、写稿并推送至飞书与 Notion
---

目标：每天早上 **09:00**，由本任务在**同一个主控会话内**严格按 **`1. AI 产业日报 → 2. X 热点 & AI Builder（纯 X List）→ 3. GitHub Trending & Hacker News`** 的先后顺序三合一串行完成抓取、去重、写稿、飞书推送与 Notion 归档（原 Latent Space `x-hotspot-bot` 已正式退役，X 圈热点与 Builder 实操统一收口至自建 X List）。

- **模型识别与自主 Orchestrate 原则**：主会话直接识别并运行在用户在 Automations 界面为本任务选定的最新模型上（不硬编码模型版本）。主 Agent 拥有**自主 Orchestrate 权**：若第 3 棒上下文偏长，可按需通过 `invoke_subagent(TypeName="self", Model="flash")` 委派独立子会话起草初稿，避免单会话尾段上下文过载；主 Agent 须在 `send` 前完成事实、链接保真与排版把关。

> **⚠️ 为什么采用 09:00 三合一串行（极简 + 零重复 + 零唤醒竞态）？**
> 1. **第 1 棒《AI 产业日报》**：客观事实雷达（≤26 字中文主标题 + 首句加粗核心事实 + 3 日自重复拦截），`send` 原子加锁写入当日共享账本 `shared/logs/daily_topic_ledger.json`。
> 2. **第 2 棒《X 热点 & AI Builder》**（目录：`ai-builder-bot/`）：纯自建 X List（`twitterapi.io` 直连），按 HN 风格综合讨论热度分（`赞 + 评×12 + 转×3 + 引×8`）排序，并发抓取去广告 Relevance 高赞评论区（单帖作者自评限 1 席）、X Article 全文与外链博客正文，拦截 `ICYMI` 与近 5 天自重复；一站式覆盖 X 全网焦点争论、一线 Builder 实操工具与深度长文。
> 3. **第 3 棒《GitHub Trending & Hacker News 早报》**：读取前 2 棒完整账本（零唤醒竞态），GitHub 全页新星优先挖掘（全局不足自动补抓 Python/TS 分榜，熟面孔折叠）+ HN Top 8 承接前 2 棒账本自动从 `#9–#12` 递补，且仅记实发条目进 `seen_history.json`。周一至周六出日榜（`=== FETCH_OK ===`），周日出周榜（`=== WEEKLY_OK ===`）。

**时间线**：每天 09:00 触发（主力）→ 10:00 由 `shared/ai_chain_health.sh` 按相同顺序三块串行做轻量化健康检查（仅核查状态与 `drafts_fresh` 本地重发兜底，不拉起无头 CLI）。

> 以下所有路径均相对于本仓库根目录（Antigravity Sidecar 已将工作区设为仓库根目录）；共享层位于 `shared/`。

⚠️ **两条工具执行铁律（防截断 & 防 `$` 吞字）：**
1. **防截断**：执行 `fetch` 后，完整输出已自动落盘到各 bot 的 `logs/last_context.txt`。请直接使用 `read_file` 读取 `logs/last_context.txt`，严禁重复执行 `fetch`。
2. **防 `$` 变量吞字**：写稿落盘时，必须使用 `write_file`（`Overwrite: true`）写入各 bot 的 `logs/report_draft.txt`，严禁使用 `cat <<EOF` 或 `echo`。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
## 第一部分（第 1 棒）：AI 产业日报

**每日统一出《AI 产业日报》（包括周日，不再出周回顾）。**

### 步骤 1 — 抓取
执行命令（若遇沙盒配置报错或拦截网络/本地代理，请使用 `BypassSandbox: true`）：
```bash
bash "AI Daily News Bot/claude_report.sh" fetch
```
按 stdout 标记决定：
- `=== SKIP_ALREADY_RAN ===` / `=== SKIP_PROXY ===` / `=== NO_NEWS ===`：记录状态并进入第二部分。
- `=== FETCH_OK ===`：使用 `read_file` 读取 `AI Daily News Bot/logs/last_context.txt` 与 `AI Daily News Bot/prompt.md`，进入步骤 2。
- 若有 `=== SOURCE_ALERT ===`，原样记下并在最终汇报单列「源健康」。

### 步骤 2 — 写稿
严格按 `AI Daily News Bot/prompt.md` 写稿（≤26 字中文主标题 + 去掉 `📄 The Details：` + 首句加粗核心事实 + 对照 `RECENT_SELF_HEADLINES` 防隔日自重复 + 最多 12 条倒序），用 `write_file`（`Overwrite: true`）写入 `AI Daily News Bot/logs/report_draft.txt`。

### 步骤 3 — 发送与核对
```bash
bash "AI Daily News Bot/claude_report.sh" send
```
核对 `tail -1 "AI Daily News Bot/logs/run.log"`。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
## 第二部分（第 2 棒）：X 热点 & AI Builder（纯 X List · HN 风格高赞高评）

### 步骤 1 — 取材
```bash
bash "ai-builder-bot/claude_report.sh" fetch
```
按 stdout 标记决定：
- `=== SKIP_ALREADY_RAN ===` / `=== SKIP_PROXY ===` / `=== FEED_ERROR ===` / `=== NO_TWEETS ===`：记录状态并进入第三部分。
- `=== FETCH_OK ===`：使用 `read_file` 读取 `ai-builder-bot/logs/last_context.txt` 与 `ai-builder-bot/prompt.md`，进入步骤 2。

### 步骤 2 — 写稿
严格按 `ai-builder-bot/prompt.md` 写稿（标题 `<b>🔥 X 热点 & AI Builder · [日期]</b>`；按高赞高评热度筛选 5–7 条、淡日 4–5 条宁缺毋滥；拦截 `ICYMI` 与 `RECENT_SELF_HEADLINES` 冷饭；标题挂主原推直链不重复；每条写透「这帖在聊啥 + 评论区在吵啥 / 评论区亮点与启发」，无评论素材绝不编造；可选文末 `⚡ 短讯雷达`），用 `write_file`（`Overwrite: true`）写入 `ai-builder-bot/logs/report_draft.txt`。

### 步骤 3 — 发送与核对
```bash
bash "ai-builder-bot/claude_report.sh" send
```
核对 `tail -1 "ai-builder-bot/logs/run.log"`。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
## 第三部分（第 3 棒）：GitHub Trending & Hacker News 早报

### 步骤 1 — 抓取
```bash
bash "github-hn-bot/claude_report.sh" fetch
```
按 stdout 标记决定：
- `=== SKIP_ALREADY_RAN ===` / `=== SKIP_PROXY ===` / `=== NO_DATA ===`：记录状态并进入汇报。
- `=== FETCH_OK ===`（周一至周六日榜）或 `=== WEEKLY_OK ===`（周日周榜）：
  使用 `read_file` 读取 `github-hn-bot/logs/last_context.txt`，并按模式读取 `github-hn-bot/prompt.md`（日榜）或 `github-hn-bot/prompt_weekly.md`（周榜），进入步骤 2。

### 步骤 2 — 写稿
严格按对应 `prompt.md` / `prompt_weekly.md` 写稿：
- GitHub 板块：完整展开 `🆕 新上榜项目`（痛点 + 机制 + 对比大白话、敢劝退），将 `🔁 持续霸榜熟面孔` 收拢到文末紧凑折叠区（每条 1 句简述 + `<b>（见 MM月DD日 推送）</b>`）；
- HN 板块：精选 Top 8，遇到 `🔁 今日 09:00 前序 AI 日报已播` 条目时优先从候选池（#9–#12）递补未播新帖；非争议帖可将 `评论区交锋与避坑` 弹性改名为 `评论区亮点`；
- 用 `write_file`（`Overwrite: true`）写入 `github-hn-bot/logs/report_draft.txt`。

### 步骤 3 — 发送与核对
```bash
bash "github-hn-bot/claude_report.sh" send
```
核对 `tail -1 "github-hn-bot/logs/run.log"`。

━━━━━━━━━━━━━━━
## 汇报格式

用中文简要汇报三块执行结果：

```text
AI 早间 09:00 三合一流水线（产业日报 → X 热点 & AI Builder → GitHub & HN）
  · 1. AI 产业日报：{状态}
  · 2. X 热点 & AI Builder：{状态}
  · 3. GitHub & HN 早报：{状态}
```

若任一块出现 `SOURCE_ALERT`，在状态后单列一节「源健康」原样列出；没有则不提。
注意：不修改任何脚本或 plist；同一块的 fetch/send 不要重复跑；任一块失败不阻塞下一块。
