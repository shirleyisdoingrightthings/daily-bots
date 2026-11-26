#!/usr/bin/env python3
"""
notion_archive.py — 定时 Bot 每日内容同步归档至 Notion 数据库。

配置方式（环境变量或仓库根目录 / shared/.env）：
- NOTION_ARCHIVE_API_KEY      Notion Internal Integration Token
- NOTION_ARCHIVE_DATABASE_ID  目标 Notion Database ID

支持两种调用模式：
1. Python 模块调用：from notion_archive import archive_to_notion
2. CLI 命令行调用：python3 notion_archive.py --source "🐙 GitHub & HN" --draft "path/to/draft.txt"
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
import urllib.error
import requests
from datetime import datetime
from pathlib import Path

# 常量定义
DEFAULT_DATABASE_ID = ""
NOTION_VERSION = "2022-06-28"
TEXT_LIMIT = 2000


def _read_dotenv_value(var_name: str) -> str:
    """按环境变量 -> shared/.env -> 仓库根目录 .env 顺序读取配置值。"""
    val = os.getenv(var_name, "").strip()
    if val:
        return val
    shared_dir = Path(__file__).resolve().parent
    for env_path in (shared_dir / ".env", shared_dir.parent / ".env"):
        if env_path.exists():
            for line in env_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith(f"{var_name}="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


# 从环境变量或 .env 获取专属归档 API KEY 与 Database ID
def get_notion_api_key() -> str:
    return _read_dotenv_value("NOTION_ARCHIVE_API_KEY")


def get_notion_database_id() -> str:
    return _read_dotenv_value("NOTION_ARCHIVE_DATABASE_ID") or DEFAULT_DATABASE_ID


def get_us_embed_url() -> str:
    """获取美股日报可选的交互式行情看板嵌入 URL（来自环境变量或本地私有 .env）。"""
    u = _read_dotenv_value("US_STOCK_NOTION_EMBED_URL")
    return u if u.lower().startswith(("http://", "https://")) else ""


# ───────────────────────────────────────────
# Markdown / HTML 解析为 Notion Blocks
# ───────────────────────────────────────────

INLINE_RE = re.compile(
    r"(?P<code>`[^`\n]+`)"
    r"|(?P<bold_link>\*\*\[[^\]\n]+\]\((?:\([^()\s]*\)|[^()\s])+\)\*\*)"
    r"|(?P<html_link><a\s+[^>]*?href=[\"'](?P<href>[^\"']+)[\"'][^>]*>(?P<anchor>.*?)</a>)"
    r"|(?P<link>\[[^\]\n]+\]\((?:\([^()\s]*\)|[^()\s])+\))"
    r"|(?P<bold>\*\*.+?\*\*)"
    r"|(?P<html_bold><b>.*?</b>)"
    r"|(?P<html_italic><(?:i|em)>.*?</(?:i|em)>)",
    re.IGNORECASE,
)

_RESIDUAL_TAG_RE = re.compile(r"</?(?:b|strong|i|em|code|a)(?:\s+[^<>]*)?>", re.I)


def add_cjk_spacing(text: str) -> str:
    """在中文汉字与数字/英文字母/Ticker/半角括号之间自动补齐 1 个半角空格（不破坏 URL、HTML 标签与 #标签）。"""
    if not text:
        return text
    parts = re.split(r"(<[^>]+>|https?://[^\s<>\"'）)]+|#[A-Za-z0-9\u4e00-\u9fff_-]+)", text)
    out = []
    for idx, seg in enumerate(parts):
        if idx % 2 == 1 or not seg:
            out.append(seg)
            continue
        # 1. 中文 ↔ 英文 / 数字 / $ / % / ★ / 带正负号数字
        seg = re.sub(r"([\u4e00-\u9fff])([A-Za-z0-9$%★]|(?<![A-Za-z0-9])[+-]\d)", r"\1 \2", seg)
        seg = re.sub(r"([A-Za-z0-9%])([\u4e00-\u9fff])", r"\1 \2", seg)
        # 2. 中文 ↔ 半角括号内的英文/数字/涨跌幅（如 宣伟(SHW) -> 宣伟 (SHW)，道指(+0.33%) -> 道指 (+0.33%)）
        seg = re.sub(r"([\u4e00-\u9fff])\(([A-Za-z0-9+\-$%])", r"\1 (\2", seg)
        seg = re.sub(r"([A-Za-z0-9%])\)([\u4e00-\u9fff])", r"\1) \2", seg)
        # 3. Ticker / 价格 紧贴涨跌幅括号（如 META(+3.40%) -> META (+3.40%)，235.28(-3.90%) -> 235.28 (-3.90%)）
        seg = re.sub(r"([A-Za-z0-9])\(([+-]\d)", r"\1 (\2", seg)
        # 4. 合并行内多余连续空格，保留换行与行首缩进
        seg = re.sub(r"(?<=[^\s]) {2,}", " ", seg)
        out.append(seg)
    return "".join(out)


def _clean_text(s: str) -> str:
    s = re.sub(r"&amp;(?:amp;)+", "&amp;", s or "")
    s = re.sub(r"(\d+)\\\.", r"\1.", s)
    return html.unescape(_RESIDUAL_TAG_RE.sub("", s))


def _text_items(
    content: str,
    *,
    bold: bool = False,
    italic: bool = False,
    code: bool = False,
    color: str | None = None,
    link: str | None = None,
) -> list:
    if not content:
        return []
    if not code:
        content = add_cjk_spacing(content)
    items = []
    chunks = [content[i : i + TEXT_LIMIT] for i in range(0, len(content), TEXT_LIMIT)]
    for chunk in chunks:
        item = {"type": "text", "text": {"content": chunk}}
        if link:
            item["text"]["link"] = {"url": link}
        annotations = {}
        if bold:
            annotations["bold"] = True
        if italic:
            annotations["italic"] = True
        if code:
            annotations["code"] = True
        if color and color != "default":
            annotations["color"] = color
        if annotations:
            item["annotations"] = annotations
        items.append(item)
    return items


def rich_text(
    text: str,
    *,
    default_bold: bool = False,
    default_italic: bool = False,
    default_color: str | None = None,
) -> list:
    """把行内 Markdown 与受控 HTML 转换为 Notion rich_text"""
    # 预处理嵌套 <b>1. <a href="...">Title</a></b> → <b>1.</b> <a href="..."><b>Title</b></a>
    text = re.sub(
        r"<b>(\d+\.)\s*(<a\s+[^>]+>)(?:<b>)?(.*?)(?:</b>)?</a>\s*</b>",
        r"<b>\1</b> \2<b>\3</b></a>",
        text,
        flags=re.I,
    )
    # 预处理裸包裹 <b><a href="...">Title</a></b> → <a href="..."><b>Title</b></a>
    text = re.sub(
        r"<b>\s*(<a\s+[^>]+>)(?:<b>)?(.*?)(?:</b>)?</a>\s*</b>",
        r"\1<b>\2</b></a>",
        text,
        flags=re.I,
    )
    parts = []
    pos = 0
    for m in INLINE_RE.finditer(text):
        if m.start() > pos:
            parts += _text_items(
                _clean_text(text[pos : m.start()]),
                bold=default_bold,
                italic=default_italic,
                color=default_color,
            )
        kind = m.lastgroup
        raw = m.group()
        if kind == "code":
            parts += _text_items(
                html.unescape(raw[1:-1]),
                bold=default_bold,
                italic=default_italic,
                code=True,
                color=default_color,
            )
        elif kind == "bold_link":
            match = re.match(r"\*\*\[([^\]\n]+)\]\(((?:\([^()\s]*\)|[^()\s])+)\)\*\*", raw)
            if match:
                label, url = match.groups()
                valid_url = url if url.lower().startswith(("http://", "https://")) else None
                parts += _text_items(html.unescape(label), bold=True, italic=default_italic, link=valid_url)
        elif kind == "bold":
            inner = raw[2:-2]
            if re.search(r"<a\s|<(?:i|em)\b|`[^`\n]+`|\[[^\]\n]+\]\((?:\([^()\s]*\)|[^()\s])+\)", inner, re.I):
                parts += rich_text(inner, default_bold=True, default_italic=default_italic, default_color=default_color)
            else:
                parts += _text_items(_clean_text(inner), bold=True, italic=default_italic, color=default_color)
        elif kind == "html_bold":
            inner = raw[3:-4]
            if re.search(r"<a\s|<(?:i|em)\b|`[^`\n]+`|\[[^\]\n]+\]\((?:\([^()\s]*\)|[^()\s])+\)", inner, re.I):
                parts += rich_text(inner, default_bold=True, default_italic=default_italic, default_color=default_color)
            else:
                parts += _text_items(_clean_text(inner), bold=True, italic=default_italic, color=default_color)
        elif kind == "html_italic":
            inner_m = re.match(r"^<(?:i|em)>(.*)</(?:i|em)>$", raw, re.I | re.S)
            inner = inner_m.group(1) if inner_m else raw
            if re.search(r"<a\s|<b\b|\*\*|\[[^\]\n]+\]\((?:\([^()\s]*\)|[^()\s])+\)", inner, re.I):
                parts += rich_text(inner, default_bold=default_bold, default_italic=True, default_color=default_color)
            else:
                parts += _text_items(_clean_text(inner), bold=default_bold, italic=True, color=default_color)
        elif kind == "link":
            match = re.match(r"\[([^\]\n]+)\]\(((?:\([^()\s]*\)|[^()\s])+)\)", raw)
            if match:
                label, url = match.groups()
                valid_url = url if url.lower().startswith(("http://", "https://")) else None
                parts += _text_items(html.unescape(label), bold=default_bold, italic=default_italic, link=valid_url)
        elif kind == "html_link":
            href = html.unescape(m.group("href")).strip()
            anchor = m.group("anchor")
            is_bold = default_bold or bool(re.search(r"<\s*(?:b|strong)\b", anchor, re.I))
            is_italic = default_italic or bool(re.search(r"<\s*(?:i|em)\b", anchor, re.I))
            clean_anchor = html.unescape(re.sub(r"<[^>]+>", "", anchor)).strip()
            valid_href = href if href.lower().startswith(("http://", "https://")) else None
            parts += _text_items(clean_anchor or href, bold=is_bold, italic=is_italic, link=valid_href)
        pos = m.end()
    if pos < len(text):
        parts += _text_items(_clean_text(text[pos:]), bold=default_bold, italic=default_italic, color=default_color)
    return parts or _text_items(_clean_text(text), bold=default_bold, italic=default_italic, color=default_color)


def _strip_tags_plain(s: str) -> str:
    """将 HTML / Markdown 链接剥离为可读纯文本"""
    s = re.sub(r"<a\s+[^>]*?href=[\"'][^\"']+[\"'][^>]*>(.*?)</a>", r"\1", s or "", flags=re.I | re.S)
    s = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", s)
    s = re.sub(r"<[^>]+>", "", s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"\1", s)
    s = re.sub(r"(\d+)\\\.", r"\1.", s)
    return html.unescape(s).strip()


def _join_clauses(clauses: list[str], max_chars: int = 1400) -> str:
    """完整分句拼接，严格避免截断在半句话中间"""
    cleaned = []
    for c in clauses:
        t = re.sub(r"\s+", " ", (c or "")).strip().rstrip("；;。")
        if t:
            cleaned.append(t)
    if not cleaned:
        return ""
    out_parts = []
    cur_len = 0
    for c in cleaned:
        add_len = len(c) + (1 if out_parts else 0)
        if out_parts and cur_len + add_len > max_chars:
            break
        out_parts.append(c)
        cur_len += add_len
    res = "；".join(out_parts)
    if len(res) > max_chars:
        cut = res[:max_chars]
        for sep in ("；", "。", "，", " "):
            idx = cut.rfind(sep)
            if idx >= max_chars // 2:
                cut = cut[:idx]
                break
        res = cut.rstrip("；，, ")
    return res + "。" if res and not res.endswith(("。", "！", "？")) else res


def _extract_cn_hk_market_brief(
    section_text: str,
    market_label: str,
    is_premarket: bool,
    *,
    include_signal: bool = True,
) -> str:
    """从 A股 或 港股 单篇正文提取完整不截断的摘要片段"""
    plain_lines = [_strip_tags_plain(l) for l in section_text.splitlines()]
    plain_lines = [
        re.sub(r"^#{1,3}\s*", "", l).strip()
        for l in plain_lines
        if l and not re.match(r"^(?:#{1,3}\s*)?(?:📈|📉|🇭🇰|🇨🇳|\s)*(?:A股|港股)市场（.*?）\s*$", l)
    ]

    indices_str = ""
    width_str = ""
    signal_str = ""
    gainers: list[str] = []
    losers: list[str] = []
    watch_titles: list[str] = []

    mode_section = ""
    for line in plain_lines:
        clean_line = re.sub(r"^(?:[\U0001F1E6-\U0001F1FF]{2}|[\U0001F300-\U0001FAFF\u2600-\u27BF\uFE0F\s])+", "", line).strip()
        if is_premarket:
            if not indices_str and re.match(r"^隔夜(?:外围映射|中概)", clean_line):
                indices_str = clean_line
            elif not width_str and re.match(r"^前日(?:收盘|港股)技术锚点", clean_line):
                width_str = clean_line
            elif line.startswith("⚡"):
                mode_section = "watch"
            elif line.startswith("🎯"):
                mode_section = "signal"
            elif mode_section == "watch" and re.match(r"^\d+\.\s+", line):
                head = line.split("：", 1)[0].split(":", 1)[0].strip()
                watch_titles.append(head)
            elif mode_section == "signal" and not signal_str and not line.lstrip("·•-* ").strip().startswith(("💡", "👀", "🚨")):
                cand = line.lstrip("·•-* ").strip()
                if re.match(r"^(?:(?:🟢|🟡|🔴|🚦)\s*)?(?:早盘)?信号灯", cand) or cand.startswith(("🟢", "🟡", "🔴")):
                    sig = re.split(r"\s*[—–]\s*", cand, maxsplit=1)[0].strip()
                    signal_str = re.sub(r"^(?:🚦\s*)?(?:早盘)?信号灯(?:状态)?[:：]\s*", "信号灯：", sig)
        else:
            if not indices_str and re.match(r"^核心指数", clean_line):
                indices_str = clean_line
            elif line.startswith(("🧭 市场宽度", "🧭 早盘宽度")):
                if "—" in line:
                    width_str = line.split("—", 1)[1].strip()
                elif "：" in line:
                    width_str = line.split("：", 1)[1].strip()
            elif line.startswith("🔥"):
                mode_section = "gain"
            elif line.startswith("❄️"):
                mode_section = "loss"
            elif line.startswith(("🔭", "🎯")):
                mode_section = "signal" if line.startswith("🎯") else ""
            elif mode_section in ("gain", "loss") and re.match(r"^\d+\.\s+", line):
                item = re.sub(r"^\d+\.\s+", "", line).strip()
                if mode_section == "gain" and len(gainers) < 3:
                    gainers.append(item)
                elif mode_section == "loss" and len(losers) < 3:
                    losers.append(item)
            elif mode_section == "signal" and not signal_str and not line.lstrip("·•-* ").strip().startswith(("💡", "👀", "🚨")):
                cand = line.lstrip("·•-* ").strip()
                if re.match(r"^(?:(?:🟢|🟡|🔴|🚦)\s*)?(?:早盘|午间)?信号灯", cand) or cand.startswith(("🟢", "🟡", "🔴")):
                    sig = re.split(r"\s*[—–]\s*", cand, maxsplit=1)[0].strip()
                    signal_str = re.sub(r"^(?:🚦\s*)?(?:早盘|午间)?信号灯(?:状态)?[:：]\s*", "信号灯：", sig)

    parts = []
    if include_signal and signal_str:
        parts.append(signal_str)
    if indices_str:
        parts.append(indices_str)
    if is_premarket:
        if width_str:
            parts.append(width_str)
        if watch_titles:
            parts.append("核心看点：" + " / ".join(watch_titles[:3]))
    else:
        if width_str:
            parts.append(width_str)
        if gainers:
            parts.append("领涨：" + "、".join(gainers))
        if losers:
            parts.append("领跌：" + "、".join(losers))
    parts = [p.strip().rstrip("；;。") for p in parts if p and p.strip().rstrip("；;。")]
    if parts:
        parts[0] = f"【{market_label}】{parts[0]}"
    return "；".join(parts)


_CIRCLED_NUMS = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮"


def _circled(idx: int) -> str:
    return _CIRCLED_NUMS[idx] if 0 <= idx < len(_CIRCLED_NUMS) else f"{idx + 1}."


def _sort_dated_events(items: list[str]) -> list[str]:
    """按 MM-DD 日期先后顺序排序事件条目（同日盘前优先于盘后），无日期条目排后。"""
    def _key(s: str) -> tuple:
        m = re.search(r"(\d{1,2})-(\d{1,2})", s)
        if not m:
            return (99, 99, 9, s)
        t_rank = 0 if "盘前" in s else (2 if "盘后" in s else 1)
        return (int(m.group(1)), int(m.group(2)), t_rank, s)
    return sorted(items, key=_key)


def _merge_same_day_events(items: list[str], *, inner_sep: str = "、", group_sep: str = "、") -> str:
    """将按日期排好序的事件合并同日重复日期前缀（如 11-26 美国 Core PCE、11-26 美国 GDP -> 11-26 美国核心 PCE、美国 GDP）。"""
    ordered = _sort_dated_events(items)
    groups: list[tuple[str, list[str]]] = []
    for raw in ordered:
        txt = re.sub(r"([\u4e00-\u9fff])\s*\bCore\s+PCE(?:\s+Price\s+Index|\s+Prices)?\b", r"\1核心 PCE", raw.strip(), flags=re.I)
        txt = re.sub(r"\bCore\s+PCE(?:\s+Price\s+Index|\s+Prices)?\b", "核心 PCE", txt, flags=re.I)
        m = re.match(r"^(\d{1,2}-\d{1,2})\s+(.+)$", txt)
        if m:
            dt, body = m.group(1), m.group(2).strip()
            if groups and groups[-1][0] == dt:
                groups[-1][1].append(body)
            else:
                groups.append((dt, [body]))
        else:
            groups.append(("", [txt]))
    rendered = []
    for dt, bodies in groups:
        if not dt:
            rendered.append(inner_sep.join(bodies))
        else:
            # 同日内若相邻项重复相同盘前/盘后前缀，合并为 "12-02 盘后 ★CRM、SNOW"
            merged_bodies = []
            prev_sess = ""
            for b in bodies:
                ms = re.match(r"^(盘前|盘后)\s+(.+)$", b)
                if ms and ms.group(1) == prev_sess:
                    merged_bodies.append(ms.group(2).strip())
                else:
                    prev_sess = ms.group(1) if ms else ""
                    merged_bodies.append(b)
            rendered.append(f"{dt} {inner_sep.join(merged_bodies)}")
    return group_sep.join(rendered)


def _earliest_mmdd(s: str) -> tuple[int, int]:
    hits = re.findall(r"(\d{1,2})-(\d{1,2})", s or "")
    if not hits:
        return (99, 99)
    return min((int(a), int(b)) for a, b in hits)


def _bold_watchlist_tickers(val: str) -> str:
    """为 AI 自选股明细行中的 Ticker 代码自动加粗（如 CEG 235.28 -> <b>CEG</b> 235.28）。"""
    if not val:
        return val
    parts = [p.strip() for p in val.split("|") if p.strip()]
    out = []
    for p in parts:
        if p.startswith("<b>"):
            out.append(p)
        else:
            out.append(re.sub(r"^([A-Z]{1,5}(?:\.[A-Z])?)(?=\s)", r"<b>\1</b>", p))
    return " | ".join(out)


def _extract_us_market_brief(market_text: str) -> str:
    """从美股收盘消息①提取前置信号灯、核心跨资产指数、BTC/恐贪情绪、AI 五层强弱与近期事件摘要"""
    plain_lines = [
        re.sub(r"^#{1,3}\s*", "", _strip_tags_plain(l)).strip()
        for l in market_text.splitlines()
        if l.strip()
    ]
    std_sig = extract_market_signal(market_text, source="美股与加密")
    signal_str = ""
    indices_str = ""
    crypto_line = ""
    crypto_str = ""
    fear_str = ""
    ai_wl_line = ""
    events_line = ""
    for line in plain_lines:
        clean = re.sub(r"^(?:[\U0001F1E6-\U0001F1FF]{2}|[\U0001F300-\U0001FAFF\u2600-\u27BF\uFE0F\s])+", "", line).strip()
        if not indices_str and re.match(r"^核心指数[:：\s]", clean):
            indices_str = clean
        if not crypto_line and line.startswith("🪙"):
            crypto_line = clean
            m_btc = re.search(r"(BTC\s+[^|｜\n]+)", clean)
            crypto_str = m_btc.group(1).strip() if m_btc else clean
        if not fear_str and "恐贪指数" in clean:
            m_fg = re.search(r"(恐贪指数\s*[^|｜\n]+)", clean)
            if m_fg:
                fear_str = m_fg.group(1).strip()
        if not ai_wl_line and line.startswith("🍰") and ("：" in clean or ":" in clean):
            ai_wl_line = re.sub(r"^[^：:]+[：:]\s*", "", clean).strip()
        if not events_line and line.startswith("📅") and ("：" in clean or ":" in clean):
            events_line = re.sub(r"^[^：:]+[：:]\s*", "", clean).strip()
        if not signal_str and not line.lstrip("·•-* ").strip().startswith(("💡", "👀", "🚨")):
            cand = line.lstrip("·•-* ").strip()
            if re.match(r"^(?:(?:🟢|🟡|🔴|🚦)\s*)?信号灯", cand):
                sig = re.split(r"\s*[—–]\s*", cand, maxsplit=1)[0].strip()
                sig = re.sub(r"^(?:🚦\s*)?信号灯(?:状态)?[:：]\s*", "", sig).strip()
                signal_str = re.sub(r"^([🟢🟡🔴])(\S)", r"\1 \2", sig)

    sig_final = std_sig or signal_str
    is_upgraded = any(
        k in market_text
        for k in ("伦敦金", "黄金", "布伦特原油", "Financial Events", "AI 自选股", "AI 五层自选股", "加密货币")
    )

    if is_upgraded:
        core_items: list[str] = []
        idx_body = re.sub(r"^核心指数[:：\s]*", "", indices_str)

        def _pick_pct(label_re: str, alias: str) -> str:
            m = re.search(rf"{label_re}(?:\s*[\(（][^()（）]{{1,16}}[\)）])?\s+[\d,.$%]+\s*\(([^)]+)\)", idx_body)
            return f"{alias} {m.group(1)}" if m else ""

        def _pick_val(label_re: str, alias: str) -> str:
            m = re.search(rf"{label_re}\s+([\d,.]+%?)", idx_body)
            return f"{alias} {m.group(1)}" if m else ""

        gold_item = _pick_pct(r"伦敦金", "伦敦金") or _pick_pct(r"黄金", "伦敦金")
        for item in (
            _pick_pct(r"标普\s*500", "标普"),
            _pick_pct(r"纳斯达克", "纳指"),
            _pick_val(r"10\s*年期美债", "10Y 美债"),
            _pick_val(r"VIX", "VIX"),
            gold_item,
            _pick_pct(r"布伦特原油", "布油"),
        ):
            if item:
                core_items.append(item)

        btc_short = ""
        m_btc = re.search(r"BTC\s+(\$?[\d,]+(?:\.\d+)?)\s*\(([^)]+)\)", crypto_line or idx_body)
        if m_btc:
            price_num = m_btc.group(1).split(".")[0]
            btc_short = f"BTC {price_num} ({m_btc.group(2)})"
        elif crypto_str:
            btc_short = crypto_str
        m_fg_num = re.search(r"恐贪指数\s*(\d+)", fear_str)
        if btc_short and m_fg_num:
            core_items.append(f"{btc_short} 恐贪 {m_fg_num.group(1)}")
        elif btc_short:
            core_items.append(btc_short)

        ai_short = ""
        if ai_wl_line:
            layer_hits = re.findall(r"[⚡🔌🏗️🧠🚀\uFE0F]+\s*([^\s·|：:]+)\s*([+-]?\d+(?:\.\d+)?%)", ai_wl_line)
            parsed_layers = []
            for lname, lpct in layer_hits:
                try:
                    parsed_layers.append((lname, lpct, float(lpct.rstrip("%"))))
                except ValueError:
                    pass
            if len(parsed_layers) >= 2:
                parsed_layers.sort(key=lambda x: x[2], reverse=True)
                top_l, bot_l = parsed_layers[0], parsed_layers[-1]
                top_verb = "领涨" if top_l[2] > 0 else "抗跌"
                ai_short = f"{top_l[0]}{top_verb} ({top_l[1]})、{bot_l[0]}承压 ({bot_l[1]})"
            else:
                ai_short = ai_wl_line[:80]

        ev_short = ""
        if events_line:
            earn_m = re.search(r"财报预告[:：]\s*(.+?)(?=\s*\|\s*宏观日程[:：]|$)", events_line)
            macro_m = re.search(r"宏观日程[:：]\s*(.+?)(?=\s*\|\s*财报预告[:：]|$)", events_line)
            ev_dated: list[tuple[tuple[int, int], str]] = []
            if macro_m:
                m_raw = macro_m.group(1).strip()
                if m_raw and "暂无" not in m_raw and "无重磅" not in m_raw:
                    m_items = _sort_dated_events([
                        re.sub(r"\s*[\(（][^()（）]{1,28}[\)）]", "", x).strip()
                        for x in re.split(r"\s*[|、]\s*", m_raw) if x.strip()
                    ])
                    if m_items:
                        ev_dated.append((_earliest_mmdd(m_items[0]), _merge_same_day_events(m_items[:3])))
            if earn_m:
                e_raw = earn_m.group(1).strip()
                if e_raw and "暂无" not in e_raw:
                    e_items = _sort_dated_events([
                        re.sub(r"\s*[\(（]\$[\d.,]+[BMT][\)）]", "", x).strip()
                        for x in re.split(r"\s*[|、]\s*", e_raw) if x.strip()
                    ])
                    if e_items:
                        star_e = [x for x in e_items if "★" in x]
                        non_star_e = [x for x in e_items if "★" not in x]
                        picked_e = _sort_dated_events((star_e + non_star_e)[:3])
                        ev_dated.append((_earliest_mmdd(picked_e[0]), _merge_same_day_events(picked_e)))
            ev_dated.sort(key=lambda x: x[0])
            ev_clean = [txt for _, txt in ev_dated]
            if not ev_clean and "暂无" not in events_line and "无重磅" not in events_line:
                ev_clean.append(events_line[:60])
            if ev_clean:
                ev_short = " ｜ ".join(ev_clean)
                if len(ev_short) > 110:
                    ev_short = ev_short[:108].rstrip("、，,;；｜/ ") + "…"

        segs: list[str] = []
        if sig_final:
            segs.append(f"信号灯：{sig_final}")
        if core_items:
            segs.append("核心资产：" + " ｜ ".join(core_items))
        if ai_short:
            segs.append(f"AI 自选股：{ai_short}")
        if ev_short:
            segs.append(f"近期预告：{ev_short}")
        if segs:
            return "\n".join(f"{_circled(idx)} {s}" for idx, s in enumerate(segs))

    # 兼容历史旧版美股日报格式
    parts = [p for p in (indices_str,) if p]
    if crypto_str and fear_str and "恐贪" not in crypto_str:
        parts.append(f"{crypto_str} ({fear_str})")
    elif crypto_str:
        parts.append(crypto_str)
    elif fear_str:
        parts.append(fear_str)
    if not parts and not sig_final:
        return ""
    body_str = "｜".join(p.rstrip("；;。") for p in parts)
    if sig_final and body_str:
        return f"信号灯：{sig_final}｜【美股盘面】{body_str}"
    if sig_final:
        return f"信号灯：{sig_final}"
    return f"【美股盘面】{body_str}"


def _extract_summary_bullets(section_text: str) -> list[str]:
    """抽取 30秒速览 区块中的要点列表"""
    m = re.search(
        r"(?:⚡\s*30秒速览|30秒速览)[\s\S]*?(?=\n\s*(?:<(?:b|strong)\b[^>]*>\s*)*(?:[#🚀🔥❄️⚡🔭🎯📰📎📈🗽🤖🐦🛠️⭐]|<a\s+[^>]*href=|\*\*\[|---)|\Z)",
        section_text,
        re.IGNORECASE,
    )
    if not m:
        return []
    summary_block = m.group().strip()
    lines = [line.strip() for line in summary_block.splitlines() if line.strip()]
    return [
        _strip_tags_plain(l).lstrip("-*•> ").strip()
        for l in lines
        if not re.search(r"30秒速览", l)
    ]


def extract_summary(text: str, source: str = "", report_type: str = "") -> str:
    """从稿件中抽取 30 秒速览或完整结构化要点（金融市场报告首部直出信号灯，保证绝不半句截断）"""
    # 1. A股与港股：信号灯置于 30 秒速览最开头，随后分别呈现 A股 + 港股 核心数据与看点
    if "A股与港股" in source or any(k in text for k in ("A股开盘前瞻", "A股早盘总结", "A股午间复盘", "A股收盘复盘")):
        secs = re.split(r"\n\s*---+\s*\n", text, maxsplit=1)
        if report_type == "开盘前瞻":
            is_pre = True
        elif report_type in ("午间复盘", "收盘复盘"):
            is_pre = False
        else:
            head_lines = "\n".join(l.strip() for l in secs[0].splitlines()[:4] if l.strip())
            is_pre = bool(re.search(r"(?:A股|港股)开盘前瞻", head_lines))
        cn_sig = extract_market_signal(secs[0], source="A股与港股")
        hk_sig = extract_market_signal(secs[1], source="A股与港股") if len(secs) > 1 else None
        cn_brief = _extract_cn_hk_market_brief(secs[0], "A股", is_pre, include_signal=False)
        hk_brief = _extract_cn_hk_market_brief(secs[1], "港股", is_pre, include_signal=False) if len(secs) > 1 else ""
        sig_parts = []
        if cn_sig:
            sig_parts.append(f"A股 {cn_sig}")
        if hk_sig:
            sig_parts.append(f"港股 {hk_sig}")
        sig_header = f"信号灯：{' / '.join(sig_parts)}" if sig_parts else ""
        body_str = _join_clauses([p for p in (cn_brief, hk_brief) if p], max_chars=1500)
        if sig_header and body_str:
            return f"{sig_header}｜{body_str}"
        if body_str or sig_header:
            return body_str or sig_header

    # 1.5 美股与加密：前置消息①盘面与加密信号灯 + 消息②美股要闻 30秒速览
    if "美股" in source or "美股收盘" in text:
        if re.search(r"(?:#{1,3}\s*|<b>|\*\*)*📋\s*美股要闻", text):
            secs = re.split(r"(?=\n\s*(?:#{1,3}\s*)?(?:<b>|\*\*)?📋\s*美股要闻)", text, maxsplit=1)
            market_part = re.sub(r"\n\s*---+\s*$", "", secs[0]).strip()
            market_brief = _extract_us_market_brief(market_part) if (len(secs) > 1 or "美股收盘" in market_part) else ""
            news_sec = secs[1] if len(secs) > 1 else text
        else:
            secs = re.split(r"\n\s*---+\s*\n", text, maxsplit=1)
            market_brief = _extract_us_market_brief(secs[0]) if ("美股收盘" in secs[0] or len(secs) > 1) else ""
            news_sec = secs[1] if len(secs) > 1 else text
        content_lines = _extract_summary_bullets(news_sec)
        if market_brief and market_brief.startswith("① ") and content_lines:
            existing_cnt = len(market_brief.splitlines())
            news_items = []
            budget = max(400, 1750 - len(market_brief))
            used = 0
            for item in content_lines:
                c = item.strip().rstrip("。；;")
                if not c:
                    continue
                sep_len = 3 if news_items else 0
                if news_items and used + sep_len + len(c) > budget:
                    break
                news_items.append(c)
                used += sep_len + len(c)
            if news_items:
                news_num = _circled(existing_cnt)
                return add_cjk_spacing(f"{market_brief}\n{news_num} 核心要闻：" + " ｜ ".join(news_items))
            return add_cjk_spacing(market_brief)
        if market_brief and market_brief.startswith("① "):
            return add_cjk_spacing(market_brief)
        clauses = ([market_brief] if market_brief else []) + content_lines
        if clauses:
            joined = _join_clauses(clauses, max_chars=1600)
            if joined:
                return add_cjk_spacing(joined)

    # 2. 匹配 30秒速览 区块（兼容带 <b> 包裹的章节标题，采用圆圈序号 ①②③ 分行增强区分度）
    content_lines = _extract_summary_bullets(text)
    if content_lines:
        bullet_lines = []
        used = 0
        for item in content_lines:
            c = item.strip().rstrip("；;")
            if not c:
                continue
            b_line = f"{_circled(len(bullet_lines))} {c}"
            if bullet_lines and used + len(b_line) + 1 > 1500:
                break
            bullet_lines.append(b_line)
            used += len(b_line) + 1
        if bullet_lines:
            return add_cjk_spacing("\n".join(bullet_lines))

    # 3. X 热点播报 & AI Builder：首段导语 + 编号主题列表（圆圈序号 ①② 格式）
    if "X 热点" in source or "AI Builder" in source or re.search(r"^(?:#+\s*)?(?:<b>)?(?:🔥\s*X(?:\s*&\s*Reddit)?\s*(?:热点|社区热议)|🛠\uFE0F?\s*AI\s*Builder)", text.strip()):
        plain_lines = [re.sub(r"^#{1,3}\s*", "", _strip_tags_plain(l)).strip() for l in text.splitlines()]
        plain_lines = [l for l in plain_lines if l]
        overview = ""
        topics: list[str] = []
        for line in plain_lines:
            if re.match(r"^(?:🔥\s*X(?:\s*&\s*Reddit)?\s*(?:热点|社区热议)|🛠\uFE0F?\s*AI\s*Builder)", line):
                continue
            if re.match(r"^\d+\.\s+", line) and len(line) <= 100:
                topics.append(line)
            elif re.match(r"^\[r/[A-Za-z0-9_]+\]\s+\S", line) and len(line) <= 100:
                topics.append(f"Reddit {line}")
            elif not overview and not line.startswith(("原帖：", "原推：", "长文", "素材：", "📎", "🗣️", "- ", "---")):
                overview = line
        if overview:
            overview = re.sub(
                r"^这批(?:推文|动态)(?:集中)?(?:发布于|来自|涵盖)\s+[^\n，。]*?(?:\s*[（(][^）)]*[）)])?\s*(?:[，,]|(?<!\d)[：:](?!\d))\s*",
                "",
                overview,
            ).strip()
        out_lines = []
        if overview:
            out_lines.append(f"{_circled(len(out_lines))} {overview}")
        if topics:
            out_lines.append(f"{_circled(len(out_lines))} 本期焦点：" + " ｜ ".join(topics))
        if out_lines:
            res = "\n".join(out_lines)
            return add_cjk_spacing(res[:1500])

    # 4. 兜底：取完整句子，不超过 600 字符，严禁半句截断
    clean = _strip_tags_plain(text).replace("\n", " ")
    clean = re.sub(r"\s+", " ", clean).strip()
    if len(clean) <= 600:
        return clean
    cut = clean[:600]
    for sep in ("。", "！", "？", "；", ". "):
        idx = cut.rfind(sep)
        if idx >= 180:
            return cut[: idx + (1 if sep != ". " else 1)].strip()
    return cut


def _classify_signal_line(s: str) -> str | None:
    if not s:
        return None
    # 优先依据首个出现的交通灯 Emoji 定性（防止后半句“控仓防守”等词汇反客为主）
    emoji_hits = [
        (s.find(em), label)
        for em, label in (("🟢", "🟢 偏多"), ("🟡", "🟡 震荡分化"), ("🔴", "🔴 防守承压"))
        if em in s
    ]
    if emoji_hits:
        return min(emoji_hits, key=lambda x: x[0])[1]

    head = re.split(r"\s*[—–]\s*", s, maxsplit=1)[0]
    if "偏多" in head or "积极进攻" in head:
        return "🟢 偏多"
    if "震荡" in head or "分化" in head or "观望" in head:
        return "🟡 震荡分化"
    if "防守承压" in head or "防御承压" in head or "偏空" in head or "承压防守" in head or "防守" in head or "谨慎" in head:
        return "🔴 防守承压"
    return None


def extract_market_signal(text: str, source: str = "") -> str | None:
    """仅对金融信源提取市场信号灯状态（🟢 偏多 / 🟡 震荡分化 / 🔴 防守承压）"""
    if "美股" not in source and "A股与港股" not in source:
        return None

    sections = re.split(r"\n\s*---+\s*\n", text, maxsplit=1) if "A股与港股" in source else [text]
    signals: list[str] = []
    for sec in sections:
        sec_sig = None
        in_target = False
        for raw_line in sec.splitlines():
            plain = re.sub(r"^#{1,3}\s*", "", _strip_tags_plain(raw_line)).lstrip("·•-* ").strip()
            if not plain:
                continue
            if plain.startswith("🎯"):
                in_target = True
                continue
            if plain.startswith(("💡", "👀", "🚨")):
                continue
            if re.match(r"^(?:(?:🟢|🟡|🔴|🚦)\s*)?(?:早盘|午间)?信号灯(?:状态)?[:：\s]", plain):
                sec_sig = _classify_signal_line(plain)
                if sec_sig:
                    break
            elif in_target and plain.startswith(("🟢", "🟡", "🔴")):
                sec_sig = _classify_signal_line(plain)
                if sec_sig:
                    break
        if sec_sig:
            signals.append(sec_sig)

    if not signals:
        return None
    uniq = set(signals)
    if len(uniq) == 1:
        return signals[0]
    if "🟢 偏多" in uniq and "🔴 防守承压" in uniq:
        return "🟡 震荡分化"
    if "🔴 防守承压" in uniq:
        return "🔴 防守承压"
    if "🟡 震荡分化" in uniq:
        return "🟡 震荡分化"
    return "🟢 偏多"


def build_clean_title(source: str, date_str: str, report_type: str = "日报") -> str:
    """构建去冗余括号的清晰 Notion 页面标题"""
    if "GitHub & HN" in source:
        return f"{date_str} GitHub & HN 本周趋势榜" if report_type == "周盘点" else f"{date_str} GitHub & HN 早报"
    if "美股" in source:
        return f"{date_str} 美股收盘 & 加密行情"
    if "A股与港股" in source:
        suffix = report_type if report_type in ("开盘前瞻", "午间复盘", "收盘复盘") else "决策复盘"
        return f"{date_str} A股&港股 {suffix}"
    if "AI 产业日报" in source:
        return f"{date_str} AI 产业日报"
    if "X 热点" in source:
        return f"{date_str} X 热点播报"
    if "AI Builder" in source:
        return f"{date_str} AI Builder 日报"
    return f"{date_str} {source} {report_type}".strip()


def _is_star_line(line: str) -> bool:
    plain = _strip_tags_plain(line).strip()
    return bool(re.match(r"^⭐+(?:\s+#\S+.*)?$", plain))


def _is_standalone_headline_link(line: str) -> bool:
    """判断整行是否为独立的新闻标题链接（HTML <a> 或 Markdown [title](url)），用于区分正文句内引用"""
    s_no_hash = re.sub(r"^#{1,3}\s*", "", line.strip()).strip()
    return bool(
        re.match(
            r"^(?:<(?:b|strong)\b[^>]*>\s*)?<a\s+[^>]*href=[\"'][^\"']+[\"'][^>]*>.*?</a>(?:\s*</(?:b|strong)>)?$",
            s_no_hash,
            re.I,
        )
        or re.match(
            r"^(?:\*\*)?\[[^\]\n]+\]\((?:\([^()\s]*\)|[^()\s])+\)(?:\*\*)?$",
            s_no_hash,
        )
    )


def _is_news_headline_line(line: str) -> bool:
    """判断在 AI 产业日报 / 美股要闻 中该行是否为单条新闻标题行"""
    s = line.strip()
    if not s:
        return False
    s_no_hash = re.sub(r"^#{1,3}\s*", "", s).strip()
    plain = re.sub(r"^#{1,3}\s*", "", _strip_tags_plain(s)).strip()
    if not plain or len(plain) > 120:
        return False
    if plain.startswith(("⭐", "📄", "⚡", "📋", "📊", "🌅", "🇨🇳", "🇭🇰", "🐙", "🔥", "❄️", "🔭", "🎯", "📎", "🚀", "📰", "🧭", "📐", "📈", "💰", "🌡", "🪙", "🇺🇸", "🌍", "·", "•", "-", "30秒速览")):
        return False
    if s.startswith("## "):
        return True
    if _is_standalone_headline_link(s):
        return True
    if s_no_hash.startswith(("<b>", "**")) and ("。" not in plain or len(plain) <= 45):
        return True
    return False


def _is_x_builder_topic_heading(line: str, plain_no_b: str) -> bool:
    """判断在 X 热点播报 / AI Builder 中该行是否为分节标题（转为 heading_2）"""
    plain_clean = re.sub(r"^#{1,3}\s*", "", plain_no_b).strip()
    if not re.match(r"^\d+\.\s+\S", plain_clean) or len(plain_clean) > 100:
        return False
    if line.startswith(("# ", "## ", "### ")):
        return True
    line_no_a = re.sub(r"</?a\b[^>]*>", "", line, flags=re.I).strip()
    stripped_bold = re.sub(r"<\s*(?:b|strong)\b[^>]*>.*?</\s*(?:b|strong)\s*>", "", line_no_a, flags=re.I)
    stripped_bold = re.sub(r"\*\*[^*]+\*\*", "", stripped_bold).strip()
    return stripped_bold == ""


def _format_cn_hk_heading(plain_title: str) -> str:
    """将 A股/港股 标题中的 2026年11月13日 统一规范为 2026-11-13"""
    def _repl(m):
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return re.sub(r"(\d{4})年(\d{1,2})月(\d{1,2})日", _repl, plain_title)


def _p_block(text: str, *, bold: bool = False, italic: bool = False, color: str | None = None) -> dict:
    return {
        "object": "block",
        "type": "paragraph",
        "paragraph": {"rich_text": rich_text(text, default_bold=bold, default_italic=italic, default_color=color)},
    }


def _h_block(level: int, text: str) -> dict:
    htype = f"heading_{level}"
    return {"object": "block", "type": htype, htype: {"rich_text": rich_text(text)}}


def _bullet_block(text: str, *, bold: bool = False, color: str | None = None) -> dict:
    return {
        "object": "block",
        "type": "bulleted_list_item",
        "bulleted_list_item": {"rich_text": rich_text(text, default_bold=bold, default_color=color)},
    }


def _num_block(text: str, *, color: str | None = None) -> dict:
    return {
        "object": "block",
        "type": "numbered_list_item",
        "numbered_list_item": {"rich_text": rich_text(text, default_color=color)},
    }


def _toggle_block(title_text: str, children: list[dict]) -> dict:
    return {
        "object": "block",
        "type": "toggle",
        "toggle": {
            "rich_text": rich_text(title_text, default_bold=True),
            "children": children,
        },
    }


def _embed_block(url: str) -> dict:
    return {
        "object": "block",
        "type": "embed",
        "embed": {"url": url},
    }


def _table_block(rows: list[tuple[str, str]], headers: tuple[str, str] = ("指标维度", "核心数据与状态")) -> dict:
    def _row(col1: str, col2: str, *, bold_col1: bool = True, bold_all: bool = False) -> dict:
        return {
            "object": "block",
            "type": "table_row",
            "table_row": {
                "cells": [
                    rich_text(col1, default_bold=(bold_col1 or bold_all)),
                    rich_text(col2, default_bold=bold_all),
                ]
            },
        }

    children = [_row(headers[0], headers[1], bold_all=True)] + [_row(k, v) for k, v in rows]
    return {
        "object": "block",
        "type": "table",
        "table": {
            "table_width": 2,
            "has_column_header": True,
            "has_row_header": False,
            "children": children,
        },
    }


def _count_blocks_recursive(block: dict) -> int:
    btype = block.get("type", "")
    inner = block.get(btype) or {}
    children = inner.get("children") or []
    return 1 + sum(_count_blocks_recursive(c) for c in children)


def _chunk_blocks_by_count(blocks: list[dict], max_blocks: int = 80) -> list[list[dict]]:
    chunks: list[list[dict]] = []
    cur: list[dict] = []
    cur_count = 0
    for b in blocks:
        b_count = _count_blocks_recursive(b)
        if cur and (cur_count + b_count > max_blocks or len(cur) >= max_blocks):
            chunks.append(cur)
            cur = []
            cur_count = 0
        cur.append(b)
        cur_count += b_count
    if cur:
        chunks.append(cur)
    return chunks


_METRIC_PREFIXES = ("📈", "📊", "🧭", "📐", "💰", "💸", "🌍", "🌡", "🪙", "🇺🇸", "📅", "🍰")
_WATCHLIST_LAYER_PREFIXES = ("⚡", "🔌", "🏗", "🧠", "🚀")
_META_PREFIXES = ("Original:", "Original：", "🏷️", "🛠️", "原帖：", "原帖:", "原推：", "原推:", "素材：")


def _format_us_metric_cell(label: str, val: str) -> str:
    """将美股核心指标表右侧单元格按语义拆分为圆圈序号 ①②③ 分区，增强 Notion 表格阅读区分度。"""
    if not val or "\n" in val:
        return val

    def _num_lines(raw_lines: list[str]) -> str:
        return "\n".join(f"{_circled(i)} {ln}" for i, ln in enumerate(raw_lines))

    # 1. 核心指数：股指 / 利率与波动 / 大宗与加密
    if "核心指数" in label:
        parts = [p.strip() for p in val.split("|") if p.strip()]
        if len(parts) >= 5:
            eq, rates, comm, other = [], [], [], []
            for p in parts:
                if any(k in p for k in ("标普", "纳斯达克", "纳指", "道琼斯", "道指", "罗素")):
                    eq.append(p)
                elif any(k in p for k in ("美债", "VIX", "美元")):
                    rates.append(p)
                elif any(k in p for k in ("伦敦金", "黄金", "原油", "布油", "比特币", "BTC")):
                    comm.append(p)
                else:
                    other.append(p)
            lines = []
            if eq:
                lines.append("股指：" + " | ".join(eq))
            if rates:
                lines.append("利率与波动：" + " | ".join(rates))
            if comm:
                lines.append("大宗与加密：" + " | ".join(comm))
            if other:
                lines.append("其他：" + " | ".join(other))
            if len(lines) >= 2:
                return _num_lines(lines)

    # 2. 市场宽度：广度 / 风向标 / 诊断
    if "市场宽度" in label:
        head, diag = (val.split("—", 1)[0].strip(), val.split("—", 1)[1].strip()) if "—" in val else (val, "")
        parts = [p.strip() for p in head.split("|") if p.strip()]
        breadth, gauges = [], []
        for p in parts:
            if any(k in p for k in ("半导体", "SOX", "高收益债", "HYG")):
                gauges.append(p)
            else:
                breadth.append(p)
        lines = []
        if breadth:
            lines.append("广度：" + " | ".join(breadth))
        if gauges:
            lines.append("风向标：" + " | ".join(gauges))
        if diag:
            lines.append("诊断：" + diag)
        if len(lines) >= 2:
            return _num_lines(lines)

    # 3. 趋势与乖离率：逐指数分行 + 短线判定
    if "趋势" in label or "乖离率" in label:
        head, judge = (val.split("—", 1)[0].strip(), val.split("—", 1)[1].strip()) if "—" in val else (val, "")
        parts = [p.strip() for p in head.split("|") if p.strip()]
        lines = []
        for p in parts:
            m_idx = re.match(r"^(标普\s*500|纳斯达克|纳指|道琼斯|道指)\s+(.+)$", p)
            if m_idx:
                lines.append(f"{m_idx.group(1)}：{m_idx.group(2)}")
            else:
                lines.append(p)
        if judge:
            lines.append(f"短线判定：{judge}")
        if len(lines) >= 2:
            return _num_lines(lines)

    # 4. 加密货币：情绪与结构 / 主流币（全部币种统一合并为一行）
    if "加密货币" in label:
        parts = [p.strip() for p in val.split("|") if p.strip()]
        mood, coins = [], []
        for p in parts:
            if any(k in p for k in ("恐贪", "总市值", "市占率")):
                mood.append(p)
            else:
                coins.append(p)
        lines = []
        if mood:
            lines.append("情绪与结构：" + " | ".join(mood))
        if coins:
            lines.append("主流币 (24h)：" + " | ".join(coins))
        if len(lines) >= 2:
            return _num_lines(lines)

    # 5. Financial Events：按日期从近到远排序、合并同日重复日期，且时间更早的分区排在前面
    if "Financial Events" in label or "事件" in label:
        earn_m = re.search(r"财报预告[:：]\s*(.+?)(?=\s*\|\s*宏观日程[:：]|$)", val)
        macro_m = re.search(r"宏观日程[:：]\s*(.+?)(?=\s*\|\s*财报预告[:：]|$)", val)
        blocks_dated: list[tuple[tuple[int, int], str]] = []
        if macro_m:
            m_items = [x.strip() for x in macro_m.group(1).split("|") if x.strip()]
            m_str = _merge_same_day_events(m_items, inner_sep="、", group_sep=" | ")
            blocks_dated.append((_earliest_mmdd(m_str), f"宏观日程：{m_str}"))
        if earn_m:
            e_items = [x.strip() for x in earn_m.group(1).split("|") if x.strip()]
            e_str = _merge_same_day_events(e_items, inner_sep="、", group_sep=" | ")
            blocks_dated.append((_earliest_mmdd(e_str), f"财报预告：{e_str}"))
        if len(blocks_dated) >= 2:
            blocks_dated.sort(key=lambda x: x[0])
            return _num_lines([txt for _, txt in blocks_dated])
        if len(blocks_dated) == 1:
            return f"① {blocks_dated[0][1]}"

    # 6. AI 自选股：整体表现（加粗最强/最弱代码）/ 分层均幅
    if "自选股" in label:
        parts = [p.strip() for p in val.split("|") if p.strip()]
        if len(parts) >= 3:
            hl = re.sub(r"(最强|最弱)\s+([A-Z]{1,5}(?:\.[A-Z])?)", r"\1 <b>\2</b>", parts[2])
            return _num_lines([f"整体表现：{parts[0]} | {hl}", f"分层均幅：{parts[1]}"])
        if len(parts) == 2:
            return _num_lines([parts[0], parts[1]])

    return val


def _split_watchlist_row(raw_line: str) -> tuple[str, str] | None:
    """将美股独立自选股区块中的单层行（⚡/🔌/🏗️/🧠/🚀）拆分为 2 列 (层级, 成分股收盘表现)"""
    plain = _strip_tags_plain(raw_line).lstrip("·•-* ").strip()
    if not plain or len(plain) < 6 or not plain.startswith(_WATCHLIST_LAYER_PREFIXES):
        return None
    m_plain = re.match(r"^(.{2,32}?)\s*(?:：|(?<!\d):(?!\d))\s*(.+)$", plain)
    if not m_plain:
        return None
    return m_plain.group(1).strip(), m_plain.group(2).strip()


def _is_metadata_line(plain_clean: str) -> bool:
    return plain_clean.startswith(_META_PREFIXES) or _is_star_line(plain_clean)


def _split_metric_row(raw_line: str) -> tuple[str, str] | None:
    """将金融报告顶部单行指标拆分为表格 (指标维度, 核心数据) 两列"""
    plain = _strip_tags_plain(raw_line).lstrip("·•-* ").strip()
    if not plain or len(plain) < 6 or not plain.startswith(_METRIC_PREFIXES):
        return None
    if re.match(r"^(?:📊\s*美股收盘|🇨🇳\s*A股|🇭🇰\s*港股)", plain):
        return None

    m_plain = re.match(r"^(.{2,28}?)\s*(?:：|(?<!\d):(?!\d)|—|–)\s*(.+)$", plain)
    if not m_plain:
        return None

    if "<a " not in raw_line.lower():
        return m_plain.group(1).strip(), m_plain.group(2).strip()

    m_raw = re.match(
        r"^(?:[-·•]\s*)?(?:<(?:b|strong)\b[^>]*>)?([^：<>\n]{2,28}?)(?:</(?:b|strong)>)?\s*(?:：|(?<!https)(?<!http)(?<!\d):(?!\d)|—|–)\s*(?:</(?:b|strong)>)?\s*(.+)$",
        raw_line.strip(),
        re.I,
    )
    if m_raw:
        k = _strip_tags_plain(m_raw.group(1)).strip()
        v = re.sub(r"^\s*</(?:b|strong)>\s*", "", m_raw.group(2)).strip()
        if k and v:
            return k, v
    return m_plain.group(1).strip(), m_plain.group(2).strip()


def _extract_numbered_body(line: str) -> str | None:
    """识别 1. / 1\\. 编号列表项，返回剥离编号后的富文本内容（保留内嵌 HTML 标签）"""
    m = re.match(r"^(\d{1,2})\\?\.\s+(.+)$", line)
    if m:
        return m.group(2).strip()
    m_b = re.match(r"^<(b|strong)>(\d{1,2})\\?\.\s+(.+)$", line, re.I)
    if m_b:
        return f"<{m_b.group(1)}>{m_b.group(3).strip()}"
    return None


def convert_to_blocks(
    content: str,
    source: str = "",
    *,
    summary_override: str | None = None,
    us_embed_url: str | None = None,
) -> list[dict]:
    """将稿件转换为 Notion Blocks 结构，并按信源优化指标表格、折叠区与目录层级（正文不使用 Callout 框）"""
    blocks: list[dict] = []
    lines = content.splitlines()
    is_gh = ("GitHub & HN" in source)
    is_ai_daily = ("AI 产业日报" in source)
    is_us = ("美股" in source)
    is_cn_hk = ("A股与港股" in source)
    is_x_or_builder = ("X 热点" in source or "AI Builder" in source)
    resolved_us_embed = (get_us_embed_url() if us_embed_url is None else (us_embed_url or "")) if is_us else ""
    inserted_us_embed = False
    in_us_news = False
    in_details = False
    in_sector_section = False
    seen_gh_content = False
    pending_star_line: str | None = None
    has_x_builder_items = is_x_or_builder and any(
        _is_x_builder_topic_heading(
            l.strip(),
            html.unescape(re.sub(r"</?(?:b|strong)>", "", l.strip(), flags=re.I)).strip(),
        )
        for l in lines
        if l.strip()
    )
    seen_first_topic = False

    def _is_h1_line(raw_l: str, p_no_b: str) -> bool:
        if raw_l.startswith("# "):
            return True
        return bool(
            re.match(
                r"^(?:🌅\s*|☀️\s*)?(?:📊\s*美股收盘|📋\s*(?:AI\s*产业日报|美股要闻)|🐙\s*GitHub|🔥\s*X(?:\s*&\s*Reddit)?\s*(?:热点|社区热议)|🛠️?\s*AI\s*Builder|🇨🇳\s*A股(?:开盘前瞻|早盘总结|午间复盘|收盘复盘)|🇭🇰\s*港股(?:开盘前瞻|早盘总结|午间复盘|收盘复盘))",
                p_no_b,
            )
            and ((raw_l.startswith("<b>") and raw_l.endswith("</b>")) or raw_l.startswith(("📋", "📊", "🌅", "☀️", "🇨🇳", "🇭🇰")))
        )

    i = 0
    while i < len(lines):
        raw = lines[i]
        line = raw.strip()
        if not line:
            i += 1
            continue

        plain_no_b = html.unescape(re.sub(r"</?(?:b|strong)>", "", line, flags=re.I)).strip()
        plain_clean = re.sub(r"^#{1,3}\s*", "", plain_no_b).strip()

        # 跟踪美股要闻段落边界（美股收盘消息①之后、--- 之后）
        if is_us and re.match(r"^(?:#\s*)?(?:<b>)?📋\s*美股要闻", plain_no_b):
            in_us_news = True
            in_details = False
            in_sector_section = False

        # A股与港股：删掉冗余的 `## 📈 A股市场（...）` / `## 🇭🇰 港股市场（...）` 外层标题
        if is_cn_hk and re.match(r"^(?:#{1,3}\s*)?(?:📈|📉|🇭🇰|🇨🇳|\s)*(?:A股|港股)市场（.*?）\s*$", plain_no_b):
            i += 1
            continue

        # GitHub & HN：删掉 Notion 正文顶部的重复大标题（页面标题已展示）
        if is_gh and not seen_gh_content and re.match(r"^🐙\s*GitHub\s+(?:Trending|Weekly)", plain_clean, re.I):
            i += 1
            continue

        # AI 产业日报：删掉 Notion 正文顶部的 `AI 产业日报 · YYYY-MM-DD` 一级标题，正文直接从首条新闻开始
        if is_ai_daily and re.match(r"^📋\s*AI\s*产业日报", plain_clean):
            i += 1
            continue

        # X 热点播报 & AI Builder：删掉 Notion 正文顶部大标题与开篇导语（页面顶部 30秒速览 属性已完整呈现）
        if is_x_or_builder:
            if re.match(r"^(?:🔥\s*X(?:\s*&\s*Reddit)?\s*(?:热点|社区热议)|🛠\uFE0F?\s*AI\s*Builder)", plain_clean):
                i += 1
                continue
            is_topic_h2 = _is_x_builder_topic_heading(line, plain_no_b)
            is_reddit_h3 = (
                bool(re.match(r"^\[r/[A-Za-z0-9_]+\]\s+\S", plain_clean))
                and len(plain_clean) <= 100
                and (
                    line.startswith(("# ", "## ", "### "))
                    or (line.startswith("<b>") and line.endswith("</b>") and line.count("<b>") == 1)
                )
            )
            if is_topic_h2 or is_reddit_h3 or plain_clean.startswith(("📎", "🗣️")) or line.startswith(("---", "━━━")):
                seen_first_topic = True
            elif has_x_builder_items and not seen_first_topic:
                i += 1
                continue
            if is_topic_h2:
                title_for_h2 = re.sub(r"^#{1,3}\s*", "", line).strip()
                blocks.append(_h_block(2, title_for_h2))
                i += 1
                continue
            if is_reddit_h3:
                title_for_h3 = re.sub(r"^#{1,3}\s*", "", line).strip()
                blocks.append(_h_block(3, title_for_h3))
                i += 1
                continue

        # 分割线
        if line.startswith("---") or line.startswith("━━━"):
            if pending_star_line:
                blocks.append(_p_block(pending_star_line, color="gray"))
                pending_star_line = None
            in_details = False
            in_sector_section = False
            seen_gh_content = True
            blocks.append({"object": "block", "type": "divider", "divider": {}})
            i += 1
            continue

        # 30 秒速览处理：页面顶部 30秒速览 属性已完整渲染，正文中删去重复的 30秒速览 与紧邻要点
        if "30秒速览" in plain_no_b and (line.startswith(("<b>", "#", ">", "⚡", "30秒速览", "**")) or re.match(r"^#{1,3}\s*⚡?\s*30秒速览", line)):
            i += 1
            while i < len(lines) and (
                lines[i].strip().startswith(("-", "•", ">"))
                or not lines[i].strip()
            ):
                i += 1
            continue

        # 金融市场指标多行合并为 2 列 Notion Table
        if is_cn_hk or (is_us and not in_us_news):
            first_row = _split_metric_row(line)
            if first_row:
                metric_rows = [first_row]
                j = i + 1
                while j < len(lines):
                    nxt = lines[j].strip()
                    if not nxt:
                        k = j + 1
                        while k < len(lines) and not lines[k].strip():
                            k += 1
                        if k < len(lines) and _split_metric_row(lines[k].strip()):
                            j = k
                            continue
                        break
                    nxt_row = _split_metric_row(nxt)
                    if not nxt_row:
                        break
                    metric_rows.append(nxt_row)
                    j += 1
                if len(metric_rows) >= 2:
                    if is_us and not in_us_news:
                        metric_rows = [(k, _format_us_metric_cell(k, v)) for k, v in metric_rows]
                    blocks.append(_table_block(metric_rows))
                    i = j
                    continue

        # 美股独立自选股追踪区块：标题 + 紧跟的五层行自动合并为独立 2 列 Notion Table
        if is_us and not in_us_news and plain_clean.startswith("🍰") and ("自选股明细" in plain_clean or "自选股追踪" in plain_clean) and not _split_metric_row(line):
            h2_txt = plain_no_b if (line.startswith("<b>") and line.endswith("</b>") and line.count("<b>") == 1) else re.sub(r"^#{1,3}\s*", "", line).strip()
            blocks.append(_h_block(2, h2_txt))
            wl_rows: list[tuple[str, str]] = []
            j = i + 1
            while j < len(lines):
                nxt = lines[j].strip()
                if not nxt:
                    k = j + 1
                    while k < len(lines) and not lines[k].strip():
                        k += 1
                    if k < len(lines) and _split_watchlist_row(lines[k].strip()):
                        j = k
                        continue
                    break
                w_row = _split_watchlist_row(nxt)
                if not w_row:
                    break
                wl_rows.append(w_row)
                j += 1
            if len(wl_rows) >= 2:
                wl_rows = [(k, _bold_watchlist_tickers(v)) for k, v in wl_rows]
                blocks.append(_table_block(wl_rows, headers=("AI 自选股层级", "成分股收盘表现")))
                i = j
                continue
            i += 1
            continue

        # 金融市场 `🎯 决策仪表盘` / `🎯 早盘决策仪表盘` / `🎯 投资启示`：直接作为标准 heading_2 展示，不嵌套 Callout
        if (is_cn_hk or (is_us and not in_us_news)) and plain_clean.startswith("🎯") and not re.match(r"^(?:-\s+|[·•●▪]\s*)", line):
            in_sector_section = False
            h2_txt = plain_no_b if (line.startswith("<b>") and line.endswith("</b>") and line.count("<b>") == 1) else re.sub(r"^#{1,3}\s*", "", line).strip()
            blocks.append(_h_block(2, h2_txt))
            i += 1
            continue

        # GitHub & HN：每周周榜 `🔥 持续霸榜` 收拢为可折叠 Toggle 区块
        if is_gh and plain_clean.startswith("🔥 持续霸榜"):
            seen_gh_content = True
            toggle_children: list[dict] = []
            repo_count = 0
            j = i + 1
            while j < len(lines):
                nxt = lines[j].strip()
                if not nxt:
                    j += 1
                    continue
                nxt_no_b = html.unescape(re.sub(r"</?(?:b|strong)>", "", nxt, flags=re.I)).strip()
                nxt_clean = re.sub(r"^#{1,3}\s*", "", nxt_no_b).strip()
                if nxt.startswith(("---", "━━━", "# ", "## ")) or any(
                    nxt_clean.startswith(p) for p in ("🚀", "🔥", "📰", "⚡", "📊")
                ):
                    break
                nxt_no_hash = re.sub(r"^#{1,3}\s*", "", nxt).strip()
                if re.match(r"^(?:<b>|\*\*)?\d+\.(?:</b>|\*\*)?\s*(?:<a\b|\[|<b\b|\*\*)", nxt_no_hash, re.I):
                    repo_count += 1
                    toggle_children.append(_h_block(3, nxt_no_hash))
                else:
                    b_m = re.match(r"^(?:-\s+|[·•●▪]\s*)", nxt)
                    if b_m:
                        item_txt = nxt[b_m.end():].strip()
                        item_plain = _strip_tags_plain(item_txt)
                        toggle_children.append(_bullet_block(item_txt, color=("gray" if _is_metadata_line(item_plain) else None)))
                    else:
                        toggle_children.append(_p_block(nxt, color=("gray" if _is_metadata_line(nxt_clean) else None)))
                j += 1

            toggle_title = f"🔥 持续霸榜（本周反复上榜 · 共 {repo_count} 项，点击展开）" if repo_count else plain_clean
            blocks.append(_toggle_block(toggle_title, toggle_children[:60]))
            for extra in toggle_children[60:]:
                blocks.append(extra)
            i = j
            continue

        # GitHub & HN：大板块用二级标题（heading_2），每条仓库/HN 标题用三级标题（heading_3）
        if is_gh:
            seen_gh_content = True
            if any(plain_clean.startswith(p) for p in ("🚀 一、", "🔥 一、", "🔥 二、", "📰 二、", "🔥 持续霸榜", "🚀 首次上榜")):
                blocks.append(_h_block(2, plain_clean))
                i += 1
                continue
            line_no_hash = re.sub(r"^#{1,3}\s*", "", line).strip()
            if re.match(r"^(?:<b>|\*\*)?\d+\.(?:</b>|\*\*)?\s*(?:<a\b|\[|<b\b|\*\*)", line_no_hash, re.I):
                blocks.append(_h_block(3, line_no_hash))
                i += 1
                continue

        # AI 产业日报 & 美股要闻：新闻标题（heading_2）→ 灰色星级与 tag → 合并行内 `📄 The Details：`
        if is_ai_daily or (is_us and in_us_news):
            if _is_star_line(line):
                in_details = False
                if pending_star_line:
                    blocks.append(_p_block(pending_star_line, color="gray"))
                pending_star_line = line
                i += 1
                continue

            if (not in_details or line.startswith("## ") or _is_standalone_headline_link(line)) and _is_news_headline_line(line):
                in_details = False
                title_for_h2 = re.sub(r"^#{1,3}\s*", "", line).strip()
                blocks.append(_h_block(2, title_for_h2))
                if pending_star_line:
                    blocks.append(_p_block(pending_star_line, color="gray"))
                    pending_star_line = None
                i += 1
                continue

            if pending_star_line:
                blocks.append(_p_block(pending_star_line, color="gray"))
                pending_star_line = None

            if re.match(r"^(?:<b>|\*\*)?📄\s*The\s*Details[:：]?(?:</b>|\*\*)?$", plain_clean, re.I):
                in_details = True
                k = i + 1
                while k < len(lines) and not lines[k].strip():
                    k += 1
                if k < len(lines):
                    nxt_raw = lines[k].strip()
                    nxt_p_no_b = html.unescape(re.sub(r"</?(?:b|strong)>", "", nxt_raw, flags=re.I)).strip()
                    nxt_clean = re.sub(r"^#{1,3}\s*", "", nxt_p_no_b).strip()
                    if (
                        not nxt_raw.startswith(("#", "---", "━━━", "-", "•", "·", "●", "▪"))
                        and not _is_star_line(nxt_raw)
                        and not _is_news_headline_line(nxt_raw)
                        and not _is_metadata_line(nxt_clean)
                        and not nxt_clean.startswith(("📄", "⚡", "📋", "📊", "🎯"))
                    ):
                        blocks.append(_p_block(f"<b>📄 The Details：</b>{nxt_raw}"))
                        i = k + 1
                        continue
                blocks.append(_p_block("<b>📄 The Details：</b>"))
                i += 1
                continue

            if plain_clean.startswith("📄"):
                in_details = True

        # Markdown 标题处理（含从 Notion 导出的 #/##/###）
        if line.startswith("### "):
            blocks.append(_h_block(3, line[4:].strip()))
            i += 1
            continue
        if line.startswith("## "):
            h2_txt = line[3:].strip()
            h2_plain = _strip_tags_plain(h2_txt)
            if is_cn_hk or (is_us and not in_us_news):
                if h2_plain.startswith(("🔥", "❄️")):
                    in_sector_section = True
                elif h2_plain.startswith(("🔭", "🎯", "⚡")):
                    in_sector_section = False
            blocks.append(_h_block(2, h2_txt))
            i += 1
            continue
        if line.startswith("# "):
            in_sector_section = False
            h1_text = line[2:].strip()
            if is_cn_hk:
                h1_text = _format_cn_hk_heading(h1_text)
            blocks.append(_h_block(1, h1_text))
            if is_us and not in_us_news and resolved_us_embed and not inserted_us_embed and "美股收盘" in h1_text:
                blocks.append(_toggle_block("📈 实时交互行情看板（点击展开）", [_embed_block(resolved_us_embed)]))
                inserted_us_embed = True
            i += 1
            continue

        # HTML 顶层主标题（日报/早报/收盘复盘总标题 → heading_1）
        if _is_h1_line(line, plain_no_b):
            in_sector_section = False
            h1_text = _format_cn_hk_heading(plain_no_b) if is_cn_hk else plain_no_b
            blocks.append(_h_block(1, h1_text))
            if is_us and not in_us_news and resolved_us_embed and not inserted_us_embed and "美股收盘" in h1_text:
                blocks.append(_toggle_block("📈 实时交互行情看板（点击展开）", [_embed_block(resolved_us_embed)]))
                inserted_us_embed = True
            i += 1
            continue

        # HTML 风格板块标题（🚀/🔥/❄️/⚡/🔭/🎯/📰/📎/🗣️/一、/二、/三、 → heading_2）
        _H2_PREFIXES = ("🚀", "🔥", "❄️", "⚡", "🔭", "🎯", "📰", "📎", "🗣️", "一、", "二、", "三、", "四、")
        if line.startswith("<b>") and line.endswith("</b>") and line.count("<b>") == 1 and any(plain_no_b.startswith(p) for p in _H2_PREFIXES):
            if is_cn_hk or (is_us and not in_us_news):
                if plain_no_b.startswith(("🔥", "❄️")):
                    in_sector_section = True
                elif plain_no_b.startswith(("🔭", "🎯", "⚡")):
                    in_sector_section = False
            blocks.append(_h_block(2, plain_no_b))
            i += 1
            continue

        # 领涨 / 领跌 Top 3 板块条目提升为 heading_3，方便目录锚点定位
        if in_sector_section and (is_cn_hk or (is_us and not in_us_news)) and re.match(r"^\d+\\?\.\s+\S", plain_clean):
            blocks.append(_h_block(3, re.sub(r"^#{1,3}\s*", "", line).strip()))
            i += 1
            continue

        # 列表条目（兼容 - / · / • / ● / ▪）
        bullet_m = re.match(r"^(?:-\s+|[·•●▪]\s*)", line)
        if bullet_m:
            item_txt = line[bullet_m.end():].strip()
            item_plain = _strip_tags_plain(item_txt)
            blocks.append(_bullet_block(item_txt, color=("gray" if _is_metadata_line(item_plain) else None)))
            i += 1
            continue

        # 原生编号列表（消除 Notion Markdown `1\. ` 转义问题）
        num_txt = _extract_numbered_body(line)
        if num_txt is not None:
            num_plain = _strip_tags_plain(num_txt)
            blocks.append(_num_block(num_txt, color=("gray" if _is_metadata_line(num_plain) else None)))
            i += 1
            continue

        # 普通段落（灰色弱化纯元数据行）
        blocks.append(_p_block(line, color=("gray" if _is_metadata_line(plain_clean) else None)))
        i += 1

    if pending_star_line:
        blocks.append(_p_block(pending_star_line, color="gray"))

    # 清理首尾与连续重复 divider，保留跨消息或参考资料前的有效分割线
    cleaned_body: list[dict] = []
    for b in blocks:
        if b["type"] == "divider":
            if not cleaned_body or cleaned_body[-1]["type"] in ("divider", "heading_1"):
                continue
        cleaned_body.append(b)
    while cleaned_body and cleaned_body[-1]["type"] == "divider":
        cleaned_body.pop()

    return cleaned_body


NOTION_MARKDOWN_VERSION = "2025-09-03"
TABLE_PAGE_WIDTH_PX = 708


def _display_units(text: str) -> int:
    plain = re.sub(r"<[^>]+>", "", text)
    plain = re.sub(r"[*`\\]", "", plain).strip()
    return sum(2 if ord(ch) > 127 else 1 for ch in plain)


def _compute_table_col_widths(table_html: str) -> list[int]:
    """为 Notion table 块计算总宽 708px 的黄金列宽（双列表格如指标/状态采用 196/512）。"""
    rows = re.findall(r"<tr\b[^>]*>(.*?)</tr>", table_html, flags=re.S)
    if not rows:
        return []
    first_cells = re.findall(r"<td\b[^>]*>(.*?)</td>", rows[0], flags=re.S)
    ncols = len(first_cells)
    if ncols <= 0:
        return []
    if ncols == 1:
        return [TABLE_PAGE_WIDTH_PX]
    if ncols == 2:
        col0_max = 0
        col1_max = 0
        for row in rows:
            cells = re.findall(r"<td\b[^>]*>(.*?)</td>", row, flags=re.S)
            if len(cells) >= 2:
                col0_max = max(col0_max, _display_units(cells[0]))
                col1_max = max(col1_max, _display_units(cells[1]))
        if col0_max <= 30 and col1_max > col0_max:
            return [196, TABLE_PAGE_WIDTH_PX - 196]
        return [TABLE_PAGE_WIDTH_PX // 2, TABLE_PAGE_WIDTH_PX - TABLE_PAGE_WIDTH_PX // 2]
    base = TABLE_PAGE_WIDTH_PX // ncols
    widths = [base] * ncols
    widths[-1] += TABLE_PAGE_WIDTH_PX - base * ncols
    return widths


def _build_fit_width_updates(markdown: str) -> list[dict]:
    """扫描 Notion Markdown v1 表格，构造 fit-page-width='true' + <colgroup> 的 PATCH content_updates。"""
    updates: list[dict] = []
    seen_old: set[str] = set()
    pattern = re.compile(
        r"(<table\b([^>]*)>)(?:\s*<colgroup>.*?</colgroup>)?(.*?</table>)",
        flags=re.S,
    )
    for m in pattern.finditer(markdown):
        old_table = m.group(0)
        raw_attrs = m.group(2) or ""
        body_and_close = m.group(3)
        widths = _compute_table_col_widths(old_table)
        if not widths:
            continue
        attrs = ['fit-page-width="true"']
        if 'header-row="true"' in raw_attrs:
            attrs.append('header-row="true"')
        if 'header-column="true"' in raw_attrs:
            attrs.append('header-column="true"')
        cols_html = "\n".join(f'<col width="{w}">' for w in widths)
        new_table = f'<table {" ".join(attrs)}>\n<colgroup>\n{cols_html}\n</colgroup>{body_and_close}'
        if new_table != old_table and old_table not in seen_old:
            seen_old.add(old_table)
            updates.append({"old_str": old_table, "new_str": new_table})
    return updates


def archive_to_notion(
    report_text: str,
    source: str,
    date_str: str | None = None,
    report_type: str = "日报",
    title_override: str | None = None,
    database_id: str | None = None,
) -> tuple[bool, str]:
    """
    将成稿归档上传到 Notion 数据库。
    返回 (成功标志, 页面URL或错误信息)
    """
    api_key = get_notion_api_key()
    if not api_key:
        return False, "未找到有效的 NOTION_ARCHIVE_API_KEY（请检查环境变量或 .env）"

    resolved_db_id = (database_id if database_id is not None else get_notion_database_id()).strip()
    if not resolved_db_id:
        return False, "未找到有效的 NOTION_ARCHIVE_DATABASE_ID（请检查环境变量或 .env）"
    database_id = resolved_db_id

    if not date_str:
        date_str = datetime.now().strftime("%Y-%m-%d")

    # 计算周次 (例如 2026-W46)
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    year, week, _ = dt.isocalendar()
    week_str = f"{year}-W{week:02d}"

    # 自动推导清晰标题、图标与 30 秒速览（金融市场报告含前置信号灯）
    icon_map = {
        "🐙 GitHub & HN": "🐙",
        "🗽 美股与加密": "🗽",
        "📈 A股与港股": "📈",
        "🤖 AI 产业日报": "🤖",
        "🐦 X 热点播报": "🐦",
        "🛠️ AI Builder": "🛠️",
    }
    icon = icon_map.get(source, "📑")
    title = title_override or build_clean_title(source, date_str, report_type=report_type)

    summary = extract_summary(report_text, source=source, report_type=report_type)
    blocks = convert_to_blocks(report_text, source=source)
    block_chunks = _chunk_blocks_by_count(blocks, max_blocks=80)
    first_chunk = block_chunks[0] if block_chunks else []
    rest_chunks = block_chunks[1:] if len(block_chunks) > 1 else []

    props = {
        "标题": {"title": [{"text": {"content": title}}]},
        "日期": {"date": {"start": date_str}},
        "周次": {"select": {"name": week_str}},
        "信源": {"select": {"name": source}},
        "类型": {"select": {"name": report_type}},
        "30秒速览": {"rich_text": rich_text(summary)},
    }

    # 构造请求体 (首次写入第 1 个安全递归切片，其余在后续追加)
    payload = {
        "parent": {"database_id": database_id},
        "icon": {"type": "emoji", "emoji": icon},
        "properties": props,
        "children": first_chunk,
    }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Notion-Version": NOTION_VERSION,
    }

    try:
        # 使用 bot_utils.resolve_proxy 探测可用代理（兼容系统 Python LibreSSL 直连偶发 EOF 问题）
        shared_dir = str(Path(__file__).resolve().parent)
        if shared_dir not in sys.path:
            sys.path.insert(0, shared_dir)
        from bot_utils import resolve_proxy

        configured_proxy = os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY") or "http://127.0.0.1:1082"
        resolved_proxy, _ = resolve_proxy(configured_proxy)

        def _make_session(proxy_url: str | None) -> requests.Session:
            s = requests.Session()
            s.trust_env = False
            s.proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else {}
            return s

        class _HeaderDict(dict):
            def __init__(self, mapping=None):
                super().__init__({str(k).lower(): str(v) for k, v in (mapping or {}).items()})

            def get(self, key, default=None):
                return super().get(str(key).lower(), default)

            def __contains__(self, key):
                return super().__contains__(str(key).lower())

            def __getitem__(self, key):
                return super().__getitem__(str(key).lower())

        class _CurlResp:
            def __init__(self, status_code: int, text: str, headers: dict[str, str] | None = None):
                self.status_code = status_code
                self.text = text
                self.headers = _HeaderDict(headers)

            def json(self):
                return json.loads(self.text)

        def _parse_curl_headers(raw_hdrs: str) -> dict[str, str]:
            parsed: dict[str, str] = {}
            for raw_line in raw_hdrs.splitlines():
                line = raw_line.rstrip("\r\n")
                if line.startswith("HTTP/"):
                    parsed = {}
                    continue
                if ":" in line:
                    k, v = line.split(":", 1)
                    parsed[k.lower().strip()] = v.strip()
            return parsed

        def _curl_request(
            method: str,
            url: str,
            body_dict: dict | None = None,
            timeout: int = 35,
            *,
            notion_version: str = NOTION_VERSION,
        ):
            """LibreSSL 2.8.3 等系统 Python 对 Notion TLS 偶发 EOF 时，回退系统 /usr/bin/curl。"""
            # 优先使用已探测到的可用代理；若已选直连或代理临时失败，顺次尝试 1082 与直连
            candidates: list[str | None] = []
            for p in (resolved_proxy if resolved_proxy else None, configured_proxy, "http://127.0.0.1:1082", None):
                if p == "":
                    p = None
                if p not in candidates:
                    candidates.append(p)

            tmp_path = None
            hdr_tmp_path = None
            try:
                if body_dict is not None:
                    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", delete=False) as tf:
                        json.dump(body_dict, tf, ensure_ascii=False)
                        tmp_path = tf.name
                with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".hdr", delete=False) as hf:
                    hdr_tmp_path = hf.name

                last_exc = None
                for px in candidates:
                    Path(hdr_tmp_path).write_text("", encoding="utf-8")
                    cmd = ["/usr/bin/curl", "-sS", "--max-time", str(timeout)]
                    if px:
                        cmd += ["-x", px]
                    cmd += [
                        "-X", method, url,
                        "-H", f"Notion-Version: {notion_version}",
                        "-K", "-",
                        "-D", hdr_tmp_path,
                        "-w", "\n%{http_code}",
                    ]
                    if tmp_path is not None:
                        cmd += ["-H", "Content-Type: application/json", "--data-binary", f"@{tmp_path}"]
                    r = subprocess.run(
                        cmd,
                        input=f'header = "Authorization: Bearer {api_key}"\n',
                        text=True,
                        capture_output=True,
                    )
                    if r.returncode != 0:
                        last_exc = RuntimeError(f"curl exit {r.returncode}: {r.stderr.strip()}")
                        continue
                    body, _, code_str = r.stdout.rpartition("\n")
                    try:
                        status_code = int(code_str.strip())
                    except ValueError:
                        last_exc = RuntimeError(f"curl bad status output: {r.stdout[-120:]}")
                        continue
                    raw_hdrs = ""
                    if hdr_tmp_path and os.path.exists(hdr_tmp_path):
                        raw_hdrs = Path(hdr_tmp_path).read_text(encoding="utf-8", errors="replace")
                    return _CurlResp(status_code, body, _parse_curl_headers(raw_hdrs))
                raise last_exc or RuntimeError("curl fallback failed")
            finally:
                if tmp_path and os.path.exists(tmp_path):
                    os.unlink(tmp_path)
                if hdr_tmp_path and os.path.exists(hdr_tmp_path):
                    os.unlink(hdr_tmp_path)

        session = _make_session(resolved_proxy if resolved_proxy else None)
        prefer_curl = False

        def _req_with_fallback(method: str, url: str, **kwargs):
            nonlocal session, prefer_curl
            body_dict = kwargs.get("json")
            notion_version = kwargs.pop("notion_version", NOTION_VERSION)
            if prefer_curl:
                return _curl_request(
                    method, url, body_dict, timeout=int(kwargs.get("timeout", 30)), notion_version=notion_version
                )
            try:
                return session.request(method, url, **kwargs)
            except requests.RequestException:
                # 先尝试 requests 切换直连/1082；若仍因 LibreSSL 2.8.3 TLS EOF 失败，永久切至系统 curl
                try:
                    alt = None if session.proxies else "http://127.0.0.1:1082"
                    session = _make_session(alt)
                    return session.request(method, url, **kwargs)
                except requests.RequestException:
                    if os.path.exists("/usr/bin/curl"):
                        prefer_curl = True
                        return _curl_request(
                            method,
                            url,
                            body_dict,
                            timeout=int(kwargs.get("timeout", 30)),
                            notion_version=notion_version,
                        )
                    raise

        def _parse_retry_after(resp_obj, attempt: int) -> float:
            hdrs = getattr(resp_obj, "headers", None)
            ra = None
            if hdrs is not None:
                if hasattr(hdrs, "get"):
                    ra = hdrs.get("Retry-After") or hdrs.get("retry-after")
                if ra is None and hasattr(hdrs, "items"):
                    for k, v in hdrs.items():
                        if str(k).lower() == "retry-after":
                            ra = v
                            break
            if ra is not None:
                try:
                    return min(max(float(str(ra).strip()), 1.0), 30.0)
                except (TypeError, ValueError):
                    pass
            return float(min(2 ** attempt, 16))

        def _request_with_retry(
            method: str,
            url: str,
            *,
            json_body: dict | None = None,
            timeout: int = 30,
            max_attempts: int = 4,
            notion_version: str = NOTION_VERSION,
        ):
            last_err = None
            if json_body is not None and notion_version == NOTION_VERSION:
                req_headers = headers
            else:
                req_headers = dict(headers)
                req_headers["Notion-Version"] = notion_version
                if json_body is None:
                    req_headers.pop("Content-Type", None)
            for attempt in range(max_attempts):
                try:
                    req_kwargs = {"headers": req_headers, "timeout": timeout, "notion_version": notion_version}
                    if json_body is not None:
                        req_kwargs["json"] = json_body
                    resp_obj = _req_with_fallback(method, url, **req_kwargs)
                    if resp_obj.status_code == 429:
                        if attempt + 1 < max_attempts:
                            time.sleep(_parse_retry_after(resp_obj, attempt))
                            continue
                        return resp_obj, None
                    if resp_obj.status_code >= 500:
                        if attempt + 1 < max_attempts:
                            time.sleep(min(2 ** attempt, 8))
                            continue
                        return resp_obj, None
                    return resp_obj, None
                except Exception as exc:
                    last_err = exc
                    if attempt + 1 < max_attempts:
                        time.sleep(min(2 ** attempt, 8))
            return None, last_err

        has_tables = any(b.get("type") == "table" for b in blocks)

        def _apply_table_fit_width(target_page_id: str) -> None:
            if not has_tables or not target_page_id:
                return
            try:
                updates: list[dict] = []
                for idx in range(2):
                    time.sleep(0.35 if idx == 0 else 0.7)
                    m_resp, m_err = _request_with_retry(
                        "GET",
                        f"https://api.notion.com/v1/pages/{target_page_id}/markdown",
                        timeout=30,
                        notion_version=NOTION_MARKDOWN_VERSION,
                    )
                    if m_resp is None or m_resp.status_code >= 400:
                        print(
                            f"  [WARN] Notion 读取表格 Markdown 失败 ({target_page_id}): {m_err or getattr(m_resp, 'text', '')[:200]}",
                            file=sys.stderr,
                        )
                        return
                    md = (m_resp.json() or {}).get("markdown") or ""
                    updates = _build_fit_width_updates(md)
                    if "<table" in md:
                        break
                if not updates:
                    return
                time.sleep(0.35)
                u_resp, u_err = _request_with_retry(
                    "PATCH",
                    f"https://api.notion.com/v1/pages/{target_page_id}/markdown",
                    json_body={"type": "update_content", "update_content": {"content_updates": updates}},
                    timeout=30,
                    notion_version=NOTION_MARKDOWN_VERSION,
                )
                if u_resp is None or u_resp.status_code >= 400:
                    print(
                        f"  [WARN] Notion 表格自适应宽度设置未生效 ({target_page_id}): {u_err or getattr(u_resp, 'text', '')[:200]}",
                        file=sys.stderr,
                    )
            except Exception as exc:
                print(f"  [WARN] Notion 表格自适应宽度异常 ({target_page_id}): {exc}", file=sys.stderr)

        def _append_chunk_list(target_page_id: str, chunks: list[list[dict]]) -> tuple[bool, list[str], str]:
            created_ids: list[str] = []
            for idx, chunk in enumerate(chunks):
                app_resp, app_err = _request_with_retry(
                    "PATCH",
                    f"https://api.notion.com/v1/blocks/{target_page_id}/children",
                    json_body={"children": chunk},
                    timeout=35,
                )
                if app_resp is None:
                    return False, created_ids, f"重试失败: {app_err}"
                if app_resp.status_code >= 400:
                    return False, created_ids, f"HTTP {app_resp.status_code}: {app_resp.text}"
                for blk in (app_resp.json().get("results") or []):
                    bid = blk.get("id")
                    if bid:
                        created_ids.append(bid)
                if idx + 1 < len(chunks):
                    time.sleep(0.35)
            return True, created_ids, ""

        # 1. 查询当天同信源、同类型的未归档记录，保证幂等更新（不产生重复卡片）
        query_body = {
            "filter": {
                "and": [
                    {"property": "日期", "date": {"equals": date_str}},
                    {"property": "信源", "select": {"equals": source}},
                    {"property": "类型", "select": {"equals": report_type}},
                ]
            },
            "sorts": [{"timestamp": "last_edited_time", "direction": "descending"}],
        }
        q_resp, q_err = _request_with_retry(
            "POST",
            f"https://api.notion.com/v1/databases/{database_id}/query",
            json_body=query_body,
            timeout=30,
        )
        if q_resp is None:
            return False, f"Notion 查询异常（重试均失败）: {q_err}"
        if q_resp.status_code >= 400:
            return False, f"Notion API 查询失败 HTTP {q_resp.status_code}: {q_resp.text}"

        matches = [m for m in (q_resp.json().get("results") or []) if not m.get("archived")]

        # 分支 A：库内尚无当日记录 -> 新建页面（必要时安全追加剩余块切片，任何失败均返回 False）
        if not matches:
            resp, last_err = _request_with_retry(
                "POST",
                "https://api.notion.com/v1/pages",
                json_body=payload,
                timeout=35,
            )
            if resp is None:
                return False, f"Notion 同步异常（重试均失败）: {last_err}"
            if resp.status_code >= 400:
                return False, f"Notion API HTTP {resp.status_code}: {resp.text}"

            data = resp.json()
            page_id = data.get("id")
            page_url = data.get("url") or (f"https://notion.so/{page_id.replace('-', '')}" if page_id else "")

            if rest_chunks and page_id:
                time.sleep(0.35)
                app_ok, _, app_err = _append_chunk_list(page_id, rest_chunks)
                if not app_ok:
                    return False, f"Notion 后续区块追加失败 ({page_url}): {app_err}"

            _apply_table_fit_width(page_id)
            return True, page_url

        # 分支 B：已存在同日同信源同类型记录 -> 保留最新一条原地覆盖，保留 Comments 与 URL，先追加后删除
        target = matches[0]
        page_id = target.get("id", "")
        page_url = target.get("url") or (f"https://notion.so/{page_id.replace('-', '')}" if page_id else "")

        old_ids: list[str] = []
        cursor: str | None = None
        while True:
            list_url = f"https://api.notion.com/v1/blocks/{page_id}/children?page_size=100"
            if cursor:
                list_url += f"&start_cursor={cursor}"
            l_resp, l_err = _request_with_retry("GET", list_url, timeout=30)
            if l_resp is None:
                return False, f"Notion 读取旧区块异常 ({page_url}): {l_err}"
            if l_resp.status_code >= 400:
                return False, f"Notion 读取旧区块失败 ({page_url}) HTTP {l_resp.status_code}: {l_resp.text}"
            l_data = l_resp.json()
            for blk in (l_data.get("results") or []):
                bid = blk.get("id")
                if bid:
                    old_ids.append(bid)
            if not l_data.get("has_more"):
                break
            cursor = l_data.get("next_cursor")
            if not cursor:
                break
            time.sleep(0.35)

        patch_props = dict(payload["properties"])

        p_resp, p_err = _request_with_retry(
            "PATCH",
            f"https://api.notion.com/v1/pages/{page_id}",
            json_body={"icon": payload["icon"], "properties": patch_props},
            timeout=30,
        )
        if p_resp is None:
            return False, f"Notion 页面属性更新异常 ({page_url}): {p_err}"
        if p_resp.status_code >= 400:
            return False, f"Notion 页面属性更新失败 ({page_url}) HTTP {p_resp.status_code}: {p_resp.text}"

        if block_chunks:
            time.sleep(0.35)
            app_ok, created_ids, app_err = _append_chunk_list(page_id, block_chunks)
            if not app_ok:
                # 仅回滚本次已写入的新区块，保留旧区块避免页面内容丢失
                for cid in created_ids:
                    time.sleep(0.35)
                    try:
                        _request_with_retry("DELETE", f"https://api.notion.com/v1/blocks/{cid}", timeout=30)
                    except Exception:
                        pass
                return False, f"Notion 页面更新追加区块失败 ({page_url}): {app_err}"

        for old_id in old_ids:
            time.sleep(0.35)
            d_resp, d_err = _request_with_retry("DELETE", f"https://api.notion.com/v1/blocks/{old_id}", timeout=30)
            if d_resp is None:
                return False, f"Notion 删除旧区块异常 ({page_url}, block={old_id}): {d_err}"
            if d_resp.status_code >= 400:
                err_txt = getattr(d_resp, "text", "") or ""
                if d_resp.status_code == 404 or "object_not_found" in err_txt or "archived" in err_txt.lower():
                    continue
                return False, f"Notion 删除旧区块失败 ({page_url}, block={old_id}) HTTP {d_resp.status_code}: {err_txt}"

        for dup in matches[1:]:
            dup_id = dup.get("id")
            if not dup_id:
                continue
            time.sleep(0.35)
            arch_resp, arch_err = _request_with_retry(
                "PATCH",
                f"https://api.notion.com/v1/pages/{dup_id}",
                json_body={"archived": True},
                timeout=30,
            )
            if arch_resp is None or arch_resp.status_code >= 400:
                print(
                    f"  [WARN] 归档重复页 {dup_id} 失败: {arch_err or getattr(arch_resp, 'text', '')}",
                    file=sys.stderr,
                )

        _apply_table_fit_width(page_id)
        return True, page_url
    except Exception as e:
        return False, f"Notion 同步异常: {str(e)}"


def main():
    parser = argparse.ArgumentParser(description="同步 Bot 稿件到 Notion 数据库")
    parser.add_argument("--source", required=True, help="信源名称，如 '🐙 GitHub & HN', '🗽 美股与加密' 等")
    parser.add_argument("--draft", required=True, help="稿件文本路径")
    parser.add_argument("--date", default=None, help="日期 YYYY-MM-DD，缺省为当天")
    parser.add_argument("--type", default="日报", choices=["日报", "周盘点", "开盘前瞻", "午间复盘", "收盘复盘"], help="类型")
    parser.add_argument("--title", default=None, help="自定义标题")
    args = parser.parse_args()

    draft_path = Path(args.draft)
    if not draft_path.exists():
        print(f"❌ 稿件文件不存在: {draft_path}", file=sys.stderr)
        sys.exit(1)

    text = draft_path.read_text(encoding="utf-8").strip()
    if not text:
        print("❌ 稿件为空", file=sys.stderr)
        sys.exit(1)

    ok, res = archive_to_notion(
        report_text=text,
        source=args.source,
        date_str=args.date,
        report_type=args.type,
        title_override=args.title,
    )
    if ok:
        print(f"✅ Notion 归档成功: {res}")
        sys.exit(0)
    else:
        print(f"⚠️ Notion 归档未完成: {res}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
