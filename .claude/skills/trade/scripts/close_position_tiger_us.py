#!/usr/bin/env python3
"""美股平仓动作脚本（老虎证券模拟账户，美股默认账户）。

**主路径：modify 持仓止损单触发价=现价，让止损单自己触发 Sell/Buy MO 平仓**（2026-08-05 用户
方案，消除「撤止损 + MO」race condition，同港股 close_position_tiger 改造）。无活动止损单 →
直接 MO（无冲突）；modify 失败/未触发 → fallback「撤止损 + MO」。

_is_stop_order 含 TRAIL（跟踪止损）——吸收 2026-08-05 中芯残留事故教训（cancel 只撤 STP/LOSS
漏 TRAIL、致 salable=0 平仓被拒）。

✅ 实测状态（2026-08-05 美股盘中）：下单链路已 paper 端到端实测通过（SPY 2 股平多：modify
触发价 771.71→773.42、止损单触发 Sell MO Filled @773.44、持仓归零、无残留止损单；过程指标
mfe_R/mae_R 正常输出）。行情走富途 OpenD 单源（老虎美股无行情权限）。

用法：
  python3 close_position_tiger_us.py [symbol] [direction] [quantity]
    不给参数 = 一键平账户唯一美股持仓
    US.MU / US.MU long 40（显式）
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import trade_utils_tiger_us as U


def _is_stop_order(o):
    """止损单判定（含 STP/STOP/TRAIL/LOSS 附加腿）。"""
    otype = getattr(o, "order_type", None)
    otype_val = otype.value if hasattr(otype, "value") else str(otype)
    upper = str(otype_val).upper()
    legs = getattr(o, "order_legs", None) or []
    return ("STP" in upper or "STOP" in upper or "TRAIL" in upper
            or any(str(getattr(leg, "leg_type", "")).upper() == "LOSS" for leg in legs))


def _status_str(o):
    status = getattr(o, "status", None)
    return status.value if hasattr(status, "value") else str(status)


_TERMINAL = ("Filled", "Cancelled", "Inactive", "Invalid", "Expired")


def _parse_args(argv):
    """解析位置参数并过滤 --mode（2026-08-05 立）：`--mode auto` 这类误传会把 `auto` 当
    quantity 报 ValueError 耽误平仓（2026-08-03 MU 空单教训同款）。平仓脚本不连账户 equity、
    --mode 无实际用途，直接忽略（含 `--mode` 后跟的值、`--mode=xxx` 两种写法）。"""
    args = []
    skip_next = False
    for a in argv:
        if skip_next:
            skip_next = False
            continue
        if a == "--mode":
            skip_next = True
            continue
        if a.startswith("--mode="):
            continue
        args.append(a)
    return args


def main():
    argv = _parse_args(sys.argv[1:])
    symbol = argv[0] if len(argv) > 0 else None
    direction = argv[1] if len(argv) > 1 else None
    quantity = int(float(argv[2])) if len(argv) > 2 else None

    if symbol and not symbol.startswith("US."):
        print(json.dumps({"ok": False, "error": f"本脚本只处理美股（US.xxx），收到 {symbol}"}))
        sys.exit(1)

    try:
        config = U.load_config()
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"老虎配置加载失败: {e}"}))
        sys.exit(1)

    # 读持仓补全 symbol / direction / quantity（一键平仓核心，只看美股持仓）
    if symbol is None or direction is None or quantity is None:
        pos = U.get_open_position_us(config, symbol)
        if pos is None:
            hint = "账户无美股持仓" if symbol is None else f"未找到美股 {symbol} 持仓"
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

    result_base = {"action": "close_position_tiger_us", "market": "US", "symbol": symbol,
                   "direction": direction, "quantity": quantity, "close_side": close_side}

    quote = U.get_quote_us(config, symbol)
    if quote is None:
        result_base.update({"ok": False, "error": f"美股报价为空: {symbol}"})
        print(json.dumps(result_base, ensure_ascii=False))
        sys.exit(1)
    current_price = quote["last"]

    tc = U.new_trade_client(config)
    target_sym = U.to_tiger_symbol_us(symbol)

    # 查活动止损单（该标的、止损类型含 TRAIL、非终结状态）
    stp = None
    for o in (tc.get_orders() or []):
        sym = str(getattr(getattr(o, "contract", None), "symbol", ""))
        if sym != target_sym:
            continue
        if not _is_stop_order(o):
            continue
        if _status_str(o) in _TERMINAL:
            continue
        stp = o
        break

    # 平仓过程指标素材（2026-08-05 立）：开仓价 = 持仓 cost_price、止损距 = |开仓价 − 活动
    # 止损触发价|（M = 仓位 × 止损距、毛值，与复盘口径一致）。平仓成交后由
    # _attach_process_metrics 原生补记 mfe_R / mae_R（复盘过程指标直接读、不必回拉历史 K）。
    entry_price = None
    stop_dist = None
    try:
        pos = U.get_open_position_us(config, symbol)
        if pos and pos.get("cost_price"):
            entry_price = float(pos["cost_price"])
            if stp is not None:
                aux = float(getattr(stp, "aux_price", 0) or 0)
                if aux > 0:
                    stop_dist = abs(entry_price - aux)
    except Exception:
        pass

    # ===== 主路径：有活动止损单 → modify 触发价 = 现价，让它触发 MO 平仓（无撤单 race）=====
    if stp is not None:
        stp_id = getattr(stp, "id", None)
        old_aux = float(getattr(stp, "aux_price", 0) or 0)
        trig = U.round_to_tick_us(current_price)
        result_base.update({"path": "modify_stop_trigger", "old_trigger": old_aux,
                            "new_trigger": trig, "current_price": current_price})
        try:
            tc.modify_order(stp, aux_price=trig)
        except Exception as e:
            result_base["modify_failed_fallback"] = f"modify 触发价抛异常 {e}，回退「撤止损 + MO」"
            _fallback_cancel_and_mo(config, symbol, close_side, quantity, result_base,
                                    current_price, direction, entry_price, stop_dist)
            sys.exit(0)

        filled, fill_price, status = U.check_order_filled_us(config, stp_id, timeout=12)
        if filled:
            fill_src = "avg_fill_price"
            if fill_price is None:
                fill_price = current_price
                fill_src = "current_price（MO 成交均价缺失兜底）"
            result_base.update({"ok": True, "order_id": stp_id, "fill_price": fill_price,
                                "fill_price_source": fill_src,
                                "method": "modify_stop_trigger（止损单触发 MO 平仓、无撤单 race）",
                                "main_status": status})
            _attach_process_metrics(result_base, config, symbol, direction, entry_price, stop_dist)
            print(json.dumps(result_base, ensure_ascii=False))
            sys.exit(0)
        else:
            result_base["modify_not_triggered_fallback"] = (
                f"modify 触发价 {trig} 后止损单未触发（{status}），回退撤止损 + MO")
            _fallback_cancel_and_mo(config, symbol, close_side, quantity, result_base,
                                    current_price, direction, entry_price, stop_dist)
            sys.exit(0)

    # ===== 无活动止损单 → 直接 MO 平仓（无止损单冲突、无 race）=====
    result_base["path"] = "no_active_stop → direct MO"
    try:
        order_id = U.submit_market_order_us(config, symbol, close_side, quantity)
    except Exception as e:
        result_base.update({"ok": False, "error": f"平仓 MKT 提交失败: {e}"})
        print(json.dumps(result_base, ensure_ascii=False))
        sys.exit(1)

    filled, fill_price, status = U.check_order_filled_us(config, order_id, timeout=8)
    if not filled:
        result_base.update({"ok": False, "error": f"平仓 MKT 未成交（{status}）", "order_id": order_id})
        print(json.dumps(result_base, ensure_ascii=False))
        sys.exit(1)
    fill_src = "avg_fill_price"
    if fill_price is None:
        fill_price = current_price
        fill_src = "current_price（MO 成交均价缺失兜底）"
    result_base.update({"ok": True, "order_id": order_id, "fill_price": fill_price,
                        "fill_price_source": fill_src, "method": "market（无活动止损单）",
                        "main_status": status})
    _attach_process_metrics(result_base, config, symbol, direction, entry_price, stop_dist)
    print(json.dumps(result_base, ensure_ascii=False))


def _attach_process_metrics(result_base, config, symbol, direction, entry_price, stop_dist):
    """平仓成交后原生补记过程指标 mfe_R / mae_R（2026-08-05 立，review-and-evaluation.md 数据约束
    方案 b 落地）：从盯盘 log 取该标的当日采样极值近似持仓期间 high/low（日内策略当天开当天平，
    当日 log 近似持仓期间；log 由 monitor_segment 按市场交易日 + 模式命名）。无 log / 缺
    entry / stop_dist 则不加字段（复盘按缺失处理、跳过过程指标）。

    口径与 review.py 一致：MFE_R = 有利方向最大幅度 ÷ 止损距（正）、MAE_R = −不利方向最大幅度
    ÷ 止损距（负，越接近 0 防守越好）；做多 fav = high − entry、做空 fav = entry − low。
    """
    if not entry_price or not stop_dist or stop_dist <= 0:
        return
    try:
        extremes = U.calc_position_extremes_us(symbol, mode=U.parse_mode())
    except Exception:
        return
    if not extremes:
        return
    raw_high, raw_low = extremes
    if direction == "long":
        fav, adv = raw_high - entry_price, entry_price - raw_low
    else:
        fav, adv = entry_price - raw_low, raw_high - entry_price
    result_base.update({
        "entry_price": round(entry_price, 4),
        "raw_high": raw_high, "raw_low": raw_low,
        "mfe_R": round(max(fav, 0.0) / stop_dist, 3),
        "mae_R": round(-max(adv, 0.0) / stop_dist, 3),
        "process_metric_note": "持仓期间极值 = 当日盯盘 log 采样近似（日内当天开当天平）",
    })


def _fallback_cancel_and_mo(config, symbol, close_side, quantity, result_base, current_price,
                            direction=None, entry_price=None, stop_dist=None):
    """fallback：撤止损单 + MO 平仓（modify 路径失败时兜底）。"""
    try:
        n, ids = U.cancel_all_stop_orders_us(config, symbol)
        result_base["stop_orders_cancelled"] = n
        if n > 0:
            result_base["cancelled_order_ids"] = ids
    except Exception as e:
        result_base["stop_orders_cancelled_warning"] = f"撤止损单失败（需手动）: {e}"
    try:
        order_id = U.submit_market_order_us(config, symbol, close_side, quantity)
    except Exception as e:
        result_base.update({"ok": False, "error": f"fallback MO 提交失败: {e}"})
        print(json.dumps(result_base, ensure_ascii=False))
        sys.exit(1)
    filled, fill_price, status = U.check_order_filled_us(config, order_id, timeout=8)
    if not filled:
        result_base.update({"ok": False, "error": f"fallback MO 未成交（{status}）", "order_id": order_id})
        print(json.dumps(result_base, ensure_ascii=False))
        sys.exit(1)
    fill_src = "avg_fill_price"
    if fill_price is None:
        fill_price = current_price
        fill_src = "current_price（MO 成交均价缺失兜底）"
    result_base.update({"ok": True, "order_id": order_id, "fill_price": fill_price,
                        "fill_price_source": fill_src, "method": "fallback：cancel_stop + MO",
                        "main_status": status})
    _attach_process_metrics(result_base, config, symbol, direction, entry_price, stop_dist)
    print(json.dumps(result_base, ensure_ascii=False))


if __name__ == "__main__":
    main()
