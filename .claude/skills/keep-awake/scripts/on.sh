#!/bin/bash
# 启用盯盘防系统睡眠（2026-08-09 跨平台适配：macOS 用 caffeinate、Windows 用 keepawake.py）。
#   macOS：caffeinate -s 创建 PreventSystemSleep assertion，防合盖(Clamshell)+维护(Maintenance)睡眠；
#          电池下合盖是硬件强制睡眠（软件防不住），但仍启动（防空闲维护睡眠），强烈建议接电源。
#   Windows：keepawake.py 常驻进程（ctypes 调 SetThreadExecutionState ES_SYSTEM_REQUIRED，
#          阻止系统自动睡眠，等价 caffeinate -s 的 assertion；进程退出即解除）。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

case "$(uname)" in
  MINGW*|MSYS*|CYGWIN*)
    # ---------- Windows：keepawake.py 常驻进程 ----------
    # 已在跑则跳过（避免重复启动）
    if powershell -NoProfile -Command "if (Get-CimInstance Win32_Process | Where-Object { \$_.CommandLine -like '*keepawake.py*' }) { exit 0 } else { exit 1 }" >/dev/null 2>&1; then
      echo "☕ keepawake.py 已在跑（防系统睡眠）"
      exit 0
    fi
    nohup python "$SCRIPT_DIR/keepawake.py" >/dev/null 2>&1 &
    disown 2>/dev/null || true
    sleep 1
    if powershell -NoProfile -Command "if (Get-CimInstance Win32_Process | Where-Object { \$_.CommandLine -like '*keepawake.py*' }) { exit 0 } else { exit 1 }" >/dev/null 2>&1; then
      echo "☕ keepawake.py 已启动（防系统睡眠）"
      echo "停用：bash .claude/skills/keep-awake/scripts/off.sh"
    else
      echo "⚠️ keepawake.py 启动失败（确认 python 在 PATH）" >&2
      exit 1
    fi
    ;;
  *)
    # ---------- macOS/Linux：caffeinate -s ----------
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
      echo "停用：bash .claude/skills/keep-awake/scripts/off.sh"
    else
      echo "⚠️ caffeinate 启动失败" >&2
      exit 1
    fi
    ;;
esac
