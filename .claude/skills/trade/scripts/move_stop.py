#!/usr/bin/env python3
"""移动止损动作脚本（给已有持仓新增止损市价单，先新增后撤旧）。

给已有持仓加止损单 = 提交一笔 MIT 止损市价单（Market-If-Touched，触发后市价成交）
——这正是长桥 App 里「已有持仓 → 新增止损单 → 设触发价 → 选市价单」对应的接口。

⚠️ 移损顺序：**先下新止损单、再撤旧止损单**（2026-08-01 用户立顺序）——仓位始终有止损保护、
无裸奔空窗（若先撤旧、新还没下，仓位瞬间无保护）。撤旧时排除刚下的新止损（保留新止损为唯一活动止损）。
保留单个活动止损的原因实测确证（2026-08-01）：
长桥对卖单的拦截分两种情况——
- 有持仓时卖超持仓 → Reject「Insufficient holdings」（止损失败）。
- **完全空仓时卖 → 被接受**（NotReported），成交即反向开仓（开空）。
故多个止损单累积、首个触发平仓后账户变空仓时，**其余 MIT 再触发会反向开仓**
（不是止损失败）。「不撤旧、累积兜底」因此不安全——每次移损先下新止损单、再撤旧止损单
（排除新止损），确保任意时刻只有一个活动止损。close_position 平仓后也会
cancel_all_stop_orders 兜底撤所有止损单。

⚠️ 区分两种止损单机制（别混淆）：
- 开仓时的「附加止损单」= attached_params STOP_LOSS，紧绑一笔新主单、主单成交才激活
  （主单未成交时止损不激活，防止主单没成交却触发止损、反向开仓）——只能随新主单提交。
- 已有持仓加的「止损单」= 独立 MIT 止损单（本脚本用的），提交后立即进入监控。

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

from trade_utils import load_config, submit_stop_order, cancel_all_stop_orders, get_open_position


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

    # 量校验：本策略规定止损量严格等于持仓量（超持仓会被券商判失效）
    try:
        _pos = get_open_position(config, symbol)
        if _pos is not None:
            _held = _pos["quantity"]
            if quantity > _held:
                result_base.update({"ok": False, "error": f"止损量 {quantity} 超过持仓量 {_held}，会被券商判失效，拒绝提交"})
                print(json.dumps(result_base, ensure_ascii=False))
                sys.exit(1)
            if quantity < _held:
                result_base["warning"] = f"止损量 {quantity} < 持仓量 {_held}（本策略规定严格相等，触发只平部分）"
        else:
            result_base["warning"] = "未读到持仓，按传入数量继续（券商可能判失效）"
    except Exception as e:
        result_base["quantity_check_warning"] = f"持仓校验失败（继续）: {e}"

    # ① 先下新止损单（仓位持续有止损保护、无裸奔空窗——先新增后撤旧，2026-08-01 用户立顺序）
    try:
        stop_order_id = submit_stop_order(config, symbol, stop_side, quantity, new_stop_price)
    except Exception as e:
        result_base.update({"ok": False, "error": f"新止损单提交失败（旧止损未撤、仍保护仓位）: {e}"})
        print(json.dumps(result_base, ensure_ascii=False))
        sys.exit(1)

    # ② 再撤旧止损单（排除刚下的新止损，保留新止损为唯一活动止损）
    try:
        n_cancelled, cancelled_ids = cancel_all_stop_orders(config, symbol, exclude_order_id=stop_order_id)
        result_base["prior_stops_cancelled"] = n_cancelled
        if n_cancelled > 0:
            result_base["cancelled_order_ids"] = cancelled_ids
    except Exception as e:
        result_base["prior_stops_cancelled_warning"] = f"撤销旧止损单失败（需手动撤，新止损已就位）: {e}"

    result_base.update({
        "ok": True,
        "stop_order_id": stop_order_id,
        "stop_method": "MIT 市价止损单（触发后市价成交）",
        "note": "先下新止损再撤旧（仓位持续保护无空窗），仅保留本笔新止损为唯一活动止损",
    })
    print(json.dumps(result_base, ensure_ascii=False))


if __name__ == "__main__":
    main()
