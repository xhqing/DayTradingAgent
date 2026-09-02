#!/bin/bash
# 手动启用防系统睡眠（备用路径；盯盘主链路由 preflight.py 内联启用，不经本脚本）。
# 2026-09-01 自 .claude/skills/keep-awake/scripts/on.sh 迁入（keep-awake skill 已撤销、
# 功能并入 trade，见 references/monitoring.md「防睡眠机制」节）。
# AC：caffeinate -s 创建 PreventSystemSleep assertion，防合盖+维护睡眠。
# 电池：合盖是硬件强制睡眠（软件防不住），但仍启动（防空闲维护睡眠），强烈建议接电源。
set -euo pipefail

# 已在跑则跳过（避免重复启动）
if pgrep -f "caffeinate -s" >/dev/null 2>&1; then
  echo "☕ caffeinate -s 已在跑（防合盖睡眠）"
  exit 0
fi

# 检测电源
SRC="电源未知"
if out=$(pmset -g batt 2>/dev/null); then
  if echo "$out" | grep -q "AC Power"; then SRC="AC"; fi
  if echo "$out" | grep -q "Battery Power"; then SRC="电池"; fi
fi

# 2026-07-27：去掉电池供电警告（开盖盯盘无所谓电池/电源；电池下合盖是硬件强制软件防不住、但防空闲维护睡眠仍有效，统一启用、不提醒）

# 后台启动 caffeinate -s，脱离当前 shell（nohup + disown：本脚本退出后继续存活）
nohup caffeinate -s >/dev/null 2>&1 &
disown 2>/dev/null || true
sleep 0.5

if pgrep -f "caffeinate -s" >/dev/null 2>&1; then
  echo "☕ caffeinate -s 已启动（${SRC}·防合盖睡眠）"
  echo "停用：bash .claude/skills/trade/scripts/keepawake_off.sh"
else
  echo "⚠️ caffeinate 启动失败" >&2
  exit 1
fi
