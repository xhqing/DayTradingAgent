#!/bin/bash
# 美股 12:00 ET 停盯前强制提醒 Stop hook（2026-07-18 立）。
# 11:45-12:00 ET 窗口（北京夏令时 23:45-23:59 / 冬令时次日 00:45-00:59），
# 每日首次进入窗口时 block Claude 停止 + 注入提醒，过 trade skill「分情况策略」检查。
# /tmp 标志文件防每日重复（每日只 block 一次）。
# 调试：用 NOW/DOW/TODAY/FLAGDIR 环境变量覆盖，测窗口与去重逻辑。

now="${NOW:-$(date "+%H:%M")}"
dow="${DOW:-$(date "+%u")}"           # 1=Mon ... 7=Sun
today="${TODAY:-$(date "+%Y%m%d")}"
flagdir="${FLAGDIR:-/tmp}"
flag="${flagdir}/us-stop-alert-${today}.flag"

# 周一到周五
{ [ "$dow" -ge 1 ] 2>/dev/null && [ "$dow" -le 5 ] 2>/dev/null; } || exit 0

# 夏令时窗口(23:45-23:59) 或 冬令时窗口(00:45-00:59)
in_window=false
if [[ "$now" > "23:44" ]] && [[ "$now" < "23:59" ]]; then in_window=true; fi
if [[ "$now" > "00:44" ]] && [[ "$now" < "00:59" ]]; then in_window=true; fi
$in_window || exit 0

# 每日只提醒一次
[ -f "$flag" ] && exit 0
touch "$flag" 2>/dev/null

# block + 注入提醒（harness 执行，不靠 agent 自觉）
cat <<'EOF'
{"decision":"block","reason":"⚠️ 临近美股 12:00 ET 停盯（剩约15分钟）。过 trade skill「交易策略纪律·临近强平/停盯硬截止的分情况策略」检查：若持仓浮盈大 + 加速赶极端（做多：加速上涨+破前高+远离VWAP上方超买；做空：加速下跌+破前低+远离VWAP下方超卖 = 动能将竭）→ 必须主动平仓🔴锁利，不赌 12:00-15:45 不可盯的午盘（2026-07-17 MU 留单回吐~$2000教训）；不适用（无持仓/非此情境）则忽略。"}
EOF
