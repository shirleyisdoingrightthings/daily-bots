#!/bin/bash
# report_base.sh — Shared runner delegated by each bot's `claude_report.sh`
#
# Expected variables set by the calling wrapper before `source`:
#   DIR         Absolute path to the bot directory
#   BOT_ID      Canonical bot identifier (e.g. ai-daily-news-bot)
#   SCRIPT      Entrypoint Python script filename under $DIR (e.g. daily_report.py)
#   PLIST       Optional explicit macOS LaunchAgent plist path
#   USAGE_MSG   Optional usage hint on invalid mode
#   EXTRA_ARGS  Optional array of extra args forwarded to $SCRIPT
#
# Secret loading priority (highest wins; existing shell env is never overwritten):
#   1. Already-exported shell environment variables (e.g. FORCE_RUN=1)
#   2. Bot-local `$DIR/.env`
#   3. Optional macOS LaunchAgent plist (`$PLIST` or `~/Library/LaunchAgents/${BOT_PLIST_PREFIX}.${BOT_ID}.plist`)
#   4. Shared `$REPO_ROOT/shared/.env` and `$REPO_ROOT/.env`

set -uo pipefail
cd "${DIR:?DIR must be set before sourcing report_base.sh}" || exit 1

MODE="${1:-}"
if [ "$MODE" != "fetch" ] && [ "$MODE" != "send" ] && [ "$MODE" != "test-list" ] && [ "$MODE" != "archive-only" ]; then
    echo "ERROR: ${USAGE_MSG:-Usage: claude_report.sh fetch|send}" >&2
    exit 2
fi

REPO_ROOT="$(cd "$DIR/.." && pwd)"

_load_dotenv_file() {
    local file="$1"
    [ -f "$file" ] || return 0
    local line key val
    while IFS= read -r line || [ -n "$line" ]; do
        line="${line%$'\r'}"
        case "$line" in
            ""|\#*) continue ;;
        esac
        line="${line#export }"
        if [[ "$line" =~ ^[[:space:]]*([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]]; then
            key="${BASH_REMATCH[1]}"
            val="${BASH_REMATCH[2]}"
            # Strip optional matching single or double quotes
            if [[ "$val" =~ ^\"(.*)\"$ ]] || [[ "$val" =~ ^\'(.*)\'$ ]]; then
                val="${BASH_REMATCH[1]}"
            fi
            if [ -z "${!key+x}" ]; then
                export "$key=$val"
            fi
        fi
    done < "$file"
}

_peek_dotenv_key() {
    local file="$1" target="$2"
    [ -f "$file" ] || return 1
    local line val
    while IFS= read -r line || [ -n "$line" ]; do
        line="${line%$'\r'}"
        line="${line#export }"
        if [[ "$line" =~ ^[[:space:]]*${target}=(.*)$ ]]; then
            val="${BASH_REMATCH[1]}"
            if [[ "$val" =~ ^\"(.*)\"$ ]] || [[ "$val" =~ ^\'(.*)\'$ ]]; then
                val="${BASH_REMATCH[1]}"
            fi
            printf '%s' "$val"
            return 0
        fi
    done < "$file"
    return 1
}

# 1. Bot-local .env
_load_dotenv_file "$DIR/.env"

# 2. Optional macOS LaunchAgent plist (keeps existing macOS setups zero-config)
if [ -z "${PLIST:-}" ] && [ -n "${BOT_ID:-}" ]; then
    PLIST_PREFIX="${BOT_PLIST_PREFIX:-}"
    if [ -z "$PLIST_PREFIX" ]; then
        PLIST_PREFIX="$(_peek_dotenv_key "$REPO_ROOT/shared/.env" "BOT_PLIST_PREFIX" || _peek_dotenv_key "$REPO_ROOT/.env" "BOT_PLIST_PREFIX" || true)"
    fi
    if [ -n "$PLIST_PREFIX" ]; then
        PLIST="${BOT_PLIST:-$HOME/Library/LaunchAgents/${PLIST_PREFIX}.${BOT_ID}.plist}"
    elif [ -n "${BOT_PLIST:-}" ]; then
        PLIST="$BOT_PLIST"
    fi
fi

if [ -n "${PLIST:-}" ] && [ -f "$PLIST" ] && [ -x "/usr/libexec/PlistBuddy" ]; then
    while IFS= read -r line; do
        key="${line%%=*}"
        val="${line#*=}"
        if [ -n "$key" ] && [ -z "${!key+x}" ]; then
            export "$key=$val"
        fi
    done < <(/usr/libexec/PlistBuddy -c "Print :EnvironmentVariables" "$PLIST" 2>/dev/null \
        | sed -n 's/^[[:space:]]*\([A-Za-z_][A-Za-z0-9_]*\) = \(.*\)$/\1=\2/p')
fi

# 3. Shared & repo-root .env fallbacks
_load_dotenv_file "$REPO_ROOT/shared/.env"
_load_dotenv_file "$REPO_ROOT/.env"

PY="${BOT_PYTHON:-/usr/bin/python3}"
if [ ! -x "$PY" ]; then
    PY="$(command -v python3)"
fi

if declare -p EXTRA_ARGS >/dev/null 2>&1; then
    exec "$PY" "${SCRIPT:?SCRIPT must be set}" --mode "$MODE" ${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"}
else
    shift || true
    exec "$PY" "${SCRIPT:?SCRIPT must be set}" --mode "$MODE" "$@"
fi
