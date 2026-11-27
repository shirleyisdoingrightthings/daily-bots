import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "shared"))
sys.path.insert(0, str(REPO_ROOT / "a-share-bot"))

import a_share_report as asr
import bot_utils as bu
import notion_archive as na


class TestAShareTimeWindowsAndIsolation(unittest.TestCase):
    def test_premarket_and_close_windows(self):
        # premarket: strictly 08:30 - 09:24
        self.assertFalse(asr._is_in_window(datetime(2026, 11, 27, 8, 29), "premarket")[0])
        self.assertTrue(asr._is_in_window(datetime(2026, 11, 27, 8, 30), "premarket")[0])
        self.assertTrue(asr._is_in_window(datetime(2026, 11, 27, 9, 24), "premarket")[0])
        self.assertFalse(asr._is_in_window(datetime(2026, 11, 27, 9, 25), "premarket")[0])
        # close: strictly >= 15:00
        self.assertFalse(asr._is_in_window(datetime(2026, 11, 27, 14, 59), "close")[0])
        self.assertTrue(asr._is_in_window(datetime(2026, 11, 27, 15, 0), "close")[0])
        self.assertTrue(asr._is_in_window(datetime(2026, 11, 27, 16, 15), "close")[0])

    def test_validate_draft_session_blocks_cross_session_mixup(self):
        pre_txt = "<b>🌅 🇨🇳 A股开盘前瞻 · 2026-11-27</b>\n正文"
        cls_txt = "<b>🇨🇳 A股收盘复盘 · 2026-11-27</b>\n正文"
        self.assertTrue(asr._validate_draft_session(pre_txt, "premarket")[0])
        self.assertFalse(asr._validate_draft_session(pre_txt, "close")[0])
        self.assertTrue(asr._validate_draft_session(cls_txt, "close")[0])
        self.assertFalse(asr._validate_draft_session(cls_txt, "premarket")[0])

    def test_run_send_rejects_missing_webhook_and_wrong_session_draft(self):
        with tempfile.TemporaryDirectory() as tmp:
            tdir = Path(tmp)
            logs = tdir / "logs"
            logs.mkdir()
            (logs / "report_cn_close.txt").write_text("<b>🌅 🇨🇳 A股开盘前瞻 · 2026-11-27</b>\n错稿", encoding="utf-8")
            (logs / "fetch_meta_close.json").write_text(
                json.dumps({"metrics": {"session": "close"}}), encoding="utf-8"
            )
            with patch.object(asr, "BASE", tdir), \
                 patch.object(asr, "LOG_FILE", logs / "run.log"), \
                 patch.object(asr, "FETCH_META", logs / "fetch_meta.json"), \
                 patch.object(asr, "DRAFT_CN", logs / "report_cn.txt"), \
                 patch.object(asr, "FEISHU_WEBHOOK_CN", ""), \
                 patch.dict(os.environ, {"FEISHU_WEBHOOK_CN": "", "FORCE_RUN": "1"}):
                rc_no_wh = asr.run_send("close")
                self.assertEqual(rc_no_wh, 1)

            with patch.object(asr, "BASE", tdir), \
                 patch.object(asr, "LOG_FILE", logs / "run.log"), \
                 patch.object(asr, "FETCH_META", logs / "fetch_meta.json"), \
                 patch.object(asr, "DRAFT_CN", logs / "report_cn.txt"), \
                 patch.dict(os.environ, {"FEISHU_WEBHOOK_CN": "https://example.test/hook", "FORCE_RUN": "1"}):
                rc_wrong = asr.run_send("close")
                self.assertEqual(rc_wrong, 1)


class TestAShareQuantMathAndHolidayCrossCheck(unittest.TestCase):
    def test_wilder_rsi_flat_series_returns_50(self):
        flat = [100.0] * 25
        self.assertEqual(asr._calc_wilder_rsi(flat, 14), 50.0)
        rising = [100.0 + i for i in range(25)]
        self.assertEqual(asr._calc_wilder_rsi(rising, 14), 100.0)

    def test_fetch_ma_trend_detail_z_vol_and_intraday_rvol_scaling(self):
        # Build 60 synthetic daily bars ending on 2026-11-27
        bars = []
        base_dt = datetime(2026, 9, 1)
        px = 3500.0
        for i in range(60):
            d = (base_dt + timedelta(days=i)).strftime("%Y-%m-%d")
            px += (8.0 if i % 2 == 0 else -5.0)
            vol = 100000.0 if i < 59 else 60000.0  # midday half-day volume = 0.6x raw -> 1.2x scaled
            bars.append([d, f"{px - 5:.2f}", f"{px:.2f}", f"{px + 10:.2f}", f"{px - 10:.2f}", f"{vol:.0f}"])
        bars[-1][0] = "2026-11-27"

        class _FakeResp:
            def raise_for_status(self):
                return None

            def json(self):
                return {"data": {"sh000001": {"qfqday": bars}}}

        with patch.object(asr.DIRECT_SESSION, "get", return_value=_FakeResp()):
            res_intraday = asr.fetch_ma_trend_detail(
                "sh000001",
                {"last": px, "high": px + 10, "low": px - 10},
                today_str="2026-11-27",
                live_date_compact="20261127",
                strict_prior_close=False,
                intraday_minutes=120,
            )
            self.assertIn("Z_vol=", res_intraday["text"])
            self.assertIn("RVOL=1.20x(折算全天)", res_intraday["text"])

            # strict_prior_close=True must strip today's 2026-11-27 bar!
            res_prior = asr.fetch_ma_trend_detail(
                "sh000001",
                {"last": px, "high": px + 10, "low": px - 10},
                today_str="2026-11-27",
                live_date_compact="20261127",
                strict_prior_close=True,
            )
            self.assertNotEqual(res_prior["bar"]["date"], "2026-11-27")
            self.assertNotIn("折算全天", res_prior["text"])

    def test_us_holiday_vs_us_bot_failure_crosscheck(self):
        with tempfile.TemporaryDirectory() as tmp:
            us_dir = Path(tmp) / "us stock bot"
            (us_dir / "logs").mkdir(parents=True)
            (us_dir / "logs" / "report_market.txt").write_text(
                "<b>📊 美股收盘 & 加密行情 · 2026-11-25</b>\n旧内容",
                encoding="utf-8",
            )
            now_fri = datetime(2026, 11, 27, 9, 10)
            with patch.object(asr, "US_BOT_DIR", us_dir):
                # Case 1: us.INX date is 2026-11-25 (< expected 2026-11-26) -> real US holiday
                _, mkt1, _, warn1 = asr._load_latest_us_report(now_fri, us_inx_date="2026-11-25 16:00:00")
                self.assertIn("隔夜美股休市提示", warn1)
                self.assertEqual(mkt1, "")
                # Case 2: us.INX date is 2026-11-26 (US traded!) but report is 2026-11-25 -> US bot stale, NOT holiday
                _, mkt2, _, warn2 = asr._load_latest_us_report(now_fri, us_inx_date="2026-11-26 16:00:00")
                self.assertIn("正常收盘", warn2)
                self.assertIn("未更新", warn2)
                self.assertEqual(mkt2, "")


class TestAShareTelegraphExaAndNotion(unittest.TestCase):
    def test_telegraph_blocks_minor_country_in_tag4_and_pins_tier0_policy(self):
        fake_feed = {
            "result": {
                "data": {
                    "feed": {
                        "list": [
                            {
                                "create_time": "2026-11-27 08:20:00",
                                "rich_text": "哥斯达黎加将基准利率从3%下调至2.75%，符合当地市场预期。",
                            },
                            {
                                "create_time": "2026-11-27 08:10:00",
                                "rich_text": "【国常会：加力发挥“国家队”稳市作用，进一步优化中央汇金公司的类“平准基金”功能】" + "内容细则" * 90,
                            },
                            {
                                "create_time": "2026-11-27 11:32:00",
                                "rich_text": "【午评：沪指半日涨1%】不应混入盘前电报。",
                            },
                        ]
                    }
                }
            }
        }

        class _FakeResp:
            def raise_for_status(self):
                return None

            def json(self):
                return fake_feed

        with patch.object(asr.DIRECT_SESSION, "get", return_value=_FakeResp()):
            teles = asr.fetch_sina_telegraph(
                limit=10,
                session_mode="premarket",
                now_dt=datetime(2026, 11, 27, 9, 10),
            )
        joined = "\n".join(teles)
        self.assertNotIn("哥斯达黎加", joined)
        self.assertNotIn("午评", joined)
        self.assertIn("平准基金", joined)
        # Tier-0 policy should retain > 350 chars (not cut at 260)
        self.assertGreater(len(teles[0]), 350)

    def test_exa_agent_blocks_untrusted_a_share_scraper_domains(self):
        fake_create = type("R", (), {"json": lambda self: {"id": "run_cn_1", "status": "completed", "output": {"structured": {"items": [
            {
                "symbol": "300502.SZ",
                "name": "新易盛",
                "verdict": "confirmed",
                "catalyst_type": "product_or_order",
                "catalyst_summary": "1.6T光模块四季度逐步放量，订单交付有信心。",
                "source_title": "爱股网问答",
                "source_url": "http://www.igu888.com/stockask/300502.html",
            },
            {
                "symbol": "688012.SH",
                "name": "中微公司",
                "verdict": "confirmed",
                "catalyst_type": "product_or_order",
                "catalyst_summary": "子公司超微量测装备研发产业化项目在南昌正式签约落地。",
                "source_title": "财联社电报",
                "source_url": "https://www.cls.cn/detail/2545210",
            },
        ]}}}})()
        with patch.object(bu, "_exa_request", return_value=fake_create), \
             patch.dict(os.environ, {"EXA_API_KEY": "test_key"}):
            res = bu.exa_agent_verify_catalysts(
                [
                    {"symbol": "300502.SZ", "name": "新易盛", "pct_change": "+9.40%"},
                    {"symbol": "688012.SH", "name": "中微公司", "pct_change": "+5.49%"},
                ],
                market_label="中国A股",
                session_date="2026-11-27",
            )
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["symbol"], "688012.SH")

    def test_notion_a_share_premarket_and_close_hierarchy_and_us_date_propagation(self):
        # 1) A-share premarket with space in "A 股" + ⚡ 三大看点 -> heading_3 (no 1./1./1. reset)
        pre_md = (
            "<b>🌅 🇨🇳 A 股开盘前瞻 · 2026-11-27</b>\n\n"
            "🌍 <b>海外市场映射</b>：富时 A50 12,612 (+0.79%) | 离岸人民币 (USDCNH) 6.6633 (+0.03%) | 美股休市（前收 11-25） | 伦敦金 (24h 实时) 3,634.81 (+1.92%) | 比特币 (24h 实时) $61,442 (+0.89%)\n"
            "📊 <b>前日 A 股收盘</b>：上证指数 3,476.88 (+0.01%) | 创业板指 2,189.82 (+0.29%) | 两市成交 13,173.8 亿元\n"
            "📅 <b>盘前宏观与事件</b>：今日 国家统计局规模以上工业企业利润 | 11-30 官方制造业 PMI\n\n"
            "<b>🔭 盘前核心定性与早盘主线</b>\n"
            "1. 国常会定调优化中央汇金类平准基金功能叠加 A50 走强，早盘情绪偏暖。\n\n"
            "<b>🎯 早盘交易台风控与 T+1 执行框架（09:15–10:00）</b>\n"
            "· <b>信号灯与 Regime</b>：🟢 高开进攻 — 政策与汇率共振。\n\n"
            "<b>⚡ 开盘前瞻三大核心看点</b>\n"
            "<b>1. 盘后至盘前国内政策与产业催化</b>\n"
            "· 国常会部署平准基金稳市。\n"
            "<b>2. 海外先导锚与产业链映射</b>\n"
            "· 美股休市，聚焦 A50 与黄金。\n"
        )
        blocks = na.convert_to_blocks(pre_md, "🇨🇳 A股")
        self.assertEqual(blocks[0]["type"], "heading_1")
        h3s = [b for b in blocks if b["type"] == "heading_3"]
        self.assertGreaterEqual(len(h3s), 2)
        summ = na.extract_summary(pre_md, "🇨🇳 A股", report_type="开盘前瞻")
        self.assertIn("🟢 偏多", summ)
        self.assertIn("国常会定调", summ)
        self.assertNotIn("盘后至盘前国内政策与产业催化", summ)

        # 2) A-share close combined 🔥 & ❄️ sector extraction
        cls_md = (
            "<b>🇨🇳 A股收盘复盘 · 2026-11-27</b>\n\n"
            "📊 <b>核心指数</b>：上证指数 3,520.00 (+1.24%) | 创业板指 2,272.00 (+3.79%)\n"
            "🧭 <b>宽度与情绪</b>：全市场 4600 涨 / 600 跌 | 涨停 106 家 — 普涨修复\n"
            "💰 <b>量能与资金</b>：沪深成交 18,500 亿元 — 放量突破\n\n"
            "<b>🎯 交易台风控与 T+1 执行框架</b>\n"
            "· <b>信号灯与 Regime</b>：🟡 震荡分化 — 放量反弹但仍处年线下方。\n\n"
            "<b>🔥 领涨/抗跌 Top 3 主线 & ❄️ 领跌 Top 3 主线</b>\n"
            "<b>1. 海外AI算力与光模块 (+5.53%)</b>：中际旭创、新易盛领涨。\n"
            "<b>2. 国产算力与半导体芯片 (+4.16%)</b>：中微公司签约。\n"
            "<b>3. AI应用与计算机软件 (+3.20%)</b>：无独立催化。\n"
            "<b>4. 煤炭电力与高股息红利 (-2.69%)</b>：中国神华回调。\n"
            "<b>5. 纯银行与四大行权重 (-1.15%)</b>：资金切向成长。\n"
        )
        cls_summ = na.extract_summary(cls_md, "🇨🇳 A股", report_type="收盘复盘")
        self.assertIn("领涨 海外 AI 算力与光模块", cls_summ)
        self.assertIn("领跌 煤炭电力与高股息红利", cls_summ)

        # 3) US Financial Events date propagation ("12-01 BHP、盘后 ★MRVL | 12-02 盘前 BMO、盘后 ★CRM")
        us_md = (
            "<b>📊 美股收盘 & 加密行情 · 2026-11-25</b>\n\n"
            "📈 <b>核心指数</b>：标普 500 7,182.78 (-0.25%) | 10 年期美债 4.864% | VIX 28.29 | 伦敦金 3,575 (-0.15%)\n"
            "📅 <b>Financial Events</b>：宏观日程：12-02 ISM 制造业 PMI | 财报预告：12-01 BHP ($175B)、盘后 ★MRVL ($115B) | 12-02 盘前 BMO ($112B)、盘后 ★CRM ($159B)\n"
        )
        us_summ = na.extract_summary(us_md, "🗽 美股与加密")
        self.assertIn("12-01 BHP、盘后 ★MRVL、12-02 盘后 ★CRM", us_summ)

    def test_shell_scripts_syntax_and_auto_repair_session_filter(self):
        for sh in ("claude_report.sh", "health_check.sh", "auto_repair.sh"):
            r = subprocess.run(["bash", "-n", str(REPO_ROOT / "a-share-bot" / sh)], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, msg=r.stderr)
        r_base = subprocess.run(["bash", "-n", str(REPO_ROOT / "shared" / "auto_repair_base.sh")], capture_output=True, text=True)
        self.assertEqual(r_base.returncode, 0, msg=r_base.stderr)


if __name__ == "__main__":
    unittest.main()
