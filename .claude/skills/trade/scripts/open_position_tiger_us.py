#!/usr/bin/env python3
"""美股开仓动作脚本（老虎证券模拟账户，美股默认账户）。

主单 LMT（控价、限价取整到美股 tick 0.01）+ 附加止损腿 OrderLeg('LOSS')（一次提交、主单成交才
激活），再回查主单成交状态。开仓失败或主单未成交则撤主单、附加腿随之自动撤销，不残留裸止损。

✅ 实测状态（2026-08-05 美股盘中）：下单链路已 paper 端到端实测通过（SPY 2 股：LMT 主单
Filled @773.68 + 附加止损腿 OrderLeg('LOSS') 激活为独立 STP 监控）。行情走富途 OpenD 单源
（老虎美股无行情权限、get_stock_briefs 报 4000 permission denied）。

用法：
  python3 open_position_tiger_us.py <symbol> <direction> <entry_ref> <stop_loss> <target> <quantity>
    symbol      美股代码（富途格式 US.MU，内部转老虎裸代码 MU）
    direction   long / short
    entry_ref   参考价
    stop_loss   止损价（附加止损腿触发价）
    target      目标止盈价
    quantity    开仓股数（0=自动算仓位：lot_size 从 get_contract 取、美股默认 1）
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import trade_utils_tiger_us as U


def main():
    if len(sys.argv) < 7:
        print(
            "用法: python3 open_position_tiger_us.py <symbol> <direction> <entry_ref> "
            "<stop_loss> <target> <quantity>\n"
            "  symbol    美股代码（US.MU）\n"
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

    if not symbol.startswith("US."):
        print(json.dumps({"ok": False, "error": f"本脚本只处理美股（US.xxx），收到 {symbol}"}))
        sys.exit(1)
    if direction not in ("long", "short"):
        print(json.dumps({"ok": False, "error": f"direction 必须是 long/short，收到 '{direction}'"}))
        sys.exit(1)

    try:
        config = U.load_config()
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"老虎配置加载失败: {e}"}))
        sys.exit(1)

    try:
        quote = U.get_quote_us(config, symbol)
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"获取美股行情失败: {e}"}))
        sys.exit(1)

    if quote is None:
        print(json.dumps({"ok": False, "error": f"美股报价为空: {symbol}（检查代码/盘外）"}))
        sys.exit(1)
    current_price = quote["last"]

    in_range, range_low, range_high, odds_at_ref, odds_at_current = U.check_price_in_range(
        direction, current_price, entry_ref, stop_loss, target, symbol
    )
    result_base = {
        "action": "open_position_tiger_us", "market": "US", "symbol": symbol, "direction": direction,
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

    # 自动算仓位（quantity=0）：equity 取老虎账户 USD 净值、lot_size 从 get_contract 取（美股默认 1）
    if quantity == 0:
        tc = U.new_trade_client(config)
        equity, currency = U.load_equity_us(config)
        if equity is None:
            result_base.update({"ok": False, "error": "老虎账户净值取不到（未开通交易/资产权限？），无法自动算仓位"})
            print(json.dumps(result_base, ensure_ascii=False))
            sys.exit(1)
        lot_size = U.get_lot_size_us(tc, symbol)
        if not lot_size:
            lot_size = 1  # 美股默认 1 股/手
        stop_distance = abs(entry_ref - stop_loss)
        quantity, max_loss, budget_B = U.calc_position_size(
            equity, 0.02, 0.10, stop_distance, lot_size, entry_price=entry_ref)
        if quantity <= 0:
            result_base.update({"ok": False, "error": "仓位为 0（止损距太大或权益不足）"})
            print(json.dumps(result_base, ensure_ascii=False))
            sys.exit(1)
        result_base.update({"auto_sized": True, "equity": equity, "equity_currency": currency,
                            "lot_size": lot_size, "budget_B": round(budget_B, 2),
                            "max_loss": round(max_loss, 2)})
    result_base["quantity"] = quantity

    # 参考价（主单已改 MKT 市价单，此价仅作输出参考）：做多取 ask / 做空取 bid，取整到美股 tick 0.01
    if direction == "long":
        lo_price = quote["ask"] if quote.get("ask") else current_price
    else:
        lo_price = quote["bid"] if quote.get("bid") else current_price
    lo_price = U.round_to_tick_us(lo_price)
    side_str = "Buy" if direction == "long" else "Sell"

    # 主单 LMT + 附加止损腿 OrderLeg('LOSS')（一次提交）
    try:
        order_id = U.submit_order_with_stop_us(config, symbol, side_str, quantity, lo_price, stop_loss)
    except Exception as e:
        result_base.update({"ok": False, "error": f"主单+附加止损提交失败: {e}", "lo_price": lo_price})
        print(json.dumps(result_base, ensure_ascii=False))
        sys.exit(1)
    result_base["lo_price"] = lo_price
    result_base["order_id"] = order_id

    filled, fill_price, status = U.check_order_filled_us(config, order_id, timeout=8)
    if not filled:
        try:
            U.cancel_order_us(config, order_id)
            result_base["warning"] = f"主单未成交（{status}），已撤主单及附加止损"
        except Exception as ce:
            result_base["warning"] = f"主单未成交（{status}），撤单失败需手动撤 {order_id}: {ce}"
        result_base.update({"ok": False, "error": f"主单未成交（{status}），本次开仓未成立"})
        print(json.dumps(result_base, ensure_ascii=False))
        sys.exit(1)

    fill_src = "avg_fill_price"
    if fill_price is None:
        fill_price = lo_price
        fill_src = "lo_price（成交均价缺失兜底）"
    result_base.update({"ok": True, "fill_price": fill_price, "fill_price_source": fill_src,
                        "method": "market+attached_stop",
                        "stop": f"attached LOSS @ {stop_loss}", "main_status": status})
    print(json.dumps(result_base, ensure_ascii=False))


if __name__ == "__main__":
    main()
