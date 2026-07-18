#!/bin/bash
# 交易信号声音提醒（macOS）+ 响铃 log（2026-07-17 用户立：响铃与记录同一脚本，时间精确到秒，便于事后按时间戳匹配响铃时刻价）。
# AI 发四类正式信号时调用——只响一声系统提示音（不要语音朗读），同时写一行响铃 log。
#
# 用法: bash alert.sh <type> [symbol] [note]
#   type   : open=开仓🟢  close=平仓🔴  ts=移动止损🟡  tp=移动止盈🟠
#   symbol : 标的代码（可选，如 US.MU）
#   note   : 备注（可选，如 入场854/止损843/止盈880）
#
# 行为:
#   ① 按信号类型播一声对应系统音（不同信号不同音色，便于听声辨识）
#   ② 同时 append 一行到 signals/ring-log.csv：时间精确到秒 + type + symbol + note
#
# 为什么有 log（2026-07-17 教训）：
#   响铃是用户能开始 App 操作的时刻，响铃时刻的市价 ≈ 用户实际成交价（市价单即时报这价）。
#   响铃后 AI 即使延迟几分钟才 monitor，只要响铃时间戳准，就能事后用时间戳匹配富途历史 K 线/quote，
#   拿到响铃那一刻的准确价格（用时间匹配，不依赖 AI 取价动作的及时性）。
#   取响铃时刻价后判成交/失败（实测价在 [盈亏比=1价, 止损价] 范围内 → 假设成交）。
#
# 调整: 想更响/换音色改下方 SOUND 映射；系统音清单 ls /System/Library/Sounds/

case "${1:-}" in
  open)  SOUND="Glass" ;;      # 开仓：清脆叮
  close) SOUND="Hero" ;;       # 平仓：上扬号角
  ts)    SOUND="Submarine" ;;  # 移动止损：低沉咚
  tp)    SOUND="Ping" ;;       # 移动止盈：短促 ping
  *)     SOUND="Funk" ;;       # 兜底
esac

# ① 响铃（后台播放，不阻塞 log 写入）
afplay "/System/Library/Sounds/${SOUND}.aiff" 2>/dev/null &

# ② 写响铃 log（时间精确到秒）
# 项目根 = 脚本所在 scripts/ 上四级（scripts→trade→skills→.claude→项目根）
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../../../.." && pwd)"
LOG_DIR="$PROJECT_ROOT/signals"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/ring-log.csv"

# 首次创建时写表头
if [ ! -f "$LOG_FILE" ]; then
  echo "timestamp,type,symbol,note" > "$LOG_FILE"
fi

TS=$(date "+%Y-%m-%d %H:%M:%S")
TYPE="${1:-unknown}"
SYMBOL="${2:-}"
NOTE="${3:-}"
echo "${TS},${TYPE},${SYMBOL},${NOTE}" >> "$LOG_FILE"
