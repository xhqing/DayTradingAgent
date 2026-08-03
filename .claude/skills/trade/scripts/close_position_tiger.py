#!/usr/bin/env python3
"""港股平仓动作脚本（老虎证券模拟账户，港股默认账户）。

一键平仓：读**港股**持仓自动算方向+量（不会碰美股持仓），**先撤该标的全部未触发止损单、
再下市价单 MKT 立即成交**——平仓是「方向不看好、要立即成交」，MKT 保证成交确定性、允许
价格往不利方向偏移。撤止损必须在平仓之前（2026-08-03 午盘实测：老虎与长桥不同——挂着的
止损单（如开仓附加腿落成的 STP）会占用平仓单的持仓校验额度，Buy 平空单被拒
「The order quantity you entered exceeds your current holdings」；先撤止损再平仓立即成交）。
平仓后不再需要撤止损（已提前撤）；撤止损到平仓之间有几秒裸奔窗口，MKT 立即成交、窗口极小。

✅ 实测状态（2026-08-03）：下单链路已 paper 开盘实测通过——MKT 平仓单 Filled @486.0
（avg_fill_price 真实成交价，非 last 兜底）、平仓后撤全部未触发止损单成功（含移损新增 STP 与
开仓附加腿落成的 STP 单，腾讯 100 股）。**午后修正**：平仓顺序改为「先撤止损再平仓」（实测
07709 空单：挂 STP 时 Buy 38,500/10,000/100 股全部 EXPIRED「exceeds holdings」，撤 3 笔止损单后
Buy MKT 立即 Filled @36.20）。

用法：
  python3 close_position_tiger.py [symbol] [direction] [quantity]
    不给参数 = 一键平账户唯一港股持仓
    HK.02800 / HK.02800 long 500（显式）
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import trade_utils_tiger as U


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
        print(json.dumps({"ok": False, "error": f"老虎配置加载失败: {e}"}))
        sys.exit(1)

    # 读持仓补全 symbol / direction / quantity（一键平仓核心）
    if symbol is None or direction is None or quantity is None:
        pos = U.get_open_position_tiger(config, symbol)
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

    result_base = {"action": "close_position_tiger", "market": "HK", "symbol": symbol,
                   "direction": direction, "quantity": quantity, "close_side": close_side}

    quote = U.get_quote_tiger(config, symbol)
    if quote is None:
        result_base.update({"ok": False, "error": f"港股报价为空: {symbol}"})
        print(json.dumps(result_base, ensure_ascii=False))
        sys.exit(1)

    # 第一步：先撤该港股标的全部未触发止损单（2026-08-03 实测：老虎平仓前必须撤止损，
    # 挂着的 STP 占用平仓单校验额度，Buy 平空被拒「exceeds holdings」；撤后平仓立即成交）
    try:
        n, ids = U.cancel_all_stop_orders_tiger(config, symbol)
        result_base["stop_orders_cancelled"] = n
        if n > 0:
            result_base["cancelled_order_ids"] = ids
    except Exception as e:
        result_base["stop_orders_cancelled_warning"] = f"撤止损单失败（需手动）: {e}"

    # 第二步：MKT 市价平仓（立即成交；方向不看好时要确定性而非控价）
    try:
        order_id = U.submit_market_order_tiger(config, symbol, close_side, quantity)
    except Exception as e:
        result_base.update({"ok": False, "error": f"平仓 MKT 提交失败: {e}"})
        print(json.dumps(result_base, ensure_ascii=False))
        sys.exit(1)

    # 回查成交（MKT 通常立即成交，但禁止用 last 冒充成交价——MU 事故教训）
    filled, fill_price, status = U.check_order_filled_tiger(config, order_id, timeout=8)
    if not filled:
        result_base.update({"ok": False, "error": f"平仓 MKT 未成交（{status}）", "order_id": order_id})
        print(json.dumps(result_base, ensure_ascii=False))
        sys.exit(1)

    fill_src = "avg_fill_price"
    if fill_price is None:
        fill_price = quote["last"]
        fill_src = "last（MKT 成交均价缺失兜底）"
    result_base.update({"ok": True, "order_id": order_id, "fill_price": fill_price,
                        "fill_price_source": fill_src, "method": "market", "main_status": status})

    print(json.dumps(result_base, ensure_ascii=False))


if __name__ == "__main__":
    main()
