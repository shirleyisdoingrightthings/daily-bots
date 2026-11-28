#!/usr/bin/python3
"""
X 热点 Bot 的数据源层（provider）。

为什么单独一个文件：X 官方 API 没有免费档，能拿到「原帖链接 + 讨论度」的路子
不止一条，而且都可能失效。把取材抽成 provider，换源时只动这里，x_report.py 不用改。

现有 provider（2026-08-31 实测可用，均直连 200）：
  ainews    主力。原站 news.smol.ai/rss.xml 自 2026-09-04 起 402（见 AINEWS_FEEDS
            处的说明），现以 latent.space/feed 兜底，取其中的 [AINews] 期刊。
            每日扫 544 个 X 账号 + 12 个 subreddit，
            单期正文约 90KB，**每条论断后面都挂原始 x.com 帖子链接**（实测 44~79 条/期）。
            章节结构固定：AI Twitter Recap → Top Story → Facts vs Opinions →
            Different perspectives（supportive / neutral / critical）→ AI Reddit Recap。
  techmeme  techmeme.com/feed.xml —— 交叉验证用。它不给 X 原帖，只用来确认
            「AINews 说的这件事，主流科技媒体是不是也在报」，避免把小圈层的口水
            当成行业热点。

已验证不可用、不要再试（2026-08-31）：
  - neodrop.ai   /rss.xml 404、/feed 返回 HTML，没有 RSS，只能爬页面
  - digg.com/rss 404
  - buzzing.cc   有 tech / hn / reddit / stocks 子站，但**没有 X 子站**
                 （twitter. 与 x. 子域 SSL 直接失败），补的是 HN/Reddit 不是 X
  - Nitter       2026 年公共实例基本全灭
  - RSSHub / RSS-Bridge 的 twitter 路由：能跑，但业界共识是别用于生产
"""

import html
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "shared"))
# X_POST_RE 在 bot_utils 里定义（ai-builder-bot 的出稿闸门也用它）；
# 这里 re-export，本模块内与 x_report.py 的 sources.X_POST_RE 用法都不变。
from bot_utils import fetch_rss, parse_entry_date, is_ai_relevant, url_key, X_POST_RE, REDDIT_POST_RE

# 随早 09:00 AI 三合一串行流水线运行，时效窗口 48h（跨天已播期刊由 sent_urls 拦截）。
DEFAULT_WINDOW_H = 48

AINEWS_FEED = "https://www.latent.space/feed"
AINEWS_TAG = "[ainews]"
TECHMEME_FEED = "https://www.techmeme.com/feed.xml"

HEADING_RE = re.compile(r"(?is)<h([1-4])[^>]*>(.*?)</h\1>")
ANCHOR_RE = re.compile(r'(?is)<a\b[^>]*href=["\']([^"\']*)["\'][^>]*>(.*?)</a>')
TAG_RE = re.compile(r"(?s)<[^>]+>")
WS_RE = re.compile(r"[ \t]+")


_TRACKING_QS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "mc_cid", "mc_eid", "ref_src", "ref_url",
}


def _clean_url(url: str) -> str:
    """去掉追踪参数并归一化 X status / Reddit 帖子链接。"""
    hit = X_POST_RE.search(url)
    if hit:
        return f"https://x.com/{hit.group(1)}/status/{hit.group(2)}"
    try:
        parts = urlsplit(url)
        host = parts.netloc.lower()
        drop_keys = set(_TRACKING_QS)
        if "x.com" in host or "twitter.com" in host:
            drop_keys.update({"s", "t"})
        if "reddit.com" in host:
            drop_keys.update({"rdt", "share_id", "context"})
        qs = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
              if k.lower() not in drop_keys]
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(qs), ""))
    except Exception:
        return url


def _plain(fragment: str, limit: int = 0, preserve_links: bool = False) -> str:
    """HTML 片段 → 纯文本。preserve_links=True 时保留有意义的 [文本](URL) 以便写稿随文加链接。"""
    work = fragment
    if preserve_links:
        def _keep_link(m):
            href = html.unescape(m.group(1) or "").strip()
            label = TAG_RE.sub("", m.group(2) or "")
            label = WS_RE.sub(" ", html.unescape(label)).strip()
            if not label:
                return ""
            if not href.startswith(("http://", "https://")):
                return label
            low = href.lower().split("?", 1)[0]
            if any(h in low for h in ("substackcdn.com", "pbs.twimg.com")) and low.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")):
                return label
            cleaned = _clean_url(href)
            if label.startswith(("http://", "https://")):
                return cleaned
            if label.startswith("[") and label.endswith("]") and len(label) > 2:
                label = label[1:-1].strip()
            label = label.replace("[", "(").replace("]", ")")
            return f"[{label}]({cleaned})"

        work = ANCHOR_RE.sub(_keep_link, work)
    text = re.sub(r"(?i)<br\s*/?>", "\n", work)
    text = re.sub(r"(?i)</(p|li|div|h[1-6])>", "\n", text)
    text = TAG_RE.sub("", text)
    text = html.unescape(text)
    text = WS_RE.sub(" ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:limit] if limit else text


def _entry_html(entry) -> str:
    """取一条 RSS 条目的全文 HTML。AINews 的正文在 content:encoded 里，
    summary 只有一小段导语，拿错了就没有原帖链接。"""
    for item in (getattr(entry, "content", None) or []):
        value = item.get("value") if isinstance(item, dict) else getattr(item, "value", "")
        if value and len(value) > 2000:
            return value
    return getattr(entry, "summary", "") or ""


def _twitter_section(body: str) -> str:
    """截出 AI Twitter Recap 那一段。取不到就退回全文——宁可多给写稿方一些
    Reddit 素材，也不要因为站点改版就整轮零产。"""
    heads = list(HEADING_RE.finditer(body))
    start = end = None
    for h in heads:
        title = _plain(h.group(2)).lower()
        if start is None and "twitter recap" in title:
            start = h.start()
            continue
        if start is not None and ("reddit recap" in title or "discord recap" in title):
            end = h.start()
            break
    if start is None:
        return body
    return body[start:end] if end else body[start:]


def _outline(section: str) -> list:
    """Twitter Recap 段的 h2 提纲，让写稿方一眼看出这期覆盖了哪些面。

    刻意不按标题切块：AINews 的结构是浮动的——**单话题日**（如 2026-08-26 全篇
    只讲 GLM-5.3-Flash）里 h2 是同一件事的各个侧面（What happened / Facts vs
    opinions / Supportive / Critical），按 h2 切会把一件事拆成十几个假话题，
    标题还全是「Supportive」这种没有信息量的词。多话题日 h2 才是真话题。
    与其猜，不如把提纲和正文一起给写稿方，让它自己判断当天是几件事。
    """
    return [_plain(m.group(2)) for m in HEADING_RE.finditer(section)
            if m.group(1) == "2"]


def _slice_safe(text: str, start: int, max_len: int) -> str:
    """按换行边界截断并避免从 [文本](URL) 链接中间腰斩。"""
    if len(text) - start <= max_len:
        return text[start:]
    end = text.rfind("\n", start, start + max_len)
    if end <= start + int(max_len * 0.5):
        end = start + max_len
        lb = text.rfind("[", start, end)
        rb = text.rfind(")", start, end)
        if lb > rb:
            end = lb
    return text[start:end].rstrip()


def _condense(section: str, budget: int) -> str:
    """把 Twitter Recap 压到预算内，**但必须保住「分歧」那几段**，并保留正文内 [锚文本](URL) 链接。"""
    text = _plain(section, preserve_links=True)
    if len(text) <= budget:
        return text
    head_budget = int(budget * 0.55)
    tail_budget = budget - head_budget
    for anchor in ("Different perspectives", "Facts vs opinions"):
        idx = text.find(anchor)
        if idx > head_budget:
            return (_slice_safe(text, 0, head_budget)
                    + "\n\n…（中间的技术细节已略，完整版见上面的原文链接）…\n\n"
                    + _slice_safe(text, idx, tail_budget))
    return _slice_safe(text, 0, budget)


_ENGAGEMENT_LINE_RE = re.compile(
    r"https?://(?:www\.)?(?:x|twitter)\.com/([A-Za-z0-9_]+)/status/(\d+)[^\n]{0,120}?[—–-]\s*(\d+(?:\.\d+)?)\s*([kKmM]?)\b"
)


def _parse_engagement_num(num_str: str, unit: str) -> float:
    try:
        val = float(num_str)
    except ValueError:
        return 0.0
    u = (unit or "").lower()
    if u == "k":
        return val * 1_000.0
    if u == "m":
        return val * 1_000_000.0
    return val


def _extract_top_tweet_engagement(section: str) -> dict:
    """提取 AINews 'Top tweets (by engagement)' 区块里的真实互动量（如 26.3k / 8.4k）。"""
    plain = _plain(section, preserve_links=True)
    idx = plain.lower().find("top tweets")
    if idx < 0:
        return {}
    tail = plain[idx:idx + 6000]
    eng_map = {}
    for m in _ENGAGEMENT_LINE_RE.finditer(tail):
        pid = m.group(2)
        raw_val = f"{m.group(3)}{m.group(4).lower()}"
        num_val = _parse_engagement_num(m.group(3), m.group(4))
        if num_val > 0 and (pid not in eng_map or num_val > eng_map[pid][1]):
            eng_map[pid] = (raw_val, num_val)
    return eng_map


def _rank_posts(section: str, max_posts: int, ctx_chars: int) -> list:
    """提取段落里的 X 原帖，结合 AINews 自带的真实互动量（Top tweets）与去重后的被引用次数排序。

    计数在 <a> 标签粒度进行，并合并 Substack 把含下划线 handle 拆成相邻双 <a> 的伪重（如 [@][arohan]），
    避免同一引用被原样 HTML 正则双计数。
    """
    eng_map = _extract_top_tweet_engagement(section)
    order, meta = [], {}
    prev_pid = None
    prev_end = -100

    # 先按 <a> 维度计数，避免 href 与锚文本同时含 URL 或相邻拆链造成双计数
    def _count_anchor(m):
        nonlocal prev_pid, prev_end
        href = html.unescape(m.group(1) or "")
        hit = X_POST_RE.search(href)
        if not hit:
            return m.group(2)
        handle, pid = hit.group(1), hit.group(2)
        if pid not in meta:
            eng_str, eng_num = eng_map.get(pid, ("", 0.0))
            meta[pid] = {
                "handle": handle,
                "url": f"https://x.com/{handle}/status/{pid}",
                "mentions": 0,
                "engagement": eng_str,
                "engagement_num": eng_num,
                "idx": len(order),
            }
            order.append(pid)
        # 相邻两个 <a>（间隔 <= 2 字符）指向同一个 status id（典型为 Substack 拆下划线 handle）只计 1 次
        if not (pid == prev_pid and m.start() - prev_end <= 2):
            meta[pid]["mentions"] += 1
        prev_pid = pid
        prev_end = m.end()
        return f"{m.group(2)} ⟦P{meta[pid]['idx']}⟧ "

    marked = ANCHOR_RE.sub(_count_anchor, section)

    # 兜底：不在 <a> 里的裸链接
    def _count_bare(m):
        handle, pid = m.group(1), m.group(2)
        if pid not in meta:
            eng_str, eng_num = eng_map.get(pid, ("", 0.0))
            meta[pid] = {
                "handle": handle,
                "url": f"https://x.com/{handle}/status/{pid}",
                "mentions": 0,
                "engagement": eng_str,
                "engagement_num": eng_num,
                "idx": len(order),
            }
            order.append(pid)
        meta[pid]["mentions"] += 1
        return f" ⟦P{meta[pid]['idx']}⟧ "

    marked = X_POST_RE.sub(_count_bare, marked)
    text = _plain(marked)

    for pid in order:
        info = meta[pid]
        token = f"⟦P{info['idx']}⟧"
        pos = text.find(token)
        if pos < 0:
            info["context"] = ""
            continue
        left = max(0, pos - ctx_chars)
        snippet = (text[left:pos] + text[pos + len(token):pos + len(token) + 60])
        info["context"] = re.sub(r"⟦P\d+⟧", "", snippet).strip()

    ranked = sorted(
        meta.values(),
        key=lambda d: (-d["engagement_num"], -d["mentions"], d["idx"]),
    )
    return [{k: v for k, v in d.items() if k not in ("idx", "engagement_num")} for d in ranked[:max_posts]]


def _reddit_section(body: str) -> str:
    """截出 AI Reddit Recap 那一段，遇到 Discord Recap 或下一个顶级 h1 即止。取不到返回空串。"""
    heads = list(HEADING_RE.finditer(body))
    start = end = None
    for h in heads:
        level = h.group(1)
        title = _plain(h.group(2)).lower()
        if start is None and "reddit recap" in title:
            start = h.start()
            continue
        if start is not None and (
            "discord recap" in title
            or "ai discord" in title
            or (level == "1" and "reddit" not in title)
        ):
            end = h.start()
            break
    if start is None:
        return ""
    return body[start:end] if end else body[start:]


def _reddit_outline(section: str) -> list:
    """Reddit Recap 段的 h2/h3 提纲，便于写稿方快速定位子版块与话题集群。"""
    if not section:
        return []
    return [_plain(m.group(2)) for m in HEADING_RE.finditer(section)
            if m.group(1) in ("2", "3")]


_REDDIT_ACTIVITY_RE = re.compile(r"\(Activity:\s*([\d,]+)\)", re.I)


def _rank_reddit_posts(section: str, max_posts: int = 20, ctx_chars: int = 260) -> list:
    """提取 AI Reddit Recap 中的 reddit.com 帖子直链，按 AINews 标注的 Activity 活跃度排序。"""
    if not section:
        return []
    order, meta = [], {}

    def _count_anchor(m):
        href = html.unescape(m.group(1) or "")
        hit = REDDIT_POST_RE.search(href)
        if not hit:
            return m.group(2)
        sub, pid = hit.group(1), hit.group(2).lower()
        full_url = _clean_url(hit.group(0))
        if pid not in meta:
            meta[pid] = {
                "subreddit": sub,
                "url": full_url,
                "mentions": 0,
                "activity": 0,
                "idx": len(order),
            }
            order.append(pid)
        meta[pid]["mentions"] += 1
        return f"{m.group(2)} ⟦R{meta[pid]['idx']}⟧ "

    marked = ANCHOR_RE.sub(_count_anchor, section)

    def _count_bare(m):
        sub, pid = m.group(1), m.group(2).lower()
        full_url = _clean_url(m.group(0))
        if pid not in meta:
            meta[pid] = {
                "subreddit": sub,
                "url": full_url,
                "mentions": 0,
                "activity": 0,
                "idx": len(order),
            }
            order.append(pid)
        meta[pid]["mentions"] += 1
        return f" ⟦R{meta[pid]['idx']}⟧ "

    marked = REDDIT_POST_RE.sub(_count_bare, marked)
    if not order:
        return []
    text = _plain(marked)

    for pid in order:
        info = meta[pid]
        token = f"⟦R{info['idx']}⟧"
        pos = text.find(token)
        if pos < 0:
            info["context"] = ""
            continue
        left = max(0, pos - 80)
        right = min(len(text), pos + len(token) + ctx_chars)
        snippet = text[left:pos] + text[pos + len(token):right]
        snippet = re.sub(r"⟦R\d+⟧", "", snippet).strip()
        act_m = _REDDIT_ACTIVITY_RE.search(snippet)
        if act_m:
            try:
                info["activity"] = int(act_m.group(1).replace(",", ""))
            except ValueError:
                pass
        info["context"] = snippet

    ranked = sorted(meta.values(), key=lambda d: (-d["activity"], -d["mentions"], d["idx"]))
    return [{k: v for k, v in d.items() if k != "idx"} for d in ranked[:max_posts]]


def fetch_ainews(window_h: int = DEFAULT_WINDOW_H, max_issues: int = 2,
                 max_posts: int = 45, recap_chars: int = 65000,
                 ctx_chars: int = 280, seen_urls: set = None,
                 reddit_chars: int = 35000, max_reddit_posts: int = 20) -> dict:
    """主力源。返回 {"name","issues","raw_count","reddit_raw_count","note"}。

    每期：{title, url, date, outline:[h2...], recap:纯文本, posts:[...],
           reddit_outline:[...], reddit_recap:纯文本, reddit_posts:[...]}
    """
    got = fetch_rss(AINEWS_FEED, limit=max_issues * 6)
    entries = [e for e in got if AINEWS_TAG in (getattr(e, "title", "") or "").lower()]
    if not entries:
        return {"name": "ainews", "issues": [], "raw_count": 0,
                "reddit_raw_count": 0, "note": "RSS 抓取失败"}

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=window_h)
    picked = []
    for e in entries:
        dt = parse_entry_date(e)
        if dt and dt < cutoff:
            continue
        link = getattr(e, "link", "") or ""
        if seen_urls and link and url_key(link) in seen_urls:
            continue  # 该期已推送过，严格拦截不重播
        picked.append((dt, e))
        if len(picked) >= max_issues:
            break

    # 窗口内无未播新刊时直接返回空列表（触发 NO_TOPICS 停发），避免重播旧期
    if not picked:
        return {"name": "ainews", "issues": [], "raw_count": 0,
                "reddit_raw_count": 0, "note": "窗口内无未播新刊"}

    issues, total_posts, total_reddit_posts = [], 0, 0
    for dt, e in picked:
        body = _entry_html(e)
        if not body:
            continue
        section = _twitter_section(body)
        posts = _rank_posts(section, max_posts, ctx_chars)
        if not posts:
            continue  # 没有原帖链接的一期对我们没用
        total_posts += len(posts)
        reddit_sec = _reddit_section(body)
        reddit_posts = _rank_reddit_posts(reddit_sec, max_reddit_posts, ctx_chars) if reddit_sec else []
        total_reddit_posts += len(reddit_posts)
        issues.append({
            "title": getattr(e, "title", "") or "AINews",
            "url": getattr(e, "link", "") or "",
            "date": dt.strftime("%Y-%m-%d") if dt else "",
            "outline": _outline(section),
            "recap": _condense(section, recap_chars),
            "posts": posts,
            "reddit_outline": _reddit_outline(reddit_sec),
            "reddit_recap": _plain(reddit_sec, limit=reddit_chars, preserve_links=True) if reddit_sec else "",
            "reddit_posts": reddit_posts,
        })

    return {"name": "ainews", "issues": issues, "raw_count": total_posts,
            "reddit_raw_count": total_reddit_posts, "note": ""}


def fetch_techmeme(window_h: int = DEFAULT_WINDOW_H, limit: int = 25,
                   max_items: int = 10) -> dict:
    """交叉验证源。只留 AI 相关条目，不产出原帖链接。"""
    entries = fetch_rss(TECHMEME_FEED, limit=limit)
    if not entries:
        return {"name": "techmeme", "items": [], "raw_count": 0, "note": "RSS 抓取失败"}

    cutoff = datetime.now(timezone.utc) - timedelta(hours=window_h)
    items = []
    for e in entries:
        dt = parse_entry_date(e)
        if dt and dt < cutoff:
            continue
        title = getattr(e, "title", "") or ""
        summary = _plain(getattr(e, "summary", "") or "", 300)
        if not is_ai_relevant(title, summary):
            continue
        items.append({"title": title, "url": getattr(e, "link", "") or "",
                      "summary": summary,
                      "date": dt.strftime("%Y-%m-%d %H:%M") if dt else ""})
        if len(items) >= max_items:
            break
    return {"name": "techmeme", "items": items, "raw_count": len(items), "note": ""}


