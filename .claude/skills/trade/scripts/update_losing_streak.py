#!/usr/bin/env python3
"""连败计数手动更新 CLI（2026-08-31 T131 配套，一级降频线数据源）。

为什么需要独立 CLI：close_position_tiger(_us).py 平仓成交后自动更新连败文件
（tmp/losing_streak.json），但 **BRACKETS 止损/止盈腿自动触发的平仓不走 close 脚本**
（券商侧条件单直接成交，如 2026-08-27 01888 PROFIT 腿 15:34 自动触发）——该路径的
连败计数靠两处补：① 盯盘中断恢复后的持仓闭环回查（resume.py 流程）发现已平仓、
补记平仓时顺手跑本脚本；② 停盯总结闭环检查（monitor_unregister.sh）发现未闭环时由
AI 按 K 线推定成交价后跑本脚本。不补则连败漏记、降频闸漏拦。

用法（平仓侧参数从 actions 开仓记录 + 推定/实测平仓价取）：
  python3 update_losing_streak.py <market> <symbol> <direction> <entry> <stop> <quantity> <fill_price> [net_pnl]
    market      HK / US
    symbol      富途格式代码（HK.09988 / US.MU）
    direction   long / short
    entry       开仓价（actions 记录的成交价）
    stop        开仓时的止损价（与 entry 算止损距）
    quantity    平仓股数
    fill_price  平仓成交价（自动触发单有回查价；推定口径按当日 K 线、结果里会标 basis）
    net_pnl     可选：实测净利（App 口径）；缺省按 fee_schedule 估

输出 JSON：{r_multiple, r_basis, streak, recent_r, gate_triggered_today}。
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import trade_utils_tiger as U


def main():
    if len(sys.argv) < 8:
        print(__doc__, file=sys.stderr)
        sys.exit(1)
    market, symbol, direction = sys.argv[1], sys.argv[2], sys.argv[3]
    entry, stop = float(sys.argv[4]), float(sys.argv[5])
    quantity = int(float(sys.argv[6]))
    fill_price = float(sys.argv[7])
    net_pnl = float(sys.argv[8]) if len(sys.argv) > 8 else None
    if market not in ("HK", "US"):
        print(json.dumps({"ok": False, "error": f"market 必须是 HK/US，收到 '{market}'"}))
        sys.exit(1)
    stop_dist = abs(entry - stop)
    result = U.update_losing_streak(market, symbol, direction, entry, stop_dist,
                                    quantity, fill_price, net_pnl=net_pnl)
    result.setdefault("ok", "skipped" not in result)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
