#!/usr/bin/env python3
"""减仓动作脚本（模拟盘模式）。

部分平仓。带重试机制，尽可能快速以优价成交。
减仓后剩余仓位仍按原止损管理（不改止损单）。

用法：
  python3 reduce_position.py <symbol> <direction> <quantity> [max_retries]
    symbol       标的代码
    direction    long / short（持仓方向：long=持多，减仓卖；short=持空，减仓买回）
    quantity     减仓数量
    max_retries  最大重试次数（默认 3）
"""

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from trade_utils import (
    load_config,
    get_quote,
    submit_market_order,
    submit_limit_order,
)


def main():
    if len(sys.argv) < 4:
        print(
            "用法: python3 reduce_position.py <symbol> <direction> <quantity> [max_retries]\n"
            "  symbol      标的代码\n"
            "  direction   long / short（当前持仓方向）\n"
            "  quantity    减仓数量\n"
            "  max_retries 最大重试次数（默认 3）",
            file=sys.stderr,
        )
        sys.exit(1)

    symbol = sys.argv[1]
    direction = sys.argv[2]
    quantity = int(float(sys.argv[3]))
    max_retries = int(sys.argv[4]) if len(sys.argv) > 4 else 3

    if direction not in ("long", "short"):
        print(json.dumps({"ok": False, "error": f"direction 必须是 long/short，收到 '{direction}'"}))
        sys.exit(1)

    try:
        config = load_config()
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"长桥配置加载失败: {e}"}))
        sys.exit(1)

    # 减仓方向：持多→卖出，持空→买回
    close_side = "Sell" if direction == "long" else "Buy"

    result_base = {
        "action": "reduce_position",
        "symbol": symbol,
        "direction": direction,
        "quantity": quantity,
        "close_side": close_side,
    }

    # 策略：先尝试限价单（比当前价略优），快速成交
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

    # 重试机制：限价单→市价单递进
    for attempt in range(max_retries):
        try:
            # 第一轮用略优的限价（买高 0.01 / 卖低 0.01），后续用市价
            if attempt == 0:
                if close_side == "Sell":
                    limit_price = round(current_price * 0.999, 2)  # 略低于现价快速卖出
                else:
                    limit_price = round(current_price * 1.001, 2)  # 略高于现价快速买回
                order_id, fill_price = submit_limit_order(
                    config, symbol, close_side, quantity, limit_price, retries=1
                )
                result_base.update({
                    "ok": True,
                    "order_id": order_id,
                    "fill_price": fill_price,
                    "method": "limit",
                    "attempt": attempt + 1,
                })
                print(json.dumps(result_base, ensure_ascii=False))
                return
            else:
                # 后续尝试用市价单
                order_id = submit_market_order(config, symbol, close_side, quantity, retries=1)
                result_base.update({
                    "ok": True,
                    "order_id": order_id,
                    "fill_price": "market",
                    "method": "market",
                    "attempt": attempt + 1,
                })
                print(json.dumps(result_base, ensure_ascii=False))
                return
        except Exception as e:
            if attempt < max_retries - 1:
                time.sleep(0.5 * (attempt + 1))
                continue
            result_base.update({"ok": False, "error": f"减仓失败（{max_retries} 次重试后）: {e}"})
            print(json.dumps(result_base, ensure_ascii=False))
            sys.exit(1)


if __name__ == "__main__":
    main()
