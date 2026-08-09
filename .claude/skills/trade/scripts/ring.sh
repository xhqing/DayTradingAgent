#!/bin/bash
# 响铃 + 记录响铃时刻到 ring-log（发信号流程的【第二个动作】，紧跟写文件之后）
# 2026-08-02 用户立：发信号三步时序 = 写信号文件 → 响铃 → 取响铃时刻价，
# 三步连贯执行、中间不插入任何其它动作。本脚本是第二步：写完信号文件立即响铃，
# 用户听到铃 = 认定 AI 拍板了（标的 + 方向从思考过程已知）、立即开始下单；
# 响铃后立即 snapshot 取价（第三步，成交价近似）。
#
# 为什么三步必须连贯（2026-08-02 用户立）：
#   写文件 → 响铃 → 取价之间不插入任何其它动作（不输出对话关键字段、不算参数），
#   几秒内一气呵成——响铃与取价之间零间隔，取到的响铃实测价才近似用户实际成交价
#   （用户听到铃即下单，取价时刻 ≈ 用户下单时刻）。若中间插入其它动作，
#   取价延迟 = 成交价偏差 = 复盘盈亏失真。
#
# 用法：bash ring.sh <type> [symbol] [note]
#   type   : open=开仓🟢/加仓🔵  close=平仓🔴/减仓🟠  ts=移动止损🟡（决定音色）
#   symbol : 标的代码（可选，写入 ring-log 备查，如 US.MU）
#   note   : 备注（可选，写入 ring-log 备查，如 入场854/止损843）
#
# 行为：响一声对应类型系统音 + append 一行到 signals/ring-log.csv（时间精确到秒）
# 只响铃、不写信号文件——写文件是第一个动作（log_signal.sh），
# 取价是第三个动作（snapshot 取 last_price）。
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

# 响铃（type → 音色；2026-08-09 跨平台适配：macOS 用系统音 afplay、
# Windows 用 PowerShell 蜂鸣按频率/时长区分音色，语义与 macOS 音色一一对应）
case "$(uname)" in
  MINGW*|MSYS*|CYGWIN*)
    # Windows：open=清脆高音(880Hz/200ms) close=上扬长音(1200Hz/400ms) ts=低沉(440Hz/300ms)
    case "$TYPE" in
      open)  _FREQ=880  _DUR=200 ;;
      close) _FREQ=1200 _DUR=400 ;;
      ts)    _FREQ=440  _DUR=300 ;;
      *)     _FREQ=700  _DUR=200 ;;
    esac
    powershell -NoProfile -Command "[console]::Beep($_FREQ,$_DUR)" >/dev/null 2>&1 &
    ;;
  *)
    case "$TYPE" in
      open)  DEFAULT_SOUND="Glass" ;;      # 开仓/加仓：清脆叮
      close) DEFAULT_SOUND="Hero" ;;       # 平仓/减仓：上扬号角
      ts)    DEFAULT_SOUND="Submarine" ;;  # 移动止损：低沉咚
      *)     DEFAULT_SOUND="Funk" ;;       # 兜底
    esac
    SOUND="${SOUND:-$DEFAULT_SOUND}"
    afplay "/System/Library/Sounds/${SOUND}.aiff" 2>/dev/null &
    ;;
esac

# 记 ring-log（响铃时刻 = 下单基准，事后匹配响铃时刻价、判成交）
LOG_FILE="$SIGNALS_DIR/ring-log.csv"
if [ ! -f "$LOG_FILE" ]; then
  echo "timestamp,type,symbol,note" > "$LOG_FILE"
fi
echo "${NOW},${TYPE},${SYMBOL},${NOTE}" >> "$LOG_FILE"
