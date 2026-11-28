import importlib.util
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "shared"))


def _load_mod(name: str, file_path: Path):
    spec = importlib.util.spec_from_file_location(name, str(file_path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


import notion_archive as na

gh_sources = _load_mod("github_hn_sources_test", REPO_ROOT / "github-hn-bot" / "sources.py")
sys.modules["sources"] = gh_sources
ghr = _load_mod("github_hn_report_test", REPO_ROOT / "github-hn-bot" / "github_hn_report.py")
sys.modules.pop("sources", None)


SAMPLE_TRENDING_HTML = """
<article class="Box-row">
  <h2 class="h3 lh-condensed"><a href="/sponsors/some-promo">Sponsor</a></h2>
</article>
<article class="Box-row">
  <h2 class="h3 lh-condensed"><a href="/obra/superpowers">obra / superpowers</a></h2>
  <p class="col-9 color-fg-muted">Old recurring repo</p>
  <span itemprop="programmingLanguage">Shell</span>
  <a href="/obra/superpowers/stargazers">328,443</a>
  <a href="/obra/superpowers/forks">12,000</a>
  <span class="d-inline-block float-sm-right">922 stars today</span>
</article>
<article class="Box-row">
  <h2 class="h3 lh-condensed"><a href="/acme/fresh-agent">acme / fresh-agent</a></h2>
  <p class="col-9 color-fg-muted">Brand new AI tool</p>
  <span itemprop="programmingLanguage">Python</span>
  <a href="/acme/fresh-agent/stargazers">4,200</a>
  <a href="/acme/fresh-agent/forks">300</a>
  <span class="d-inline-block float-sm-right">810 stars today</span>
</article>
"""


class TestGitHubHNSourcesAndReport(unittest.TestCase):
    def test_parse_trending_skips_sponsor_and_keeps_continuous_rank(self):
        seen = {"https://github.com/obra/superpowers": "11月20日"}
        parsed = gh_sources._parse_trending_html(SAMPLE_TRENDING_HTML, seen)
        self.assertEqual(len(parsed), 2)
        self.assertEqual(parsed[0]["rank"], 1)
        self.assertEqual(parsed[0]["repo"], "obra/superpowers")
        self.assertEqual(parsed[0]["prev_date"], "11月20日")
        self.assertEqual(parsed[1]["rank"], 2)
        self.assertEqual(parsed[1]["repo"], "acme/fresh-agent")
        self.assertIsNone(parsed[1]["prev_date"])

    def test_clean_readme_strips_all_html_and_entities(self):
        raw = """
        <!-- comment -->
        <picture><source media="(prefers-color-scheme: dark)" srcset="x.png"><img src="y.png"></picture>
        <table><tr><td align="center"><strong>Fast&nbsp;Agent</strong></td></tr></table>
        <details><summary>Details</summary>Runs local agents cleanly &amp; reliably.</details>
        """
        cleaned = gh_sources._clean_readme(raw)
        self.assertNotIn("<source", cleaned)
        self.assertNotIn("<td", cleaned)
        self.assertNotIn("&nbsp;", cleaned)
        self.assertIn("Fast Agent", cleaned)
        self.assertIn("Runs local agents cleanly & reliably.", cleaned)

    def test_hn_dedups_against_x_status_ids_and_duplicate_ext_urls(self):
        fake_algolia = {
            "hits": [
                {
                    "objectID": "5001",
                    "title": "Claude sabotaging ML?",
                    "url": "https://twitter.com/RealAstropulse/status/2126119408997466389",
                    "points": 400,
                    "num_comments": 200,
                    "author": "alice",
                },
                {
                    "objectID": "5002",
                    "title": "Duplicate URL submission",
                    "url": "https://example.com/post?utm_source=hn",
                    "points": 300,
                    "num_comments": 50,
                    "author": "bob",
                },
                {
                    "objectID": "5003",
                    "title": "Duplicate URL submission lower",
                    "url": "https://www.example.com/post/",
                    "points": 120,
                    "num_comments": 10,
                    "author": "carol",
                },
            ]
        }
        mock_resp = MagicMock(status_code=200)
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.side_effect = [[], [], fake_algolia, {"hits": []}]
        with patch.object(gh_sources._HTTP_SESSION, "get", return_value=mock_resp), \
             patch.object(gh_sources, "_fetch_hn_item_details", return_value=None):
            res = gh_sources.fetch_hn_top8(
                limit=8,
                today_ledger_x_ids={"2126119408997466389"},
            )
        items = res["items"]
        self.assertEqual(len(items), 2)  # 5003 deduped by ext_url
        by_id = {it["id"]: it for it in items}
        self.assertTrue(by_id["5001"]["in_today_ledger"])
        self.assertFalse(by_id["5002"]["in_today_ledger"])

    def test_run_send_only_records_published_urls_and_logs_fail_on_feishu_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            tdir = Path(tmp)
            today = datetime.now().strftime("%Y-%m-%d")
            draft = tdir / "report_draft.txt"
            meta = tdir / "fetch_meta.json"
            seen_p = tdir / "seen_history.json"
            log_p = tdir / "run.log"
            draft.write_text(
                f'<b>🐙 GitHub Trending & Hacker News 早报 · {today}</b>\n'
                '<b>1.</b> <a href="https://example.com/published"><b>已发条目</b></a>\n'
                'Original: Pub（💬 <a href="https://news.ycombinator.com/item?id=101">50 条评论</a>）',
                encoding="utf-8",
            )
            meta.write_text(
                json.dumps({
                    "date_str": today,
                    "fetch_date": today,
                    "log_summary": "test",
                    "metrics": {
                        "since": "daily",
                        "hn_url_pairs": {
                            "https://example.com/published": ["https://news.ycombinator.com/item?id=101", "https://example.com/published"],
                            "https://example.com/unpublished-9": ["https://news.ycombinator.com/item?id=109", "https://example.com/unpublished-9"],
                        },
                    },
                }),
                encoding="utf-8",
            )
            logs = []
            with patch.object(ghr, "DRAFT_FILE", draft), \
                 patch.object(ghr, "FETCH_META", meta), \
                 patch.object(ghr, "SEEN_HISTORY", seen_p), \
                 patch.object(ghr, "LOG_FILE", log_p), \
                 patch.object(ghr, "CACHE_FILE", tdir / "pending.json"), \
                 patch.object(ghr, "ARCHIVE_DIR", tdir / "archive"), \
                 patch.object(ghr, "FEISHU_WEBHOOK", "https://open.feishu.cn/open-apis/bot/v2/hook/test"), \
                 patch.object(ghr, "already_ran_today", return_value=False), \
                 patch.object(ghr, "write_log", lambda s, m, **kw: logs.append((s, m))), \
                 patch.object(ghr, "save_pending", lambda *a: None), \
                 patch.object(ghr, "archive_and_sync_notion", return_value={"notion_ok": True}), \
                 patch.object(ghr, "record_daily_ledger", lambda *a, **kw: None), \
                 patch.dict(os.environ, {"FORCE_RUN": "1"}):
                # 1. Feishu failure writes [FAIL] and returns 1
                with patch.object(ghr, "send_feishu", side_effect=RuntimeError("timeout /open-apis/bot/v2/hook/fake-tok")):
                    rc_fail = ghr.run_send("daily")
                self.assertEqual(rc_fail, 1)
                self.assertEqual(logs[-1][0], "FAIL")
                self.assertNotIn("fake-tok", logs[-1][1])

                # 2. Feishu success records ONLY published #101, NOT unpublished #109
                with patch.object(ghr, "send_feishu", return_value=1):
                    rc_ok = ghr.run_send("daily")
                self.assertEqual(rc_ok, 0)
                seen_data = json.loads(seen_p.read_text(encoding="utf-8"))
                self.assertIn("https://news.ycombinator.com/item?id=101", seen_data)
                self.assertIn("https://example.com/published", seen_data)
                self.assertNotIn("https://news.ycombinator.com/item?id=109", seen_data)
                self.assertNotIn("https://example.com/unpublished-9", seen_data)

    def test_notion_weekly_toggle_counts_bullet_items_and_avoids_long_h3(self):
        weekly_txt = (
            "<b>🐙 GitHub Weekly 周榜 & Hacker News 早报 · 2026-11-22</b>\n\n"
            "<b>🔥 持续霸榜（本周反复上榜常客）</b>\n"
            '· <a href="https://github.com/obra/superpowers"><b>obra/superpowers</b></a>（Shell）—— 技能库。<b>（见 11月15日 推送）</b>\n'
            '· <a href="https://github.com/firecrawl/firecrawl"><b>firecrawl/firecrawl</b></a>（TS）—— 网页清洗。<b>（见 11月15日 推送）</b>\n'
        )
        blocks = na.convert_to_blocks(weekly_txt, source="🐙 GitHub & HN")
        toggles = [b for b in blocks if b["type"] == "toggle"]
        self.assertEqual(len(toggles), 1)
        title = "".join(rt["text"]["content"] for rt in toggles[0]["toggle"]["rich_text"])
        self.assertIn("共 2 项", title)
        self.assertEqual(len(toggles[0]["toggle"]["children"]), 2)
        self.assertTrue(all(c["type"] == "bulleted_list_item" for c in toggles[0]["toggle"]["children"]))


if __name__ == "__main__":
    unittest.main()
