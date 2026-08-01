#!/usr/bin/env python3
"""开仓动作脚本。

计算价格范围 → 检查当前价是否在范围内 → 先挂止损条件单 → 再下开仓单。
止损单前置：确保持仓一旦建立就有止损保护，不存在裸奔空窗；开仓失败则撤销前置止损单。

用法：
  python3 open_position.py <symbol> <direction> <entry_ref> <stop_loss> <target> <quantity>
    symbol      标的代码（如 US.MU / HK.00981）
    direction   long / short
    entry_ref   参考价（初始预期赔率 ≥ 1.2 时的入场价）
    stop_loss   止损价
    target      目标止盈价
    quantity    开仓数量（股数，0=自动算仓位）

输出 JSON：
  成功：{ok, action, stop_order_id, order_id, fill_price, method, ...}
  失败：{ok:false, error, range_low, range_high, current_price, ...}
"""

import json
import os
import sys

# 脚本目录加入 sys.path（import trade_utils）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from trade_utils import (
    load_config,
    get_quote,
    smart_order,
    submit_stop_order,
    cancel_order,
    check_price_in_range,
    calc_position_size,
    load_equity,
)


def main():
    if len(sys.argv) < 7:
        print(
            "用法: python3 open_position.py <symbol> <direction> <entry_ref> "
            "<stop_loss> <target> <quantity>\n"
            "  symbol    标的代码（US.MU / HK.00981）\n"
            "  direction long / short\n"
            "  entry_ref 参考价\n"
            "  stop_loss 止损价\n"
            "  target    目标止盈价\n"
            "  quantity  开仓数量（股数，0=自动算仓位）",
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

    # ① 加载配置 + 取当前报价
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

    # ② 计算价格范围 + 检查是否在范围内
    in_range, range_low, range_high, odds_at_ref, odds_at_current = check_price_in_range(
        direction, current_price, entry_ref, stop_loss, target
    )

    result_base = {
        "action": "open_position",
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
                f"修正预期赔率需在 0.6~10 之间（当前价超出合理范围）。"
            ),
        })
        print(json.dumps(result_base, ensure_ascii=False))
        sys.exit(1)

    # ③ 自动算仓位（quantity=0 时）
    if quantity == 0:
        equity = load_equity()
        stop_distance = abs(entry_ref - stop_loss)
        lot_size = 1  # 美股可零股；港股需从 snapshot 获取 lot_size
        # TODO: 港股从快照取 lot_size
        if symbol.startswith("HK."):
            lot_size = 100  # 临时默认，实盘应查快照
        quantity, max_loss, budget_B = calc_position_size(
            equity, 0.02, 0.025, stop_distance, lot_size
        )
        if quantity <= 0:
            result_base.update({"ok": False, "error": "计算出的仓位为 0（止损距太大或权益不足）"})
            print(json.dumps(result_base, ensure_ascii=False))
            sys.exit(1)
        result_base.update({
            "auto_sized": True,
            "equity": equity,
            "budget_B": round(budget_B, 2),
            "max_loss": round(max_loss, 2),
        })

    result_base["quantity"] = quantity

    # ④ 第一步：先挂止损条件单（确保持仓一旦建立就有止损保护，不存在裸奔空窗）
    stop_side = "Sell" if direction == "long" else "Buy"
    try:
        stop_order_id = submit_stop_order(config, symbol, stop_side, quantity, stop_loss)
        result_base["stop_order_id"] = stop_order_id
    except Exception as e:
        result_base.update({"ok": False, "error": f"止损单前置失败，不开仓（避免裸奔）: {e}"})
        print(json.dumps(result_base, ensure_ascii=False))
        sys.exit(1)

    # ⑤ 第二步：开仓（止损已在，再下开仓单）
    try:
        order_id, fill_price, method = smart_order(
            config, symbol, "Buy" if direction == "long" else "Sell", quantity, quote
        )
    except Exception as e:
        # 开仓失败 → 撤销已挂的止损单，避免残留
        try:
            cancel_order(config, stop_order_id)
            result_base["warning"] = f"开仓失败，已撤销前置止损单: {e}"
        except Exception:
            result_base["warning"] = f"开仓失败且撤销止损单也失败（需手动撤销止损单 {stop_order_id}）: {e}"
        result_base.update({"ok": False, "error": f"开仓失败: {e}"})
        print(json.dumps(result_base, ensure_ascii=False))
        sys.exit(1)

    result_base.update({
        "ok": True,
        "order_id": order_id,
        "fill_price": fill_price,
        "method": method,
    })
    print(json.dumps(result_base, ensure_ascii=False))


if __name__ == "__main__":
    main()
