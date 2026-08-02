#!/bin/bash
# 写信号文件（复盘用）——发信号流程的【第一个动作】，拍板即写、信号内容先落盘
# 2026-08-02 用户立：发信号三步时序 = 写信号文件 → 响铃 → 取响铃时刻价，
# 三步连贯执行、中间不插入任何其它动作，取到的响铃实测价才能近似实际成交价。
# 本脚本是第一步：信号内容先完整落盘（响铃时文件已就绪，用户可查字段下单）；
# 响铃（ring.sh）与取价（snapshot）在写文件之后立即连续执行。
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
#   ② 信号正文首行带发信号时间戳——取写文件当下时刻（≈ AI 拍板时刻）。
# 只写文件、不响铃——响铃是第二个动作（ring.sh）。
#
# 为什么时间戳取写文件当下时刻（而非读 ring-log）：
#   本脚本是发信号第一个动作、跑在响铃之前，写文件时 ring-log 还没有本次信号的
#   响铃记录（若读 ring-log 末行会错误引用上一条信号的响铃时刻）。三步连贯执行
#   （几秒内完成），写文件时刻 ≈ AI 拍板时刻 ≈ 响铃时刻（前后相差仅秒级），
#   复盘以写文件时刻（= 发信号时刻）对齐。响铃时刻由 ring.sh 另记 ring-log
#   （复盘匹配响铃时刻价用）。
#
# 调试 / 测试：可用环境变量覆盖
#   PROJECT_ROOT=xxx   覆盖项目根
#   RING_TS=xxx        覆盖时间戳（默认 date 实测 = 写文件当下时刻）

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

# 时间戳：取写文件当下时刻（≈ AI 拍板时刻 = 发信号时刻；本脚本跑在响铃之前、
# ring-log 尚无本次响铃记录、不能读它——详见头部注释）
RING_TS="${RING_TS:-$(date "+%Y-%m-%d %H:%M:%S")}"

# 与前一条信号之间空一行分隔（文件已非空时先补一个空行）
[ -s "$SIGNAL_FILE" ] && echo "" >> "$SIGNAL_FILE"
# 信号标题框线内带发信号时间戳（标题行后、第二条框线前；= 响铃时刻，2026-07-29 用户立）
# CONTENT 前 3 行 = ═══(上框线)、标题、═══(下框线)；awk 在第 2 行(标题)后插入时间戳行
printf '%s\n' "$CONTENT" | awk -v ts="$RING_TS" 'NR==2{print; print "> ⏰ 发信号时间：" ts; next} 1' >> "$SIGNAL_FILE"
