#!/bin/bash
# health_check.sh — Lightweight Level 1 health check for A-Share Bot (delegates to shared/health_check_base.sh)
DIR="$(cd "$(dirname "$0")" && pwd)"
BOT_NAME="A股智能决策日报"
NO_NEWS_PATTERN="周末非交易日|今日休市|已过.*开盘前瞻窗口|法定节假日"
NO_NEWS_MSG="今日非交易日或已过盘前窗口，正常跳过"
HOUR=$(date +%H)
if [ "$((10#$HOUR))" -lt 15 ]; then
    SESSION_FILTER="premarket"
else
    SESSION_FILTER="close"
fi
export BOT_NAME NO_NEWS_PATTERN NO_NEWS_MSG SESSION_FILTER
source "$DIR/../shared/health_check_base.sh"
