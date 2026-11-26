#!/usr/bin/python3
"""
🇨🇳 A股智能决策日报（09:00 开盘前瞻紧接美股收盘日报 + 16:15 收盘复盘）
- 与《美股收盘 & 加密行情日报》全面对齐：220 日 K 线（MA5/10/20/50/200 + Wilder RSI(14) + 布林 %B(20,2)）
- 09:00 开盘前瞻（premarket）三维度联动：
  1. 自动读取前一日 A 股收盘复盘（market_memory.json + archive/*_close.txt）与最新技术锚点
  2. 抓取昨日 15:00 收盘后至今日 09:00 开盘前的国内宏观政策与行业催化电报（新浪 7x24 + Exa 补盲）
  3. 自动承接今早 09:00 刚生成的《美股收盘 & 加密行情日报》正稿 + 隔夜美股/中概 ETF 映射报价
- 16:15 收盘复盘（close）动态捕捉全市场及 12 大核心主线领涨/领跌 Top 3 板块与个股，
  自动写入 market_memory.json 与 archive/YYYY-MM-DD_close.txt 供次日盘前联动。
"""

from __future__ import annotations

import argparse
import html
import json
import math
import os
import re
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "shared"))
from bot_utils import (
    archive_and_sync_notion,
    emit_fetch_output,
    exa_agent_verify_catalysts,
    exa_enrich_enabled,
    exa_search_fallback,
    format_exa_catalyst_block,
    make_logger,
    make_pending_saver,
    sanitize_html,
    send_feishu,
)

BASE = Path(__file__).parent
LOG_FILE = BASE / "logs" / "run.log"
JSONL_FILE = BASE / "logs" / "run.jsonl"
LOG_FILE.parent.mkdir(exist_ok=True)
CACHE_FILE = BASE / "pending_messages.json"
FETCH_META = BASE / "logs" / "fetch_meta.json"
LAST_CONTEXT = BASE / "logs" / "last_context.txt"
MARKET_MEMORY = BASE / "logs" / "market_memory.json"
ARCHIVE_DIR = BASE / "logs" / "archive"
MAX_MEMORY_DAYS = 5

DRAFT_CN = BASE / "logs" / "report_cn.txt"
US_BOT_DIR = REPO_ROOT / "us stock bot"

FEISHU_WEBHOOK_CN = os.getenv("FEISHU_WEBHOOK_CN", "")

write_log = make_logger(LOG_FILE, JSONL_FILE)
save_pending = make_pending_saver(CACHE_FILE)

DIRECT_SESSION = requests.Session()
DIRECT_SESSION.trust_env = False
DIRECT_SESSION.proxies = {}
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# 1. A股核心指数
CN_INDICES = [
    ("sh000001", "上证指数"),
    ("sz399001", "深证成指"),
    ("sz399006", "创业板指"),
    ("sh000688", "科创50"),
    ("sh000300", "沪深300"),
]

# 2. A股 12 大核心主线板块池（72 只核心龙头样本）
CN_SECTOR_POOL: dict[str, list[tuple[str, str]]] = {
    "AI算力与光模块": [
        ("sh688256", "寒武纪"), ("sh601138", "工业富联"), ("sz300308", "中际旭创"),
        ("sz300502", "新易盛"), ("sz000977", "浪潮信息"), ("sh603019", "中科曙光"),
    ],
    "人形机器人与智驾硬件": [
        ("sh601689", "拓普集团"), ("sz002050", "三花智控"), ("sz300124", "汇川技术"),
        ("sh688017", "绿的谐波"), ("sz002472", "双环传动"), ("sh603728", "鸣志电器"),
    ],
    "半导体芯片与设备": [
        ("sh688981", "中芯国际"), ("sz002371", "北方华创"), ("sh688012", "中微公司"),
        ("sh688041", "海光信息"), ("sh603501", "韦尔股份"), ("sh603986", "兆易创新"),
    ],
    "国防军工与低空商业航天": [
        ("sh600760", "中航沈飞"), ("sh600893", "航发动力"), ("sz000768", "中航西飞"),
        ("sh600118", "中国卫星"), ("sz002085", "万丰奥威"), ("sh688122", "西部超导"),
    ],
    "煤炭电力与高股息红利": [
        ("sh601088", "中国神华"), ("sh601225", "陕西煤业"), ("sh600900", "长江电力"),
        ("sh601898", "中煤能源"), ("sh600025", "华能水电"), ("sh601985", "中国核电"),
    ],
    "有色金属与贵金属": [
        ("sh601899", "紫金矿业"), ("sh603993", "洛阳钼业"), ("sh600547", "山东黄金"),
        ("sz000807", "云铝股份"), ("sh601600", "中国铝业"), ("sh600489", "中金黄金"),
    ],
    "消费电子与果链": [
        ("sz002475", "立讯精密"), ("sz002241", "歌尔股份"), ("sz300433", "蓝思科技"),
        ("sz002938", "鹏鼎控股"), ("sh688036", "传音控股"), ("sz002600", "领益智造"),
    ],
    "券商互金与银行保险": [
        ("sz300059", "东方财富"), ("sh600030", "中信证券"), ("sh601318", "中国平安"),
        ("sh600036", "招商银行"), ("sz002332", "同花顺"), ("sh601688", "华泰证券"),
    ],
    "食品饮料与白酒消费": [
        ("sh600519", "贵州茅台"), ("sz000858", "五粮液"), ("sz000568", "泸州老窖"),
        ("sh600809", "山西汾酒"), ("sh600887", "伊利股份"), ("sh603288", "海天味业"),
    ],
    "创新药与医疗器械": [
        ("sh600276", "恒瑞医药"), ("sz300760", "迈瑞医疗"), ("sh603259", "药明康德"),
        ("sz300015", "爱尔眼科"), ("sh688271", "联影医疗"), ("sz000661", "长春高新"),
    ],
    "房地产与基建建材": [
        ("sh600048", "保利发展"), ("sz000002", "万科A"), ("sz001979", "招商蛇口"),
        ("sh600585", "海螺水泥"), ("sz002271", "东方雨虹"), ("sh601668", "中国建筑"),
    ],
    "光伏储能与锂电新能源": [
        ("sz300274", "阳光电源"), ("sz300750", "宁德时代"), ("sh601012", "隆基绿能"),
        ("sz002594", "比亚迪"), ("sh600438", "通威股份"), ("sz002460", "赣锋锂业"),
    ],
}

# 3. 海外市场与 A 股产业链先行映射报价（09:00 开盘前瞻核心锚）
US_MAPPING = [
    ("us.INX", "标普500"),
    ("us.IXIC", "纳斯达克"),
    ("us.DJI", "道琼斯"),
    ("usKWEB", "中概互联ETF(KWEB)"),
    ("usFXI", "中国大盘ETF(FXI)"),
    ("usNVDA", "英伟达(AI算力/光模块映射)"),
    ("usMU", "美光(存储/半导体映射)"),
    ("usAAPL", "苹果(果链/消费电子映射)"),
    ("usTSLA", "特斯拉(机器人/智驾映射)"),
    ("usBABA", "阿里ADR"),
    ("usPDD", "拼多多"),
    ("usJD", "京东ADR"),
]


def _safe_float(v: Any) -> float:
    try:
        return float(v)
    except Exception:
        return 0.0


def fetch_gtimg_quotes(codes: list[str]) -> dict[str, dict[str, Any]]:
    """通过腾讯财经 qt.gtimg.cn 批量拉取 A 股与海外映射行情。"""
    if not codes:
        return {}
    unique_codes = list(dict.fromkeys(codes))
    out: dict[str, dict[str, Any]] = {}
    batch_size = 55
    for i in range(0, len(unique_codes), batch_size):
        batch = unique_codes[i : i + batch_size]
        url = "https://qt.gtimg.cn/q=" + ",".join(batch)
        try:
            resp = DIRECT_SESSION.get(url, headers={"User-Agent": _UA}, timeout=15)
            resp.encoding = "gbk"
        except Exception as exc:
            print(f"[WARN] qt.gtimg.cn 批量请求异常: {exc}", file=sys.stderr)
            continue
        for line in resp.text.splitlines():
            line = line.strip()
            if not line.startswith("v_"):
                continue
            m = re.match(r'^v_([A-Za-z0-9_.]+)="([^"]*)";?$', line)
            if not m:
                continue
            code, body = m.group(1), m.group(2)
            parts = body.split("~")
            if len(parts) < 35:
                continue
            name = parts[1].strip()
            last = _safe_float(parts[3])
            prev_close = _safe_float(parts[4])
            open_p = _safe_float(parts[5])
            pct = _safe_float(parts[32])
            high = _safe_float(parts[33])
            low = _safe_float(parts[34])
            turnover_rate = _safe_float(parts[38]) if len(parts) > 38 else 0.0
            amplitude = _safe_float(parts[43]) if len(parts) > 43 else 0.0
            raw_amount = _safe_float(parts[37]) if len(parts) > 37 else 0.0
            amount_yi = raw_amount / 1e4 if raw_amount else 0.0

            out[code] = {
                "code": code,
                "name": name,
                "last": last,
                "prev_close": prev_close,
                "open": open_p,
                "high": high,
                "low": low,
                "pct": pct,
                "pct_str": f"{pct:+.2f}%",
                "turnover_rate": turnover_rate,
                "amplitude": amplitude,
                "amount_yi": amount_yi,
                "time": parts[30] if len(parts) > 30 else "",
            }
    return out


def fetch_sina_cn_sectors_top3() -> dict[str, list[dict[str, Any]]] | None:
    """从新浪财经全市场行业接口获取 A 股涨幅 Top 3 行业 & 跌幅 Top 3 行业，以及各行业内 Top 3 个股。"""
    try:
        url = "https://vip.stock.finance.sina.com.cn/q/view/newSinaHy.php"
        resp = DIRECT_SESSION.get(url, headers={"User-Agent": _UA}, timeout=10)
        resp.encoding = "gbk"
        m = re.search(r"=\s*(\{.*\})", resp.text)
        if not m:
            return None
        raw_dict = json.loads(m.group(1))
        sectors = []
        for node_code, val_str in raw_dict.items():
            parts = val_str.split(",")
            if len(parts) < 12:
                continue
            sec_name = parts[1].strip()
            sec_pct = _safe_float(parts[5])
            sectors.append({"node": node_code, "name": sec_name, "pct": sec_pct})
        if len(sectors) < 6:
            return None

        sectors_sorted = sorted(sectors, key=lambda x: x["pct"], reverse=True)
        top_gainers = sectors_sorted[:3]
        top_losers = list(reversed(sectors_sorted[-3:]))

        def _fetch_node_stocks(node: str, asc: int) -> list[dict[str, Any]]:
            s_url = (
                "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
                f"Market_Center.getHQNodeData?page=1&num=6&sort=changepercent&asc={asc}&node={node}"
            )
            r = DIRECT_SESSION.get(s_url, headers={"User-Agent": _UA}, timeout=8)
            rows = r.json() or []
            res = []
            for st in rows:
                pct_v = _safe_float(st.get("changepercent", 0))
                if asc == 1 and pct_v >= 0:
                    continue
                if asc == 0 and pct_v <= 0:
                    continue
                res.append({
                    "code": st.get("symbol", ""),
                    "name": st.get("name", ""),
                    "last": _safe_float(st.get("trade", 0)),
                    "pct": pct_v,
                    "amount_yi": _safe_float(st.get("amount", 0)) / 1e8,
                    "turnover": _safe_float(st.get("turnoverratio", 0)),
                })
                if len(res) >= 3:
                    break
            return res

        for sec in top_gainers:
            sec["stocks"] = _fetch_node_stocks(sec["node"], asc=0)
        for sec in top_losers:
            sec["stocks"] = _fetch_node_stocks(sec["node"], asc=1)

        return {"gainers": top_gainers, "losers": top_losers}
    except Exception as exc:
        print(f"[WARN] 新浪全市场行业接口回退至内置板块池: {exc}", file=sys.stderr)
        return None


def compute_pool_sectors_top3(
    sector_pool: dict[str, list[tuple[str, str]]],
    quotes: dict[str, dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """基于实时行情计算 12 大主线板块均值，排出涨幅 Top 3 & 跌幅 Top 3 板块，且领跌板块严格不混入收涨股。"""
    sectors: list[dict[str, Any]] = []
    for sec_name, members in sector_pool.items():
        valid_stocks = []
        for code, fallback_name in members:
            q = quotes.get(code)
            if q and q["last"] > 0:
                valid_stocks.append({
                    "code": code,
                    "name": fallback_name or q["name"],
                    "last": q["last"],
                    "pct": q["pct"],
                    "amount_yi": q["amount_yi"],
                    "turnover": q["turnover_rate"],
                })
        if not valid_stocks:
            continue
        avg_pct = sum(s["pct"] for s in valid_stocks) / len(valid_stocks)
        sectors.append({"name": sec_name, "pct": avg_pct, "all_stocks": valid_stocks})

    sectors_sorted = sorted(sectors, key=lambda x: x["pct"], reverse=True)
    gainers = []
    for sec in sectors_sorted[:3]:
        pos = [s for s in sec["all_stocks"] if s["pct"] > 0]
        pool_use = pos if (sec["pct"] > 0 and pos) else sec["all_stocks"]
        top3_up = sorted(pool_use, key=lambda s: s["pct"], reverse=True)[:3]
        gainers.append({"name": sec["name"], "pct": sec["pct"], "stocks": top3_up})

    losers = []
    for sec in reversed(sectors_sorted[-3:]):
        neg = [s for s in sec["all_stocks"] if s["pct"] < 0]
        top3_down = sorted(neg, key=lambda s: s["pct"])[:3]
        losers.append({"name": sec["name"], "pct": sec["pct"], "stocks": top3_down})

    return {"gainers": gainers, "losers": losers}


def _calc_wilder_rsi(closes: list[float], period: int = 14) -> float | None:
    if len(closes) < period + 1:
        return None
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(d, 0.0) for d in deltas]
    losses = [max(-d, 0.0) for d in deltas]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _calc_bollinger_pct_b(closes: list[float], period: int = 20, num_std: float = 2.0) -> tuple[float, float, float] | None:
    if len(closes) < period:
        return None
    win = closes[-period:]
    mid = sum(win) / period
    var = sum((x - mid) ** 2 for x in win) / period
    std = math.sqrt(var)
    if std <= 0:
        return None
    upper = mid + num_std * std
    lower = mid - num_std * std
    pct_b = (closes[-1] - lower) / (upper - lower)
    return pct_b, upper, lower


def fetch_ma_trend(code: str, live_last: float = 0.0, today_str: str = "") -> str:
    """拉取最近 220 日 K 线，计算 MA5/10/20/50/200、Wilder RSI(14)、布林 %B(20,2) 与距 52 周高点回撤。"""
    try:
        url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={code},day,,,220,qfq"
        resp = DIRECT_SESSION.get(url, headers={"User-Agent": _UA}, timeout=10)
        data = resp.json().get("data", {}).get(code, {})
        klines = data.get("day") or data.get("qfqday") or []
        if len(klines) < 20:
            return ""
        closes = [_safe_float(row[2]) for row in klines if len(row) >= 3]
        highs = [_safe_float(row[3]) for row in klines if len(row) >= 4]
        last_bar_date = str(klines[-1][0])[:10] if klines and len(klines[-1]) >= 1 else ""
        if live_last > 0 and today_str:
            if last_bar_date == today_str:
                closes[-1] = live_last
            elif today_str > last_bar_date and datetime.now().hour >= 10:
                closes.append(live_last)
        if len(closes) < 20 or not closes[-1]:
            return ""

        cur = closes[-1]
        ma5 = sum(closes[-5:]) / 5.0
        ma10 = sum(closes[-10:]) / 10.0
        ma20 = sum(closes[-20:]) / 20.0
        bias5 = (cur - ma5) / ma5 * 100.0 if ma5 else 0.0
        bias20 = (cur - ma20) / ma20 * 100.0 if ma20 else 0.0

        if ma5 > ma10 > ma20:
            trend = "🟢多头排列"
        elif ma5 < ma10 < ma20:
            trend = "🔴空头排列"
        elif cur >= ma20:
            trend = "🟡均线缠绕偏强(站上MA20)"
        else:
            trend = "🟡均线缠绕偏弱(跌破MA20)"

        parts = [f"{trend} | MA5={ma5:.2f} (乖离率 {bias5:+.2f}%) | MA20={ma20:.2f} ({bias20:+.2f}%)"]

        if len(closes) >= 50:
            ma50 = sum(closes[-50:]) / 50.0
            d50 = (cur - ma50) / ma50 * 100.0 if ma50 else 0.0
            parts.append(f"MA50={ma50:.2f} ({d50:+.1f}%)")
        if len(closes) >= 200:
            ma200 = sum(closes[-200:]) / 200.0
            d200 = (cur - ma200) / ma200 * 100.0 if ma200 else 0.0
            y_tag = "站上年线" if cur >= ma200 else "跌破年线"
            parts.append(f"MA200={ma200:.2f} ({d200:+.1f}%/{y_tag})")

        rsi = _calc_wilder_rsi(closes, 14)
        if rsi is not None:
            if rsi >= 70:
                r_lbl = "超买"
            elif rsi >= 60:
                r_lbl = "偏强"
            elif rsi <= 30:
                r_lbl = "超卖"
            elif rsi <= 40:
                r_lbl = "偏弱近超卖"
            else:
                r_lbl = "中性"
            parts.append(f"RSI(14)={rsi:.1f} ({r_lbl})")

        bb = _calc_bollinger_pct_b(closes, 20, 2.0)
        if bb is not None:
            pct_b, upper, lower = bb
            rb = round(pct_b, 2)
            if rb >= 1.0:
                b_lbl = "突破上轨"
            elif rb >= 0.8:
                b_lbl = "近上轨"
            elif rb <= 0.0:
                b_lbl = "跌破下轨"
            elif rb <= 0.2:
                b_lbl = "近下轨支撑"
            else:
                b_lbl = "中轨区间"
            parts.append(f"布林%B={pct_b:.2f} ({b_lbl}, 上轨 {upper:.1f}/下轨 {lower:.1f})")

        if highs:
            hi_52 = max(highs)
            if hi_52 > 0:
                dd = (cur - hi_52) / hi_52 * 100.0
                parts.append(f"距阶段高点 {dd:+.1f}%")

        return " | ".join(parts)
    except Exception as exc:
        print(f"[WARN] A股({code}) 220日K抓取失败: {exc}", file=sys.stderr)
        return ""


def _parse_sina_ctime(ctime: str) -> datetime | None:
    s = (ctime or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(s[:19], fmt)
        except Exception:
            continue
    return None


_SINA_NOISE_RE = re.compile(
    r"(?:"
    r"(?:控股股东|股东|实控人).{0,24}(?:解除)?质押|"
    r"(?:部分)?(?:股份|股权|股票)(?:解除)?质押|"
    r"质押\d+(?:\.\d+)?万股|"
    r"聘任.{1,8}为(?:公司)?(?:总经理|副总经理|董事会秘书|董秘|财务总监|证券事务代表)|"
    r"辞去(?:公司)?(?:董事|独立董事|监事|副总经理|财务总监|董秘)|"
    r"证监局：对.{2,30}(?:资本管理|私募|投资管理|资产管理).{0,12}出具警示函|"
    r"有投资者向.{1,25}提问.{0,120}(?:暂不涉及|没有回购计划|影响有限|尚存在不确定性|请关注公司公告)|"
    r"向.{1,16}(?:增资|提供(?:履约)?担保)"
    r")"
)

_SINA_HIGH_SIGNAL_RE = re.compile(
    r"(?:"
    r"A股收评|早评|LPR|贷款市场报价利率|中国人民银行|央行|美联储|国务院|政治局|国常会|"
    r"CPI|PPI|PMI|GDP|非农|社融|M2|降准|降息|逆回购|MLF|商务部|统计局|财政部|证监会|发改委|工信部|国资委|"
    r"关税|汇率|离岸人民币|黄金|原油|美债|算力|半导体|芯片|光模块|机器人|新能源|消费补贴|以旧换新|"
    r"业绩预告|业绩快报|财报|季报|年报|回购|增持|并购重组"
    r")"
)

_MARKET_RECAP_LINE_RE = re.compile(r"【(?:A股收评|午评)[：:]")


def _clean_cn_name(name: str) -> str:
    s = re.sub(r"\([^)]*\)", "", name or "")
    s = re.sub(r"(股份|集团|控股|有限|公司)$", "", s.strip())
    return s.strip()


def _tracked_core_names() -> tuple[str, ...]:
    names: list[str] = []
    for members in CN_SECTOR_POOL.values():
        for _, nm in members:
            core = _clean_cn_name(nm)
            if len(core) >= 2 and core not in names:
                names.append(core)
    return tuple(names)


def fetch_sina_telegraph(
    limit: int = 40,
    session_mode: str = "close",
    max_age_hours: int | None = None,
    now_dt: datetime | None = None,
) -> list[str]:
    """抓取新浪财经 7x24 小时滚动电报；盘前模式显式标注昨日 15:00 至今日开盘前的政策与行业催化。"""
    ref_now = now_dt or datetime.now()
    if max_age_hours is None:
        max_age_hours = 78 if (ref_now.weekday() == 0 and ref_now.hour < 12) else 36
    cutoff = ref_now - timedelta(hours=max_age_hours)
    core_names = _tracked_core_names()

    # 盘后至盘前窗口起点：周一取上周五 15:00，其余工作日取昨日 15:00
    days_back = 3 if ref_now.weekday() == 0 else 1
    post_close_start = (ref_now - timedelta(days=days_back)).replace(hour=15, minute=0, second=0, microsecond=0)

    collected: list[tuple[datetime, str, bool, str]] = []
    seen: set[str] = set()
    for tag_id, tag_label, pg_size in [(10, "A股市场", 28), (4, "公司/行业", 30), (0, "宏观/政策", 38)]:
        url = (
            "https://zhibo.sina.com.cn/api/zhibo/feed"
            f"?page=1&page_size={pg_size}&zhibo_id=152&tag_id={tag_id}&dire=f&dpc=1"
        )
        try:
            resp = DIRECT_SESSION.get(url, headers={"User-Agent": _UA}, timeout=10)
            resp.raise_for_status()
            feed_list = (
                resp.json()
                .get("result", {})
                .get("data", {})
                .get("feed", {})
                .get("list", [])
            )
            for row in feed_list:
                rich = row.get("rich_text") or ""
                txt = re.sub(r"<[^>]+>", "", html.unescape(rich)).strip()
                txt = re.sub(r"\s+", " ", txt)
                ctime = (row.get("create_time") or "").strip()
                dt = _parse_sina_ctime(ctime)
                if dt is not None and dt < cutoff:
                    continue
                if len(txt) < 20:
                    continue
                if _SINA_NOISE_RE.search(txt) and not any(nm in txt for nm in core_names):
                    continue
                key = txt[:40]
                if key not in seen:
                    seen.add(key)
                    sort_dt = dt or datetime.min
                    in_overnight = bool(session_mode == "premarket" and dt is not None and dt >= post_close_start)
                    win_tag = " · 盘后-盘前" if in_overnight else ""
                    is_high = bool(
                        _SINA_HIGH_SIGNAL_RE.search(txt)
                        or any(nm in txt for nm in core_names)
                        or (session_mode == "premarket" and in_overnight and tag_id == 0)
                    )
                    collected.append((sort_dt, ctime, is_high, f"[{ctime} · {tag_label}{win_tag}] {txt[:260]}"))
        except Exception as exc:
            print(f"[WARN] 新浪 7x24 电报(tag={tag_id})抓取异常: {exc}", file=sys.stderr)

    collected.sort(key=lambda x: (x[0], x[1]), reverse=True)
    if len(collected) <= limit:
        return [line for _, _, _, line in collected]

    high_quota = min(20, limit)
    picked_high = [item for item in collected if item[2]][:high_quota]
    picked_set = {item[3] for item in picked_high}
    rest_slots = limit - len(picked_high)
    picked_rest = [item for item in collected if item[3] not in picked_set][:rest_slots]
    final_items = picked_high + picked_rest
    final_items.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return [line for _, _, _, line in final_items]


def _format_std_symbol(code: str) -> str:
    if code.startswith("sh"):
        return f"{code[2:]}.SH"
    if code.startswith("sz"):
        return f"{code[2:]}.SZ"
    if code.startswith("us"):
        return code[2:]
    return code


def _select_uncovered_a_share_movers(
    cn_pool_sectors: dict[str, list[dict[str, Any]]],
    telegraph: list[str],
    max_total: int = 5,
) -> list[dict[str, Any]]:
    """筛出新浪电报未提及、但涨跌幅 >= 4.0% 的 A 股领涨/领跌龙头，用于 Exa 定向补盲。"""
    substantive_lines = [t for t in telegraph if not _MARKET_RECAP_LINE_RE.search(t)]
    blob = "\n".join(substantive_lines)
    by_sym: dict[str, tuple[float, dict[str, Any]]] = {}

    for sec in cn_pool_sectors.get("gainers", []) + cn_pool_sectors.get("losers", []):
        for st in sec.get("stocks", []):
            pct = st.get("pct", 0.0)
            if abs(pct) < 4.0:
                continue
            name = st["name"]
            core = _clean_cn_name(name)
            if core and len(core) >= 2 and core in blob:
                continue
            sym = _format_std_symbol(st["code"])
            if sym not in by_sym or abs(pct) > by_sym[sym][0]:
                by_sym[sym] = (
                    abs(pct),
                    {
                        "symbol": sym,
                        "name": name,
                        "pct_change": f"{pct:+.2f}%",
                        "sector": f"A股·{sec['name']}",
                    },
                )
    ordered = sorted(by_sym.values(), key=lambda x: -x[0])
    return [row for _, row in ordered[:max_total]]


def enrich_cn_macro_events(telegraph: list[str], today_str: str) -> list[str]:
    """当电报提及中国 LPR 等重要宏观事件但未公布实际数值时，用轻量 Exa /search 补全。"""
    blob = "\n".join(telegraph)
    lines: list[str] = []
    lpr_scheduled = ("LPR" in blob or "贷款市场报价利率" in blob)
    lpr_reported = bool(re.search(r"(?:1年期|一年期|5年期|五年期)LPR.{0,12}(?:为|报|维持|下调|上调)\s*\d+\.\d+%", blob))
    if lpr_scheduled and not lpr_reported:
        hits = exa_search_fallback(
            f"China LPR loan prime rate decision {today_str} 1-year 5-year PBOC 中国LPR报价",
            num_results=2,
            max_chars=700,
            start_published_date=today_str,
        )
        for h in hits:
            if h.get("text"):
                lines.append(f"· [中国LPR结果补全] {h['title']}：{h['text'][:350]} ({h['url']})")
    return lines


def _load_market_memory() -> list[dict[str, Any]]:
    """读取最近 5 个交易日的 A 股跨日盘面记忆。"""
    try:
        if MARKET_MEMORY.exists():
            data = json.loads(MARKET_MEMORY.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data.get("cn", []) or []
            if isinstance(data, list):
                return data
    except Exception as exc:
        print(f"[WARN] 读取 market_memory.json 失败: {exc}", file=sys.stderr)
    return []


def _load_latest_a_share_close_archive() -> tuple[str, str]:
    """读取最近一份本地 A 股收盘复盘归档（供次日 09:00 开盘前瞻联动前日收盘复盘）。"""
    if not ARCHIVE_DIR.exists():
        return "", ""
    candidates = sorted(ARCHIVE_DIR.glob("*_close.txt"), reverse=True)
    for p in candidates:
        try:
            txt = p.read_text(encoding="utf-8").strip()
            if txt:
                return p.stem.replace("_close", ""), txt[:3600]
        except Exception:
            continue
    return "", ""


def _load_latest_us_report(now: datetime | None = None) -> tuple[str, str, str]:
    """自动读取今早 09:00 刚跑完的《美股收盘 & 加密行情日报》（先读 logs/report_market.txt & report_news.txt，回退 archive/*.txt）。"""
    ref_now = now or datetime.now()
    m_path = US_BOT_DIR / "logs" / "report_market.txt"
    n_path = US_BOT_DIR / "logs" / "report_news.txt"
    us_market = ""
    us_news = ""
    us_source_label = ""

    # 优先读取 72 小时内新生成的 report_market.txt（涵盖周一沿用上周六刚生成的周五收盘稿）
    if m_path.exists():
        try:
            age_h = (ref_now.timestamp() - m_path.stat().st_mtime) / 3600.0
            if age_h <= 84:
                us_market = m_path.read_text(encoding="utf-8").strip()
                us_source_label = f"实时承接 us stock bot 最新成稿（更新于 {age_h:.1f} 小时前）"
        except Exception:
            pass
    if n_path.exists():
        try:
            age_h = (ref_now.timestamp() - n_path.stat().st_mtime) / 3600.0
            if age_h <= 84:
                us_news = n_path.read_text(encoding="utf-8").strip()
        except Exception:
            pass

    if not us_market:
        us_arch = US_BOT_DIR / "logs" / "archive"
        if us_arch.exists():
            for p in sorted(us_arch.glob("*.txt"), reverse=True):
                try:
                    raw = p.read_text(encoding="utf-8").strip()
                    if raw:
                        parts = re.split(r"\n\s*---+\s*\n", raw, maxsplit=1)
                        us_market = parts[0].strip()
                        us_news = parts[1].strip() if len(parts) > 1 else ""
                        us_source_label = f"承接 us stock bot 归档稿（{p.stem}）"
                        break
                except Exception:
                    continue

    # 提炼要闻的 30 秒速览段落，避免上下文冗余过长
    news_summary = ""
    if us_news:
        clean_n = re.sub(r"<[^>]+>", "", html.unescape(us_news))
        news_lines = [l.strip() for l in clean_n.splitlines() if l.strip()]
        picked = []
        for ln in news_lines:
            if ln.startswith("📋"):
                picked.append(ln)
            elif ln.startswith("- "):
                picked.append(ln)
            elif len(picked) >= 6:
                break
        news_summary = "\n".join(picked[:7])

    return us_source_label, us_market[:4200], news_summary


def _format_memory_block(cn_list: list[dict[str, Any]]) -> list[str]:
    if not cn_list:
        return []
    lines = [
        "",
        "==========================================",
        "【零、🧠 A股跨日盘面状态记忆（最近 5 个交易日轮动、量能与信号灯追踪）】",
        "==========================================",
    ]
    for row in cn_list[-MAX_MEMORY_DAYS:]:
        g_str = "、".join(row.get("top3_gainers", []))
        l_str = "、".join(row.get("top3_losers", []))
        lines.append(
            f"- [{row.get('date', '?')}] 指数：{row.get('indices', '?')} | 量能：{row.get('turnover', '?')}\n"
            f"  · 领涨Top3：{g_str} | 领跌Top3：{l_str}\n"
            f"  · 当日信号灯：{row.get('signal', '未知')} | 当日重点关注：{row.get('focus', '未知')}"
        )
    return lines


def _extract_signal_and_focus(report_text: str) -> tuple[str, str]:
    clean = re.sub(r"<[^>]+>", "", html.unescape(report_text))
    sig, focus = "未知", "未知"
    for raw_line in clean.splitlines():
        line = raw_line.strip().lstrip("·•- ")
        if line.startswith(("信号灯状态", "大盘信号灯", "早盘信号灯")):
            sig = re.sub(r"^[^：:]+[：:]\s*", "", line)[:90]
        elif "重点关注" in line[:16] or line.startswith("关注方向"):
            focus = re.sub(r"^[^：:]+[：:]\s*", "", line)[:110]
    return sig, focus


def _format_sector_block(title: str, sec_list: list[dict[str, Any]], is_gainer: bool) -> list[str]:
    lines = [f"\n{title}"]
    role_label = "板块内涨幅 Top 3" if is_gainer else "板块内跌幅 Top 3"
    for i, sec in enumerate(sec_list, 1):
        lines.append(f"\n{i}. 【{sec['name']}】 板块涨跌幅：{sec['pct']:+.2f}%")
        lines.append(f"   {role_label}：")
        stocks = sec.get("stocks") or []
        if not stocks:
            lines.append("   · （无符合方向的核心成分股）")
        for st in stocks:
            extra = f" | 换手 {st['turnover']:.2f}%" if st.get("turnover") else ""
            lines.append(
                f"   · {st['name']} ({st['code']})：{st['last']:.2f} ({st['pct']:+.2f}%) | 成交 {st['amount_yi']:.1f} 亿{extra}"
            )
    return lines


def _session_ran_today(log_file: Path, session_mode: str) -> bool:
    if os.getenv("FORCE_RUN") == "1" or not log_file.exists():
        return False
    today_prefix = datetime.now().strftime("%Y-%m-%d")
    tag = f"({session_mode})"
    try:
        for line in log_file.read_text(encoding="utf-8").splitlines():
            if line.startswith(today_prefix) and "[OK]" in line and tag in line:
                return True
    except Exception:
        return False
    return False


def _is_in_window(now: datetime, session_mode: str) -> tuple[bool, str]:
    if session_mode == "premarket":
        if now.hour < 8 or (now.hour == 8 and now.minute < 45):
            return False, "未到 09:00 开盘前瞻窗口（premarket），自动跳过"
        if now.hour > 9 or (now.hour == 9 and now.minute > 45):
            return False, "已过 09:45 开盘前瞻窗口（premarket），自动跳过"
    return True, ""


def run_fetch(session_mode: str) -> int:
    now = datetime.now()
    if os.getenv("FORCE_RUN") != "1":
        in_win, skip_msg = _is_in_window(now, session_mode)
        if not in_win:
            print("=== SKIP_EXPIRED ===")
            write_log("WARN", skip_msg)
            return 0

    if _session_ran_today(LOG_FILE, session_mode):
        print("=== SKIP_ALREADY_RAN ===")
        return 0

    if now.weekday() >= 5 and os.getenv("FORCE_RUN") != "1":
        print("=== SKIP_NON_TRADING_DAY ===")
        write_log("WARN", f"周末非交易日（{session_mode}），未出稿")
        return 0

    today_str = now.strftime("%Y-%m-%d")
    session_label = {
        "premarket": "开盘前瞻（09:00–09:30 盘前）",
        "close": "收盘复盘（16:15 盘后）",
    }.get(session_mode, "收盘复盘（16:15 盘后）")

    cn_pool_codes = [c for members in CN_SECTOR_POOL.values() for c, _ in members]
    all_codes = [c for c, _ in CN_INDICES] + cn_pool_codes + [c for c, _ in US_MAPPING]

    print(f"📡 正在抓取 A 股量化技术锚点、板块排行与隔夜海外映射（模式：{session_label}）...", file=sys.stderr)
    quotes = fetch_gtimg_quotes(all_codes)
    if not quotes:
        print("=== SKIP_NO_DATA ===")
        write_log("FAIL", f"A股行情接口返回为空（{session_mode}）")
        return 1

    today_compact = now.strftime("%Y%m%d")
    sh_date = re.sub(r"\D", "", quotes.get("sh000001", {}).get("time", ""))[:8]
    if session_mode == "close" and os.getenv("FORCE_RUN") != "1":
        if sh_date and sh_date != today_compact:
            print("=== SKIP_NON_TRADING_DAY ===")
            write_log("WARN", f"A股今日休市（{session_mode}；最近交易日 {sh_date}），未出稿")
            return 0

    cn_sina_sectors = fetch_sina_cn_sectors_top3()
    cn_pool_sectors = compute_pool_sectors_top3(CN_SECTOR_POOL, quotes)

    ma_targets = [c for c, _ in CN_INDICES]
    ma_map: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=5) as pool:
        futs = {
            c: pool.submit(fetch_ma_trend, c, quotes.get(c, {}).get("last", 0.0), today_str)
            for c in ma_targets
        }
        for c, fut in futs.items():
            ma_map[c] = fut.result()

    telegraph = fetch_sina_telegraph(limit=40, session_mode=session_mode)

    exa_catalyst_block = ""
    exa_macro_lines: list[str] = []
    if exa_enrich_enabled(default_enabled=True):
        if session_mode == "close":
            uncovered = _select_uncovered_a_share_movers(cn_pool_sectors, telegraph, max_total=5)
            if uncovered:
                print(
                    f"🔍 触发 Exa Agent A 股异动定向催化补盲（{len(uncovered)} 只）："
                    + "、".join(f"{u['name']}({u['pct_change']})" for u in uncovered),
                    file=sys.stderr,
                )
                confirmed = exa_agent_verify_catalysts(
                    uncovered,
                    market_label="中国A股",
                    session_date=today_str,
                    max_entities=5,
                )
                exa_catalyst_block = format_exa_catalyst_block(
                    confirmed,
                    title="【六、🔍 Exa Agent A股异动个股定向催化核查（仅保留交叉验证的真实催化）】",
                )
        exa_macro_lines = enrich_cn_macro_events(telegraph, today_str)

    sh_amt = quotes.get("sh000001", {}).get("amount_yi", 0.0)
    sz_amt = quotes.get("sz399001", {}).get("amount_yi", 0.0)
    cn_total_amt = sh_amt + sz_amt

    cn_stk_up = sum(1 for c in cn_pool_codes if quotes.get(c, {}).get("pct", 0) > 0)
    cn_stk_down = sum(1 for c in cn_pool_codes if quotes.get(c, {}).get("pct", 0) < 0)
    cn_sec_all = [
        sum(quotes.get(c, {}).get("pct", 0) for c, _ in members) / max(1, len(members))
        for members in CN_SECTOR_POOL.values()
    ]
    cn_sec_up = sum(1 for v in cn_sec_all if v > 0)
    cn_sec_down = sum(1 for v in cn_sec_all if v < 0)

    out: list[str] = [
        f"=== FETCH_{session_mode.upper()}_OK ===",
        "=== CONTEXT_BEGIN ===",
        f"今天日期：{today_str}",
        f"场次模式：{session_label}（{session_mode}）",
        f"A股最近收盘交易日：{sh_date[:4]}-{sh_date[4:6]}-{sh_date[6:8]}" if len(sh_date) == 8 else f"A股最近收盘交易日：{today_str}",
    ]

    mem_cn = _load_market_memory()
    out.extend(_format_memory_block(mem_cn))

    # 盘前模式：自动承接今早 09:00《美股收盘 & 加密行情日报》正稿 + 前一日 A 股收盘复盘归档
    if session_mode == "premarket":
        us_src, us_mkt_txt, us_news_sum = _load_latest_us_report(now)
        out.extend([
            "",
            "==========================================",
            f"【一、🌍 海外市场映射：承接今早美股与加密日报（{us_src or '未找到本地美股成稿，仅使用实时映射报价'}）】",
            "==========================================",
            "· 海外关键指数与产业链映射实时报价：",
        ])
        for code, label in US_MAPPING:
            q = quotes.get(code)
            if q:
                out.append(f"  - {label} ({code})：{q['last']} ({q['pct_str']})")
        if us_mkt_txt:
            out.append("\n· 今早《美股收盘 & 加密行情日报》全文参考（请直接引用其中 10Y 美债、VIX、伦敦金、原油、BTC、AI 五层分化与核心叙事）：")
            out.append(us_mkt_txt)
        if us_news_sum:
            out.append("\n· 今早《美股要闻》30 秒速览：")
            out.append(us_news_sum)

        prev_dt, prev_close_txt = _load_latest_a_share_close_archive()
        if prev_close_txt:
            out.extend([
                "",
                "==========================================",
                f"【二、📜 前一日 A 股收盘复盘归档（{prev_dt}，用于盘前主线延续性研判）】",
                "==========================================",
                prev_close_txt,
            ])
    else:
        out.extend([
            "",
            "==========================================",
            "【一、🌍 隔夜美股与海外资产参照报价】",
            "==========================================",
        ])
        for code, label in US_MAPPING:
            q = quotes.get(code)
            if q:
                out.append(f"- {label} ({code})：{q['last']} ({q['pct_str']})")

    idx_title = (
        "【三、🇨🇳 A股核心指数、前日市场宽度与 220 日量化技术锚点（MA5/20/50/200 + RSI14 + 布林%B）】"
        if session_mode == "premarket"
        else "【二、🇨🇳 A股今日核心指数、市场宽度与 220 日量化技术诊断（MA5/20/50/200 + RSI14 + 布林%B）】"
    )
    out.extend([
        "",
        "==========================================",
        idx_title,
        "==========================================",
        f"- 沪深两市合计成交额：{cn_total_amt:,.1f} 亿元（上证 {sh_amt:,.1f} 亿 + 深证 {sz_amt:,.1f} 亿）",
        f"- A股市场宽度：12 大核心主线板块 {cn_sec_up} 涨 {cn_sec_down} 跌 | 72 只行业龙头样本股 {cn_stk_up} 涨 {cn_stk_down} 跌",
    ])
    for code, label in CN_INDICES:
        q = quotes.get(code)
        if q:
            ma_info = f" | {ma_map[code]}" if ma_map.get(code) else ""
            out.append(f"- {label} ({code})：{q['last']:.2f} ({q['pct_str']}) | 振幅 {q['amplitude']:.2f}%{ma_info}")

    sec_title = (
        "【四、🇨🇳 A股前一日领涨/领跌 Top 3 板块与核心龙头（作为今日早盘延续/兑现对照）】"
        if session_mode == "premarket"
        else "【三、🇨🇳 A股今日领涨/领跌 Top 3 板块与板块内 Top 3 个股】"
    )
    out.extend([
        "",
        "==========================================",
        sec_title,
        "==========================================",
    ])
    if cn_sina_sectors:
        out.extend(_format_sector_block("🔥 A股全市场领涨 Top 3 行业板块（新浪行业榜）：", cn_sina_sectors["gainers"], is_gainer=True))
        out.extend(_format_sector_block("❄️ A股全市场领跌 Top 3 行业板块（新浪行业榜）：", cn_sina_sectors["losers"], is_gainer=False))
    out.extend(_format_sector_block("🧭 A股 12 大主线赛道涨幅 Top 3：", cn_pool_sectors["gainers"], is_gainer=True))
    out.extend(_format_sector_block("🧭 A股 12 大主线赛道跌幅 Top 3：", cn_pool_sectors["losers"], is_gainer=False))

    tele_title = (
        f"【五、📰 收盘后至开盘前（昨日 15:00 – 今日开盘）宏观政策与行业电报（最新 {len(telegraph)} 条）】"
        if session_mode == "premarket"
        else f"【四、📰 实时 7x24 财经电报与政策/行业/公司催化（最新 {len(telegraph)} 条）】"
    )
    out.extend([
        "",
        "==========================================",
        tele_title,
        "==========================================",
    ])
    for t in telegraph:
        out.append(f"· {t}")

    if exa_catalyst_block:
        out.extend(["", "==========================================", exa_catalyst_block, "=========================================="])
    if exa_macro_lines:
        out.extend([
            "",
            "==========================================",
            f"【七、📡 Exa 轻量检索补充（当日重要宏观/日程结果，共 {len(exa_macro_lines)} 条）】",
            "==========================================",
            *exa_macro_lines,
        ])

    out.append("=== CONTEXT_END ===")

    sh_q = quotes.get("sh000001", {})
    cy_q = quotes.get("sz399006", {})
    kc_q = quotes.get("sh000688", {})
    trade_date = sh_date or now.strftime("%Y%m%d")
    trade_date_fmt = f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:8]}" if len(trade_date) == 8 else today_str

    snapshot_for_memory = {
        "date": trade_date_fmt,
        "cn": {
            "date": trade_date_fmt,
            "indices": (
                f"上证 {sh_q.get('last', 0):.2f} ({sh_q.get('pct_str', '?')}) | "
                f"创业板 {cy_q.get('last', 0):.2f} ({cy_q.get('pct_str', '?')}) | "
                f"科创50 {kc_q.get('last', 0):.2f} ({kc_q.get('pct_str', '?')})"
            ),
            "turnover": f"{cn_total_amt:,.1f}亿",
            "top3_gainers": [f"{s['name']}({s['pct']:+.2f}%)" for s in cn_pool_sectors["gainers"]],
            "top3_losers": [f"{s['name']}({s['pct']:+.2f}%)" for s in cn_pool_sectors["losers"]],
        },
    }

    meta = {
        "log_summary": f"A股({session_mode}) 220日量化技术+板块Top3 ({len(quotes)}标的) + 电报 {len(telegraph)} 条",
        "metrics": {
            "session": session_mode,
            "quotes_count": len(quotes),
            "telegraph_count": len(telegraph),
        },
        "snapshot": snapshot_for_memory,
    }
    FETCH_META.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    emit_fetch_output(out, LAST_CONTEXT)
    return 0


def run_send(session_override: str | None = None) -> int:
    t0 = time.time()
    try:
        meta = json.loads(FETCH_META.read_text(encoding="utf-8")) if FETCH_META.exists() else {}
    except Exception:
        meta = {}
    sess_mode = session_override or meta.get("metrics", {}).get("session") or "close"

    if _session_ran_today(LOG_FILE, sess_mode):
        print(f"今天 A 股 {sess_mode} 场次已成功运行过，跳过发送。如需强制请设置 FORCE_RUN=1。", file=sys.stderr)
        return 0

    if not DRAFT_CN.exists():
        write_log("FAIL", f"A股稿件缺失（{sess_mode}；未找到 {DRAFT_CN.name}）", metrics={"session": sess_mode})
        return 1

    text_cn = DRAFT_CN.read_text(encoding="utf-8").strip()
    if not text_cn:
        write_log("FAIL", f"A股稿件为空（{sess_mode}）", metrics={"session": sess_mode})
        return 1

    clean_cn = sanitize_html(text_cn)
    save_pending([clean_cn])

    print("📨 正在推送《🇨🇳 A股报告》至 A 股飞书机器人...", file=sys.stderr)
    sent_cn = send_feishu(clean_cn, FEISHU_WEBHOOK_CN)
    print(f"  ✓ A股推送成功（{sent_cn} 条）", file=sys.stderr)
    CACHE_FILE.unlink(missing_ok=True)

    snap = meta.get("snapshot") or {}
    rtype = {"premarket": "开盘前瞻", "close": "收盘复盘"}.get(sess_mode, "收盘复盘")
    doc_date = datetime.now().strftime("%Y-%m-%d") if sess_mode == "premarket" else (snap.get("date") or datetime.now().strftime("%Y-%m-%d"))

    notion_meta = archive_and_sync_notion(
        text_cn,
        "🇨🇳 A股",
        date_str=doc_date,
        archive_dir=ARCHIVE_DIR,
        archive_filename=f"{doc_date}_{sess_mode}.txt",
        report_type=rtype,
    )
    notion_tag = " | Notion ✓" if notion_meta.get("notion_ok") else " | Notion ⚠️"

    try:
        if sess_mode == "close" and snap.get("cn"):
            cn_sig, cn_focus = _extract_signal_and_focus(text_cn)
            cn_entry = {**snap["cn"], "signal": cn_sig, "focus": cn_focus}
            cn_hist = [r for r in _load_market_memory() if r.get("date") != cn_entry["date"]]
            cn_hist.append(cn_entry)
            MARKET_MEMORY.write_text(
                json.dumps({"cn": cn_hist[-MAX_MEMORY_DAYS:]}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            print(f"  ✓ 已更新 A 股跨日盘面状态记忆库（保留最近 {len(cn_hist[-MAX_MEMORY_DAYS:])} 个交易日）", file=sys.stderr)
    except Exception as exc:
        print(f"[WARN] 更新 market_memory.json 失败: {exc}", file=sys.stderr)

    duration = round(time.time() - t0, 1)
    write_log(
        "OK",
        f"A股({len(text_cn)}字) 飞书推送成功 ({sess_mode}){notion_tag}",
        metrics={**(meta.get("metrics") or {}), "session": sess_mode, **notion_meta, "duration_s": duration, "cn_chars": len(text_cn)},
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="🇨🇳 A股智能决策日报（开盘前瞻 + 收盘复盘）")
    parser.add_argument("--mode", choices=["fetch", "send"], required=True)
    parser.add_argument("--session", choices=["premarket", "close"], default=None)
    args = parser.parse_args()
    if args.mode == "fetch":
        return run_fetch(session_mode=args.session or "close")
    return run_send(session_override=args.session)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        err = traceback.format_exc().strip().splitlines()[-1]
        write_log("FAIL", err)
        raise
