#!/bin/bash
# 写信号文件（复盘用）——发信号流程的【最后一个动作】，在响铃 + 对话输出关键字段之后
# 2026-07-29 用户立：响铃和写信号文件分离——实战下单靠「听到铃 + 对话关键字段」（秒级），
# 信号文件主要用于复盘（事后回查每笔信号的完整内容 + 时间戳），故写文件放最后、不阻塞下单。
#
# 用法（信号内容是多行 markdown，经 stdin 传入，避免命令行参数转义问题）：
#   cat <<'SIGNAL' | bash log_signal.sh <market>
#   ## 🟢🟢🟢 开仓 · <标的代码> <中文名> · 做多/做空 🟢🟢🟢
#   （完整信号内容：标题 + 表格 + 依据 + 风险，对话给关键字段、文件里写完整复盘内容）
#   SIGNAL
#   market : HKT=港股  ET=美股（决定写到哪个信号文件）
#
# 行为：
#   ① 把 stdin 信号内容 append 到 signals/YYYY-MM-DD-<market>-signals.md
#   ② 信号正文首行带发信号时间戳——读 ring-log.csv 末行（= 最近一次响铃时刻，
#      与下单基准一致），保证「响铃时刻 / ring-log / 信号文件时间戳」三者一致。
# 只写文件、不响铃——响铃是第一个动作（ring.sh）。
#
# 为什么时间戳读 ring-log 末行（而非写文件当下的时间）：
#   响铃时刻 = AI 拍板时刻 = 用户下单基准；写文件是最后一步（比响铃晚几秒～十几秒）。
#   复盘要以「发信号时刻」对齐，故信号文件时间戳取响铃时刻（ring-log 末行），
#   而非写文件时刻——这样信号文件、ring-log、响铃时刻完全一致，复盘匹配准确。
#
# 调试 / 测试：可用环境变量覆盖
#   PROJECT_ROOT=xxx   覆盖项目根
#   RING_TS=xxx        覆盖时间戳（默认读 ring-log 末行）

set -uo pipefail

MARKET="${1:-}"

# 读 stdin 信号内容（一次性读尽再判断，避免空内容写空文件）
CONTENT=$(cat)
if [ -z "$CONTENT" ]; then
  echo "Error: 信号内容为空——请经 stdin 传入：cat <<'SIGNAL' ... SIGNAL | bash log_signal.sh <market>" >&2
  exit 1
fi

if [ -z "$MARKET" ]; then
  echo "Usage: cat <<'SIGNAL' ... SIGNAL | bash log_signal.sh <HKT|ET>" >&2
  echo "Error: 缺少必填参数 market（market=HKT/ET）" >&2
  exit 1
fi

case "$MARKET" in
  HKT|ET) ;;
  *)
    echo "Error: market 必须是 HKT 或 ET，当前为 '$MARKET'" >&2
    exit 1
    ;;
esac

# 定位项目根 = 脚本所在 scripts/ 上四级
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="${PROJECT_ROOT:-$(cd "$SCRIPT_DIR/../../../.." && pwd)}"
SIGNALS_DIR="$PROJECT_ROOT/signals"
SIGNAL_FILE="$SIGNALS_DIR/$(date "+%Y-%m-%d")-${MARKET}-signals.md"
LOG_FILE="$SIGNALS_DIR/ring-log.csv"

# 时间戳：优先读 ring-log 末行（= 响铃时刻，与下单基准一致）；ring-log 不存在则用当前时间
if [ -f "$LOG_FILE" ]; then
  RING_TS="${RING_TS:-$(tail -1 "$LOG_FILE" | cut -d',' -f1)}"
else
  RING_TS="${RING_TS:-$(date "+%Y-%m-%d %H:%M:%S")}"
fi

# 与前一条信号之间空一行分隔（文件已非空时先补一个空行）
[ -s "$SIGNAL_FILE" ] && echo "" >> "$SIGNAL_FILE"
# 信号标题框线内带发信号时间戳（标题行后、第二条框线前；= 响铃时刻，2026-07-29 用户立）
# CONTENT 前 3 行 = ═══(上框线)、标题、═══(下框线)；awk 在第 2 行(标题)后插入时间戳行
printf '%s\n' "$CONTENT" | awk -v ts="$RING_TS" 'NR==2{print; print "> ⏰ 发信号时间：" ts; next} 1' >> "$SIGNAL_FILE"
