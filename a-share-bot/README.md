# 🇨🇳 A-Share Stock Bot (`a-share-bot`)

A 股买方量化与策略决策机器人（已剥离港股），严格按双场次隔离提供：
1. **09:05–09:24 开盘前瞻（`premarket`）**：紧接美股收盘日报在同一串行流水线内于 09:25 集合竞价前完成，自动融合 **前日 A 股收盘复盘归档 + 255 日量化技术锚点**、**上一交易日 15:00 至今日 09:24 净化后国内宏观政策电报**、以及 **实时跨资产先导锚（富时 A50、离岸人民币 USDCNH、伦敦金/布油/BTC 24h）与今早美股映射**（内置美股休市防二次计价硬隔离）。草稿路径：`logs/report_cn_premarket.txt`。
2. **16:15 收盘复盘（`close`）**：工作日 15:00 收盘后抓取全市场真实涨跌家数与自然涨停/跌停/封板率、大小盘/成长红利风格差、255 日量化技术诊断（`MA20/50/200 · HV20 · Z_vol · CLV · RVOL · Wilder RSI(14) · 布林 %B`）、股债/期指基差/四大国家队宽基 ETF 代理、16 大高纯度机构主线与 Exa 权威公告催化核查，并沉淀 `logs/market_memory.json` 与 `logs/archive/YYYY-MM-DD_close.txt` 供次日盘前联动。草稿路径：`logs/report_cn_close.txt`。

## 环境变量（`.env` 或 LaunchAgent plist）
- `FEISHU_WEBHOOK_CN`（必填）：A 股群飞书 Webhook URL（不会静默回退至其他群）
- `EXA_API_KEY` / `ENABLE_EXA_ENRICH=1`（可选）：收盘异动股权威公告与宏观日程补盲
- `NOTION_ARCHIVE_API_KEY` / `NOTION_ARCHIVE_DATABASE_ID`（可选）：同步归档至 Notion 数据库

## 手动调试
```bash
bash "a-share-bot/claude_report.sh" fetch premarket
bash "a-share-bot/claude_report.sh" send premarket
bash "a-share-bot/claude_report.sh" fetch close
bash "a-share-bot/claude_report.sh" send close
```
