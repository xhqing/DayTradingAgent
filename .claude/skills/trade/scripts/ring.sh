#!/bin/bash
# 响铃 + 记录响铃时刻到 ring-log（拍板发信号的【第一个动作】，零延迟）
# 2026-07-29 用户立：响铃和写信号文件分离——用户时刻盯会话（含 AI 思考过程）、
# 时刻准备下单，故 AI 拍板决定发信号的那一刻就立即响铃；用户听到铃 = 认定 AI 拍板了，
# 立即开始下单。响铃后 AI 才在对话输出关键字段（参考价/止损/价格范围），
# 最后才写信号文件（复盘用，由 log_signal.sh 完成，不阻塞下单）。
#
# 为什么响铃必须最先、且和写文件分离（2026-07-29 用户立）：
#   原 alert.sh「写文件 + 响铃」二合一，写文件（构造完整 markdown）有延迟，
#   会让响铃滞后几秒～十几秒；用户实战下单靠「听到铃 + 对话关键字段」（秒级），
#   信号文件主要用于复盘（太慢、不靠它下单）。故拆分：响铃单飞（本脚本，即时），
#   写文件另走 log_signal.sh（最后）。
#
# 用法：bash ring.sh <type> [symbol] [note]
#   type   : open=开仓🟢/加仓🔵  close=平仓🔴/减仓🟠  ts=移动止损🟡（决定音色）
#   symbol : 标的代码（可选，写入 ring-log 备查，如 US.MU）
#   note   : 备注（可选，写入 ring-log 备查，如 入场854/止损843）
#
# 行为：响一声对应类型系统音 + append 一行到 signals/ring-log.csv（时间精确到秒）
# 只响铃、不写信号文件——写文件是最后一个动作（复盘用），由 log_signal.sh 完成。
#
# 调试 / 测试：可用环境变量覆盖
#   PROJECT_ROOT=xxx   覆盖项目根（默认 = 脚本上四级）
#   SOUND=xxx          覆盖音色（默认按 type 映射）
#   NOW=xxx            覆盖响铃时间戳（默认 date 实测，精确到秒）
# 调整：想更响/换音色改下方 SOUND 映射；系统音清单 ls /System/Library/Sounds/

set -uo pipefail

TYPE="${1:-}"
SYMBOL="${2:-}"
NOTE="${3:-}"

if [ -z "$TYPE" ]; then
  echo "Usage: bash ring.sh <open|close|ts> [symbol] [note]" >&2
  echo "Error: 缺少必填参数 type（type=open/close/ts，决定音色）" >&2
  exit 1
fi

# 定位项目根 = 脚本所在 scripts/ 上四级（scripts→trade→skills→.claude→项目根）
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="${PROJECT_ROOT:-$(cd "$SCRIPT_DIR/../../../.." && pwd)}"
SIGNALS_DIR="$PROJECT_ROOT/signals"
mkdir -p "$SIGNALS_DIR"

NOW="${NOW:-$(date "+%Y-%m-%d %H:%M:%S")}"

# 响铃（type → 音色）
case "$TYPE" in
  open)  DEFAULT_SOUND="Glass" ;;      # 开仓/加仓：清脆叮
  close) DEFAULT_SOUND="Hero" ;;       # 平仓/减仓：上扬号角
  ts)    DEFAULT_SOUND="Submarine" ;;  # 移动止损：低沉咚
  *)     DEFAULT_SOUND="Funk" ;;       # 兜底
esac
SOUND="${SOUND:-$DEFAULT_SOUND}"
afplay "/System/Library/Sounds/${SOUND}.aiff" 2>/dev/null &

# 记 ring-log（响铃时刻 = 下单基准，事后匹配响铃时刻价、判成交）
LOG_FILE="$SIGNALS_DIR/ring-log.csv"
if [ ! -f "$LOG_FILE" ]; then
  echo "timestamp,type,symbol,note" > "$LOG_FILE"
fi
echo "${NOW},${TYPE},${SYMBOL},${NOTE}" >> "$LOG_FILE"
