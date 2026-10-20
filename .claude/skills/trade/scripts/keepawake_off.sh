#!/bin/bash
# 停用盯盘防睡眠（解除 caffeinate）——引用计数版（2026-08-17 立，多会话并行盯盘·方案 A）。
# 2026-09-01 自 .claude/skills/keep-awake/scripts/off.sh 迁入（keep-awake skill 已撤销、
# 功能并入 trade，见 references/monitoring.md「防睡眠机制」节），逻辑逐行保留、仅路径重推。
#
# 为什么引用计数：多会话并行盯盘时每个会话的 preflight 都会启 caffeinate -s（全局一进程，
# 第二家跳过），若一家停盯就直接 pkill，会把还在盯的其它会话的防睡眠一起杀掉——合盖即睡、
# 采样全断。改为：停盯会话先从 tmp/monitor_sessions.txt 注销自己；注册文件还有其它会话
# = 还有别人在盯，只注销、不 kill caffeinate；最后一人（注册文件空 / 无注册）才关灯。
#
# 兼容性：单会话场景行为不变（注册文件空 → 直接 pkill）；手动全局启用（不经 preflight、
# 无注册）的场景按「无注册 = 最后一人」处理、照常 kill。
#
# 用法：bash .claude/skills/trade/scripts/keepawake_off.sh [--force]
#
# ⛔ 停盯边界时间闸（2026-08-24 立，T118）：盘中（距收盘 >5 分钟）解除防睡眠会被
# stop_gate.py 拒绝——盯盘终止条件只有「用户喊停」或「收盘」（取先到），空仓 / 无信号
# 都不是停盯理由（2026-08-24 11:48 违规提前停盯教训）。确属用户喊停：加 --force。
# 注意：本闸只拦「解除防睡眠」动作；手动全局启用防睡眠后想单独关闭（非停盯场景）
# 同样加 --force 即可。
# 会话 ID 按宿主双读：Claude Code / ZCode / CodeBuddy 为 CLAUDE_CODE_SESSION_ID，pi 为 PI_SESSION_ID。
SESSION_ID="${CLAUDE_CODE_SESSION_ID:-${PI_SESSION_ID:-}}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# trade/scripts -> trade -> skills -> .claude -> 项目根
REG_FILE="$SCRIPT_DIR/../../../../tmp/monitor_sessions.txt"

# —— 停盯边界时间闸：盘中拒绝解除防睡眠（用户喊停 --force 放行）——
STOP_GATE="$SCRIPT_DIR/stop_gate.py"

# 2026-10-20 立（多会话并行安全版）：市场由【本会话自己的动作记录】判定
# （stop_summary.py --detect-market 读 actions 里 🆔 会话 标记），而不是「今天有哪些 log」——
# 后者在港股+美股并行时会把港股会话误判成美股，导致港股盘中非法停盯。判不出时不传 --market，
# 保持旧保守行为（任一市场在盘中即拒绝）。
MARKET_DETECTED=$(python3 "$SCRIPT_DIR/stop_summary.py" --detect-market 2>/dev/null | tr -d '[:space:]')
MARKET_FLAG=""
case "$MARKET_DETECTED" in
  us) MARKET_FLAG="--market us" ;;
  hk) MARKET_FLAG="--market hk" ;;
  *)  MARKET_FLAG="" ;;
esac


# 2026-10-20 立：按「今天实际盯的市场」给 stop_gate 传 --market，
# 避免美股会话停盯被港股盘中窗口误拦（2026-10-19 实测踩到）。
MARKET_FLAG=""
if ls "$SCRIPT_DIR/../../../../tmp"/monitor_log_US_*_"$(date +%Y%m%d)"_*.csv >/dev/null 2>&1 || \
   ls "$SCRIPT_DIR/../../../../tmp"/monitor_log_US_*_"$(date -v-1d +%Y%m%d)"_*.csv >/dev/null 2>&1; then
  MARKET_FLAG="--market us"
elif ls "$SCRIPT_DIR/../../../../tmp"/monitor_log_HK_*_"$(date +%Y%m%d)"_*.csv >/dev/null 2>&1; then
  MARKET_FLAG="--market hk"
fi
FORCE_FLAG=""
for a in "$@"; do
    [ "$a" = "--force" ] && FORCE_FLAG="--force"
done
if [ -f "$STOP_GATE" ]; then
    if ! python3 "$STOP_GATE" check $FORCE_FLAG $MARKET_FLAG; then
        echo "⛔ 已拒绝解除防睡眠：停盯需等收盘边界（≤5 分钟窗口自动放行）或用户明确喊停（--force）" >&2
        exit 2
    fi
fi

# ① 先注销本会话（在注册文件里则移除；不在 / 非会话环境跳过）
if [ -n "${SESSION_ID}" ] && [ -f "$REG_FILE" ]; then
    grep -vxF "${SESSION_ID}" "$REG_FILE" > "$REG_FILE.tmp" 2>/dev/null || true
    mv "$REG_FILE.tmp" "$REG_FILE"
fi

# ② 引用计数判定：注册文件还有别的会话 = 别人在盯，不关灯
if [ -f "$REG_FILE" ] && [ -s "$REG_FILE" ]; then
    REMAINING=$(grep -cv '^$' "$REG_FILE" 2>/dev/null || echo 0)
    if [ "$REMAINING" -gt 0 ]; then
        echo "☕ caffeinate -s 保留（还有 ${REMAINING} 个盯盘会话在场——引用计数，最后一人停盯才解除防睡眠）"
        exit 0
    fi
fi

# ③ 最后一人（或无注册场景）：解除防睡眠
if pkill -f "caffeinate -s" 2>/dev/null; then
  echo "☕ caffeinate -s 已停用（防睡眠解除——本会话是最后一个盯盘会话）"
else
  echo "（无 caffeinate -s 在跑，无需停用）"
fi
