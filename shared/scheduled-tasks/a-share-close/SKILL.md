---
name: a-share-close
description: 每周一至周五 16:15 抓取并推送《🇨🇳 A股收盘复盘》（仅 A 股，已移除港股），同时沉淀次日盘前记忆底座
---

目标：每周一至周五 **16:15**（A 股收盘后），抓取全天收盘行情、市场宽度、220 日量化技术指标（MA5/20/50/200、Wilder RSI(14)、布林 %B）、全市场与 12 大核心主线领涨/领跌 Top 3 板块与个股及 7x24 电报，写出《🇨🇳 A股收盘复盘》推送至 A 股飞书机器人并同步 Notion，同时更新 `logs/market_memory.json` 与 `logs/archive/YYYY-MM-DD_close.txt` 供次日 09:00《A股开盘前瞻》联动。

> **两条执行铁律**：
> - `fetch` 后使用 `read_file` 读取 `a-share-bot/logs/last_context.txt` 与 `a-share-bot/prompt_close.md`，严禁重复 `fetch`。
> - 写稿落盘一律使用 `write_file`（`Overwrite: true`）写入 `a-share-bot/logs/report_cn.txt`，严禁 `cat <<EOF`。

### 步骤 1 — 抓取
执行命令（遇沙盒拦截网络时使用 `BypassSandbox: true`）：
```bash
bash "a-share-bot/claude_report.sh" fetch close
```
按 stdout 标记决定：
- `=== SKIP_ALREADY_RAN ===` / `=== SKIP_NON_TRADING_DAY ===` / `=== SKIP_NO_DATA ===`：立即停止并如实汇报。
- `=== FETCH_CLOSE_OK ===`：并行读取 `a-share-bot/logs/last_context.txt` 与 `a-share-bot/prompt_close.md`。

### 步骤 2 — 写稿
严格遵循 `a-share-bot/prompt_close.md` 写出《🇨🇳 A股收盘复盘》（领跌板块严禁混入收涨股、无独立催化明确写「未见个股独立消息催化」、策略师观点给明确支撑/阻力锚点），用 `write_file`（`Overwrite: true`）写入 `a-share-bot/logs/report_cn.txt`。

### 步骤 3 — 发送与核对
```bash
bash "a-share-bot/claude_report.sh" send close
```
执行 `tail -1 "a-share-bot/logs/run.log"` 核对 `[OK]` 并简报结果。
