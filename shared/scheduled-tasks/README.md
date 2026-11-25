# scheduled-tasks — 自动化定时任务 `SKILL.md` 编排库

所有 Bot 的**统一 AI 写稿编排入口**均定义在本目录下，供 Antigravity Scheduled Automations（或其他兼容 `SKILL.md` 的 Agent 调度器）直接引用。

## 调度总览（Asia/Shanghai）

| `SKILL.md` 路径 | Task ID | 调度 | 作用 |
| :--- | :--- | :--- | :--- |
| [`ai-daily-afternoon/SKILL.md`](./ai-daily-afternoon/SKILL.md) | `ai-daily-afternoon` | 每天 09:00（`0 9 * * *`） | AI 三合一串行流水线：`1. AI 产业日报 → 2. X 热点播报 → 3. AI Builder 日报`（共享当日账本 `daily_topic_ledger.json` 跨 Bot 去重） |
| [`cn-hk-stock-bot/SKILL.md`](./cn-hk-stock-bot/SKILL.md) | `cn-hk-premarket` / `cn-hk-midday` / `cn-hk-close` | 工作日 09:00 / 12:05 / 16:15 | A股 & 港股开盘前瞻、午间缺早补午与收盘复盘 |
| [`github-hn-morning/SKILL.md`](./github-hn-morning/SKILL.md) | `github-hn-morning` | 每天 09:30（`30 9 * * *`） | GitHub Trending（周一至周六日榜 / 周日周榜）+ Hacker News Top 8（读取 09:00 账本去重） |
| [`morning-catchup-daily-bots/SKILL.md`](./morning-catchup-daily-bots/SKILL.md) | `morning-catchup-daily-bots` | 周二至周六 10:00（`0 10 * * 2-6`） | 美股收盘 & 24h 加密合盘日报 |

> 注：各 bot 目录下的命令行包装脚本保留 `claude_report.sh` 文件名以兼容既有自动化路径，内部统一 `source ../shared/report_base.sh`。手动强制补发当天已有草稿时，可执行：`FORCE_RUN=1 bash "<bot>/claude_report.sh" send`。

## 本机非仓库依赖（换机部署清单）

1. **Agent 调度配置**：将调度器的工作区根目录指向本仓库根目录，并指向对应 `SKILL.md`。
2. **密钥与环境变量**：推荐使用仓库根目录 `.env`（复制自 [`.env.example`](../../.env.example)）；在 macOS 上亦可通过 LaunchAgent plist 存放环境变量（模板见 [`docs/launchd-env.plist.example`](../../docs/launchd-env.plist.example)，并在本地 `shared/.env` 中配置 `BOT_PLIST_PREFIX`）。
3. **轻量化健康检查（可选，仅 Level 1 新鲜草稿重发）**：
   - 每日 10:00：`shared/ai_chain_health.sh`（串行覆盖 `AI Daily News Bot` → `x-hotspot-bot` → `ai-builder-bot`）
   - 每日 10:30：`github-hn-bot/health_check.sh`
   - 每日 11:00：`us stock bot/health_check.sh`
   - 工作日 17:15：`cn-hk-stock-bot/health_check.sh`
4. **运行时状态文件（已 `.gitignore`）**：各 bot `logs/` 下的 `sent_urls.json` / `seen_history.json`（跨天去重）、`market_memory.json`（5 日盘面记忆）与 `.zero_streak.json`（源连续零产监控）。
