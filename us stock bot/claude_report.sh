#!/bin/bash
# claude_report.sh — Thin wrapper delegating to shared/report_base.sh
DIR="$(cd "$(dirname "$0")" && pwd)"
BOT_ID="us-stock-bot"
SCRIPT="stock_report.py"
USAGE_MSG="Usage: claude_report.sh {fetch|send}"
source "$DIR/../shared/report_base.sh"
