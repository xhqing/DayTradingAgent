#!/usr/bin/env python3
"""平仓动作脚本。

清空全部持仓。智能下单（先限价争取更优成交价，未成交改市价确保成交）。
平仓成功后自动撤销该标的所有未触发止损条件单。

用法：
  python3 close_position.py <symbol> <direction> <quantity>
    symbol    标的代码
    direction long / short（持仓方向：long=持多→卖出平仓；short=持空→买回平仓）
    quantity  平仓数量
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from trade_utils import (
    load_config,
    get_quote,
    smart_order,
    cancel_all_stop_orders,
)


def main():
    if len(sys.argv) < 4:
        print(
            "用法: python3 close_position.py <symbol> <direction> <quantity>\n"
            "  symbol    标的代码\n"
            "  direction long / short（当前持仓方向）\n"
            "  quantity  平仓数量",
            file=sys.stderr,
        )
        sys.exit(1)

    symbol = sys.argv[1]
    direction = sys.argv[2]
    quantity = int(float(sys.argv[3]))

    if direction not in ("long", "short"):
        print(json.dumps({"ok": False, "error": f"direction 必须是 long/short，收到 '{direction}'"}))
        sys.exit(1)

    try:
        config = load_config()
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"长桥配置加载失败: {e}"}))
        sys.exit(1)

    close_side = "Sell" if direction == "long" else "Buy"

    result_base = {
        "action": "close_position",
        "symbol": symbol,
        "direction": direction,
        "quantity": quantity,
        "close_side": close_side,
    }

    try:
        quote = get_quote(config, symbol)
    except Exception as e:
        result_base.update({"ok": False, "error": f"获取报价失败: {e}"})
        print(json.dumps(result_base, ensure_ascii=False))
        sys.exit(1)

    if quote is None:
        result_base.update({"ok": False, "error": f"报价为空: {symbol}"})
        print(json.dumps(result_base, ensure_ascii=False))
        sys.exit(1)

    current_price = quote["last"]

    # 智能下单（先限价单争取更优成交价，未成交则改市价单确保成交）
    try:
        order_id, fill_price, method = smart_order(
            config, symbol, close_side, quantity, quote
        )
    except Exception as e:
        result_base.update({"ok": False, "error": f"平仓失败: {e}"})
        print(json.dumps(result_base, ensure_ascii=False))
        sys.exit(1)

    result_base.update({
        "ok": True,
        "order_id": order_id,
        "fill_price": fill_price,
        "method": method,
    })

    # 平仓成功后撤销该标的所有未触发止损单（不撤会意外触发产生反向持仓）
    try:
        n_cancelled, cancelled_ids = cancel_all_stop_orders(config, symbol)
        result_base["stop_orders_cancelled"] = n_cancelled
        if n_cancelled > 0:
            result_base["cancelled_order_ids"] = cancelled_ids
    except Exception as e:
        result_base["stop_orders_cancelled_warning"] = f"撤销止损单失败（需手动撤销）: {e}"

    print(json.dumps(result_base, ensure_ascii=False))


if __name__ == "__main__":
    main()
