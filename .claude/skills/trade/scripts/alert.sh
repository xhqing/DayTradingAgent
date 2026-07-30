#!/bin/bash
# ⚠️ 已弃用（2026-07-29）——响铃和写信号文件分离，改用两个脚本：
#   - 拍板后【第一个动作】（响铃，即时）：bash ring.sh <type> [symbol] [note]
#   - 【最后一个动作】（写文件，复盘用）：cat <<'SIGNAL' ... SIGNAL | bash log_signal.sh <market>
#
# 为什么弃用（2026-07-29 用户立）：原 alert.sh「写文件 + 响铃」二合一，写文件（构造完整 markdown）
# 有延迟、会让响铃滞后几秒～十几秒；用户实战下单靠「听到铃 + 对话关键字段」（秒级），
# 信号文件主要用于复盘（太慢、不靠它下单）。故拆分：响铃单飞（ring.sh）、写文件另走（log_signal.sh）。
#
# 详见 ring.sh / log_signal.sh 头注释 + SKILL.md「信号格式 · 🔔 信号时序总则」。
# 本脚本保留只为向后兼容提示，不再执行实际动作（调用即报错退出、提示用新脚本）。

echo "⚠️ alert.sh 已弃用（2026-07-29，响铃与写文件分离）。" >&2
echo "  拍板后第一个动作（响铃）：bash ring.sh <type> [symbol] [note]" >&2
echo "  最后一个动作（写文件）：  cat <<'SIGNAL' ... SIGNAL | bash log_signal.sh <market>" >&2
exit 1
