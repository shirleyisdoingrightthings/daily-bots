#!/usr/bin/python3
"""
sources.py — GitHub Trending & Hacker News 早报的数据抓取层

包含两个核心数据源：
1. GitHub Trending（https://github.com/trending?since=daily 或 weekly）
   - 解析全页（~25 个）仓库，过滤非 owner/repo 卡片后按新鲜度优先（🆕 未在近 30 天播过的新星优先，
     全局榜新星不足时自动补抓 Python / TypeScript 趋势榜），彻底根治连续多日全员复读老面孔。
   - 仅对新上榜仓库并发拉取 README 核心段落（全量剥离 HTML 标签与实体解码），节省请求并提高信息密度。
2. Hacker News 热榜 Top 8（Firebase top/best + Algolia 36h/front_page 并发拉取 + 综合热度分）
   - 仅对命中今日前序 AI 日报账本（URL 或 x_status_ids）的条目动态扩容候选池（最多 12 条）
   - 并发抓取外链正文摘要（复用 shared/bot_utils.fetch_article_text）及按子回复热度重排的顶层评论。
"""

from __future__ import annotations

import html
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "shared"))
from bot_utils import X_POST_RE, fetch_article_text
from bot_utils import url_key as _base_url_key


def url_key(url: str) -> str:
    """统一复用共享层 url_key（已原生保留 HN ?id=、归一 x.com / https / www.）。"""
    return _base_url_key(url)


_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

_HTTP_SESSION = requests.Session()
_HTTP_SESSION.headers.update({"User-Agent": _UA})

_ARTICLE_RE = re.compile(r'<article\s+class="Box-row"[^>]*>(.*?)</article>', re.DOTALL)
_H2_LINK_RE = re.compile(r'<h2\s+class="h3\s+lh-condensed"[^>]*>.*?<a\s+[^>]*href="(/[^"]+)"', re.DOTALL)
_DESC_RE = re.compile(r'<p\s+class="col-9[^"]*"[^>]*>(.*?)</p>', re.DOTALL)
_LANG_RE = re.compile(r'itemprop="programmingLanguage"[^>]*>(.*?)</span>', re.DOTALL)
_STARGAZERS_RE = re.compile(r'href="/[^/]+/[^/]+/stargazers"[^>]*>(.*?)</a>', re.DOTALL)
_FORKS_RE = re.compile(r'href="/[^/]+/[^/]+/forks"[^>]*>(.*?)</a>', re.DOTALL)
_PERIOD_STARS_RE = re.compile(r'class="d-inline-block\s+float-sm-right"[^>]*>(.*?)</span>', re.DOTALL)
_TAG_STRIP_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_RETRY_STATUSES = {429, 500, 502, 503, 504}


def _get_with_retry(url: str, *, timeout: int = 25, retries: int = 2) -> requests.Response:
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            resp = _HTTP_SESSION.get(url, timeout=timeout)
            if resp.status_code in _RETRY_STATUSES and attempt < retries:
                retry_after = resp.headers.get("Retry-After")
                delay = int(retry_after) if (retry_after and retry_after.isdigit()) else 2 * (attempt + 1)
                time.sleep(min(delay, 10))
                continue
            resp.raise_for_status()
            return resp
        except Exception as exc:
            last_exc = exc
            if attempt < retries:
                time.sleep(2 * (attempt + 1))
                continue
            raise
    if last_exc:
        raise last_exc
    raise RuntimeError(f"GET failed: {url}")


def _clean_text(raw: str) -> str:
    if not raw:
        return ""
    txt = _TAG_STRIP_RE.sub(" ", raw)
    return _WS_RE.sub(" ", html.unescape(txt)).strip()


def _clean_readme(md: str, max_chars: int = 1000) -> str:
    """清洗 README.md，全量剥离 HTML 标签与注释、解码 HTML 实体、去掉徽章与空行。"""
    if not md:
        return ""
    md = re.sub(r"<!--.*?-->", " ", md, flags=re.DOTALL)
    md = re.sub(r"(?is)<(script|style).*?</\1>", " ", md)
    md = _TAG_STRIP_RE.sub(" ", md)
    md = html.unescape(md)
    lines = []
    for raw_line in md.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("[![") or line.startswith("!["):
            continue
        if re.match(r"^[\-\=\|\:\s]+$", line):
            continue
        line = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", line)
        line = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", line)
        line = _WS_RE.sub(" ", line).strip()
        if len(line) >= 8:
            lines.append(line)
        if sum(len(x) for x in lines) >= max_chars * 1.3:
            break
    joined = " / ".join(lines)
    return joined[:max_chars].strip()


def _fetch_repo_readme(repo: str, timeout: int = 8) -> str:
    """尝试拉取仓库的 README 摘要（依次尝试 HEAD/README.md 与 HEAD/readme.md）。"""
    for filename in ("README.md", "readme.md", "README.rst"):
        url = f"https://raw.githubusercontent.com/{repo}/HEAD/{filename}"
        try:
            resp = _HTTP_SESSION.get(url, timeout=timeout)
            if resp.status_code == 200 and resp.text:
                cleaned = _clean_readme(resp.text)
                if cleaned:
                    return cleaned
        except Exception:
            continue
    return ""


def _parse_trending_html(
    raw_html: str,
    seen_history: dict[str, str],
    *,
    source_tag: str = "",
) -> list[dict[str, Any]]:
    """解析单页 GitHub Trending HTML，先过滤非 owner/repo 卡片再连续编号。"""
    articles = _ARTICLE_RE.findall(raw_html or "")
    parsed: list[dict[str, Any]] = []
    rank = 0
    for block in articles:
        m_link = _H2_LINK_RE.search(block)
        if not m_link:
            continue
        rel_path = m_link.group(1).strip().strip("/")
        parts = [p.strip() for p in rel_path.split("/") if p.strip()]
        if len(parts) != 2 or parts[0].lower() in ("sponsors", "topics", "collections", "orgs", "marketplace", "apps", "features"):
            continue
        rank += 1
        repo_name = f"{parts[0]}/{parts[1]}"
        repo_url = f"https://github.com/{repo_name}"

        m_desc = _DESC_RE.search(block)
        desc = _clean_text(m_desc.group(1)) if m_desc else ""

        m_lang = _LANG_RE.search(block)
        lang = _clean_text(m_lang.group(1)) if m_lang else "Unknown"

        m_stars = _STARGAZERS_RE.search(block)
        total_stars = _clean_text(m_stars.group(1)) if m_stars else ""

        m_forks = _FORKS_RE.search(block)
        forks = _clean_text(m_forks.group(1)) if m_forks else ""

        m_period = _PERIOD_STARS_RE.search(block)
        period_stars = _clean_text(m_period.group(1)) if m_period else ""

        prev_date = seen_history.get(url_key(repo_url))

        parsed.append({
            "rank": rank,
            "repo": repo_name,
            "url": repo_url,
            "description": desc,
            "language": lang,
            "total_stars": total_stars,
            "forks": forks,
            "period_stars": period_stars,
            "prev_date": prev_date,
            "source_tag": source_tag,
            "readme_excerpt": "",
        })
    return parsed


def fetch_github_trending(
    since: str = "daily",
    limit: int = 10,
    seen_history: dict[str, str] | None = None,
    *,
    min_fresh: int = 5,
    max_fresh: int = 8,
) -> dict[str, Any]:
    """抓取 GitHub Trending（daily 或 weekly）：全页解析 + 新星优先 + 语种分榜自动补盲。

    1) 解析全局 Trending 全部 ~25 个仓库（不再截断前 10）；
    2) 若全局榜中未在 seen_history 出现的新面孔不足 min_fresh，自动补抓 python / typescript 趋势榜；
    3) 仅对新上榜仓库并发抓 README，避免浪费带宽重复拉取持续霸榜老面孔。"""
    seen_history = seen_history or {}
    url = f"https://github.com/trending?since={since}"
    resp = _get_with_retry(url, timeout=25, retries=2)
    main_repos = _parse_trending_html(resp.text, seen_history, source_tag="")

    if not main_repos:
        print(f"  ⚠️ GitHub Trending 解析到 0 个仓库（疑似反爬或 DOM 改版）：{url}", file=sys.stderr)
        return {
            "since": since,
            "url": url,
            "repos": [],
            "fresh_repos": [],
            "recurring_repos": [],
            "raw_count": 0,
        }

    seen_repo_names = {r["repo"].lower() for r in main_repos}
    fresh_repos = [r for r in main_repos if not r.get("prev_date")]
    recurring_repos = [r for r in main_repos[:limit] if r.get("prev_date")]

    # 全局榜新星不足时，自动补抓 Python / TypeScript 趋势榜挖掘新面孔
    if len(fresh_repos) < min_fresh:
        for lang_slug, tag in (("python", "Python 趋势补盲"), ("typescript", "TypeScript 趋势补盲")):
            if len(fresh_repos) >= max_fresh:
                break
            sub_url = f"https://github.com/trending/{lang_slug}?since={since}"
            try:
                sub_resp = _get_with_retry(sub_url, timeout=20, retries=1)
                sub_repos = _parse_trending_html(sub_resp.text, seen_history, source_tag=tag)
                for sr in sub_repos:
                    rk = sr["repo"].lower()
                    if rk in seen_repo_names or sr.get("prev_date"):
                        continue
                    seen_repo_names.add(rk)
                    fresh_repos.append(sr)
                    if len(fresh_repos) >= max_fresh:
                        break
            except Exception as exc:
                print(f"  [WARN] GitHub 补盲分榜 {lang_slug} 抓取失败：{exc}", file=sys.stderr)

    fresh_repos = fresh_repos[:max_fresh]
    for idx, r in enumerate(fresh_repos, 1):
        r["rank"] = idx

    # 仅对新面孔并发拉取 README（若当天实在一个新面孔都没有，才兜底拉取前 3 个熟面孔）
    readme_targets = fresh_repos if fresh_repos else recurring_repos[:3]
    if readme_targets:
        with ThreadPoolExecutor(max_workers=5) as pool:
            futs = [(r, pool.submit(_fetch_repo_readme, r["repo"])) for r in readme_targets]
            for r, fut in futs:
                try:
                    r["readme_excerpt"] = fut.result(timeout=12)
                except Exception:
                    r["readme_excerpt"] = ""

    combined = fresh_repos + recurring_repos
    return {
        "since": since,
        "url": url,
        "repos": combined,
        "fresh_repos": fresh_repos,
        "recurring_repos": recurring_repos,
        "raw_count": len(main_repos),
    }


def _fetch_hn_item_details(item: dict[str, Any]) -> None:
    """补充单条 HN 帖子的外链正文摘要 + 按子回复热度重排的前 4 条核心评论。"""
    oid = item["id"]
    ext_url = item.get("url", "")

    if ext_url and not ext_url.startswith("https://news.ycombinator.com"):
        try:
            item["article_excerpt"] = fetch_article_text(ext_url, timeout=8, max_chars=1000, min_chars=120)
        except Exception:
            item["article_excerpt"] = ""

    try:
        resp = _HTTP_SESSION.get(
            f"https://hn.algolia.com/api/v1/items/{oid}",
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            if not item.get("article_excerpt") and data.get("text"):
                item["article_excerpt"] = _clean_text(data.get("text", ""))[:1000]
            children = data.get("children") or []
            scored_comments: list[tuple[int, int, str]] = []
            for idx, ch in enumerate(children):
                c_text = _clean_text(ch.get("text") or "")
                author = ch.get("author") or "anon"
                if len(c_text) < 50:
                    continue
                sub_replies = len(ch.get("children") or [])
                reply_tag = f"（{sub_replies} 条追评）" if sub_replies else ""
                # 综合排序：有追评的争议/高互动评论优先，其次保持原始靠前位置
                weight = sub_replies * 100 + min(len(c_text), 300) // 10 - idx * 3
                scored_comments.append((weight, idx, f"@{author}{reply_tag}：{c_text[:360]}"))
            scored_comments.sort(key=lambda x: (-x[0], x[1]))
            item["top_comments"] = [c[2] for c in scored_comments[:4]]
    except Exception:
        pass


def fetch_hn_top8(
    window_h: int = 36,
    limit: int = 8,
    seen_history: dict[str, str] | None = None,
    today_ledger_urls: set[str] | None = None,
    today_ledger_x_ids: set[str] | None = None,
) -> dict[str, Any]:
    """并发融合 Hacker News 的 Best（高分精选榜）与 Top（实时首页高讨论热榜），取综合热度前 8 名。"""
    seen_history = seen_history or {}
    today_ledger_urls = today_ledger_urls or set()
    today_ledger_x_ids = today_ledger_x_ids or set()
    cutoff = int(time.time()) - window_h * 3600

    top_ids: set[str] = set()
    best_ids: set[str] = set()
    algolia_hits: list[dict[str, Any]] = []

    def _get_json(u: str, to: int):
        r = _HTTP_SESSION.get(u, timeout=to)
        r.raise_for_status()
        return r.json()

    api_tasks = {
        "top": ("https://hacker-news.firebaseio.com/v0/topstories.json", 8),
        "best": ("https://hacker-news.firebaseio.com/v0/beststories.json", 8),
        "story": (f"https://hn.algolia.com/api/v1/search?tags=story&numericFilters=created_at_i>{cutoff}&hitsPerPage=30", 15),
        "front": ("https://hn.algolia.com/api/v1/search?tags=front_page&hitsPerPage=30", 15),
    }

    with ThreadPoolExecutor(max_workers=4) as pool:
        futs = {k: pool.submit(_get_json, u, to) for k, (u, to) in api_tasks.items()}
        for k, fut in futs.items():
            try:
                data = fut.result(timeout=16)
                if k == "top" and isinstance(data, list):
                    top_ids = {str(x) for x in data[:25]}
                elif k == "best" and isinstance(data, list):
                    best_ids = {str(x) for x in data[:25]}
                elif isinstance(data, dict):
                    algolia_hits.extend(data.get("hits") or [])
            except Exception as exc:
                print(f"[sources] HN API 请求异常 ({k}): {exc}", file=sys.stderr)

    by_id: dict[str, dict[str, Any]] = {}
    for h in algolia_hits:
        oid = str(h.get("objectID") or "")
        if not oid:
            continue
        created_i = int(h.get("created_at_i") or 0)
        if created_i and created_i < int(time.time()) - 48 * 3600:
            continue
        pts = int(h.get("points") or 0)
        n_comments = int(h.get("num_comments") or 0)
        if oid not in by_id or pts > by_id[oid]["points"]:
            hn_url = f"https://news.ycombinator.com/item?id={oid}"
            ext_url = h.get("url") or hn_url
            prev_date = (
                seen_history.get(url_key(hn_url))
                or seen_history.get(url_key(ext_url))
            )
            m_x = X_POST_RE.search(ext_url or "")
            x_id_hit = bool(m_x and m_x.group(2) in today_ledger_x_ids)
            in_today_ledger = (
                url_key(ext_url) in today_ledger_urls
                or url_key(hn_url) in today_ledger_urls
                or x_id_hit
            )
            badges = []
            if oid in best_ids:
                badges.append("Best高赞")
            if oid in top_ids:
                badges.append("Top实时热议")
            badge_str = "/".join(badges) if badges else "24h热榜"

            heat_score = pts + int(n_comments * 1.2) + (80 if oid in top_ids else 0) + (60 if oid in best_ids else 0)

            by_id[oid] = {
                "id": oid,
                "title": (h.get("title") or "").strip(),
                "url": ext_url,
                "hn_url": hn_url,
                "points": pts,
                "num_comments": n_comments,
                "badge": badge_str,
                "heat_score": heat_score,
                "author": h.get("author") or "",
                "created_at": h.get("created_at") or "",
                "prev_date": prev_date,
                "in_today_ledger": in_today_ledger,
                "article_excerpt": "",
                "top_comments": [],
            }

    # 外链去重：同一篇文章被二次提交上 HN 时只保留热度最高的那一条讨论串
    deduped: list[dict[str, Any]] = []
    seen_ext_keys: set[str] = set()
    for it in sorted(by_id.values(), key=lambda x: x["heat_score"], reverse=True):
        ek = url_key(it["url"])
        if ek and not ek.startswith("https://news.ycombinator.com/item") and ek in seen_ext_keys:
            continue
        if ek:
            seen_ext_keys.add(ek)
        deduped.append(it)

    # 仅对命中今日前序 AI 账本的条目扩容递补候选池（跨天重复项保留名次不递补）
    ledger_overlap_cnt = sum(1 for it in deduped[:limit] if it.get("in_today_ledger"))
    fetch_cap = min(limit + ledger_overlap_cnt, 12)
    top_items = deduped[:fetch_cap]

    if top_items:
        with ThreadPoolExecutor(max_workers=6) as pool:
            futs = [pool.submit(_fetch_hn_item_details, item) for item in top_items]
            for fut in futs:
                try:
                    fut.result(timeout=18)
                except Exception:
                    pass

    return {
        "items": top_items,
        "raw_count": len(by_id),
    }
