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

下单失败自动降档重试（2026-08-11 立，00100 待办）：目标量被拒（Invalid / 提交异常）时
按降档序列（逐次减半取整手）自动重试到可下上限，每次失败输出 reason；成交量 < 目标量时
输出 downscaled=true + target_quantity + failed_attempts + warning（仓位缩水提示）。
不降档的两种失败：cross-trading（账户已有同标的未成交挂单，改单或撤单后重挂）/ 挂起超时（撤单退出由 AI 决策）。

输出 JSON：
  成功：{ok, action, order_id, fill_price, method:"market+attached_stop"|"limit+attached_stop",
        downscaled?(target_quantity, failed_attempts, warning, actual_max_loss), ...}
  失败：{ok:false, error, failures:[{qty,status,reason,hint?}], range_low, range_high, current_price, ...}
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import trade_utils_tiger as U


def _downscale_sequence(quantity, lot_size):
    """开仓降档序列：目标量优先，之后逐次减半（向下取整手）直到最低整手。

    2026-08-11 立（00100 待办）：8-06 MINIMAX 计划 58,400 股被拒后 AI 手动试探
    5000/500/200/120 多次失败才摸到 10,000 可下（且未二分穷尽上限）——脚本应自动按
    降档序列重试到可下上限，把「摸索可下量」从 AI 手动循环里去掉。
    例：58,400（lot 20）→ 29,200 → 14,600 → 7,300 → 3,640 → 1,820 → 900 → 440 → 220 → 100 → 40 → 20。
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
    """读 skill config.json 的 risk 节（2026-08-16 修复硬编码）。

    原实现把 risk_fraction=0.02 / f_max=0.10 硬编码在自动算仓位调用里，config.json
    「修改本文件即可调参」契约对这两个参数不生效（当前数值恰好一致、属定时炸弹；
    max_leverage 一直在 calc_position_size 内部从 config 读）。统一从 config 读，
    缺失回退默认 0.02 / 0.10（与 config.example.json 默认一致）。
    """
    import json as _json
    try:
        _cfg = _json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                            "..", "config.json")))
        risk = _cfg.get("risk", {})
        return (float(risk.get("risk_fraction", 0.02)),
                float(risk.get("f_max", 0.10)))
    except Exception:
        return 0.02, 0.10


def _enforce_explicit_quantity_risk(quantity, entry_ref, stop_loss, result_base, equity,
                                    currency, lot):
    """显式传量路径的风控校验（2026-08-16 立，修复「显式传量绕过全部风控上限」）。

    原实现：f_max / max_leverage / equity 约束只在 quantity=0 自动算仓位时生效，显式
    传量时唯一护栏是券商保证金拒单（58,400 股 MINIMAX 大单场景正是此路径）——AI 误传
    超限量时实际 max_loss 可远超 f_max 上限、杠杆可远超 max_leverage。

    现校验三项（equity 为 None 时跳过 equity 相关项并 warning）：
    1. max_loss = quantity × 止损距 ≤ equity × f_max；
    2. 开仓市值 = quantity × entry_ref ≤ equity × max_leverage；
    3. quantity 合 lot（整手）。
    超限**拒绝下单**（ok:false，不只是 warning）——降档循环的被动降档不走此路径
    （降档只会更小、天然更安全）。返回 (ok, error_or_none)。
    """
    import json as _json
    risk_fraction, f_max = _risk_params_from_config()
    stop_distance = abs(entry_ref - stop_loss)
    actual_max_loss = quantity * stop_distance
    max_loss_cap = equity * f_max if equity else None
    notes = []
    if max_loss_cap is not None and actual_max_loss > max_loss_cap:
        notes.append(f"实际 max_loss {actual_max_loss:,.2f} 超过 equity×f_max 上限 "
                     f"{max_loss_cap:,.2f}（equity {equity:,.2f} × {f_max}）")
    try:
        _cfg = _json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                            "..", "config.json")))
        max_leverage = float(_cfg.get("risk", {}).get("max_leverage", 10))
    except Exception:
        max_leverage = 10.0
    if equity:
        notional = quantity * entry_ref
        notional_cap = equity * max_leverage
        if notional > notional_cap:
            notes.append(f"开仓市值 {notional:,.2f} 超过 equity×max_leverage 上限 "
                         f"{notional_cap:,.2f}（equity {equity:,.2f} × {max_leverage}）")
    if lot and quantity % lot != 0:
        notes.append(f"数量 {quantity} 不是整手（lot={lot} 的整数倍）")
    if notes:
        return False, "；".join(notes) + "。显式传量同样受风控上限约束（f_max / max_leverage），拒绝下单——请调小数量或改用 0 自动算仓位"
    return True, None


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
    # 账户选择（2026-08-12 立）：默认 None=paper 模拟账户；--account live 切实盘账户。
    # ⚠️ 实盘=真钱，AI 调用 --account live 前必须已征得用户明确同意（SKILL「auto 账户选择」双闸）。
    account = None
    if "--account" in args:
        idx = args.index("--account")
        if idx + 1 >= len(args):
            print("用法错误：--account 需要参数 live / paper", file=sys.stderr)
            sys.exit(1)
        account = args[idx + 1].lower()
        del args[idx:idx + 2]
        if account not in ("live", "paper"):
            print(json.dumps({"ok": False, "error": f"--account 必须是 live/paper，收到 '{account}'"}))
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
        quote = U.get_quote_tiger(config, symbol)
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"获取行情失败: {e}"}))
        sys.exit(1)

    if quote is None:
        print(json.dumps({"ok": False, "error": f"港股报价为空: {symbol}（检查代码/盘后）"}))
        sys.exit(1)
    current_price = quote["last"]

    # 真实费率上下文（2026-08-12）：含 shares / sec_type / market / 当月订单序号（阶梯平台费）
    # quantity>0 才用真实费率；quantity==0（自动算仓位）此时 shares 未知，价格范围检查用旧百分比口径
    # （_net_odds 按 entry/target 算每股费，不依赖 shares），算出仓位后由调用方按真实费率复核。
    fee_ctx = U.build_fee_ctx(symbol, quantity, config) if quantity > 0 else None
    in_range, range_low, range_high, odds_at_ref, odds_at_current = U.check_price_in_range(
        direction, current_price, entry_ref, stop_loss, target, symbol, fee_ctx
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
    # base_currency='HKD' 直接取 HKD 净值）、lot_size 从 get_contract 取真实每手股数。
    # risk_fraction / f_max 从 config.json 读（2026-08-16 修：原硬编码 0.02/0.10，
    # config「修改本文件即可调参」契约对这两个参数不生效）。
    lot_size = None
    equity = None
    currency = None
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

    # 止损价 tick 取整（2026-08-16 修）：原实现限价取整了、止损价原样传入（与
    # move_stop_tiger / close_position_tiger 不一致）——不合 tick 的 stop_loss 会让主单
    # + 附加腿整体被拒，降档循环再用同一个坏止损价把所有档全烧完。对齐两脚本口径。
    _tc_tick = U.new_trade_client(config)
    tick_sizes = U.get_tick_sizes_tiger(_tc_tick, symbol)
    _stop_raw = stop_loss
    stop_loss = U.round_to_tick_tiger(stop_loss, tick_sizes)
    if stop_loss != _stop_raw:
        result_base["stop_loss_adjusted"] = f"{_stop_raw} → {stop_loss}（取整到港股 tick）"

    # 显式传量的风控校验（2026-08-16 立）：显式传量此前绕过 f_max / max_leverage 全部
    # 上限（唯一护栏是券商保证金拒单）。现在与自动算仓位同一套约束：超限拒绝下单。
    if not result_base.get("auto_sized"):
        try:
            equity, currency = U.load_equity_tiger(config, base_currency='HKD')
        except Exception:
            equity, currency = None, None
        if equity is None:
            result_base["risk_check_note"] = "账户净值取不到，跳过 f_max / max_leverage 校验（仅整手校验）"
            equity = None
        _lot_for_check = lot_size if lot_size else U.get_lot_size_tiger(_tc_tick, symbol)
        ok, err = _enforce_explicit_quantity_risk(
            quantity, entry_ref, stop_loss, result_base, equity, currency, _lot_for_check)
        if not ok:
            result_base.update({"ok": False, "error": err,
                                "equity": equity, "equity_currency": currency})
            print(json.dumps(result_base, ensure_ascii=False))
            sys.exit(1)
        result_base["risk_checked"] = True

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

    # 购买力上限（2026-08-16 立，2026-08-06 00100 被拒根因闭环）：算出目标量后按单标的
    # 保证金率算可买上限，超了**下单前主动降档**（输出 capped_by_buying_power），被动降档
    # 保留兜底。可买股数上限 = buying_power × long_initial_margin ÷ 参考价（2026-08-12 实测
    # 00100 保证金率 0.75；buying_power 为 USD 口径、价格为 HKD，直接相除偏保守方向、安全）。
    _bp_shares, _bp_val, _bp_margin = U.get_buying_power_tiger(config, symbol, entry_ref, tc=_tc_tick)
    if _bp_shares is not None and quantity > _bp_shares:
        _lot_bp = lot_size if lot_size else (U.get_lot_size_tiger(_tc_tick, symbol) or 1)
        capped = max(int(_bp_shares // _lot_bp) * _lot_bp, 0)
        result_base["capped_by_buying_power"] = True
        result_base["buying_power"] = _bp_val
        result_base["margin_rate"] = _bp_margin
        result_base["buying_power_max_shares"] = _bp_shares
        result_base["target_quantity"] = quantity
        result_base["quantity"] = capped
        result_base["warning"] = (
            f"目标量 {quantity} 股超购买力上限 {_bp_shares} 股（buying_power {_bp_val:,.0f} × "
            f"保证金率 {_bp_margin} ÷ 参考价 {entry_ref}），下单前主动降档到 {capped} 股——"
            f"2026-08-06 00100 大单被拒根因闭环，减少被动拒单")
        if capped <= 0:
            result_base.update({"ok": False, "error": "购买力上限降档后为 0（buying_power 不足以开 1 手）"})
            print(json.dumps(result_base, ensure_ascii=False))
            sys.exit(1)
        quantity = capped

    # 主单（LMT/MKT）+ 附加止损腿 OrderLeg('LOSS')（一次提交；券商语义 2026-08-03 实测：
    # 附加腿落成独立 STP 单、主单成交后进入 HELD 监控，可独立撤销）
    #
    # 失败自动降档重试（2026-08-11 立，00100 待办）：目标量被拒（提交抛异常 / 回查 Invalid）
    # 时按降档序列逐次减半重试到可下上限，避免 8-06 MINIMAX 58,400 被拒后 AI 手动乱试；
    # 每次失败输出 reason（cross-trading 等具体原因不再丢失）。不降档的失败：
    # ① cross-trading（账户已有同标的未成交挂单，新单与其交叉成交被拒，2026-08-11 用户纠正：
    #    与持仓止损单无关——开仓=空仓建仓）——与量无关，降档无意义，须改单或撤单再重挂；
    # ② 挂起超时未成交——可能是限价/盘口问题而非被拒，撤单退出由 AI 决策；
    # ③ 提交超时模糊失败（2026-08-16 立）——请求可能已达券商，继续降档=再下一单、
    #    真实重复开仓路径，立即停止并提示先查当日订单确认；
    # ④ 部分成交超时（PartiallyFilled，2026-08-16 立）——已成交部分建了仓，再降档会
    #    叠仓；停止循环、读实际成交量如实上报。
    lot = lot_size if lot_size else (U.get_lot_size_tiger(U.new_trade_client(config), symbol) or 1)
    attempts = _downscale_sequence(quantity, lot)
    failures = []
    order_id = None
    fill_price = None
    status = ""
    filled = False   # 2026-08-16 修：循环前初始化——全部档位 submit 抛异常时循环耗尽后
    # `if not filled:` 不再 UnboundLocalError 崩溃（traceback 代替干净 JSON、AI 拿不到失败详情）
    part_filled_qty = None
    for qty in attempts:
        # 重复下单防抖（2026-08-16 立）：降档重试前查同标的当日已有无活动开仓方向委托单
        # ——上一档「提交超时」可能实际已在场，此时再降档下单=双倍持仓。
        try:
            has_active, active_ids = U.has_active_open_order_tiger(config, symbol, side_str)
            if has_active:
                result_base.update({"ok": False,
                                    "error": f"重复下单防抖：{symbol} 已有活动开仓方向委托单 {active_ids}，拒绝继续降档下单——先查当日订单确认状态",
                                    "failures": failures, "blocked_by": "active_open_order_dedup"})
                print(json.dumps(result_base, ensure_ascii=False))
                sys.exit(1)
        except Exception as de:
            failures.append({"qty": qty, "status": "dedup_check_failed", "reason": str(de),
                             "note": "防抖检查查询失败，为避免重复下单风险停止降档"})
            break
        try:
            order_id = U.submit_order_with_stop_tiger(
                config, symbol, side_str, qty, lo_price, stop_loss,
                order_type=order_type.upper())
        except Exception as e:
            msg = str(e)
            if "模糊失败" in msg or U._is_ambiguous_timeout_error(e):
                failures.append({"qty": qty, "status": "submit_timeout_ambiguous", "reason": msg,
                                 "hint": "请求可能已达券商，禁止降档续下——先查当日订单确认是否已成交，未确认前不得再下单"})
                break  # 模糊失败：可能已下单成功，降档=双倍持仓
            if "cross" in msg.lower() and "pending" in msg.lower():
                failures.append({"qty": qty, "status": "submit_exception", "reason": msg,
                                 "hint": "账户已有同标的未成交委托单（之前下单残留），改单或撤销该挂单后再重新挂单"})
                break  # cross-trading 与量无关，降档无意义
            failures.append({"qty": qty, "status": "submit_exception", "reason": msg})
            continue
        filled, fill_price, status, reason = U.check_order_filled_tiger(config, order_id, timeout=8)
        if filled:
            break
        if status == "PartiallyFilled":
            # 部分成交超时（2026-08-16 修：原 "Filled" in status 把 PartiallyFilled 当
            # 全额成交）：读实际成交量如实上报，已成交部分已建仓、不降档叠仓。
            part_filled_qty = U.get_order_filled_qty_tiger(config, order_id)
            failures.append({"qty": qty, "status": status,
                             "filled_qty": part_filled_qty,
                             "reason": reason or "部分成交未在超时内全额成交——已成交部分已建仓，请按 filled_qty 复核持仓，勿重复开仓"})
            break
        if "Invalid" not in status:
            # 挂起超时 / 轮询异常 / 其它非被拒状态：撤单退出（不降档，让 AI 决策）
            try:
                U.cancel_order_tiger(config, order_id)
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
            # 部分成交：如实上报实际成交量（不是失败到零、也不是全额成功）
            result_base.update({"ok": True, "part_filled": True, "quantity": part_filled_qty,
                                "order_id": order_id,
                                "fill_price": fill_price,
                                "warning": f"部分成交 {part_filled_qty}/{quantity} 股（超时未全成）——已按实际成交量上报，请复核持仓与残留附加止损腿",
                                "failures": failures, "main_status": status,
                                "method": f"{order_type}+attached_stop"})
            print(json.dumps(result_base, ensure_ascii=False))
            sys.exit(0)
        result_base.update({"ok": False, "error": f"开仓全部失败（尝试 {len(failures)} 档）", "failures": failures})
        print(json.dumps(result_base, ensure_ascii=False))
        sys.exit(1)

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
        fill_price = lo_price if lo_price else current_price
        fill_src = "lo_price（成交均价缺失兜底）" if lo_price else "current_price（成交均价缺失兜底）"
    result_base.update({"ok": True, "fill_price": fill_price, "fill_price_source": fill_src,
                        "method": f"{order_type}+attached_stop",
                        "stop": f"attached LOSS @ {stop_loss}", "main_status": status})
    print(json.dumps(result_base, ensure_ascii=False))


if __name__ == "__main__":
    main()
