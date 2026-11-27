#!/usr/bin/python3
"""
🇨🇳 A股智能决策日报（09:05–09:24 开盘前瞻紧接美股收盘日报 + 16:15 收盘复盘）
- 买方量化微观结构与技术锚点：
  · 255 日 K 线（完整 52 周 + MA20/50/200 + HV20 波动率标准化 Z-score + CLV + 成交额/量比 RVOL + Wilder RSI(14) + 布林 %B(20,2)）
  · 大小盘/成长红利风格差（中证1000-沪深300、创业板-中证红利）+ 10Y/30Y 国债 ETF + 四大国家队宽基 ETF 成交额
  · 全市场真实涨跌家数与自然涨停/跌停/最高连板（东方财富直连，异常自动平滑回退）
  · 实时跨资产先导锚（离岸人民币 USDCNH、富时中国 A50 期指、伦敦金/布油 24h、IF/IC/IM 股指期货基差）
  · 16 大高纯度机构主线板块（96 只核心龙头，按成交额与涨跌幅双维度刻画）+ 东财行业榜盘外补充（剔除迷你伪行业）
- 双场次严格隔离（premarket vs close 独立草稿与元数据、时间窗防呆、盘中实时数据隔离防污染、Exa 权威信源白/黑名单）
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

# 1. A股核心宽基与风格指数（含大小盘与红利杠铃锚）
CN_INDICES = [
    ("sh000001", "上证指数"),
    ("sz399001", "深证成指"),
    ("sz399006", "创业板指"),
    ("sh000688", "科创50"),
    ("sh000300", "沪深300"),
    ("sh000905", "中证500"),
    ("sh000852", "中证1000"),
    ("sz399303", "国证2000"),
    ("bj899050", "北证50"),
    ("sh000922", "中证红利"),
]

# 仅对 5 大主指数 + 中证1000 做 255 日量化技术诊断（控制并发与信息密度）
MA_TARGET_INDICES = ("sh000001", "sz399001", "sz399006", "sh000688", "sh000300", "sh000852")

# 国债利率代理与国家队/平准基金宽基 ETF 成交额代理
CN_BOND_ETFS = [
    ("sh511260", "10年国债ETF"),
    ("sh511090", "30年国债ETF"),
]
CN_BROAD_ETFS = [
    ("sh510300", "沪深300ETF"),
    ("sh510050", "上证50ETF"),
    ("sh512100", "中证1000ETF"),
    ("sh588000", "科创50ETF"),
]

# 2. A股 16 大高纯度机构主线板块池（96 只核心龙头样本，彻底拆分银行/券商、海外算力/国产半导体、整车/光伏）
CN_SECTOR_POOL: dict[str, list[tuple[str, str]]] = {
    "海外AI算力与光模块": [
        ("sz300308", "中际旭创"), ("sz300502", "新易盛"), ("sh601138", "工业富联"),
        ("sz300394", "天孚通信"), ("sz002463", "沪电股份"), ("sz300476", "胜宏科技"),
    ],
    "国产算力与半导体芯片": [
        ("sh688256", "寒武纪"), ("sh688041", "海光信息"), ("sh688981", "中芯国际"),
        ("sz002371", "北方华创"), ("sh688012", "中微公司"), ("sh603986", "兆易创新"),
    ],
    "AI应用与计算机软件": [
        ("sh688111", "金山办公"), ("sz002230", "科大讯飞"), ("sz300442", "润泽科技"),
        ("sz000063", "中兴通讯"), ("sh600845", "宝信软件"), ("sz000977", "浪潮信息"),
    ],
    "人形机器人与智驾硬件": [
        ("sh601689", "拓普集团"), ("sz002050", "三花智控"), ("sz300124", "汇川技术"),
        ("sh688017", "绿的谐波"), ("sz002472", "双环传动"), ("sh603728", "鸣志电器"),
    ],
    "消费电子与果链": [
        ("sz002475", "立讯精密"), ("sz002241", "歌尔股份"), ("sz300433", "蓝思科技"),
        ("sz002938", "鹏鼎控股"), ("sh688036", "传音控股"), ("sz002600", "领益智造"),
    ],
    "纯银行与四大行权重": [
        ("sh601398", "工商银行"), ("sh601288", "农业银行"), ("sh601939", "建设银行"),
        ("sh600036", "招商银行"), ("sh601166", "兴业银行"), ("sh601988", "中国银行"),
    ],
    "券商与金融IT": [
        ("sz300059", "东方财富"), ("sh600030", "中信证券"), ("sh601688", "华泰证券"),
        ("sz300033", "同花顺"), ("sz300803", "指南针"), ("sh601318", "中国平安"),
    ],
    "煤炭电力与高股息红利": [
        ("sh601088", "中国神华"), ("sh601225", "陕西煤业"), ("sh600900", "长江电力"),
        ("sh601898", "中煤能源"), ("sh600025", "华能水电"), ("sh600941", "中国移动"),
    ],
    "油气化工与航运顺周期": [
        ("sh600938", "中国海油"), ("sh601857", "中国石油"), ("sh600309", "万华化学"),
        ("sh601919", "中远海控"), ("sz002493", "荣盛石化"), ("sh600989", "宝丰能源"),
    ],
    "有色金属与贵金属": [
        ("sh601899", "紫金矿业"), ("sh603993", "洛阳钼业"), ("sh600547", "山东黄金"),
        ("sz000807", "云铝股份"), ("sh601600", "中国铝业"), ("sh600489", "中金黄金"),
    ],
    "国防军工与低空商业航天": [
        ("sh600760", "中航沈飞"), ("sh600893", "航发动力"), ("sz000768", "中航西飞"),
        ("sh600118", "中国卫星"), ("sz002085", "万丰奥威"), ("sh688122", "西部超导"),
    ],
    "光伏储能与锂电新能源": [
        ("sz300750", "宁德时代"), ("sz300274", "阳光电源"), ("sh601012", "隆基绿能"),
        ("sh600438", "通威股份"), ("sz002460", "赣锋锂业"), ("sz300014", "亿纬锂能"),
    ],
    "汽车整车与智能出行": [
        ("sz002594", "比亚迪"), ("sh601127", "赛力斯"), ("sz000625", "长安汽车"),
        ("sh601633", "长城汽车"), ("sh600066", "宇通客车"), ("sh600660", "福耀玻璃"),
    ],
    "出海家电与工程机械": [
        ("sz000333", "美的集团"), ("sh600690", "海尔智家"), ("sz000651", "格力电器"),
        ("sh600031", "三一重工"), ("sz000425", "徐工机械"), ("sz000338", "潍柴动力"),
    ],
    "食品饮料与白酒消费": [
        ("sh600519", "贵州茅台"), ("sz000858", "五粮液"), ("sz000568", "泸州老窖"),
        ("sh600809", "山西汾酒"), ("sh600887", "伊利股份"), ("sh603288", "海天味业"),
    ],
    "创新药与医疗器械": [
        ("sh600276", "恒瑞医药"), ("sh688235", "百济神州"), ("sz300760", "迈瑞医疗"),
        ("sh603259", "药明康德"), ("sh688271", "联影医疗"), ("sz300015", "爱尔眼科"),
    ],
}

# 2026–2027 中国 A 股主要法定节假日休市表（避免工作日假期误发盘前前瞻）
_CN_HOLIDAYS = {
    "2026-01-01", "2026-01-02",
    "2026-02-16", "2026-02-17", "2026-02-18", "2026-02-19", "2026-02-20", "2026-02-23",
    "2026-04-06", "2026-05-01", "2026-05-04", "2026-05-05", "2026-06-19",
    "2026-09-25", "2026-10-01", "2026-10-02", "2026-10-05", "2026-10-06", "2026-10-07",
    "2027-01-01",
    "2027-02-05", "2027-02-08", "2027-02-09", "2027-02-10", "2027-02-11", "2027-02-12",
    "2027-04-05", "2027-05-03", "2027-05-04", "2027-05-05", "2027-06-09",
    "2027-09-15", "2027-10-01", "2027-10-04", "2027-10-05", "2027-10-06", "2027-10-07",
}

# 3. 海外市场与 A 股产业链先行映射报价（替换港股电商，强化半导体/光模块硬件锚）
US_MAPPING = [
    ("us.INX", "标普500"),
    ("us.IXIC", "纳斯达克"),
    ("us.DJI", "道琼斯"),
    ("usKWEB", "中概互联ETF(KWEB)"),
    ("usFXI", "中国大盘ETF(FXI)"),
    ("usSOXX", "费城半导体ETF(SOXX)"),
    ("usNVDA", "英伟达(AI算力映射)"),
    ("usAVGO", "博通(AI网络/ASIC映射)"),
    ("usCOHR", "Coherent(光模块映射)"),
    ("usMU", "美光(存储映射)"),
    ("usAAPL", "苹果(果链/消费电子映射)"),
    ("usTSLA", "特斯拉(机器人/智驾映射)"),
]

_PSEUDO_INDUSTRY_BLACKLIST = {
    "次新股", "开发区", "纺织机械", "陶瓷行业", "玻璃行业", "综合行业",
    "自行车", "家具行业", "仪器仪表", "塑料制品", "物资外贸", "印刷包装",
}


def _safe_float(v: Any) -> float:
    try:
        return float(v)
    except Exception:
        return 0.0


def _session_meta_path(session_mode: str) -> Path:
    return BASE / "logs" / f"fetch_meta_{session_mode}.json"


def _session_draft_path(session_mode: str) -> Path:
    p = BASE / "logs" / f"report_cn_{session_mode}.txt"
    if p.exists():
        return p
    return DRAFT_CN


def _prev_cn_trading_day(ref_dt: datetime) -> datetime:
    """结合周末与 _CN_HOLIDAYS 向前推算上一个真实 A 股交易日。"""
    cur = ref_dt - timedelta(days=1)
    for _ in range(15):
        if cur.weekday() < 5 and cur.strftime("%Y-%m-%d") not in _CN_HOLIDAYS:
            return cur
        cur -= timedelta(days=1)
    return ref_dt - timedelta(days=1)


def _elapsed_cn_trading_minutes(now: datetime) -> int:
    """计算 A 股当日已交易分钟数（09:30-11:30 为 120m，13:00-15:00 为 120m，总 240m）。"""
    hm = now.hour * 60 + now.minute
    if hm <= 9 * 60 + 30:
        return 0
    if hm < 11 * 60 + 30:
        return hm - (9 * 60 + 30)
    if hm < 13 * 60:
        return 120
    if hm < 15 * 60:
        return 120 + (hm - 13 * 60)
    return 240


def fetch_gtimg_quotes(codes: list[str]) -> dict[str, dict[str, Any]]:
    """通过腾讯财经 qt.gtimg.cn 批量拉取 A 股与海外映射行情（含 HTTP 状态检查与 2 次退避重试）。"""
    if not codes:
        return {}
    unique_codes = list(dict.fromkeys(codes))
    out: dict[str, dict[str, Any]] = {}
    batch_size = 55
    for i in range(0, len(unique_codes), batch_size):
        batch = unique_codes[i : i + batch_size]
        url = "https://qt.gtimg.cn/q=" + ",".join(batch)
        resp = None
        for attempt in range(3):
            try:
                r = DIRECT_SESSION.get(url, headers={"User-Agent": _UA}, timeout=15)
                r.raise_for_status()
                r.encoding = "gbk"
                resp = r
                break
            except Exception as exc:
                if attempt == 2:
                    print(f"[WARN] qt.gtimg.cn 批量请求重试 3 次均失败: {exc}", file=sys.stderr)
                else:
                    time.sleep(0.8 * (attempt + 1))
        if resp is None:
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


def fetch_sina_macro_and_futures() -> dict[str, dict[str, Any]]:
    """直连新浪财经拉取 24h 实时跨资产与股指期货主力（离岸人民币 USDCNH、富时 A50、伦敦金、布油、IF/IC/IM）。"""
    codes = ["fx_susdcnh", "hf_CHA50CFD", "hf_XAU", "hf_OIL", "nf_IF0", "nf_IC0", "nf_IM0"]
    out: dict[str, dict[str, Any]] = {}
    try:
        r = DIRECT_SESSION.get(
            "https://hq.sinajs.cn/list=" + ",".join(codes),
            headers={"User-Agent": _UA, "Referer": "https://finance.sina.com.cn/"},
            timeout=10,
        )
        r.raise_for_status()
        r.encoding = "gbk"
    except Exception as exc:
        print(f"[WARN] 新浪跨资产/期货行情抓取异常: {exc}", file=sys.stderr)
        return out

    for line in r.text.splitlines():
        m = re.match(r'^var hq_str_([A-Za-z0-9_]+)="([^"]*)";?$', line.strip())
        if not m:
            continue
        code, body = m.group(1), m.group(2)
        if not body:
            continue
        p = body.split(",")
        if code == "fx_susdcnh" and len(p) >= 9:
            last = _safe_float(p[1])
            prev_c = _safe_float(p[3])
            if last > 0:
                pct = ((last - prev_c) / prev_c * 100.0) if prev_c > 0 else 0.0
                out[code] = {
                    "name": "离岸人民币(USDCNH)",
                    "last": last,
                    "pct": pct,
                    "time": p[0],
                }
        elif code.startswith("hf_") and len(p) >= 13:
            last = _safe_float(p[0])
            prev_c = _safe_float(p[7])
            if last > 0:
                pct = ((last - prev_c) / prev_c * 100.0) if prev_c > 0 else 0.0
                label_map = {
                    "hf_CHA50CFD": "富时中国A50期指",
                    "hf_XAU": "伦敦金(24h实时)",
                    "hf_OIL": "布伦特原油(24h实时)",
                }
                out[code] = {
                    "name": label_map.get(code, p[13] if len(p) > 13 else code),
                    "last": last,
                    "pct": pct,
                    "date": p[12],
                    "time": p[6],
                }
        elif code.startswith("nf_") and len(p) >= 15:
            last = _safe_float(p[3])
            prev_settle = _safe_float(p[13]) or _safe_float(p[14])
            if last > 0:
                pct = ((last - prev_settle) / prev_settle * 100.0) if prev_settle > 0 else 0.0
                out[code] = {
                    "name": code.replace("nf_", ""),
                    "last": last,
                    "pct": pct,
                }
    return out


def fetch_btc_realtime() -> dict[str, Any] | None:
    """直连 OKX 公共行情抓取 24h 实时 BTC 报价（避免美股休市日或周一照抄 26–60h 前过期旧稿）。"""
    try:
        r = DIRECT_SESSION.get(
            "https://www.okx.com/api/v5/market/ticker?instId=BTC-USDT",
            headers={"User-Agent": _UA},
            timeout=6,
        )
        r.raise_for_status()
        rows = (r.json() or {}).get("data") or []
        if rows:
            last = _safe_float(rows[0].get("last"))
            o24 = _safe_float(rows[0].get("open24h"))
            if last > 0:
                pct = ((last - o24) / o24 * 100.0) if o24 > 0 else 0.0
                return {"last": last, "pct": pct}
    except Exception:
        pass
    return None


def fetch_eastmoney_breadth_and_limits(trade_date_compact: str) -> dict[str, Any]:
    """通过东方财富免费接口拉取沪深北交易所零重叠真实涨跌平家数、自然涨停/跌停数、炸板数与最高连板高度（失败平滑回退空字典）。"""
    res: dict[str, Any] = {}
    hdrs = {"User-Agent": _UA, "Referer": "https://quote.eastmoney.com/"}
    # 1) 交易所级精确涨跌平家数（上证 1.000001 + 深证 0.399001 + 北证 0.899050，零重复计数）
    for attempt in range(2):
        try:
            u_br = (
                "https://push2.eastmoney.com/api/qt/ulist.np/get"
                "?fltt=2&secids=1.000001,0.399001,0.899050&fields=f12,f14,f104,f105,f106"
            )
            r = DIRECT_SESSION.get(u_br, headers=hdrs, timeout=8)
            r.raise_for_status()
            diff = ((r.json() or {}).get("data") or {}).get("diff") or []
            up_sum = sum(int(x.get("f104") or 0) for x in diff if isinstance(x, dict))
            dn_sum = sum(int(x.get("f105") or 0) for x in diff if isinstance(x, dict))
            fl_sum = sum(int(x.get("f106") or 0) for x in diff if isinstance(x, dict))
            if up_sum + dn_sum >= 2000:
                res["up"] = up_sum
                res["down"] = dn_sum
                res["flat"] = fl_sum
                break
        except Exception as exc:
            if attempt == 1:
                print(f"[WARN] 东财全市场宽度抓取失败: {exc}", file=sys.stderr)
            else:
                time.sleep(0.5)

    # 2) 自然涨停池（含最高连板 lbc）、跌停池、炸板池
    for key, ep in (
        ("zt", "getTopicZTPool"),
        ("dt", "getTopicDTPool"),
        ("zb", "getTopicZBPool"),
    ):
        try:
            u = (
                f"https://push2ex.eastmoney.com/{ep}"
                f"?ut=7eea3edcaed734bea9cbfc24409ed989&dpt=wz.ztzt&Pageindex=0&pagesize=30"
                f"&sort=lbc:desc&date={trade_date_compact}"
            )
            r = DIRECT_SESSION.get(u, headers=hdrs, timeout=8)
            r.raise_for_status()
            d = (r.json() or {}).get("data") or {}
            tc = d.get("tc")
            if tc is not None:
                res[key] = int(tc)
            if key == "zt":
                pool = d.get("pool") or []
                max_lbc = max((int(x.get("lbc") or 0) for x in pool if isinstance(x, dict)), default=0)
                if max_lbc > 0:
                    res["max_lbc"] = max_lbc
        except Exception:
            continue
    return res


def fetch_eastmoney_sectors_top3() -> dict[str, list[dict[str, Any]]] | None:
    """抓取东方财富行业板块真实领涨(po=1)与领跌(po=0)排行（强过滤成分股 >= 8 且成交额 >= 50 亿、剔除垃圾伪行业），作为盘外题材补充。"""
    hdrs = {"User-Agent": _UA, "Referer": "https://quote.eastmoney.com/"}

    def _query_side(po: int, want_negative: bool) -> list[dict[str, Any]]:
        u = (
            "https://push2.eastmoney.com/api/qt/clist/get"
            f"?pn=1&pz=40&po={po}&np=1&fltt=2&invt=2&fid=f3&fs=m:90+t:2+f:!50"
            "&fields=f12,f14,f3,f6,f104,f105"
        )
        for attempt in range(2):
            try:
                r = DIRECT_SESSION.get(u, headers=hdrs, timeout=8)
                r.raise_for_status()
                diff = ((r.json() or {}).get("data") or {}).get("diff") or []
                out_list = []
                for row in diff:
                    if not isinstance(row, dict):
                        continue
                    name = str(row.get("f14") or "").strip()
                    if not name or name in _PSEUDO_INDUSTRY_BLACKLIST:
                        continue
                    up_n = int(row.get("f104") or 0)
                    dn_n = int(row.get("f105") or 0)
                    cnt = up_n + dn_n
                    amt_yi = _safe_float(row.get("f6")) / 1e8
                    pct = _safe_float(row.get("f3"))
                    if cnt < 8 or amt_yi < 50.0:
                        continue
                    if want_negative and pct >= 0:
                        continue
                    out_list.append({
                        "code": row.get("f12", ""),
                        "name": name,
                        "pct": pct,
                        "amount_yi": amt_yi,
                        "up": up_n,
                        "down": dn_n,
                        "stocks": [],
                    })
                    if len(out_list) >= 3:
                        break
                return out_list
            except Exception as exc:
                if attempt == 1:
                    print(f"[WARN] 东财行业榜(po={po})抓取失败: {exc}", file=sys.stderr)
                else:
                    time.sleep(0.5)
        return []

    gainers = _query_side(po=1, want_negative=False)
    losers = _query_side(po=0, want_negative=True)
    if not gainers and not losers:
        return None
    return {"gainers": gainers, "losers": losers}


def compute_pool_sectors_top3(
    sector_pool: dict[str, list[tuple[str, str]]],
    quotes: dict[str, dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """基于实时行情计算 16 大高纯度机构主线均值与板块总成交额（剔除停牌股），领跌板块严格不混入收涨股。"""
    sectors: list[dict[str, Any]] = []
    for sec_name, members in sector_pool.items():
        valid_stocks = []
        for code, fallback_name in members:
            q = quotes.get(code)
            # 过滤停牌股（无成交额且无开盘价）
            if q and q["last"] > 0 and (q.get("amount_yi", 0.0) > 0 or q.get("open", 0.0) > 0):
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
        total_amt = sum(s["amount_yi"] for s in valid_stocks)
        sectors.append({
            "name": sec_name,
            "pct": avg_pct,
            "amount_yi": total_amt,
            "all_stocks": valid_stocks,
        })

    sectors_sorted = sorted(sectors, key=lambda x: x["pct"], reverse=True)
    gainers = []
    for sec in sectors_sorted[:3]:
        pos = [s for s in sec["all_stocks"] if s["pct"] > 0]
        pool_use = pos if (sec["pct"] > 0 and pos) else sec["all_stocks"]
        top3_up = sorted(pool_use, key=lambda s: s["pct"], reverse=True)[:3]
        gainers.append({
            "name": sec["name"],
            "pct": sec["pct"],
            "amount_yi": sec["amount_yi"],
            "stocks": top3_up,
        })

    losers = []
    for sec in reversed(sectors_sorted[-3:]):
        neg = [s for s in sec["all_stocks"] if s["pct"] < 0]
        top3_down = sorted(neg, key=lambda s: s["pct"])[:3]
        losers.append({
            "name": sec["name"],
            "pct": sec["pct"],
            "amount_yi": sec["amount_yi"],
            "stocks": top3_down,
        })

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
        return 50.0 if avg_gain == 0 else 100.0
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


def _calc_hv20(closes: list[float]) -> float | None:
    """20 日年化已实现波动率 HV20 (%)。"""
    if len(closes) < 21:
        return None
    rets = []
    for a, b in zip(closes[-21:-1], closes[-20:]):
        if a > 0 and b > 0:
            rets.append(math.log(b / a))
    if len(rets) < 15:
        return None
    mean_r = sum(rets) / len(rets)
    var = sum((r - mean_r) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(max(var, 0.0) * 252.0) * 100.0


def fetch_ma_trend_detail(
    code: str,
    live_quote: dict[str, Any] | None = None,
    today_str: str = "",
    live_date_compact: str = "",
    *,
    strict_prior_close: bool = False,
    intraday_minutes: int = 240,
) -> dict[str, Any]:
    """
    拉取 255 日 K 线（覆盖完整 52 周 ~245 交易日 + MA200 缓冲），计算：
    MA20 / MA50 / MA200、HV20、波动率标准化 Z_vol、CLV、量比 RVOL（盘中自动按交易分钟折算）、
    Wilder RSI(14)、布林 %B(20,2) 与 52W 回撤。
    - strict_prior_close=True（premarket 遇盘中调试时）：自动剥离 date >= today_str 的盘中未收盘 K 线，锁定真实前日收盘。
    """
    try:
        url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={code},day,,,255,qfq"
        resp = DIRECT_SESSION.get(url, headers={"User-Agent": _UA}, timeout=12)
        resp.raise_for_status()
        data = resp.json().get("data", {}).get(code, {})
        klines = data.get("qfqday") or data.get("day") or []
        if not klines:
            return {"text": "", "bar": None}

        if strict_prior_close and today_str:
            klines = [r for r in klines if len(r) >= 5 and str(r[0]).strip() < today_str]
            if len(klines) < 20:
                return {"text": "", "bar": None}

        if len(klines) < 20:
            return {"text": "", "bar": None}

        closes = [_safe_float(r[2]) for r in klines if len(r) >= 5 and _safe_float(r[2]) > 0]
        highs = [_safe_float(r[3]) for r in klines if len(r) >= 5 and _safe_float(r[3]) > 0]
        if len(closes) < 20:
            return {"text": "", "bar": None}

        last_bar = klines[-1]
        last_bar_date = str(last_bar[0]).strip()
        live_last = _safe_float((live_quote or {}).get("last"))
        live_high = _safe_float((live_quote or {}).get("high"))
        live_low = _safe_float((live_quote or {}).get("low"))
        today_compact = today_str.replace("-", "") if today_str else ""

        appended_today = False
        if (
            not strict_prior_close
            and live_last > 0
            and today_str
            and live_date_compact == today_compact
        ):
            if today_str > last_bar_date:
                closes.append(live_last)
                if live_high > 0:
                    highs.append(live_high)
                appended_today = True
            elif today_str == last_bar_date:
                closes[-1] = live_last
                if live_high > 0 and highs:
                    highs[-1] = max(highs[-1], live_high)

        cur = closes[-1]
        ma20 = sum(closes[-20:]) / 20.0
        ma50 = sum(closes[-50:]) / 50.0 if len(closes) >= 50 else None
        ma200 = sum(closes[-200:]) / 200.0 if len(closes) >= 200 else None
        bias20 = (cur - ma20) / ma20 * 100.0 if ma20 > 0 else 0.0

        hv20 = _calc_hv20(closes)
        # 真实 HV20 波动率标准化 20 日偏离 Z_vol（与布林 %B 互补，不再共线冗余）
        z_str = ""
        if hv20 and hv20 > 0 and ma20 > 0 and cur > 0:
            hv20_20d = (hv20 / 100.0) / math.sqrt(252.0) * math.sqrt(20.0)
            if hv20_20d > 0:
                z_vol = math.log(cur / ma20) / hv20_20d
                z_str = f" · Z_vol={z_vol:+.2f}σ"

        if ma50 is not None and ma200 is not None:
            if cur > ma50 > ma200:
                trend = "🟢中期多头"
            elif cur < ma50 < ma200:
                trend = "🔴中期空头(跌破年线)"
            elif cur < ma200:
                trend = "🔴弱势震荡(年线下方)"
            else:
                trend = "🟡震荡整理(年线上方)"
        else:
            trend = "🟢多头" if cur > ma20 else "🔴空头"

        parts = [trend, f"MA20={ma20:.2f} ({bias20:+.2f}%{z_str})"]
        if ma50 is not None:
            parts.append(f"MA50={ma50:.2f} ({(cur - ma50) / ma50 * 100.0:+.1f}%)")
        if ma200 is not None:
            d200 = (cur - ma200) / ma200 * 100.0
            pos200 = "年线上方" if cur >= ma200 else "跌破年线"
            parts.append(f"MA200={ma200:.2f} ({d200:+.1f}%/{pos200})")
        if hv20 is not None:
            parts.append(f"HV20={hv20:.1f}%")

        # CLV 严格保证与 cur 处于同一交易日
        if appended_today:
            bh, bl, bc = live_high, live_low, live_last
        elif not strict_prior_close and live_high > 0 and live_low > 0 and live_date_compact == today_compact and today_str == last_bar_date:
            bh, bl, bc = live_high, live_low, live_last
        else:
            bh = _safe_float(last_bar[3]) if len(last_bar) >= 5 else 0.0
            bl = _safe_float(last_bar[4]) if len(last_bar) >= 5 else 0.0
            bc = _safe_float(last_bar[2]) if len(last_bar) >= 5 else 0.0
        if bh > bl > 0 and bc > 0:
            clv = max(0.0, min(1.0, (bc - bl) / (bh - bl)))
            parts.append(f"CLV={clv:.2f}")

        # 量比 RVOL：仅当 K 线末日与当前统计日一致时计算；若处于盘中未收盘则按已交易分钟折算全天并显式标注
        if not appended_today and len(last_bar) >= 6 and len(klines) >= 21:
            lv = _safe_float(last_bar[5])
            pvs = [_safe_float(r[5]) for r in klines[-21:-1] if len(r) >= 6 and _safe_float(r[5]) > 0]
            if lv > 0 and len(pvs) >= 15:
                avg_v = sum(pvs) / len(pvs)
                if avg_v > 0:
                    if not strict_prior_close and 0 < intraday_minutes < 240 and last_bar_date == today_str:
                        scale = 240.0 / max(intraday_minutes, 30)
                        parts.append(f"RVOL={lv * scale / avg_v:.2f}x(折算全天)")
                    else:
                        parts.append(f"RVOL={lv / avg_v:.2f}x")

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
            hi_52 = max(highs[-245:])
            if hi_52 > 0:
                parts.append(f"距52W高 {(cur - hi_52) / hi_52 * 100.0:+.1f}%")

        # 返回最后一根已收盘日线柱摘要（供盘前遭遇盘中调试时回退前日收盘价）
        prev_c = _safe_float(klines[-2][2]) if len(klines) >= 2 else 0.0
        bar_close = _safe_float(last_bar[2])
        bar_pct = ((bar_close - prev_c) / prev_c * 100.0) if prev_c > 0 else 0.0
        bar_hi = _safe_float(last_bar[3])
        bar_lo = _safe_float(last_bar[4])
        bar_amp = ((bar_hi - bar_lo) / prev_c * 100.0) if prev_c > 0 and bar_hi > bar_lo else 0.0
        return {
            "text": " | ".join(parts),
            "bar": {
                "date": last_bar_date,
                "close": bar_close,
                "pct": bar_pct,
                "amplitude": bar_amp,
            },
        }
    except Exception as exc:
        print(f"[WARN] A股({code}) 255日K抓取失败: {exc}", file=sys.stderr)
        return {"text": "", "bar": None}


def fetch_ma_trend(
    code: str,
    live_last: float = 0.0,
    today_str: str = "",
    live_date_compact: str = "",
) -> str:
    """兼容旧签名，返回格式化量化技术文本。"""
    q_stub = {"last": live_last} if live_last > 0 else None
    return fetch_ma_trend_detail(code, q_stub, today_str, live_date_compact)["text"]


_SINA_NOISE_RE = re.compile(
    r"(?:互动平台|投资者关系|答复称|股票交易异常波动|股票交易严重异常波动|"
    r"解除质押|补充质押|股东户数|召开.*股东大会|大宗交易成交|龙虎榜营业部|"
    r"广州车展|速成车|带病体保险|公关报告|体验中心|新车上市|上市发布)"
)
_SINA_TIER0_POLICY_RE = re.compile(
    r"(?:政治局|国常会|国务院|央行|中国人民银行|证监会|财政部|发改委|工信部|商务部|国家数据局|"
    r"平准基金|中央汇金|降准|降息|LPR|MLF|逆回购|特别国债|社融|M2|CPI|PPI|PMI|GDP|关税|对华|中美|出口管制)"
)
_SINA_HIGH_SIGNAL_RE = re.compile(
    r"(?:政治局|国常会|国务院|央行|中国人民银行|证监会|财政部|发改委|工信部|商务部|统计局|国家数据局|"
    r"平准基金|中央汇金|降准|降息|LPR|MLF|逆回购|专项债|特别国债|社融|M2|CPI|PPI|PMI|GDP|进出口|工业利润|"
    r"收评|午评|北向|南向|成交额|融资余额|关税|美联储|美债|中概|英伟达|算力|半导体|芯片|光模块|人工智能|AI|机器人|新能源|黄金|原油|夜盘期货)"
)
_MARKET_RECAP_LINE_RE = re.compile(r"【(?:A股)?(?:收评|午评|早评|开盘|午间|涨停分析)")
_SINA_GLOBAL_RELEVANT_RE = re.compile(
    r"(?:中国|国内|中国人民银行|中国央行|央行|证监会|发改委|工信部|中国财政部|财政部拟|超长期特别国债|专项债|商务部|统计局|国常会|政治局|国务院|国家数据局|"
    r"平准基金|中央汇金|A股|沪深|上证|创业板|科创|北证|北向|南向|人民币|离岸人民币|夜盘期货|中概|港股|"
    r"美联储|欧央行|欧洲央行|日本央行|美国财政部|美债|纳斯达克|标普|英伟达|苹果|特斯拉|关税|对华|中美|黄金|布伦特|欧佩克|OPEC|芯片|半导体|算力|人工智能)"
)
_SINA_MINOR_COUNTRY_NOISE_RE = re.compile(
    r"(?:罗马尼亚|匈牙利|捷克|波兰|希腊|保加利亚|塞尔维亚|立陶宛|爱沙尼亚|拉脱维亚|冰岛|挪威克朗|列伊|福林|"
    r"哥斯达黎加|新西兰|泰国出口|巴西央行|土耳其|瑞典央行|瑞士央行|墨西哥|印尼|菲律宾|马来西亚|"
    r"安哥拉|尼日利亚|肯尼亚|南非兰特|苏里南|巴拉圭|乌拉圭|秘鲁|哥伦比亚|智利比索|厄瓜多尔|欧洲议会|边际贷款利率借款|"
    r"指政府蓄意低报|选举干预)"
)


def _parse_sina_ctime(raw: str) -> datetime | None:
    if not raw:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(raw.strip(), fmt)
        except ValueError:
            continue
    return None


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
    limit: int = 35,
    session_mode: str = "close",
    max_age_hours: int | None = None,
    now_dt: datetime | None = None,
) -> list[str]:
    """
    抓取新浪财经 7x24 小时滚动电报：
    - 全频道（含 tag_id=0 与 tag_id=4）统一封杀小国利率/海外口水/车展噪音；
    - 结合 _CN_HOLIDAYS 跨假回溯上一个真实交易日 15:00；
    - 盘前模式严控上界 dt <= 今日 09:24，杜绝把午评错标为「盘后-盘前」；
    - Tier-0 政策重磅（政治局/国常会/平准基金/央行等）强制置顶且保留至 500 字符防截断。
    """
    ref_now = now_dt or datetime.now()
    prev_td = _prev_cn_trading_day(ref_now)
    gap_days = max(1, (ref_now.date() - prev_td.date()).days)
    if max_age_hours is None:
        max_age_hours = max(36, gap_days * 24 + 12)
    cutoff = ref_now - timedelta(hours=max_age_hours)
    core_names = _tracked_core_names()

    post_close_start = prev_td.replace(hour=15, minute=0, second=0, microsecond=0)
    premarket_upper = ref_now.replace(hour=9, minute=25, second=0, microsecond=0)

    collected: list[tuple[int, datetime, str, str]] = []  # (tier, dt, ctime, line)
    seen: set[str] = set()
    feed_specs = (
        [(10, "A股市场", 35, 2), (4, "公司/行业", 35, 2), (0, "宏观/外围", 40, 1)]
        if session_mode == "premarket"
        else [(10, "A股市场", 30, 1), (4, "公司/行业", 30, 1), (0, "宏观/政策", 40, 1)]
    )
    for tag_id, tag_label, pg_size, max_pages in feed_specs:
        for page in range(1, max_pages + 1):
            url = (
                "https://zhibo.sina.com.cn/api/zhibo/feed"
                f"?page={page}&page_size={pg_size}&zhibo_id=152&tag_id={tag_id}&dire=f&dpc=1"
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
                    # 盘前模式严控上界 <= 今日 09:24，排除午评/盘中快讯混入盘前电报
                    if session_mode == "premarket" and dt is not None and dt >= premarket_upper:
                        continue
                    if len(txt) < 20:
                        continue
                    hits_core = any(nm in txt for nm in core_names)
                    if _SINA_NOISE_RE.search(txt) and not hits_core:
                        continue
                    # 全频道封杀非系统性小国利率/外围口水；对非 tag_id=10（A股市场）严格要求命中中国或核心全球宏观白名单
                    if _SINA_MINOR_COUNTRY_NOISE_RE.search(txt) and not hits_core:
                        continue
                    if tag_id != 10 and not (_SINA_GLOBAL_RELEVANT_RE.search(txt) or hits_core):
                        continue
                    key = re.sub(r"[【】\[\]\s，。,:：]", "", txt)[:32]
                    if key in seen:
                        continue
                    seen.add(key)

                    sort_dt = dt or datetime.min
                    in_overnight = bool(
                        session_mode == "premarket"
                        and dt is not None
                        and post_close_start <= dt < premarket_upper
                    )
                    win_tag = " · 盘后-盘前" if in_overnight else ""
                    is_tier0 = bool(_SINA_TIER0_POLICY_RE.search(txt))
                    is_high = is_tier0 or bool(_SINA_HIGH_SIGNAL_RE.search(txt) or hits_core)
                    tier = 2 if is_tier0 else (1 if is_high else 0)
                    max_len = 500 if is_tier0 else 280
                    tier_tag = " · ★政策/宏观" if is_tier0 else ""
                    collected.append((tier, sort_dt, ctime, f"[{ctime} · {tag_label}{win_tag}{tier_tag}] {txt[:max_len]}"))
            except Exception as exc:
                print(f"[WARN] 新浪 7x24 电报(tag={tag_id}, p={page})抓取异常: {exc}", file=sys.stderr)

    # 先保底 Tier-0 政策重磅（最多 10 条），再按高优与时间倒序补齐
    tier0 = sorted([c for c in collected if c[0] == 2], key=lambda x: (x[1], x[2]), reverse=True)[:12]
    t0_set = {c[3] for c in tier0}
    rest_high = sorted([c for c in collected if c[0] == 1 and c[3] not in t0_set], key=lambda x: (x[1], x[2]), reverse=True)
    rest_low = sorted([c for c in collected if c[0] == 0 and c[3] not in t0_set], key=lambda x: (x[1], x[2]), reverse=True)
    picked = (tier0 + rest_high + rest_low)[:limit]
    # 输出按 Tier-0 置顶 + 各自时间倒序，确保国常会/央行等核心政策永远位于最上方
    picked.sort(key=lambda x: (1 if x[0] == 2 else 0, x[1], x[2]), reverse=True)
    return [line for _, _, _, line in picked]


def _build_cn_macro_calendar_hints(now: datetime) -> list[str]:
    """根据国家统计局/央行法定月度发布窗口，提示未来 5 个自然日内即将公布的中国核心宏观数据。"""
    schedule = [
        (1, "财新中国制造业 PMI"),
        (3, "财新中国服务业 PMI"),
        (7, "中国外汇储备 / 进出口贸易数据窗口"),
        (9, "中国 CPI / PPI 通胀数据窗口"),
        (10, "中国社融 / M2 / 新增人民币贷款窗口（10–15日）"),
        (15, "央行 MLF 续作操作 & 国家统计局宏观三驾马车（工业/消费/固投）"),
        (20, "央行 1 年期 / 5 年期 LPR 报价（09:00）"),
        (27, "国家统计局规模以上工业企业利润"),
        (30, "国家统计局官方制造业 / 非制造业 PMI（月末 09:30）"),
        (31, "国家统计局官方制造业 / 非制造业 PMI（月末 09:30）"),
    ]
    hints: list[str] = []
    seen_ev: set[str] = set()
    for offset in range(0, 6):
        d = now + timedelta(days=offset)
        day_num = d.day
        mmdd = d.strftime("%m-%d")
        for target_day, desc in schedule:
            if day_num == target_day and desc not in seen_ev:
                seen_ev.add(desc)
                tag = "今日" if offset == 0 else mmdd
                hints.append(f"{tag} {desc}")
    return hints


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
    all_quotes: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """筛出新浪电报未实质报道、但涨跌幅 >= 4.0% 的 A 股领涨/领跌龙头（覆盖 Top3 板块 + 全池高成交异动），用于 Exa 定向补盲。"""
    substantive_lines = [t for t in telegraph if not _MARKET_RECAP_LINE_RE.search(t)]
    blob = "\n".join(substantive_lines)
    by_sym: dict[str, tuple[float, dict[str, Any]]] = {}

    def _consider(st: dict[str, Any], sec_name: str) -> None:
        pct = st.get("pct", 0.0)
        if abs(pct) < 4.0:
            return
        name = st["name"]
        core = _clean_cn_name(name)
        if core and len(core) >= 2 and core in blob:
            return
        sym = _format_std_symbol(st["code"])
        score = abs(pct) + (1.5 if st.get("amount_yi", 0.0) >= 30.0 else 0.0)
        if sym not in by_sym or score > by_sym[sym][0]:
            by_sym[sym] = (
                score,
                {
                    "symbol": sym,
                    "name": name,
                    "pct_change": f"{pct:+.2f}%",
                    "direction": "up" if pct > 0 else "down",
                    "sector": sec_name,
                },
            )

    for sec in cn_pool_sectors.get("gainers", []) + cn_pool_sectors.get("losers", []):
        for st in sec.get("stocks", []):
            _consider(st, sec["name"])

    if all_quotes:
        for sec_name, members in CN_SECTOR_POOL.items():
            for code, nm in members:
                q = all_quotes.get(code)
                if q and q.get("last", 0) > 0 and q.get("amount_yi", 0) >= 20.0 and abs(q.get("pct", 0)) >= 5.0:
                    _consider({
                        "code": code,
                        "name": nm or q["name"],
                        "pct": q["pct"],
                        "amount_yi": q["amount_yi"],
                    }, sec_name)

    ranked = sorted(by_sym.values(), key=lambda x: x[0], reverse=True)
    return [item for _, item in ranked[:max_total]]


def enrich_cn_macro_events(telegraph: list[str], today_str: str) -> list[str]:
    """当月 20 日 LPR 报价或月末官方 PMI 日若电报未明确覆盖结果，调用 1 次轻量 Exa /search 补盲。"""
    try:
        dt = datetime.strptime(today_str, "%Y-%m-%d")
    except ValueError:
        return []
    blob = "\n".join(telegraph)
    queries: list[tuple[str, str]] = []
    if dt.day in (20, 21, 22) and "LPR" not in blob:
        queries.append(("LPR报价", f"中国 央行 LPR 报价 1年期 5年期 贷款市场报价利率 {today_str[:7]}"))
    if dt.day in (30, 31, 1) and "PMI" not in blob:
        queries.append(("官方PMI", f"国家统计局 中国制造业采购经理指数 PMI {today_str[:7]}"))
    if not queries:
        return []

    out: list[str] = []
    for label, q in queries[:1]:
        hits = exa_search_fallback(
            q,
            num_results=2,
            max_chars=500,
            category="news",
            start_published_date=(dt - timedelta(days=2)).strftime("%Y-%m-%d"),
        )
        for h in hits:
            if h.get("text"):
                out.append(f"· [Exa日程核实·{label}] {h['title']}：{h['text'][:240]} ({h['url']})")
    return out


def _load_market_memory() -> list[dict[str, Any]]:
    if not MARKET_MEMORY.exists():
        return []
    try:
        data = json.loads(MARKET_MEMORY.read_text(encoding="utf-8"))
        rows = data.get("cn", [])
        return rows if isinstance(rows, list) else []
    except Exception:
        return []


def _load_latest_a_share_close_archive(today_str: str = "") -> tuple[str, str]:
    """读取最近一份早于 today_str 的 A 股收盘复盘归档（作为次日开盘前瞻的「前日收盘复盘」底座）。"""
    if not ARCHIVE_DIR.exists():
        return "", ""
    candidates = sorted(ARCHIVE_DIR.glob("*_close.txt"), reverse=True)
    for p in candidates:
        dt_part = p.stem.replace("_close", "")
        if today_str and dt_part >= today_str:
            continue
        try:
            txt = p.read_text(encoding="utf-8").strip()
            if txt:
                return dt_part, txt[:3200]
        except Exception:
            continue
    return "", ""


def _load_latest_us_report(
    now: datetime | None = None,
    us_inx_date: str = "",
) -> tuple[str, str, str, str]:
    """
    自动读取今早刚跑完的《美股收盘 & 加密行情日报》，并交叉校验 us.INX 实时交易日：
    - 若 us_inx_date < expected_us_date：确认为真实隔夜美股休市（如感恩节），折叠旧稿并注入防二次计价告警；
    - 若 us_inx_date >= expected_us_date 但美股成稿早于 expected_us_date：说明美股 Bot 未更新（如 SKIP_PROXY），丢弃过期旧稿并提示以实时报价为准；
    - 归档回退严格限制 <= 84 小时，且确保 report_news 与 report_market 日期一致。
    """
    ref_now = now or datetime.now()
    m_path = US_BOT_DIR / "logs" / "report_market.txt"
    n_path = US_BOT_DIR / "logs" / "report_news.txt"
    us_market = ""
    us_news = ""
    us_source_label = ""

    if m_path.exists():
        try:
            age_h = (ref_now.timestamp() - m_path.stat().st_mtime) / 3600.0
            if age_h <= 84:
                us_market = m_path.read_text(encoding="utf-8").strip()
                us_source_label = f"承接 us stock bot 最新成稿（更新于 {age_h:.1f} 小时前）"
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
                    age_h = (ref_now.timestamp() - p.stat().st_mtime) / 3600.0
                    if age_h > 84:
                        continue
                    raw = p.read_text(encoding="utf-8").strip()
                    if raw:
                        parts = re.split(r"\n\s*---+\s*\n", raw, maxsplit=1)
                        us_market = parts[0].strip()
                        us_news = parts[1].strip() if len(parts) > 1 else ""
                        us_source_label = f"承接 us stock bot 归档稿（{p.stem}）"
                        break
                except Exception:
                    continue

    # 校验 report_news 与 report_market 日期一致性
    m_dt = re.search(r"美股收盘\s*&\s*加密行情\s*·\s*(\d{4}-\d{2}-\d{2})", us_market)
    n_dt = re.search(r"美股要闻\s*·\s*(\d{4}-\d{2}-\d{2})", us_news)
    us_sess_date = m_dt.group(1) if m_dt else ""
    if us_sess_date and n_dt and n_dt.group(1) != us_sess_date:
        us_news = ""

    expected_days_back = 3 if ref_now.weekday() == 0 else 1
    expected_us_date = (ref_now - timedelta(days=expected_days_back)).strftime("%Y-%m-%d")
    us_holiday_warn = ""

    norm_inx_dt = ""
    if us_inx_date:
        m_i = re.search(r"(\d{4})-?(\d{2})-?(\d{2})", us_inx_date)
        if m_i:
            norm_inx_dt = f"{m_i.group(1)}-{m_i.group(2)}-{m_i.group(3)}"

    if norm_inx_dt and norm_inx_dt < expected_us_date:
        # 真实隔夜美股休市（如感恩节次日）：强告警并折叠过期美股正文防二次计价
        us_holiday_warn = (
            f"⚠️ 隔夜美股休市提示：昨夜（{expected_us_date}）美股无新增收盘，实时美股报价仍停留在 "
            f"{norm_inx_dt} 旧收盘（已于前一日 A 股盘面消化）！请直接以 24h 实时「富时 A50 期指 + 离岸人民币 USDCNH + 伦敦金/布油 + BTC」及国内盘后政策为今日定价主锚，严禁把旧美股涨跌当成今日新增利空/利多重复预警！"
        )
        us_market = ""
        us_news = ""
    elif us_sess_date and us_sess_date < expected_us_date:
        if norm_inx_dt and norm_inx_dt >= expected_us_date:
            # 隔夜美股实际正常交易，但美股 Bot 未能出稿（如代理跳过）：丢弃旧稿防误导
            us_holiday_warn = (
                f"⚠️ 提示：隔夜美股（{norm_inx_dt}）正常收盘，但今早美股日报成稿停留在旧日期 {us_sess_date} 未更新；"
                "已自动屏蔽过期旧稿，请直接依据下方最新海外实时报价与国内电报撰写映射。"
            )
            us_market = ""
            us_news = ""
        else:
            us_holiday_warn = (
                f"⚠️ 隔夜美股休市提示：昨夜（{expected_us_date}）美股无新增收盘，本地最新美股成稿为 "
                f"{us_sess_date} 旧收盘（已于前一日 A 股盘面消化），已自动折叠旧正文，严禁二次计价！"
            )
            us_market = ""
            us_news = ""

    news_summary = ""
    if us_news:
        clean_n = re.sub(r"<[^>]+>", "", html.unescape(us_news))
        news_lines = [l.strip() for l in clean_n.splitlines() if l.strip()]
        picked = []
        for ln in news_lines:
            if ln.startswith("📋") or ln.startswith("- "):
                picked.append(ln)
            elif len(picked) >= 6:
                break
        news_summary = "\n".join(picked[:7])

    return us_source_label, us_market[:5200], news_summary, us_holiday_warn


def _format_memory_block(cn_list: list[dict[str, Any]]) -> list[str]:
    if not cn_list:
        return []
    lines = [
        "",
        "==========================================",
        "【零、🧠 A股跨日盘面状态记忆（最近 5 个交易日轮动、量能、宽度与风格差追踪）】",
        "==========================================",
        "（写稿提示：请对比前几日量能、涨跌家数与主线轮动，判断某板块是连续强势接力还是一日游切换，并复盘前日观点兑现。）",
    ]
    for row in cn_list[-MAX_MEMORY_DAYS:]:
        g_str = "、".join(row.get("top3_gainers", []))
        l_str = "、".join(row.get("top3_losers", []))
        extra_bits = []
        if row.get("breadth_str"):
            extra_bits.append(f"宽度：{row['breadth_str']}")
        if row.get("style_str"):
            extra_bits.append(f"风格差：{row['style_str']}")
        extra_s = (" | " + " | ".join(extra_bits)) if extra_bits else ""
        lines.append(
            f"- [{row.get('date', '?')}] 指数：{row.get('indices', '?')} | 量能：{row.get('turnover', '?')}{extra_s}\n"
            f"  · 领涨Top3：{g_str} | 领跌Top3：{l_str}\n"
            f"  · 当日信号灯：{row.get('signal', '未知')} | 当日重点关注：{row.get('focus', '未知')}"
        )
    return lines


def _extract_signal_and_focus(report_text: str) -> tuple[str, str]:
    clean = re.sub(r"<[^>]+>", "", html.unescape(report_text))
    sig, focus = "未知", "未知"
    in_dashboard = False
    for raw_line in clean.splitlines():
        line = raw_line.strip().lstrip("·•- ")
        if line.startswith("🎯"):
            in_dashboard = True
            continue
        if in_dashboard and line.startswith(("🔥", "❄️", "⚡", "🔭")):
            in_dashboard = False
        if not in_dashboard and sig != "未知" and focus != "未知":
            continue
        line_no_em = re.sub(r"^(?:[\U0001F300-\U0001FAFF\u2600-\u27BF\uFE0F\s])+", "", line).strip()
        if sig == "未知" and line_no_em.startswith(("信号灯与", "信号灯状态", "大盘信号灯", "早盘信号灯")):
            sig = re.sub(r"^[^：:]+[：:]\s*", "", line_no_em)[:90]
        elif (in_dashboard or focus == "未知") and (
            line_no_em.startswith(("相对强弱关注", "早盘重点关注", "重点关注", "关注方向"))
            or ("重点关注" in line_no_em[:16])
        ):
            focus = re.sub(r"^[^：:]+[：:]\s*", "", line_no_em)[:110]
    return sig, focus


def _format_sector_block(title: str, sec_list: list[dict[str, Any]], is_gainer: bool) -> list[str]:
    lines = [f"\n{title}"]
    role_label = "板块内涨幅 Top 3" if is_gainer else "板块内跌幅 Top 3"
    for i, sec in enumerate(sec_list, 1):
        amt_tag = f" | 样本成交 {sec['amount_yi']:.1f} 亿" if sec.get("amount_yi") else ""
        lines.append(f"\n{i}. 【{sec['name']}】 板块均幅：{sec['pct']:+.2f}%{amt_tag}")
        stocks = sec.get("stocks") or []
        if stocks:
            lines.append(f"   {role_label}：")
            for st in stocks:
                extra = f" | 换手 {st['turnover']:.2f}%" if st.get("turnover") else ""
                lines.append(
                    f"   · {st['name']} ({st['code']})：{st['last']:.2f} ({st['pct']:+.2f}%) | 成交 {st['amount_yi']:.1f} 亿{extra}"
                )
        elif sec.get("up") is not None:
            lines.append(f"   · 板块内上涨 {sec['up']} 只 / 下跌 {sec['down']} 只（东财全行业参考）")
        else:
            lines.append("   · （注：核心成分股中无符合该方向的标的）")
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
    """严格时段防呆：premarket 锁死 08:30–09:24（绝不越过 09:25 集合竞价出价点），close 锁死 >= 15:00。"""
    if session_mode == "premarket":
        if now.hour < 8 or (now.hour == 8 and now.minute < 30):
            return False, "未到 08:30 开盘前瞻窗口（premarket），自动跳过"
        if now.hour > 9 or (now.hour == 9 and now.minute > 24):
            return False, "已过 09:24 开盘前瞻窗口（已进入 09:25 集合竞价/开盘时段，premarket 自动跳过）"
    elif session_mode == "close":
        if now.hour < 15:
            return False, "未到 15:00 收盘时间（close），禁止盘中生成收盘复盘，自动跳过"
    return True, ""


def run_fetch(session_mode: str) -> int:
    now = datetime.now()
    if os.getenv("FORCE_RUN") != "1":
        in_win, skip_msg = _is_in_window(now, session_mode)
        if not in_win:
            print("=== SKIP_EXPIRED ===")
            write_log("WARN", skip_msg, metrics={"session": session_mode})
            return 0

    if _session_ran_today(LOG_FILE, session_mode):
        print("=== SKIP_ALREADY_RAN ===")
        return 0

    today_str = now.strftime("%Y-%m-%d")
    if (now.weekday() >= 5 or today_str in _CN_HOLIDAYS) and os.getenv("FORCE_RUN") != "1":
        print("=== SKIP_NON_TRADING_DAY ===")
        reason = "法定节假日" if today_str in _CN_HOLIDAYS else "周末非交易日"
        write_log("WARN", f"{reason}（{session_mode}），未出稿", metrics={"session": session_mode})
        return 0

    session_label = {
        "premarket": "开盘前瞻（09:05–09:24 盘前）",
        "close": "收盘复盘（16:15 盘后）",
    }.get(session_mode, "收盘复盘（16:15 盘后）")

    cn_pool_codes = [c for members in CN_SECTOR_POOL.values() for c, _ in members]
    all_codes = (
        [c for c, _ in CN_INDICES]
        + [c for c, _ in CN_BOND_ETFS]
        + [c for c, _ in CN_BROAD_ETFS]
        + cn_pool_codes
        + [c for c, _ in US_MAPPING]
    )

    print(f"📡 正在抓取 A 股量化微观结构、255 日技术锚点与跨资产映射（模式：{session_label}）...", file=sys.stderr)
    quotes = fetch_gtimg_quotes(all_codes)
    # 强制校验核心宽基指数完整度（防局部批次超时导致假休市/0 成交额污染）
    core_ok = all(quotes.get(c, {}).get("last", 0) > 0 for c in ("sh000001", "sz399001", "sz399006"))
    if not quotes or not core_ok:
        print("=== SKIP_NO_DATA ===")
        write_log("FAIL", f"A股核心指数行情缺失或接口返回为空（{session_mode}）", metrics={"session": session_mode})
        return 1

    today_compact = now.strftime("%Y%m%d")
    sh_time_raw = re.sub(r"\D", "", quotes.get("sh000001", {}).get("time", ""))
    sh_date = sh_time_raw[:8]
    if session_mode == "close" and os.getenv("FORCE_RUN") != "1":
        if sh_date and sh_date != today_compact:
            print("=== SKIP_NON_TRADING_DAY ===")
            write_log("WARN", f"A股今日休市（{session_mode}；最近交易日 {sh_date}），未出稿", metrics={"session": session_mode})
            return 0

    # 判断盘中调试场景（用于 premarket 前日收盘隔离 & close 盘中警告 + RVOL 折算）
    is_premarket_intraday = bool(session_mode == "premarket" and sh_date == today_compact)
    elapsed_mins = _elapsed_cn_trading_minutes(now) if sh_date == today_compact else 240
    is_close_intraday = bool(session_mode == "close" and sh_date == today_compact and now.hour < 15)

    # 并行抓取：新浪跨资产/期指 + OKX BTC 24h + 东财宽度/涨跌停 + 东财行业榜 + 6 大核心指数 255 日量化技术
    trade_date_for_em = (
        _prev_cn_trading_day(now).strftime("%Y%m%d")
        if is_premarket_intraday
        else (sh_date or today_compact)
    )
    ma_detail_map: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        fut_macro = pool.submit(fetch_sina_macro_and_futures)
        fut_btc = pool.submit(fetch_btc_realtime)
        fut_em_br = pool.submit(fetch_eastmoney_breadth_and_limits, trade_date_for_em)
        fut_em_sec = pool.submit(fetch_eastmoney_sectors_top3)
        ma_futs = {
            c: pool.submit(
                fetch_ma_trend_detail,
                c,
                quotes.get(c),
                today_str,
                sh_date,
                strict_prior_close=is_premarket_intraday,
                intraday_minutes=(240 if is_premarket_intraday else elapsed_mins),
            )
            for c in MA_TARGET_INDICES
        }
        sina_macro = fut_macro.result()
        btc_rt = fut_btc.result()
        em_breadth = fut_em_br.result()
        em_sectors = fut_em_sec.result()
        for c, fut in ma_futs.items():
            ma_detail_map[c] = fut.result()

    cn_pool_sectors = compute_pool_sectors_top3(CN_SECTOR_POOL, quotes)
    telegraph = fetch_sina_telegraph(limit=35, session_mode=session_mode, now_dt=now)

    exa_catalyst_block = ""
    exa_macro_lines: list[str] = []
    if exa_enrich_enabled(default_enabled=True):
        if session_mode == "close":
            uncovered = _select_uncovered_a_share_movers(
                cn_pool_sectors, telegraph, max_total=5, all_quotes=quotes
            )
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
                    title="【六、🔍 Exa Agent A股异动个股定向催化核查（仅保留权威公告与一线合规媒体交叉验证）】",
                )
        exa_macro_lines = enrich_cn_macro_events(telegraph, today_str)

    sh_amt = quotes.get("sh000001", {}).get("amount_yi", 0.0)
    sz_amt = quotes.get("sz399001", {}).get("amount_yi", 0.0)
    bj_amt = quotes.get("bj899050", {}).get("amount_yi", 0.0)
    cn_total_amt = sh_amt + sz_amt
    cn_all_amt = cn_total_amt + bj_amt

    # 计算两市成交额较前日增减（读取最近一条早于当前统计日的 memory）
    mem_cn = _load_market_memory()
    effective_trade_date = (
        (ma_detail_map.get("sh000001", {}).get("bar") or {}).get("date")
        if is_premarket_intraday
        else (f"{sh_date[:4]}-{sh_date[4:6]}-{sh_date[6:8]}" if len(sh_date) == 8 else today_str)
    ) or today_str

    prev_mem_amt = 0.0
    for row in reversed(mem_cn):
        if row.get("date") and row["date"] < effective_trade_date:
            if row.get("turnover_yi"):
                prev_mem_amt = _safe_float(row["turnover_yi"])
                break
            m_amt = re.search(r"([\d,.]+)", str(row.get("turnover") or ""))
            if m_amt:
                prev_mem_amt = _safe_float(m_amt.group(1).replace(",", ""))
                break

    amt_delta_str = ""
    if prev_mem_amt > 0 and not is_premarket_intraday and not is_close_intraday:
        diff_yi = cn_total_amt - prev_mem_amt
        diff_pct = diff_yi / prev_mem_amt * 100.0
        amt_delta_str = f"（较前日 {diff_yi:+,.1f} 亿 / {diff_pct:+.1f}%）"

    # 16 大主线与 96 样本统计（剔除停牌）
    active_pool_quotes = [
        quotes[c] for c in cn_pool_codes
        if quotes.get(c) and quotes[c].get("last", 0) > 0 and (quotes[c].get("amount_yi", 0) > 0 or quotes[c].get("open", 0) > 0)
    ]
    cn_stk_up = sum(1 for q in active_pool_quotes if q.get("pct", 0) > 0)
    cn_stk_down = sum(1 for q in active_pool_quotes if q.get("pct", 0) < 0)
    cn_sec_all = []
    for members in CN_SECTOR_POOL.values():
        vals = [
            quotes[c]["pct"] for c, _ in members
            if quotes.get(c) and quotes[c].get("last", 0) > 0 and (quotes[c].get("amount_yi", 0) > 0 or quotes[c].get("open", 0) > 0)
        ]
        if vals:
            cn_sec_all.append(sum(vals) / len(vals))
    cn_sec_up = sum(1 for v in cn_sec_all if v > 0)
    cn_sec_down = sum(1 for v in cn_sec_all if v < 0)

    # 风格差与股指期货基差
    pct_300 = quotes.get("sh000300", {}).get("pct", 0.0)
    pct_1000 = quotes.get("sh000852", {}).get("pct", 0.0)
    pct_cy = quotes.get("sz399006", {}).get("pct", 0.0)
    pct_div = quotes.get("sh000922", {}).get("pct", 0.0)
    style_1000_300 = pct_1000 - pct_300
    style_cy_div = pct_cy - pct_div
    style_str = f"小大盘差(中证1000-沪深300) {style_1000_300:+.2f}% | 成长红利差(创业板-中证红利) {style_cy_div:+.2f}%"

    basis_parts = []
    for fut_code, spot_code, lbl in (
        ("nf_IF0", "sh000300", "IF主力"),
        ("nf_IC0", "sh000905", "IC主力"),
        ("nf_IM0", "sh000852", "IM主力"),
    ):
        fq = sina_macro.get(fut_code)
        sq = quotes.get(spot_code)
        if fq and sq and sq.get("last", 0) > 0:
            diff_pt = fq["last"] - sq["last"]
            diff_pct = diff_pt / sq["last"] * 100.0
            tag = "升水" if diff_pt >= 0 else "贴水"
            basis_parts.append(f"{lbl} {fq['last']:.1f} ({tag} {diff_pt:+.1f}点/{diff_pct:+.2f}%)")

    now_hhmm = now.strftime("%H:%M BJT")
    out: list[str] = [
        f"=== FETCH_{session_mode.upper()}_OK ===",
        "=== CONTEXT_BEGIN ===",
        f"今天日期：{today_str}",
        f"场次模式：{session_label}（{session_mode}）",
        f"A股最近已收盘交易日：{effective_trade_date}",
    ]
    if is_premarket_intraday:
        out.append(
            f"⚠️ [盘中调试保护] 当前抓取时间为 {now_hhmm}（A股已开盘），255 日量化技术锚点已自动锁定前日收盘（{effective_trade_date}）；"
            "下方面板实时行情快照仅供参考，请严格以「前日收盘归档」和锁定后的量化锚点作为前日基准！"
        )
    if is_close_intraday:
        out.append(
            f"⚠️ [盘中实时快照 {now_hhmm}，非 15:00 收盘价] 本次因 FORCE_RUN=1 于盘中触发，RVOL 已按已交易 {elapsed_mins}/240 分钟折算全天，严禁将该快照写入跨日收盘记忆库！"
        )

    out.extend(_format_memory_block(mem_cn))

    # 实时跨资产与先行定价锚（盘前/收盘均直抓，彻底解决美股休市或周一 26–60h 旧数据问题）
    live_macro_bits = []
    for mk in ("hf_CHA50CFD", "fx_susdcnh", "hf_XAU", "hf_OIL"):
        mv = sina_macro.get(mk)
        if mv:
            fmt = f"{mv['last']:.4f}" if mk == "fx_susdcnh" else f"{mv['last']:,.2f}"
            live_macro_bits.append(f"{mv['name']} {fmt} ({mv['pct']:+.2f}%)")
    if btc_rt:
        live_macro_bits.append(f"比特币(24h实时) ${btc_rt['last']:,.0f} ({btc_rt['pct']:+.2f}%)")

    us_inx_dt = quotes.get("us.INX", {}).get("time", "")
    if session_mode == "premarket":
        us_src, us_mkt_txt, us_news_sum, us_holiday_warn = _load_latest_us_report(now, us_inx_date=us_inx_dt)
        out.extend([
            "",
            "==========================================",
            f"【一、🌍 海外市场与 24h 实时定价锚（{us_src or '直接采用实时跨资产与海外映射报价'}）】",
            "==========================================",
        ])
        if live_macro_bits:
            out.append(f"· 实时跨资产先导锚（抓取于 {now_hhmm}）：" + " | ".join(live_macro_bits))
        if us_holiday_warn:
            out.append(us_holiday_warn)
        out.append("· 海外指数与 A 股产业链映射报价（括号内为最近交易日）：")
        for code, label in US_MAPPING:
            q = quotes.get(code)
            if q:
                q_dt = str(q.get("time") or "")[:10]
                dt_s = f" [{q_dt}]" if q_dt else ""
                out.append(f"  - {label} ({code})：{q['last']} ({q['pct_str']}){dt_s}")
        if us_mkt_txt:
            out.append("\n· 今早《美股收盘 & 加密行情日报》正稿参考（请直接承接其中 2Y/10Y 美债、VIX/VIX3M、AI 自选股五层分化与核心叙事）：")
            out.append(us_mkt_txt)
        if us_news_sum:
            out.append("\n· 今早《美股要闻》30 秒速览：")
            out.append(us_news_sum)

        prev_dt, prev_close_txt = _load_latest_a_share_close_archive(today_str)
        if prev_close_txt:
            out.extend([
                "",
                "==========================================",
                f"【二、📜 前一日 A 股收盘复盘归档（{prev_dt}，用于盘前主线延续性与兑现研判）】",
                "==========================================",
                prev_close_txt,
            ])
    else:
        out.extend([
            "",
            "==========================================",
            f"【一、🌍 实时跨资产锚与隔夜海外参照（抓取于 {now_hhmm}）】",
            "==========================================",
        ])
        if live_macro_bits:
            out.append("- 实时跨资产先导锚：" + " | ".join(live_macro_bits))
        for code, label in US_MAPPING:
            q = quotes.get(code)
            if q:
                q_dt = str(q.get("time") or "")[:10]
                dt_s = f" [{q_dt}]" if q_dt else ""
                out.append(f"- {label} ({code})：{q['last']} ({q['pct_str']}){dt_s}")

    # A 股核心微观结构 + 255 日量化技术诊断
    idx_title = (
        "【三、🇨🇳 A股核心指数、微观结构宽度与 255 日量化技术锚点（MA20/50/200 · HV20 · Z_vol · CLV · RVOL · RSI14 · 布林%B）】"
        if session_mode == "premarket"
        else "【二、🇨🇳 A股今日核心指数、微观结构宽度与 255 日量化技术诊断（MA20/50/200 · HV20 · Z_vol · CLV · RVOL · RSI14 · 布林%B）】"
    )
    breadth_real_str = ""
    if em_breadth.get("up") is not None:
        br_parts = [f"全市场真实宽度 {em_breadth['up']} 涨 / {em_breadth['down']} 跌"]
        if em_breadth.get("zt") is not None:
            lbc_s = f"（最高 {em_breadth['max_lbc']} 连板）" if em_breadth.get("max_lbc") else ""
            br_parts.append(f"自然涨停 {em_breadth['zt']} 家{lbc_s}")
        if em_breadth.get("dt") is not None:
            br_parts.append(f"跌停 {em_breadth['dt']} 家")
        if em_breadth.get("zb") is not None:
            zt_n = em_breadth.get("zt", 0)
            zb_n = em_breadth["zb"]
            seal_rate = (zt_n / (zt_n + zb_n) * 100.0) if (zt_n + zb_n) > 0 else 0.0
            br_parts.append(f"炸板 {zb_n} 家（封板率 {seal_rate:.0f}%）")
        breadth_real_str = " | ".join(br_parts)

    bond_str = " | ".join(
        f"{lbl} {quotes[c]['last']:.3f} ({quotes[c]['pct_str']})"
        for c, lbl in CN_BOND_ETFS if quotes.get(c) and quotes[c].get("last", 0) > 0
    )
    broad_etf_str = " | ".join(
        f"{lbl} {quotes[c]['pct_str']} (成交 {quotes[c]['amount_yi']:.1f}亿)"
        for c, lbl in CN_BROAD_ETFS if quotes.get(c) and quotes[c].get("last", 0) > 0
    )

    out.extend([
        "",
        "==========================================",
        idx_title,
        "==========================================",
        f"- 沪深两市合计成交额：{cn_total_amt:,.1f} 亿元（上证 {sh_amt:,.1f} 亿 + 深证 {sz_amt:,.1f} 亿；含北交所全市场约 {cn_all_amt:,.1f} 亿）{amt_delta_str}",
        f"- 风格因子差：{style_str}",
    ])
    if breadth_real_str:
        out.append(f"- {breadth_real_str}")
    out.append(
        f"- 机构主线池宽度：{len(cn_sec_all)} 大主线 {cn_sec_up} 涨 {cn_sec_down} 跌 | "
        f"{len(active_pool_quotes)} 只活跃龙头 {cn_stk_up} 涨 {cn_stk_down} 跌"
    )
    if bond_str:
        out.append(f"- 国内利率代理（股债跷跷板）：{bond_str}")
    if basis_parts:
        out.append("- 股指期货主力基差：" + " | ".join(basis_parts))
    if broad_etf_str:
        out.append(f"- 国家队/宽基 ETF 成交额代理：{broad_etf_str}")

    for code, label in CN_INDICES:
        q = quotes.get(code)
        if not q:
            continue
        md = ma_detail_map.get(code) or {}
        ma_info = f" | {md['text']}" if md.get("text") else ""
        if is_premarket_intraday and md.get("bar"):
            b = md["bar"]
            out.append(f"- {label} ({code}) [前收 {b['date']}]：{b['close']:.2f} ({b['pct']:+.2f}%) | 振幅 {b['amplitude']:.2f}%{ma_info}")
        else:
            out.append(f"- {label} ({code})：{q['last']:.2f} ({q['pct_str']}) | 振幅 {q['amplitude']:.2f}%{ma_info}")

    # 板块排行：16 大高纯度机构主线作为唯一主榜 + 东财行业榜作为盘外补充
    sec_title = (
        "【四、🇨🇳 A股 16 大高纯度机构主线领涨/领跌 Top 3（主榜）与东财行业补充】"
        if session_mode == "premarket"
        else "【三、🇨🇳 A股今日 16 大高纯度机构主线领涨/领跌 Top 3（主榜）与东财行业补充】"
    )
    out.extend([
        "",
        "==========================================",
        sec_title,
        "==========================================",
    ])
    out.extend(_format_sector_block("🔥 A股 16 大机构主线领涨 Top 3（主榜，含样本总成交额）：", cn_pool_sectors["gainers"], is_gainer=True))
    out.extend(_format_sector_block("❄️ A股 16 大机构主线领跌 Top 3（主榜，严禁混入收涨股）：", cn_pool_sectors["losers"], is_gainer=False))
    if em_sectors:
        out.extend(_format_sector_block("⚡ 盘外热点行业参考（东财全市场行业榜，已剔除成分<8或成交<50亿伪行业）：", em_sectors["gainers"], is_gainer=True))
        out.extend(_format_sector_block("⚡ 盘外承压行业参考（东财全市场行业榜）：", em_sectors["losers"], is_gainer=False))

    # 国内宏观日程窗口提示
    cn_cal_hints = _build_cn_macro_calendar_hints(now)
    if cn_cal_hints:
        out.extend([
            "",
            "==========================================",
            "【📅 国内宏观发布窗口提示（未来 5 个自然日）】",
            "==========================================",
            "· " + " | ".join(cn_cal_hints),
        ])

    tele_title = (
        f"【五、📰 收盘后至开盘前（上一交易日 15:00 – 今日 09:24）宏观政策与行业电报（已净化，最新 {len(telegraph)} 条）】"
        if session_mode == "premarket"
        else f"【四、📰 实时 7x24 财经电报与政策/行业/公司催化（已净化，最新 {len(telegraph)} 条）】"
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

    snapshot_for_memory = None
    if not is_close_intraday:
        snapshot_for_memory = {
            "date": effective_trade_date,
            "cn": {
                "date": effective_trade_date,
                "indices": (
                    f"上证 {sh_q.get('last', 0):.2f} ({sh_q.get('pct_str', '?')}) | "
                    f"创业板 {cy_q.get('last', 0):.2f} ({cy_q.get('pct_str', '?')}) | "
                    f"科创50 {kc_q.get('last', 0):.2f} ({kc_q.get('pct_str', '?')})"
                ),
                "turnover": f"{cn_total_amt:,.1f}亿",
                "turnover_yi": round(cn_total_amt, 1),
                "breadth_str": breadth_real_str or f"{cn_stk_up}涨/{cn_stk_down}跌(样本)",
                "style_str": f"1000-300 {style_1000_300:+.2f}%",
                "style_1000_300": round(style_1000_300, 2),
                "top3_gainers": [f"{s['name']}({s['pct']:+.2f}%)" for s in cn_pool_sectors["gainers"]],
                "top3_losers": [f"{s['name']}({s['pct']:+.2f}%)" for s in cn_pool_sectors["losers"]],
            },
        }

    meta = {
        "log_summary": f"A股({session_mode}) 255日量化微观结构+16大主线 ({len(quotes)}标的) + 电报 {len(telegraph)} 条",
        "metrics": {
            "session": session_mode,
            "quotes_count": len(quotes),
            "telegraph_count": len(telegraph),
            "intraday_snapshot": is_close_intraday,
        },
        "snapshot": snapshot_for_memory,
    }
    meta_json = json.dumps(meta, ensure_ascii=False, indent=2)
    _session_meta_path(session_mode).write_text(meta_json, encoding="utf-8")
    FETCH_META.write_text(meta_json, encoding="utf-8")
    emit_fetch_output(out, LAST_CONTEXT)
    return 0


def _validate_draft_session(text_cn: str, sess_mode: str) -> tuple[bool, str]:
    """硬闸门：校验草稿标题与目标场次严格匹配，杜绝晚盘重发早盘稿污染飞书与 Notion。"""
    head = "\n".join(text_cn.splitlines()[:5])
    if sess_mode == "premarket":
        if "收盘复盘" in head and "开盘前瞻" not in head:
            return False, "草稿标题为《收盘复盘》，与目标场次 premarket 不匹配，已拦截发送"
        if "开盘前瞻" not in head:
            return False, "premarket 草稿缺少《开盘前瞻》标题，已拦截发送"
    elif sess_mode == "close":
        if "开盘前瞻" in head and "收盘复盘" not in head:
            return False, "草稿标题为《开盘前瞻》，与目标场次 close 不匹配，已拦截发送"
        if "收盘复盘" not in head:
            return False, "close 草稿缺少《收盘复盘》标题，已拦截发送"
    return True, ""


def run_send(session_override: str | None = None) -> int:
    t0 = time.time()
    meta_path = _session_meta_path(session_override) if session_override else FETCH_META
    if not meta_path.exists():
        meta_path = FETCH_META
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    except Exception:
        meta = {}
    sess_mode = session_override or meta.get("metrics", {}).get("session") or "close"

    if _session_ran_today(LOG_FILE, sess_mode):
        print(f"今天 A 股 {sess_mode} 场次已成功运行过，跳过发送。如需强制请设置 FORCE_RUN=1。", file=sys.stderr)
        return 0

    webhook = os.getenv("FEISHU_WEBHOOK_CN", "") or FEISHU_WEBHOOK_CN
    if not webhook:
        write_log("FAIL", f"缺少 FEISHU_WEBHOOK_CN 环境变量（{sess_mode}），拒绝回退至其他群", metrics={"session": sess_mode})
        return 1

    draft_path = _session_draft_path(sess_mode)
    if not draft_path.exists():
        write_log("FAIL", f"A股稿件缺失（{sess_mode}；未找到 {draft_path.name}）", metrics={"session": sess_mode})
        return 1

    # 校验草稿新鲜度（非强制模式下草稿必须不早于本次 fetch 元数据）
    if os.getenv("FORCE_RUN") != "1" and meta_path.exists():
        try:
            if draft_path.stat().st_mtime + 2.0 < meta_path.stat().st_mtime:
                write_log(
                    "FAIL",
                    f"草稿 {draft_path.name} 早于本次 fetch 快照（疑似旧场次残留草稿，{sess_mode}），已拦截发送",
                    metrics={"session": sess_mode},
                )
                return 1
        except OSError:
            pass

    text_cn = draft_path.read_text(encoding="utf-8").strip()
    if not text_cn:
        write_log("FAIL", f"A股稿件为空（{sess_mode}）", metrics={"session": sess_mode})
        return 1

    ok_sess, err_sess = _validate_draft_session(text_cn, sess_mode)
    if not ok_sess:
        write_log("FAIL", err_sess, metrics={"session": sess_mode})
        return 1

    clean_cn = sanitize_html(text_cn)
    save_pending([clean_cn])

    print("📨 正在推送《🇨🇳 A股报告》至 A 股飞书机器人...", file=sys.stderr)
    sent_cn = send_feishu(clean_cn, webhook)
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
        if sess_mode == "close" and snap and snap.get("cn") and not (meta.get("metrics") or {}).get("intraday_snapshot"):
            cn_sig, cn_focus = _extract_signal_and_focus(text_cn)
            cn_entry = {**snap["cn"], "signal": cn_sig, "focus": cn_focus}
            cn_hist = [r for r in _load_market_memory() if r.get("date") != cn_entry["date"]]
            cn_hist.append(cn_entry)
            cn_hist.sort(key=lambda r: r.get("date", ""))
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
    _cli_sess = "close"
    for _idx, _arg in enumerate(sys.argv):
        if _arg == "--session" and _idx + 1 < len(sys.argv):
            _cli_sess = sys.argv[_idx + 1]
    try:
        sys.exit(main())
    except Exception:
        err = traceback.format_exc().strip().splitlines()[-1]
        write_log("FAIL", f"{err} ({_cli_sess})", metrics={"session": _cli_sess})
        raise
