#!/usr/bin/python3
"""
美股收盘 & 24h 加密行情合盘日报
数据源：
  · CNBC quote API: 4 大指数 / 10Y美债 / VIX / DXY / 黄金 / 布伦特原油 / SOX / HYG / 11 个 GICS 板块 ETF 与核心成分股 / 28 只权重股 / RSP / 可选个人自选股(watchlist.json)
  · 腾讯财经日 K (`fqkline`, 60日): 标普 500 / 纳指 / 道指 MA5/MA10/MA20 均线与乖离率 + RSI(14) + 布林带 %B(20,2)
  · CoinGecko + Alternative.me / CMC: BTC/ETH/BNB/SOL/XRP/HYPE 24h 行情、总市值、BTC 市占率与恐贪指数
  · Nasdaq Calendar API: 未来 5 个交易日重大美股财报（含自选股优先标注）与核心美国宏观日程（FOMC/CPI/PCE/非农/GDP/ISM）
  · RSS × 5: CNBC 财经头条 / CNBC 盈利 / Dow Jones MarketWatch / Nasdaq Markets / Nasdaq Earnings
  · 可选 Exa 定向补盲: 付费墙短摘要补全 + Exa Agent 异动股三态催化验证（confirmed/absent/cannot_verify）
由 AI 定时任务写稿：① 收盘仪表盘（美股 + 大宗 + 加密 + 自选股 + 决策信号灯）② 美股要闻播报，推送 2 条飞书卡片并归档至 Notion。
本脚本只负责抓取与推送，不含写稿用的第三方大模型 API。

两种运行模式（--mode）：
- fetch：抓取行情 + 新闻 + 可选 Exa 补盲 → context 落盘 logs/last_context.txt + 写 logs/fetch_meta.json
- send ：读取写好的两份稿子 → 依次推送飞书 + 合并归档 Notion + 更新 5 日 market_memory.json + 写日志

关于"当天"的定义：本 bot 播报的是**最近一个已收盘的美股交易日**。北京时间
10:06 推送时，美东时间是前一日 22:06，所以周二～周六推送的是前一交易日收盘。
周日与周一（北京时间）没有新的收盘场次，脚本会输出 SKIP_NO_NEW_SESSION 并
跳过，避免把上周五的行情当成新内容重复播报。

写稿规范存放于 prompt_market.md（消息①）与 prompt_news.md（消息②）。
"""

from __future__ import annotations

import os
import re
import sys
import math
import html
import time
import json
import argparse
import traceback
import requests
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

# 共享工具库
# 从脚本自身位置推导共享层（bot 目录的同级 shared/），
# 这样整个 bots 文件夹搬到任何位置都不用改路径
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "shared"))
from bot_utils import (sanitize_html, with_retry, fetch_rss, parse_entry_date,
                       already_ran_today, fetch_article_text,
                       url_key, load_sent_urls, record_sent_urls, extract_hrefs,
                       load_daily_ledger, format_ledger_context,
                       archive_and_sync_notion,
                       send_feishu, update_zero_streak,
                       make_logger, make_pending_saver, proxy_ok,
                       emit_fetch_output,
                       exa_enrich_enabled, exa_fetch_contents,
                       exa_search_fallback, exa_agent_verify_catalysts,
                       format_exa_catalyst_block)

LOG_FILE   = Path(__file__).parent / "logs" / "run.log"
JSONL_FILE = Path(__file__).parent / "logs" / "run.jsonl"
LOG_FILE.parent.mkdir(exist_ok=True)
CACHE_FILE = Path(__file__).parent / "pending_messages.json"

# Claude routine 把写好的两份稿子分别存到这里，再用 --mode send 发送
DRAFT_MARKET = Path(__file__).parent / "logs" / "report_market.txt"  # 消息①收盘仪表盘
DRAFT_NEWS   = Path(__file__).parent / "logs" / "report_news.txt"    # 消息②新闻播报
# fetch 模式写出、send 模式读回的边车：承载 OK 日志摘要与 health_check 所需 metrics
FETCH_META   = Path(__file__).parent / "logs" / "fetch_meta.json"
# fetch 抓来的完整 stdout（marker + context）落盘一份：调用方截断、进程中断、
# 或写稿失败要重来时，不必再打一遍外部 API。每次 fetch 覆盖写，只留最近一次。
LAST_CONTEXT = Path(__file__).parent / "logs" / "last_context.txt"
# 跨天去重档案：send 成功后记录稿件里实际用到的链接，fetch 时据此排除
SENT_URLS    = Path(__file__).parent / "logs" / "sent_urls.json"
# RSS 源连续零产计数（fetch 阶段唯一写入，health_check 只读）
ZERO_STREAK  = Path(__file__).parent / "logs" / ".zero_streak.json"
ZERO_STREAK_THRESHOLD = 3
# 已播报过的交易日（避免周日/周一重复播上周五的收盘）
LAST_SESSION = Path(__file__).parent / "logs" / ".last_session"
# 跨日盘面状态记忆库（保留最近 5 个交易日的板块轮动、信号灯与关注方向，借鉴 claude-quickstarts State Store 设计）
MARKET_MEMORY = Path(__file__).parent / "logs" / "market_memory.json"
MAX_MEMORY_DAYS = 5
# 个人分层自选股配置（本地私有，默认读取同目录 watchlist.json，已在 .gitignore 忽略；支持 WATCHLIST_FILE 覆盖）
WATCHLIST_FILE = Path(os.getenv("WATCHLIST_FILE") or (Path(__file__).parent / "watchlist.json"))

# ===== P0: 显式代理配置 =====
_PROXY = os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY")
SESSION = requests.Session()
SESSION.proxies = {"http": _PROXY, "https": _PROXY}
# feedparser 内部使用 urllib，通过环境变量注入代理
if _PROXY:
    os.environ.setdefault("HTTP_PROXY",  _PROXY)
    os.environ.setdefault("HTTPS_PROXY", _PROXY)


# ===== P1: 结构化日志（实现见 shared/bot_utils.make_logger）=====
write_log = make_logger(LOG_FILE, JSONL_FILE)



# ===== 配置 =====
# 飞书自定义机器人：webhook 地址在群「设置 → 群机器人 → 添加机器人 → 自定义机器人」
# 里取得。
FEISHU_WEBHOOK = os.getenv("FEISHU_WEBHOOK", "")
COINGECKO_API_KEY = os.getenv("COINGECKO_API_KEY", "")

# CNBC 免费 quote 接口，一次调用可取全部标的，无需 API key
CNBC_QUOTE_URL = (
    "https://quote.cnbc.com/quote-html-webservice/restQuote/symbolType/symbol"
    "?symbols={syms}&requestMethod=itv&noform=1&partnerId=2&fund=1"
    "&exthrs=1&output=json"
)

INDICES = [(".SPX", "标普500"), (".IXIC", "纳斯达克"),
           (".DJI", "道琼斯"), (".RUT", "罗素2000")]
# 跨资产宏观与大宗指标（含 2Y/10Y 利率曲线与 VIX/VIX3M 波动率期限结构；XAU= 伦敦金与 @LCO.1 布油为 24h 实时报价）
MACRO   = [("US2Y", "2年期美债"), ("US10Y", "10年期美债"), (".VIX", "VIX"), (".VIX3M", "VIX3M"),
           (".DXY", "美元指数"), ("XAU=", "伦敦金(24h实时)"), ("@LCO.1", "布伦特原油(24h实时)")]
# 行业景气与久期中性信用风向标（SOXX 规避休市日 .SOX 归零 UNCH；IEI 为 3-7 年美债 ETF，HYG-IEI 剔除利率干扰）
RISK_GAUGES = [("SOXX", "半导体ETF(SOXX)"), ("HYG", "高收益债(HYG)"), ("IEI", "中期美债(IEI)")]
# 11 个 GICS 板块，用 SPDR 行业 ETF 代理
SECTORS = [("XLK", "科技"), ("XLC", "通信服务"), ("XLY", "可选消费"),
           ("XLP", "必需消费"), ("XLE", "能源"), ("XLF", "金融"),
           ("XLV", "医疗"), ("XLI", "工业"), ("XLB", "原材料"),
           ("XLRE", "房地产"), ("XLU", "公用事业")]

# 11 个 GICS 板块的核心成分股映射（每板块 8–12 只核心权重，避免领跌板块样本不足）
SECTOR_STOCKS = {
    "XLK":  [("NVDA", "英伟达"), ("AAPL", "苹果"), ("MSFT", "微软"), ("AVGO", "博通"),
             ("AMD", "AMD"), ("ORCL", "甲骨文"), ("CRM", "Salesforce"), ("MU", "美光"),
             ("QCOM", "高通"), ("PLTR", "Palantir"), ("ADBE", "Adobe"), ("INTC", "英特尔")],
    "XLC":  [("META", "Meta"), ("GOOGL", "Alphabet"), ("NFLX", "奈飞"),
             ("TMUS", "T-Mobile"), ("DIS", "迪士尼"), ("VZ", "Verizon"),
             ("CMCSA", "康卡斯特"), ("T", "AT&T")],
    "XLY":  [("AMZN", "亚马逊"), ("TSLA", "特斯拉"), ("HD", "家得宝"),
             ("MCD", "麦当劳"), ("NKE", "耐克"), ("BKNG", "Booking"),
             ("LOW", "劳氏"), ("TJX", "TJX"), ("SBUX", "星巴克")],
    "XLP":  [("WMT", "沃尔玛"), ("COST", "好市多"), ("PG", "宝洁"),
             ("KO", "可口可乐"), ("PEP", "百事"), ("PM", "菲利普莫里斯"),
             ("MDLZ", "亿滋"), ("CL", "高露洁")],
    "XLE":  [("XOM", "埃克森美孚"), ("CVX", "雪佛龙"), ("COP", "康菲石油"),
             ("SLB", "斯伦贝谢"), ("EOG", "EOG能源"), ("OXY", "西方石油"),
             ("MPC", "马拉松原油"), ("PSX", "菲利普斯66")],
    "XLF":  [("BRK.B", "伯克希尔"), ("JPM", "摩根大通"), ("V", "Visa"),
             ("MA", "万事达"), ("GS", "高盛"), ("COIN", "Coinbase"),
             ("BAC", "美国银行"), ("MS", "摩根士丹利")],
    "XLV":  [("LLY", "礼来"), ("UNH", "联合健康"), ("JNJ", "强生"),
             ("ABBV", "艾伯维"), ("MRK", "默沙东"), ("ISRG", "直觉外科"),
             ("TMO", "赛默飞"), ("AMGN", "安进")],
    "XLI":  [("GE", "GE航空"), ("CAT", "卡特彼勒"), ("RTX", "雷神技术"),
             ("UNP", "联合太平洋"), ("HON", "霍尼韦尔"), ("ETN", "伊顿"),
             ("DE", "迪尔"), ("LMT", "洛克希德马丁")],
    "XLB":  [("LIN", "林德气体"), ("NEM", "纽蒙特矿业"), ("FCX", "自由港麦克莫兰"),
             ("SHW", "宣伟"), ("APD", "空气化工"), ("NUE", "纽柯钢铁"),
             ("ECL", "艺康"), ("DD", "杜邦")],
    "XLRE": [("PLD", "安博"), ("AMT", "美国电塔"), ("EQIX", "Equinix"),
             ("WELL", "Welltower"), ("SPG", "西蒙地产"), ("O", "Realty Income"),
             ("PSA", "Public Storage"), ("DLR", "Digital Realty")],
    "XLU":  [("NEE", "NextEra"), ("CEG", "Constellation核电"), ("VST", "Vistra电力"),
             ("SO", "南方电力"), ("DUK", "杜克能源"), ("AEP", "美国电力"),
             ("SRE", "Sempra"), ("D", "Dominion")],
}

# 均衡 30 只全行业核心权重（覆盖全部 11 个 GICS 板块，消除原 28 只科技偏斜 78.6% 的宽度失真）
CORE_BREADTH_30 = [
    "NVDA", "AAPL", "MSFT", "AVGO",          # 科技 (4)
    "META", "GOOGL", "NFLX",                 # 通信服务 (3)
    "AMZN", "TSLA", "HD",                    # 可选消费 (3)
    "WMT", "COST", "PG",                     # 必需消费 (3)
    "BRK.B", "JPM", "V",                     # 金融 (3)
    "LLY", "UNH", "JNJ",                     # 医疗 (3)
    "GE", "CAT", "UNP",                      # 工业 (3)
    "XOM", "CVX",                            # 能源 (2)
    "LIN", "FCX",                            # 原材料 (2)
    "PLD", "AMT",                            # 房地产 (2)
    "NEE", "CEG",                            # 公用事业 (2)
]

# 重点高关注度异动股池（含 AI/芯片/加密映射股，专用于全市场焦点异动榜，不冒充全市场宽度）
WATCHLIST = ["NVDA", "AAPL", "MSFT", "GOOGL", "AMZN", "META", "TSLA", "AVGO",
             "AMD", "NFLX", "ORCL", "CRM", "ADBE", "INTC", "MU", "QCOM",
             "PLTR", "SMCI", "ARM", "TSM", "JPM", "BRK.B", "LLY", "UNH",
             "XOM", "WMT", "COIN", "MSTR"]

# (feed_url, limit, is_general)
# 2026-11-26 量化审阅提纯：撤除充斥 Motley Fool / MarketBeat 荐股软文的 Nasdaq 双源，
# 替换为经实测高信噪比的 CNBC Economy + WSJ US Business + 美联储官方 Press Releases：
RSS_SOURCES = [
    ("https://www.cnbc.com/id/100003114/device/rss/rss.html",        10, False),  # CNBC 财经头条
    ("https://www.cnbc.com/id/20910258/device/rss/rss.html",          8, False),  # CNBC 宏观经济与央行/关税
    ("https://www.cnbc.com/id/15839135/device/rss/rss.html",          8, False),  # CNBC 盈利/财报
    ("https://feeds.content.dowjones.io/public/rss/RSSMarketsMain",   6, False),  # WSJ Markets（配 Exa 补全付费墙正文）
    ("https://feeds.content.dowjones.io/public/rss/WSJcomUSBusiness", 6, False),  # WSJ US Business（公司/司法/并购独家）
    ("https://www.federalreserve.gov/feeds/press_all.xml",            5, False),  # 美联储官方声明与监管发布
]

# ===== P2: 消息缓存（降级策略）=====
# 飞书推送失败时把稿件存到 pending_messages.json，避免内容丢失。
# 注意：**不做自动重发**——重发要判断"这稿子还是今天的吗"，跨天重发旧稿比丢一次
# 更糟。当天补救由 health_check → claude_catchup 重走完整流程负责，缓存只作为
# 人工恢复的兜底副本。
save_pending = make_pending_saver(CACHE_FILE)



# ===== 行情抓取 =====
def _num(s):
    """CNBC 返回的价格带千分位逗号与百分号，统一转成 float；失败返回 None。"""
    if s is None:
        return None
    try:
        return float(str(s).replace(",", "").replace("%", "").replace("+", "").strip())
    except ValueError:
        return None


@with_retry(max_retries=2, base_delay=5, exceptions=(Exception,))
def fetch_quotes(symbols: list) -> dict:
    """分批取回全部标的行情（每批最多 50 个，防止加入大型自选股池后 URL 超长），返回 {symbol: quote_dict}。"""
    merged: dict = {}
    batch_size = 50
    for i in range(0, len(symbols), batch_size):
        chunk = symbols[i:i + batch_size]
        url = CNBC_QUOTE_URL.format(syms="|".join(chunk))
        resp = SESSION.get(url, headers={"User-Agent": _UA}, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        quotes = data.get("FormattedQuoteResult", {}).get("FormattedQuote", [])
        if isinstance(quotes, dict):          # 单标的时 CNBC 返回对象而非数组
            quotes = [quotes]
        for q in quotes:
            sym = q.get("symbol")
            if sym:
                merged[sym] = q
    return merged


def load_custom_watchlist(path: Path | None = None) -> dict | None:
    """加载个人分层自选股配置（watchlist.json）。文件不存在时静默返回 None，保证开源默认运行不报错。"""
    cfg_path = path or WATCHLIST_FILE
    if not cfg_path or not cfg_path.exists():
        return None
    try:
        raw = json.loads(cfg_path.read_text(encoding="utf-8"))
        groups = []
        all_symbols: list[str] = []
        seen: set[str] = set()
        for g in raw.get("groups") or []:
            g_name = str(g.get("name") or "").strip()
            g_emoji = str(g.get("emoji") or "•").strip()
            tickers = []
            for t in g.get("tickers") or []:
                if isinstance(t, str):
                    sym, nm = t.strip().upper(), t.strip().upper()
                elif isinstance(t, dict):
                    sym = str(t.get("symbol") or "").strip().upper()
                    nm = str(t.get("name") or sym).strip()
                else:
                    continue
                if not sym:
                    continue
                tickers.append({"symbol": sym, "name": nm})
                if sym not in seen:
                    seen.add(sym)
                    all_symbols.append(sym)
            if g_name and tickers:
                groups.append({
                    "name": g_name,
                    "emoji": g_emoji,
                    "description": str(g.get("description") or "").strip(),
                    "tickers": tickers,
                })
        if not groups:
            return None
        return {
            "name": str(raw.get("name") or "自选股").strip(),
            "emoji": str(raw.get("emoji") or "⭐").strip(),
            "methodology": str(raw.get("methodology") or "").strip(),
            "groups": groups,
            "all_symbols": all_symbols,
        }
    except Exception as e:
        print(f"[WARN] 加载自选股配置 {cfg_path} 失败: {e}", file=sys.stderr)
        return None


_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def fmt_quote(q: dict) -> str:
    """格式化为「7,489.72 (+0.70%)」；2Y/10Y 美债优先按 bp 格式化。"""
    if not q:
        return "未知"
    last = q.get("last", "?")
    pct = q.get("change_pct", "")
    if q.get("symbol") in ("US10Y", "US2Y"):
        if str(pct).endswith("bp"):
            return f"{last} ({pct})"
        chg = _num(q.get("change"))
        if chg is not None:
            return f"{last} ({chg * 100:+.1f}bp)"
    if pct:
        return f"{last} ({pct})"
    return f"{last}"


def session_date(quotes: dict) -> str:
    """取最近收盘交易日（YYYY-MM-DD）。优先用标普，缺失时退回任意有时间的标的。

    退回分支是给 Twelve Data 兜底路径用的：那条路拿不到指数，只有板块 ETF，
    但 ETF 和指数在同一个交易时段收盘，日期一致。session 是本 bot 的幂等键
    （同一场次只播一次），推不出来的话整个流程就断了。
    """
    q = quotes.get(".SPX", {})
    t = q.get("last_time", "") or ""
    if t:
        return t.split("T")[0]
    for qq in quotes.values():
        t = (qq or {}).get("last_time", "") or ""
        if t:
            return t.split("T")[0]
    return ""


def _fetch_crypto_perp_metrics(spot_chg_map: dict[str, float | None] | None = None) -> dict:
    """抓取 BTC/ETH 永续合约年化资金费率与 24h 持仓量（OI）变化%（显式按时间戳排序并换算币本位 OI 剔除币价涨跌错觉）。"""
    spot_map = spot_chg_map or {}
    out: dict = {}
    for ccy, inst in (("BTC", "BTC-USDT-SWAP"), ("ETH", "ETH-USDT-SWAP")):
        try:
            r_fr = SESSION.get(
                f"https://www.okx.com/api/v5/public/funding-rate?instId={inst}",
                headers={"User-Agent": _UA},
                timeout=8,
            )
            r_fr.raise_for_status()
            fr_rows = r_fr.json().get("data") or []
            if not fr_rows:
                continue
            ann_fr = float(fr_rows[0]["fundingRate"]) * 3.0 * 365.0 * 100.0

            oi_chg_str = "未知"
            try:
                r_oi = SESSION.get(
                    f"https://www.okx.com/api/v5/rubik/stat/contracts/open-interest-volume?ccy={ccy}&period=1H",
                    headers={"User-Agent": _UA},
                    timeout=8,
                )
                r_oi.raise_for_status()
                raw_rows = r_oi.json().get("data") or []
                parsed_rows = []
                for row in raw_rows:
                    if len(row) >= 2:
                        try:
                            parsed_rows.append((int(row[0]), float(row[1])))
                        except (TypeError, ValueError):
                            continue
                # 显式按毫秒时间戳降序排列，精确匹配最接近 24 小时前的数据点（杜绝索引偏移）
                parsed_rows.sort(key=lambda x: x[0], reverse=True)
                if len(parsed_rows) >= 2:
                    ts_now, oi_now = parsed_rows[0]
                    target_ts = ts_now - 86_400_000
                    prev_row = min(parsed_rows[1:], key=lambda x: abs(x[0] - target_ts))
                    if abs(prev_row[0] - target_ts) <= 3 * 3_600_000 and prev_row[1] > 0:
                        oi_usd_chg = (oi_now - prev_row[1]) / prev_row[1] * 100.0
                        spot_chg = spot_map.get(ccy)
                        if spot_chg is not None and (1.0 + spot_chg / 100.0) > 0:
                            oi_coin_chg = ((1.0 + oi_usd_chg / 100.0) / (1.0 + spot_chg / 100.0) - 1.0) * 100.0
                            oi_chg_str = f"币本位{oi_coin_chg:+.2f}% (USD {oi_usd_chg:+.2f}%)"
                        else:
                            oi_chg_str = f"USD {oi_usd_chg:+.2f}%"
            except Exception:
                pass
            out[ccy] = {"ann_funding": f"{ann_fr:+.2f}%", "oi_24h_chg": oi_chg_str, "venue": "OKX"}
        except Exception:
            pass

    if len(out) < 2:
        try:
            r_hl = SESSION.post(
                "https://api.hyperliquid.xyz/info",
                json={"type": "metaAndAssetCtxs"},
                headers={"User-Agent": _UA},
                timeout=8,
            )
            r_hl.raise_for_status()
            meta, ctxs = r_hl.json()
            names = [u["name"] for u in meta.get("universe", [])]
            for ccy in ("BTC", "ETH"):
                if ccy in out or ccy not in names:
                    continue
                c = ctxs[names.index(ccy)]
                ann_fr = float(c["funding"]) * 24.0 * 365.0 * 100.0
                out[ccy] = {"ann_funding": f"{ann_fr:+.2f}%", "oi_24h_chg": "未知", "venue": "Hyperliquid"}
        except Exception:
            pass
    return out


# ===== 加密核心行情（2026-11 合并自 crypto daily bot，仅保留行情指标，不抓加密新闻）=====
def fetch_crypto_snapshot() -> dict:
    """抓取核心币价（BTC/ETH/SOL/BNB/XRP/HYPE）、全球加密市值/BTC 市占率、恐惧贪婪指数与永续合约资金费率/OI。
    任一子项失败均降级为“未知”，绝不阻断美股主流程。"""
    cg_headers = {"x-cg-demo-api-key": COINGECKO_API_KEY} if COINGECKO_API_KEY else {}
    prices = {k: "未知" for k in ("BTC", "ETH", "SOL", "BNB", "XRP", "HYPE")}
    btc_chg_num: float | None = None
    eth_chg_num: float | None = None
    try:
        url = (
            "https://api.coingecko.com/api/v3/simple/price"
            "?ids=bitcoin,ethereum,solana,binancecoin,ripple,hyperliquid"
            "&vs_currencies=usd&include_24hr_change=true"
        )
        resp = SESSION.get(url, headers=cg_headers, timeout=15)
        resp.raise_for_status()
        pdata = resp.json()
        btc_chg_num = pdata.get("bitcoin", {}).get("usd_24h_change")
        eth_chg_num = pdata.get("ethereum", {}).get("usd_24h_change")

        def _fmt_cg(key: str) -> str:
            d = pdata.get(key, {})
            price = d.get("usd", 0)
            change = d.get("usd_24h_change", 0) or 0
            if not price:
                return "未知"
            sign = "+" if change >= 0 else ""
            return f"${price:,.2f} ({sign}{change:.2f}%)"

        prices = {
            "BTC":  _fmt_cg("bitcoin"),
            "ETH":  _fmt_cg("ethereum"),
            "SOL":  _fmt_cg("solana"),
            "BNB":  _fmt_cg("binancecoin"),
            "XRP":  _fmt_cg("ripple"),
            "HYPE": _fmt_cg("hyperliquid"),
        }
    except Exception as e:
        print(f"[WARN] 加密币价抓取失败: {e}", file=sys.stderr)

    global_mkt = {
        "btc_dominance": "未知",
        "total_market_cap_b": "未知",
        "market_cap_change_24h": "未知",
        "bull_bear_label": "未知",
    }
    try:
        resp = SESSION.get("https://api.coingecko.com/api/v3/global",
                           headers=cg_headers, timeout=15)
        resp.raise_for_status()
        d = resp.json().get("data", {})
        btc_dom = d.get("market_cap_percentage", {}).get("btc", 0) or 0
        total_mc = d.get("total_market_cap", {}).get("usd", 0) or 0
        mc_chg = d.get("market_cap_change_percentage_24h_usd", 0) or 0
        lag_tag = ""
        if btc_chg_num is not None and btc_dom >= 50:
            if (btc_chg_num * mc_chg < 0 and abs(btc_chg_num - mc_chg) >= 2.0) or abs(btc_chg_num - mc_chg) >= 3.5:
                lag_tag = "·口径延迟"
        btc_dom_r = round(btc_dom, 1)
        if btc_dom_r >= 58.0:
            label = "BTC 主导·避险偏强"
        elif btc_dom_r >= 52.0:
            label = "过渡区·BTC与山寨拉锯"
        elif btc_dom_r >= 46.0:
            label = "偏牛·资金轮动山寨"
        else:
            label = "山寨季·高风险偏好"
        global_mkt = {
            "btc_dominance": f"{btc_dom_r:.1f}%",
            "total_market_cap_b": f"${total_mc / 1e9:,.0f}B",
            "market_cap_change_24h": f"{'+' if mc_chg >= 0 else ''}{mc_chg:.2f}%{lag_tag}",
            "bull_bear_label": label,
        }
    except Exception as e:
        print(f"[WARN] 加密全球市值抓取失败: {e}", file=sys.stderr)

    fear_val, fear_label = "未知", "未知"
    try:
        resp = SESSION.get("https://api.alternative.me/fng/", timeout=10)
        resp.raise_for_status()
        item = resp.json()["data"][0]
        fear_val, fear_label = item["value"], item["value_classification"]
    except Exception as e:
        print(f"[WARN] 恐惧贪婪主源(alternative.me)失败: {e}，改用 CMC 备用源", file=sys.stderr)
        try:
            now_ts = int(time.time())
            resp = SESSION.get(
                "https://api.coinmarketcap.com/data-api/v3/fear-greed/chart"
                f"?start={now_ts - 86400 * 2}&end={now_ts}",
                headers={"User-Agent": _UA}, timeout=10)
            resp.raise_for_status()
            point = resp.json()["data"]["dataList"][-1]
            fear_val, fear_label = str(point["score"]), point["name"]
        except Exception as e2:
            print(f"[WARN] 恐惧贪婪备用源(CMC)也失败: {e2}", file=sys.stderr)

    perp = _fetch_crypto_perp_metrics({"BTC": btc_chg_num, "ETH": eth_chg_num})
    return {
        "prices": prices,
        "global": global_mkt,
        "fear_val": fear_val,
        "fear_label": fear_label,
        "perp": perp,
    }


# ===== 新闻抓取 =====
# 过滤非美股主线噪音、区域泛财经通讯与模板化荐股软文（不影响真实美股/宏观/央行/自选股新闻）
_NOISE_NEWS_TITLE_RE = re.compile(
    r"(?:\b3\s+key\s+takeaways\s+for\s+long-term\s+investors\b|"
    r"\binside\s+india\s+newsletter\b|"
    r"\bk-beauty\s+stocks\b|"
    r"\bshould\s+you\s+(?:buy|avoid)\b|\bhere.?s\s+why\s+i.?m\s+buying\b|\bbetter\s+buy\s*:|"
    r"\bi\s+think\s+it.?s\s+a\s+buy\b|\bhas\s+turned\s+\$|\bweek\s+in\s+review\b|"
    r"^(?:opinion|commentary|cramer|liesman)\s*:|"
    r"\bjim\s+cramer.?s\b|\bcramer.?s\s+lightning\s+round\b|"
    r"\b(?:kospi|nikkei|hang\s+seng|asx\s+200|ftse\s+100)\s+(?:leads|climbs|drops|falls|rises)\b.*\basia(?:-pacific)?\s+markets\b|"
    r"\blifts\s+asia\s+markets\b|\beuropean\s+markets\s+(?:open|close|head)\b)",
    re.IGNORECASE,
)

# 屏蔽大盘收盘综述与软文抢占 Exa /contents 与 /search 补全额度（确保 100% 留给 WSJ 独家与硬催化）
_SKIP_EXA_TITLE_RE = re.compile(
    r"(tipped\s+to\s+open|lower\s+open\s+predicted|should\s+you\s+(?:buy|avoid)|"
    r"here.?s\s+why\s+i.?m\s+buying|i\s+think\s+it.?s\s+a\s+buy|has\s+turned\s+\$|"
    r"market\s+sharply\s+lower|market\s+may|stock\s+market\s+tipped|"
    r"stock\s+market\s+news|stocks\s+(?:finish|close|pare|slide|rally|rebound|slip|rise|fall)|"
    r"wall\s+street\s+can.?t|live\s*coverage|week\s+in\s+review)",
    re.IGNORECASE,
)


def _match_watchlist_hits(title: str, text: str, wl_syms: list[str] | None) -> list[str]:
    """检测新闻标题或正文是否命中个人自选股池（Ticker 或品牌名），返回去重后的代码列表。"""
    if not wl_syms:
        return []
    blob = f"{title}\n{text[:1000]}"
    low = blob.lower()
    hits = []
    for sym in wl_syms:
        s = sym.upper()
        matched = False
        if len(s) >= 3 and re.search(rf"\b{re.escape(s)}\b", blob):
            matched = True
        else:
            for alias in _US_BRAND_ALIASES.get(s, ()):
                if alias in low:
                    matched = True
                    break
        if matched:
            hits.append(s)
    return hits


def build_news_context(
    entries: list,
    custom_wl_syms: list[str] | None = None,
    today_ledger_urls: set | None = None,
    session_str: str = "",
) -> tuple:
    """整理新闻素材，返回 (news_lines, kept_per_source, drop_stats)。

    过滤顺序：标题/URL 缺失 → 单次运行内 URL 去重 → 跨天已播去重 → 今日跨 Bot 账本 URL 去重 → 噪音/专栏/模板文过滤 → 48h 时间窗。
    """
    now        = datetime.now(timezone.utc)
    time_limit = now - timedelta(days=2)
    seen_urls: set = set()
    sent_before    = load_sent_urls(SENT_URLS)
    ledger_urls    = today_ledger_urls or set()

    kept_per_source: dict = {}
    drops = {"dup": 0, "already_sent": 0, "stale": 0, "off_topic": 0, "ledger_dup": 0}
    picked = []

    for entry in entries:
        title = getattr(entry, "title", None)
        if not title:
            continue
        url = getattr(entry, "link", "") or getattr(entry, "id", "")
        uk = url_key(url)
        if not uk or uk in seen_urls:
            drops["dup"] += 1
            continue
        seen_urls.add(uk)
        if uk in sent_before:
            drops["already_sent"] += 1
            continue
        if uk in ledger_urls:
            drops["ledger_dup"] += 1
            continue
        if _NOISE_NEWS_TITLE_RE.search(title):
            drops["off_topic"] += 1
            continue
        pub = parse_entry_date(entry)
        if not pub or pub < time_limit:
            drops["stale"] += 1
            continue
        pub_date = pub.strftime("%Y-%m-%d")
        snippet = getattr(entry, "summary", "") or ""
        src = getattr(entry, "__src", "?")
        kept_per_source[src] = kept_per_source.get(src, 0) + 1
        picked.append((title, url, src, snippet, pub_date))

    def _material(item):
        title, url, src, snippet, pub_date = item
        body = fetch_article_text(url)
        text = body if body else snippet[:500]
        kind = "正文" if body else "摘要"
        return {"src": src, "title": title, "url": url, "text": text, "kind": kind, "pub_date": pub_date}

    lines = []
    if picked:
        with ThreadPoolExecutor(max_workers=8) as ex:
            rows = list(ex.map(_material, picked))

        # Exa 轻量补全：按宏观/信用/自选股/聪明钱信号优先级排序，优先补全核心 PCE、对冲基金爆仓与 WSJ 独家
        if exa_enrich_enabled(default_enabled=True):
            _EXA_HIGH_SIGNAL_RE = re.compile(
                r"\b(?:PCE|CPI(?!-W)|inflation|Fed(?:eral\s+Reserve)?|Powell|Warsh|tariff|CDS|credit|"
                r"downgrade|hedge\s+fund|lost\s+\d+%|Treasury\s+auction|SEC|DOJ|FCC|antitrust|"
                r"earnings|guidance|CapEx|billion|rate\s+(?:hike|cut))\b",
                re.IGNORECASE,
            )

            def _exa_row_prio(row: dict) -> tuple[int, int, int]:
                hits_wl = bool(_match_watchlist_hits(row["title"], row["text"], custom_wl_syms))
                hits_sig = bool(_EXA_HIGH_SIGNAL_RE.search(row["title"] + " " + row["text"][:200]))
                sig_rank = 0 if (hits_wl or hits_sig) else 1
                src_name = row["src"]
                src_rank = 0 if ("dowjones" in src_name or "wsj" in src_name) else (1 if "cnbc" in src_name else 2)
                return (sig_rank, src_rank, len(row["text"]))

            short_rows = [
                r for r in rows
                if r["kind"] == "摘要"
                and len(r["text"].strip()) < 350
                and not _SKIP_EXA_TITLE_RE.search(r["title"])
            ]
            short_rows.sort(key=_exa_row_prio)
            target_rows = short_rows[:8]
            if target_rows:
                exa_bodies = exa_fetch_contents([r["url"] for r in target_rows], max_chars=1800)
                upgraded = 0
                for r in target_rows:
                    got = exa_bodies.get(url_key(r["url"]))
                    if got and len(got) >= 300:
                        r["text"] = got
                        r["kind"] = "Exa补全正文"
                        upgraded += 1
                if upgraded:
                    print(f"  ✓ Exa /contents 补全 {upgraded}/{len(target_rows)} 条短摘要正文", file=sys.stderr)

                still_short = [r for r in target_rows if len(r["text"].strip()) < 280][:5]
                if still_short:
                    since_iso = (now - timedelta(days=2)).strftime("%Y-%m-%d")
                    _EARNINGS_RE = re.compile(
                        r"\b(?:earnings|quarterly results|Q[1-4]\s+(?:results|earnings|revenue)|FY\d{2,4}|"
                        r"revenue|guidance|10-[KQ]|8-K|6-K|EPS|beats?|misses?)\b",
                        re.IGNORECASE,
                    )
                    _NON_CORP_FILING_RE = re.compile(
                        r"\b(?:hedge fund|tells investors|investor letter|lost\s+\d+%)\b",
                        re.IGNORECASE,
                    )
                    _EXA_NOISE_DOMAINS = [
                        "analystmarkets.com", "newser.ai", "newser.com", "marketbeat.com", "fool.com",
                    ]
                    _TITLE_STOP = {
                        "this", "that", "with", "from", "into", "over", "after", "before", "about",
                        "their", "they", "them", "what", "when", "where", "which", "while", "will",
                        "would", "could", "should", "have", "has", "been", "being", "more", "most",
                        "than", "then", "says", "said", "tells", "report", "reports", "news",
                        "first", "second", "third", "fourth", "quarter", "year", "years", "week",
                        "weeks", "month", "months", "days", "today", "stock", "stocks", "market",
                        "markets", "shares", "price", "prices", "investors", "company", "firm",
                        "other", "under", "amid", "says", "wall", "street",
                    }

                    def _hit_matches_title(orig_title: str, hit_title: str, hit_body: str) -> bool:
                        toks = [
                            w.lower() for w in re.findall(r"[A-Za-z][A-Za-z0-9]{3,}", orig_title)
                            if w.lower() not in _TITLE_STOP
                        ]
                        if not toks:
                            return True
                        hay = (hit_title + " " + hit_body[:500]).lower()
                        matched = sum(1 for t in set(toks) if t in hay)
                        need = 2 if len(set(toks)) >= 3 else 1
                        return matched >= need

                    searched = 0
                    for r in still_short:
                        is_fin_report = (
                            (
                                "earnings" in r["src"].lower()
                                or "15839135" in r["src"]
                                or bool(_EARNINGS_RE.search(r["title"]))
                            )
                            and not _NON_CORP_FILING_RE.search(r["title"])
                        )
                        hits = []
                        if is_fin_report:
                            hits = exa_search_fallback(
                                r["title"],
                                num_results=2,
                                max_chars=900,
                                category="financial report",
                                start_published_date=since_iso,
                                exclude_domains=_EXA_NOISE_DOMAINS,
                            )
                        if not hits:
                            is_fin_report = False
                            hits = exa_search_fallback(
                                r["title"],
                                num_results=2,
                                max_chars=900,
                                category="news",
                                start_published_date=since_iso,
                                exclude_domains=_EXA_NOISE_DOMAINS,
                            )
                        snippets = []
                        for h in hits:
                            raw_t = (h.get("text") or "").strip()
                            # 清理 Markdown 表格线、图片标签与 AI 免责声明，确保留存有效正文
                            clean_t = re.sub(r"!\[[^\]]*\]\([^)]*\)|\[[^\]]*\]\([^)]*\)|[#|*`]{2,}", " ", raw_t)
                            clean_t = re.sub(r"\s+", " ", clean_t).strip()
                            if (
                                len(clean_t) >= 120
                                and "ai-generated" not in clean_t.lower()
                                and "gemini 3" not in clean_t.lower()
                                and _hit_matches_title(r["title"], h.get("title", ""), clean_t)
                            ):
                                snippets.append(f"{h['title']}：{clean_t[:460]}")
                        if snippets:
                            r["text"] = f"{r['text']}\n[Exa补充背景] " + " | ".join(snippets)
                            r["kind"] = "摘要+Exa财报补充" if is_fin_report else "摘要+Exa补充"
                            searched += 1
                    if searched:
                        print(f"  ✓ Exa /search 补全 {searched}/{len(still_short)} 条付费墙/短摘要背景", file=sys.stderr)

        MIN_USABLE = 50
        _EARNINGS_STALE_RE = re.compile(r"\b(?:earnings|quarter|Q[1-4]|FY\d{2,4}|same-store|beats?|misses?|guidance)\b", re.I)
        for r in rows:
            if len(r["text"].strip()) < MIN_USABLE:
                drops["no_content"] = drops.get("no_content", 0) + 1
                kept_per_source[r["src"]] = max(0, kept_per_source.get(r["src"], 1) - 1)
                continue
            wl_hits = _match_watchlist_hits(r["title"], r["text"], custom_wl_syms)
            wl_tag = f"\n[命中AI自选股] {', '.join(wl_hits)}" if wl_hits else ""
            stale_earn_tag = ""
            if session_str and r.get("pub_date") and r["pub_date"] < session_str and _EARNINGS_STALE_RE.search(r["title"]) and not wl_hits:
                stale_earn_tag = f"\n[⚠️ T-1 滞后财报提示] 发布于 {r['pub_date']}（早于收盘日 {session_str}），中小盘请勿入选"
            lines.append(
                f"[原始英文标题] {r['title']}\n[发布日期] {r.get('pub_date', '?')}\n[链接] {r['url']}\n[媒体] {r['src']}{wl_tag}{stale_earn_tag}\n"
                f"[正文/摘要（{r['kind']}）] {r['text']}\n----"
            )
    return lines, kept_per_source, drops


_US_BRAND_ALIASES = {
    "NVDA": ("nvidia",), "AAPL": ("apple",), "MSFT": ("microsoft",), "AVGO": ("broadcom",),
    "AMD": ("advanced micro devices",), "ORCL": ("oracle",), "CRM": ("salesforce",),
    "MU": ("micron",), "QCOM": ("qualcomm",), "PLTR": ("palantir",), "ADBE": ("adobe",),
    "INTC": ("intel",), "SMCI": ("super micro", "supermicro"), "ARM": ("arm holdings",),
    "TSM": ("tsmc", "taiwan semiconductor"), "META": ("meta platforms", "facebook"),
    "GOOGL": ("alphabet", "google"), "NFLX": ("netflix",), "TMUS": ("t-mobile",),
    "DIS": ("disney",), "VZ": ("verizon",), "AMZN": ("amazon",), "TSLA": ("tesla",),
    "HD": ("home depot",), "MCD": ("mcdonald",), "NKE": ("nike",), "BKNG": ("booking",),
    "WMT": ("walmart",), "COST": ("costco",), "PG": ("procter",), "KO": ("coca-cola",),
    "PEP": ("pepsico",), "PM": ("philip morris",), "XOM": ("exxon",), "CVX": ("chevron",),
    "COP": ("conocophillips",), "SLB": ("schlumberger",), "EOG": ("eog resources",),
    "OXY": ("occidental",), "BRK.B": ("berkshire",), "JPM": ("jpmorgan", "jp morgan"),
    "V": ("visa",), "MA": ("mastercard",), "GS": ("goldman sachs",), "COIN": ("coinbase",),
    "MSTR": ("microstrategy", "strategy inc"), "LLY": ("eli lilly", "lilly"),
    "UNH": ("unitedhealth",), "JNJ": ("johnson & johnson",), "ABBV": ("abbvie",),
    "MRK": ("merck",), "ISRG": ("intuitive surgical",), "GE": ("ge aerospace",),
    "CAT": ("caterpillar",), "RTX": ("raytheon", "rtx corp"), "UNP": ("union pacific",),
    "HON": ("honeywell",), "ETN": ("eaton",), "LIN": ("linde",), "NEM": ("newmont",),
    "FCX": ("freeport",), "SHW": ("sherwin-williams",), "APD": ("air products",),
    "NUE": ("nucor",), "PLD": ("prologis",), "AMT": ("american tower",),
    "EQIX": ("equinix",), "WELL": ("welltower",), "SPG": ("simon property",),
    "O": ("realty income",), "NEE": ("nextera",), "CEG": ("constellation energy",),
    "VST": ("vistra",), "SO": ("southern company",), "DUK": ("duke energy",),
    "AEP": ("american electric",),
    "GEV": ("ge vernova",), "VRT": ("vertiv",), "ANET": ("arista networks", "arista"),
    "CRWV": ("coreweave",), "NBIS": ("nebius",), "ANT": ("anthropic",),
    "MRVL": ("marvell",), "ASML": ("asml",), "NOW": ("servicenow",),
    "CRWD": ("crowdstrike",),
}


def _select_uncovered_us_movers(
    quotes: dict,
    news_lines: list,
    max_total: int = 6,
    custom_wl: dict | None = None,
) -> list:
    """挑出 RSS 新闻未覆盖、但在领涨/领跌板块 Top 3、全市场权重或个人自选股中波动显著的美股标的。"""
    news_blob = "\n".join(news_lines)
    news_low = news_blob.lower()

    def _is_covered(sym: str) -> bool:
        if len(sym) >= 3 and re.search(rf"\b{re.escape(sym)}\b", news_blob):
            return True
        for alias in _US_BRAND_ALIASES.get(sym, ()):
            if alias in news_low:
                return True
        return False

    # 汇总领涨/领跌 Top 3 板块内各自 Top 3 成分股 + 全市场 WATCHLIST 涨跌前 5 + 个人自选股显著异动
    sec_rows = []
    for sym, name in SECTORS:
        qq = quotes.get(sym)
        if qq and _num(qq.get("change_pct")) is not None:
            sec_rows.append((name, sym, _num(qq.get("change_pct"))))
    sec_rows.sort(key=lambda r: -r[2])
    focus_secs = [(n, s, True) for n, s, _ in sec_rows[:3]] + [(n, s, False) for n, s, _ in sec_rows[-3:]]

    sym_cn = {m_sym: m_cn for members in SECTOR_STOCKS.values() for m_sym, m_cn in members}
    if custom_wl:
        for g in custom_wl.get("groups") or []:
            for t in g.get("tickers") or []:
                sym_cn.setdefault(t["symbol"], t.get("name") or t["symbol"])

    candidates = {}

    def _consider(sym: str, sector_label: str, priority: bool = False):
        qq = quotes.get(sym)
        pct = _num(qq.get("change_pct")) if qq else None
        if pct is None or _is_covered(sym):
            return
        if sym not in candidates:
            candidates[sym] = {
                "symbol": sym,
                "name": sym_cn.get(sym) or qq.get("shortName") or sym,
                "pct": pct,
                "pct_change": qq.get("change_pct", f"{pct:+.2f}%"),
                "sector": sector_label,
                "priority": priority,
            }
        elif priority:
            candidates[sym]["priority"] = True

    for sec_name, sec_sym, is_gainer in focus_secs:
        members = []
        for m_sym, _ in SECTOR_STOCKS.get(sec_sym, []):
            mq = quotes.get(m_sym)
            p = _num(mq.get("change_pct")) if mq else None
            if p is not None:
                if (not is_gainer and p < 0) or (is_gainer and p > 0):
                    members.append((m_sym, p))
        members.sort(key=lambda x: x[1], reverse=is_gainer)
        for m_sym, _ in members[:3]:
            _consider(m_sym, f"{sec_name}({sec_sym})")

    mv = []
    for sym in WATCHLIST:
        qq = quotes.get(sym)
        p = _num(qq.get("change_pct")) if qq else None
        if p is not None:
            mv.append((sym, p))
    mv.sort(key=lambda x: -x[1])
    for sym, _ in mv[:5] + mv[-5:]:
        _consider(sym, "全市场焦点权重")

    # 个人自选股（如 AI 自选股 29 只）单日波动 >= 2.5% 且无 RSS 覆盖时优先纳入补盲池
    if custom_wl:
        wl_name = custom_wl.get("name") or "自选股"
        for g in custom_wl.get("groups") or []:
            g_lbl = f"{wl_name}-{g.get('name', '')}"
            for t in g.get("tickers") or []:
                sym = t["symbol"]
                qq = quotes.get(sym)
                p = _num(qq.get("change_pct")) if qq else None
                if p is not None and abs(p) >= 2.5:
                    _consider(sym, g_lbl, priority=True)

    all_rows = [r for r in candidates.values() if abs(r["pct"]) >= 1.5]
    if not all_rows:
        all_rows = sorted(candidates.values(), key=lambda r: -abs(r["pct"]))[:3]

    gainers = sorted([r for r in all_rows if r["pct"] > 0], key=lambda r: (not r["priority"], -r["pct"]))
    losers = sorted([r for r in all_rows if r["pct"] < 0], key=lambda r: (not r["priority"], r["pct"]))

    # 兼顾涨跌两端：最多取 2 只领涨未覆盖股 + 4 只领跌未覆盖股（自选股大波动优先），空余名额按绝对涨幅补足
    picked = gainers[:2] + losers[:4]
    if len(picked) < max_total:
        used = {r["symbol"] for r in picked}
        rest = sorted([r for r in all_rows if r["symbol"] not in used], key=lambda r: (not r["priority"], -abs(r["pct"])))
        picked.extend(rest[: max_total - len(picked)])

    return [
        {
            "symbol": r["symbol"],
            "name": r["name"],
            "pct_change": r["pct_change"],
            "sector": r["sector"],
        }
        for r in picked[:max_total]
    ]


# ===== 推送飞书 =====
def send_report(text: str) -> int:
    """推送一份稿件，返回实际发出的消息条数。

    sanitize 一次、在转换前完成：HTML 只是内部中间格式，清洗保证正文里的裸
    < > & 不会被标签解析吃掉。转 post、按 20KB 分页、失败重试都由
    bot_utils.send_feishu 统一负责（三个 bot 共用同一实现）。"""
    return send_feishu(sanitize_html(text), FEISHU_WEBHOOK)


# ===== 代理预检 + 端口自愈 =====
def _proxy_ok() -> bool:
    """代理预检 + 端口自愈，实现见 shared/bot_utils.proxy_ok。"""
    global _PROXY
    ok, _PROXY = proxy_ok(_PROXY, SESSION)
    return ok



# ===== 双维度均线趋势、波动率与量比（2026-11 买方量化升级版）=====
_US_MA_MAP = {
    ".SPX": ("us.INX", "标普500"),
    ".IXIC": ("us.IXIC", "纳斯达克"),
    ".RUT": ("usIWM.AM", "罗素2000(IWM)"),
}


def _compute_rsi14(closes: list[float]) -> float | None:
    """按 Wilder 平滑法计算 14 日 RSI。"""
    if len(closes) < 15:
        return None
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(d, 0.0) for d in deltas]
    losses = [max(-d, 0.0) for d in deltas]
    avg_gain = sum(gains[:14]) / 14.0
    avg_loss = sum(losses[:14]) / 14.0
    for g, l in zip(gains[14:], losses[14:]):
        avg_gain = (avg_gain * 13.0 + g) / 14.0
        avg_loss = (avg_loss * 13.0 + l) / 14.0
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _compute_bollinger_20(closes: list[float]) -> tuple[float, float, float, float] | None:
    """计算 20 日布林带 (mid, upper, lower, pct_b)，标准差取 2σ。"""
    if len(closes) < 20:
        return None
    win = closes[-20:]
    mid = sum(win) / 20.0
    var = sum((x - mid) ** 2 for x in win) / 20.0
    std = var ** 0.5
    upper = mid + 2.0 * std
    lower = mid - 2.0 * std
    pct_b = (closes[-1] - lower) / (upper - lower) if upper > lower else 0.5
    return mid, upper, lower, pct_b


def _compute_hv20(closes: list[float]) -> float | None:
    """计算 20 日年化对数收益已实现波动率 HV20（单位：%）。"""
    if len(closes) < 21:
        return None
    win = closes[-21:]
    rets = []
    for i in range(1, len(win)):
        if win[i - 1] > 0 and win[i] > 0:
            rets.append(math.log(win[i] / win[i - 1]))
    if len(rets) < 15:
        return None
    mean_r = sum(rets) / len(rets)
    var = sum((r - mean_r) ** 2 for r in rets) / max(1, len(rets) - 1)
    return (var ** 0.5) * math.sqrt(252.0) * 100.0


def fetch_us_ma_trends_detail(session_str: str = "", vix_val: float | None = None) -> tuple[dict, dict]:
    """拉取腾讯 220 日美股日 K，计算双维度趋势(MA50/MA200 + MA20/HV20标准化Z-score)、HV20/VRP、RVOL、RSI(14) 与布林 %B，并校验 K 线日期。"""
    out: dict = {}
    rvols: dict = {}
    s = requests.Session()
    s.trust_env = False
    s.proxies = {}
    for cnbc_sym, (gt_code, label) in _US_MA_MAP.items():
        klines = None
        last_err = None
        for ep in ("usfqkline/get", "fqkline/get"):
            try:
                url = f"https://web.ifzq.gtimg.cn/appstock/app/{ep}?param={gt_code},day,,,220,qfq"
                resp = s.get(url, headers={"User-Agent": _UA}, timeout=8)
                if resp.status_code != 200:
                    last_err = f"HTTP {resp.status_code}"
                    continue
                node = resp.json().get("data", {}).get(gt_code, {})
                cand = node.get("qfqday") or node.get("day") or []
                if cand:
                    klines = cand
                    break
            except Exception as exc:
                last_err = str(exc)
        if not klines:
            print(f"[WARN] {label}({gt_code}) 日K抓取失败: {last_err}", file=sys.stderr)
            continue

        try:
            last_bar_date = str(klines[-1][0])[:10] if klines and len(klines[-1]) >= 1 else ""
            date_warn = ""
            if session_str and last_bar_date and last_bar_date != session_str:
                print(f"[WARN] {label}({gt_code}) K线末日 {last_bar_date} != CNBC收盘日 {session_str}", file=sys.stderr)
                date_warn = f"⚠️K线滞后({last_bar_date}) | "

            closes = []
            for r in klines:
                if len(r) >= 3 and r[2]:
                    cv = _num(r[2])
                    if cv is not None and cv > 0:
                        closes.append(cv)
            if len(closes) < 20:
                continue

            cur = closes[-1]
            ma20 = sum(closes[-20:]) / 20.0
            bias20 = (cur - ma20) / ma20 * 100.0 if ma20 else 0.0
            ma50 = sum(closes[-50:]) / 50.0 if len(closes) >= 50 else None
            ma200 = sum(closes[-200:]) / 200.0 if len(closes) >= 200 else None

            if ma50 is not None and ma200 is not None:
                if cur >= ma50 and ma50 >= ma200:
                    mid_trend = "🟢中期多头(MA50>MA200)"
                elif cur < ma200:
                    mid_trend = "🔴中期空头(跌破年线)"
                elif cur < ma50:
                    mid_trend = "🟡中期承压(MA50下方/年线上方)"
                else:
                    mid_trend = "🟡中期修复(MA50上方/MA50<MA200)"
            else:
                mid_trend = "🟡站上MA20" if cur >= ma20 else "🔴跌破MA20"

            # 20 日布林带与 20 日价格分布标准差 Z-score = (Close - MA20) / bb_std（与布林 %B 严格一致）
            bb = _compute_bollinger_20(closes)
            z_str = ""
            if bb is not None:
                _, upper, lower, pct_b = bb
                bb_std = (upper - lower) / 4.0
                if bb_std > 0:
                    z_val = (cur - ma20) / bb_std
                    z_str = f" · Z={z_val:+.2f}σ"

            hv20 = _compute_hv20(closes)
            hv_str = ""
            if hv20 is not None and hv20 > 0:
                if cnbc_sym == ".SPX" and vix_val is not None:
                    vrp = vix_val - hv20
                    hv_str = f"HV20={hv20:.1f}% / VRP={vrp:+.1f}pt"
                else:
                    hv_str = f"HV20={hv20:.1f}%"

            # 相对量比 RVOL：严格校验末根 K 线本身有有效成交量，杜绝零值前移错位
            rvol_str = ""
            last_vol = _num(klines[-1][5]) if len(klines[-1]) >= 6 else None
            if last_vol is not None and last_vol > 0 and len(klines) >= 21:
                prev_vols = []
                for r in klines[-21:-1]:
                    pv = _num(r[5]) if len(r) >= 6 else None
                    if pv is not None and pv > 0:
                        prev_vols.append(pv)
                if len(prev_vols) >= 15:
                    avg_v20 = sum(prev_vols) / len(prev_vols)
                    if avg_v20 > 0:
                        rv = last_vol / avg_v20
                        rvols[cnbc_sym] = rv
                        rvol_str = f"RVOL={rv:.2f}x"

            extra_parts = []
            if ma50 is not None:
                d50 = (cur - ma50) / ma50 * 100.0
                extra_parts.append(f"MA50={ma50:,.1f}({d50:+.1f}%)")
            if ma200 is not None:
                d200 = (cur - ma200) / ma200 * 100.0
                pos200 = "年线上方" if cur >= ma200 else "跌破年线"
                extra_parts.append(f"MA200={ma200:,.1f}({d200:+.1f}%/{pos200})")
            if hv_str:
                extra_parts.append(hv_str)
            if rvol_str:
                extra_parts.append(rvol_str)

            rsi14 = _compute_rsi14(closes)
            if rsi14 is not None:
                if rsi14 >= 70:
                    rsi_lbl = "超买警惕"
                elif rsi14 >= 60:
                    rsi_lbl = "偏强"
                elif rsi14 <= 30:
                    rsi_lbl = "超卖反弹区"
                elif rsi14 <= 40:
                    rsi_lbl = "偏弱近超卖"
                else:
                    rsi_lbl = "中性"
                extra_parts.append(f"RSI(14)={rsi14:.1f}({rsi_lbl})")

            if bb is not None:
                _, upper, lower, pct_b = bb
                pct_b_r = round(pct_b, 2)
                if pct_b_r > 1.0:
                    bb_lbl = "破上轨"
                elif pct_b_r >= 0.8:
                    bb_lbl = "近上轨"
                elif pct_b_r < 0.0:
                    bb_lbl = "破下轨"
                elif pct_b_r <= 0.2:
                    bb_lbl = "近下轨支撑"
                else:
                    bb_lbl = "中轨区间"
                extra_parts.append(f"布林%B={pct_b_r:.2f}({bb_lbl}, 上轨{upper:,.1f}/下轨{lower:,.1f})")

            extra_str = (" | " + " | ".join(extra_parts)) if extra_parts else ""
            out[cnbc_sym] = (
                f"{date_warn}{mid_trend} | MA20={ma20:,.2f}({bias20:+.2f}%{z_str}){extra_str}"
            )
        except Exception as exc:
            print(f"[WARN] {label}({gt_code}) 日K解析计算异常: {exc}", file=sys.stderr)
            continue
    return out, rvols


def fetch_us_ma_trends(session_str: str = "", vix_val: float | None = None) -> dict:
    """兼容旧签名，返回 {cnbc_sym: trend_str}。"""
    trends, _ = fetch_us_ma_trends_detail(session_str=session_str, vix_val=vix_val)
    return trends


# ===== Financial Events（当日已公布实际值 + NYSE 休市/半日市 + 未来 5 交易日财报与宏观）=====
_NYSE_HOLIDAYS = {
    "2026-01-01": "元旦",
    "2026-01-19": "马丁路德金纪念日",
    "2026-02-16": "总统日",
    "2026-04-03": "耶稣受难日",
    "2026-05-25": "阵亡将士纪念日",
    "2026-06-19": "六月节",
    "2026-07-03": "独立日补休",
    "2026-09-07": "劳动节",
    "2026-11-26": "感恩节",
    "2026-12-25": "圣诞节",
    "2027-01-01": "元旦",
    "2027-01-18": "马丁路德金纪念日",
    "2027-02-15": "总统日",
    "2027-03-26": "耶稣受难日",
    "2027-05-31": "阵亡将士纪念日",
    "2027-06-18": "六月节补休",
    "2027-07-05": "独立日补休",
    "2027-09-06": "劳动节",
    "2027-11-25": "感恩节",
    "2027-12-24": "圣诞节补休",
}
_NYSE_HALF_DAYS = {
    "2026-11-27": "黑色星期五（美东 13:00 提前收盘）",
    "2026-12-24": "平安夜（美东 13:00 提前收盘）",
    "2027-11-26": "黑色星期五（美东 13:00 提前收盘）",
}

_MACRO_EVENT_RE = re.compile(
    r"\b(?:FOMC|Federal Funds|Fed Interest Rate|Fed Chair|"
    r"CPI|Consumer Price Index|Core CPI|"
    r"PCE|Personal Consumption Expenditures|Core PCE|"
    r"Nonfarm Payrolls|Non-Farm Payrolls|Unemployment Rate|"
    r"GDP|Gross Domestic Product|"
    r"ISM Manufacturing|ISM Services|ISM Non-Manufacturing|"
    r"Retail Sales|PPI|Producer Price Index|CB Consumer Confidence|"
    r"(?:10|20|30)-Year\s+(?:Note|Bond)\s+Auction)\b",
    re.IGNORECASE,
)
# 仅过滤短期国库券 Bill Auction 与周度零售/油气噪音，保留 10Y/20Y/30Y 中长端美债拍卖
_MACRO_NOISE_RE = re.compile(r"\b(?:Redbook|Chain Store|MBA|EIA|API|Bill Auction|Baker Hughes)\b", re.IGNORECASE)


def _parse_market_cap(raw: str) -> float:
    if not raw:
        return 0.0
    s = str(raw).replace("$", "").replace(",", "").strip().upper()
    mult = 1.0
    if s.endswith("T"):
        mult, s = 1e12, s[:-1]
    elif s.endswith("B"):
        mult, s = 1e9, s[:-1]
    elif s.endswith("M"):
        mult, s = 1e6, s[:-1]
    try:
        return float(s) * mult
    except ValueError:
        return 0.0


def _canonical_macro_name(ev_name: str) -> tuple[str, str]:
    low_ev = ev_name.lower()
    if "core pce" in low_ev:
        return "core_pce", "美国核心 PCE"
    if "pce" in low_ev or "personal consumption" in low_ev:
        return "pce", "美国 PCE"
    if "core cpi" in low_ev:
        return "core_cpi", "美国核心 CPI"
    if "cpi" in low_ev or "consumer price" in low_ev:
        return "cpi", "美国 CPI"
    if "nonfarm" in low_ev or "non-farm" in low_ev:
        return "nfp", "美国非农就业"
    if "unemployment" in low_ev:
        return "unemp", "美国失业率"
    if "gdp" in low_ev or "gross domestic" in low_ev:
        return "gdp", "美国 GDP"
    if "ism manufacturing" in low_ev:
        return "ism_mfg", "ISM 制造业 PMI"
    if "ism services" in low_ev or "ism non-manufacturing" in low_ev:
        return "ism_svc", "ISM 服务业 PMI"
    if "retail sales" in low_ev:
        return "retail", "美国零售销售"
    if "ppi" in low_ev or "producer price" in low_ev:
        return "ppi", "美国 PPI"
    if "cb consumer confidence" in low_ev:
        return "cb_conf", "美国谘商会消费者信心"
    if "auction" in low_ev:
        return re.sub(r"\s+", " ", low_ev), ev_name
    if "fomc" in low_ev or "federal funds" in low_ev or "fed interest rate" in low_ev or "fed chair" in low_ev:
        return re.sub(r"\s+", " ", low_ev), ev_name
    key = re.sub(r"\s*\([^)]*\)|\b(?:MoM|YoY|QoQ|s\.a\.|n\.s\.a\.|Prelim|Final|Rev)\b", "", low_ev, flags=re.I).strip()
    return key, ev_name


def fetch_financial_events(
    session_str: str,
    custom_wl_syms: set[str] | None = None,
    days_ahead: int = 5,
) -> dict:
    """从 Nasdaq Calendar API 并行拉取：① 收盘日已公布核心宏观实际值 ② 次日 NYSE 休市/半日市提醒 ③ 未来 5 个交易日重大财报与宏观日程。"""
    custom_wl = {s.upper() for s in (custom_wl_syms or set())}
    priority_syms = custom_wl | {s.upper() for s in WATCHLIST}

    try:
        base_dt = datetime.strptime(session_str, "%Y-%m-%d")
    except Exception:
        base_dt = datetime.now()

    # 检查未来 4 个自然日内是否有 NYSE 休市或 13:00 ET 半日市
    holiday_alerts: list[str] = []
    for offset in range(1, 5):
        cand_s = (base_dt + timedelta(days=offset)).strftime("%Y-%m-%d")
        mmdd = cand_s[5:]
        if cand_s in _NYSE_HOLIDAYS:
            holiday_alerts.append(f"⚠️ {mmdd} {_NYSE_HOLIDAYS[cand_s]}美股休市")
        elif cand_s in _NYSE_HALF_DAYS:
            holiday_alerts.append(f"⏰ {mmdd} {_NYSE_HALF_DAYS[cand_s]}")

    target_dates: list[str] = []
    cur = base_dt
    while len(target_dates) < days_ahead:
        cur += timedelta(days=1)
        d_s = cur.strftime("%Y-%m-%d")
        if cur.weekday() < 5 and d_s not in _NYSE_HOLIDAYS:
            target_dates.append(d_s)

    headers = {
        "User-Agent": _UA,
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://www.nasdaq.com",
        "Referer": "https://www.nasdaq.com/",
    }

    def _fetch_today_actuals(dt_str: str) -> list[dict]:
        out_act: list[dict] = []
        if not dt_str:
            return out_act
        try:
            r = SESSION.get(
                f"https://api.nasdaq.com/api/calendar/economicevents?date={dt_str}",
                headers=headers,
                timeout=12,
            )
            if r.status_code != 200:
                return out_act
            rows = (r.json().get("data") or {}).get("rows") or []
            seen: set[str] = set()
            for row in rows:
                country = str(row.get("country") or "").strip()
                if country.lower() not in ("united states", "us", "u.s."):
                    continue
                ev_name = html.unescape(str(row.get("eventName") or "")).strip()
                if not ev_name or _MACRO_NOISE_RE.search(ev_name) or not _MACRO_EVENT_RE.search(ev_name):
                    continue
                actual = html.unescape(str(row.get("actual") or "")).replace("\xa0", "").strip()
                if not actual or actual in ("-", "--", "N/A", "n/a"):
                    continue
                key, disp_ev = _canonical_macro_name(ev_name)
                if key in seen:
                    continue
                seen.add(key)
                cons = html.unescape(str(row.get("consensus") or "")).replace("\xa0", "").strip()
                prev = html.unescape(str(row.get("previous") or "")).replace("\xa0", "").strip()
                out_act.append({"date": dt_str, "event": disp_ev, "actual": actual, "consensus": cons, "previous": prev})
        except Exception:
            pass
        return out_act

    def _fetch_day(dt_str: str) -> tuple[list[dict], list[dict]]:
        day_earn: list[dict] = []
        day_econ: list[dict] = []
        try:
            r = SESSION.get(
                f"https://api.nasdaq.com/api/calendar/earnings?date={dt_str}",
                headers=headers,
                timeout=12,
            )
            if r.status_code == 200:
                rows = (r.json().get("data") or {}).get("rows") or []
                for row in rows:
                    sym = str(row.get("symbol") or "").strip().upper()
                    if not sym:
                        continue
                    mcap = _parse_market_cap(row.get("marketCap") or "")
                    in_custom = sym in custom_wl
                    in_prio = sym in priority_syms
                    if not in_prio and mcap < 50_000_000_000:
                        continue
                    t_raw = str(row.get("time") or "").lower()
                    t_lbl = "盘前" if "pre" in t_raw else ("盘后" if "after" in t_raw else "")
                    day_earn.append({
                        "date": dt_str,
                        "symbol": sym,
                        "name": str(row.get("name") or sym).strip(),
                        "market_cap": mcap,
                        "time": t_lbl,
                        "eps_forecast": str(row.get("epsForecast") or "").strip(),
                        "is_custom": in_custom,
                    })
        except Exception:
            pass

        try:
            r2 = SESSION.get(
                f"https://api.nasdaq.com/api/calendar/economicevents?date={dt_str}",
                headers=headers,
                timeout=12,
            )
            if r2.status_code == 200:
                rows2 = (r2.json().get("data") or {}).get("rows") or []
                seen_ev: set[str] = set()
                for row in rows2:
                    country = str(row.get("country") or "").strip()
                    if country.lower() not in ("united states", "us", "u.s."):
                        continue
                    ev_name = html.unescape(str(row.get("eventName") or "")).strip()
                    if not ev_name or _MACRO_NOISE_RE.search(ev_name) or not _MACRO_EVENT_RE.search(ev_name):
                        continue
                    actual = html.unescape(str(row.get("actual") or "")).replace("\xa0", "").strip()
                    if actual and actual not in ("-", "--", "N/A", "n/a"):
                        continue
                    key, disp_ev = _canonical_macro_name(ev_name)
                    if key in seen_ev:
                        continue
                    seen_ev.add(key)
                    cons = html.unescape(str(row.get("consensus") or "")).replace("\xa0", "").strip()
                    prev = html.unescape(str(row.get("previous") or "")).replace("\xa0", "").strip()
                    day_econ.append({
                        "date": dt_str,
                        "event": disp_ev,
                        "consensus": cons,
                        "previous": prev,
                    })
        except Exception:
            pass
        return day_earn, day_econ

    all_earnings: list[dict] = []
    all_macro: list[dict] = []
    today_actuals: list[dict] = []
    try:
        with ThreadPoolExecutor(max_workers=6) as ex:
            act_fut = ex.submit(_fetch_today_actuals, session_str)
            for e_list, m_list in ex.map(_fetch_day, target_dates):
                all_earnings.extend(e_list)
                all_macro.extend(m_list)
            today_actuals = act_fut.result()
    except Exception as e:
        print(f"[WARN] Financial Events 抓取失败: {e}", file=sys.stderr)

    _time_rank = {"盘前": 0, "": 1, "盘后": 2}
    seen_e: set[tuple[str, str]] = set()
    uniq_earnings = []
    for item in sorted(
        all_earnings,
        key=lambda x: (x["date"], _time_rank.get(x["time"], 1), not x["is_custom"], -x["market_cap"]),
    ):
        k = (item["date"], item["symbol"])
        if k not in seen_e:
            seen_e.add(k)
            uniq_earnings.append(item)

    all_macro.sort(key=lambda x: x["date"])
    return {
        "window": f"{target_dates[0]} ~ {target_dates[-1]}" if target_dates else "",
        "holiday_alerts": holiday_alerts,
        "today_actuals": today_actuals[:6],
        "earnings": uniq_earnings[:8],
        "macro": all_macro[:6],
    }


def _load_us_memory() -> list:
    try:
        if MARKET_MEMORY.exists():
            rows = json.loads(MARKET_MEMORY.read_text(encoding="utf-8"))
            if isinstance(rows, list):
                return rows
    except Exception:
        pass
    return []


def _format_us_memory_block() -> str:
    rows = _load_us_memory()[-MAX_MEMORY_DAYS:]
    if not rows:
        return "暂无历史盘面记忆（今日为首日建档）"
    lines = ["（写稿提示：请对比下方前几日盘面记忆，在板块轮动与核心叙事中点出某板块是“连续多日领涨/领跌”还是“单日高低切换反转”，并在信号灯与分析师观点中复盘前日关注方向与信号灯变化。）"]
    for r in rows:
        g_str = "、".join(r.get("top3_gainers", []))
        l_str = "、".join(r.get("top3_losers", []))
        lines.append(
            f"- [{r.get('session', '?')}] 指数：{r.get('indices', '?')}\n"
            f"  · 领涨Top3：{g_str} | 领跌Top3：{l_str}\n"
            f"  · 当日信号灯：{r.get('signal', '未知')} | 当日重点关注：{r.get('focus', '未知')}"
        )
    return "\n".join(lines)


def _extract_us_signal_and_focus(report_text: str) -> tuple:
    clean = re.sub(r"<[^>]+>", "", html.unescape(report_text))
    sig, focus = "未知", "未知"
    for raw_line in clean.splitlines():
        line = raw_line.strip().lstrip("·•- ")
        if line.startswith(("信号灯状态", "美股信号灯", "信号灯与")):
            sig = re.sub(r"^[^：:]+[：:]\s*", "", line)[:90]
        elif "重点关注" in line[:16] or "相对强弱关注" in line[:16] or line.startswith("关注方向"):
            focus = re.sub(r"^[^：:]+[：:]\s*", "", line)[:110]
    return sig, focus


def _format_custom_watchlist_block(custom_wl: dict | None, quotes: dict) -> str:
    """格式化自定义分层自选股汇总（单行总览 + 逐层成分股明细），无配置时返回空串。"""
    if not custom_wl:
        return ""
    group_summaries = []
    detail_lines = []
    all_items = []
    for g in custom_wl.get("groups") or []:
        g_name = g["name"]
        g_emoji = g.get("emoji", "•")
        g_pcts = []
        ticker_strs = []
        for t in g.get("tickers") or []:
            sym = t["symbol"]
            nm = t.get("name", sym)
            qq = quotes.get(sym)
            if qq and _num(qq.get("change_pct")) is not None:
                p_val = _num(qq.get("change_pct"))
                g_pcts.append(p_val)
                all_items.append((sym, nm, qq.get("last", "?"), qq.get("change_pct", "?"), p_val))
                ticker_strs.append(f"{sym} {qq.get('last', '?')} ({qq.get('change_pct', '?')})")
            else:
                ticker_strs.append(f"{sym} 未知")
        if g_pcts:
            avg_p = sum(g_pcts) / len(g_pcts)
            avg_str = f"{avg_p:+.2f}%"
            short_g = g_name.rstrip("层")
            group_summaries.append(f"{g_emoji} {short_g} {avg_str}")
            detail_lines.append(f"{g_emoji} {g_name}（均值 {avg_str}）：{' | '.join(ticker_strs)}")
        else:
            detail_lines.append(f"{g_emoji} {g_name}（均值 未知）：{' | '.join(ticker_strs)}")

    if not all_items:
        return ""
    up = sum(1 for x in all_items if x[4] > 0)
    down = sum(1 for x in all_items if x[4] < 0)
    flat = len(all_items) - up - down
    sorted_all = sorted(all_items, key=lambda x: x[4], reverse=True)
    best = sorted_all[0]
    worst = sorted_all[-1]
    flat_s = f" {flat} 平" if flat else ""
    summary_line = (
        f"总览：{len(all_items)} 只 {up} 涨 {down} 跌{flat_s} | "
        f"{' · '.join(group_summaries)} | "
        f"最强 {best[0]} ({best[3]}) · 最弱 {worst[0]} ({worst[3]})"
    )
    title = f"{custom_wl.get('emoji', '⭐')} {custom_wl.get('name', '自选股')}"
    return f"【{title}追踪（共 {len(all_items)} 只）】\n{summary_line}\n" + "\n".join(detail_lines)


def _format_financial_events_block(events: dict | None) -> str:
    """格式化收盘日已公布宏观实际值、NYSE 休市/半日市提醒及未来 5 交易日财报与宏观日程。"""
    if not events:
        return "暂无未来 5 日日程数据"
    holiday_alerts = events.get("holiday_alerts") or []
    today_actuals = events.get("today_actuals") or []
    earn_items = events.get("earnings") or []
    macro_items = events.get("macro") or []
    lines = [f"时间窗口：{events.get('window', '')}"]
    if holiday_alerts:
        lines.append("NYSE 休市/半日市提醒：" + " | ".join(holiday_alerts))
    if today_actuals:
        a_strs = []
        for a in today_actuals:
            cmp_parts = [f"实际{a['actual']}"]
            if a.get("consensus"):
                cmp_parts.append(f"预期{a['consensus']}")
            if a.get("previous"):
                cmp_parts.append(f"前值{a['previous']}")
            a_strs.append(f"{a['event']}（{' / '.join(cmp_parts)}）")
        lines.append("收盘日已公布核心宏观实际值：" + " | ".join(a_strs))
    if earn_items:
        e_strs = []
        for e in earn_items:
            mmdd = e["date"][5:]
            star = "★" if e.get("is_custom") else ""
            t_str = f" {e['time']}" if e.get("time") else ""
            mcap_b = e["market_cap"] / 1e9 if e.get("market_cap") else 0
            cap_str = f" (${mcap_b:,.0f}B)" if mcap_b >= 10 else ""
            e_strs.append(f"{mmdd}{t_str} {star}{e['symbol']}{cap_str}")
        lines.append("即将到来的重大财报（★为个人自选股）：" + " | ".join(e_strs))
    else:
        lines.append("即将到来的重大财报：未来5个交易日暂无市值≥500亿美元或自选股财报")
    if macro_items:
        m_strs = []
        for m in macro_items:
            mmdd = m["date"][5:]
            extra = []
            if m.get("consensus"):
                extra.append(f"预期{m['consensus']}")
            if m.get("previous"):
                extra.append(f"前值{m['previous']}")
            ex_s = f"（{'/'.join(extra)}）" if extra else ""
            m_strs.append(f"{mmdd} {m['event']}{ex_s}")
        lines.append("美国核心宏观日程：" + " | ".join(m_strs))
    else:
        lines.append("美国核心宏观日程：未来5个交易日暂无 FOMC/CPI/PCE/非农/GDP/ISM/长债拍卖 公布")
    return "\n".join(lines)


def _resolve_session_vix(quotes: dict, session_str: str) -> tuple[float | None, str]:
    """防 CBOE VIX 在节假日或盘后全球交易时段跨日串期：若 .VIX 日期晚于收盘日，优先取前收对应收盘日口径。"""
    vq = quotes.get(".VIX") or {}
    if not vq:
        return None, "未知"
    v_date = str(vq.get("last_time") or "")[:10]
    if session_str and v_date and v_date > session_str:
        prev_c = _num(vq.get("previous_day_closing"))
        live_l = vq.get("last", "?")
        if prev_c is not None and prev_c > 0:
            return prev_c, f"{prev_c:.2f} (收盘口径; 盘后实时 {live_l})"
        return None, f"{live_l} (跨日实时/{v_date})"
    return _num(vq.get("last")), fmt_quote(vq)


def _fmt_intraday_structure(q: dict | None, rvol: float | None = None) -> str:
    """计算并格式化指数日内结构：振幅%（以前收为分母，买方通行口径）、收盘位置 CLV=(C-L)/(H-L)、RVOL 与距 52 周高点回撤%。"""
    if not q:
        return ""
    last = _num(q.get("last"))
    hi = _num(q.get("high"))
    lo = _num(q.get("low"))
    prev_c = _num(q.get("previous_day_closing"))
    yrhi = _num(q.get("yrhiprice"))
    parts = []
    if last is not None and hi is not None and lo is not None and hi > lo and lo > 0:
        denom = prev_c if (prev_c is not None and prev_c > 0) else lo
        amp = (hi - lo) / denom * 100.0
        clv = max(0.0, min(1.0, (last - lo) / (hi - lo)))
        parts.append(f"日内低{lo:,.1f}/高{hi:,.1f}")
        parts.append(f"振幅 {amp:.2f}%")
        parts.append(f"CLV={clv:.2f}")
    if rvol is not None and rvol > 0:
        parts.append(f"RVOL={rvol:.2f}x")
    if last is not None and yrhi is not None and yrhi > 0:
        dd52 = (last - yrhi) / yrhi * 100.0
        parts.append(f"距52W高 {dd52:+.2f}%")
    return " · ".join(parts)


def _exa_macro_surprise_check(
    session_str: str,
    quotes: dict,
    fin_events: dict,
    news_lines: list[str],
) -> list[str]:
    """当收盘日出现高波动（标普振幅>=1.5%或VIX波动>=5%）且当日宏观实际值未覆盖 PCE/CPI/非农/美债拍卖时，用 Exa /search 补查头号宏观驱动。"""
    spx = quotes.get(".SPX") or {}
    vix = quotes.get(".VIX") or {}
    hi, lo = _num(spx.get("high")), _num(spx.get("low"))
    prev_c = _num(spx.get("previous_day_closing")) or lo
    amp = ((hi - lo) / prev_c * 100.0) if (hi and lo and prev_c and prev_c > 0) else 0.0
    vix_pct = abs(_num(vix.get("change_pct")) or 0.0)
    if amp < 1.5 and vix_pct < 5.0:
        return []
    actual_blob = " ".join(a.get("event", "") for a in (fin_events.get("today_actuals") or []))
    # 排除 federalreserve.gov 监管门槛公告（含 CPI-W）及无详情短摘要，避免误拦截真实通胀/拍卖冲击补查
    substantive_news = [
        ln for ln in news_lines
        if "federalreserve.gov" not in ln and len(ln) >= 600
    ]
    check_blob = actual_blob + "\n" + "\n".join(substantive_news)
    if re.search(r"\b(?:Core\s+PCE|CPI(?!-W)|Nonfarm\s+Payrolls|FOMC\s+decision|Treasury\s+auction)\b|核心\s*PCE", check_blob, re.I):
        return []
    hits = exa_search_fallback(
        f"US economic data Core PCE CPI GDP Treasury auction Fed reaction {session_str}",
        num_results=2,
        max_chars=700,
        category="news",
        start_published_date=session_str,
        exclude_domains=["analystmarkets.com", "newser.ai", "newser.com"],
    )
    out = []
    for h in hits:
        if h.get("text") and len(h["text"].strip()) >= 120:
            out.append(f"· [Exa当日宏观冲击补查] {h['title']}：{h['text'][:450]} ({h['url']})")
    return out


# ===== 汇总抓取 =====
def gather() -> dict:
    print("📈 抓取行情...", file=sys.stderr)
    custom_wl = load_custom_watchlist()
    custom_wl_syms = custom_wl["all_symbols"] if custom_wl else []
    if custom_wl:
        print(f"  ✓ 已加载个人自选股配置《{custom_wl['name']}》（{len(custom_wl_syms)} 只）", file=sys.stderr)

    sector_member_syms = [sym for members in SECTOR_STOCKS.values() for sym, _ in members]
    all_syms = list(dict.fromkeys(
        [s for s, _ in INDICES] + [s for s, _ in MACRO] + [s for s, _ in RISK_GAUGES] +
        [s for s, _ in SECTORS] + CORE_BREADTH_30 + WATCHLIST + sector_member_syms + custom_wl_syms + ["RSP"]
    ))
    quotes = {}
    try:
        quotes = fetch_quotes(all_syms)
    except Exception as e:
        print(f"[WARN] CNBC 行情抓取失败: {e}", file=sys.stderr)

    if not quotes or not quotes.get(".SPX"):
        print("[WARN] CNBC 行情源无标普数据，视为行情抓取失败", file=sys.stderr)
        return None
    print(f"  ✓ {len(quotes)} 个标的", file=sys.stderr)

    sess = session_date(quotes)
    if not sess:
        print("[WARN] 推不出收盘交易日，视为行情抓取失败", file=sys.stderr)
        return None
    print(f"  最近收盘交易日: {sess}", file=sys.stderr)

    print("\n📡 抓取 RSS 源...", file=sys.stderr)
    entries = []
    fetched_counts: dict = {}
    for url, limit, is_general in RSS_SOURCES:
        es = fetch_rss(url, limit)
        domain = url.split("/")[2]
        for e in es:
            e["__src"] = domain
            e["__general"] = is_general
        entries.extend(es)
        fetched_counts[domain] = fetched_counts.get(domain, 0) + len(es)
        print(f"  ✓ {len(es)} 条  {url}", file=sys.stderr)

    ledger = load_daily_ledger(exclude_bot="🗽 美股与加密")
    today_ledger_urls = ledger.get("urls") or set()
    ledger_block = format_ledger_context(exclude_bot="🗽 美股与加密")

    news_lines, kept_per_source, drops = build_news_context(
        entries,
        custom_wl_syms=custom_wl_syms,
        today_ledger_urls=today_ledger_urls,
        session_str=sess,
    )
    news_count = len(news_lines)

    source_stats = {d: {"fetched": n, "kept": kept_per_source.get(d, 0)}
                    for d, n in fetched_counts.items()}
    zero_sources = [d for d, s in source_stats.items() if s["kept"] == 0]
    stale_sources = update_zero_streak(ZERO_STREAK, zero_sources, list(source_stats),
                                       threshold=ZERO_STREAK_THRESHOLD)
    try:
        streak_now = json.loads(ZERO_STREAK.read_text(encoding="utf-8"))
    except Exception:
        streak_now = {}

    print(f"\n📰 共抓取 {len(entries)} 条 → 保留 {news_count} 条有效新闻", file=sys.stderr)
    print(f"   过滤明细：重复 {drops['dup']} · 已播过 {drops['already_sent']} · "
          f"超 48h {drops['stale']} · 非美股 {drops.get('off_topic', 0)} · "
          f"无正文 {drops.get('no_content', 0)}", file=sys.stderr)
    for d, s in sorted(source_stats.items(), key=lambda kv: -kv[1]["kept"]):
        n = streak_now.get(d, 0)
        flag = f"  ⚠️ 零产（连续 {n} 天）" if s["kept"] == 0 else ""
        print(f"   {d:26s} 抓{s['fetched']:>2} → 留{s['kept']:>2}{flag}", file=sys.stderr)

    print("\n🪙 抓取加密核心行情...", file=sys.stderr)
    crypto = fetch_crypto_snapshot()
    print(f"  ✓ BTC {crypto['prices'].get('BTC', '未知')} | 恐贪指数 {crypto['fear_val']} ({crypto['fear_label']})",
          file=sys.stderr)

    print("\n📅 抓取未来 5 个交易日重大财报与宏观日程...", file=sys.stderr)
    fin_events = fetch_financial_events(sess, set(custom_wl_syms))
    print(f"  ✓ 财报 {len(fin_events.get('earnings') or [])} 条 | 宏观 {len(fin_events.get('macro') or [])} 条",
          file=sys.stderr)

    vix_num, _ = _resolve_session_vix(quotes, sess)
    ma_trends, rvols = fetch_us_ma_trends_detail(session_str=sess, vix_val=vix_num)

    exa_catalysts = []
    exa_macro_lines = []
    if exa_enrich_enabled(default_enabled=True):
        uncovered = _select_uncovered_us_movers(quotes, news_lines, max_total=6, custom_wl=custom_wl)
        if uncovered:
            print(
                f"\n🔍 触发 Exa Agent 异动股定向催化补盲（{len(uncovered)} 只未命中 RSS）："
                + "、".join(f"{u['symbol']}({u['pct_change']})" for u in uncovered),
                file=sys.stderr,
            )
            exa_catalysts = exa_agent_verify_catalysts(
                uncovered,
                market_label="美股",
                session_date=sess,
                max_entities=6,
            )
        exa_macro_lines = _exa_macro_surprise_check(sess, quotes, fin_events, news_lines)
        if exa_macro_lines:
            print(f"  ✓ Exa 高波动当日宏观冲击补查命中 {len(exa_macro_lines)} 条", file=sys.stderr)

    return {"quotes": quotes, "session": sess,
            "crypto": crypto,
            "ma_trends": ma_trends,
            "rvols": rvols,
            "fin_events": fin_events,
            "custom_watchlist": custom_wl,
            "ledger_block": ledger_block,
            "exa_catalysts": exa_catalysts,
            "exa_macro_lines": exa_macro_lines,
            "news_lines": news_lines,
            "news_count": news_count, "entries": entries,
            "zero_sources": zero_sources, "source_stats": source_stats,
            "stale_sources": stale_sources,
            "today": datetime.now().strftime("%Y-%m-%d")}


def build_context(data: dict) -> str:
    """拼出交给写稿模型的完整买方量化 context。"""
    q = data["quotes"]
    c = data.get("crypto") or {}
    cp = c.get("prices", {})
    cg = c.get("global", {})
    cperp = c.get("perp") or {}
    rvols = data.get("rvols") or {}

    idx_lines = []
    for sym, name in INDICES:
        qq = q.get(sym)
        intra = _fmt_intraday_structure(qq, rvol=rvols.get(sym)) if sym in (".SPX", ".IXIC", ".RUT") else ""
        intra_s = f" | {intra}" if intra else ""
        idx_lines.append(f"{name}: {fmt_quote(qq)}{intra_s}")
    idx = "\n".join(idx_lines)

    # 利率曲线 2s10s 利差与同日口径 VIX/VIX3M 波动率期限结构（杜绝跨日实时 VIX 污染）
    sess_str = data.get("session") or ""
    y2 = _num((q.get("US2Y") or {}).get("last"))
    y10 = _num((q.get("US10Y") or {}).get("last"))
    spread_2s10s = f" | 2s10s利差 {(y10 - y2) * 100:+.1f}bp" if (y2 is not None and y10 is not None) else ""
    vix_l, vix_disp = _resolve_session_vix(q, sess_str)
    vix3m_q = q.get(".VIX3M") or {}
    vix3m_date = str(vix3m_q.get("last_time") or "")[:10]
    vix3m_l = _num(vix3m_q.get("last"))
    if sess_str and vix3m_date and vix3m_date > sess_str:
        vix3m_l = _num(vix3m_q.get("previous_day_closing")) or vix3m_l
    vix_term = ""
    if vix_l and vix3m_l and vix3m_l > 0:
        ratio = vix_l / vix3m_l
        t_lbl = "倒挂/Backwardation(短期恐慌极值)" if ratio >= 1.0 else "升水/Contango(常态)"
        vix_term = f" | VIX/VIX3M={ratio:.2f}({t_lbl})"

    bjt_ts = datetime.now().strftime("%H:%M BJT")
    mac_parts = []
    for sym, name in MACRO:
        if sym == ".VIX":
            mac_parts.append(f"{name} {vix_disp}")
        else:
            mac_parts.append(f"{name} {fmt_quote(q.get(sym))}")
    mac = " | ".join(mac_parts)
    core_cross = f"{mac}{spread_2s10s}{vix_term} | 比特币(24h实时) {cp.get('BTC', '未知')}（24h资产抓取于 {bjt_ts}）"

    # 板块按涨跌幅排序，让写稿时一眼看出轮动方向
    sec_rows = []
    for sym, name in SECTORS:
        qq = q.get(sym)
        if qq:
            sec_rows.append((name, sym, qq.get("change_pct", "?"), _num(qq.get("change_pct"))))
    sec_rows.sort(key=lambda r: (r[3] is None, -(r[3] or 0)))
    sec = "\n".join(f"{n}({s}): {p}" for n, s, p, _ in sec_rows)

    def _sector_top3_stocks(sec_sym: str, is_gainer: bool, sec_pct: float | None = None) -> str:
        members = SECTOR_STOCKS.get(sec_sym, [])
        items = []
        for m_sym, m_cn in members:
            mq = q.get(m_sym)
            p_val = _num(mq.get("change_pct")) if mq else None
            if mq and p_val is not None:
                items.append((m_sym, m_cn, mq.get("last", "?"),
                              mq.get("change_pct", "?"), p_val))
        # 领跌榜严禁混入上涨股；收涨板块的领涨榜只列上涨股（大盘普跌“抗跌”板块除外）
        if not is_gainer:
            filtered = [x for x in items if x[4] < 0]
            if not filtered:
                return "板块内核心成分股全线收涨（无下跌个股）"
            filtered.sort(key=lambda x: x[4])
            top3 = filtered[:3]
            suffix = f"（注：核心成分股中仅 {len(top3)} 只收跌）" if len(top3) < 3 else ""
            return "、".join(f"{cn}({sym}) {last} ({pct})" for sym, cn, last, pct, _ in top3) + suffix
        if sec_pct is not None and sec_pct > 0:
            pos = [x for x in items if x[4] > 0]
            if pos:
                items = pos
        items.sort(key=lambda x: x[4], reverse=True)
        top3 = items[:3]
        if not top3:
            return "暂无成分股行情"
        return "、".join(f"{cn}({sym}) {last} ({pct})" for sym, cn, last, pct, _ in top3)

    valid_secs = [r for r in sec_rows if r[3] is not None]
    top3_gain_secs = valid_secs[:3]
    top3_lose_secs = list(reversed(valid_secs[-3:])) if len(valid_secs) >= 3 else []

    sec_top3_gain_lines = []
    for idx_i, (n, s, p, p_num) in enumerate(top3_gain_secs, 1):
        sec_top3_gain_lines.append(
            f"{idx_i}. 【{n}({s})】 板块涨跌幅：{p}\n"
            f"   · 板块内领涨 Top 3：{_sector_top3_stocks(s, is_gainer=True, sec_pct=p_num)}"
        )
    sec_top3_lose_lines = []
    for idx_i, (n, s, p, p_num) in enumerate(top3_lose_secs, 1):
        sec_top3_lose_lines.append(
            f"{idx_i}. 【{n}({s})】 板块涨跌幅：{p}\n"
            f"   · 板块内领跌 Top 3：{_sector_top3_stocks(s, is_gainer=False, sec_pct=p_num)}"
        )
    sec_detail_block = (
        "🔥 领涨 Top 3 板块（及板块内涨幅 Top 3 个股）：\n"
        + ("\n".join(sec_top3_gain_lines) or "未知")
        + "\n\n❄️ 领跌 Top 3 板块（及板块内跌幅 Top 3 个股）：\n"
        + ("\n".join(sec_top3_lose_lines) or "未知")
    )

    # 个股按涨跌幅排序，取两端各 8 个作为高关注度异动榜
    mv = []
    for sym in WATCHLIST:
        qq = q.get(sym)
        if qq:
            mv.append((sym, qq.get("shortName", sym), qq.get("last", "?"),
                       qq.get("change_pct", "?"), _num(qq.get("change_pct"))))
    mv.sort(key=lambda r: (r[4] is None, -(r[4] or 0)))
    gainers = "\n".join(f"{s}: {p} ({c})" for s, _, p, c, _ in mv[:8])
    losers  = "\n".join(f"{s}: {p} ({c})" for s, _, p, c, _ in mv[-8:][::-1])

    spx = q.get(".SPX", {})
    rut = q.get(".RUT", {})
    rsp = q.get("RSP", {})
    spx_pct = _num(spx.get("change_pct"))
    rut_pct = _num(rut.get("change_pct"))
    rsp_pct = _num(rsp.get("change_pct"))
    spread_str = (
        f" (RSP-SPX剪刀差 {rsp_pct - spx_pct:+.2f}%)"
        if (spx_pct is not None and rsp_pct is not None) else ""
    )
    rut_spread_str = (
        f" | 小大盘风格差(RUT-SPX) {rut_pct - spx_pct:+.2f}%"
        if (spx_pct is not None and rut_pct is not None) else ""
    )
    hyg_pct = _num((q.get("HYG") or {}).get("change_pct"))
    iei_pct = _num((q.get("IEI") or {}).get("change_pct"))
    credit_dn_str = ""
    if hyg_pct is not None and iei_pct is not None:
        ex_c = hyg_pct - iei_pct
        c_lbl = "信用利差收窄" if ex_c > 0.05 else ("信用利差走阔" if ex_c < -0.05 else "信用利差走平")
        credit_dn_str = f" | 久期中性信用代理(HYG-IEI) {ex_c:+.2f}% ({c_lbl})"
    risk_str = " | ".join(f"{name}: {fmt_quote(q.get(sym))}" for sym, name in RISK_GAUGES) + credit_dn_str

    ma_trends = data.get("ma_trends") or {}
    ma_lines = "\n".join(
        f"{_US_MA_MAP[sym][1]} 双维度趋势与波动率: {ma_trends[sym]}"
        for sym, _ in INDICES if ma_trends.get(sym) and sym in _US_MA_MAP
    ) or "均线与波动率数据暂缺"

    # 均衡全行业市场宽度（11大板块 + 30只跨全行业核心权重 + RSP-SPX + RUT-SPX + 久期中性信用）
    sec_up = sum(1 for _, _, _, v in sec_rows if v is not None and v > 0)
    sec_down = sum(1 for _, _, _, v in sec_rows if v is not None and v < 0)
    b30_vals = [_num((q.get(sym) or {}).get("change_pct")) for sym in CORE_BREADTH_30]
    b30_up = sum(1 for v in b30_vals if v is not None and v > 0)
    b30_down = sum(1 for v in b30_vals if v is not None and v < 0)
    breadth_block = (
        f"11大GICS板块：{sec_up} 涨 {sec_down} 跌 | "
        f"30只全行业均衡权重：{b30_up} 涨 {b30_down} 跌 | "
        f"等权标普(RSP)：{fmt_quote(rsp)}{spread_str}{rut_spread_str}\n"
        f"行业与久期中性信用风向标：{risk_str}"
    )

    perp_parts = []
    for coin in ("BTC", "ETH"):
        pm = cperp.get(coin)
        if pm:
            perp_parts.append(f"{coin} 年化资金费率 {pm['ann_funding']} / OI 24h {pm['oi_24h_chg']} ({pm['venue']})")
    perp_line = ("\n永续合约杠杆定位: " + " | ".join(perp_parts)) if perp_parts else ""

    crypto_block = (
        f"恐惧贪婪指数: {c.get('fear_val', '未知')} ({c.get('fear_label', '未知')}) | "
        f"全球加密市值: {cg.get('total_market_cap_b', '未知')} ({cg.get('market_cap_change_24h', '未知')}) | "
        f"BTC 市占率: {cg.get('btc_dominance', '未知')}（{cg.get('bull_bear_label', '未知')}）{perp_line}\n"
        f"BTC: {cp.get('BTC', '未知')} | ETH: {cp.get('ETH', '未知')} | BNB: {cp.get('BNB', '未知')}\n"
        f"SOL: {cp.get('SOL', '未知')} | XRP: {cp.get('XRP', '未知')} | HYPE: {cp.get('HYPE', '未知')}"
    )
    events_block = _format_financial_events_block(data.get("fin_events"))
    custom_wl_block = _format_custom_watchlist_block(data.get("custom_watchlist"), q)
    custom_wl_section = f"{custom_wl_block}\n----------------\n" if custom_wl_block else ""
    exa_block = format_exa_catalyst_block(data.get("exa_catalysts") or [])
    exa_section = f"{exa_block}\n----------------\n" if exa_block else ""
    exa_macro_lines = data.get("exa_macro_lines") or []
    exa_macro_section = (
        "【📡 Exa 高波动当日宏观冲击补查】\n" + "\n".join(exa_macro_lines) + "\n----------------\n"
    ) if exa_macro_lines else ""
    ledger_blk = (data.get("ledger_block") or "").strip()
    ledger_section = (
        f"【跨 Bot 去重参考：今日其他 Bot（尤其 09:00 AI 产业日报）已播清单，美股要闻默认跳过同事件】\n"
        f"{ledger_blk}\n----------------\n"
    ) if ledger_blk else ""
    return (
        f"【收盘交易日】{data['session']}\n"
        f"----------------\n"
        + ledger_section
        + f"【🧠 跨日盘面状态记忆（最近 5 个交易日板块轮动、信号灯与关注方向追踪）】\n{_format_us_memory_block()}\n"
        f"----------------\n"
        f"【核心跨资产指数（股市日内结构 + 2Y/10Y/2s10s + VIX/VIX3M + 汇商加密）】\n{idx}\n{core_cross}\n"
        f"标普 开{spx.get('open','?')} 高{spx.get('high','?')} 低{spx.get('low','?')} "
        f"52周高{spx.get('yrhiprice','?')} 52周低{spx.get('yrloprice','?')}\n"
        f"----------------\n"
        f"【均衡宽度、久期中性信用与双维度趋势/波动率】\n{breadth_block}\n{ma_lines}\n"
        f"----------------\n"
        f"【加密货币（24h 含永续合约杠杆定位）】\n{crypto_block}\n"
        f"----------------\n"
        f"【Financial Events（收盘日宏观实际值 + NYSE 休市提醒 + 未来 5 交易日财报与宏观）】\n{events_block}\n"
        f"----------------\n"
        + exa_macro_section
        + custom_wl_section
        + f"【11大板块表现（按涨跌幅排序）】\n{sec}\n"
        f"----------------\n"
        f"【领涨 Top 3 板块 & 领跌 Top 3 板块（含各板块内 Top 3 个股）】\n{sec_detail_block}\n"
        f"----------------\n"
        f"【全市场焦点个股涨幅前 8】\n{gainers}\n"
        f"----------------\n"
        f"【全市场焦点个股跌幅前 8】\n{losers}\n"
        f"----------------\n"
        + exa_section
        + "\n"
        + "\n".join(data["news_lines"])
    )


def _summary_and_metrics(data: dict) -> tuple:
    q = data["quotes"]
    spx, ixic = q.get(".SPX", {}), q.get(".IXIC", {})
    vix = q.get(".VIX", {})
    btc = (data.get("crypto") or {}).get("prices", {}).get("BTC", "?")
    summary = (f"收盘{data['session']} 标普{spx.get('last','?')}({spx.get('change_pct','?')}) "
               f"纳指{ixic.get('last','?')}({ixic.get('change_pct','?')}) "
               f"VIX{vix.get('last','?')} BTC {btc} → 抓取{len(data['entries'])}条 → 保留{data['news_count']}条")
    metrics = {
        "session": data["session"],
        "spx": spx.get("last"), "spx_pct": spx.get("change_pct"),
        "ixic_pct": ixic.get("change_pct"), "vix": vix.get("last"),
        "btc": btc,
        "rss_fetched": len(data["entries"]), "rss_kept": data["news_count"],
        "rss_zero_sources": data["zero_sources"],
        "rss_source_stats": data["source_stats"],
        "rss_stale_sources": data["stale_sources"],
    }
    return summary, metrics


# ===== 模式 1：fetch =====
def run_fetch() -> int:
    if already_ran_today(LOG_FILE):
        print("=== SKIP_ALREADY_RAN ===")
        return 0

    if not _proxy_ok():
        print(f"=== SKIP_PROXY === {_PROXY}")
        return 0

    data = gather()
    if data is None:
        print("=== SKIP_NO_QUOTES ===")
        write_log("WARN", "行情数据抓取失败，跳过本次发送，等待下次运行")
        return 0

    # 同一个收盘场次只播一次：北京时间周日/周一没有新场次，
    # 美股假期同理。这是本 bot 特有的幂等条件，比"今天是否跑过"更准确。
    sess = data["session"]
    try:
        last = LAST_SESSION.read_text(encoding="utf-8").strip()
    except Exception:
        last = ""
    if sess and sess == last:
        print(f"=== SKIP_NO_NEW_SESSION === {sess}")
        write_log("WARN", f"无有效新闻（{sess} 收盘已播报过，无新交易日）")
        return 0

    summary, metrics = _summary_and_metrics(data)
    q = data["quotes"]
    sec_rows = []
    for sym, name in SECTORS:
        qq = q.get(sym)
        if qq and _num(qq.get("change_pct")) is not None:
            sec_rows.append((name, sym, qq.get("change_pct", "?"), _num(qq.get("change_pct"))))
    sec_rows.sort(key=lambda r: -r[3])
    spx_q, ixic_q, dji_q = q.get(".SPX", {}), q.get(".IXIC", {}), q.get(".DJI", {})
    snapshot = {
        "session": sess,
        "indices": (
            f"标普 {spx_q.get('last','?')}({spx_q.get('change_pct','?')}) | "
            f"纳指 {ixic_q.get('last','?')}({ixic_q.get('change_pct','?')}) | "
            f"道指 {dji_q.get('last','?')}({dji_q.get('change_pct','?')})"
        ),
        "top3_gainers": [f"{n}({s} {p})" for n, s, p, _ in sec_rows[:3]],
        "top3_losers": [f"{n}({s} {p})" for n, s, p, _ in reversed(sec_rows[-3:])] if len(sec_rows) >= 3 else [],
    }
    FETCH_META.write_text(
        json.dumps({"log_summary": summary, "metrics": metrics,
                    "session": sess, "snapshot": snapshot}, ensure_ascii=False),
        encoding="utf-8",
    )

    # 先攒成列表再一次性交给 emit_fetch_output，顺带落盘 logs/last_context.txt。
    out = ["=== FETCH_OK ===",
           f"今天日期：{data['today']}",
           f"收盘交易日：{sess}",
           f"保留 {data['news_count']} 条有效新闻（共抓取 {len(data['entries'])} 条）"]
    # 上次播的是哪个场次。本 bot 按"最近一个已收盘交易日"取数，中间漏跑几天时会
    # 直接跳到最新场次，被跳过的那些收盘就永远不会播——2026-08-27 的收盘就是这么
    # 丢的（08-27 播的是 08-26，之后两天没跑，08-30 直接播 08-28）。两个日期并排
    # 打出来，写稿方一眼能看出中间是否缺了场次。
    if last and last != sess:
        out.append(f"上次播报的收盘交易日：{last}")
    if data["zero_sources"]:
        out.append(f"零产源：{', '.join(data['zero_sources'])}")
    if data["stale_sources"]:
        out.append("=== SOURCE_ALERT ===")
        for d, n in data["stale_sources"].items():
            out.append(f"{d} 已连续 {n} 天零产，建议从 RSS_SOURCES 移除或更换")
        out.append("=== SOURCE_ALERT_END ===")
    out += ["=== CONTEXT_BEGIN ===", build_context(data), "=== CONTEXT_END ==="]
    emit_fetch_output(out, LAST_CONTEXT)
    return 0


# ===== 模式 2：send =====
def run_send() -> int:
    t0 = time.time()

    if already_ran_today(LOG_FILE):
        print("今天已成功运行过，跳过发送。如需强制请设置 FORCE_RUN=1。", file=sys.stderr)
        return 0

    if not DRAFT_MARKET.exists() or not DRAFT_NEWS.exists():
        write_log("FAIL", f"稿子文件缺失：需要 {DRAFT_MARKET.name} 与 {DRAFT_NEWS.name}")
        return 1
    market = DRAFT_MARKET.read_text(encoding="utf-8").strip()
    news   = DRAFT_NEWS.read_text(encoding="utf-8").strip()
    if not market or not news:
        write_log("FAIL", "稿子文件为空（消息①或②）")
        return 1

    # 不再做代理预检：飞书直连可达，推送阶段本来就不需要翻墙代理。
    # （Telegram 时代代理一挂当天就整个不播，现在只有抓取阶段依赖它。）
    save_pending([market, news])
    print("📨 推送到飞书...", file=sys.stderr)
    send_report(market)
    save_pending([news])          # 消息①已发：更新缓存，防止重跑时重复推送
    send_report(news)
    CACHE_FILE.unlink(missing_ok=True)
    print("  ✓ 推送成功", file=sys.stderr)

    try:
        meta = json.loads(FETCH_META.read_text(encoding="utf-8"))
    except Exception:
        meta = {"log_summary": "AI写稿", "metrics": {}, "session": ""}
    sess_date = meta.get("session") or datetime.now().strftime("%Y-%m-%d")

    # 自动同步归档到 Notion「每日情报归档与决策看板」数据库，并留存合并文稿
    combined_report = f"{market}\n\n---\n\n{news}"
    notion_meta = archive_and_sync_notion(
        combined_report,
        "🗽 美股与加密",
        date_str=sess_date,
        archive_dir=DRAFT_MARKET.parent / "archive",
        archive_filename=f"{sess_date}.txt",
    )
    notion_tag = " | Notion ✓" if notion_meta.get("notion_ok") else " | Notion ⚠️"

    hrefs = extract_hrefs(market) + extract_hrefs(news)
    if hrefs:
        total = record_sent_urls(SENT_URLS, hrefs)
        print(f"  ✓ 已归档 {len(hrefs)} 条链接用于跨天去重（档案共 {total} 条）",
              file=sys.stderr)

    # 记下已播报的收盘场次，供下次 fetch 判断有无新场次
    if meta.get("session"):
        try:
            LAST_SESSION.write_text(meta["session"], encoding="utf-8")
        except Exception as e:
            print(f"[WARN] .last_session 写入失败: {e}", file=sys.stderr)

    # 更新最近 5 个交易日跨日盘面状态记忆库（State Store）
    try:
        snap = meta.get("snapshot") or {}
        if snap.get("session"):
            sig, focus = _extract_us_signal_and_focus(market)
            entry = {**snap, "signal": sig, "focus": focus}
            hist = [r for r in _load_us_memory() if r.get("session") != entry["session"]]
            hist.append(entry)
            MARKET_MEMORY.write_text(
                json.dumps(hist[-MAX_MEMORY_DAYS:], ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            print(f"  ✓ 已更新美股跨日盘面状态记忆库（保留最近 {len(hist[-MAX_MEMORY_DAYS:])} 个交易日）",
                  file=sys.stderr)
    except Exception as e:
        print(f"[WARN] market_memory.json 写入失败: {e}", file=sys.stderr)

    duration = round(time.time() - t0, 1)
    write_log(
        "OK",
        f"AI写稿 → {meta.get('log_summary', '')} → 飞书推送成功{notion_tag}",
        metrics={**meta.get("metrics", {}), **notion_meta, "ai_calls": 0, "source": "antigravity",
                 "duration_s": duration},
    )
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="美股收盘日报")
    parser.add_argument(
        "--mode", choices=["fetch", "send"], required=True,
        help="fetch=抓取并输出 context（零 API）/ send=发送写好的两稿",
    )
    parsed = parser.parse_args()

    try:
        sys.exit(run_fetch() if parsed.mode == "fetch" else run_send())
    except Exception:
        err = traceback.format_exc().strip().splitlines()[-1]
        write_log("FAIL", err)
        raise
