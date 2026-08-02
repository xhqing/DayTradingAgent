#!/usr/bin/env python3
"""平仓动作脚本（一键平仓，市价单 MO）。

长桥**没有**专门的平仓接口（2026-08-01 已核实：App 上的「平仓」按钮 = 读持仓 +
反向下 submit_order）。本脚本读持仓自动算出方向和数量，用**市价单 MO**反向下单——
平仓是「方向不看好、要立即成交」，MO 保证成交确定性、允许价格往不利方向偏移
（区别于开仓用 LO 控价、只往更优方向偏）。

平仓成功后撤销该标的全部未触发 MIT 止损单（防反向开仓——平仓后账户空仓，残留 MIT
止损单若被价格触发会被长桥接受、反向开仓；**这属于本交易策略平仓动作的一部分**）。

用法：
  python3 close_position.py [symbol] [direction] [quantity]
    symbol    可选；不给则平账户里唯一持仓（账户有多个持仓时必须指定 symbol）
    direction 可选（long/short）；不给则从持仓自动判断
    quantity  可选；不给则从持仓读全部数量

三种调用都支持：
  close_position.py             # 一键平掉唯一持仓
  close_position.py US.MU       # 指定标的、方向+量自动读
  close_position.py US.MU long 158   # 全显式（旧用法兼容）

输出 JSON：
  成功：{ok, action, symbol, direction, quantity, close_side, order_id, fill_price,
         method:"market", stop_orders_cancelled, ...}
  失败：{ok:false, error, ...}
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from trade_utils import (
    load_config,
    get_quote,
    submit_market_order,
    check_order_filled,
    cancel_all_stop_orders,
    get_open_position,
)


def main():
    symbol = sys.argv[1] if len(sys.argv) > 1 else None
    direction = sys.argv[2] if len(sys.argv) > 2 else None
    quantity = int(float(sys.argv[3])) if len(sys.argv) > 3 else None

    try:
        config = load_config()
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"长桥配置加载失败: {e}"}))
        sys.exit(1)

    # 读持仓补全 symbol / direction / quantity（一键平仓核心）
    if symbol is None or direction is None or quantity is None:
        pos = get_open_position(config, symbol)
        if pos is None:
            hint = f"账户无持仓" if symbol is None else f"未找到 {symbol} 的持仓"
            print(json.dumps({"ok": False, "error": hint}, ensure_ascii=False))
            sys.exit(1)
        if symbol is None:
            symbol = pos["symbol"]
        if direction is None:
            direction = pos["side"]
        if quantity is None:
            quantity = pos["quantity"]

    if direction not in ("long", "short"):
        print(json.dumps({"ok": False, "error": f"direction 必须是 long/short，收到 '{direction}'"}))
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

    # 市价单 MO 平仓（立即成交，允许不利方向价格偏移；平仓要确定性而非控价）
    try:
        order_id = submit_market_order(config, symbol, close_side, quantity)
    except Exception as e:
        result_base.update({"ok": False, "error": f"平仓市价单提交失败: {e}"})
        print(json.dumps(result_base, ensure_ascii=False))
        sys.exit(1)

    # 回查成交（MO 通常立即成交，但禁止用 last 冒充成交价——MU 事故教训）
    filled, fill_price, status = check_order_filled(config, order_id, timeout=8)
    if not filled:
        result_base.update({
            "ok": False,
            "error": f"平仓 MO 未成交（status={status}），本次平仓未成立",
            "order_id": order_id,
        })
        print(json.dumps(result_base, ensure_ascii=False))
        sys.exit(1)

    fill_price_src = "avg_fill_price"
    if fill_price is None:
        fill_price = quote["last"]
        fill_price_src = "last（MO 成交均价缺失，用最新价兜底）"

    result_base.update({
        "ok": True,
        "order_id": order_id,
        "fill_price": fill_price,
        "fill_price_source": fill_price_src,
        "method": "market",
        "main_status": status,
    })

    # 平仓动作的一部分：撤销该标的所有未触发止损单（防反向开仓：平仓后空仓，残留 MIT 触发会反向开仓）
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
