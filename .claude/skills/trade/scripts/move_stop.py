#!/usr/bin/env python3
"""移动止损动作脚本（模拟盘模式）。

不删除旧止损单，直接新增止损条件单。
旧止损单留在更宽价位做备用兜底（移动止损只朝有利方向移）。

用法：
  python3 move_stop.py <symbol> <direction> <new_stop_price> <quantity>
    symbol          标的代码
    direction       long / short（持仓方向：long→止损 Sell；short→止损 Buy）
    new_stop_price  新止损价
    quantity        持仓数量（止损触发时平仓的数量）
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from trade_utils import load_config, submit_stop_order


def main():
    if len(sys.argv) < 5:
        print(
            "用法: python3 move_stop.py <symbol> <direction> <new_stop_price> <quantity>\n"
            "  symbol          标的代码\n"
            "  direction       long / short（当前持仓方向）\n"
            "  new_stop_price  新止损价\n"
            "  quantity        持仓数量",
            file=sys.stderr,
        )
        sys.exit(1)

    symbol = sys.argv[1]
    direction = sys.argv[2]
    new_stop_price = float(sys.argv[3])
    quantity = int(float(sys.argv[4]))

    if direction not in ("long", "short"):
        print(json.dumps({"ok": False, "error": f"direction 必须是 long/short，收到 '{direction}'"}))
        sys.exit(1)

    try:
        config = load_config()
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"长桥配置加载失败: {e}"}))
        sys.exit(1)

    # 移动止损方向：做多→新止损卖出；做空→新止损买回
    stop_side = "Sell" if direction == "long" else "Buy"

    result_base = {
        "action": "move_stop",
        "symbol": symbol,
        "direction": direction,
        "new_stop_price": new_stop_price,
        "quantity": quantity,
    }

    try:
        stop_order_id = submit_stop_order(config, symbol, stop_side, quantity, new_stop_price)
    except Exception as e:
        result_base.update({"ok": False, "error": f"止损单提交失败: {e}"})
        print(json.dumps(result_base, ensure_ascii=False))
        sys.exit(1)

    result_base.update({
        "ok": True,
        "stop_order_id": stop_order_id,
        "note": "旧止损单未删除，保留在更宽价位做备用兜底",
    })
    print(json.dumps(result_base, ensure_ascii=False))


if __name__ == "__main__":
    main()
