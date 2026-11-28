#!/usr/bin/python3
"""
GitHub Trending & Hacker News 早报

作为每日 09:00 `ai-daily` 四合一串行流水线的第 4 棒（1. AI 产业日报 → 2. X & Reddit → 3. AI Builder → 4. GitHub & HN）运行，
自动读取前序 3 棒已播账本做跨 Bot 去重，由 AI 写稿后推送飞书并同步归档至 Notion。
- 周一至周六（或 --since daily）：抓取 GitHub Daily 飙升榜（新星优先 + 熟面孔折叠）+ Hacker News 24h 热榜 Top 8，输出 === FETCH_OK ===
- 周日（或 --since weekly）：抓取 GitHub Weekly 周趋势榜 + Hacker News 24h 热榜 Top 8，输出 === WEEKLY_OK ===

跨天重复处理规则：
- 本地原子维护 logs/seen_history.json（记录过去 30 天**实际推送出稿**的仓库与 HN 帖子及其首次推送日期，绝不写入未发布的候选池条目）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "shared"))
from bot_utils import (
    already_ran_today,
    archive_and_sync_notion,
    atomic_json_update,
    emit_fetch_output,
    extract_hrefs,
    extract_report_date,
    format_ledger_context,
    load_daily_ledger,
    make_logger,
    make_pending_saver,
    proxy_ok,
    record_daily_ledger,
    redact_secrets,
    sanitize_html,
    send_feishu,
    validate_draft_freshness_and_identity,
)

import sources
from sources import url_key

BASE = Path(__file__).parent
LOG_FILE = BASE / "logs" / "run.log"
JSONL_FILE = BASE / "logs" / "run.jsonl"
LOG_FILE.parent.mkdir(exist_ok=True)
ARCHIVE_DIR = BASE / "logs" / "archive"
ARCHIVE_DIR.mkdir(exist_ok=True)

CACHE_FILE = BASE / "pending_messages.json"
DRAFT_FILE = BASE / "logs" / "report_draft.txt"
FETCH_META = BASE / "logs" / "fetch_meta.json"
LAST_CONTEXT = BASE / "logs" / "last_context.txt"
SEEN_HISTORY = BASE / "logs" / "seen_history.json"
HISTORY_KEEP_DAYS = 30

FEISHU_WEBHOOK = os.getenv("FEISHU_WEBHOOK", "")

write_log = make_logger(LOG_FILE, JSONL_FILE)
save_pending = make_pending_saver(CACHE_FILE)
_PROXY = os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY")
os.environ.setdefault("HTTP_PROXY", _PROXY or "")
os.environ.setdefault("HTTPS_PROXY", _PROXY or "")


def _proxy_ok() -> bool:
    global _PROXY
    ok, _PROXY = proxy_ok(_PROXY, None)
    return ok


def load_seen_history(path: Path) -> dict[str, str]:
    """读取历史已推送记录，返回 {url_key: 'MM月DD日'}（仅含今天之前的记录）。"""
    today_iso = datetime.now().strftime("%Y-%m-%d")
    cutoff_iso = (datetime.now() - timedelta(days=HISTORY_KEEP_DAYS)).strftime("%Y-%m-%d")
    result: dict[str, str] = {}
    if not path.exists():
        return result
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return result
        for k, v in data.items():
            if isinstance(v, dict):
                iso_d = v.get("iso_date", "")
                disp_d = v.get("display_date", "")
                nk = url_key(k)
                if nk and iso_d and iso_d >= cutoff_iso and iso_d < today_iso and disp_d:
                    result[nk] = disp_d
    except Exception:
        pass
    return result


def record_seen_history(path: Path, urls: list[str]) -> int:
    """将本次实际成功推送的 URL 原子加锁记录到 seen_history.json。若此前已有记录，保留最早推送日期。"""
    now = datetime.now()
    today_iso = now.strftime("%Y-%m-%d")
    today_disp = f"{now.month}月{now.day}日"
    cutoff_iso = (now - timedelta(days=HISTORY_KEEP_DAYS)).strftime("%Y-%m-%d")

    def _mutate(raw):
        data: dict[str, dict[str, str]] = {}
        if isinstance(raw, dict):
            for k, v in raw.items():
                if isinstance(v, dict) and v.get("iso_date", "") >= cutoff_iso:
                    nk = url_key(k)
                    if nk:
                        data[nk] = v
        for u in urls:
            k = url_key(u)
            if not k:
                continue
            if k not in data:
                data[k] = {
                    "iso_date": today_iso,
                    "display_date": today_disp,
                    "last_iso_date": today_iso,
                }
            else:
                data[k]["last_iso_date"] = today_iso
        return data

    try:
        saved = atomic_json_update(path, _mutate, indent=2)
        return len(saved)
    except Exception as exc:
        print(f"  [WARN] seen_history.json 写入失败（不影响主流程）：{exc}", file=sys.stderr)
        return 0


def run_fetch(since_override: str | None = None) -> int:
    if already_ran_today(LOG_FILE):
        emit_fetch_output(["=== SKIP_ALREADY_RAN ==="], LAST_CONTEXT)
        return 0

    if not _proxy_ok():
        write_log("WARN", f"代理不可用（{_PROXY}），跳过抓取")
        emit_fetch_output(["=== SKIP_PROXY ==="], LAST_CONTEXT)
        return 0

    now = datetime.now()
    today_str = f"{now.year}年{now.month}月{now.day}日"
    is_sunday = (now.weekday() == 6)

    since = since_override or os.getenv("GH_TRENDING_SINCE", "").strip().lower()
    if since not in ("daily", "weekly"):
        since = "weekly" if is_sunday else "daily"

    seen = load_seen_history(SEEN_HISTORY)
    ledger = load_daily_ledger(exclude_bot="🐙 GitHub & HN")
    today_ledger_urls = ledger["urls"]
    today_ledger_x_ids = ledger["x_status_ids"]
    gh_limit = 10 if since == "daily" else 12

    print(
        f"📡 正在并发抓取 GitHub Trending (since={since}, 新星优先) 与 Hacker News (Best+Top) Top 8...",
        file=sys.stderr,
    )

    gh_err = ""
    hn_err = ""
    gh_data: dict = {"since": since, "url": f"https://github.com/trending?since={since}", "repos": [], "fresh_repos": [], "recurring_repos": [], "raw_count": 0}
    hn_data: dict = {"items": [], "raw_count": 0}

    with ThreadPoolExecutor(max_workers=2) as pool:
        f_gh = pool.submit(sources.fetch_github_trending, since=since, limit=gh_limit, seen_history=seen)
        f_hn = pool.submit(
            sources.fetch_hn_top8,
            window_h=36,
            limit=8,
            seen_history=seen,
            today_ledger_urls=today_ledger_urls,
            today_ledger_x_ids=today_ledger_x_ids,
        )
        try:
            gh_data = f_gh.result(timeout=45)
        except Exception as exc:
            gh_err = str(exc)
            print(f"  ⚠️ GitHub Trending 抓取异常：{exc}", file=sys.stderr)
        try:
            hn_data = f_hn.result(timeout=45)
        except Exception as exc:
            hn_err = str(exc)
            print(f"  ⚠️ Hacker News 抓取异常：{exc}", file=sys.stderr)

    repos = gh_data.get("repos") or []
    fresh_repos = gh_data.get("fresh_repos") if "fresh_repos" in gh_data else [r for r in repos if not r.get("prev_date")]
    recurring_repos = gh_data.get("recurring_repos") if "recurring_repos" in gh_data else [r for r in repos if r.get("prev_date")]
    hn_items = hn_data.get("items") or []

    if not repos and not hn_items:
        write_log("WARN", "GitHub Trending 与 Hacker News 均未抓到有效条目")
        emit_fetch_output(["=== NO_DATA ==="], LAST_CONTEXT)
        return 0

    if not repos:
        write_log("WARN", f"GitHub Trending 单源降级为空（{gh_err or '解析0条'}），继续输出 HN")
    if not hn_items:
        write_log("WARN", f"Hacker News 单源降级为空（{hn_err or 'API返回0条'}），继续输出 GitHub")

    marker = "=== WEEKLY_OK ===" if since == "weekly" else "=== FETCH_OK ==="
    out: list[str] = [
        marker,
        "=== CONTEXT_BEGIN ===",
        f"今天日期：{today_str}",
        f"榜单模式：{'GitHub Weekly 本周趋势榜 + Hacker News (Best+Top) 热榜 Top 8' if since == 'weekly' else 'GitHub Daily 今日飙升榜（新星优先） + Hacker News (Best+Top) 热榜 Top 8'}",
        f"GitHub 源地址：{gh_data['url']}",
    ]
    if not repos:
        out.append("⚠️ 【单源降级声明】今日 GitHub Trending 抓取失败/为空，写稿时请如实注明「今日 GitHub Trending 暂不可用」，严禁编造仓库。")
    if not hn_items:
        out.append("⚠️ 【单源降级声明】今日 Hacker News API 抓取失败/为空，写稿时请如实注明「今日 Hacker News 暂不可用」，严禁编造热帖。")

    ledger_block = format_ledger_context(exclude_bot="🐙 GitHub & HN")
    if ledger_block:
        out.extend(["", ledger_block])

    out.extend([
        "",
        "==========================================",
        f"【第一部分：GitHub Trending ({since.upper()}) · 今日新面孔 {len(fresh_repos)} 个 + 持续霸榜熟面孔 {len(recurring_repos)} 个】",
        "==========================================",
    ])

    repeat_repos = len(recurring_repos)
    if fresh_repos:
        out.append("\n>>> 🆕 新上榜项目（请完整展开：场景定位 / 适用建议 / 它是干嘛的 / 个人 Build 姿势）")
        for r in fresh_repos:
            in_ledger = url_key(r["url"]) in today_ledger_urls
            tag_note = f"（来源：{r['source_tag']}）" if r.get("source_tag") else ""
            status_str = (
                "🔁 今日 09:00 前序 AI 日报已播（建议 1 句话简述并注明详见今日前序日报）"
                if in_ledger else f"🆕 首次上榜{tag_note}"
            )
            if in_ledger:
                repeat_repos += 1
            out.append(f"\n[GitHub 新星 #{r['rank']}] {r['repo']}")
            out.append(f"- 链接：{r['url']}")
            out.append(f"- 状态：{status_str}")
            out.append(f"- 语言：{r['language']} | 总 Star：{r['total_stars']} | Fork：{r['forks']} | 周期增量：{r['period_stars']}")
            out.append(f"- 官方简介：{r['description'] or '（无简介）'}")
            if r.get("readme_excerpt"):
                out.append(f"- README 详情摘录：{r['readme_excerpt']}")

    if recurring_repos:
        out.append("\n>>> 🔁 持续霸榜熟面孔（请统一收进「🔁 持续霸榜熟面孔」或周榜「🔥 持续霸榜」区块，每条 1 句话简述并注明首次推送日期，绝不反复写长文）")
        for r in recurring_repos:
            out.append(
                f"- {r['repo']} | {r['url']} | 语言：{r['language']} | 总 Star：{r['total_stars']} | 周期增量：{r['period_stars']} | 首次推送：{r['prev_date']} | 简介：{r['description'] or '（无简介）'}"
            )

    out.extend([
        "",
        "==========================================",
        f"【第二部分：Hacker News (Best高赞 + Top实时热议) 候选池 Top {len(hn_items)}（从中精选 Top 8，若遇今日前序已播条目优先用下方备选新帖递补或 1 句话折叠）】",
        "==========================================",
    ])

    repeat_hn = 0
    hn_url_pairs: dict[str, list[str]] = {}
    for idx, item in enumerate(hn_items, 1):
        if item.get("prev_date"):
            status_str = f"🔁 重复在榜（首次见于 {item['prev_date']} 推送）"
            repeat_hn += 1
        elif item.get("in_today_ledger"):
            status_str = "🔁 今日 09:00 前序 AI 日报已播（优先用下方备选新帖替换，或 1 句话简述注明「详见今日 AI 日报」）"
            repeat_hn += 1
        else:
            status_str = "🆕 首次上榜"
        out.append(f"\n[HN #{idx}] {item['title']}")
        out.append(f"- 原文链接：{item['url']}")
        out.append(f"- HN 讨论串：{item['hn_url']}")
        out.append(f"- 榜单来源：{item.get('badge', '热榜')}")
        out.append(f"- 状态：{status_str}")
        out.append(f"- 热度：{item['points']} points | {item['num_comments']} 条评论 | 发布者：{item['author']}")
        if item["article_excerpt"]:
            out.append(f"- 原文内容摘要：{item['article_excerpt']}")
        if item["top_comments"]:
            out.append("- 评论区高赞观点：")
            for c in item["top_comments"]:
                out.append(f"  * {c}")
        pair = [item["hn_url"], item["url"]]
        for u in pair:
            uk = url_key(u)
            if uk:
                hn_url_pairs[uk] = pair

    out.append("=== CONTEXT_END ===")

    meta = {
        "date_str": now.strftime("%Y-%m-%d"),
        "fetch_date": now.strftime("%Y-%m-%d"),
        "log_summary": f"GitHub({since}) 新{len(fresh_repos)}+熟{len(recurring_repos)} + HN候选{len(hn_items)}条(含重{repeat_hn})",
        "metrics": {
            "since": since,
            "gh_count": len(repos),
            "gh_fresh": len(fresh_repos),
            "gh_repeat": repeat_repos,
            "hn_count": len(hn_items),
            "hn_repeat": repeat_hn,
            "hn_url_pairs": hn_url_pairs,
        },
    }
    FETCH_META.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    emit_fetch_output(out, LAST_CONTEXT)
    return 0


def run_send(since_override: str | None = None) -> int:
    t0 = time.time()
    if already_ran_today(LOG_FILE):
        print("今天已成功运行过，跳过发送。如需强制请设置 FORCE_RUN=1。", file=sys.stderr)
        return 0

    if not DRAFT_FILE.exists():
        write_log("FAIL", f"稿子文件不存在：{DRAFT_FILE}")
        return 1

    report = DRAFT_FILE.read_text(encoding="utf-8").strip()
    if not report:
        write_log("FAIL", f"稿子文件为空：{DRAFT_FILE}")
        return 1

    report = sanitize_html(report)
    ok_gate, gate_err = validate_draft_freshness_and_identity(
        DRAFT_FILE,
        report,
        fetch_meta_path=FETCH_META,
        webhook=FEISHU_WEBHOOK,
        expected_title_markers=("GitHub", "Hacker News"),
        require_hrefs=True,
    )
    if not ok_gate:
        write_log("FAIL", gate_err)
        return 1

    hrefs = extract_hrefs(report)

    save_pending([report])
    print("📨 推送到飞书...", file=sys.stderr)
    try:
        sent = send_feishu(report, FEISHU_WEBHOOK)
    except Exception as exc:
        err = redact_secrets(f"飞书推送失败：{exc}")
        write_log("FAIL", err)
        print(f"  ✗ {err}", file=sys.stderr)
        return 1
    print(f"  ✓ 推送成功（{sent} 条）", file=sys.stderr)
    CACHE_FILE.unlink(missing_ok=True)

    try:
        meta = json.loads(FETCH_META.read_text(encoding="utf-8"))
    except Exception:
        meta = {"log_summary": "", "metrics": {}}

    since = (
        since_override
        or os.getenv("GH_TRENDING_SINCE", "").strip().lower()
        or str(meta.get("metrics", {}).get("since", "")).strip().lower()
    )
    if since not in ("daily", "weekly"):
        since = "weekly" if datetime.now().weekday() == 6 else "daily"
    is_weekly = (since == "weekly")

    doc_date = extract_report_date(report) or meta.get("date_str") or datetime.now().strftime("%Y-%m-%d")
    notion_meta = archive_and_sync_notion(
        report,
        "🐙 GitHub & HN",
        date_str=doc_date,
        archive_dir=ARCHIVE_DIR,
        archive_filename=f"{doc_date}.txt",
        report_type="周盘点" if is_weekly else "日报",
    )
    notion_tag = " | Notion ✓" if notion_meta.get("notion_ok") else " | Notion ⚠️"

    # 仅记录实际出现在成稿中的 hrefs 及其配对的 HN/外链 URL，绝不把未发布的备选池 #9-#12 污染进 seen_history.json
    hn_url_pairs = meta.get("metrics", {}).get("hn_url_pairs", {}) or {}
    published_urls: list[str] = []
    for u in hrefs:
        published_urls.append(u)
        uk = url_key(u)
        if uk in hn_url_pairs:
            published_urls.extend(hn_url_pairs[uk])

    total_seen = record_seen_history(SEEN_HISTORY, published_urls)
    record_daily_ledger("🐙 GitHub & HN", report, hrefs, date_str=doc_date)
    print(f"  ✓ 已更新跨天在榜记忆库（仅记实发条目，档案共 {total_seen} 条 URL）", file=sys.stderr)

    duration = round(time.time() - t0, 1)
    metrics = {k: v for k, v in meta.get("metrics", {}).items() if k not in ("track_urls", "hn_url_pairs")}
    write_log(
        "OK",
        f"AI写稿 → {meta.get('log_summary', '')} → 飞书推送成功（{sent} 条 / {len(report)}字）{notion_tag}",
        metrics={**metrics, **notion_meta, "duration_s": duration, "report_chars": len(report)},
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="GitHub Trending & Hacker News 早报")
    parser.add_argument("--mode", choices=["fetch", "send"], required=True)
    parser.add_argument("--since", choices=["daily", "weekly"], default=None,
                        help="强制指定 GitHub Trending 时间跨度（默认周一至周六 daily，周日 weekly）")
    args = parser.parse_args()
    if args.mode == "fetch":
        return run_fetch(since_override=args.since)
    return run_send(since_override=args.since)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        err = redact_secrets(traceback.format_exc().strip().splitlines()[-1])
        write_log("FAIL", err)
        raise
