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

# 与前一条动作之间空一行分隔（文件已非空时先补一个空行）
[ -s "$ACTION_FILE" ] && echo "" >> "$ACTION_FILE"
# 标题行后带时间戳（与 log_signal.sh 同样逻辑）。
# 2026-08-31 修（T129）：原 awk NR==2 固定行号插入对空行脆弱——动作内容首两行为
# 「框线 + 空行」时（2026-08-31 实录），时间行插在标题行【前】而非【后】，
# account_status._parse_actions 按标题行分节后平仓节内无时间行 → ts='' 排到最前 →
# close 先于 open → 持仓推导残留 open 仓位 → 采样段「账户已无持仓但 actions 无平仓
# 记录」误告警持续多段。改为「首个以 🟢🔴🟡🔵 开头的行（动作标题行，标准格式 =
# 框线/标题/⏰时间/框线）后插入」；整个内容都没有 emoji 标题行时兜底追加末尾 + 警示
# （宁可位置错在末尾、不再错在标题前——标题前会破坏 _parse_actions 分节）。
printf '%s\n' "$CONTENT" | awk -v ts="$ACTION_TS" '
  !done && (index($0,"🟢")==1 || index($0,"🔴")==1 || index($0,"🟡")==1 || index($0,"🔵")==1) {
    print; print "> ⏰ 动作时间：" ts; done=1; next
  }
  {print}
  END {
    if (!done) {
      print "> ⏰ 动作时间：" ts
      print "log_action.sh ⚠️：未找到以 🟢🔴🟡🔵 开头的动作标题行，时间戳已追加在末尾——请检查动作内容格式（标准 = 框线/标题/⏰时间/框线）" > "/dev/stderr"
    }
  }' >> "$ACTION_FILE"
