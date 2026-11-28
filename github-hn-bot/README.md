# 🐙 GitHub Trending & Hacker News Bot

> [← Back to Daily Bots Root README](../README.md)

**Stage 3 of 3 in the daily `09:00 CST` `ai-daily` sequential pipeline.** Pairs **GitHub Trending** (full-page fresh-first discovery + Python/TypeScript fallback + compact recurring toggle; Sunday Weekly mode with "Sustained Trend" spotlight) with **Hacker News Top 8** (parallel Firebase `top`/`best` + Algolia 36h/frontpage scores + reply-weighted top comments), reading Stages 1–2 `daily_topic_ledger.json` with zero wake-up race and recording only actually published URLs in `seen_history.json`.

---

## Workflow Architecture

```mermaid
flowchart TD
    A["Trigger: Daily 09:30 CST<br/>Mon-Sat: daily | Sun: weekly"] --> B["claude_report.sh fetch [daily|weekly]<br/>github_hn_report.py --mode fetch"]
    B --> C["Parallel Fetch (sources.py)<br/>1. GitHub Trending HTML + concurrent README.md<br/>2. HN Firebase top/best + Algolia 36h (pool up to 12)<br/>+ external article bodies + top 4 comments"]
    C --> D["Cross-Day &amp; Cross-Bot Tagging<br/>1. 30-day seen_history.json (marks 🆕 vs 🔁 MM月DD日)<br/>2. 09:00 shared/logs/daily_topic_ledger.json (marks overlap)"]
    D --> E{"Mode?"}
    E -- "Mon-Sat (daily)" --> F1["Emit === FETCH_OK ===<br/>Draft via prompt.md"]
    E -- "Sunday (weekly)" --> F2["Emit === WEEKLY_OK ===<br/>Draft via prompt_weekly.md"]
    F1 & F2 --> G["Save logs/report_draft.txt"]
    G --> H["claude_report.sh send [daily|weekly]"]
    H --> I["Feishu Card 2.0 + Notion (🐙 GitHub &amp; HN)<br/>Report Type: 日报 (daily) / 周盘点 (weekly)<br/>+ Update 30-day seen_history.json"]
```

---

## Data Sources & Deduplication

1. **GitHub Trending (`sources.py`)**:
   - Scrapes `https://github.com/trending?since=daily` (or `weekly` on Sundays).
   - Concurrently downloads `raw.githubusercontent.com/<owner>/<repo>/HEAD/README.md`, strips badge clutter and HTML noise, and feeds clean architectural summaries to the drafting prompt.
2. **Hacker News (`sources.py`)**:
   - Combines Firebase `topstories` + `beststories` with Algolia 36h / `front_page` signals into a candidate pool of up to 12 stories (top 8 published + 4 backfill candidates).
   - Extracts linked article text via `shared/bot_utils.fetch_article_text` and top 4 high-signal discussion comments.
3. **30-Day Memory & 09:00 Ledger Alignment**:
   - Stores canonical repo URLs and HN `item?id=` keys in `logs/seen_history.json` (30-day rolling window) so recurring items are labeled with their first-seen date (`🔁 见 MM月DD日 推送`) and collapsed to a 1-line recap.
   - Reads `shared/logs/daily_topic_ledger.json` written by the 09:00 AI pipeline so overlapping HN stories are either replaced from the Top 9–12 backfill pool or folded cleanly.

---

## CLI Usage

```bash
# Auto-detect daily (Mon-Sat) vs weekly (Sun)
bash github-hn-bot/claude_report.sh fetch
bash github-hn-bot/claude_report.sh send

# Explicitly override mode
FORCE_RUN=1 bash github-hn-bot/claude_report.sh fetch weekly
FORCE_RUN=1 bash github-hn-bot/claude_report.sh send weekly

# Health check (runs at 10:30)
bash github-hn-bot/health_check.sh
```

---

## Key Files & Environment Variables

- `sources.py` — GitHub Trending scraper, README fetcher, HN Firebase + Algolia aggregator, comment extractor
- `github_hn_report.py` — History tagging, ledger cross-check, Feishu push, and Notion archiving (`日报` / `周盘点`)
- `prompt.md` / `prompt_weekly.md` — Plain-language editorial templates (GitHub: *"What it does"* + *"Personal Build angle"*; HN: *"What it's about"* + *"What comments are debating"*)
- **Env Vars**: `FEISHU_WEBHOOK` (required), `NOTION_ARCHIVE_API_KEY` & `NOTION_ARCHIVE_DATABASE_ID` (optional), `HTTPS_PROXY` (optional).
