#!/bin/bash
# auto_repair_base.sh — 共享 Level 1 非 AI 自愈重发逻辑
#
# 由各 Bot 的 auto_repair.sh 薄包装在 `source` 之前设置好以下变量：
#   DIR        Bot 根目录（含 logs/run.log、changelog.md、claude_report.sh）
#   BOT_NAME   Bot 名称（日志标识）
#   SCRIPT     主脚本绝对路径（兼容既有薄包装变量）
#   ERROR      health_check 从 [FAIL] 行提取的失败原因
#   DRAFTS     当日稿件相对路径列表（空格分隔），用于新鲜度检查
#
# 策略：
#   - 仅当今日稿件已存在且新鲜（drafts_fresh）且为瞬时错误（网络/超时/代理/限流）时，
#     等待 30s 重跑 `bash claude_report.sh send`。
#   - 缺稿、非瞬时错误或重跑仍失败时记录日志转人工介入（缺跑唤醒由 Antigravity Sidecar 负责）。

set -o pipefail

PYTHON="${BOT_PYTHON:-/usr/bin/python3}"

LOG_DIR="$DIR/logs"
mkdir -p "$LOG_DIR"
REPAIR_LOG="$LOG_DIR/auto_repair.log"
RUN_LOG="$LOG_DIR/run.log"
CHANGELOG="$DIR/changelog.md"
TODAY="$(date '+%Y-%m-%d')"
SHORT="$(echo "$ERROR" | cut -c1-120)"

log()    { echo "$(date '+%Y-%m-%d %H:%M:%S')  $*" | tee -a "$REPAIR_LOG"; }
notify() { log "$1"; }

# 把 changelog 中“含本次错误摘要”的 [ ]/[/] 条目改成指定标记（/=修复中, x=待验证）
mark_changelog() {
    [ -f "$CHANGELOG" ] || return 0
    "$PYTHON" - "$CHANGELOG" "$SHORT" "$1" <<'PY'
import sys, re
path, short, mark = sys.argv[1], sys.argv[2], sys.argv[3]
key = short[:40]
lines = open(path, encoding="utf-8").read().splitlines()
out, done = [], False
for ln in lines:
    if not done and key and key in ln and re.match(r"^- \[[ /]\]", ln):
        ln = re.sub(r"^- \[[ /]\]", f"- [{mark}]", ln, count=1)
        done = True
    out.append(ln)
open(path, "w", encoding="utf-8").write("\n".join(out) + "\n")
PY
}

# 检查今日稿件是否可用：DRAFTS 中每个文件都存在、非空、且当天修改过。
drafts_fresh() {
    [ -n "${DRAFTS:-}" ] || return 1
    local f
    for f in $DRAFTS; do
        [ -s "$DIR/$f" ] || return 1
        [ "$(date -r "$DIR/$f" '+%Y-%m-%d')" = "$TODAY" ] || return 1
    done
    return 0
}

# 重跑发送环节；若配置了 SESSION_FILTER（双场次 Bot），透传场次并仅认对应场次的 [OK]，防止早盘 [OK] 掩盖收盘 [FAIL]。
rerun() {
    if [ -n "${SESSION_FILTER:-}" ]; then
        log "重跑：bash claude_report.sh send $SESSION_FILTER"
        bash "$DIR/claude_report.sh" send "$SESSION_FILTER" >>"$REPAIR_LOG" 2>&1
        grep -qE "^$TODAY.*\[OK\].*\($SESSION_FILTER\)" "$RUN_LOG" 2>/dev/null
    else
        log "重跑：bash claude_report.sh send"
        bash "$DIR/claude_report.sh" send >>"$REPAIR_LOG" 2>&1
        grep -q "$TODAY.*\[OK\]" "$RUN_LOG" 2>/dev/null
    fi
}

# 瞬时错误判定（网络/超时/代理/限流等可重试类）
is_transient() {
    echo "$ERROR" | grep -qiE \
      "time( |d )?out|timeout|connection|connect|network|网络|proxy|代理|temporar|rate.?limit|429|502|503|RequestException|SSLError|Max retries|ConnectionError|ReadTimeout|getaddrinfo|Remote end closed|unreachable|EOF occurred"
}

log "===== auto_repair 触发：${BOT_NAME} ====="
log "错误：$ERROR"
mark_changelog "/"

if ! drafts_fresh; then
    log "今日稿件缺失或过期（DRAFTS=${DRAFTS:-未配置}），重跑 send 无意义（由 Antigravity Sidecar 负责唤醒重跑）"
elif is_transient; then
    log "判定为瞬时错误 → Level 1：等待 30s 后重跑"
    sleep 30
    if rerun; then
        log "✅ Level 1 重跑成功"
        mark_changelog "x"
        notify "瞬时故障已自愈（Level 1 重跑成功）"
        exit 0
    fi
    log "Level 1 重跑仍失败"
else
    log "判定为非瞬时错误 → 跳过自动重试，转人工介入"
fi

log "❌ 自愈失败，需人工介入"
notify "自动修复失败，请人工介入：${SHORT}"
exit 1
