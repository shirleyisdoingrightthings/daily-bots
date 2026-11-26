#!/bin/bash
# claude_report.sh — A-Share Stock Bot wrapper (delegates to shared/report_base.sh)
DIR="$(cd "$(dirname "$0")" && pwd)"
# 复用既有 A 股飞书 Webhook 与 Exa 凭据（BOT_ID 兼容既有 LaunchAgent plist；也可在 .env 中设置 FEISHU_WEBHOOK_CN）
BOT_ID="cn-hk-stock-bot"
SCRIPT="a_share_report.py"
USAGE_MSG="Usage: claude_report.sh {fetch [premarket|close] | send [premarket|close]}"
EXTRA_ARGS=()
if [ "${1:-}" = "fetch" ]; then
    EXTRA_ARGS=(--session "${2:-close}")
elif [ -n "${2:-}" ]; then
    EXTRA_ARGS=(--session "$2")
fi
source "$DIR/../shared/report_base.sh"
