#!/bin/bash
# 写交易动作文件——下单成功的【最后一个动作】
# 2026-07-30 立：模拟盘模式下，AI 调用脚本下单成功后记录交易动作到 actions/ 目录。
# 2026-08-06 修：增加市场参数 market（hkt/et）——此前写死 ET 后缀，港股动作误写入
#   ET 文件（2026-08-06 上午 01888 四条记录全进了 2026-08-06-ET-actions.md）。
# 2026-08-16 修：market / mode 两个参数改为【必填】（无默认值、缺参报错退出）——
#   默认值已两次诱发事故（2026-08-06 忘传 market 港股进 ET 文件、2026-08-13 忘传
#   mode 读 ring-log 旧末行致动作时间倒流一天），去掉向后兼容、强制显式传参。
# 内容与信号文件大致相同，但框架是「动作」而非「信号」。
#
# 用法（交易动作内容是多行 markdown，经 stdin 传入）：
#   cat <<'ACTION' | bash log_action.sh <market> <mode>
#   ## 🟢🟢🟢 开仓 · <标的代码> <中文名> · 做多/做空 🟢🟢🟢
#   （完整交易动作内容：标题 + 表格 + 依据 + 下单结果）
#   ACTION
#     market    hkt（港股）/ et（美股），必填：写入 actions/YYYY-MM-DD-HKT/ET-actions.md
#     mode      auto / signal，必填：auto 用当前时间戳、signal 读 ring-log 末行
#
# 行为：
#   ① 把 stdin 交易动作内容 append 到 actions/YYYY-MM-DD-{HKT,ET}-actions.md
#   （按 market 参数选后缀）
#   ② 动作正文首行带时间戳——auto 模式 = 当前时间（下单时刻）；signal 模式读
#      ring-log.csv 末行（= 拍板时刻），无则用当前时间。
#
# 调试 / 测试：可用环境变量覆盖
#   PROJECT_ROOT=xxx   覆盖项目根
#   ACTION_TS=xxx      覆盖时间戳

set -uo pipefail

# 市场参数：$1 = hkt / et（2026-08-16 起必填——默认值曾致港股动作误入 ET 文件）
if [ $# -lt 1 ] || [ -z "${1:-}" ]; then
  echo "Error: 缺少 market 参数——用法：bash log_action.sh <hkt|et> <auto|signal>（两个参数必填，2026-08-16 起无默认值）" >&2
  exit 1
fi
MARKET="$1"
case "$MARKET" in
  hkt) MARKET_SUFFIX="HKT" ;;
  et)  MARKET_SUFFIX="ET" ;;
  *)
    echo "Error: 未知市场参数 '$MARKET'（应为 hkt / et）" >&2
    exit 1
    ;;
esac

# 模式参数：$2 = auto / signal（2026-08-16 起必填——默认 signal 曾致 auto 动作
# 误读 ring-log 旧末行、时间倒流一天：2026-08-13 两条动作标成 2026-08-12 13:45:43）。
# auto 模式动作时间 = 当前时间（下单时刻）；signal 模式才读 ring-log（拍板时刻 = 响铃时刻）。
if [ $# -lt 2 ] || [ -z "${2:-}" ]; then
  echo "Error: 缺少 mode 参数——用法：bash log_action.sh <hkt|et> <auto|signal>（两个参数必填，2026-08-16 起无默认值）" >&2
  exit 1
fi
MODE="$2"
case "$MODE" in
  auto|signal) : ;;
  *)
    echo "Error: 未知模式参数 '$MODE'（应为 auto / signal）" >&2
    exit 1
    ;;
esac

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
ACTION_FILE="$ACTIONS_DIR/$(date "+%Y-%m-%d")-${MARKET_SUFFIX}-actions.md"
LOG_FILE="$PROJECT_ROOT/signals/ring-log.csv"

# 时间戳：auto 模式直接用当前时间（下单时刻）；signal 模式读 ring-log 末行（= 拍板时刻）；
# ring-log 不存在时 fallback 当前时间。
if [ "$MODE" = "auto" ]; then
  ACTION_TS="${ACTION_TS:-$(date "+%Y-%m-%d %H:%M:%S")}"
elif [ -f "$LOG_FILE" ]; then
  ACTION_TS="${ACTION_TS:-$(tail -1 "$LOG_FILE" | cut -d',' -f1)}"
else
  ACTION_TS="${ACTION_TS:-$(date "+%Y-%m-%d %H:%M:%S")}"
fi

# 与前一条动作之间空一行分隔（文件已非空时先补一个空行）——只在预检通过后才写（见下）。
# 标题行插入时间戳（与 log_signal.sh 同样逻辑）；2026-09-18 重写（T144 + T154，两次盘中实录）：
#   ① 标题行判定剥离 Markdown 标题前缀（^#+\s*）后再看 emoji 是否在行首——此前只认
#      「emoji 在第 1 字符」，SKILL.md 示例格式（## 🟢🟢🟢 开仓 …）全部落空，时间戳被
#      兜底追加到条目末尾 → account_status._parse_actions 按标题行分节后平仓节内无时间行
#      → ts='' 排最前 → close 先于 open → 持仓推导残留 open 仓位 → 采样段持续误报
#      「账户已无持仓但 actions 无平仓记录」（误报淹没真警报，09-11 / 09-16 两次实录）；
#   ② 内容里已手写「⏰ 动作时间」行时不再插入（避免双时间戳）；
#   ③ 预检不到标题行 → 拒写 + 非零退出（田「兜底追加末尾」在时间戳顺序敏感的 actions
#      文件里是危险默认值——宁可让 AI 看到报错重发，也不把坏数据写进文件）。
if ! printf '%s\n' "$CONTENT" | awk '
  { line = $0; sub(/^#+[ \t]*/, "", line)
    if (index(line,"🟢")==1 || index(line,"🔴")==1 || index(line,"🟡")==1 || index(line,"🔵")==1) found=1 }
  END { exit(found ? 0 : 1) }'; then
  echo "Error: 未检测到动作标题行（行首或 Markdown 标题（## ）后以 🟢🔴🟡🔵 开头，如「## 🟢🟢🟢 开仓 · …」）——拒写；时间戳位置对解析很关键，宁可不写也不写错位数据，请补标题行后重发" >&2
  exit 1
fi
[ -s "$ACTION_FILE" ] && echo "" >> "$ACTION_FILE"
printf '%s\n' "$CONTENT" | awk -v ts="$ACTION_TS" '
  BEGIN { has_ts = 0 }
  /⏰[ \t]*动作时间/ { has_ts = 1 }
  { lines[NR] = $0; keep[NR] = 1 }
  END {
    for (i = 1; i <= NR; i++) {
      print lines[i]
      if (!has_ts) {
        line = lines[i]; sub(/^#+[ \t]*/, "", line)
        if (!inserted && (index(line,"🟢")==1 || index(line,"🔴")==1 || index(line,"🟡")==1 || index(line,"🔵")==1)) {
          print "> ⏰ 动作时间：" ts; inserted = 1
        }
      }
    }
    if (has_ts)
      print "log_action.sh ℹ️：内容已含 ⏰ 动作时间行，未重复插入时间戳" > "/dev/stderr"
    else if (!inserted)
      print "log_action.sh ⚠️：未找到动作标题行（预检已过，理论到不了这里）" > "/dev/stderr"
  }' >> "$ACTION_FILE"
