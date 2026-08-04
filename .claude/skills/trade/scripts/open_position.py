#!/usr/bin/env python3
"""开仓动作脚本（主订单 + 附加止损单）。

REST 一次提交主单（LO 开仓）+ 附加止损单（STOP_LOSS MIT，主单成交才激活），
再回查主单成交状态。附加止损随主单一同提交——开仓失败或主单未成交则撤主单，
附加止损随之自动撤销，不残留裸止损单（取代旧「先挂止损单、再下开仓单」两步）。

订单模型（2026-08-02 用户厘清）：
- 订单本质按类型分（LO 限价 / MO 市价 / MIT 市价触发等）；本脚本开仓下的是 LO。
- 「主订单 / 附加订单」不是订单的固有分类，只是开仓把 LO + MIT 打包一次提交时的相对
  称呼：主单 = 这次的开仓 LO，附加 = 跟随提交的 STOP_LOSS 止损单（主单成交才激活、
  主单撤则附加自动撤）。STOP_LOSS 的方向与触发符号由券商后端按主单方向自动定
  （做多→跌触发卖、做空→涨触发买），无需传 side / trigger_direction。

用法：
  python3 open_position.py <symbol> <direction> <entry_ref> <stop_loss> <target> <quantity>
    symbol      标的代码（如 US.MU / HK.00981）
    direction   long / short
    entry_ref   参考价（初始预期赔率 ≥ 1.2 时的入场价）
    stop_loss   止损价（附加止损触发价）
    target      目标止盈价
    quantity    开仓数量（股数，0=自动算仓位）

输出 JSON：
  成功：{ok, action, order_id, fill_price, method:"limit+attached_stop", lo_price,
         stop:"attached STOP_LOSS MIT @ <stop_loss>", ...}
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
    submit_order_with_stop,
    cancel_order,
    check_order_filled,
    check_price_in_range,
    calc_position_size,
    load_equity,
    parse_mode,
)


def main():
    if len(sys.argv) < 7:
        print(
            "用法: python3 open_position.py <symbol> <direction> <entry_ref> "
            "<stop_loss> <target> <quantity>\n"
            "  symbol    标的代码（US.MU / HK.00981）\n"
            "  direction long / short\n"
            "  entry_ref 参考价\n"
            "  stop_loss 止损价（附加止损触发价）\n"
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
    mode = parse_mode()  # signal（默认，equity 走 equity-log）/ auto（equity 走账户 API）

    if not symbol.startswith("US."):
        # 分而治之（2026-08-01 立）：美股脚本只处理美股；港股走 open_position_hk.py（长桥备选）/
        # open_position_tiger.py（老虎默认）。原港股 lot_size 硬编码 100 的死分支已随此校验移除
        # （港股每手股数因标而异：盈富 500、腾讯 100，由港股脚本从行情接口取真实值，不硬编码）。
        print(json.dumps({"ok": False, "error": f"本脚本只处理美股（US.xxx），港股用 open_position_hk.py / open_position_tiger.py，收到 {symbol}"}))
        sys.exit(1)

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
        direction, current_price, entry_ref, stop_loss, target, symbol
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
        equity, _eq_cur, eq_src = load_equity(mode)
        stop_distance = abs(entry_ref - stop_loss)
        lot_size = 1  # 美股可零股（港股每手股数因标而异，由港股脚本从行情接口取，本脚本只处理美股）
        quantity, max_loss, budget_B = calc_position_size(
            equity, 0.02, 0.10, stop_distance, lot_size
        )
        if quantity <= 0:
            result_base.update({"ok": False, "error": "计算出的仓位为 0（止损距太大或权益不足）"})
            print(json.dumps(result_base, ensure_ascii=False))
            sys.exit(1)
        result_base.update({
            "auto_sized": True,
            "mode": mode,
            "equity": equity,
            "equity_source": eq_src,
            "budget_B": round(budget_B, 2),
            "max_loss": round(max_loss, 2),
        })

    result_base["quantity"] = quantity

    # ④ 主单 LO 价：做多取 ask（主动买，跨价拿量争取即时成交）、做空取 bid（主动卖）
    #    用主动价而非被动价（bid 买/ask 卖）：附加止损要主单成交才激活，成交确定性优先于一个 tick 的价差。
    if direction == "long":
        lo_price = quote["ask"] if quote.get("ask") else current_price
    else:
        lo_price = quote["bid"] if quote.get("bid") else current_price
    # 限价取整到 tick（2026-07-31 修：长桥限价不合 tick 报 602035 Wrong bid size）
    if ".US" in symbol:
        lo_price = round(lo_price, 2)

    side_str = "Buy" if direction == "long" else "Sell"

    # ⑤ REST 一次提交主单（LO）+ 附加止损（STOP_LOSS MIT）
    try:
        order_id = submit_order_with_stop(symbol, side_str, quantity, lo_price, stop_loss)
        result_base["order_id"] = order_id
        result_base["lo_price"] = lo_price
    except Exception as e:
        result_base.update({"ok": False, "error": f"主单 + 附加止损提交失败: {e}"})
        print(json.dumps(result_base, ensure_ascii=False))
        sys.exit(1)

    # ⑥ 回查主单成交状态（禁止用 last 冒充成交价，2026-07-31 MU 事故教训）
    filled, fill_price, status = check_order_filled(config, order_id, timeout=8)
    if not filled:
        # 主单未成交 → 撤主单（附加随之自动撤销），不残留挂单 + 裸止损
        try:
            cancel_order(config, order_id)
            result_base["warning"] = f"主单未成交（status={status}），已撤销主单及附加止损"
        except Exception as ce:
            result_base["warning"] = (
                f"主单未成交（status={status}），撤销主单失败（需手动撤 {order_id}）: {ce}"
            )
        result_base.update({"ok": False, "error": f"主单未成交（status={status}），本次开仓未成立"})
        print(json.dumps(result_base, ensure_ascii=False))
        sys.exit(1)

    # 成交均价缺失时用挂单价兜底（标注来源，便于复盘甄别）
    fill_price_src = "avg_fill_price"
    if fill_price is None:
        fill_price = lo_price
        fill_price_src = "lo_price（成交均价缺失，用挂单价兜底）"

    result_base.update({
        "ok": True,
        "order_id": order_id,
        "fill_price": fill_price,
        "fill_price_source": fill_price_src,
        "method": "limit+attached_stop",
        "stop": f"attached STOP_LOSS MIT @ {stop_loss}",
        "lo_price": lo_price,
        "main_status": status,
    })
    print(json.dumps(result_base, ensure_ascii=False))


if __name__ == "__main__":
    main()
