#!/bin/bash
# health_check.sh — Lightweight Level 1 health check for A-Share Bot
DIR="$(cd "$(dirname "$0")" && pwd)"
BOT_NAME="A股智能决策日报"
BOT_ID="a-share-bot"
LOG_FILE="$DIR/logs/run.log"
DRAFT_FILES=("$DIR/logs/report_cn.txt")
SEND_CMD="bash \"$DIR/claude_report.sh\" send"
source "$DIR/../shared/health_check_base.sh"
