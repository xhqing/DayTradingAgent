#!/usr/bin/env python3
"""港股移动止盈动作脚本（老虎证券模拟账户，港股默认账户，2026-08-23 用户立——第 4 类动作）。

**优先 modify 现有活动止盈单的触发价**（单步、无撤单 race，机制同 move_stop 对止损单的
modify）。止盈单来源：开仓双腿附加单的 PROFIT 腿，主单成交后落成独立止盈单（BRACKETS
括号订单：与止损单一边触发、另一边自动作废）。

止盈单识别（order_type 层面）：PROFIT 腿落成的独立单预期为触及限价（LMT）形态、带
parent_id 关联开仓主单——与普通挂单的区分靠 parent_id + attr_desc（附加单属性描述）。
⚠️ 识别细节待 paper 实测校准（记 TODO：实测确认 PROFIT 腿落成后的 order_type 与 attr_desc
实际取值）；当前实现按「该标的活动单里非止损类条件单 + 有 parent_id」优先匹配、匹配不到
时 fallback 下新止盈限价单。

分支（同 move_stop 逻辑骨架）：
  - 1 个活动止盈单 → modify 触发价（主路径，单步）
  - 0 个活动止盈单 → 下新止盈限价单（补止盈）
  - ≥2 个活动止盈单 → 撤多余保留 1 个再 modify（异常清理）
  - modify 抛异常 → fallback「先下新止盈单 + 撤旧」（保证仓位有止盈保护）

移动止盈方向硬校验（同 move_stop 口径）：新止盈必须未穿越现价——做多止盈在现价上方、
做空止盈在现价下方。传到现价错误一侧会瞬间触发平仓（那是主动平仓的语义，归 close_position
脚本管，它正是故意把触发价逼近现价来平仓的）。

用法：
  python3 move_target_tiger.py <symbol> <direction> <new_target_price> <quantity>
    symbol           港股代码（HK.02800）
    direction        long / short（持仓方向；fallback 下新止盈单时定 side）
    new_target_price 新止盈触发价
    quantity         持仓数量（严格=持仓量）
"""

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import trade_utils_tiger as U


def _is_stop_order(o):
    """止损单判定（与 move_stop / close 同口径：STP/STOP/TRAIL/LOSS 腿）。"""
    otype = getattr(o, "order_type", None)
    otype_val = otype.value if hasattr(otype, "value") else str(otype)
    upper = str(otype_val).upper()
    legs = getattr(o, "order_legs", None) or []
    return ("STP" in upper or "STOP" in upper or "TRAIL" in upper
            or any(str(getattr(leg, "leg_type", "")).upper() == "LOSS" for leg in legs))


def _is_profit_order(o):
    """止盈单判定（2026-08-23 立）：PROFIT 附加腿落成的独立单。

    判定口径：该标的的活动单里排除止损单后，PROFIT 腿标记 / attr_desc 含「止盈」 /
    带 parent_id 的附加单落成单即止盈单（开仓附加腿都有 parent_id 关联主单；普通手动
    挂单无 parent_id）。LMT 形态 + parent_id 是 PROFIT 腿的预期落成形态，待 paper 实测
    校准（docstring 头部有说明）。"""
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
        print("用法: python3 move_target_tiger.py <symbol> <direction> <new_target_price> <quantity>",
              file=sys.stderr)
        sys.exit(1)

    symbol = sys.argv[1]
    direction = sys.argv[2]
    new_target_price = float(sys.argv[3])
    quantity = int(float(sys.argv[4]))
    # 账户选择（同 move_stop）：默认 None=paper；--account live 切实盘。⚠️ live=真钱须用户已确认。
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
    # 实盘解锁前置闸（2026-08-21 立，实盘误开防护检查点①）。
    import live_unlock
    live_unlock.live_gate_for_order_scripts(account, "move_target_tiger")

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

    # 新止盈价方向硬校验：新止盈必须未穿越现价——做多止盈在现价上方、做空在现价下方。
    # 传到错误一侧会瞬间触发平仓（那是 close_position 的语义，不经本校验）。
    try:
        _quote = U.get_quote_tiger(config, symbol)
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"获取港股报价失败（无法校验新止盈价方向）: {e}"},
                         ensure_ascii=False))
        sys.exit(1)
    if _quote is None or _quote.get("last") is None:
        print(json.dumps({"ok": False, "error": f"港股报价为空: {symbol}（无法校验新止盈价方向）"},
                         ensure_ascii=False))
        sys.exit(1)
    _last = float(_quote["last"])
    if direction == "long" and new_target_price <= _last:
        print(json.dumps({"ok": False, "error": (
            f"做多新止盈价 {new_target_price} ≤ 现价 {_last}——止盈已穿越现价、提交会瞬间触发平仓；"
            f"要主动平仓请用 close_position_tiger.py，不要用移动止盈脚本")}, ensure_ascii=False))
        sys.exit(1)
    if direction == "short" and new_target_price >= _last:
        print(json.dumps({"ok": False, "error": (
            f"做空新止盈价 {new_target_price} ≥ 现价 {_last}——止盈已穿越现价、提交会瞬间触发平仓；"
            f"要主动平仓请用 close_position_tiger.py，不要用移动止盈脚本")}, ensure_ascii=False))
        sys.exit(1)

    result_base = {"action": "move_target_tiger", "market": "HK", "symbol": symbol,
                   "direction": direction, "new_target_price": new_target_price, "quantity": quantity}

    # 量校验：止盈量严格=持仓量
    try:
        pos = U.get_open_position_tiger(config, symbol)
        if pos is not None:
            held = pos["quantity"]
            if quantity > held:
                result_base.update({"ok": False, "error": f"止盈量 {quantity} 超过港股持仓量 {held}，拒绝提交"})
                print(json.dumps(result_base, ensure_ascii=False))
                sys.exit(1)
            if quantity < held:
                result_base["warning"] = f"止盈量 {quantity} < 持仓量 {held}（策略规定严格相等）"
        else:
            result_base["warning"] = "未读到港股持仓，按传入数量继续"
    except Exception as e:
        result_base["quantity_check_warning"] = f"持仓校验失败（继续）: {e}"

    tc = U.new_trade_client(config)
    tick_sizes = U.get_tick_sizes_tiger(tc, symbol)
    trig = U.round_to_tick_tiger(new_target_price, tick_sizes)
    target_sym = U.to_tiger_symbol(symbol)
    profit_side = "Sell" if direction == "long" else "Buy"

    # 查当前活动止盈单（该标的、止盈类、非终结状态）
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
            profit_order_id = U.submit_limit_order_tiger(config, symbol, profit_side, quantity, trig)
        except Exception as e:
            result_base.update({"ok": False, "error": f"新止盈单提交失败: {e}"})
            print(json.dumps(result_base, ensure_ascii=False))
            sys.exit(1)
        result_base.update({"ok": True, "profit_order_id": profit_order_id, "trigger_price": trig,
                            "profit_method": "新下止盈限价单（仓位原无活动止盈单）"})
        print(json.dumps(result_base, ensure_ascii=False))
        sys.exit(0)

    # 分支 ≥2：多个活动止盈单 → 撤多余保留第一个（异常清理）
    if len(profit_orders) > 1:
        result_base["warning"] = f"检测到 {len(profit_orders)} 个活动止盈单，撤多余的保留第一个再 modify"
        keep = profit_orders[0]
        for extra in profit_orders[1:]:
            try:
                U.cancel_order_tiger(config, getattr(extra, "id", None))
            except Exception as e:
                result_base.setdefault("extra_cancel_warnings", []).append(str(e))
        profit_orders = [keep]

    # 分支 1：单个活动止盈单 → modify（主路径）。PROFIT 腿落成 LMT 形态时价格字段是
    # limit_price；STP 形态则是 aux_price——两个都传，券商按单型取其一。
    po = profit_orders[0]
    po_id = getattr(po, "id", None)
    old_px = float(getattr(po, "limit_price", 0) or getattr(po, "aux_price", 0) or 0)
    try:
        tc.modify_order(po, limit_price=trig, aux_price=trig)
    except Exception as e:
        # modify 抛异常 → fallback「先下新止盈单 + 撤旧」（分步报告，同 move_stop 2026-08-16 修）
        result_base["modify_failed_fallback"] = f"modify 抛异常 {e}，回退到「先下新止盈单 + 撤旧」"
        new_id = None
        new_err = None
        try:
            new_id = U.submit_limit_order_tiger(config, symbol, profit_side, quantity, trig)
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
            U.cancel_order_tiger(config, po_id)
        except Exception as e3:
            cancel_warn = f"撤旧止盈单 {po_id} 失败（需手动撤，防止旧触发价 {old_px} 的止盈仍生效）: {e3}"
        result_base.update({"ok": True, "profit_order_id": new_id, "trigger_price": trig,
                            "profit_method": "fallback：先下新止盈单 + 撤旧（modify 失败）",
                            **({"fallback_cancel_warning": cancel_warn} if cancel_warn else {})})
        print(json.dumps(result_base, ensure_ascii=False))
        sys.exit(0)

    # 验证 modify（价格是否真的改到 trig）
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
