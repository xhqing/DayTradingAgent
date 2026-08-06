#!/usr/bin/env python3
"""美股移动止损动作脚本（老虎证券模拟账户，美股默认账户）。

**优先 modify 现有活动 STP 单的 aux_price**（单步、无撤单 race，同港股 move_stop_tiger 改造）。
分支：1 个活动 STP → modify aux_price（主路径）；0 个 → 下新 STP（补保护）；≥2 个 → 撤多余
保留 1 个再 modify；modify 抛异常 → fallback「先下新 STP + 撤旧」。触发价取整到美股 tick 0.01。

⏳ 实测状态（2026-08-05）：基于港股 move_stop_tiger.py 解耦 + 美股适配；待美股盘中实测。

用法：
  python3 move_stop_tiger_us.py <symbol> <direction> <new_stop_price> <quantity>
    symbol          美股代码（US.MU）
    direction       long / short（持仓方向；fallback 下新 STP 时定 side）
    new_stop_price  新止损价
    quantity        持仓数量（严格=持仓量）
"""

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import trade_utils_tiger_us as U


def _is_stop_order(o):
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


def main():
    if len(sys.argv) < 5:
        print("用法: python3 move_stop_tiger_us.py <symbol> <direction> <new_stop_price> <quantity>", file=sys.stderr)
        sys.exit(1)

    symbol = sys.argv[1]
    direction = sys.argv[2]
    new_stop_price = float(sys.argv[3])
    quantity = int(float(sys.argv[4]))

    if not symbol.startswith("US."):
        print(json.dumps({"ok": False, "error": f"本脚本只处理美股（US.xxx），收到 {symbol}"}))
        sys.exit(1)
    if direction not in ("long", "short"):
        print(json.dumps({"ok": False, "error": f"direction 非法 '{direction}'"}))
        sys.exit(1)

    try:
        config = U.load_config()
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"老虎配置加载失败: {e}"}))
        sys.exit(1)

    result_base = {"action": "move_stop_tiger_us", "market": "US", "symbol": symbol, "direction": direction,
                   "new_stop_price": new_stop_price, "quantity": quantity}

    # 量校验：止损量严格=持仓量（超持仓券商判失效）
    try:
        pos = U.get_open_position_us(config, symbol)
        if pos is not None:
            held = pos["quantity"]
            if quantity > held:
                result_base.update({"ok": False, "error": f"止损量 {quantity} 超过美股持仓量 {held}，券商判失效，拒绝提交"})
                print(json.dumps(result_base, ensure_ascii=False))
                sys.exit(1)
            if quantity < held:
                result_base["warning"] = f"止损量 {quantity} < 持仓量 {held}（策略规定严格相等）"
        else:
            result_base["warning"] = "未读到美股持仓，按传入数量继续"
    except Exception as e:
        result_base["quantity_check_warning"] = f"持仓校验失败（继续）: {e}"

    tc = U.new_trade_client(config)
    trig = U.round_to_tick_us(new_stop_price)
    target_sym = U.to_tiger_symbol_us(symbol)
    stop_side = "Sell" if direction == "long" else "Buy"

    # 查当前活动止损单（该标的、止损类型含 TRAIL、非终结状态）
    stp_orders = []
    for o in (tc.get_orders() or []):
        sym = str(getattr(getattr(o, "contract", None), "symbol", ""))
        if sym != target_sym:
            continue
        if not _is_stop_order(o):
            continue
        if _status_str(o) in _TERMINAL:
            continue
        stp_orders.append(o)

    # 路径分支 0：无活动止损单 → 下新 STP（补保护）
    if len(stp_orders) == 0:
        result_base["path"] = "no_active_stop → submit new STP"
        try:
            stop_order_id = U.submit_stop_order_us(config, symbol, stop_side, quantity, trig)
        except Exception as e:
            result_base.update({"ok": False, "error": f"新止损单提交失败: {e}"})
            print(json.dumps(result_base, ensure_ascii=False))
            sys.exit(1)
        result_base.update({"ok": True, "stop_order_id": stop_order_id, "trigger_price": trig,
                            "stop_method": "新下 STP（仓位原无活动止损单）"})
        print(json.dumps(result_base, ensure_ascii=False))
        sys.exit(0)

    # 路径分支 ≥2：多个活动止损单 → 撤多余保留第一个（异常清理）
    if len(stp_orders) > 1:
        result_base["warning"] = f"检测到 {len(stp_orders)} 个活动止损单，撤多余的保留第一个再 modify"
        keep = stp_orders[0]
        for extra in stp_orders[1:]:
            try:
                U.cancel_order_us(config, getattr(extra, "id", None))
            except Exception as e:
                result_base.setdefault("extra_cancel_warnings", []).append(str(e))
        stp_orders = [keep]

    # 路径分支 1：单个活动止损单 → modify aux_price（主路径）
    stp = stp_orders[0]
    stp_id = getattr(stp, "id", None)
    old_aux = float(getattr(stp, "aux_price", 0) or 0)
    try:
        tc.modify_order(stp, aux_price=trig)
    except Exception as e:
        # modify 抛异常 → fallback「先下新 STP + 撤旧」（保证仓位有止损保护）
        result_base["modify_failed_fallback"] = f"modify 抛异常 {e}，回退到「先下新 STP + 撤旧」"
        try:
            new_id = U.submit_stop_order_us(config, symbol, stop_side, quantity, trig)
            U.cancel_order_us(config, stp_id)
            result_base.update({"ok": True, "stop_order_id": new_id, "trigger_price": trig,
                                "stop_method": "fallback：先下新 STP + 撤旧（modify 失败）"})
            print(json.dumps(result_base, ensure_ascii=False))
            sys.exit(0)
        except Exception as e2:
            result_base.update({"ok": False, "error": f"modify 失败且 fallback 也失败: {e2}"})
            print(json.dumps(result_base, ensure_ascii=False))
            sys.exit(1)

    # 验证 modify（aux_price 是否真的改到 trig）
    time.sleep(1.5)
    verified = False
    for o in (tc.get_orders() or []):
        if str(getattr(o, "id", "")) == str(stp_id):
            new_aux = float(getattr(o, "aux_price", 0) or 0)
            if abs(new_aux - trig) < 0.001:
                verified = True
            break

    result_base.update({"ok": verified, "stop_order_id": stp_id, "trigger_price": trig,
                        "old_trigger": old_aux,
                        "stop_method": "modify aux_price（单步、无撤单 race）",
                        "verified": verified})
    if not verified:
        result_base["warning"] = "modify 提交无异常但 aux_price 验证未确认，请人工核查"
    print(json.dumps(result_base, ensure_ascii=False))


if __name__ == "__main__":
    main()
