#!/bin/bash
# 启用合盖盯盘（防系统睡眠）。
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

if [ "$SRC" = "电池" ]; then
  echo "⚠️ 电池供电：caffeinate -s 防不住合盖睡眠（硬件强制），仅防空闲维护睡眠。强烈建议接电源。"
fi

# 后台启动 caffeinate -s，脱离当前 shell（nohup + disown：本脚本退出后继续存活）
nohup caffeinate -s >/dev/null 2>&1 &
disown 2>/dev/null || true
sleep 0.5

if pgrep -f "caffeinate -s" >/dev/null 2>&1; then
  echo "☕ caffeinate -s 已启动（${SRC}·防合盖睡眠）"
  echo "停用：bash .claude/skills/keep-awake/scripts/off.sh"
else
  echo "⚠️ caffeinate 启动失败" >&2
  exit 1
fi
