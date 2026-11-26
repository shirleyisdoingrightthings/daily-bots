"""Unit tests for Exa enrichment helpers in shared/bot_utils.py and stock_report.py."""

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "shared"))
sys.path.insert(0, str(ROOT / "us stock bot"))

import bot_utils
import stock_report


class TestExaHelpers(unittest.TestCase):
    def test_effort_clamps_unsafe_modes(self):
        self.assertEqual(bot_utils._normalize_exa_effort("low"), "low")
        self.assertEqual(bot_utils._normalize_exa_effort("medium"), "medium")
        self.assertEqual(bot_utils._normalize_exa_effort("auto"), "medium")
        self.assertEqual(bot_utils._normalize_exa_effort("ultra"), "medium")

    def test_exa_enrich_enabled_switch(self):
        with patch.dict(os.environ, {"EXA_API_KEY": "test-key", "ENABLE_EXA_ENRICH": "0"}):
            self.assertFalse(bot_utils.exa_enrich_enabled(default_enabled=True))
        with patch.dict(os.environ, {"EXA_API_KEY": "test-key", "ENABLE_EXA_ENRICH": "1"}):
            self.assertTrue(bot_utils.exa_enrich_enabled(default_enabled=False))

    def test_exa_fetch_contents_maps_url_key(self):
        mock_sess = MagicMock()
        mock_sess.proxies = {}
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "results": [
                {
                    "url": "https://www.wsj.com/articles/test-story?mod=rss_markets_main",
                    "text": "A" * 400,
                },
                {
                    "url": "https://www.wsj.com/articles/too-short",
                    "text": "short",
                },
            ]
        }
        mock_sess.request.return_value = mock_resp

        with patch.dict(os.environ, {"EXA_API_KEY": "test-key"}):
            out = bot_utils.exa_fetch_contents(
                ["https://www.wsj.com/articles/test-story?mod=rss_markets_main"],
                max_chars=500,
                session=mock_sess,
            )

        self.assertIn(bot_utils.url_key("https://www.wsj.com/articles/test-story"), out)
        self.assertEqual(len(out), 1)

    def test_exa_agent_filters_only_confirmed(self):
        mock_sess = MagicMock()
        mock_sess.proxies = {}
        create_resp = MagicMock()
        create_resp.status_code = 200
        create_resp.json.return_value = {"id": "agent_run_test123", "status": "queued"}
        poll_resp = MagicMock()
        poll_resp.status_code = 200
        poll_resp.json.return_value = {
            "id": "agent_run_test123",
            "status": "completed",
            "costDollars": {"total": 0.1},
            "output": {
                "structured": {
                    "items": [
                        {
                            "symbol": "PDD",
                            "name": "拼多多",
                            "verdict": "confirmed",
                            "catalyst_type": "earnings",
                            "catalyst_summary": "拼多多发布三季报营收超预期并宣布股票回购计划",
                            "source_title": "Reuters",
                            "source_url": "https://www.reuters.com/example",
                        },
                        {
                            "symbol": "NKE",
                            "name": "耐克",
                            "verdict": "absent",
                            "catalyst_type": None,
                            "catalyst_summary": None,
                            "source_title": None,
                            "source_url": None,
                        },
                        {
                            "symbol": "UNH",
                            "name": "联合健康",
                            "verdict": "cannot_verify",
                            "catalyst_type": None,
                            "catalyst_summary": "should be ignored",
                            "source_title": None,
                            "source_url": None,
                        },
                    ]
                }
            },
        }
        mock_sess.request.side_effect = [create_resp, poll_resp]

        entities = [
            {"symbol": "PDD", "name": "拼多多", "pct_change": "+11.04%", "sector": "中概ADR"},
            {"symbol": "NKE", "name": "耐克", "pct_change": "-5.45%", "sector": "可选消费"},
            {"symbol": "UNH", "name": "联合健康", "pct_change": "-5.55%", "sector": "医疗"},
        ]
        with patch.dict(os.environ, {"EXA_API_KEY": "test-key", "EXA_AGENT_EFFORT": "medium"}):
            confirmed = bot_utils.exa_agent_verify_catalysts(
                entities,
                market_label="美股",
                session_date="2026-11-19",
                poll_interval=0.01,
                session=mock_sess,
            )

        self.assertEqual(len(confirmed), 1)
        self.assertEqual(confirmed[0]["symbol"], "PDD")
        self.assertEqual(confirmed[0]["pct_change"], "+11.04%")
        block = bot_utils.format_exa_catalyst_block(confirmed)
        self.assertIn("拼多多(PDD +11.04%)", block)
        self.assertIn("[财报业绩]", block)


class TestUsUncoveredSelection(unittest.TestCase):
    def test_select_uncovered_us_movers_skips_covered(self):
        quotes = {
            "XLK": {"change_pct": "-1.90%"},
            "XLY": {"change_pct": "-2.79%"},
            "XLE": {"change_pct": "-0.65%"},
            "XLU": {"change_pct": "-1.24%"},
            "XLP": {"change_pct": "-1.45%"},
            "XLC": {"change_pct": "-2.67%"},
            "XLB": {"change_pct": "-2.06%"},
            "AMD": {"change_pct": "-7.72%", "shortName": "AMD"},
            "NKE": {"change_pct": "-5.45%", "shortName": "Nike"},
            "UNH": {"change_pct": "-5.55%", "shortName": "UnitedHealth"},
            "INTC": {"change_pct": "+1.92%", "shortName": "Intel"},
        }
        news_lines = ["[原始英文标题] AMD dives on short seller report"]
        picked = stock_report._select_uncovered_us_movers(quotes, news_lines, max_total=6)
        syms = [p["symbol"] for p in picked]
        self.assertNotIn("AMD", syms)
        self.assertIn("NKE", syms)
        self.assertIn("UNH", syms)
        self.assertIn("INTC", syms)

    def test_noise_filter_and_watchlist_news_tagging(self):
        self.assertTrue(stock_report._NOISE_NEWS_TITLE_RE.search("AXIS: 3 Key Takeaways for Long-Term Investors"))
        self.assertTrue(stock_report._NOISE_NEWS_TITLE_RE.search("Inside India newsletter: Roaring economy and auto sales"))
        self.assertTrue(stock_report._NOISE_NEWS_TITLE_RE.search("K-beauty stocks rally ahead of Black Friday"))
        self.assertFalse(stock_report._NOISE_NEWS_TITLE_RE.search("S&P warns Nvidia credit rating could be cut"))
        self.assertFalse(stock_report._NOISE_NEWS_TITLE_RE.search("Aschenbrenner's Situational Awareness lost 65% in Q3"))

        hits = stock_report._match_watchlist_hits(
            "Nvidia and CoreWeave expand compute agreement",
            "Oracle also raised debt for data centers.",
            ["NVDA", "CRWV", "ORCL", "TSLA"],
        )
        self.assertEqual(hits, ["NVDA", "CRWV", "ORCL"])

    def test_select_uncovered_us_movers_prioritizes_watchlist_and_matches_direction(self):
        quotes = {
            "XLK": {"change_pct": "-1.50%"},
            "XLY": {"change_pct": "-0.20%"},
            "XLV": {"change_pct": "-0.56%"},
            "XLB": {"change_pct": "+2.07%"},
            "XLRE": {"change_pct": "+0.92%"},
            "XLC": {"change_pct": "+0.89%"},
            "AMZN": {"change_pct": "-3.12%", "shortName": "Amazon"},
            "TSLA": {"change_pct": "-1.18%", "shortName": "Tesla"},
            "MCD": {"change_pct": "+1.00%", "shortName": "McDonald's"},
            "CEG": {"change_pct": "-3.90%", "shortName": "Constellation Energy"},
            "NVDA": {"change_pct": "-3.91%", "shortName": "Nvidia"},
        }
        custom_wl = {
            "groups": [
                {"name": "能源层", "emoji": "⚡", "tickers": [{"symbol": "CEG", "name": "Constellation Energy"}]},
                {"name": "芯片层", "emoji": "🔌", "tickers": [{"symbol": "NVDA", "name": "英伟达"}]},
            ],
            "all_symbols": ["CEG", "NVDA"],
        }
        news_lines = ["[原始英文标题] Nvidia faces credit review"]
        picked = stock_report._select_uncovered_us_movers(
            quotes, news_lines, max_total=6, custom_wl=custom_wl
        )
        syms = [p["symbol"] for p in picked]
        self.assertIn("CEG", syms)
        self.assertNotIn("NVDA", syms)
        # XLY 为领跌板块，收涨的 MCD 不应进入领跌板块候选
        self.assertNotIn("MCD", syms)

    def test_fetch_financial_events_skips_published_macro_items(self):
        fake_econ = {
            "data": {
                "rows": [
                    {
                        "country": "United States",
                        "eventName": "Core PCE Price Index MoM",
                        "actual": "0.3%",
                        "consensus": "0.3%",
                        "previous": "0.3%",
                    },
                    {
                        "country": "United States",
                        "eventName": "GDP Growth Rate QoQ",
                        "actual": "",
                        "consensus": "3.4%",
                        "previous": "2.2%",
                    },
                    {
                        "country": "United States",
                        "eventName": "GDP Price Index QoQ",
                        "actual": "--",
                        "consensus": "2.5%",
                        "previous": "2.3%",
                    },
                ]
            }
        }
        fake_earn = {"data": {"rows": []}}

        def fake_get(url, **kwargs):
            resp = MagicMock(status_code=200)
            if "calendar/economic" in url:
                resp.json.return_value = fake_econ
            else:
                resp.json.return_value = fake_earn
            return resp

        with patch.object(stock_report.SESSION, "get", side_effect=fake_get):
            res = stock_report.fetch_financial_events("2026-11-25", {"MRVL"})
        macro_names = [m["event"] for m in res["macro"]]
        self.assertFalse(any("PCE" in n for n in macro_names))
        self.assertTrue(any("美国 GDP" == n for n in macro_names))
        # GDP 衍生子项应被主事件去重
        self.assertEqual(len([m for m in res["macro"] if m["date"] == "2026-11-26"]), 1)


if __name__ == "__main__":
    unittest.main()
