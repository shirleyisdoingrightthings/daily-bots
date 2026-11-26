---
name: morning-catchup-daily-bots
description: 每周一至周六早 09:00 严格串行运行：第 1 棒《美股收盘 & 加密行情日报》→ 第 2 棒《🇨🇳 A股开盘前瞻》（务必在 09:30 前完成）
---

目标：每周一至周六 **09:00**，在**同一个主控会话内严格串行执行**早间跨市场行情流水线，并在 **09:30 A 股开盘前**完成全部写稿、推送与 Notion 归档：
1. **第 1 棒（09:00 立刻启动）**：`us stock bot`（美股收盘 & 加密行情日报，周二至周六出稿；周一无美股新收盘场次时自动 `SKIP_NO_NEW_SESSION` 跳过）。
2. **第 2 棒（紧接第 1 棒完成后立刻启动，务必在 09:30 前完成）**：`a-share-bot` `premarket`（🇨🇳 A 股开盘前瞻，已彻底移除港股；周一至周五出稿，自动读取第 1 棒刚生成的美股日报正稿 + 前一日 A 股收盘复盘 + 盘后至盘前宏观政策电报；周六非交易日自动 `SKIP_NON_TRADING_DAY` 跳过）。

> **执行与模型调度原则**：
> - 本任务对 **09:00 → 09:30 时效窗口**要求极高，建议由当前主控模型直接高效完成两棒串行写稿与核校（减少不必要的子代理来回上下文损耗）；所有写稿口径、星级标准与排版细则**一律以各 bot 目录下的 `prompt_*.md` 为唯一真相源**，避免本文件重复描述导致规则漂移。
> - **防截断**：执行 `fetch` 后直接使用 `read_file` 读取 `logs/last_context.txt`，严禁重跑 `fetch`。
> - **防 `$` 吞字**：写稿落盘一律使用 `write_file`（`Overwrite: true`），严禁 `cat <<EOF` 或 `echo`。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
## 第一棒（09:00）：美股收盘 & 加密行情日报（`us stock bot`）

### 步骤 1 — 抓取
执行命令（遇沙盒拦截网络/本地代理 `127.0.0.1:1082` 时使用 `BypassSandbox: true`）：
```bash
bash "us stock bot/claude_report.sh" fetch
```
按 stdout 标记决定：
- `=== SKIP_ALREADY_RAN ===`：今天已推送过 → 记「已跳过」，**立即进入第二棒（A 股开盘前瞻）**。
- `=== SKIP_NO_NEW_SESSION ===`：无新美股交易日（周一或美股假期）→ 记「无新交易日，已跳过」，**立即进入第二棒（A 股开盘前瞻）**。
- `=== SKIP_PROXY ===` / `=== SKIP_NO_QUOTES ===`：记未推送原因，**仍继续执行第二棒（A 股开盘前瞻）**。
- `=== FETCH_OK ===`：并行用 `read_file` 读取 `us stock bot/logs/last_context.txt`、`us stock bot/prompt_market.md`、`us stock bot/prompt_news.md`，进入步骤 2。

### 步骤 2 — 写两稿（严格遵循 `prompt_market.md` 与 `prompt_news.md`）
1. 按 `us stock bot/prompt_market.md` 写出《美股收盘 & 加密行情日报》并 `write_file`（`Overwrite: true`）写入 `us stock bot/logs/report_market.txt`。
2. 按 `us stock bot/prompt_news.md` 写出《美股要闻》（仅选 4–5 星高价值要闻，日期明确、严禁相对星期词）并 `write_file`（`Overwrite: true`）写入 `us stock bot/logs/report_news.txt`。

### 步骤 3 — 发送与核对
```bash
bash "us stock bot/claude_report.sh" send
```
执行 `tail -1 "us stock bot/logs/run.log"` 确认 `[OK]`，随即**立即进入第二棒**（此时 `us stock bot/logs/report_market.txt` 与 `report_news.txt` 已落盘就绪，供 A 股开盘前瞻直接承接）。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
## 第二棒（紧接第一棒，09:30 前完成）：🇨🇳 A股开盘前瞻（`a-share-bot`）

### 步骤 1 — 抓取（自动承接第一棒美股日报 + 前日 A 股收盘 + 盘后至盘前电报）
执行命令（遇沙盒拦截网络时使用 `BypassSandbox: true`）：
```bash
bash "a-share-bot/claude_report.sh" fetch premarket
```
按 stdout 标记决定：
- `=== SKIP_ALREADY_RAN ===`：今日盘前已推送 → 记「已跳过」，进入汇报。
- `=== SKIP_NON_TRADING_DAY ===`：周六/非交易日 → 记「周末非交易日，已跳过」，进入汇报。
- `=== SKIP_EXPIRED ===`：已超过 09:45 开盘前瞻有效窗口 → 记「已过盘前窗口，自动跳过」，进入汇报。
- `=== SKIP_NO_DATA ===`：行情抓取失败 → 记「数据缺失未推送」，进入汇报。
- `=== FETCH_PREMARKET_OK ===`：并行用 `read_file` 读取 `a-share-bot/logs/last_context.txt` 与 `a-share-bot/prompt_premarket.md`，进入步骤 2。

### 步骤 2 — 写稿（严格遵循 `a-share-bot/prompt_premarket.md`）
1. 严格对齐美股与加密日报的顶部指标卡 + 量化锚点 + 三维看点 + 早盘决策仪表盘结构，融合：
   - **前一日 A 股收盘复盘**（归档 + State Store 记忆 + 220 日 MA50/MA200/RSI/布林 %B）；
   - **昨日 15:00 收盘后至今日开盘前的国内宏观政策与行业电报**；
   - **今早刚发出的《美股收盘 & 加密行情日报》核心数据与海外映射**（跨资产数字 100% 一致）。
2. 使用 `write_file`（`Overwrite: true`）将纯正文写入 `a-share-bot/logs/report_cn.txt`。

### 步骤 3 — 发送与核对
```bash
bash "a-share-bot/claude_report.sh" send premarket
```
执行 `tail -1 "a-share-bot/logs/run.log"` 确认 `[OK]`。

━━━━━━━━━━━━━━━
## 汇报格式

```text
早间行情串行流水线（09:00 美股 & 加密 → 09:30 前 A 股开盘前瞻）
  · 1. 美股收盘 & 加密行情日报：{状态}
  · 2. 🇨🇳 A股开盘前瞻：{状态}
```

若出现 `SOURCE_ALERT` 或加密恐贪指数双源失败，在状态后单列一句提示；无异常则不提。
