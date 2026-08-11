#!/bin/bash
# 注册当前会话为「盯盘会话」，纳入密采样守护 watcher 的守护范围（2026-08-11 立）。
#
# 盯盘启动时（preflight 之后）调用一次：把本会话的 CLAUDE_CODE_SESSION_ID 写入
# tmp/monitor_sessions.txt（去重）。watcher（launchd 每 120 秒）对注册列表里的每个会话
# 独立判定：jsonl 停更 > 60 秒 → 该会话中断 → 系统通知提醒用户唤醒 AI。
#
# 停盯时调用 scripts/monitor_unregister.sh 注销（从注册文件删除本会话）。
# 注册文件在 tmp/（已被 .gitignore 忽略，不进仓库）。
#
# 用法：bash .claude/skills/trade/scripts/monitor_register.sh
set -e
SESSION_ID="${CLAUDE_CODE_SESSION_ID:-}"
if [ -z "${SESSION_ID}" ]; then
    echo "⚠️ 未拿到 CLAUDE_CODE_SESSION_ID（非 Claude Code 会话内？），跳过注册" >&2
    exit 1
fi
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# scripts/ 向上 4 级 = 项目根（scripts -> trade -> skills -> .claude -> 项目根）
REG_FILE="$SCRIPT_DIR/../../../../tmp/monitor_sessions.txt"
mkdir -p "$(dirname "$REG_FILE")"
if ! grep -qxF "${SESSION_ID}" "$REG_FILE" 2>/dev/null; then
    echo "${SESSION_ID}" >> "$REG_FILE"
fi
echo "✅ 盯盘会话已注册（${SESSION_ID}），watcher 将守护本会话"
