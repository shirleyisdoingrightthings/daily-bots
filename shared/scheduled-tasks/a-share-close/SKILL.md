---
name: a-share-close
description: 每周一至周五 16:15 抓取并推送《🇨🇳 A股收盘复盘》（仅 A 股，已移除港股），同时沉淀次日盘前记忆底座
---

目标：每周一至周五 **16:15**（A 股收盘后），抓取全天收盘行情、全市场真实宽度与涨跌停情绪、大小盘/成长红利风格差、255 日量化技术指标（MA20/50/200、HV20、Z_vol、CLV、RVOL、Wilder RSI(14)、布林 %B）、股债/期指/宽基 ETF 代理、16 大高纯度机构主线及 Exa 权威催化，写出《🇨🇳 A股收盘复盘》推送至 A 股飞书机器人并同步 Notion，同时更新 `logs/market_memory.json` 与 `logs/archive/YYYY-MM-DD_close.txt` 供次日盘前联动。

> **两条执行铁律**：
> - `fetch` 后使用 `read_file` 读取 `a-share-bot/logs/last_context.txt` 与 `a-share-bot/prompt_close.md`，严禁重复 `fetch`。
> - 写稿落盘一律使用 `write_file`（`Overwrite: true`）写入 `a-share-bot/logs/report_cn_close.txt`，严禁 `cat <<EOF`。

### 步骤 1 — 抓取
执行命令（遇沙盒拦截网络时使用 `BypassSandbox: true`）：
```bash
bash "a-share-bot/claude_report.sh" fetch close
```
按 stdout 标记决定：
- `=== SKIP_ALREADY_RAN ===` / `=== SKIP_NON_TRADING_DAY ===` / `=== SKIP_EXPIRED ===` / `=== SKIP_NO_DATA ===`：立即停止并如实汇报。
- `=== FETCH_CLOSE_OK ===`：并行读取 `a-share-bot/logs/last_context.txt` 与 `a-share-bot/prompt_close.md`。

### 步骤 2 — 写稿
严格遵循 `a-share-bot/prompt_close.md` 写出《🇨🇳 A股收盘复盘》（结论前置、无催化主线 1 行紧凑带过、领跌主线严禁混入收涨股、波动率校准净敞口与 T+1 触发器），用 `write_file`（`Overwrite: true`）写入 `a-share-bot/logs/report_cn_close.txt`。

### 步骤 3 — 发送与核对
```bash
bash "a-share-bot/claude_report.sh" send close
```
执行 `tail -1 "a-share-bot/logs/run.log"` 核对 `[OK]` 并简报结果。
