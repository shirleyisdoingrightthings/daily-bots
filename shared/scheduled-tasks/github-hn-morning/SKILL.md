---
name: github-hn-morning
description: （已并入 09:00 `ai-daily` 四合一串行流水线第 4 棒）仅在需要手动单独补跑 GitHub Trending & Hacker News 早报时使用
---

> **⚠️ 调度归并说明**：本任务已于 2026-11-28 正式并入每日 **09:00 `ai-daily` 四合一串行流水线**（`1. AI 产业日报 → 2. X & Reddit → 3. AI Builder → 4. GitHub & HN`），彻底消除休眠唤醒导致的跨 Bot 账本竞态。日常定时执行请见 `shared/scheduled-tasks/ai-daily/SKILL.md`。

以下流程仅用于**手动单独补跑** `github-hn-bot`：

1. 抓取：`bash "github-hn-bot/claude_report.sh" fetch`（遇沙盒拦截网络/代理时用 `BypassSandbox: true`），之后用 `read_file` 读取 `github-hn-bot/logs/last_context.txt`。
2. 写稿：
   - `=== FETCH_OK ===`（周一至周六日榜）读取 `github-hn-bot/prompt.md`（GitHub 新星完整展开 + `🔁 持续霸榜熟面孔` 紧凑折叠；HN Top 8 避开前 3 棒已播议题）；
   - `=== WEEKLY_OK ===`（周日周榜）读取 `github-hn-bot/prompt_weekly.md`（含 `🔥 持续霸榜` 无序列表折叠块）；
   - 使用 `write_file`（`Overwrite: true`）写入 `github-hn-bot/logs/report_draft.txt`，严禁 `cat <<EOF`。
3. 发送与核对：`bash "github-hn-bot/claude_report.sh" send` 并检查 `tail -1 "github-hn-bot/logs/run.log"`。
