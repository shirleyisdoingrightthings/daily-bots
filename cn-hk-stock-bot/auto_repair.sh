#!/bin/bash
# auto_repair.sh — A股 & 港股智能决策日报
# 检测到 [FAIL] 时由 health_check.sh 触发（委托 ../shared/auto_repair_base.sh）

DIR="$(cd "$(dirname "$0")" && pwd)"
BOT_NAME="CN/HK Stock Bot"
SCRIPT="$DIR/cn_hk_report.py"
ERROR="${1:-unknown error}"
DRAFTS="logs/report_cn.txt logs/report_hk.txt"

source "$DIR/../shared/auto_repair_base.sh"
