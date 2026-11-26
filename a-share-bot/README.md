# 🇨🇳 A-Share Stock Bot (`a-share-bot`)

A 股专属智能决策机器人（已剥离港股），提供两个场次：
1. **09:00–09:30 开盘前瞻（`premarket`）**：紧接 09:00《美股收盘 & 加密行情日报》之后在同一串行流水线内运行，自动承接刚生成的美股日报正稿 + 前一日 A 股收盘复盘归档 + 昨日 15:00 至今日开盘前的宏观政策与行业电报。
2. **16:15 收盘复盘（`close`）**：工作日盘后动态捕捉 A 股全市场及 12 大主线领涨/领跌 Top 3 板块与个股、220 日量化技术指标（MA5/20/50/200、Wilder RSI(14)、布林 %B），并更新 `logs/market_memory.json` 与 `logs/archive/YYYY-MM-DD_close.txt` 供次日盘前联动。

## 手动调试
```bash
bash "a-share-bot/claude_report.sh" fetch premarket
bash "a-share-bot/claude_report.sh" send premarket
bash "a-share-bot/claude_report.sh" fetch close
bash "a-share-bot/claude_report.sh" send close
```
