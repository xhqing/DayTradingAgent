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

下单失败自动降档重试（2026-08-11 立，00100 待办，同港股 open_position_tiger）：目标量被拒
（Invalid / 提交异常）时按降档序列（逐次减半取整手）自动重试到可下上限，每次失败输出 reason；
成交量 < 目标量时输出 downscaled=true + target_quantity + failed_attempts + warning。
不降档的两种失败：cross-trading（账户已有同标的未成交挂单，改单或撤单后重挂）/ 挂起超时（撤单退出由 AI 决策）。
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import trade_utils_tiger_us as U
import trade_utils_tiger as T   # 2026-08-16：降档循环用 _is_ambiguous_timeout_error 判模糊失败


def _downscale_sequence(quantity, lot_size=1):
    """开仓降档序列：目标量优先，之后逐次减半（向下取整手）直到最低整手。

    2026-08-11 立（00100 待办，与港股 open_position_tiger 同步）：目标量被拒时自动
    逐次减半重试到可下上限，避免 AI 手动乱试（8-06 港股 MINIMAX 教训同型）。
    例：1000 股（lot 1）→ 500 → 250 → 125 → 62 → 31 → 15 → 7 → 3 → 1。
    """
    lot = max(int(lot_size or 1), 1)
    seq = [quantity]
    q = int(int(quantity) // 2 // lot) * lot
    while q >= lot and q > 0:
        seq.append(q)
        if q == lot:
            break
        q = int(q // 2 // lot) * lot
    return seq


def _risk_params_from_config():
    """读 skill config.json 的 risk 节（2026-08-16 修，同港股版：原硬编码 0.02/0.10，
    config「修改本文件即可调参」契约不生效）。缺失回退默认。"""
    import json as _json
    try:
        _cfg = _json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                            "..", "config.json")))
        risk = _cfg.get("risk", {})
        return (float(risk.get("risk_fraction", 0.02)),
                float(risk.get("f_max", 0.10)))
    except Exception:
        return 0.02, 0.10


def _enforce_explicit_quantity_risk(quantity, entry_ref, stop_loss, equity):
    """显式传量路径的风控校验（2026-08-16 立，同港股版：原显式传量绕过 f_max /
    max_leverage 全部上限、唯一护栏是券商保证金拒单）。超限拒绝下单。
    返回 (ok, error_or_none)。"""
    import json as _json
    _, f_max = _risk_params_from_config()
    stop_distance = abs(entry_ref - stop_loss)
    actual_max_loss = quantity * stop_distance
    notes = []
    if equity:
        max_loss_cap = equity * f_max
        if actual_max_loss > max_loss_cap:
            notes.append(f"实际 max_loss {actual_max_loss:,.2f} 超过 equity×f_max 上限 "
                         f"{max_loss_cap:,.2f}（equity {equity:,.2f} × {f_max}）")
        try:
            _cfg = _json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                "..", "config.json")))
            max_leverage = float(_cfg.get("risk", {}).get("max_leverage", 10))
        except Exception:
            max_leverage = 10.0
        notional = quantity * entry_ref
        notional_cap = equity * max_leverage
        if notional > notional_cap:
            notes.append(f"开仓市值 {notional:,.2f} 超过 equity×max_leverage 上限 "
                         f"{notional_cap:,.2f}（equity {equity:,.2f} × {max_leverage}）")
    if notes:
        return False, "；".join(notes) + "。显式传量同样受风控上限约束（f_max / max_leverage），拒绝下单——请调小数量或改用 0 自动算仓位"
    return True, None


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
    # 账户选择（2026-08-12 立）：默认 None=paper 模拟账户；--account live 切实盘账户。
    # ⚠️ 实盘=真钱，AI 调用 --account live 前必须已征得用户明确同意（SKILL「auto 账户选择」双闸）。
    account = None
    if "--account" in sys.argv:
        idx = sys.argv.index("--account")
        if idx + 1 >= len(sys.argv):
            print("用法错误：--account 需要参数 live / paper", file=sys.stderr)
            sys.exit(1)
        account = sys.argv[idx + 1].lower()
        if account not in ("live", "paper"):
            print(json.dumps({"ok": False, "error": f"--account 必须是 live/paper，收到 '{account}'"}))
            sys.exit(1)

    if not symbol.startswith("US."):
        print(json.dumps({"ok": False, "error": f"本脚本只处理美股（US.xxx），收到 {symbol}"}))
        sys.exit(1)
    if direction not in ("long", "short"):
        print(json.dumps({"ok": False, "error": f"direction 必须是 long/short，收到 '{direction}'"}))
        sys.exit(1)
    # 止损价方向硬校验（2026-08-16 立）：做多必须 stop < entry_ref、做空必须 stop > entry_ref——
    # 方向错的止损腿开盘即触发，且下游赔率计算因止损距 ≤0 报错（此前实现返回 inf、以最诱人形态放行）。
    if direction == "long" and stop_loss >= entry_ref:
        print(json.dumps({"ok": False, "error": (
            f"做多止损价必须在参考价下方（stop_loss={stop_loss} ≥ entry_ref={entry_ref}），"
            f"方向反了——这样的止损腿开盘即触发")}, ensure_ascii=False))
        sys.exit(1)
    if direction == "short" and stop_loss <= entry_ref:
        print(json.dumps({"ok": False, "error": (
            f"做空止损价必须在参考价上方（stop_loss={stop_loss} ≤ entry_ref={entry_ref}），"
            f"方向反了——这样的止损腿开盘即触发")}, ensure_ascii=False))
        sys.exit(1)

    try:
        config = U.load_config(account=account)
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

    # 真实费率上下文（2026-08-12；2026-08-17 平台费改固定模式、不再查当月订单数；美股按股计费）：
    # 含 shares / sec_type / market。quantity>0 才用真实费率；quantity==0（自动算仓位）此时
    # shares 未知，价格范围检查用旧百分比口径（_net_odds 按 entry/target 算每股费，
    # 不依赖 shares），算出仓位后由调用方按真实费率复核。
    # （2026-08-16 修复：原版无此守卫，quantity=0 时真实费率分支 (fee_open+fee_close)/shares
    # 除零崩溃——「数量传 0 = 自动算仓位」的文档化主用法在美股不可用。对齐港股版守卫。）
    fee_ctx = U.build_fee_ctx(symbol, quantity, config) if quantity > 0 else None
    in_range, range_low, range_high, odds_at_ref, odds_at_current = U.check_price_in_range(
        direction, current_price, entry_ref, stop_loss, target, symbol, fee_ctx
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

    # 自动算仓位（quantity=0）：equity 取老虎账户 USD 净值、lot_size 从 get_contract 取（美股默认 1）。
    # risk_fraction / f_max 从 config.json 读（2026-08-16 修，同港股版：原硬编码 0.02/0.10）。
    lot_size = None
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
        risk_fraction, f_max = _risk_params_from_config()
        quantity, max_loss, budget_B = U.calc_position_size(
            equity, risk_fraction, f_max, stop_distance, lot_size, entry_price=entry_ref)
        if quantity <= 0:
            result_base.update({"ok": False, "error": "仓位为 0（止损距太大或权益不足）"})
            print(json.dumps(result_base, ensure_ascii=False))
            sys.exit(1)
        result_base.update({"auto_sized": True, "equity": equity, "equity_currency": currency,
                            "lot_size": lot_size, "budget_B": round(budget_B, 2),
                            "max_loss": round(max_loss, 2)})
    result_base["quantity"] = quantity

    # 止损价 tick 取整（2026-08-16 修，同港股版：原止损价原样传入，不合 tick 的附加腿
    # 会连累主单整体被拒、降档循环烧完全部档）。美股统一 0.01。
    _stop_raw = stop_loss
    stop_loss = U.round_to_tick_us(stop_loss)
    if stop_loss != _stop_raw:
        result_base["stop_loss_adjusted"] = f"{_stop_raw} → {stop_loss}（取整到美股 tick 0.01）"

    # 显式传量的风控校验（2026-08-16 立，同港股版：显式传量不再绕过 f_max / max_leverage）
    if not result_base.get("auto_sized"):
        try:
            equity, currency = U.load_equity_us(config)
        except Exception:
            equity, currency = None, None
        if equity is None:
            result_base["risk_check_note"] = "账户净值取不到，跳过 f_max / max_leverage 校验"
        ok, err = _enforce_explicit_quantity_risk(quantity, entry_ref, stop_loss, equity)
        if not ok:
            result_base.update({"ok": False, "error": err,
                                "equity": equity, "equity_currency": currency})
            print(json.dumps(result_base, ensure_ascii=False))
            sys.exit(1)
        result_base["risk_checked"] = True

    # 购买力上限（2026-08-16 立，同港股版：可买股数上限 = buying_power × long_initial_margin
    # ÷ 参考价；美股 get_contract 已实测返回 long_initial_margin 字段、USD 同币种无汇率偏差）。
    _bp_shares, _bp_val, _bp_margin = T.get_buying_power_tiger(config, symbol, entry_ref)
    if _bp_shares is not None and quantity > _bp_shares:
        _lot_bp = lot_size if lot_size else 1
        capped = max(int(_bp_shares // _lot_bp) * _lot_bp, 0)
        result_base["capped_by_buying_power"] = True
        result_base["buying_power"] = _bp_val
        result_base["margin_rate"] = _bp_margin
        result_base["buying_power_max_shares"] = _bp_shares
        result_base["target_quantity"] = quantity
        result_base["quantity"] = capped
        result_base["warning"] = (
            f"目标量 {quantity} 股超购买力上限 {_bp_shares} 股（buying_power {_bp_val:,.0f} × "
            f"保证金率 {_bp_margin} ÷ 参考价 {entry_ref}），下单前主动降档到 {capped} 股——减少被动拒单")
        if capped <= 0:
            result_base.update({"ok": False, "error": "购买力上限降档后为 0（buying_power 不足以开 1 股）"})
            print(json.dumps(result_base, ensure_ascii=False))
            sys.exit(1)
        quantity = capped

    # 参考价（主单已改 MKT 市价单，此价仅作输出参考）：做多取 ask / 做空取 bid，取整到美股 tick 0.01
    if direction == "long":
        lo_price = quote["ask"] if quote.get("ask") else current_price
    else:
        lo_price = quote["bid"] if quote.get("bid") else current_price
    lo_price = U.round_to_tick_us(lo_price)
    side_str = "Buy" if direction == "long" else "Sell"

    # 主单 MKT + 附加止损腿 OrderLeg('LOSS')（一次提交）
    #
    # 失败自动降档重试（2026-08-11 立，00100 待办，与港股 open_position_tiger 同逻辑）：
    # 目标量被拒（提交抛异常 / 回查 Invalid）时按降档序列逐次减半重试到可下上限，每次失败
    # 输出 reason。不降档的失败：① cross-trading（账户已有同标的未成交挂单，新单与其
    # 交叉成交被拒，2026-08-11 用户纠正：与持仓止损单无关——开仓=空仓建仓），与量无关、
    # 降档无意义，须改单或撤单再重挂；② 挂起超时未成交（可能是盘口问题而非被拒，
    # 撤单退出由 AI 决策）；③ 提交超时模糊失败（2026-08-16 立，请求可能已达券商，
    # 降档=重复开仓路径，立即停止）；④ 部分成交超时（PartiallyFilled，2026-08-16 立，
    # 已成交部分已建仓、不降档叠仓，读实际成交量如实上报）。
    lot = lot_size if lot_size else (U.get_lot_size_us(U.new_trade_client(config), symbol) or 1)
    attempts = _downscale_sequence(quantity, lot)
    failures = []
    order_id = None
    fill_price = None
    status = ""
    filled = False   # 2026-08-16 修（同港股版）：全部档位 submit 抛异常时不再 UnboundLocalError
    part_filled_qty = None
    for qty in attempts:
        # 重复下单防抖（2026-08-16 立，同港股版）：上一档超时可能实际已在场
        try:
            has_active, active_ids = U.has_active_open_order_us(config, symbol, side_str)
            if has_active:
                result_base.update({"lo_price": lo_price,
                                    "ok": False,
                                    "error": f"重复下单防抖：{symbol} 已有活动开仓方向委托单 {active_ids}，拒绝继续降档下单——先查当日订单确认状态",
                                    "failures": failures, "blocked_by": "active_open_order_dedup"})
                print(json.dumps(result_base, ensure_ascii=False))
                sys.exit(1)
        except Exception as de:
            failures.append({"qty": qty, "status": "dedup_check_failed", "reason": str(de),
                             "note": "防抖检查查询失败，为避免重复下单风险停止降档"})
            break
        try:
            order_id = U.submit_order_with_stop_us(config, symbol, side_str, qty, lo_price, stop_loss)
        except Exception as e:
            msg = str(e)
            if "模糊失败" in msg or T._is_ambiguous_timeout_error(e):
                failures.append({"qty": qty, "status": "submit_timeout_ambiguous", "reason": msg,
                                 "hint": "请求可能已达券商，禁止降档续下——先查当日订单确认是否已成交，未确认前不得再下单"})
                break  # 模糊失败：可能已下单成功，降档=双倍持仓
            if "cross" in msg.lower() and "pending" in msg.lower():
                failures.append({"qty": qty, "status": "submit_exception", "reason": msg,
                                 "hint": "账户已有同标的未成交委托单（之前下单残留），改单或撤销该挂单后再重新挂单"})
                break  # cross-trading 与量无关，降档无意义
            failures.append({"qty": qty, "status": "submit_exception", "reason": msg})
            continue
        filled, fill_price, status, reason = U.check_order_filled_us(config, order_id, timeout=8)
        if filled:
            break
        if status == "PartiallyFilled":
            part_filled_qty = U.get_order_filled_qty_us(config, order_id)
            failures.append({"qty": qty, "status": status,
                             "filled_qty": part_filled_qty,
                             "reason": reason or "部分成交未在超时内全额成交——已成交部分已建仓，请按 filled_qty 复核持仓，勿重复开仓"})
            break
        if "Invalid" not in status:
            # 挂起超时 / 轮询异常 / 其它非被拒状态：撤单退出（不降档，让 AI 决策）
            try:
                U.cancel_order_us(config, order_id)
                cancel_note = "已撤主单及附加止损"
            except Exception as ce:
                cancel_note = f"撤单失败需手动撤 {order_id}: {ce}"
            failures.append({"qty": qty, "status": status, "reason": reason or "", "cancel_note": cancel_note})
            break
        # Invalid（被拒）→ 记录失败原因并降档继续
        hint = ""
        if reason and "cross" in reason.lower() and "pending" in reason.lower():
            hint = "账户已有同标的未成交委托单（之前下单残留），改单或撤销该挂单后再重新挂单"
        failures.append({"qty": qty, "status": status,
                         "reason": reason or "（无具体原因，被拒订单 reason 常见为通用文案）",
                         **({"hint": hint} if hint else {})})
        if hint:
            break  # cross-trading 与量无关，降档无意义
    if not filled:
        if part_filled_qty:
            result_base.update({"lo_price": lo_price,
                                "ok": True, "part_filled": True, "quantity": part_filled_qty,
                                "order_id": order_id,
                                "fill_price": fill_price,
                                "warning": f"部分成交 {part_filled_qty}/{quantity} 股（超时未全成）——已按实际成交量上报，请复核持仓与残留附加止损腿",
                                "failures": failures, "main_status": status,
                                "method": "market+attached_stop"})
            print(json.dumps(result_base, ensure_ascii=False))
            sys.exit(0)
        result_base.update({"lo_price": lo_price,
                            "ok": False, "error": f"开仓全部失败（尝试 {len(failures)} 档）", "failures": failures})
        print(json.dumps(result_base, ensure_ascii=False))
        sys.exit(1)

    result_base["lo_price"] = lo_price
    if len(failures) > 0:
        # 降档成交：保留目标量字段、更新实际量，提示仓位缩水
        result_base["target_quantity"] = quantity
        result_base["quantity"] = attempts[len(failures)] if len(failures) < len(attempts) else qty
        result_base["downscaled"] = True
        result_base["failed_attempts"] = failures
        stop_distance = abs(entry_ref - stop_loss)
        actual_max_loss = result_base["quantity"] * stop_distance
        result_base["actual_max_loss"] = round(actual_max_loss, 2)
        budget_note = ""
        if result_base.get("budget_B"):
            budget_note = f"，预算 B {result_base['budget_B']:.2f} 的 {actual_max_loss / result_base['budget_B'] * 100:.0f}%"
        result_base["warning"] = (
            f"目标量 {quantity} 被拒（{len(failures)} 档失败），降档成交 {result_base['quantity']}——"
            f"实际 max_loss {actual_max_loss:.2f}{budget_note}，仓位缩水，收益被拖累")
    result_base["order_id"] = order_id
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
