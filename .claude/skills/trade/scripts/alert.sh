#!/bin/bash
# 交易信号声音提醒（macOS）。AI 发四类正式信号时调用——只响一声系统提示音（不要语音朗读）。
# 用法: bash alert.sh <type>
#   type : open=开仓🟢  close=平仓🔴  ts=移动止损🟡  tp=移动止盈🟠
# 行为: 按信号类型播一声对应系统音（不同信号不同音色，便于听声辨识）。
# 调整: 想更响/换音色改下方 SOUND 映射；系统音清单 ls /System/Library/Sounds/

case "${1:-}" in
  open)  SOUND="Glass" ;;      # 开仓：清脆叮
  close) SOUND="Hero" ;;       # 平仓：上扬号角
  ts)    SOUND="Submarine" ;;  # 移动止损：低沉咚
  tp)    SOUND="Ping" ;;       # 移动止盈：短促 ping
  *)     SOUND="Funk" ;;       # 兜底
esac

afplay "/System/Library/Sounds/${SOUND}.aiff" 2>/dev/null
