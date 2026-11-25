#!/bin/bash
# health_check.sh — A股 & 港股智能决策日报
# 每个交易日 17:15 检查当天运行状态（Option B 轻量化健康检查）

DIR="$(cd "$(dirname "$0")" && pwd)"
BOT_NAME="CN/HK Stock Bot"

CATCHUP_FROM=9
if [ -z "${SESSION_FILTER:-}" ]; then
    HOUR_NOW=$(date '+%-H')
    if [ "$HOUR_NOW" -ge 16 ]; then
        SESSION_FILTER="close"
    else
        SESSION_FILTER="premarket"
    fi
fi
NO_NEWS_PATTERN="休市|非交易日|自动跳过"
NO_NEWS_MSG="今日休市、已过时效窗口或早盘已推送，未出稿（非故障）"

content_check() {
    local qcnt
    qcnt=$(jsonl_field quotes_count 99)
    if [ -n "$qcnt" ] && [ "$qcnt" -lt 20 ] 2>/dev/null; then
        notify WARN "本次仅抓到 ${qcnt} 只标的，请检查行情接口"
        echo "[health_check] WARN: quotes_count=${qcnt}，行情数量偏低"
    fi
}

source "$DIR/../shared/health_check_base.sh"
