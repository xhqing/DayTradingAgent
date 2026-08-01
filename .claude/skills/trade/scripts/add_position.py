#!/usr/bin/env python3
"""加仓动作脚本（模拟盘模式）。

与开仓相同的 6 要素校验 + 下单 + 止损条件单。
加仓 = 独立一笔交易，独立止损、独立预算。

用法：
  python3 add_position.py <symbol> <direction> <entry_ref> <stop_loss> <target> <quantity>
    （参数含义同 open_position.py）
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from trade_utils import (
    load_config,
    get_quote,
    submit_limit_order,
    submit_stop_order,
    check_price_in_range,
    calc_position_size,
    load_equity,
)


def main():
    if len(sys.argv) < 7:
        print(
            "用法: python3 add_position.py <symbol> <direction> <entry_ref> "
            "<stop_loss> <target> <quantity>\n"
            "  symbol    标的代码\n"
            "  direction long / short\n"
            "  entry_ref 参考价\n"
            "  stop_loss 止损价（本笔独立止损）\n"
            "  target    目标止盈价\n"
            "  quantity  加仓数量（0=自动算）",
            file=sys.stderr,
        )
        sys.exit(1)

    symbol = sys.argv[1]
    direction = sys.argv[2]
    entry_ref = float(sys.argv[3])
    stop_loss = float(sys.argv[4])
    target = float(sys.argv[5])
    quantity = int(float(sys.argv[6]))

    if direction not in ("long", "short"):
        print(json.dumps({"ok": False, "error": f"direction 必须是 long/short，收到 '{direction}'"}))
        sys.exit(1)

    try:
        config = load_config()
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"长桥配置加载失败: {e}"}))
        sys.exit(1)

    try:
        quote = get_quote(config, symbol)
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"获取报价失败: {e}"}))
        sys.exit(1)

    if quote is None:
        print(json.dumps({"ok": False, "error": f"报价为空: {symbol}"}))
        sys.exit(1)

    current_price = quote["last"]

    in_range, range_low, range_high, odds_at_ref, odds_at_current = check_price_in_range(
        direction, current_price, entry_ref, stop_loss, target
    )

    result_base = {
        "action": "add_position",
        "symbol": symbol,
        "direction": direction,
        "entry_ref": entry_ref,
        "stop_loss": stop_loss,
        "target": target,
        "current_price": current_price,
        "range_low": round(range_low, 4),
        "range_high": round(range_high, 4),
        "odds_at_ref": round(odds_at_ref, 2),
        "odds_at_current": round(odds_at_current, 2),
    }

    if not in_range:
        result_base.update({
            "ok": False,
            "error": (
                f"当前价 {current_price} 不在可接受范围 [{range_low:.4f}, {range_high:.4f}] 内。"
                f"参考价处初始预期赔率 {odds_at_ref:.2f}，当前价处修正预期赔率 {odds_at_current:.2f}。"
            ),
        })
        print(json.dumps(result_base, ensure_ascii=False))
        sys.exit(1)

    if quantity == 0:
        equity = load_equity()
        stop_distance = abs(entry_ref - stop_loss)
        lot_size = 100 if symbol.startswith("HK.") else 1
        quantity, max_loss, budget_B = calc_position_size(
            equity, 0.02, 0.025, stop_distance, lot_size
        )
        if quantity <= 0:
            result_base.update({"ok": False, "error": "计算出的仓位为 0"})
            print(json.dumps(result_base, ensure_ascii=False))
            sys.exit(1)
        result_base.update({
            "auto_sized": True,
            "equity": equity,
            "budget_B": round(budget_B, 2),
            "max_loss": round(max_loss, 2),
        })

    result_base["quantity"] = quantity

    try:
        order_id, fill_price = submit_limit_order(
            config, symbol, "Buy" if direction == "long" else "Sell", quantity, current_price
        )
    except Exception as e:
        result_base.update({"ok": False, "error": f"限价单提交失败: {e}"})
        print(json.dumps(result_base, ensure_ascii=False))
        sys.exit(1)

    result_base.update({"order_id": order_id, "fill_price": fill_price})

    stop_side = "Sell" if direction == "long" else "Buy"
    try:
        stop_order_id = submit_stop_order(config, symbol, stop_side, quantity, stop_loss)
    except Exception as e:
        result_base.update({
            "ok": True,
            "warning": f"止损单提交失败（需手动补挂）: {e}",
            "stop_order_id": None,
        })
        print(json.dumps(result_base, ensure_ascii=False))
        sys.exit(0)

    result_base.update({"ok": True, "stop_order_id": stop_order_id})
    print(json.dumps(result_base, ensure_ascii=False))


if __name__ == "__main__":
    main()
