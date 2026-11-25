#!/bin/bash
# claude_report.sh — A-Share & Hong Kong Stock Bot wrapper (delegates to shared/report_base.sh)
DIR="$(cd "$(dirname "$0")" && pwd)"
BOT_ID="cn-hk-stock-bot"
SCRIPT="cn_hk_report.py"
USAGE_MSG="Usage: claude_report.sh {fetch [premarket|midday|close] | send [premarket|midday|close]}"
EXTRA_ARGS=()
if [ "${1:-}" = "fetch" ]; then
    EXTRA_ARGS=(--session "${2:-close}")
elif [ -n "${2:-}" ]; then
    EXTRA_ARGS=(--session "$2")
fi
source "$DIR/../shared/report_base.sh"
