#!/usr/bin/env python3
"""港股开仓动作脚本（老虎证券模拟账户，港股默认账户）。

主单（默认 MKT 市价单，可 --order-type lmt 切回限价）+ 附加止损腿 OrderLeg('LOSS')
（一次提交、主单成交才激活），再回查主单成交状态。附加止损随主单一同提交——
开仓失败或主单未成交则撤主单，附加腿随之自动撤销，不残留裸止损单。

✅ 实测状态（2026-08-03）：下单链路已 paper 开盘实测通过——LMT 主单 FILLED @486.2 +
附加止损腿 OrderLeg('LOSS') 一次提交成功、附加腿激活为独立 STP 单进入 HELD 监控（腾讯 100 股）。
⚠️ 2026-08-07 改用市价单默认（用户立）：高波动标的（MINIMAX 当日 5 次 LMT 开仓全 Invalid——
限价单 + 8 秒超时撤单与快速跳动盘口不匹配）改用 MKT 主单 + 附加止损腿一次提交；
MKT+腿的兼容性需 paper 开盘实测确认。
实测发现并修复 2 个 bug（详见 trade_utils_tiger.py 与 CHANGELOG 2026-08-03）：① create_order 的
order_type 传枚举对象序列化失败（须传字符串 'LMT'）；② 成交回查 status 枚举须取 .value。

用法：
  python3 open_position_tiger.py <symbol> <direction> <entry_ref> <stop_loss> <target> <quantity> [--order-type lmt|mkt]
    symbol      港股代码（富途格式 HK.02800，内部转老虎 02800）
    direction   long / short
    entry_ref   参考价
    stop_loss   止损价（附加止损腿触发价）
    target      目标止盈价
    quantity    开仓数量（股数，0=自动算仓位：lot_size 从 get_contract 取真实每手）
    --order-type 主单类型：mkt（默认，市价单）/ lmt（限价单，限价=盘口 ask/bid 取整到 tick）

输出 JSON：
  成功：{ok, action, order_id, fill_price, method:"market+attached_stop"|"limit+attached_stop", ...}
  失败：{ok:false, error, range_low, range_high, current_price, ...}
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import trade_utils_tiger as U


def main():
    args = sys.argv[1:]
    order_type = "mkt"
    if "--order-type" in args:
        idx = args.index("--order-type")
        if idx + 1 >= len(args):
            print("用法错误：--order-type 需要参数 lmt 或 mkt", file=sys.stderr)
            sys.exit(1)
        order_type = args[idx + 1].lower()
        del args[idx:idx + 2]
    if order_type not in ("lmt", "mkt"):
        print(json.dumps({"ok": False, "error": f"--order-type 必须是 lmt/mkt，收到 '{order_type}'"}))
        sys.exit(1)

    if len(args) < 6:
        print(
            "用法: python3 open_position_tiger.py <symbol> <direction> <entry_ref> "
            "<stop_loss> <target> <quantity> [--order-type lmt|mkt]\n"
            "  symbol    港股代码（HK.02800）\n"
            "  direction long / short\n"
            "  entry_ref 参考价\n"
            "  stop_loss 止损价（附加止损触发价）\n"
            "  target    目标止盈价\n"
            "  quantity  开仓股数（0=自动算仓位）\n"
            "  --order-type 主单类型：mkt（默认，市价单）/ lmt（限价单）",
            file=sys.stderr,
        )
        sys.exit(1)

    symbol = args[0]
    direction = args[1]
    entry_ref = float(args[2])
    stop_loss = float(args[3])
    target = float(args[4])
    quantity = int(float(args[5]))

    if not symbol.startswith("HK."):
        print(json.dumps({"ok": False, "error": f"本脚本只处理港股（HK.xxx），收到 {symbol}"}))
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
        quote = U.get_quote_tiger(config, symbol)
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"获取行情失败: {e}"}))
        sys.exit(1)

    if quote is None:
        print(json.dumps({"ok": False, "error": f"港股报价为空: {symbol}（检查代码/盘后）"}))
        sys.exit(1)
    current_price = quote["last"]

    in_range, range_low, range_high, odds_at_ref, odds_at_current = U.check_price_in_range(
        direction, current_price, entry_ref, stop_loss, target, symbol
    )
    result_base = {
        "action": "open_position_tiger", "market": "HK", "symbol": symbol, "direction": direction,
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

    # 自动算仓位（quantity=0）：equity 取老虎账户净值（港股口径 HKD，与标的止损距同币种——
    # 2026-08-05 修：原取 USD 净值直接当 HKD 用，B 被低估 ~7.8 倍；现 get_prime_assets
    # base_currency='HKD' 直接取 HKD 净值）、lot_size 从 get_contract 取真实每手股数
    if quantity == 0:
        tc = U.new_trade_client(config)
        equity, currency = U.load_equity_tiger(config, base_currency='HKD')
        if equity is None:
            result_base.update({"ok": False, "error": "老虎账户净值取不到（未开通交易/资产权限？），无法自动算仓位"})
            print(json.dumps(result_base, ensure_ascii=False))
            sys.exit(1)
        lot_size = U.get_lot_size_tiger(tc, symbol)
        if not lot_size:
            result_base.update({"ok": False, "error": f"查不到 {symbol} 的 lot_size，无法自动算仓位"})
            print(json.dumps(result_base, ensure_ascii=False))
            sys.exit(1)
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

    # 主单提交参数：mkt 市价单（不限价，高波动标的不被限价+超时甩开）/ lmt 限价单
    # （做多取 ask 主动买、做空取 bid 主动卖，取整到港股 tick）
    side_str = "Buy" if direction == "long" else "Sell"
    if order_type == "lmt":
        if direction == "long":
            lo_price = quote["ask"] if quote.get("ask") else current_price
        else:
            lo_price = quote["bid"] if quote.get("bid") else current_price
        tick_sizes = U.get_tick_sizes_tiger(U.new_trade_client(config), symbol)
        lo_price = U.round_to_tick_tiger(lo_price, tick_sizes)
        result_base["lo_price"] = lo_price
    else:
        lo_price = None

    # 主单（LMT/MKT）+ 附加止损腿 OrderLeg('LOSS')（一次提交；券商语义 2026-08-03 实测：
    # 附加腿落成独立 STP 单、主单成交后进入 HELD 监控，可独立撤销）
    try:
        order_id = U.submit_order_with_stop_tiger(
            config, symbol, side_str, quantity, lo_price, stop_loss,
            order_type=order_type.upper())
    except Exception as e:
        result_base.update({"ok": False, "error": f"主单+附加止损提交失败: {e}"})
        print(json.dumps(result_base, ensure_ascii=False))
        sys.exit(1)
    result_base["order_id"] = order_id

    filled, fill_price, status = U.check_order_filled_tiger(config, order_id, timeout=8)
    if not filled:
        try:
            U.cancel_order_tiger(config, order_id)
            result_base["warning"] = f"主单未成交（{status}），已撤主单及附加止损"
        except Exception as ce:
            result_base["warning"] = f"主单未成交（{status}），撤单失败需手动撤 {order_id}: {ce}"
        result_base.update({"ok": False, "error": f"主单未成交（{status}），本次开仓未成立"})
        print(json.dumps(result_base, ensure_ascii=False))
        sys.exit(1)

    fill_src = "avg_fill_price"
    if fill_price is None:
        fill_price = lo_price if lo_price else current_price
        fill_src = "lo_price（成交均价缺失兜底）" if lo_price else "current_price（成交均价缺失兜底）"
    result_base.update({"ok": True, "fill_price": fill_price, "fill_price_source": fill_src,
                        "method": f"{order_type}+attached_stop",
                        "stop": f"attached LOSS @ {stop_loss}", "main_status": status})
    print(json.dumps(result_base, ensure_ascii=False))


if __name__ == "__main__":
    main()
