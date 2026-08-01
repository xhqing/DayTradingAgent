#!/bin/bash
# 写交易动作文件（美股模拟盘）——下单成功的【最后一个动作】
# 2026-07-30 立：美股模拟盘模式下，AI 调用脚本下单成功后记录交易动作到 actions/ 目录。
# 内容与信号文件大致相同，但框架是「动作」而非「信号」。
#
# 用法（交易动作内容是多行 markdown，经 stdin 传入）：
#   cat <<'ACTION' | bash log_action.sh
#   ## 🟢🟢🟢 开仓 · <标的代码> <中文名> · 做多/做空 🟢🟢🟢
#   （完整交易动作内容：标题 + 表格 + 依据 + 下单结果）
#   ACTION
#
# 行为：
#   ① 把 stdin 交易动作内容 append 到 actions/YYYY-MM-DD-ET-actions.md
#   ② 动作正文首行带时间戳——读 ring-log.csv 末行（= 拍板时刻），无则用当前时间。
#
# 调试 / 测试：可用环境变量覆盖
#   PROJECT_ROOT=xxx   覆盖项目根
#   ACTION_TS=xxx      覆盖时间戳

set -uo pipefail

# 读 stdin 内容
CONTENT=$(cat)
if [ -z "$CONTENT" ]; then
  echo "Error: 交易动作内容为空——请经 stdin 传入" >&2
  exit 1
fi

# 定位项目根 = 脚本所在 scripts/ 上四级
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="${PROJECT_ROOT:-$(cd "$SCRIPT_DIR/../../../.." && pwd)}"
ACTIONS_DIR="$PROJECT_ROOT/actions"
mkdir -p "$ACTIONS_DIR"
ACTION_FILE="$ACTIONS_DIR/$(date "+%Y-%m-%d")-ET-actions.md"
LOG_FILE="$PROJECT_ROOT/signals/ring-log.csv"

# 时间戳：优先读 ring-log 末行（= 拍板时刻）；不存在则用当前时间
if [ -f "$LOG_FILE" ]; then
  ACTION_TS="${ACTION_TS:-$(tail -1 "$LOG_FILE" | cut -d',' -f1)}"
else
  ACTION_TS="${ACTION_TS:-$(date "+%Y-%m-%d %H:%M:%S")}"
fi

# 与前一条动作之间空一行分隔（文件已非空时先补一个空行）
[ -s "$ACTION_FILE" ] && echo "" >> "$ACTION_FILE"
# 标题框线内带时间戳（与 log_signal.sh 同样逻辑）
printf '%s\n' "$CONTENT" | awk -v ts="$ACTION_TS" 'NR==2{print; print "> ⏰ 动作时间：" ts; next} 1' >> "$ACTION_FILE"
