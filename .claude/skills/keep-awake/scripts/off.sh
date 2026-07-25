#!/bin/bash
# 停用合盖盯盘（解除防睡眠）。
if pkill -f "caffeinate -s" 2>/dev/null; then
  echo "☕ caffeinate -s 已停用（防睡眠解除）"
else
  echo "（无 caffeinate -s 在跑，无需停用）"
fi
