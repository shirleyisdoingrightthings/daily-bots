import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "shared"))
sys.path.insert(0, str(REPO_ROOT / "AI Daily News Bot"))
sys.path.insert(0, str(REPO_ROOT / "ai-builder-bot"))


def _load_mod(name: str, file_path: Path):
    spec = importlib.util.spec_from_file_location(name, str(file_path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


import bot_utils as bu
import daily_report as dr
import builder_report as br

x_sources = _load_mod("x_hotspot_sources_test", REPO_ROOT / "x-hotspot-bot" / "sources.py")
sys.modules["sources"] = x_sources
xr = _load_mod("x_hotspot_report_test", REPO_ROOT / "x-hotspot-bot" / "x_report.py")
sys.modules.pop("sources", None)


class TestBotUtilsCoreUpgrades(unittest.TestCase):
    def test_redact_secrets_masks_feishu_webhook_token(self):
        raw = "ConnectionError: Max retries exceeded with url: /open-apis/bot/v2/hook/00000000-fake-test-token-1234 (Caused by...)"
        redacted = bu.redact_secrets(raw)
        self.assertIn("/open-apis/bot/v2/hook/***", redacted)
        self.assertNotIn("fake-test-token", redacted)

    def test_url_key_normalization_across_x_hn_reddit_http(self):
        self.assertEqual(
            bu.url_key("https://twitter.com/RealAstropulse/status/2126119408997466389?s=20"),
            bu.url_key("http://www.x.com/realastropulse/status/2126119408997466389/"),
        )
        self.assertEqual(
            bu.url_key("https://news.ycombinator.com/item?id=50663089&p=2"),
            "https://news.ycombinator.com/item?id=50663089",
        )
        self.assertNotEqual(
            bu.url_key("https://news.ycombinator.com/item?id=111"),
            bu.url_key("https://news.ycombinator.com/item?id=222"),
        )
        self.assertEqual(
            bu.url_key("https://old.reddit.com/r/LocalLLaMA/comments/abc123/long_slug/?rdt=1"),
            bu.url_key("https://www.reddit.com/r/localllama/comments/abc123/"),
        )
        self.assertEqual(
            bu.url_key("http://huggingface.co/llamaindex/OCRia-1/"),
            bu.url_key("https://huggingface.co/llamaindex/OCRia-1?utm_source=x"),
        )

    def test_decode_http_bytes_prevents_utf8_mojibake(self):
        utf8_html = "<p>There’s a 《声生不息》 UTF-8 test.</p>".encode("utf-8")
        decoded = bu._decode_http_bytes(utf8_html, content_type="text/html", resp_encoding="ISO-8859-1")
        self.assertIn("There’s", decoded)
        self.assertIn("《声生不息》", decoded)

    def test_html_to_lark_md_preserves_bold_on_unnumbered_link_and_sections(self):
        lines = bu.html_to_lark_md(
            '<b><a href="https://example.com">Headline</a></b>\n\n<b>🗣️ Reddit 实测与吐槽</b>\n内容'
        )
        self.assertEqual(lines[0], "**[Headline](https://example.com)**")
        self.assertIn("**🗣️ Reddit 实测与吐槽**", lines)

    def test_extract_report_headlines_keeps_long_titles_and_reddit(self):
        long_title = "OpenAI 披露内部 Astra 代理越权访问研究基础设施，已移除共享软件库 Artifactory 并改用离线快照评测机制（超长标题测试）"
        draft = (
            "<b>📋 AI 产业日报 · 2026-11-28</b>\n\n"
            f'<a href="https://openai.com/x"><b>{long_title}</b></a>\n\n'
            "<b>[r/LocalLLaMA] 16 台 GB10 跑满血 Kimi-K3.1-Pro 实测</b>\n"
        )
        heads = bu.extract_report_headlines(draft)
        self.assertTrue(any("Astra" in h for h in heads))
        self.assertTrue(any("[r/LocalLLaMA]" in h for h in heads))

    def test_validate_draft_freshness_rejects_stale_draft_and_wrong_title(self):
        with tempfile.TemporaryDirectory() as tmp:
            tdir = Path(tmp)
            draft = tdir / "report_draft.txt"
            meta = tdir / "fetch_meta.json"
            today = datetime.now().strftime("%Y-%m-%d")
            draft.write_text(f'<b>📋 AI 产业日报 · {today}</b>\n<a href="https://a.com">x</a>', encoding="utf-8")
            meta.write_text(json.dumps({"fetch_date": today}), encoding="utf-8")
            # Make draft older than fetch_meta by 10 seconds
            old_t = time.time() - 20
            new_t = time.time()
            os.utime(draft, (old_t, old_t))
            os.utime(meta, (new_t, new_t))
            with patch.dict(os.environ, {"FORCE_RUN": ""}):
                ok, reason = bu.validate_draft_freshness_and_identity(
                    draft, draft.read_text(encoding="utf-8"),
                    fetch_meta_path=meta, webhook="https://open.feishu.cn/open-apis/bot/v2/hook/test",
                    expected_title_markers=("AI 产业日报",),
                )
                self.assertFalse(ok)
                self.assertIn("旧稿", reason)

                # Touch draft newer than meta, but wrong title
                os.utime(draft, (new_t + 5, new_t + 5))
                ok2, reason2 = bu.validate_draft_freshness_and_identity(
                    draft, f'<b>🛠 AI Builder · {today}</b>\n<a href="https://a.com">x</a>',
                    fetch_meta_path=meta, webhook="https://open.feishu.cn/open-apis/bot/v2/hook/test",
                    expected_title_markers=("AI 产业日报",),
                )
                self.assertFalse(ok2)
                self.assertIn("身份标识", reason2)

    def test_recent_self_headlines_and_atomic_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger_p = Path(tmp) / "ledger.json"
            yest = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
            today = datetime.now().strftime("%Y-%m-%d")
            bu.record_daily_ledger(
                "🤖 AI 产业日报",
                f'<b>📋 AI 产业日报 · {yest}</b>\n<a href="https://a.com"><b>SpaceX 获批百万卫星</b></a>',
                date_str=yest,
                ledger_path=ledger_p,
            )
            bu.record_daily_ledger(
                "🐙 GitHub & HN",
                f'<b>🐙 GitHub · {today}</b>\n<b>1.</b> <a href="https://github.com/a/b"><b>a/b</b></a>',
                date_str=today,
                ledger_path=ledger_p,
            )
            recent = bu.format_recent_self_headlines("🤖 AI 产业日报", days=3, date_str=today, ledger_path=ledger_p)
            self.assertIn("SpaceX 获批百万卫星", recent)
            # exclude_bots prevents GitHub & HN from polluting Builder
            led = bu.load_daily_ledger(date_str=today, exclude_bots=("🛠️ AI Builder", "🐙 GitHub & HN"), ledger_path=ledger_p)
            self.assertEqual(led["entries"], [])


class TestAIDailyAndXHotspotUpgrades(unittest.TestCase):
    def test_strip_tldr_read_time_and_clean_link(self):
        self.assertEqual(
            dr._strip_tldr_read_time("Using peers' work to gain access (13 minute read)"),
            "Using peers' work to gain access",
        )
        self.assertEqual(
            dr._strip_tldr_read_time("Matrix mult exponent (6 hour read)"),
            "Matrix mult exponent",
        )
        self.assertEqual(
            dr._clean_tldr_link("https://news.ycombinator.com/item?id=12345&amp;utm_source=tldr"),
            "https://news.ycombinator.com/item?id=12345",
        )

    def test_x_sources_engagement_and_no_split_anchor_double_count(self):
        html_sec = """
        <p>Check <a href="https://x.com/_arohan_/status/2126389028975165465">@</a><a href="https://x.com/_arohan_/status/2126389028975165465">arohan</a>
        and <a href="https://x.com/jakozloski/status/2126281598517576049">@jakozloski</a>.</p>
        <h3>Top tweets (by engagement)</h3>
        <p><a href="https://x.com/jakozloski/status/2126281598517576049">@jakozloski: audit</a> — 26.3k</p>
        """
        ranked = x_sources._rank_posts(html_sec, max_posts=10, ctx_chars=120)
        by_handle = {p["handle"]: p for p in ranked}
        self.assertEqual(by_handle["_arohan_"]["mentions"], 1)
        self.assertEqual(by_handle["jakozloski"]["engagement"], "26.3k")
        self.assertEqual(ranked[0]["handle"], "jakozloski")

    def test_x_report_zero_streak_runs_before_no_topics_return(self):
        with tempfile.TemporaryDirectory() as tmp:
            tdir = Path(tmp)
            zs = tdir / ".zero_streak.json"
            zs.write_text(json.dumps({"ainews": 2}), encoding="utf-8")
            logs = []
            with patch.object(xr, "already_ran_today", return_value=False), \
                 patch.object(xr, "_proxy_ok", return_value=True), \
                 patch.object(xr, "gather", return_value=("", {"ainews_fresh": 0}, ["ainews"])), \
                 patch.object(xr, "ZERO_STREAK", zs), \
                 patch.object(xr, "LAST_CONTEXT", tdir / "last_context.txt"), \
                 patch.object(xr, "write_log", lambda s, m, **kw: logs.append((s, m))):
                rc = xr.run_fetch()
                self.assertEqual(rc, 0)
                ctx = (tdir / "last_context.txt").read_text(encoding="utf-8")
                self.assertIn("=== SOURCE_ALERT ===", ctx)
                self.assertIn("ainews 已连续 3 天零产", ctx)


class TestAIBuilderHNStyleUpgrades(unittest.TestCase):
    def test_discussion_heat_prioritizes_high_like_high_reply_and_penalizes_icymi(self):
        rauchg = {"likes": 3509, "replies": 186, "retweets": 258, "quotes": 40, "has_article": False, "is_repeat_ext": False, "is_icymi": False}
        icymi_cold = {"likes": 119, "replies": 4, "retweets": 12, "quotes": 1, "has_article": False, "is_repeat_ext": True, "is_icymi": True}
        active_debate = {"likes": 300, "replies": 80, "retweets": 20, "quotes": 15, "has_article": False, "is_repeat_ext": False, "is_icymi": False}
        self.assertGreater(br._calc_discussion_heat(rauchg), br._calc_discussion_heat(active_debate))
        self.assertGreater(br._calc_discussion_heat(active_debate), br._calc_discussion_heat(icymi_cold) * 10)

    def test_fetch_tweet_top_replies_filters_root_ads_and_caps_author_self_replies(self):
        fake_payload = {
            "tweets": [
                {"id": "100", "isReply": False, "conversationId": "100", "author": {"userName": "trq212", "name": "Thariq"}, "text": "root tweet", "likeCount": 5000, "replyCount": 270},
                {"id": "999", "isReply": False, "conversationId": "999", "author": {"userName": "Supermicro", "name": "Supermicro"}, "text": "Introducing Supermicro's promoted ad with 11k likes!", "likeCount": 11071, "replyCount": 908},
                {"id": "101", "isReply": True, "conversationId": "100", "inReplyToId": "100", "author": {"userName": "spam", "name": "Spam"}, "text": "@trq212 wow!", "likeCount": 500, "replyCount": 0},
                {"id": "102", "isReply": True, "conversationId": "100", "inReplyToId": "100", "author": {"userName": "trq212", "name": "Thariq"}, "text": "Author self reply #1 comparing Claude vs Codex on creativity and alignment.", "likeCount": 512, "replyCount": 63},
                {"id": "103", "isReply": True, "conversationId": "100", "inReplyToId": "100", "author": {"userName": "trq212", "name": "Thariq"}, "text": "Author self reply #2 this is also a call for better multidimensional evals.", "likeCount": 273, "replyCount": 17},
                {"id": "104", "isReply": True, "conversationId": "100", "inReplyToId": "100", "author": {"userName": "trq212", "name": "Thariq"}, "text": "Author self reply #3 pokemon joke garchomp plus tyranitar abuser.", "likeCount": 35, "replyCount": 3},
                {"id": "105", "isReply": True, "conversationId": "100", "inReplyToId": "100", "author": {"userName": "ext_builder1", "name": "Builder One"}, "text": "@trq212 Real external critique on RPG stat sheets needing task-calibrated weights.", "likeCount": 20, "replyCount": 2},
                {"id": "106", "isReply": True, "conversationId": "100", "inReplyToId": "100", "author": {"userName": "ext_builder2", "name": "Builder Two"}, "text": "@trq212 Another external perspective about latency vs accuracy tradeoffs in agents.", "likeCount": 15, "replyCount": 1},
            ]
        }
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = fake_payload
        with patch.object(br, "_twitterapi_get", return_value=mock_resp):
            reps = br._fetch_tweet_top_replies("100", "trq212", "fake-key", max_replies=3)
        self.assertEqual(len(reps), 3)
        self.assertEqual(sum(1 for r in reps if r["is_author"]), 1)
        self.assertEqual(sum(1 for r in reps if not r["is_author"]), 2)
        self.assertFalse(any(r["handle"] == "Supermicro" for r in reps))

    def test_notion_preserves_all_hn_style_builder_linked_headings(self):
        import notion_archive as na
        draft = (
            "<b>🔥 X 热点 & AI Builder · 2026-11-28</b>\n\n"
            "这批讨论来自 2026-11-27 12:42 ~ 11-28 07:41 UTC（北京时间 11月27日 20:42），聚焦沙盒与蒸馏。\n\n"
            '<b>1.</b> <a href="https://x.com/ClementDelangue/status/2126322165364719677"><b>Hugging Face CEO 力挺蒸馏：防能力与权力极端集中</b></a>\n'
            "👤 Clem（@ClementDelangue）｜🔥 5775 赞 · 💬 228 评\n"
            "· <b>这帖在聊啥</b>：内容一。\n\n"
            '<b>2.</b> <a href="https://x.com/OfficialLoganK/status/2126434025292144821"><b>不用装新 App：短信交互把 Agent 上手压到分钟级</b></a>\n'
            "👤 Logan（@OfficialLoganK）｜🔥 2122 赞 · 💬 236 评\n"
            "· <b>这帖在聊啥</b>：内容二。\n\n"
            "<b>⚡ 短讯雷达</b>\n"
            '- <a href="https://x.com/swyx/status/2126352854394790172"><b>@swyx</b></a>：短讯。\n'
        )
        blocks = na.convert_to_blocks(draft, source="🛠️ AI Builder")
        h2s = ["".join(rt["text"]["content"] for rt in b["heading_2"]["rich_text"]) for b in blocks if b["type"] == "heading_2"]
        self.assertEqual(len(h2s), 3)
        self.assertTrue(h2s[0].startswith("1. Hugging Face"))
        self.assertTrue(h2s[1].startswith("2. 不用装新 App"))
        self.assertEqual(h2s[2], "⚡ 短讯雷达")
        self.assertEqual(na.build_clean_title("🛠️ AI Builder", "2026-11-28"), "2026-11-28 X 热点 & AI Builder")
        summary = na.extract_summary(draft, source="🛠️ AI Builder")
        self.assertNotIn("2026-11-27 12:42", summary)
        self.assertIn("聚焦沙盒与蒸馏", summary)


if __name__ == "__main__":
    unittest.main()
