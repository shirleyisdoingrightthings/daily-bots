#!/usr/bin/python3
"""
A股 & 港股智能决策日报（开盘前瞻 09:00 + 午间缺早补午 12:05 + 收盘复盘 16:15）
借鉴 GitHub Trending 热门项目 ZhuLinsen/daily_stock_analysis 的「决策仪表盘 + 均线乖离率趋势诊断 + 市场宽度 + 舆情催化」框架，
并升级为「动态捕捉当日领涨 Top 3 板块 & 领跌 Top 3 板块 + 各板块内领涨/领跌 Top 3 个股」，告别固定死名单。

分别向「A股飞书机器人」与「港股飞书机器人」推送：
- 周一至周五 09:00（--session premarket，09:00–10:00 有效，过 10:00 自动跳过）：《🇨🇳 A股开盘前瞻》与《🇭🇰 港股开盘前瞻》
- 周一至周五 12:05（--session midday，12:00–13:00 有效，且仅在当天 premarket 未成功发送时触发）：《🇨🇳 A股早盘总结 & 午后前瞻》与《🇭🇰 港股早盘总结 & 午后前瞻》
- 周一至周五 16:15（--session close）：《🇨🇳 A股收盘复盘》与《🇭🇰 港股收盘复盘》
"""

from __future__ import annotations

import argparse
import html
import json
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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "shared"))
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
MAX_MEMORY_DAYS = 5

DRAFT_CN = BASE / "logs" / "report_cn.txt"
DRAFT_HK = BASE / "logs" / "report_hk.txt"

FEISHU_WEBHOOK_CN = os.getenv("FEISHU_WEBHOOK_CN", "")
FEISHU_WEBHOOK_HK = os.getenv("FEISHU_WEBHOOK_HK", "")

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

# 2. 港股核心指数
HK_INDICES = [
    ("hkHSI", "恒生指数"),
    ("hkHSTECH", "恒生科技指数"),
    ("hkHSCEI", "国企指数"),
]

# 3. 港股 10 大行业板块成分池（用于毫秒级动态计算当日港股领涨 Top 3 板块 & 领跌 Top 3 板块，以及各板块内的 Top 3 个股）
HK_SECTOR_POOL: dict[str, list[tuple[str, str]]] = {
    "科网平台与AI云": [
        ("hk00700", "腾讯控股"), ("hk09988", "阿里巴巴-W"), ("hk03690", "美团-W"),
        ("hk09618", "京东集团-SW"), ("hk09888", "百度集团-SW"), ("hk01024", "快手-W"),
    ],
    "半导体与硬科技": [
        ("hk00981", "中芯国际"), ("hk01347", "华虹半导体"), ("hk01810", "小米集团-W"),
        ("hk02382", "舜宇光学科技"), ("hk00285", "比亚迪电子"), ("hk02018", "瑞声科技"),
    ],
    "智能电动车与智驾": [
        ("hk01211", "比亚迪股份"), ("hk02015", "理想汽车-W"), ("hk09868", "小鹏汽车-W"),
        ("hk09866", "蔚来-SW"), ("hk00175", "吉利汽车"), ("hk09863", "零跑汽车"),
    ],
    "生物医药与创新药": [
        ("hk06160", "百济神州"), ("hk02269", "药明生物"), ("hk01801", "信达生物"),
        ("hk09926", "康方生物"), ("hk01093", "石药集团"), ("hk03692", "翰森制药"),
    ],
    "新消费与潮玩餐饮": [
        ("hk09992", "泡泡玛特"), ("hk09633", "农夫山泉"), ("hk06862", "海底捞"),
        ("hk09987", "百胜中国"), ("hk02020", "安踏体育"), ("hk00291", "华润啤酒"),
    ],
    "游戏内容与文旅出行": [
        ("hk09999", "网易-S"), ("hk09626", "哔哩哔哩-W"), ("hk09961", "携程集团-S"),
        ("hk00780", "同程旅行"), ("hk00772", "阅文集团"), ("hk03888", "金山软件"),
    ],
    "能源石油与电力绿电": [
        ("hk00883", "中国海洋石油"), ("hk00857", "中国石油股份"), ("hk00386", "中国石化"),
        ("hk01088", "中国神华"), ("hk00836", "华润电力"), ("hk00916", "龙源电力"),
    ],
    "有色金属与黄金资源": [
        ("hk02899", "紫金矿业"), ("hk01378", "中国宏桥"), ("hk03993", "洛阳钼业"),
        ("hk01818", "招金矿业"), ("hk01171", "兖矿能源"), ("hk02600", "中国铝业"),
    ],
    "大金融与港股流动性": [
        ("hk00388", "香港交易所"), ("hk02318", "中国平安"), ("hk01299", "友邦保险"),
        ("hk03968", "招商银行"), ("hk01398", "工商银行"), ("hk06030", "中信证券"),
    ],
    "高息电讯与地产公用": [
        ("hk00941", "中国移动"), ("hk00728", "中国电信"), ("hk00762", "中国联通"),
        ("hk01109", "华润置地"), ("hk00688", "中国海外发展"), ("hk00002", "中电控股"),
    ],
}

# 4. A股 12 大行业板块成分池（作为新浪/东财行业接口的毫秒级增强与兜底，确保 100% 稳定输出涨跌幅 Top 3 板块及各板块内 Top 3 个股）
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

# 5. 隔夜美股与热门中概股（09:00 开盘前瞻的核心跨市场映射）
US_MAPPING = [
    ("us.IXIC", "纳斯达克"),
    ("us.INX", "标普500"),
    ("usNVDA", "英伟达(算力映射)"),
    ("usTSLA", "特斯拉(智驾/机器人映射)"),
    ("usAAPL", "苹果(果链映射)"),
    ("usBABA", "阿里ADR"),
    ("usPDD", "拼多多"),
    ("usJD", "京东ADR"),
    ("usNTES", "网易ADR"),
    ("usBIDU", "百度ADR"),
    ("usLI", "理想ADR"),
    ("usXPEV", "小鹏ADR"),
    ("usFUTU", "富途控股"),
    ("usTCOM", "携程ADR"),
]


def _safe_float(v: Any) -> float:
    try:
        return float(v)
    except Exception:
        return 0.0


def fetch_gtimg_quotes(codes: list[str]) -> dict[str, dict[str, Any]]:
    """通过腾讯财经 qt.gtimg.cn 批量拉取 A股/港股/美股行情（支持分批请求）。"""
    if not codes:
        return {}
    unique_codes = list(dict.fromkeys(codes))
    out: dict[str, dict[str, Any]] = {}
    batch_size = 55
    for i in range(0, len(unique_codes), batch_size):
        batch = unique_codes[i:i + batch_size]
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
            if code.startswith("hk"):
                amount_yi = raw_amount / 1e8 if raw_amount else 0.0
            else:
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
    """尝试从新浪财经全市场行业接口获取 A股今日涨幅 Top 3 行业 & 跌幅 Top 3 行业，并拉取各行业内 Top 3 领涨/领跌个股。"""
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
            # asc=0 按涨幅从高到低取前3，asc=1 按涨幅从低到高（即跌幅最大）取前3
            s_url = (
                "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
                f"Market_Center.getHQNodeData?page=1&num=3&sort=changepercent&asc={asc}&node={node}"
            )
            r = DIRECT_SESSION.get(s_url, headers={"User-Agent": _UA}, timeout=8)
            rows = r.json() or []
            res = []
            for st in rows[:3]:
                res.append({
                    "code": st.get("symbol", ""),
                    "name": st.get("name", ""),
                    "last": _safe_float(st.get("trade", 0)),
                    "pct": _safe_float(st.get("changepercent", 0)),
                    "amount_yi": _safe_float(st.get("amount", 0)) / 1e8,
                    "turnover": _safe_float(st.get("turnoverratio", 0)),
                })
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
    """基于实时行情计算各板块涨跌幅均值，排出今日涨幅 Top 3 板块与跌幅 Top 3 板块，并提取每个板块内的 Top 3 领涨/领跌个股。"""
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
        sectors.append({
            "name": sec_name,
            "pct": avg_pct,
            "all_stocks": valid_stocks,
        })

    sectors_sorted = sorted(sectors, key=lambda x: x["pct"], reverse=True)
    gainers = []
    for sec in sectors_sorted[:3]:
        top3_up = sorted(sec["all_stocks"], key=lambda s: s["pct"], reverse=True)[:3]
        gainers.append({"name": sec["name"], "pct": sec["pct"], "stocks": top3_up})

    losers = []
    for sec in reversed(sectors_sorted[-3:]):
        top3_down = sorted(sec["all_stocks"], key=lambda s: s["pct"])[:3]
        losers.append({"name": sec["name"], "pct": sec["pct"], "stocks": top3_down})

    return {"gainers": gainers, "losers": losers}


def fetch_ma_trend(code: str, live_last: float = 0.0, today_str: str = "") -> str:
    """拉取最近 25 日 K 线，计算 MA5 / MA10 / MA20 与 MA5 乖离率，输出趋势状态。"""
    try:
        url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={code},day,,,25,qfq"
        resp = DIRECT_SESSION.get(url, headers={"User-Agent": _UA}, timeout=8)
        data = resp.json().get("data", {}).get(code, {})
        klines = data.get("day") or data.get("qfqday") or []
        if len(klines) < 19:
            return ""
        closes = [_safe_float(row[2]) for row in klines if len(row) >= 3]
        last_bar_date = str(klines[-1][0])[:10] if klines and len(klines[-1]) >= 1 else ""
        if live_last > 0 and today_str:
            if last_bar_date == today_str:
                closes[-1] = live_last
            else:
                closes.append(live_last)
        if len(closes) < 20 or not closes[-1]:
            return ""
        cur = closes[-1]
        ma5 = sum(closes[-5:]) / 5.0
        ma10 = sum(closes[-10:]) / 10.0
        ma20 = sum(closes[-20:]) / 20.0
        bias5 = (cur - ma5) / ma5 * 100.0 if ma5 else 0.0
        if ma5 > ma10 > ma20:
            trend = "🟢多头排列"
        elif ma5 < ma10 < ma20:
            trend = "🔴空头排列"
        elif cur >= ma20:
            trend = "🟡均线缠绕偏强(站上MA20)"
        else:
            trend = "🟡均线缠绕偏弱(跌破MA20)"
        return f"{trend} | MA5={ma5:.2f} (乖离率 {bias5:+.2f}%) | MA20={ma20:.2f}"
    except Exception:
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
    r"A股收评|港股收盘|午评|早评|LPR|贷款市场报价利率|中国人民银行|央行|美联储|日本央行|"
    r"CPI|PPI|PMI|GDP|非农|社融|降准|降息|商务部|统计局|财政部|证监会|国资委|"
    r"恒指公司|恒生指数.{0,10}检讨|纳入|剔除|黄金|原油|美债|离岸人民币|"
    r"大摩|摩根士丹利|高盛|瑞银|汇丰|巴克莱|摩根大通|花旗|美银|财报|季报|年报|业绩|目标价|评级"
    r")"
)

_MARKET_RECAP_LINE_RE = re.compile(r"【(?:港股收盘|A股收评|午评|港股午评)[：:]")


def _tracked_core_names() -> tuple[str, ...]:
    names: list[str] = []
    for pool in (CN_SECTOR_POOL, HK_SECTOR_POOL):
        for members in pool.values():
            for _, nm in members:
                core = _clean_cn_name(nm)
                if len(core) >= 2 and core not in names:
                    names.append(core)
    return tuple(names)


def fetch_sina_telegraph(
    limit: int = 38,
    max_age_hours: int | None = None,
    now_dt: datetime | None = None,
) -> list[str]:
    """抓取新浪财经 7x24 小时滚动电报（A股焦点 tag_id=10 + 公司/行业 tag_id=4 + 宏观全局 tag_id=0），免 Key 直连。

    过滤与保活策略：
    - 过滤超出时效窗口的陈旧条目（工作日默认 36h，周一上午放宽至 78h 以保留周五收评）；
    - 滤除非核心池小公司的股权质押、常规高管聘任、无实质互动易问答与私募警示函等盘后刷屏噪音；
    - 优先保活当日高信号条目（收评/午评/LPR/CPI/央行/大行评级/财报，最多预留 16 个名额），防止 16:10 盘后公告潮把上午重磅消息挤出窗口；
    - 最终按发布时间严格倒序输出前 limit 条。"""
    ref_now = now_dt or datetime.now()
    if max_age_hours is None:
        max_age_hours = 78 if (ref_now.weekday() == 0 and ref_now.hour < 12) else 36
    cutoff = ref_now - timedelta(hours=max_age_hours)
    core_names = _tracked_core_names()

    collected: list[tuple[datetime, str, bool, str]] = []
    seen: set[str] = set()
    for tag_id, tag_label, pg_size in [(10, "A股/港股市场", 25), (4, "公司/行业", 30), (0, "宏观/外围", 35)]:
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
                # 盘后刷屏噪音过滤：命中核心标的池或高信号关键词则放行，其余琐碎质押/人事/互动易直接过滤
                if _SINA_NOISE_RE.search(txt) and not any(nm in txt for nm in core_names):
                    continue
                key = txt[:40]
                if key not in seen:
                    seen.add(key)
                    sort_dt = dt or datetime.min
                    is_high = bool(_SINA_HIGH_SIGNAL_RE.search(txt) or any(nm in txt for nm in core_names))
                    collected.append((sort_dt, ctime, is_high, f"[{ctime} · {tag_label}] {txt[:260]}"))
        except Exception as exc:
            print(f"[WARN] 新浪 7x24 电报(tag={tag_id})抓取异常: {exc}", file=sys.stderr)

    collected.sort(key=lambda x: (x[0], x[1]), reverse=True)
    if len(collected) <= limit:
        return [line for _, _, _, line in collected]

    # 先保活最多 16 条高信号条目（确保上午 LPR/大行研报/午评不被 16:15 公告潮挤掉），再用其余最新条目填满 limit
    high_quota = min(16, limit)
    picked_high = [item for item in collected if item[2]][:high_quota]
    picked_set = {item[3] for item in picked_high}
    rest_slots = limit - len(picked_high)
    picked_rest = [item for item in collected if item[3] not in picked_set][:rest_slots]
    final_items = picked_high + picked_rest
    final_items.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return [line for _, _, _, line in final_items]


_ADR_DUAL_CANONICAL = {
    "usBABA": ("BABA / 09988.HK", "阿里巴巴"),
    "hk09988": ("BABA / 09988.HK", "阿里巴巴"),
    "usJD": ("JD / 09618.HK", "京东集团"),
    "hk09618": ("JD / 09618.HK", "京东集团"),
    "usNTES": ("NTES / 09999.HK", "网易"),
    "hk09999": ("NTES / 09999.HK", "网易"),
    "usBIDU": ("BIDU / 09888.HK", "百度集团"),
    "hk09888": ("BIDU / 09888.HK", "百度集团"),
    "usLI": ("LI / 02015.HK", "理想汽车"),
    "hk02015": ("LI / 02015.HK", "理想汽车"),
    "usXPEV": ("XPEV / 09868.HK", "小鹏汽车"),
    "hk09868": ("XPEV / 09868.HK", "小鹏汽车"),
    "usTCOM": ("TCOM / 09961.HK", "携程集团"),
    "hk09961": ("TCOM / 09961.HK", "携程集团"),
    "usPDD": ("PDD", "拼多多"),
    "usFUTU": ("FUTU", "富途控股"),
}

_CN_ADR_CODES = ("usBABA", "usPDD", "usJD", "usNTES", "usBIDU", "usLI", "usXPEV", "usFUTU", "usTCOM")


def _clean_cn_name(name: str) -> str:
    s = re.sub(r"\([^)]*\)", "", name or "")
    s = re.sub(r"(-SW|-W|-S|ADR|股份|集团|控股|有限|公司)$", "", s.strip())
    return s.strip()


def _format_std_symbol(code: str) -> str:
    if code in _ADR_DUAL_CANONICAL:
        return _ADR_DUAL_CANONICAL[code][0]
    if code.startswith("hk"):
        return f"{code[2:]}.HK"
    if code.startswith("sh"):
        return f"{code[2:]}.SH"
    if code.startswith("sz"):
        return f"{code[2:]}.SZ"
    if code.startswith("us"):
        return code[2:]
    return code


def _select_uncovered_cn_hk_movers(
    session_mode: str,
    quotes: dict[str, dict[str, Any]],
    cn_pool_sectors: dict[str, list[dict[str, Any]]],
    hk_pool_sectors: dict[str, list[dict[str, Any]]],
    telegraph: list[str],
    max_total: int = 6,
) -> list[dict[str, Any]]:
    """筛出新浪电报未提及、但涨跌幅显著的 A股/港股/中概异动龙头，合并两地上市标的并去重。

    排除纯报价罗列型收评/午评（如“网易跌17.60%”），避免误判为“已有催化解释”；
    名额优先向港股/中概股倾斜（Exa Financial Markets 对英文财报/SEC 6-K/港交所/大行评级命中率远高于纯 A 股）。"""
    substantive_lines = [t for t in telegraph if not _MARKET_RECAP_LINE_RE.search(t)]
    blob = "\n".join(substantive_lines)
    raw_candidates: list[tuple[str, float, dict[str, Any]]] = []

    def _is_covered(name: str, sym: str) -> bool:
        core = _clean_cn_name(name)
        if core and len(core) >= 2 and core in blob:
            return True
        if sym and len(sym) >= 4 and sym.split()[0] in blob:
            return True
        return False

    def _add(bucket: str, code: str, name: str, pct: float, sector: str) -> None:
        std_sym = _format_std_symbol(code)
        canon_name = _ADR_DUAL_CANONICAL[code][1] if code in _ADR_DUAL_CANONICAL else name
        if _is_covered(canon_name, std_sym):
            return
        raw_candidates.append((
            bucket,
            abs(pct),
            {
                "symbol": std_sym,
                "name": canon_name,
                "pct_change": f"{pct:+.2f}%",
                "sector": sector,
            },
        ))

    if session_mode == "premarket":
        for code, label in US_MAPPING:
            if code not in _CN_ADR_CODES:
                continue
            q = quotes.get(code)
            if q and abs(q.get("pct", 0.0)) >= 3.5:
                _add("hk_adr", code, label, q["pct"], "隔夜中概ADR")
        for sec in hk_pool_sectors.get("gainers", []) + hk_pool_sectors.get("losers", []):
            for st in sec.get("stocks", []):
                if abs(st.get("pct", 0.0)) >= 4.0:
                    _add("hk_adr", st["code"], st["name"], st["pct"], f"港股·{sec['name']}")
        for sec in cn_pool_sectors.get("gainers", []) + cn_pool_sectors.get("losers", []):
            for st in sec.get("stocks", []):
                if abs(st.get("pct", 0.0)) >= 4.0:
                    _add("cn", st["code"], st["name"], st["pct"], f"A股·{sec['name']}")
    else:
        for sec in cn_pool_sectors.get("gainers", []) + cn_pool_sectors.get("losers", []):
            for st in sec.get("stocks", []):
                # A股提高阈值至 >=4.0%，避免普跌/普涨日大量随波逐流的纯A股占满 Exa 额度
                if abs(st.get("pct", 0.0)) >= 4.0:
                    _add("cn", st["code"], st["name"], st["pct"], f"A股·{sec['name']}")
        for sec in hk_pool_sectors.get("gainers", []) + hk_pool_sectors.get("losers", []):
            for st in sec.get("stocks", []):
                if abs(st.get("pct", 0.0)) >= 3.0:
                    _add("hk_adr", st["code"], st["name"], st["pct"], f"港股·{sec['name']}")

    # 按 canonical symbol 去重，保留涨跌幅绝对值更大的一条（如网易 ADR -11.39% 与网易-S -14.39% 合并为 1 个名额）
    by_sym: dict[str, tuple[str, float, dict[str, Any]]] = {}
    for bucket, mag, row in raw_candidates:
        key = row["symbol"]
        if key not in by_sym or mag > by_sym[key][1]:
            by_sym[key] = (bucket, mag, row)

    cn_rows = sorted([v for v in by_sym.values() if v[0] == "cn"], key=lambda x: -x[1])
    hk_rows = sorted([v for v in by_sym.values() if v[0] == "hk_adr"], key=lambda x: -x[1])

    picked: list[dict[str, Any]] = []
    if session_mode == "premarket":
        # 盘前优先保障中概/港股大异动（最多 4 只）与 A 股竞价极端异动（至少 2 只）
        picked.extend(r[2] for r in hk_rows[:4])
        picked.extend(r[2] for r in cn_rows[: max(2, max_total - len(picked))])
        if len(picked) < max_total:
            rest = [r[2] for r in hk_rows[4:] + cn_rows[max(2, max_total - len(hk_rows[:4])) :]]
            picked.extend(rest[: max_total - len(picked)])
    else:
        # 午盘/收盘：名额向港股/中概倾斜（港股最多 4 只 + A股最多 2 只），单边不足则由另一边递补
        picked.extend(r[2] for r in hk_rows[:4])
        cn_cap = max(2, max_total - len(picked))
        picked.extend(r[2] for r in cn_rows[:cn_cap])
        if len(picked) < max_total:
            used = {x["symbol"] for x in picked}
            extras = sorted(
                [r for r in hk_rows[4:] + cn_rows[cn_cap:] if r[2]["symbol"] not in used],
                key=lambda x: -x[1],
            )
            picked.extend(r[2] for r in extras[: max_total - len(picked)])

    return picked[:max_total]


def enrich_cn_hk_macro_events(telegraph: list[str], today_str: str, session_mode: str) -> list[str]:
    """当电报出现重要日程提醒（如中国 LPR / 恒指季检）但未公布实际结果时，用轻量 Exa /search 补全。"""
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

    hsi_review = ("恒指公司" in blob or "恒生指数" in blob and "检讨" in blob)
    if session_mode == "close" and hsi_review and "纳入" not in blob and "剔除" not in blob:
        hits = exa_search_fallback(
            f"Hang Seng Indexes quarterly review results {today_str} 恒生指数季检结果 纳入 剔除",
            num_results=2,
            max_chars=700,
            start_published_date=today_str,
        )
        for h in hits:
            if h.get("text"):
                lines.append(f"· [恒指季检补全] {h['title']}：{h['text'][:350]} ({h['url']})")

    return lines


def _load_market_memory() -> dict[str, list[dict[str, Any]]]:
    """读取最近 5 个交易日的 A股/港股跨日盘面记忆（借鉴 claude-quickstarts State Store 设计）。"""
    try:
        if MARKET_MEMORY.exists():
            data = json.loads(MARKET_MEMORY.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {"cn": data.get("cn", []), "hk": data.get("hk", [])}
    except Exception as exc:
        print(f"[WARN] 读取 market_memory.json 失败: {exc}", file=sys.stderr)
    return {"cn": [], "hk": []}


def _format_memory_block(mem: dict[str, list[dict[str, Any]]]) -> list[str]:
    """将最近 5 个交易日的板块轮动、信号灯与关注方向格式化进 Context。"""
    cn_list = mem.get("cn") or []
    hk_list = mem.get("hk") or []
    if not cn_list and not hk_list:
        return []
    lines = [
        "",
        "==========================================",
        "【零、🧠 跨日盘面状态记忆（最近 5 个交易日板块轮动、信号灯与关注方向追踪）】",
        "==========================================",
        "（写稿提示：请对比下方前几日数据，在正文中自然点出某板块是“连续多日领涨/领跌”还是“昨日领涨今日转领跌の一日游”，并在信号灯与分析师观点中复盘前日关注方向与信号灯变化。）",
    ]
    if cn_list:
        lines.append("\n🇨🇳 A股近期盘面记忆：")
        for row in cn_list[-MAX_MEMORY_DAYS:]:
            g_str = "、".join(row.get("top3_gainers", []))
            l_str = "、".join(row.get("top3_losers", []))
            lines.append(
                f"- [{row.get('date', '?')}] 指数：{row.get('indices', '?')} | 量能：{row.get('turnover', '?')}\n"
                f"  · 领涨Top3：{g_str} | 领跌Top3：{l_str}\n"
                f"  · 当日信号灯：{row.get('signal', '未知')} | 当日重点关注：{row.get('focus', '未知')}"
            )
    if hk_list:
        lines.append("\n🇭🇰 港股近期盘面记忆：")
        for row in hk_list[-MAX_MEMORY_DAYS:]:
            g_str = "、".join(row.get("top3_gainers", []))
            l_str = "、".join(row.get("top3_losers", []))
            lines.append(
                f"- [{row.get('date', '?')}] 指数：{row.get('indices', '?')}\n"
                f"  · 领涨Top3：{g_str} | 领跌Top3：{l_str}\n"
                f"  · 当日信号灯：{row.get('signal', '未知')} | 当日重点关注：{row.get('focus', '未知')}"
            )
    return lines


def _extract_signal_and_focus(report_text: str) -> tuple[str, str]:
    """从写好的收盘复盘稿件中提取「信号灯状态」和「👀 重点关注」摘要。"""
    clean = re.sub(r"<[^>]+>", "", html.unescape(report_text))
    sig = "未知"
    focus = "未知"
    for raw_line in clean.splitlines():
        line = raw_line.strip().lstrip("·•- ")
        if line.startswith("信号灯状态") or line.startswith("大盘信号灯") or line.startswith("港股信号灯"):
            sig = re.sub(r"^[^：:]+[：:]\s*", "", line)[:90]
        elif "重点关注" in line[:16] or line.startswith("关注方向"):
            focus = re.sub(r"^[^：:]+[：:]\s*", "", line)[:110]
    return sig, focus


def _format_sector_block(title: str, sec_list: list[dict[str, Any]], is_gainer: bool, is_hk: bool = False) -> list[str]:
    lines = [f"\n{title}"]
    role_label = "板块内涨幅 Top 3" if is_gainer else "板块内跌幅 Top 3"
    ccy = "亿港元" if is_hk else "亿"
    for i, sec in enumerate(sec_list, 1):
        lines.append(f"\n{i}. 【{sec['name']}】 板块涨跌幅：{sec['pct']:+.2f}%")
        lines.append(f"   {role_label}：")
        for st in sec.get("stocks", []):
            extra = f" | 换手 {st['turnover']:.2f}%" if (not is_hk and st.get("turnover")) else ""
            lines.append(
                f"   · {st['name']} ({st['code']})：{st['last']:.2f} ({st['pct']:+.2f}%) | 成交 {st['amount_yi']:.1f} {ccy}{extra}"
            )
    return lines


def _session_ran_today(log_file: Path, session_mode: str) -> bool:
    """按场次判定今天是否已成功推送（避免 09:00 premarket 的 [OK] 误拦 16:15 close）。"""
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
    """校验场次时效窗口，返回 (是否放行, 跳过日志文案)。"""
    if session_mode == "premarket":
        if now.hour < 9:
            return False, "未到 09:00 开盘前瞻窗口（premarket），自动跳过"
        if now.hour > 10 or (now.hour == 10 and now.minute > 0):
            return False, "已过 10:00 开盘前瞻窗口（premarket），自动跳过"
    elif session_mode == "midday":
        if now.hour < 12:
            return False, "未到 12:00 午间窗口（midday），自动跳过"
        if now.hour > 13 or (now.hour == 13 and now.minute > 0):
            return False, "已过 13:00 午间窗口（midday），自动跳过"
    return True, ""


def run_fetch(session_mode: str) -> int:
    now = datetime.now()
    if os.getenv("FORCE_RUN") != "1":
        in_win, skip_msg = _is_in_window(now, session_mode)
        if not in_win:
            print("=== SKIP_EXPIRED ===")
            write_log("WARN", skip_msg)
            return 0

        if session_mode == "midday" and _session_ran_today(LOG_FILE, "premarket"):
            print("=== SKIP_PREMARKET_ALREADY_RAN ===")
            write_log("WARN", "今日已正常推送开盘前瞻（premarket），午间场（midday）自动跳过不重复打扰")
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
        "premarket": "开盘前瞻（09:00 盘前）",
        "midday": "早盘收盘总结 & 午后前瞻（12:05 午休）",
        "close": "收盘复盘（16:15 盘后）",
    }.get(session_mode, "收盘复盘（16:15 盘后）")

    cn_pool_codes = [c for members in CN_SECTOR_POOL.values() for c, _ in members]
    hk_pool_codes = [c for members in HK_SECTOR_POOL.values() for c, _ in members]

    all_codes = (
        [c for c, _ in CN_INDICES]
        + cn_pool_codes
        + [c for c, _ in HK_INDICES]
        + hk_pool_codes
        + [c for c, _ in US_MAPPING]
    )
    print(f"📡 正在抓取 A股 / 港股动态板块排行 & 各板块 Top 3 个股（模式：{session_label}）...", file=sys.stderr)
    quotes = fetch_gtimg_quotes(all_codes)
    if not quotes:
        print("=== SKIP_NO_DATA ===")
        write_log("FAIL", f"行情接口返回为空（{session_mode}）")
        return 1

    today_compact = now.strftime("%Y%m%d")
    sh_date = re.sub(r"\D", "", quotes.get("sh000001", {}).get("time", ""))[:8]
    hk_date = re.sub(r"\D", "", quotes.get("hkHSI", {}).get("time", ""))[:8]
    if session_mode in ("midday", "close") and os.getenv("FORCE_RUN") != "1":
        if sh_date and hk_date and sh_date != today_compact and hk_date != today_compact:
            print("=== SKIP_NON_TRADING_DAY ===")
            write_log("WARN", f"沪深港今日均休市（{session_mode}；A股最近 {sh_date} / 港股最近 {hk_date}），未出稿")
            return 0

    # A股优先拉取新浪全市场行业实时涨跌 Top 3 + 行业内 Top 3 个股，同时提供 12 大主线板块池对比
    cn_sina_sectors = fetch_sina_cn_sectors_top3()
    cn_pool_sectors = compute_pool_sectors_top3(CN_SECTOR_POOL, quotes)
    hk_pool_sectors = compute_pool_sectors_top3(HK_SECTOR_POOL, quotes)

    ma_targets = [c for c, _ in CN_INDICES] + [c for c, _ in HK_INDICES]
    ma_map: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=6) as pool:
        futs = {
            c: pool.submit(fetch_ma_trend, c, quotes.get(c, {}).get("last", 0.0), today_str)
            for c in ma_targets
        }
        for c, fut in futs.items():
            ma_map[c] = fut.result()

    telegraph = fetch_sina_telegraph(limit=38)

    exa_catalyst_block = ""
    exa_macro_lines: list[str] = []
    if exa_enrich_enabled(default_enabled=True):
        uncovered = _select_uncovered_cn_hk_movers(
            session_mode, quotes, cn_pool_sectors, hk_pool_sectors, telegraph, max_total=6
        )
        if uncovered:
            print(
                f"🔍 触发 Exa Agent 异动股定向催化补盲（{len(uncovered)} 只未命中电报）："
                + "、".join(f"{u['name']}({u['pct_change']})" for u in uncovered),
                file=sys.stderr,
            )
            confirmed = exa_agent_verify_catalysts(
                uncovered,
                market_label="A股与港股/中概股",
                session_date=today_str,
                max_entities=6,
            )
            exa_catalyst_block = format_exa_catalyst_block(
                confirmed,
                title="【七、🔍 Exa Agent 异动个股定向催化核查（仅保留经交叉验证的真实催化）】",
            )
        exa_macro_lines = enrich_cn_hk_macro_events(telegraph, today_str, session_mode)

    sh_amt = quotes.get("sh000001", {}).get("amount_yi", 0.0)
    sz_amt = quotes.get("sz399001", {}).get("amount_yi", 0.0)
    cn_total_amt = sh_amt + sz_amt

    # 计算 A股与港股市场宽度（板块涨跌比 + 核心成分股涨跌比）
    cn_stk_up = sum(1 for c in cn_pool_codes if quotes.get(c, {}).get("pct", 0) > 0)
    cn_stk_down = sum(1 for c in cn_pool_codes if quotes.get(c, {}).get("pct", 0) < 0)
    cn_sec_all = [
        sum(quotes.get(c, {}).get("pct", 0) for c, _ in members) / max(1, len(members))
        for members in CN_SECTOR_POOL.values()
    ]
    cn_sec_up = sum(1 for v in cn_sec_all if v > 0)
    cn_sec_down = sum(1 for v in cn_sec_all if v < 0)

    hk_stk_up = sum(1 for c in hk_pool_codes if quotes.get(c, {}).get("pct", 0) > 0)
    hk_stk_down = sum(1 for c in hk_pool_codes if quotes.get(c, {}).get("pct", 0) < 0)
    hk_sec_all = [
        sum(quotes.get(c, {}).get("pct", 0) for c, _ in members) / max(1, len(members))
        for members in HK_SECTOR_POOL.values()
    ]
    hk_sec_up = sum(1 for v in hk_sec_all if v > 0)
    hk_sec_down = sum(1 for v in hk_sec_all if v < 0)

    out: list[str] = [
        f"=== FETCH_{session_mode.upper()}_OK ===",
        "=== CONTEXT_BEGIN ===",
        f"今天日期：{today_str}",
        f"场次模式：{session_label}（{session_mode}）",
    ]
    if session_mode in ("midday", "close"):
        if sh_date and sh_date != today_compact:
            out.append(f"⚠️ 单边休市提示：A股今日休市（最近交易日 {sh_date}），请勿将前日数据误报为今日早盘/收盘表现")
        if hk_date and hk_date != today_compact:
            out.append(f"⚠️ 单边休市提示：港股今日休市（最近交易日 {hk_date}），请勿将前日数据误报为今日早盘/收盘表现")
    mem = _load_market_memory()
    out.extend(_format_memory_block(mem))
    out.extend([
        "",
        "==========================================",
        "【一、隔夜美股与中概股先行映射指标】",
        "==========================================",
    ])
    for code, label in US_MAPPING:
        q = quotes.get(code)
        if q:
            out.append(f"- {label} ({code})：{q['last']} ({q['pct_str']})")

    if session_mode == "midday":
        prev_cn = (mem.get("cn") or [{}])[-1]
        prev_turnover_hint = (
            f" | 前一交易日({prev_cn.get('date', '?')})全天成交：{prev_cn.get('turnover', '未知')}"
            if prev_cn.get("turnover")
            else ""
        )
        amt_line = f"- 沪深两市早盘半日合计成交额：{cn_total_amt:,.1f} 亿元（上证 {sh_amt:,.1f} 亿 + 深证 {sz_amt:,.1f} 亿{prev_turnover_hint}）"
        cn_sec_heading = "【三、🇨🇳 A股早盘涨幅 Top 3 板块 & 跌幅 Top 3 板块（含各板块内 Top 3 个股）】"
        cn_sina_up = "🔥 A股早盘全市场领涨 Top 3 行业板块（新浪实时行业榜）："
        cn_sina_dn = "❄️ A股早盘全市场领跌 Top 3 行业板块（新浪实时行业榜）："
        hk_sec_heading = "【五、🇭🇰 港股早盘涨幅 Top 3 板块 & 跌幅 Top 3 板块（含各板块内 Top 3 个股）】"
        hk_up_title = "🔥 港股早盘领涨 Top 3 板块（及板块内涨幅 Top 3 个股）："
        hk_dn_title = "❄️ 港股早盘领跌 Top 3 板块（及板块内跌幅 Top 3 个股）："
    else:
        amt_line = f"- 沪深两市合计成交额：{cn_total_amt:,.1f} 亿元（上证 {sh_amt:,.1f} 亿 + 深证 {sz_amt:,.1f} 亿）"
        cn_sec_heading = "【三、🇨🇳 A股今日涨幅 Top 3 板块 & 跌幅 Top 3 板块（含各板块内 Top 3 个股）】"
        cn_sina_up = "🔥 A股全市场领涨 Top 3 行业板块（新浪实时行业榜）："
        cn_sina_dn = "❄️ A股全市场领跌 Top 3 行业板块（新浪实时行业榜）："
        hk_sec_heading = "【五、🇭🇰 港股今日涨幅 Top 3 板块 & 跌幅 Top 3 板块（含各板块内 Top 3 个股）】"
        hk_up_title = "🔥 港股今日领涨 Top 3 板块（及板块内涨幅 Top 3 个股）："
        hk_dn_title = "❄️ 港股今日领跌 Top 3 板块（及板块内跌幅 Top 3 个股）："

    out.extend([
        "",
        "==========================================",
        "【二、🇨🇳 A股核心指数、市场宽度与技术面诊断（含 MA5/MA20 乖离率）】",
        "==========================================",
        amt_line,
        f"- A股市场宽度：12大核心主线板块 {cn_sec_up} 涨 {cn_sec_down} 跌 | 72只行业龙头样本股 {cn_stk_up} 涨 {cn_stk_down} 跌",
    ])
    for code, label in CN_INDICES:
        q = quotes.get(code)
        if q:
            ma_info = f" | {ma_map[code]}" if ma_map.get(code) else ""
            out.append(
                f"- {label} ({code})：{q['last']:.2f} ({q['pct_str']}) | "
                f"振幅 {q['amplitude']:.2f}%{ma_info}"
            )

    out.extend([
        "",
        "==========================================",
        cn_sec_heading,
        "==========================================",
    ])
    if cn_sina_sectors:
        out.extend(_format_sector_block(cn_sina_up, cn_sina_sectors["gainers"], is_gainer=True, is_hk=False))
        out.extend(_format_sector_block(cn_sina_dn, cn_sina_sectors["losers"], is_gainer=False, is_hk=False))
    out.extend(_format_sector_block("🧭 A股核心主线赛道涨幅 Top 3（及赛道内领涨 Top 3 龙头）：", cn_pool_sectors["gainers"], is_gainer=True, is_hk=False))
    out.extend(_format_sector_block("🧭 A股核心主线赛道跌幅 Top 3（及赛道内领跌 Top 3 龙头）：", cn_pool_sectors["losers"], is_gainer=False, is_hk=False))

    out.extend([
        "",
        "==========================================",
        "【四、🇭🇰 港股核心指数、市场宽度与技术面诊断（含 MA5/MA20 乖离率）】",
        "==========================================",
        f"- 港股市场宽度：10大核心行业板块 {hk_sec_up} 涨 {hk_sec_down} 跌 | 60只核心样本股 {hk_stk_up} 涨 {hk_stk_down} 跌",
    ])
    for code, label in HK_INDICES:
        q = quotes.get(code)
        if q:
            ma_info = f" | {ma_map[code]}" if ma_map.get(code) else ""
            out.append(
                f"- {label} ({code})：{q['last']:.2f} ({q['pct_str']}) | "
                f"振幅 {q['amplitude']:.2f}%{ma_info}"
            )

    out.extend([
        "",
        "==========================================",
        hk_sec_heading,
        "==========================================",
    ])
    out.extend(_format_sector_block(hk_up_title, hk_pool_sectors["gainers"], is_gainer=True, is_hk=True))
    out.extend(_format_sector_block(hk_dn_title, hk_pool_sectors["losers"], is_gainer=False, is_hk=True))

    out.extend([
        "",
        "==========================================",
        f"【六、实时 7x24 财经电报与政策/行业/公司催化（最新 {len(telegraph)} 条）】",
        "==========================================",
    ])
    for t in telegraph:
        out.append(f"· {t}")

    if exa_catalyst_block:
        out.extend([
            "",
            "==========================================",
            exa_catalyst_block,
            "==========================================",
        ])

    if exa_macro_lines:
        out.extend([
            "",
            "==========================================",
            f"【八、📡 Exa 轻量检索补充（当日重要宏观/日程结果，共 {len(exa_macro_lines)} 条）】",
            "==========================================",
        ])
        out.extend(exa_macro_lines)

    out.append("=== CONTEXT_END ===")

    sh_q = quotes.get("sh000001", {})
    cy_q = quotes.get("sz399006", {})
    kc_q = quotes.get("sh000688", {})
    hsi_q = quotes.get("hkHSI", {})
    hstech_q = quotes.get("hkHSTECH", {})
    hscei_q = quotes.get("hkHSCEI", {})
    trade_date = (sh_q.get("time", "")[:8] or now.strftime("%Y%m%d"))
    if len(trade_date) == 8 and trade_date.isdigit():
        trade_date_fmt = f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:8]}"
    else:
        trade_date_fmt = now.strftime("%Y-%m-%d")

    snapshot_for_memory = {
        "date": trade_date_fmt,
        "cn": {
            "date": trade_date_fmt,
            "indices": (
                f"上证 {sh_q.get('last', 0):.2f}({sh_q.get('pct_str', '?')}) | "
                f"创业板 {cy_q.get('last', 0):.2f}({cy_q.get('pct_str', '?')}) | "
                f"科创50 {kc_q.get('last', 0):.2f}({kc_q.get('pct_str', '?')})"
            ),
            "turnover": f"{cn_total_amt:,.1f}亿",
            "top3_gainers": [f"{s['name']}({s['pct']:+.2f}%)" for s in cn_pool_sectors["gainers"]],
            "top3_losers": [f"{s['name']}({s['pct']:+.2f}%)" for s in cn_pool_sectors["losers"]],
        },
        "hk": {
            "date": trade_date_fmt,
            "indices": (
                f"恒指 {hsi_q.get('last', 0):.2f}({hsi_q.get('pct_str', '?')}) | "
                f"恒科 {hstech_q.get('last', 0):.2f}({hstech_q.get('pct_str', '?')}) | "
                f"国企 {hscei_q.get('last', 0):.2f}({hscei_q.get('pct_str', '?')})"
            ),
            "top3_gainers": [f"{s['name']}({s['pct']:+.2f}%)" for s in hk_pool_sectors["gainers"]],
            "top3_losers": [f"{s['name']}({s['pct']:+.2f}%)" for s in hk_pool_sectors["losers"]],
        },
    }

    meta = {
        "log_summary": f"A股/港股({session_mode}) 动态板块Top3+个股Top3 ({len(quotes)}标的) + 电报 {len(telegraph)} 条",
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
        print(f"今天 {sess_mode} 场次已成功运行过，跳过发送。如需强制请设置 FORCE_RUN=1。", file=sys.stderr)
        return 0

    if not DRAFT_CN.exists() or not DRAFT_HK.exists():
        write_log("FAIL", f"A股或港股稿件缺失（{sess_mode}；需同时存在 {DRAFT_CN.name} 与 {DRAFT_HK.name}）", metrics={"session": sess_mode})
        return 1

    text_cn = DRAFT_CN.read_text(encoding="utf-8").strip()
    text_hk = DRAFT_HK.read_text(encoding="utf-8").strip()
    if not text_cn or not text_hk:
        write_log("FAIL", f"A股或港股稿件为空（{sess_mode}）", metrics={"session": sess_mode})
        return 1

    clean_cn = sanitize_html(text_cn)
    clean_hk = sanitize_html(text_hk)
    save_pending([clean_cn, clean_hk])

    print("📨 正在推送《A股报告》至 A股飞书机器人...", file=sys.stderr)
    sent_cn = send_feishu(clean_cn, FEISHU_WEBHOOK_CN)
    print(f"  ✓ A股推送成功（{sent_cn} 条）", file=sys.stderr)
    save_pending([clean_hk])

    print("📨 正在推送《港股报告》至 港股飞书机器人...", file=sys.stderr)
    sent_hk = send_feishu(clean_hk, FEISHU_WEBHOOK_HK)
    print(f"  ✓ 港股推送成功（{sent_hk} 条）", file=sys.stderr)
    CACHE_FILE.unlink(missing_ok=True)

    snap = meta.get("snapshot") or {}
    rtype = {"premarket": "开盘前瞻", "midday": "午间复盘", "close": "收盘复盘"}.get(sess_mode, "收盘复盘")
    doc_date = datetime.now().strftime("%Y-%m-%d") if sess_mode == "premarket" else (snap.get("date") or datetime.now().strftime("%Y-%m-%d"))

    # 自动同步归档到 Notion「每日情报归档与决策看板」数据库，并按场次落盘本地归档
    combined_cn_hk = f"{text_cn}\n\n---\n\n{text_hk}"
    notion_meta = archive_and_sync_notion(
        combined_cn_hk,
        "📈 A股与港股",
        date_str=doc_date,
        archive_dir=DRAFT_CN.parent / "archive",
        archive_filename=f"{doc_date}_{sess_mode}.txt",
        report_type=rtype,
    )
    notion_tag = " | Notion ✓" if notion_meta.get("notion_ok") else " | Notion ⚠️"

    # 若为收盘复盘（或包含收盘 snapshot），自动更新最近 5 个交易日跨日盘面状态记忆库
    try:
        if sess_mode == "close" and snap.get("cn") and snap.get("hk"):
            cn_sig, cn_focus = _extract_signal_and_focus(text_cn)
            hk_sig, hk_focus = _extract_signal_and_focus(text_hk)
            cn_entry = {**snap["cn"], "signal": cn_sig, "focus": cn_focus}
            hk_entry = {**snap["hk"], "signal": hk_sig, "focus": hk_focus}

            mem = _load_market_memory()
            cn_hist = [r for r in mem.get("cn", []) if r.get("date") != cn_entry["date"]]
            hk_hist = [r for r in mem.get("hk", []) if r.get("date") != hk_entry["date"]]
            cn_hist.append(cn_entry)
            hk_hist.append(hk_entry)
            MARKET_MEMORY.write_text(
                json.dumps(
                    {"cn": cn_hist[-MAX_MEMORY_DAYS:], "hk": hk_hist[-MAX_MEMORY_DAYS:]},
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            print(f"  ✓ 已更新跨日盘面状态记忆库（保留最近 {len(cn_hist[-MAX_MEMORY_DAYS:])} 个交易日）", file=sys.stderr)
    except Exception as exc:
        print(f"[WARN] 更新 market_memory.json 失败: {exc}", file=sys.stderr)

    duration = round(time.time() - t0, 1)
    write_log(
        "OK",
        f"A股({len(text_cn)}字) + 港股({len(text_hk)}字) 飞书双机器人推送成功 ({sess_mode}){notion_tag}",
        metrics={**(meta.get("metrics") or {}), "session": sess_mode, **notion_meta, "duration_s": duration, "cn_chars": len(text_cn), "hk_chars": len(text_hk)},
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="A股 & 港股智能决策日报")
    parser.add_argument("--mode", choices=["fetch", "send"], required=True)
    parser.add_argument("--session", choices=["premarket", "midday", "close"], default=None,
                        help="premarket=09:00开盘前瞻，midday=12:05午间缺早补午，close=16:15收盘复盘（send 未传时自动从 fetch_meta 读取）")
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
