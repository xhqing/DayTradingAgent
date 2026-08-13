#!/usr/bin/env python3
"""港股移动止损动作脚本（老虎证券模拟账户，港股默认账户）。

**优先 modify 现有活动 STP 单的 aux_price**（单步、无撤单 race）——2026-08-05 实测 modify 开仓
附加止损单（OrderLeg('LOSS') 落成的独立 STP）aux_price 492→493 成功、status=Submitted，故移损
不再需要「先撤旧 STP + 再下新 STP」两步。分支：
  - 1 个活动 STP → modify aux_price（主路径，单步）
  - 0 个活动 STP → 下新 STP（仓位无止损保护、补上）
  - ≥2 个活动 STP → 撤多余保留 1 个再 modify（异常清理）
  - modify 抛异常 → fallback「先下新 STP + 撤旧」（保证仓位有止损保护）

量严格=持仓量（超持仓券商判失效）。触发价取整到港股 tick。

✅ 实测状态（2026-08-05）：modify 附加止损单 aux_price 成功（腾讯 100 股、492→493、
status=Submitted）。主路径 modify 验证通过。

用法：
  python3 move_stop_tiger.py <symbol> <direction> <new_stop_price> <quantity>
    symbol          港股代码（HK.02800）
    direction       long / short（持仓方向；fallback 下新 STP 时定 side）
    new_stop_price  新止损价
    quantity        持仓数量（严格=持仓量）
"""

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import trade_utils_tiger as U


def _is_stop_order(o):
    """订单是否为止损单（STP/STOP 类型）。"""
    otype = getattr(o, "order_type", None)
    otype_val = otype.value if hasattr(otype, "value") else str(otype)
    return "STP" in str(otype_val).upper() or "STOP" in str(otype_val).upper()


def _status_str(o):
    status = getattr(o, "status", None)
    return status.value if hasattr(status, "value") else str(status)


# 已终结状态（不再活动的订单，查活动 STP 时排除）
_TERMINAL = ("Filled", "Cancelled", "Inactive", "Invalid", "Expired")


def main():
    if len(sys.argv) < 5:
        print("用法: python3 move_stop_tiger.py <symbol> <direction> <new_stop_price> <quantity>", file=sys.stderr)
        sys.exit(1)

    symbol = sys.argv[1]
    direction = sys.argv[2]
    new_stop_price = float(sys.argv[3])
    quantity = int(float(sys.argv[4]))
    # 账户选择（2026-08-12 立）：默认 None=paper；--account live 切实盘。⚠️ live=真钱须用户已确认。
    account = None
    if "--account" in sys.argv:
        idx = sys.argv.index("--account")
        account = sys.argv[idx + 1].lower() if idx + 1 < len(sys.argv) else None
        if account not in ("live", "paper"):
            print(json.dumps({"ok": False, "error": f"--account 必须是 live/paper，收到 '{account}'"}))
            sys.exit(1)

    if not symbol.startswith("HK."):
        print(json.dumps({"ok": False, "error": f"本脚本只处理港股（HK.xxx），收到 {symbol}"}))
        sys.exit(1)
    if direction not in ("long", "short"):
        print(json.dumps({"ok": False, "error": f"direction 非法 '{direction}'"}))
        sys.exit(1)

    try:
        config = U.load_config(account=account)
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"老虎配置加载失败: {e}"}))
        sys.exit(1)

    result_base = {"action": "move_stop_tiger", "market": "HK", "symbol": symbol, "direction": direction,
                   "new_stop_price": new_stop_price, "quantity": quantity}

    # 量校验：本策略规定止损量严格=持仓量（超持仓会被券商判失效）
    try:
        pos = U.get_open_position_tiger(config, symbol)
        if pos is not None:
            held = pos["quantity"]
            if quantity > held:
                result_base.update({"ok": False, "error": f"止损量 {quantity} 超过港股持仓量 {held}，券商判失效，拒绝提交"})
                print(json.dumps(result_base, ensure_ascii=False))
                sys.exit(1)
            if quantity < held:
                result_base["warning"] = f"止损量 {quantity} < 持仓量 {held}（策略规定严格相等）"
        else:
            result_base["warning"] = "未读到港股持仓，按传入数量继续"
    except Exception as e:
        result_base["quantity_check_warning"] = f"持仓校验失败（继续）: {e}"

    tc = U.new_trade_client(config)
    tick_sizes = U.get_tick_sizes_tiger(tc, symbol)
    trig = U.round_to_tick_tiger(new_stop_price, tick_sizes)
    target_sym = U.to_tiger_symbol(symbol)
    stop_side = "Sell" if direction == "long" else "Buy"

    # 查当前活动 STP 单（该标的、止损类型、非终结状态）
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
            stop_order_id = U.submit_stop_order_tiger(config, symbol, stop_side, quantity, trig)
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
                U.cancel_order_tiger(config, getattr(extra, "id", None))
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
            new_id = U.submit_stop_order_tiger(config, symbol, stop_side, quantity, trig)
            U.cancel_order_tiger(config, stp_id)
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
