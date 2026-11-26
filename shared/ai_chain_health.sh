#!/bin/bash
# ai_chain_health.sh — AI 三合一流水线（产业日报 → X 热点 → AI Builder）串行体检
#
# 三个 Bot 共用同一个飞书 webhook 且由 Antigravity Sidecar `ai-daily`
# 在每天 09:00 串行执行，本脚本在每日 10:00 按相同顺序串行做轻量化健康检查。

set -u

BOTS_DIR="${BOTS_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"

CHAIN=(
    "AI Daily News Bot"
    "x-hotspot-bot"
    "ai-builder-bot"
)

BLOCK_TIMEOUT=${BLOCK_TIMEOUT:-300}

ts() { date '+%Y-%m-%d %H:%M:%S'; }

echo "$(ts)  ===== AI 三块串行体检开始 ====="
for name in "${CHAIN[@]}"; do
    script="$BOTS_DIR/$name/health_check.sh"
    if [ ! -f "$script" ]; then
        echo "$(ts)  ❌ 找不到 ${script}，跳过这一块"
        continue
    fi

    echo "$(ts)  --- $name 开始 ---"
    bash "$script" &
    pid=$!

    waited=0
    while kill -0 "$pid" 2>/dev/null; do
        if [ "$waited" -ge "$BLOCK_TIMEOUT" ]; then
            echo "$(ts)  ⏱ $name 超过 ${BLOCK_TIMEOUT}s 未结束，终止后继续下一块"
            kill -TERM "$pid" 2>/dev/null
            sleep 5
            kill -KILL "$pid" 2>/dev/null
            break
        fi
        sleep 5
        waited=$((waited + 5))
    done

    wait "$pid" 2>/dev/null
    rc=$?
    echo "$(ts)  --- $name 结束（exit=${rc}）---"
done
echo "$(ts)  ===== AI 三块串行体检结束 ====="
