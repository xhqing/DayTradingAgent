#!/bin/bash
# 注册当前会话为「盯盘会话」，纳入密采样守护 watcher 的守护范围（2026-08-11 立；
# 2026-08-12 收窄为仅港股）。
#
# 盯盘启动时（preflight 之后）调用一次：把本会话的 CLAUDE_CODE_SESSION_ID 写入
# tmp/monitor_sessions.txt（去重）。watcher（launchd 每 10 秒触发）判定：① 死会话
# （jsonl 停更 > 30 分钟）自动剔除；② 任一密采样脚本（monitor_segment.py /
# ws_segment.py / futu_ws_segment.py）在跑 → 正常 Running、不报；③ 采样没在跑 +
# jsonl 停更 > 90 秒 → 报中断。
#
# ⚠️ **仅港股盯盘注册、美股不注册**（2026-08-12 用户立）：watcher 只在港股盘中检查
# （美股时段不打扰用户休息），美股会话注册了第二天港股开盘会被误报。实际注册入口在
# preflight.py 的 _register_monitor_session()，已内置港股盘中判断（含盘前 30 分钟），
# 本脚本是备用 / 手动注册路径，调用前请确认当前是港股盯盘。
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
