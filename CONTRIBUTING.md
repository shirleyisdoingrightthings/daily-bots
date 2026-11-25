# Contributing to Daily Bots

Thank you for your interest in contributing to **Daily Bots**!

## Architecture Rules

1. **Fetch / Draft / Send Decoupling**: Python scripts (`*_report.py`) handle deterministic data fetching, deduplication, sanitization, Feishu delivery, and Notion archiving with **zero hardcoded LLM API calls**. AI drafting is driven cleanly by Markdown prompts (`prompt*.md`) and agent orchestration (`shared/scheduled-tasks/*/SKILL.md`).
2. **Controlled HTML Whitelist**: Drafts must only use `<b>...</b>` and `<a href="URL">...</a>`. All other HTML tags are stripped by `shared/bot_utils.sanitize_html()`, and Markdown syntax (`**bold**`, `[text](url)`) should be avoided so Feishu Card 2.0 renders cleanly.
3. **Reuse `shared/`**: Do not copy-paste HTTP retry, deduplication, Feishu pagination, Notion archiving, or health check logic into individual bots. Extend `shared/bot_utils.py` or `shared/*_base.sh` instead.
4. **Zero Secrets in Git**: Never commit `.env`, `*.plist`, webhook URLs, API keys, Notion Database IDs, or personal file paths. Use environment variables and update `.env.example` when introducing new configuration keys.

## Adding a New Bot

1. Create a new directory `<your-bot>/` with:
   - `<your-bot>_report.py` implementing `--mode fetch` and `--mode send`
   - `prompt.md` defining output rules and formatting
   - `claude_report.sh`, `health_check.sh`, and `auto_repair.sh` thin wrappers delegating to `../shared/*_base.sh`
   - `README.md` documenting workflow diagram, data sources, CLI usage, and env vars
2. Add a corresponding `SKILL.md` under `shared/scheduled-tasks/` and link your bot in the root `README.md`.

## Verification Before Opening a PR

```bash
# 1. Python syntax check
python3 -m py_compile shared/*.py */*.py

# 2. Shell syntax check
for f in shared/*.sh */*.sh; do bash -n "$f"; done

# 3. Run full offline test suite
python3 -m unittest discover -s shared/tests -v
```
