#!/bin/bash
# auto_repair.sh — Thin wrapper delegating to shared/auto_repair_base.sh
DIR="$(cd "$(dirname "$0")" && pwd)"
BOT_NAME="A股智能决策日报"
SCRIPT="a_share_report.py"
source "$DIR/../shared/auto_repair_base.sh"
