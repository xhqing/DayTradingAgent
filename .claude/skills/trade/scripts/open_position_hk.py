#!/usr/bin/env python3
"""港股开仓动作脚本（长桥模拟盘备选账户，与美股 open_position.py 解耦）。

主单 LO（控价、限价取整到港股 tick）+ 附加止损 STOP_LOSS MIT（一次 REST 提交、主单成交才激活）。
仅用于港股（HK.xxx）；美股用 open_position.py。

⚠️ 港股长桥是**备选账户**——港股交易默认走老虎模拟账户，只有用户**特别说明**用长桥时才用本脚本。
港股盘中时段：北京 09:30-12:00 / 13:00-16:00（12:00-13:00 午休不可交易）。

用法：
  python3 open_position_hk.py <symbol> <direction> <entry_ref> <stop_loss> <target> <quantity>
    symbol      港股代码（富途格式 HK.02800）
    direction   long / short
    entry_ref   参考价
    stop_loss   止损价（附加止损触发价）
    target      目标止盈价
    quantity    开仓股数（0=按 lot_size 自动算仓位）

输出 JSON：成功 {ok, order_id, fill_price, method:"limit+attached_stop", lo_price, stop, ...}
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import trade_utils_hk as U


def main():
    if len(sys.argv) < 7:
        print(
            "用法: python3 open_position_hk.py <symbol> <direction> <entry_ref> "
            "<stop_loss> <target> <quantity>\n"
            "  symbol    港股代码（HK.02800）\n"
            "  direction long / short\n"
            "  entry_ref 参考价\n"
            "  stop_loss 止损价\n"
            "  target    目标止盈价\n"
            "  quantity  开仓股数（0=自动算仓位）",
            file=sys.stderr,
        )
        sys.exit(1)

    symbol = sys.argv[1]
    direction = sys.argv[2]
    entry_ref = float(sys.argv[3])
    stop_loss = float(sys.argv[4])
    target = float(sys.argv[5])
    quantity = int(float(sys.argv[6]))

    if not symbol.startswith("HK."):
        print(json.dumps({"ok": False, "error": f"本脚本只处理港股（HK.xxx），收到 {symbol}"}))
        sys.exit(1)
    if direction not in ("long", "short"):
        print(json.dumps({"ok": False, "error": f"direction 必须是 long/short，收到 '{direction}'"}))
        sys.exit(1)

    try:
        config = U.load_config()
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"配置加载失败: {e}"}))
        sys.exit(1)

    quote = U.get_quote_hk(config, symbol)
    if quote is None:
        print(json.dumps({"ok": False, "error": f"港股报价为空: {symbol}（检查代码/盘后）"}))
        sys.exit(1)
    current_price = quote["last"]

    in_range, range_low, range_high, odds_at_ref, odds_at_current = U.check_price_in_range(
        direction, current_price, entry_ref, stop_loss, target
    )
    result_base = {
        "action": "open_position_hk", "market": "HK", "symbol": symbol, "direction": direction,
        "entry_ref": entry_ref, "stop_loss": stop_loss, "target": target,
        "current_price": current_price, "range_low": round(range_low, 4), "range_high": round(range_high, 4),
        "odds_at_ref": round(odds_at_ref, 2), "odds_at_current": round(odds_at_current, 2),
    }
    if not in_range:
        result_base.update({"ok": False, "error": (
            f"当前价 {current_price} 不在范围 [{range_low:.4f}, {range_high:.4f}] 内。"
            f"参考价赔率 {odds_at_ref:.2f}，当前价赔率 {odds_at_current:.2f}。")})
        print(json.dumps(result_base, ensure_ascii=False))
        sys.exit(1)

    # 自动算仓位（quantity=0）：港股 lot_size 从 static_info 取真实每手股数
    if quantity == 0:
        equity = U.load_equity_hk(config)
        lot_size = U.get_lot_size_hk(config, symbol)
        if not lot_size:
            result_base.update({"ok": False, "error": f"查不到 {symbol} 的 lot_size，无法自动算仓位"})
            print(json.dumps(result_base, ensure_ascii=False))
            sys.exit(1)
        stop_distance = abs(entry_ref - stop_loss)
        quantity, max_loss, budget_B = U.calc_position_size(equity, 0.02, 0.10, stop_distance, lot_size)
        if quantity <= 0:
            result_base.update({"ok": False, "error": "仓位为 0（止损距太大或权益不足）"})
            print(json.dumps(result_base, ensure_ascii=False))
            sys.exit(1)
        result_base.update({"auto_sized": True, "equity": equity, "lot_size": lot_size,
                            "budget_B": round(budget_B, 2), "max_loss": round(max_loss, 2)})
    result_base["quantity"] = quantity

    # 主单 LO 价：做多取 ask（主动买）/做空取 bid（主动卖），取整到港股 tick
    if direction == "long":
        lo_price = quote["ask"] if quote.get("ask") else current_price
    else:
        lo_price = quote["bid"] if quote.get("bid") else current_price
    lo_price = U.round_to_tick_hk(lo_price)
    side_str = "Buy" if direction == "long" else "Sell"

    # REST 一次提交主单(LO) + 附加止损(STOP_LOSS MIT)
    try:
        order_id = U.submit_order_with_stop_hk(symbol, side_str, quantity, lo_price, stop_loss)
    except Exception as e:
        result_base.update({"ok": False, "error": f"主单+附加止损提交失败: {e}", "lo_price": lo_price})
        print(json.dumps(result_base, ensure_ascii=False))
        sys.exit(1)

    filled, fill_price, status = U.check_order_filled_hk(config, order_id, timeout=8)
    if not filled:
        try:
            U.cancel_order_hk(config, order_id)
            result_base["warning"] = f"主单未成交（{status}），已撤主单及附加止损"
        except Exception as ce:
            result_base["warning"] = f"主单未成交（{status}），撤单失败需手动撤 {order_id}: {ce}"
        result_base.update({"ok": False, "error": f"主单未成交（{status}），本次开仓未成立", "order_id": order_id})
        print(json.dumps(result_base, ensure_ascii=False))
        sys.exit(1)

    fill_src = "avg_fill_price"
    if fill_price is None:
        fill_price = lo_price
        fill_src = "lo_price（成交均价缺失兜底）"
    result_base.update({"ok": True, "order_id": order_id, "fill_price": fill_price,
                        "fill_price_source": fill_src, "method": "limit+attached_stop",
                        "stop": f"attached STOP_LOSS MIT @ {stop_loss}", "lo_price": lo_price, "main_status": status})
    print(json.dumps(result_base, ensure_ascii=False))


if __name__ == "__main__":
    main()
