#!/bin/bash
# 注销当前会话：从密采样守护 watcher 的注册列表移除（2026-08-11 立）。
#
# 停盯时（停盯总结收尾）调用一次：把本会话的 CLAUDE_CODE_SESSION_ID 从
# tmp/monitor_sessions.txt 删除——注销后 watcher 不再守护本会话（正常停盯不被误报中断）。
#
# ⛔ 停盯边界时间闸（2026-08-24 立，T118）：盘中（距收盘 >5 分钟）执行本脚本会被
# stop_gate.py 拒绝——盯盘终止条件只有「用户喊停」或「收盘」（取先到），空仓 / 无信号 /
# 市场无聊都不是停盯理由（2026-08-24 11:48 违规提前停盯的教训，散文规定会衰减故机械强制）。
# 确属用户喊停：加 --force 显式放行（用户指令优先于机械闸）。
#
# 用法：bash .claude/skills/trade/scripts/monitor_unregister.sh [--force]
set -e
SESSION_ID="${CLAUDE_CODE_SESSION_ID:-}"

# —— 停盯边界时间闸：盘中拒绝注销（用户喊停 --force 放行）——
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
FORCE_FLAG=""
for a in "$@"; do
    [ "$a" = "--force" ] && FORCE_FLAG="--force"
done
if [ -f "$SCRIPT_DIR/stop_gate.py" ]; then
    if ! python3 "$SCRIPT_DIR/stop_gate.py" check $FORCE_FLAG; then
        echo "⛔ 已拒绝注销：停盯需等收盘边界（≤5 分钟窗口自动放行）或用户明确喊停（--force）" >&2
        exit 2
    fi
fi

if [ -z "${SESSION_ID}" ]; then
    echo "⚠️ 未拿到 CLAUDE_CODE_SESSION_ID（非 Claude Code 会话内？），跳过注销" >&2
    exit 1
fi
# scripts/ 向上 4 级 = 项目根（scripts -> trade -> skills -> .claude -> 项目根）
REG_FILE="$SCRIPT_DIR/../../../../tmp/monitor_sessions.txt"
if [ -f "$REG_FILE" ]; then
    grep -vxF "${SESSION_ID}" "$REG_FILE" > "$REG_FILE.tmp" 2>/dev/null || true
    mv "$REG_FILE.tmp" "$REG_FILE"
fi
echo "✅ 盯盘会话已注销（${SESSION_ID}），watcher 不再守护本会话"

# 标的池认领联动释放（2026-08-19 立，TODO「标的池自动划分」）：停盯注销的同时释放本会话
# 认领的标的池——其它（或重启的）会话可立即认领，不留死占（忘跑 pool_claim release --all
# 时由本联动 + 死会话自动清理双兜底）。
if [ -f "$SCRIPT_DIR/pool_claim.py" ]; then
    python3 "$SCRIPT_DIR/pool_claim.py" release --all || true
fi
