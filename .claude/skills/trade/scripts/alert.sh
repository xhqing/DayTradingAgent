#!/bin/bash
# 发出正式交易信号：① 写入信号文件 ② 响铃并记录响铃时刻
# （2026-07-18 用户立：信号文件是用户下单的基准，故写文件是发信号后的第一个动作、先于响铃；
#  写文件与响铃合并进本同一脚本，脚本内顺序固定为「先写文件、再响铃」。）
#
# 用户以 signals/ 下信号文件的内容为下单基准——信号文件是信号最终落地、用户实际参照下单的东西
# （对话会被推走、易丢失）。故决定发信号后，本脚本是要调用的第一个动作：先把信号内容写进对应市场文件
# （确立基准），紧接着响铃通知用户「文件已就绪、可去看文件下单」，并记录响铃时刻（事后按时间戳匹配响铃时刻价）。
#
# 用法（信号内容是多行 markdown，经 stdin 传入，避免命令行参数转义问题）：
#   cat <<'SIGNAL' | bash alert.sh <type> <market> [symbol] [note]
#   ## 🟢🟢🟢 开仓 · <标的代码> <中文名> · 做多/做空 🟢🟢🟢
#   （完整信号内容：标题 + 表格，对话里呈现什么就写什么）
#   SIGNAL
#     type   : open=开仓🟢/加仓🔵  close=平仓🔴/减仓🟠  ts=移动止损🟡（决定音色）
#     market : HKT=港股  ET=美股（决定写到哪个信号文件）
#     symbol : 标的代码（可选，写入 ring-log 备查，如 US.MU）
#     note   : 备注（可选，写入 ring-log 备查，如 入场854/止损843）
#
# 行为（两步，顺序固定）：
#   ① 把 stdin 的信号内容 append 到 signals/YYYY-MM-DD-<market>-signals.md
#   ② 响一声对应类型系统音 + append 一行到 signals/ring-log.csv（时间精确到秒）
#
# 为什么写文件先于响铃（2026-07-18 用户立）：
#   响铃是通知用户「现在去看文件下单」——文件必须先写好，用户被铃叫醒/吸引过去时，文件已是完整、可直接下单的内容；
#   先响铃、文件没写好 = 用户听到铃却看不到内容。写文件在前、响铃在后，确保铃响那一刻文件已就绪。
#
# 为什么有响铃 log（2026-07-17 教训）：
#   响铃是用户能开始 App 操作的时刻，响铃时刻的市价 ≈ 用户实际成交价。
#   响铃后 AI 即使延迟几分钟才 monitor，只要响铃时间戳准，就能事后用时间戳匹配富途历史 K 线/quote，
#   拿到响铃那一刻的准确价格。取响铃时刻价后判成交/失败（实测价落在不对称成交范围 → 假设成交：
#   做多 [参考价−0.8R, 盈亏比=1价] / 做空 [盈亏比=1价, 参考价+0.8R]，R=|参考价−止损价|、盈亏比=1价=(止盈+止损)/2；详见 SKILL「响铃后立即取实测价」段）。
#
# 调试 / 测试：可用环境变量覆盖
#   PROJECT_ROOT=xxx   覆盖项目根（默认 = 脚本上四级），测试时指向隔离目录避免污染真实 signals/
#   SOUND=xxx          覆盖音色（默认按 type 映射）
#   NOW=xxx            覆盖响铃时间戳（默认 date 实测，精确到秒）
# 调整：想更响/换音色改下方 SOUND 映射；系统音清单 ls /System/Library/Sounds/

set -uo pipefail

TYPE="${1:-}"
MARKET="${2:-}"
SYMBOL="${3:-}"
NOTE="${4:-}"

# 读 stdin 信号内容（一次性读尽再判断，避免空内容写空文件）
CONTENT=$(cat)
if [ -z "$CONTENT" ]; then
  echo "Error: 信号内容为空——请经 stdin 传入：cat <<'SIGNAL' ... SIGNAL | bash alert.sh <type> <market> [symbol] [note]" >&2
  exit 1
fi

if [ -z "$TYPE" ] || [ -z "$MARKET" ]; then
  echo "Usage: cat <<'SIGNAL' ... SIGNAL | bash alert.sh <open|close|ts> <HKT|ET> [symbol] [note]" >&2
  echo "Error: 缺少必填参数 type / market（type=open/close/ts, market=HKT/ET）" >&2
  exit 1
fi

# 定位项目根 = 脚本所在 scripts/ 上四级（scripts→trade→skills→.claude→项目根）
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="${PROJECT_ROOT:-$(cd "$SCRIPT_DIR/../../../.." && pwd)}"
SIGNALS_DIR="$PROJECT_ROOT/signals"
mkdir -p "$SIGNALS_DIR"

TODAY=$(date "+%Y-%m-%d")
NOW="${NOW:-$(date "+%Y-%m-%d %H:%M:%S")}"

# ① 写信号文件
case "$MARKET" in
  HKT|ET)
    SIGNAL_FILE="$SIGNALS_DIR/${TODAY}-${MARKET}-signals.md"
    ;;
  *)
    echo "Error: market 必须是 HKT 或 ET，当前为 '$MARKET'" >&2
    exit 1
    ;;
esac

# 与前一条信号之间空一行分隔（文件已非空时先补一个空行）
[ -s "$SIGNAL_FILE" ] && echo "" >> "$SIGNAL_FILE"
printf '%s\n' "$CONTENT" >> "$SIGNAL_FILE"

# ② 响铃 + 写 log
case "$TYPE" in
  open)  DEFAULT_SOUND="Glass" ;;      # 开仓/加仓：清脆叮
  close) DEFAULT_SOUND="Hero" ;;       # 平仓/减仓：上扬号角
  ts)    DEFAULT_SOUND="Submarine" ;;  # 移动止损：低沉咚
  *)     DEFAULT_SOUND="Funk" ;;       # 兜底
esac
SOUND="${SOUND:-$DEFAULT_SOUND}"

afplay "/System/Library/Sounds/${SOUND}.aiff" 2>/dev/null &

LOG_FILE="$SIGNALS_DIR/ring-log.csv"
if [ ! -f "$LOG_FILE" ]; then
  echo "timestamp,type,symbol,note" > "$LOG_FILE"
fi
echo "${NOW},${TYPE},${SYMBOL},${NOTE}" >> "$LOG_FILE"
