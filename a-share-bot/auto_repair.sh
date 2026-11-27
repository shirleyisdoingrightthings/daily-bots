#!/bin/bash
# auto_repair.sh — Thin wrapper delegating to shared/auto_repair_base.sh
DIR="$(cd "$(dirname "$0")" && pwd)"
BOT_NAME="A股智能决策日报"
SCRIPT="$DIR/a_share_report.py"
ERROR="${1:-未知错误}"
if [ -z "${SESSION_FILTER:-}" ]; then
    HOUR=$(date +%H)
    if [ "$((10#$HOUR))" -lt 16 ]; then
        SESSION_FILTER="premarket"
    else
        SESSION_FILTER="close"
    fi
fi
DRAFTS="logs/report_cn_${SESSION_FILTER}.txt"
if [ ! -s "$DIR/$DRAFTS" ] && [ -s "$DIR/logs/report_cn.txt" ]; then
    DRAFTS="logs/report_cn.txt"
fi
export BOT_NAME SCRIPT ERROR SESSION_FILTER DRAFTS
source "$DIR/../shared/auto_repair_base.sh"
