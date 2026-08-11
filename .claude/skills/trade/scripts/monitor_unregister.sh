#!/bin/bash
# 注销当前会话：从密采样守护 watcher 的注册列表移除（2026-08-11 立）。
#
# 停盯时（停盯总结收尾）调用一次：把本会话的 CLAUDE_CODE_SESSION_ID 从
# tmp/monitor_sessions.txt 删除——注销后 watcher 不再守护本会话（正常停盯不被误报中断）。
#
# 用法：bash .claude/skills/trade/scripts/monitor_unregister.sh
set -e
SESSION_ID="${CLAUDE_CODE_SESSION_ID:-}"
if [ -z "${SESSION_ID}" ]; then
    echo "⚠️ 未拿到 CLAUDE_CODE_SESSION_ID（非 Claude Code 会话内？），跳过注销" >&2
    exit 1
fi
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# scripts/ 向上 4 级 = 项目根（scripts -> trade -> skills -> .claude -> 项目根）
REG_FILE="$SCRIPT_DIR/../../../../tmp/monitor_sessions.txt"
if [ -f "$REG_FILE" ]; then
    grep -vxF "${SESSION_ID}" "$REG_FILE" > "$REG_FILE.tmp" 2>/dev/null || true
    mv "$REG_FILE.tmp" "$REG_FILE"
fi
echo "✅ 盯盘会话已注销（${SESSION_ID}），watcher 不再守护本会话"
