#!/bin/bash
# claude_report.sh — Thin wrapper delegating to shared/report_base.sh
DIR="$(cd "$(dirname "$0")" && pwd)"
BOT_ID="github-hn-bot"
SCRIPT="github_hn_report.py"
USAGE_MSG="Usage: claude_report.sh {fetch|send} [daily|weekly]"
EXTRA_ARGS=()
if { [ "${1:-}" = "fetch" ] || [ "${1:-}" = "send" ]; } && { [ "${2:-}" = "daily" ] || [ "${2:-}" = "weekly" ]; }; then
    EXTRA_ARGS=(--since "$2")
fi
source "$DIR/../shared/report_base.sh"
