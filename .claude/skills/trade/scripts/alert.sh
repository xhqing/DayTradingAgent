#!/bin/bash
# ⚠️ 已弃用（2026-07-29）——写文件、响铃、取价分离，改用三个动作：
#   - 拍板后【第一个动作】（写信号文件，先落盘）：cat <<'SIGNAL' ... SIGNAL | bash log_signal.sh <market>
#   - 【第二个动作】（响铃，用户听到即下单）：bash ring.sh <type> [symbol] [note]
#   - 【第三个动作】（响铃后立即 snapshot 取响铃时刻价，成交价近似）
#
# 为什么弃用（2026-07-29 用户立）：原 alert.sh「写文件 + 响铃」二合一，写文件（构造完整 markdown）
# 有延迟、会让响铃滞后几秒～十几秒；用户实战下单靠「听到铃 + 对话关键字段」（秒级），
# 信号文件主要用于复盘（太慢、不靠它下单）。故拆分：写文件（log_signal.sh）、响铃（ring.sh）、
# 取价（snapshot）三步独立；2026-08-02 起按「写文件 → 响铃 → 取价」三步连贯执行。
#
# 详见 ring.sh / log_signal.sh 头注释 + references/signal-mode.md「信号时序总则」。
# 本脚本保留只为向后兼容提示，不再执行实际动作（调用即报错退出、提示用新脚本）。

echo "⚠️ alert.sh 已弃用（2026-07-29，响铃与写文件分离）。" >&2
echo "  拍板后第一个动作（写信号文件）：cat <<'SIGNAL' ... SIGNAL | bash log_signal.sh <market>" >&2
echo "  第二个动作（响铃）：bash ring.sh <type> [symbol] [note]" >&2
echo "  第三个动作（响铃后取价）：snapshot 取 last_price 当成交价近似" >&2
exit 1
