#!/usr/bin/env python3
"""港股移动止损动作脚本（长桥模拟盘备选账户，与美股 move_stop.py 解耦）。

给港股已有持仓加 MIT 止损市价单（与持仓反向）。**先下新止损单、再撤旧止损单**（仓位持续保护无空窗），
量严格=持仓量（超持仓券商判失效）。触发价取整到港股 tick。仅处理港股。

⚠️ 港股长桥是**备选账户**——默认老虎，用户特别说明才用本脚本。

用法：
  python3 move_stop_hk.py <symbol> <direction> <new_stop_price> <quantity>
    symbol          港股代码（HK.02800）
    direction       long / short（持仓方向：long→止损 Sell；short→止损 Buy）
    new_stop_price  新止损价
    quantity        持仓数量（严格=持仓量）
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import trade_utils_hk as U


def main():
    if len(sys.argv) < 5:
        print("用法: python3 move_stop_hk.py <symbol> <direction> <new_stop_price> <quantity>", file=sys.stderr)
        sys.exit(1)

    symbol = sys.argv[1]
    direction = sys.argv[2]
    new_stop_price = float(sys.argv[3])
    quantity = int(float(sys.argv[4]))

    if not symbol.startswith("HK."):
        print(json.dumps({"ok": False, "error": f"本脚本只处理港股（HK.xxx），收到 {symbol}"}))
        sys.exit(1)
    if direction not in ("long", "short"):
        print(json.dumps({"ok": False, "error": f"direction 非法 '{direction}'"}))
        sys.exit(1)

    try:
        config = U.load_config()
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"配置加载失败: {e}"}))
        sys.exit(1)

    stop_side = "Sell" if direction == "long" else "Buy"
    result_base = {"action": "move_stop_hk", "market": "HK", "symbol": symbol, "direction": direction,
                   "new_stop_price": new_stop_price, "quantity": quantity}

    # 量校验：本策略规定止损量严格=持仓量（超持仓会被券商判失效）
    try:
        pos = U.get_open_position_hk(config, symbol)
        if pos is not None:
            held = pos["quantity"]
            if quantity > held:
                result_base.update({"ok": False, "error": f"止损量 {quantity} 超过港股持仓量 {held}，券商判失效，拒绝提交"})
                print(json.dumps(result_base, ensure_ascii=False))
                sys.exit(1)
            if quantity < held:
                result_base["warning"] = f"止损量 {quantity} < 持仓量 {held}（策略规定严格相等）"
        else:
            result_base["warning"] = "未读到港股持仓，按传入数量继续"
    except Exception as e:
        result_base["quantity_check_warning"] = f"持仓校验失败（继续）: {e}"

    # ① 先下新止损单（仓位持续有止损保护、无裸奔空窗——先新增后撤旧）+ 触发价取整到港股 tick
    trig = U.round_to_tick_hk(new_stop_price)
    try:
        stop_order_id = U.submit_stop_order_hk(config, symbol, stop_side, quantity, trig)
    except Exception as e:
        result_base.update({"ok": False, "error": f"新止损单提交失败（旧止损未撤、仍保护仓位）: {e}"})
        print(json.dumps(result_base, ensure_ascii=False))
        sys.exit(1)

    # ② 再撤旧止损单（排除刚下的新止损）
    try:
        n, ids = U.cancel_all_stop_orders_hk(config, symbol, exclude_order_id=stop_order_id)
        result_base["prior_stops_cancelled"] = n
        if n > 0:
            result_base["cancelled_order_ids"] = ids
    except Exception as e:
        result_base["prior_stops_cancelled_warning"] = f"撤旧止损单失败（需手动，新止损已就位）: {e}"

    result_base.update({"ok": True, "stop_order_id": stop_order_id, "trigger_price": trig,
                        "stop_method": "MIT 市价止损单（触发后市价成交）",
                        "note": "先新增再撤旧（仓位持续保护无空窗），仅留本笔为唯一活动止损"})
    print(json.dumps(result_base, ensure_ascii=False))


if __name__ == "__main__":
    main()
