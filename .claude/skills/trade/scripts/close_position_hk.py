#!/usr/bin/env python3
"""港股平仓动作脚本（长桥模拟盘备选账户，与美股 close_position.py 解耦）。

一键平仓：读**港股**持仓自动算方向+量（不会碰美股持仓），市价单 MO 立即成交。
平仓后撤该港股标的的全部未触发 MIT 止损单（防反向开仓，属平仓动作一部分）。
仅处理港股（HK.xxx）；美股用 close_position.py。

⚠️ 港股长桥是**备选账户**——默认老虎，用户特别说明才用本脚本。

用法：
  python3 close_position_hk.py [symbol] [direction] [quantity]
    不给参数 = 一键平账户唯一港股持仓
    HK.02800 / HK.02800 long 500（显式）
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import trade_utils_hk as U


def main():
    symbol = sys.argv[1] if len(sys.argv) > 1 else None
    direction = sys.argv[2] if len(sys.argv) > 2 else None
    quantity = int(float(sys.argv[3])) if len(sys.argv) > 3 else None

    if symbol and not symbol.startswith("HK."):
        print(json.dumps({"ok": False, "error": f"本脚本只处理港股（HK.xxx），收到 {symbol}"}))
        sys.exit(1)

    try:
        config = U.load_config()
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"配置加载失败: {e}"}))
        sys.exit(1)

    if symbol is None or direction is None or quantity is None:
        pos = U.get_open_position_hk(config, symbol)
        if pos is None:
            hint = "账户无港股持仓" if symbol is None else f"未找到港股 {symbol} 持仓"
            print(json.dumps({"ok": False, "error": hint}, ensure_ascii=False))
            sys.exit(1)
        if symbol is None:
            symbol = pos["symbol"]
        if direction is None:
            direction = pos["side"]
        if quantity is None:
            quantity = pos["quantity"]

    if direction not in ("long", "short"):
        print(json.dumps({"ok": False, "error": f"direction 非法 '{direction}'"}))
        sys.exit(1)
    close_side = "Sell" if direction == "long" else "Buy"

    result_base = {"action": "close_position_hk", "market": "HK", "symbol": symbol,
                   "direction": direction, "quantity": quantity, "close_side": close_side}

    quote = U.get_quote_hk(config, symbol)
    if quote is None:
        result_base.update({"ok": False, "error": f"港股报价为空: {symbol}"})
        print(json.dumps(result_base, ensure_ascii=False))
        sys.exit(1)

    # MO 市价平仓（立即成交，方向不看好时要确定性）
    try:
        order_id = U.submit_market_order_hk(config, symbol, close_side, quantity)
    except Exception as e:
        result_base.update({"ok": False, "error": f"平仓 MO 提交失败: {e}"})
        print(json.dumps(result_base, ensure_ascii=False))
        sys.exit(1)

    filled, fill_price, status = U.check_order_filled_hk(config, order_id, timeout=8)
    if not filled:
        result_base.update({"ok": False, "error": f"平仓 MO 未成交（{status}）", "order_id": order_id})
        print(json.dumps(result_base, ensure_ascii=False))
        sys.exit(1)

    fill_src = "avg_fill_price"
    if fill_price is None:
        fill_price = quote["last"]
        fill_src = "last（成交均价缺失兜底）"
    result_base.update({"ok": True, "order_id": order_id, "fill_price": fill_price,
                        "fill_price_source": fill_src, "method": "market", "main_status": status})

    # 撤该港股标的全部未触发 MIT 止损单（防反向开仓）
    try:
        n, ids = U.cancel_all_stop_orders_hk(config, symbol)
        result_base["stop_orders_cancelled"] = n
        if n > 0:
            result_base["cancelled_order_ids"] = ids
    except Exception as e:
        result_base["stop_orders_cancelled_warning"] = f"撤止损单失败（需手动）: {e}"

    print(json.dumps(result_base, ensure_ascii=False))


if __name__ == "__main__":
    main()
