#!/bin/bash
# health_check.sh — AI Builder Bot
# 薄封装：设好本 bot 的差异点后，交给同级 shared/health_check_base.sh 执行。
# 完整流程与踩坑记录见那个文件，别把逻辑抄回来。

DIR="$(cd "$(dirname "$0")" && pwd)"

BOT_NAME="AI Builder Bot"

# 09:00 三合一流水线第 3 棒，10:00 体检需将检查窗口提前到 09:00
CATCHUP_FROM=9

# 终态 WARN 的匹配模式。⚠️ 必须与 builder_report.py 里 write_log("WARN", ...) 的实际措辞
# 一致——对不上就会把"正常的没东西可播"误判成缺跑，白派一次无头补跑。
NO_NEWS_PATTERN="没有可用的新推文"
NO_NEWS_MSG="今天 X List 窗口内没有可用新推文，未出稿（非故障）"

# 本 bot 仅使用自建 X List 数据源，无分源零产告警。

# 第 4 节：推文数异常偏低检查
content_check() {
    local tweets
    tweets=$(jsonl_field tweets 99)
    if [ -n "$tweets" ] && [ "$tweets" -lt 5 ] 2>/dev/null; then
        notify WARN "本次仅 ${tweets} 条推文，请检查 X List 与 twitterapi.io Key / 数据源状态"
        echo "[health_check] WARN: tweets=${tweets}，推文数量异常偏低"
    fi
}

source "$DIR/../shared/health_check_base.sh"
