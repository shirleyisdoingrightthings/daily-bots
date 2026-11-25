#!/usr/bin/python3
"""
AI Builder 日报

每天把自建 X List 中一线 AI builder（研究员 / 创始人 / 产品 / 工程）本人发的推文整理成一份
播报，**每条话题都带原推直链**，由 AI 定时任务写稿后推送到飞书并归档至 Notion。本脚本只负责取材与推送，
不含写稿用的第三方大模型 API。

四种运行模式（--mode）：
- fetch       ：通过 twitterapi.io 拉取自建 X List 过去 24h 原创与引用推文 → 落盘 logs/last_context.txt
- send        ：读取写好的稿子文件 → 清洗 HTML → 推送飞书 + 同步 Notion + 写日志
- archive-only：读取写好的稿子文件 → 清洗 HTML → 仅归档本地与 Notion + 写日志（不推飞书）
- test-list   ：只读测试 X List 与数据源连通性，不写 run.log 或 last_context.txt

写稿规范在同目录 prompt.md，由 Antigravity Sidecar 定时任务读取。
"""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlsplit

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "shared"))
from bot_utils import (already_ran_today, load_sent_urls, url_key,
                       make_logger, make_pending_saver, proxy_ok,
                       emit_fetch_output, send_x_linked_report,
                       sanitize_html, extract_hrefs, X_POST_RE,
                       extract_report_date, archive_and_sync_notion,
                       record_sent_urls, fetch_article_text,
                       load_daily_ledger, record_daily_ledger,
                       format_ledger_context)

BASE = Path(__file__).parent
LOG_FILE = BASE / "logs" / "run.log"
JSONL_FILE = BASE / "logs" / "run.jsonl"
LOG_FILE.parent.mkdir(exist_ok=True)
CACHE_FILE = BASE / "pending_messages.json"
DRAFT_FILE = BASE / "logs" / "report_draft.txt"
FETCH_META = BASE / "logs" / "fetch_meta.json"
LAST_CONTEXT = BASE / "logs" / "last_context.txt"
SENT_URLS = BASE / "logs" / "sent_urls.json"


def _resolve_plist_path() -> Path | None:
    """解析可选的 macOS LaunchAgent plist 路径（支持 BOT_PLIST / BOT_PLIST_PREFIX，Linux/无 plist 时返回 None）。"""
    explicit = os.getenv("BOT_PLIST", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    prefix = os.getenv("BOT_PLIST_PREFIX", "").strip()
    if not prefix:
        for env_path in (BASE / ".env", BASE.parent / "shared" / ".env", BASE.parent / ".env"):
            if env_path.exists():
                for line in env_path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line.startswith("BOT_PLIST_PREFIX="):
                        prefix = line.split("=", 1)[1].strip().strip('"').strip("'")
                        break
            if prefix:
                break
    if not prefix:
        return None
    return Path.home() / "Library" / "LaunchAgents" / f"{prefix}.ai-builder-bot.plist"

# 每位 builder 最多进多少条，避免高频账号刷屏挤掉其他人
PER_BUILDER_CAP = int(os.getenv("BUILDER_PER_ACCOUNT_CAP", "4"))
WINDOW_H = int(os.getenv("BUILDER_WINDOW_H", "24"))
LIST_SCAN_LIMIT = int(os.getenv("BUILDER_LIST_SCAN_LIMIT", "200"))
INCLUDE_REPLIES = os.getenv("BUILDER_INCLUDE_REPLIES", "0") == "1"

FEISHU_WEBHOOK = os.getenv("FEISHU_WEBHOOK", "")

write_log = make_logger(LOG_FILE, JSONL_FILE)
save_pending = make_pending_saver(CACHE_FILE)
_PROXY = os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY")


def _proxy_ok() -> bool:
    """代理预检 + 端口/TUN 自愈，实现见 shared/bot_utils.proxy_ok。"""
    global _PROXY
    ok, _PROXY = proxy_ok(_PROXY, None)
    if ok and _PROXY:
        os.environ["HTTP_PROXY"] = _PROXY
        os.environ["HTTPS_PROXY"] = _PROXY
    return ok


def _load_plist_env_if_needed() -> None:
    """直接运行 test-list / archive-only 时从可选的 .env 或 macOS LaunchAgent plist 补全环境变量（不覆盖已存在变量）。"""
    global _PROXY, FEISHU_WEBHOOK, PER_BUILDER_CAP, WINDOW_H, LIST_SCAN_LIMIT, INCLUDE_REPLIES
    for env_path in (BASE / ".env", BASE.parent / "shared" / ".env", BASE.parent / ".env"):
        if env_path.exists():
            try:
                for line in env_path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    k = k.strip().removeprefix("export ").strip()
                    v = v.strip().strip('"').strip("'")
                    if k and k not in os.environ:
                        os.environ[k] = v
            except Exception:
                pass
    plist_path = _resolve_plist_path()
    if plist_path and plist_path.exists():
        try:
            with plist_path.open("rb") as fh:
                data = plistlib.load(fh)
            env_map = data.get("EnvironmentVariables") or {}
            for k, v in env_map.items():
                if isinstance(k, str) and v is not None and k not in os.environ:
                    os.environ[k] = str(v)
        except Exception as exc:
            print(f"[WARN] 读取 plist 环境变量失败: {exc}", file=sys.stderr)
    _PROXY = os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY") or _PROXY
    FEISHU_WEBHOOK = os.getenv("FEISHU_WEBHOOK", FEISHU_WEBHOOK)
    PER_BUILDER_CAP = int(os.getenv("BUILDER_PER_ACCOUNT_CAP", str(PER_BUILDER_CAP)))
    WINDOW_H = int(os.getenv("BUILDER_WINDOW_H", str(WINDOW_H)))
    LIST_SCAN_LIMIT = int(os.getenv("BUILDER_LIST_SCAN_LIMIT", str(LIST_SCAN_LIMIT)))
    INCLUDE_REPLIES = os.getenv("BUILDER_INCLUDE_REPLIES", "0") == "1"


def _iso(s: str):
    """ISO8601 或 Twitter RFC2822 时间字符串转 aware datetime；解析失败返回 None。"""
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        pass
    try:
        dt = parsedate_to_datetime(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


TWITTERAPI_IO_BASE = "https://api.twitterapi.io"


def _parse_list_id(raw: str) -> int | None:
    """支持纯数字 List ID 或完整 URL（https://x.com/i/lists/<id>）。"""
    val = (raw or "").strip()
    if not val:
        return None
    if val.isdigit():
        return int(val)
    m = re.search(r"/lists/(\d+)", val)
    return int(m.group(1)) if m else None


_TCO_RE = re.compile(r"https?://t\.co/[A-Za-z0-9]+")
_X_ARTICLE_URL_RE = re.compile(r"https?://(?:www\.)?(?:x|twitter)\.com/i/article/\d+\S*", re.IGNORECASE)
_SKIP_EXT_HOSTS = (
    "x.com", "twitter.com", "t.co", "twimg.com",
    "youtube.com", "youtu.be", "instagram.com", "tiktok.com",
)
_SKIP_EXT_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".mp4", ".mov", ".pdf", ".zip")


def _twitterapi_proxies() -> dict[str, str] | None:
    if not _PROXY:
        return None
    return {"http": _PROXY, "https": _PROXY}


def _twitterapi_url_pairs(tweet_obj: dict) -> list[tuple[str, str]]:
    """提取 twitterapi.io 推文 entities.urls 中的 (tco_url, expanded_url) 对。"""
    entities = tweet_obj.get("entities") or {}
    urls = entities.get("urls") or []
    pairs: list[tuple[str, str]] = []
    for u in urls:
        if not isinstance(u, dict):
            continue
        tco = (u.get("url") or "").strip()
        dest = (u.get("expanded_url") or u.get("expandedUrl") or "").strip()
        if tco or dest:
            pairs.append((tco, dest))
    return pairs


def _expand_text_twitterapi(raw_text: str, url_pairs: list[tuple[str, str]], *, strip_x_article: bool = False) -> str:
    text = (raw_text or "").strip()
    for tco, dest in url_pairs:
        if tco and dest and tco in text:
            text = text.replace(tco, dest)
    text = _TCO_RE.sub("", text)
    if strip_x_article:
        text = _X_ARTICLE_URL_RE.sub("", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return "\n".join(ln.rstrip() for ln in text.splitlines()).strip()


def _extract_ext_urls_twitterapi(tweet_obj: dict) -> list[str]:
    sources = [tweet_obj]
    qt = tweet_obj.get("quoted_tweet") or tweet_obj.get("quotedTweet")
    if isinstance(qt, dict):
        sources.append(qt)

    seen: set[str] = set()
    out: list[str] = []
    for obj in sources:
        for _, dest in _twitterapi_url_pairs(obj):
            if not dest.startswith(("http://", "https://")):
                continue
            try:
                sp = urlsplit(dest)
                host = (sp.hostname or "").lower()
                path_low = (sp.path or "").lower()
            except Exception:
                continue
            if not host or any(host == h or host.endswith("." + h) for h in _SKIP_EXT_HOSTS):
                continue
            if path_low.endswith(_SKIP_EXT_SUFFIXES):
                continue
            uk = url_key(dest)
            if uk and uk not in seen:
                seen.add(uk)
                out.append(dest)
    return out


def _has_x_article_twitterapi(tweet_obj: dict) -> bool:
    if isinstance(tweet_obj.get("article"), dict) and tweet_obj["article"]:
        return True
    for _, dest in _twitterapi_url_pairs(tweet_obj):
        if _X_ARTICLE_URL_RE.search(dest):
            return True
    return False


def _extract_article_text_twitterapi(article_obj: dict | None) -> tuple[str, str]:
    """从 twitterapi.io 的 article 对象提取 (title, full_text)。"""
    if not isinstance(article_obj, dict):
        return "", ""
    title = (article_obj.get("title") or "").strip()
    paras: list[str] = []
    contents = article_obj.get("contents")
    if isinstance(contents, list):
        for blk in contents:
            if isinstance(blk, dict):
                t = (blk.get("text") or "").strip()
                if t:
                    paras.append(t)
            elif isinstance(blk, str) and blk.strip():
                paras.append(blk.strip())
    if not paras:
        cs = article_obj.get("contentState") or article_obj.get("content_state")
        blocks = cs.get("blocks") if isinstance(cs, dict) else None
        if isinstance(blocks, list):
            for blk in blocks:
                if isinstance(blk, dict):
                    t = (blk.get("text") or "").strip()
                    if t:
                        paras.append(t)
    if paras:
        return title, "\n".join(paras)
    fallback = (
        article_obj.get("body")
        or article_obj.get("text")
        or article_obj.get("preview_text")
        or article_obj.get("previewText")
        or ""
    )
    return title, str(fallback).strip()


def _format_tweet_text_twitterapi(tweet_obj: dict) -> str:
    has_art = _has_x_article_twitterapi(tweet_obj)
    art = tweet_obj.get("article") if isinstance(tweet_obj.get("article"), dict) else None
    text = _expand_text_twitterapi(
        tweet_obj.get("text") or tweet_obj.get("fullText") or "",
        _twitterapi_url_pairs(tweet_obj),
        strip_x_article=has_art,
    )
    if art:
        a_title = (art.get("title") or "").strip()
        a_preview = (art.get("preview_text") or art.get("previewText") or "").strip()
        header = f"[X Article: {a_title}]" if a_title else "[X Article]"
        if a_preview and a_preview not in text:
            preview_snip = a_preview.replace("\n", " ")[:240]
            if text:
                text = f"{header} {text} | 导语：{preview_snip}".strip()
            else:
                text = f"{header} 导语：{preview_snip}".strip()
        elif a_title:
            text = f"{header} {text}".strip()

    qt = tweet_obj.get("quoted_tweet") or tweet_obj.get("quotedTweet")
    if isinstance(qt, dict):
        q_author = qt.get("author") or {}
        q_user = (q_author.get("userName") or q_author.get("username") or "").lstrip("@") or "unknown"
        q_id = str(qt.get("id") or "").strip()
        q_url = (qt.get("url") or qt.get("twitterUrl") or "").strip()
        if not q_url and q_user != "unknown" and q_id:
            q_url = f"https://x.com/{q_user}/status/{q_id}"
        q_text = _expand_text_twitterapi(
            qt.get("text") or qt.get("fullText") or "",
            _twitterapi_url_pairs(qt),
            strip_x_article=_has_x_article_twitterapi(qt),
        ).replace("\n", " ").strip()[:280]
        if q_text or q_url:
            text = f"{text}\n[引用 @{q_user} ({q_url})]: {q_text}".strip()
    return text


_TWITTERAPI_RETRY_STATUSES = {429, 500, 502, 503, 504}


def _twitterapi_get(
    path: str,
    *,
    params: dict,
    api_key: str,
    timeout: int = 25,
    max_attempts: int = 4,
) -> requests.Response:
    """带短暂重试的 twitterapi.io GET 请求，应对上游瞬时 503/429（不计费）。"""
    url = f"{TWITTERAPI_IO_BASE}{path}"
    headers = {"X-API-Key": api_key}
    proxies = _twitterapi_proxies()
    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            resp = requests.get(
                url,
                params=params,
                headers=headers,
                proxies=proxies,
                timeout=timeout,
            )
            if resp.status_code in _TWITTERAPI_RETRY_STATUSES and attempt < max_attempts:
                delay = 2 * (2 ** (attempt - 1))
                print(
                    f"  [WARN] twitterapi.io HTTP {resp.status_code}（第 {attempt}/{max_attempts} 次），{delay}s 后重试...",
                    file=sys.stderr,
                )
                time.sleep(delay)
                continue
            return resp
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < max_attempts:
                delay = 2 * (2 ** (attempt - 1))
                print(
                    f"  [WARN] twitterapi.io 网络异常（第 {attempt}/{max_attempts} 次）：{exc}，{delay}s 后重试...",
                    file=sys.stderr,
                )
                time.sleep(delay)
                continue
            raise
    if last_exc:
        raise last_exc
    raise RuntimeError("twitterapi.io 请求失败")


def _fetch_twitterapi_article(tweet_id: str, api_key: str) -> tuple[str, str]:
    resp = _twitterapi_get(
        "/twitter/article",
        params={"tweet_id": str(tweet_id)},
        api_key=api_key,
        timeout=20,
        max_attempts=3,
    )
    resp.raise_for_status()
    payload = resp.json()
    art = payload.get("article") if isinstance(payload, dict) else None
    if not isinstance(art, dict) and isinstance(payload, dict) and (payload.get("contents") or payload.get("title")):
        art = payload
    return _extract_article_text_twitterapi(art)


def _fetch_x_list_twitterapi(
    list_id: int,
    sent_before: set,
    *,
    api_key: str,
    ledger_x_ids: set | None = None,
    enrich_details: bool = True,
) -> tuple[list, dict]:
    """通过 twitterapi.io /twitter/list/tweets 抓取公开 X List 过去 WINDOW_H 小时推文（零封号风险）。"""
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=WINDOW_H)
    since_ts = int(cutoff.timestamp())

    by_author: dict[str, list[dict]] = {}
    seen_in_run: set[str] = set()
    seen_cursors: set[str] = set()
    dropped_rt = dropped_reply = dropped_old = dropped_dup = 0
    old_streak = 0
    scanned = 0
    cursor = ""
    max_pages = max(1, (LIST_SCAN_LIMIT + 19) // 20)

    for _ in range(max_pages):
        params: dict[str, str | int] = {
            "listId": str(list_id),
            "sinceTime": since_ts,
            "includeReplies": "true" if INCLUDE_REPLIES else "false",
        }
        if cursor:
            params["cursor"] = cursor

        resp = _twitterapi_get(
            "/twitter/list/tweets",
            params=params,
            api_key=api_key,
            timeout=25,
            max_attempts=4,
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"twitterapi.io HTTP {resp.status_code}: {resp.text[:240]}")
        data = resp.json()
        if not isinstance(data, dict):
            raise RuntimeError(f"twitterapi.io 返回非 JSON 对象: {str(data)[:200]}")
        if str(data.get("status", "")).lower() == "error":
            raise RuntimeError(f"twitterapi.io 错误: {data.get('msg') or data.get('message') or data}")

        tweets = data.get("tweets") or []
        if not tweets:
            break

        stop_early = False
        for t in tweets:
            if not isinstance(t, dict):
                continue
            scanned += 1
            if scanned > LIST_SCAN_LIMIT:
                stop_early = True
                break

            t_date = _iso(str(t.get("createdAt") or ""))
            if t_date is not None and t_date < cutoff:
                dropped_old += 1
                old_streak += 1
                if old_streak >= 8:
                    stop_early = True
                    break
                continue
            old_streak = 0

            raw_text = (t.get("text") or t.get("fullText") or "").strip()
            if t.get("retweeted_tweet") or t.get("retweetedTweet") or raw_text.startswith("RT @"):
                dropped_rt += 1
                continue

            author = t.get("author") or {}
            handle = (author.get("userName") or author.get("username") or "").lstrip("@").strip()
            if not handle:
                continue

            is_reply = bool(t.get("isReply") or t.get("inReplyToId"))
            if not INCLUDE_REPLIES and is_reply:
                reply_user = (t.get("inReplyToUsername") or "").lstrip("@").strip()
                if reply_user.lower() != handle.lower():
                    dropped_reply += 1
                    continue

            t_id = str(t.get("id") or "").strip()
            if not t_id:
                continue
            url = f"https://x.com/{handle}/status/{t_id}"
            uk = url_key(url)
            if (
                uk in sent_before
                or uk in seen_in_run
                or (ledger_x_ids and t_id in ledger_x_ids)
            ):
                dropped_dup += 1
                continue
            seen_in_run.add(uk)

            has_art = _has_x_article_twitterapi(t)
            art_obj = t.get("article") if isinstance(t.get("article"), dict) else None
            art_title = (art_obj.get("title") or "").strip() if art_obj else ""
            art_has_blocks = bool(art_obj and (art_obj.get("contents") or (isinstance(art_obj.get("contentState"), dict) and art_obj["contentState"].get("blocks"))))
            _, initial_full = _extract_article_text_twitterapi(art_obj) if art_has_blocks else ("", "")

            by_author.setdefault(handle, []).append({
                "id": int(t_id) if t_id.isdigit() else t_id,
                "handle": handle,
                "name": (author.get("name") or handle).strip(),
                "bio": (author.get("description") or "").replace("\n", " ").strip()[:80],
                "url": url,
                "text": _format_tweet_text_twitterapi(t),
                "createdAt": (t_date or now).astimezone(timezone.utc).isoformat(),
                "likes": int(t.get("likeCount") or t.get("favoriteCount") or 0),
                "retweets": int(t.get("retweetCount") or 0),
                "replies": int(t.get("replyCount") or 0),
                "isQuote": bool(t.get("quoted_tweet") or t.get("quotedTweet")),
                "has_article": has_art,
                "article_title": art_title,
                "article_full_text": initial_full[:15000] if art_has_blocks else "",
                "ext_urls": _extract_ext_urls_twitterapi(t),
                "ext_excerpts": [],
            })

        if stop_early:
            break

        next_cursor = (data.get("next_cursor") or data.get("nextCursor") or "").strip()
        has_next = bool(data.get("has_next_page", data.get("hasNextPage", bool(next_cursor))))
        if not has_next or not next_cursor or next_cursor in seen_cursors:
            break
        seen_cursors.add(next_cursor)
        cursor = next_cursor

    rows: list[dict] = []
    dropped_cap = 0
    for handle_rows in by_author.values():
        handle_rows.sort(key=lambda x: -x["likes"])
        rows.extend(handle_rows[:PER_BUILDER_CAP])
        if len(handle_rows) > PER_BUILDER_CAP:
            dropped_cap += len(handle_rows) - PER_BUILDER_CAP
    rows.sort(key=lambda x: -x["likes"])

    enriched_articles = 0
    enriched_blogs = 0
    if enrich_details and rows:
        article_rows = [r for r in rows if r.get("has_article") and not r.get("article_full_text")][:8]
        for r in article_rows:
            try:
                a_title, full_art = _fetch_twitterapi_article(str(r["id"]), api_key)
                if full_art:
                    r["article_full_text"] = full_art[:15000]
                    if a_title and not r.get("article_title"):
                        r["article_title"] = a_title
                    enriched_articles += 1
            except Exception as exc:
                print(f"  [WARN] 拉取 X Article 详情失败 ({r['url']}): {exc}", file=sys.stderr)

        ext_tasks: list[tuple[dict, str]] = []
        seen_ext: set[str] = set()
        for r in rows:
            for u in r.get("ext_urls") or []:
                uk = url_key(u)
                if uk and uk not in seen_ext:
                    seen_ext.add(uk)
                    ext_tasks.append((r, u))
                if len(ext_tasks) >= 15:
                    break
            if len(ext_tasks) >= 15:
                break

        if ext_tasks:
            with ThreadPoolExecutor(max_workers=5) as ex:
                fut_map = [
                    (r, u, ex.submit(fetch_article_text, u, 10, 4000, 180))
                    for r, u in ext_tasks
                ]
                for r, u, fut in fut_map:
                    try:
                        txt = (fut.result(timeout=12) or "").strip()
                        if txt:
                            r["ext_excerpts"].append((u, txt))
                            enriched_blogs += 1
                    except Exception:
                        pass

    stats = {
        "backend": "twitterapi.io",
        "list_id": list_id,
        "scanned": scanned,
        "builders": len(by_author),
        "dropped_rt": dropped_rt,
        "dropped_reply": dropped_reply,
        "dropped_old": dropped_old,
        "dropped_dup": dropped_dup,
        "dropped_cap": dropped_cap,
        "enriched_articles": enriched_articles,
        "enriched_blogs": enriched_blogs,
        "fetched_at": now,
    }
    return rows, stats


def _fetch_x_list(
    list_id: int,
    sent_before: set,
    *,
    ledger_x_ids: set | None = None,
    enrich_details: bool = True,
) -> tuple[list, dict]:
    """通过 twitterapi.io 抓取自建 X List 过去 WINDOW_H 小时推文。"""
    api_key = (os.getenv("TWITTERAPI_IO_KEY") or "").strip()
    if not api_key:
        raise RuntimeError(
            "未配置 TWITTERAPI_IO_KEY：请在环境变量、.env 或 LaunchAgent plist 中填入 TWITTERAPI_IO_KEY"
        )
    return _fetch_x_list_twitterapi(
        list_id,
        sent_before,
        api_key=api_key,
        ledger_x_ids=ledger_x_ids,
        enrich_details=enrich_details,
    )


def _format_context(rows: list[dict], stats: dict) -> str:
    """格式化输出给写稿模型的 X List 素材上下文。"""
    times = [_iso(x["createdAt"]) for x in rows]
    times = [t for t in times if t]
    span = (
        f"{min(times).strftime('%Y-%m-%d %H:%M')} ~ "
        f"{max(times).strftime('%Y-%m-%d %H:%M')} UTC"
    ) if times else "无"

    fetched_at = stats["fetched_at"]
    backend_label = stats.get("backend") or "x_list"
    lines = [
        f"【抓取模式】X List 直连 / {backend_label}（List ID: {stats['list_id']}，回看窗口 {WINDOW_H}h，抓取时间 {fetched_at.strftime('%Y-%m-%d %H:%M')} UTC）",
        f"【推文实际时间区间】{span}",
        f"【本次可用推文】{len(rows)} 条（活跃 {stats['builders']} 位 builder｜"
        f"共扫描 {stats['scanned']} 条，过滤转推 {stats['dropped_rt']}、回复 {stats['dropped_reply']}、"
        f"超窗 {stats['dropped_old']}、已播/前序去重 {stats['dropped_dup']}、单人上限 {stats['dropped_cap']}｜"
        f"深抓 X 长文 {stats.get('enriched_articles', 0)} 篇、外链正文 {stats.get('enriched_blogs', 0)} 篇）",
        "",
        "推文的 赞/转/回 是 X 上的真实互动量，可以照实写。",
        "",
    ]

    for i, x in enumerate(rows, 1):
        when = _iso(x["createdAt"])
        when_s = when.strftime("%m-%d %H:%M UTC") if when else x["createdAt"]
        lines.append(f"--- {i}. @{x['handle']}（{x['name']}｜{x['bio']}）")
        lines.append(
            f"[时间] {when_s}"
            f"　[赞] {x['likes']}　[转] {x['retweets']}　[回] {x['replies']}"
            + ("　[引用推]" if x["isQuote"] else "")
            + ("　[X长文]" if x.get("has_article") else "")
        )
        lines.append(f"[原推] {x['url']}")
        lines.append(f"[正文] {x['text']}")
        if x.get("article_full_text"):
            a_title = x.get("article_title") or "X Article"
            lines.append(f"[X Article 全文摘录（《{a_title}》，直接归属上面的 [原推] 链接，正文勿另挂 x.com/i/article 链接）]:")
            lines.append(x["article_full_text"])
        for ext_u, ext_txt in x.get("ext_excerpts") or []:
            lines.append(f"[外链技术博客/文档链接] {ext_u}")
            lines.append(f"[外链正文摘录]:\n{ext_txt}")
        lines.append("")

    return "\n".join(lines)


def gather() -> tuple[str, dict]:
    """拉取自建 X List 过去 WINDOW_H 小时推文，返回 (context, metrics)。"""
    list_id = _parse_list_id(os.getenv("X_LIST_ID", ""))
    if list_id is None:
        raise RuntimeError(
            "未配置 X_LIST_ID：请在环境变量、.env 或 LaunchAgent plist 中填入 X_LIST_ID"
        )

    ledger = load_daily_ledger(exclude_bot="🛠️ AI Builder")
    sent_before = load_sent_urls(SENT_URLS) | ledger["urls"]
    rows, stats = _fetch_x_list(
        list_id,
        sent_before,
        ledger_x_ids=ledger["x_status_ids"],
        enrich_details=True,
    )
    fetched_at = stats["fetched_at"]
    context = _format_context(rows, stats)
    ledger_block = format_ledger_context(exclude_bot="🛠️ AI Builder")
    if ledger_block:
        context = f"{ledger_block}\n\n{context}"
    metrics = {
        "source_mode": "x_list",
        "list_id": list_id,
        "tweets": len(rows),
        "builders": stats["builders"],
        "dropped_rt": stats["dropped_rt"],
        "dropped_reply": stats["dropped_reply"],
        "dropped_old": stats["dropped_old"],
        "dropped_dup": stats["dropped_dup"],
        "dropped_cap": stats["dropped_cap"],
        "enriched_articles": stats.get("enriched_articles", 0),
        "enriched_blogs": stats.get("enriched_blogs", 0),
        "feed_lag_h": 0.0,
        "feed_generated_at": fetched_at.isoformat(),
    }
    return context, metrics


# ===== 模式 1：fetch =====

def run_fetch() -> int:
    if already_ran_today(LOG_FILE):
        print("=== SKIP_ALREADY_RAN ===")
        return 0

    if not _proxy_ok():
        print(f"=== SKIP_PROXY === {_PROXY}")
        return 0

    try:
        context, metrics = gather()
    except Exception as e:
        print("=== FEED_ERROR ===")
        print(f"拉取 X List 失败：{e}")
        write_log("WARN", f"X List 拉取失败，未发送：{e}")
        return 0

    if metrics["tweets"] == 0:
        print("=== NO_TWEETS ===")
        write_log("WARN", "X List 窗口内没有可用的新推文，未发送")
        return 0

    FETCH_META.write_text(json.dumps(
        {"log_summary": f"builder {metrics['builders']} 位 / 推文 {metrics['tweets']} 条（X List 直连）",
         "fetch_date": datetime.now().strftime("%Y-%m-%d"),
         "metrics": metrics}, ensure_ascii=False), encoding="utf-8")

    out = [
        "=== FETCH_OK ===",
        f"今天日期：{datetime.now().strftime('%Y-%m-%d')}",
        "=== CONTEXT_BEGIN ===",
        context,
        "=== CONTEXT_END ===",
    ]
    emit_fetch_output(out, LAST_CONTEXT)
    return 0


# ===== 模式 2：test-list =====

def run_test_list() -> int:
    """只读测试 twitterapi.io API Key 与 X List 连通性，不写日志也不改去重记录。"""
    _load_plist_env_if_needed()
    list_id = _parse_list_id(os.getenv("X_LIST_ID", ""))
    if list_id is None:
        print("ERROR: 未检测到有效的 X_LIST_ID（请在环境变量或 LaunchAgent plist 中设置）", file=sys.stderr)
        return 2
    if not _proxy_ok():
        print(f"ERROR: 代理不可用：{_PROXY}", file=sys.stderr)
        return 2

    rows, stats = _fetch_x_list(list_id, set(), enrich_details=False)
    print(
        f"✓ X List {list_id} 测试成功（{stats.get('backend', 'x_list')}）：扫描 {stats['scanned']} 条，"
        f"近 {WINDOW_H}h 命中 {stats['builders']} 位 builder 共 {len(rows)} 条可用推文"
        f"（过滤转推 {stats['dropped_rt']}、回复 {stats['dropped_reply']}、超窗 {stats['dropped_old']}）"
    )
    for idx, r in enumerate(rows[:5], 1):
        snippet = r["text"].replace("\n", " ")[:110]
        print(f"  {idx}. @{r['handle']} ({r['likes']}赞) {r['url']} -> {snippet}")
    return 0


# ===== 模式 3：send =====

def run_send(draft_path: Path) -> int:
    return send_x_linked_report(
        draft_path,
        log_file=LOG_FILE, cache_file=CACHE_FILE, fetch_meta=FETCH_META,
        sent_urls=SENT_URLS, webhook=FEISHU_WEBHOOK,
        write_log=write_log, save_pending=save_pending,
        link_label="原推")


# ===== 模式 4：archive-only =====

def run_archive_only(draft_path: Path) -> int:
    """只读取并清洗草稿，写入本地归档 + 同步 Notion + 记录去重，不推送飞书。"""
    if not draft_path.exists():
        print(f"ERROR: 找不到草稿文件：{draft_path}", file=sys.stderr)
        return 1
    raw = draft_path.read_text(encoding="utf-8")
    report = sanitize_html(raw).strip()
    if not report:
        print("ERROR: 草稿内容为空", file=sys.stderr)
        return 1

    hrefs = extract_hrefs(report)
    x_links = [u for u in hrefs if X_POST_RE.search(u)]
    if not x_links:
        msg = "稿件里没有任何 x.com 原帖链接（格式须为 x.com/<user>/status/<id>），拒绝归档"
        print(f"=== ARCHIVE_FAIL === {msg}", file=sys.stderr)
        write_log("FAIL", msg)
        return 1

    doc_date = extract_report_date(report) or datetime.now().strftime("%Y-%m-%d")
    meta_summary = ""
    if FETCH_META.exists():
        try:
            meta = json.loads(FETCH_META.read_text(encoding="utf-8"))
            if meta.get("fetch_date") == doc_date:
                meta_summary = (meta.get("log_summary") or "").strip()
        except Exception:
            pass

    _proxy_ok()
    arch = archive_and_sync_notion(
        report,
        "🛠️ AI Builder",
        date_str=doc_date,
        archive_dir=LOG_FILE.parent / "archive",
        archive_filename=f"{doc_date}.txt",
    )
    arch_json = json.dumps(arch, ensure_ascii=False, default=str) if isinstance(arch, dict) else str(arch)
    print(f"ARCHIVE_RESULT: {arch_json}")

    notion_ok = bool(arch.get("notion_ok")) if isinstance(arch, dict) else False
    notion_detail = (
        str(arch.get("notion_url") or arch.get("notion_err") or "")
        if isinstance(arch, dict)
        else ""
    )

    if not notion_ok:
        prefix = f"{meta_summary} | " if meta_summary else ""
        err_msg = f"{prefix}Notion 单独归档失败 | Notion ✗ ({notion_detail or arch_json})"
        write_log("WARN", err_msg)
        print(f"=== ARCHIVE_FAIL === {err_msg}", file=sys.stderr)
        return 1

    record_sent_urls(SENT_URLS, x_links)
    record_daily_ledger("🛠️ AI Builder", report, hrefs, date_str=doc_date)
    prefix = f"{meta_summary} | " if meta_summary else ""
    status_line = f"{prefix}Notion 单独归档成功 | Notion ✓ {notion_detail}".strip()
    write_log("OK", status_line)
    print(f"=== ARCHIVE_OK === {status_line}")
    return 0


if __name__ == "__main__":
    _load_plist_env_if_needed()
    parser = argparse.ArgumentParser(description="AI Builder 日报")
    parser.add_argument("--mode", choices=["fetch", "send", "test-list", "archive-only"], required=True)
    parser.add_argument("--draft", default=str(DRAFT_FILE))
    args = parser.parse_args()
    if args.mode == "fetch":
        sys.exit(run_fetch())
    if args.mode == "test-list":
        sys.exit(run_test_list())
    if args.mode == "archive-only":
        sys.exit(run_archive_only(Path(args.draft)))
    sys.exit(run_send(Path(args.draft)))
