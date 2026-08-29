#!/usr/bin/env python3
"""美股移动止盈动作脚本（老虎证券模拟账户，美股默认账户，2026-08-23 用户立——第 4 类动作）。

机制同港股 move_target_tiger（modify 活动止盈单触发价为主路径、0 个补新、≥2 撤多余、
modify 失败 fallback 先下新+撤旧），差异只有：symbol 走美股（US.MU → 裸代码 MU）、
tick 0.01 固定、行情走富途 OpenD 单源（老虎美股无行情权限）、modify 带 outside_rth=True
（美股盘前可交易窗口，同 move_stop_tiger_us）。

止盈单识别与方向校验口径同港股版（见 move_target_tiger.py docstring；识别细节待 paper
实测校准）。

用法：
  python3 move_target_tiger_us.py <symbol> <direction> <new_target_price> <quantity>
    symbol           美股代码（US.MU）
    direction        long / short（持仓方向；fallback 下新止盈单时定 side）
    new_target_price 新止盈触发价
    quantity         持仓数量（严格=持仓量）
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


def _is_profit_order(o):
    """止盈单判定（同港股版）：非止损 + PROFIT 腿标记 / attr_desc 含止盈 / 带 parent_id。"""
    if _is_stop_order(o):
        return False
    legs = getattr(o, "order_legs", None) or []
    if any(str(getattr(leg, "leg_type", "")).upper() == "PROFIT" for leg in legs):
        return True
    attr = str(getattr(o, "attr_desc", "") or "")
    if "止盈" in attr or "PROFIT" in attr.upper():
        return True
    return getattr(o, "parent_id", None) is not None


def _status_str(o):
    status = getattr(o, "status", None)
    return status.value if hasattr(status, "value") else str(status)


_TERMINAL = ("Filled", "Cancelled", "Inactive", "Invalid", "Expired")


def main():
    if len(sys.argv) < 5:
        print("用法: python3 move_target_tiger_us.py <symbol> <direction> <new_target_price> <quantity>",
              file=sys.stderr)
        sys.exit(1)

    symbol = sys.argv[1]
    direction = sys.argv[2]
    new_target_price = float(sys.argv[3])
    quantity = int(float(sys.argv[4]))
    # 账户选择 + 实盘解锁前置闸（同 move_stop_tiger_us）。
    account = None
    if "--account" in sys.argv:
        idx = sys.argv.index("--account")
        if idx + 1 >= len(sys.argv):
            print("用法错误：--account 需要一个值：live / paper", file=sys.stderr)
            sys.exit(1)
        account = sys.argv[idx + 1].lower()
        if account not in ("live", "paper"):
            print(json.dumps({"ok": False, "error": f"--account 必须是 live/paper，收到 '{account}'"}))
            sys.exit(1)
    import live_unlock
    live_unlock.live_gate_for_order_scripts(account, "move_target_tiger_us")

    if not symbol.startswith("US."):
        print(json.dumps({"ok": False, "error": f"本脚本只处理美股（US.xxx），收到 {symbol}"}))
        sys.exit(1)
    if direction not in ("long", "short"):
        print(json.dumps({"ok": False, "error": f"direction 非法 '{direction}'"}))
        sys.exit(1)

    try:
        config = U.load_config(account=account)
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"老虎配置加载失败: {e}"}))
        sys.exit(1)

    # 新止盈价方向硬校验（同港股版）：做多止盈在现价上方、做空在现价下方。
    try:
        _quote = U.get_quote_us(config, symbol)
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"获取美股行情失败（无法校验新止盈价方向）: {e}"},
                         ensure_ascii=False))
        sys.exit(1)
    if _quote is None or _quote.get("last") is None:
        print(json.dumps({"ok": False, "error": f"美股报价为空: {symbol}（无法校验新止盈价方向）"},
                         ensure_ascii=False))
        sys.exit(1)
    _last = float(_quote["last"])
    if direction == "long" and new_target_price <= _last:
        print(json.dumps({"ok": False, "error": (
            f"做多新止盈价 {new_target_price} ≤ 现价 {_last}——止盈已穿越现价、提交会瞬间触发平仓；"
            f"要主动平仓请用 close_position_tiger_us.py，不要用移动止盈脚本")}, ensure_ascii=False))
        sys.exit(1)
    if direction == "short" and new_target_price >= _last:
        print(json.dumps({"ok": False, "error": (
            f"做空新止盈价 {new_target_price} ≥ 现价 {_last}——止盈已穿越现价、提交会瞬间触发平仓；"
            f"要主动平仓请用 close_position_tiger_us.py，不要用移动止盈脚本")}, ensure_ascii=False))
        sys.exit(1)

    result_base = {"action": "move_target_tiger_us", "market": "US", "symbol": symbol,
                   "direction": direction, "new_target_price": new_target_price, "quantity": quantity}

    # 量校验：止盈量严格=持仓量
    try:
        pos = U.get_open_position_us(config, symbol)
        if pos is not None:
            held = pos["quantity"]
            if quantity > held:
                result_base.update({"ok": False, "error": f"止盈量 {quantity} 超过美股持仓量 {held}，拒绝提交"})
                print(json.dumps(result_base, ensure_ascii=False))
                sys.exit(1)
            if quantity < held:
                result_base["warning"] = f"止盈量 {quantity} < 持仓量 {held}（策略规定严格相等）"
        else:
            result_base["warning"] = "未读到美股持仓，按传入数量继续"
    except Exception as e:
        result_base["quantity_check_warning"] = f"持仓校验失败（继续）: {e}"

    tc = U.new_trade_client(config)
    trig = U.round_to_tick_us(new_target_price)
    target_sym = U.to_tiger_symbol_us(symbol)
    profit_side = "Sell" if direction == "long" else "Buy"

    # 查当前活动止盈单
    profit_orders = []
    for o in (tc.get_orders() or []):
        sym = str(getattr(getattr(o, "contract", None), "symbol", ""))
        if sym != target_sym:
            continue
        if not _is_profit_order(o):
            continue
        if _status_str(o) in _TERMINAL:
            continue
        profit_orders.append(o)

    # 分支 0：无活动止盈单 → 下新止盈限价单（补止盈）
    if len(profit_orders) == 0:
        result_base["path"] = "no_active_profit → submit new profit LMT"
        try:
            profit_order_id = U.submit_limit_order_us(config, symbol, profit_side, quantity, trig)
        except Exception as e:
            result_base.update({"ok": False, "error": f"新止盈单提交失败: {e}"})
            print(json.dumps(result_base, ensure_ascii=False))
            sys.exit(1)
        result_base.update({"ok": True, "profit_order_id": profit_order_id, "trigger_price": trig,
                            "profit_method": "新下止盈限价单（仓位原无活动止盈单）"})
        print(json.dumps(result_base, ensure_ascii=False))
        sys.exit(0)

    # 分支 ≥2：撤多余保留第一个
    if len(profit_orders) > 1:
        result_base["warning"] = f"检测到 {len(profit_orders)} 个活动止盈单，撤多余的保留第一个再 modify"
        keep = profit_orders[0]
        for extra in profit_orders[1:]:
            try:
                U.cancel_order_us(config, getattr(extra, "id", None))
            except Exception as e:
                result_base.setdefault("extra_cancel_warnings", []).append(str(e))
        profit_orders = [keep]

    # 分支 1：modify 主路径（outside_rth=True，同 move_stop_tiger_us 的盘前窗口口径）
    po = profit_orders[0]
    po_id = getattr(po, "id", None)
    old_px = float(getattr(po, "limit_price", 0) or getattr(po, "aux_price", 0) or 0)
    try:
        tc.modify_order(po, limit_price=trig, aux_price=trig, outside_rth=True)
    except Exception as e:
        result_base["modify_failed_fallback"] = f"modify 抛异常 {e}，回退到「先下新止盈单 + 撤旧」"
        new_id = None
        new_err = None
        try:
            new_id = U.submit_limit_order_us(config, symbol, profit_side, quantity, trig)
        except Exception as e2:
            new_err = e2
        if new_id is None:
            result_base.update({"ok": False, "error": (
                f"modify 失败，且 fallback 新止盈单提交也失败（旧止盈单仍在场、触发价未变"
                f"{old_px}）: {new_err}——若失败原因是提交超时（模糊失败），新单可能已在券商侧"
                f"受理，补挂前须先查当日订单确认，否则会多重止盈")},
                ensure_ascii=False)
            print(json.dumps(result_base, ensure_ascii=False))
            sys.exit(1)
        cancel_warn = None
        try:
            U.cancel_order_us(config, po_id)
        except Exception as e3:
            cancel_warn = f"撤旧止盈单 {po_id} 失败（需手动撤，防止旧触发价 {old_px} 的止盈仍生效）: {e3}"
        result_base.update({"ok": True, "profit_order_id": new_id, "trigger_price": trig,
                            "profit_method": "fallback：先下新止盈单 + 撤旧（modify 失败）",
                            **({"fallback_cancel_warning": cancel_warn} if cancel_warn else {})})
        print(json.dumps(result_base, ensure_ascii=False))
        sys.exit(0)

    # 验证 modify
    time.sleep(1.5)
    verified = False
    for o in (tc.get_orders() or []):
        if str(getattr(o, "id", "")) == str(po_id):
            new_px = float(getattr(o, "limit_price", 0) or getattr(o, "aux_price", 0) or 0)
            if abs(new_px - trig) < 0.001:
                verified = True
            break

    result_base.update({"ok": verified, "profit_order_id": po_id, "trigger_price": trig,
                        "old_trigger": old_px,
                        "profit_method": "modify 止盈触发价（单步、无撤单 race）",
                        "verified": verified})
    if not verified:
        result_base["warning"] = "modify 提交无异常但价格验证未确认，请人工核查"
    print(json.dumps(result_base, ensure_ascii=False))


if __name__ == "__main__":
    main()
