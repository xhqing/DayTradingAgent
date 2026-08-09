#!/bin/bash
# 停用盯盘防系统睡眠（2026-08-09 跨平台适配：macOS 停 caffeinate、Windows 停 keepawake.py）。
case "$(uname)" in
  MINGW*|MSYS*|CYGWIN*)
    if powershell -NoProfile -Command "\$p = Get-CimInstance Win32_Process | Where-Object { \$_.CommandLine -like '*keepawake.py*' }; if (\$p) { \$p | ForEach-Object { Stop-Process -Id \$_.ProcessId -Force }; exit 0 } else { exit 1 }" >/dev/null 2>&1; then
      echo "☕ keepawake.py 已停用（防睡眠解除）"
    else
      echo "（无 keepawake.py 在跑，无需停用）"
    fi
    ;;
  *)
    if pkill -f "caffeinate -s" 2>/dev/null; then
      echo "☕ caffeinate -s 已停用（防睡眠解除）"
    else
      echo "（无 caffeinate -s 在跑，无需停用）"
    fi
    ;;
esac
