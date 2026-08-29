#!/usr/bin/env python3
"""美股平仓动作脚本（老虎证券模拟账户，美股默认账户）。

**唯一平仓机制：「止损/止盈触发价交替逼近现价」循环（2026-08-23 用户立，同港股版）
——平仓不下普通订单。** 用户口径：情况不对立马跑路靠止损单触发后的市价成交；直接用
止损单、把触发价设为现价；**没有止损单就再设一个止损单、止损价 = 现价**。循环四步：
① 止损触发价 → 现价（通常第一次即触发）② 没触发则止盈触发价 → 现价 ③ 再没触发回到
① 循环逼近（止损价与止盈价不断接近逼迫平仓）④ 止损单失效或无止损单 → 立刻重设止损
STP 触发价 = 现价。每轮先复查持仓（轮询滞后误判时按已平收尾、防超卖反向开仓）；modify
带 outside_rth=True（美股盘前可交易窗口）。

_is_stop_order 含 TRAIL（跟踪止损）——吸收 2026-08-05 中芯残留事故教训（cancel 只撤 STP/LOSS
漏 TRAIL、致 salable=0 平仓被拒）。

✅ 实测状态（2026-08-05 美股盘中，当时还是单步 modify 方案）：下单链路已 paper 端到端实测
通过（SPY 2 股平多：modify 触发价 771.71→773.42、止损单触发 Sell MO Filled @773.44、持仓归零、
无残留止损单；过程指标 mfe_R/mae_R 正常输出）。交替逼近循环 + 双腿开仓后的全链路待下个
交易日 paper 实测（TODO T116）。行情走富途 OpenD 单源（老虎美股无行情权限）。

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
    """止损单判定（含 STP/STOP/TRAIL/LOSS 附加腿；2026-08-23 增补排除止盈单——PROFIT 腿
    落成单不能被止损路径误抓误撤）。"""
    otype = getattr(o, "order_type", None)
    otype_val = otype.value if hasattr(otype, "value") else str(otype)
    upper = str(otype_val).upper()
    legs = getattr(o, "order_legs", None) or []
    if any(str(getattr(leg, "leg_type", "")).upper() == "PROFIT" for leg in legs):
        return False
    attr = str(getattr(o, "attr_desc", "") or "")
    if "止盈" in attr:
        return False
    return ("STP" in upper or "STOP" in upper or "TRAIL" in upper
            or any(str(getattr(leg, "leg_type", "")).upper() == "LOSS" for leg in legs))


def _is_profit_order(o):
    """止盈单判定（2026-08-23 立，口径同 move_target_tiger_us）：非止损 + PROFIT 腿标记 /
    attr_desc 含止盈 / 带 parent_id 的附加单落成单。供平仓「止损/止盈交替逼近」循环查活动止盈单用。"""
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


def _parse_args(argv):
    """解析位置参数并过滤 --mode（2026-08-05 立）：`--mode auto` 这类误传会把 `auto` 当
    quantity 报 ValueError 耽误平仓（2026-08-03 MU 空单教训同款）。平仓脚本不连账户 equity、
    --mode 无实际用途，直接忽略（含 `--mode` 后跟的值、`--mode=xxx` 两种写法）。
    2026-08-12 立：同时解析 --account live/paper（实盘备选账户；返回 (位置参数, account)）。
    ⚠️ --account live（实盘）= 真钱，AI 调用前须已征得用户明确同意。"""
    args = []
    account = None
    skip_next = False
    # 2026-08-17 修：--account 是最后一个 token（缺值）时原来静默保持 None → 悄悄用默认
    # paper 账户——传了 --account 说明想选账户、值丢了不该静默兜底，补明确报错。
    if argv and argv[-1] == "--account":
        print("用法错误：--account 需要一个值：live / paper", file=sys.stderr)
        sys.exit(1)
    for a in argv:
        if skip_next:
            skip_next = False
            if account == "pending":
                account = a.lower()   # --account 后跟的值（占位兑现）
            continue
        if a == "--mode":
            skip_next = True
            continue
        if a.startswith("--mode="):
            continue
        if a == "--account":
            skip_next = True
            account = "pending"   # 占位：下一 token 才是真值（2026-08-21 修——原版只跳过
            continue              # 不赋值，--account live 落 None = 静默走模拟盘，live 闸全失效）
        if a.startswith("--account="):
            account = a.split("=", 1)[1].lower()
            continue
        args.append(a)
    return args, account


def main():
    argv, account = _parse_args(sys.argv[1:])
    symbol = argv[0] if len(argv) > 0 else None
    direction = argv[1] if len(argv) > 1 else None
    quantity = int(float(argv[2])) if len(argv) > 2 else None

    if symbol and not symbol.startswith("US."):
        print(json.dumps({"ok": False, "error": f"本脚本只处理美股（US.xxx），收到 {symbol}"}))
        sys.exit(1)
    if account not in (None, "live", "paper"):
        print(json.dumps({"ok": False, "error": f"--account 必须是 live/paper，收到 '{account}'"}))
        sys.exit(1)
    # 实盘解锁前置闸（2026-08-21 立，实盘误开防护检查点①）：--account live 且解锁文件
    # 无效 → blocked_by:"live_locked" 结构化拒单（详见 scripts/live_unlock.py）。
    import live_unlock
    live_unlock.live_gate_for_order_scripts(account, "close_position_tiger_us")

    try:
        config = U.load_config(account=account)
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
    else:
        # 显式传参路径复核持仓（2026-08-16 立，同港股版：原不读持仓不校验方向——
        # direction=short 而无空仓时 close_side=Buy 的 MO 凭空开多仓）。持仓不存在 /
        # 方向不匹配 / 超量均拒绝。
        pos = U.get_open_position_us(config, symbol)
        if pos is None:
            print(json.dumps({"ok": False, "error": f"未找到美股 {symbol} 持仓——显式传参平仓拒绝执行（防止凭空反向开仓）"},
                             ensure_ascii=False))
            sys.exit(1)
        if pos["side"] != direction:
            print(json.dumps({"ok": False, "error": (
                f"direction={direction} 与实际持仓方向 {pos['side']} 不符（{symbol} {pos['quantity']} 股）"
                f"——按错误方向平仓会凭空反向开仓，拒绝执行")},
                ensure_ascii=False))
            sys.exit(1)
        if quantity > pos["quantity"]:
            print(json.dumps({"ok": False, "error": (
                f"平仓量 {quantity} 超过持仓量 {pos['quantity']}——超量 MO 会反向开仓，拒绝执行")},
                ensure_ascii=False))
            sys.exit(1)
        if quantity < pos["quantity"]:
            print(json.dumps({"ok": True, "warning": (
                f"平仓量 {quantity} < 持仓量 {pos['quantity']}，平仓后将有剩余持仓（非全平）")},
                ensure_ascii=False))

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

    # 查活动止损单 / 止盈单（2026-08-23 起开仓带 LOSS+PROFIT 双腿，两类单都在场）
    def _find_active_orders():
        stps, profits = [], []
        for o in (tc.get_orders() or []):
            sym = str(getattr(getattr(o, "contract", None), "symbol", ""))
            if sym != target_sym:
                continue
            if _status_str(o) in _TERMINAL:
                continue
            if _is_stop_order(o):
                stps.append(o)
            elif _is_profit_order(o):
                profits.append(o)
        return stps, profits

    stps, profits = _find_active_orders()
    stp = stps[0] if stps else None
    profit_o = profits[0] if profits else None

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

    # ===== 唯一平仓机制：「止损/止盈触发价交替逼近现价」循环（2026-08-23 用户立，同港股版）=====
    # 平仓不下普通订单（用户当日修订口径）：一律靠止损/止盈条件单触发后的市价成交离场。
    # ① 止损触发价→现价（通常第一次即触发）② 没触发则止盈触发价→现价 ③ 再没触发回到 ①
    # 循环逼近；④ 止损单失效（插针触发但平仓失败）或开仓时就无止损单 → 立刻重设止损单、
    # 触发价=现价（这是「没有止损单」场景的唯一处理方式）。
    result_base["path"] = "converge_loop（止损/止盈触发价交替逼近现价）"
    close_rounds = []
    max_rounds = 8
    filled = False
    fill_price = None
    fill_order_id = None
    fill_status = ""
    for rnd in range(1, max_rounds + 1):
        try:
            pos_now = U.get_open_position_us(config, symbol)
        except Exception:
            pos_now = None
        if pos_now is None or abs(pos_now.get("quantity", 0)) == 0:
            filled = True
            fill_price = fill_price or current_price
            close_rounds.append({"round": rnd, "event": "持仓复查为空——条件单已实际触发成交（轮询滞后误判）"})
            break

        quote_now = U.get_quote_us(config, symbol)
        if quote_now is None or quote_now.get("last") is None:
            close_rounds.append({"round": rnd, "event": "行情取不到，跳过本轮逼近（下轮再试）"})
            continue
        px_now = float(quote_now["last"])
        trig_now = U.round_to_tick_us(px_now)

        want_stop = (rnd % 2 == 1)
        active_stps, active_profits = _find_active_orders()
        if want_stop and not active_stps:
            close_rounds.append({"round": rnd, "event": f"止损单失效（无活动止损单）→ 立刻重设止损 STP 触发价={trig_now}"})
            try:
                stp_side = "Sell" if direction == "long" else "Buy"
                new_stp_id = U.submit_stop_order_us(config, symbol, stp_side, quantity, trig_now)
                filled, fill_price, fill_status, _r = U.check_order_filled_us(config, new_stp_id, timeout=12)
                fill_order_id = new_stp_id
                if filled:
                    break
            except Exception as e:
                close_rounds.append({"round": rnd, "event": f"重设止损单异常: {e}"})
            continue
        if (not want_stop) and not active_profits:
            close_rounds.append({"round": rnd, "event": "无活动止盈单（未带止盈开仓或已失效），切回止损轮"})
            continue

        target_o = (active_stps[0] if active_stps else None) if want_stop else (active_profits[0] if active_profits else None)
        if target_o is None:
            continue
        t_id = getattr(target_o, "id", None)
        kind = "止损" if want_stop else "止盈"
        try:
            # outside_rth=True（2026-08-18 美股盘前可交易，同原 modify 口径）
            tc.modify_order(target_o, aux_price=trig_now, limit_price=trig_now, outside_rth=True)
        except Exception as e:
            close_rounds.append({"round": rnd, "event": f"modify {kind}单异常: {e}"})
            continue
        close_rounds.append({"round": rnd, "event": f"modify {kind}单触发价 → {trig_now}（现价 {px_now}）"})

        filled_r, fill_price_r, status_r, _reason = U.check_order_filled_us(config, t_id, timeout=12)
        if filled_r:
            filled = True
            fill_price = fill_price_r
            fill_status = status_r
            fill_order_id = t_id
            break

    if filled:
        fill_src = "avg_fill_price"
        if fill_price is None:
            fill_price = current_price
            fill_src = "current_price（成交均价缺失兜底）"
        result_base.update({"ok": True, "order_id": fill_order_id, "fill_price": fill_price,
                            "fill_price_source": fill_src,
                            "method": "converge_loop（止损/止盈触发价交替逼近现价平仓）",
                            "main_status": fill_status, "rounds": close_rounds})
        _cancel_residual_stops(config, symbol, result_base, exclude=fill_order_id)
        _cancel_residual_profits(config, symbol, result_base, exclude=fill_order_id)
        U.attach_net_pnl_app(result_base, config, target_sym, direction, quantity,
                             fill_price, close_order_id=fill_order_id, entry_price=entry_price)
        _attach_process_metrics(result_base, config, symbol, direction, entry_price, stop_dist,
                                quantity=quantity)
        print(json.dumps(result_base, ensure_ascii=False))
        sys.exit(0)
    else:
        result_base.update({"ok": False,
                            "error": f"交替逼近循环 {max_rounds} 轮未成交（价格快速离开或券商要求穿越），"
                                     f"止损/止盈条件单仍在场——请 AI 决策重跑平仓或人工处理",
                            "rounds": close_rounds})
        print(json.dumps(result_base, ensure_ascii=False))
        sys.exit(1)



def _cancel_residual_stops(config, symbol, result_base, exclude=None):
    """平仓成交后复查并撤掉该美股标的全部残留止损单（2026-08-16 立，同港股版）。
    撤单失败不阻断输出（warning 提示手动处理）。"""
    try:
        n, ids = U.cancel_all_stop_orders_us(config, symbol, exclude_order_id=exclude)
        if n > 0:
            result_base["residual_stop_orders_cancelled"] = n
            result_base["cancelled_order_ids"] = ids
    except Exception as e:
        result_base["residual_stop_cancel_warning"] = f"平仓后撤残留止损单失败（需手动检查，防日后反向开仓）: {e}"


def _cancel_residual_profits(config, symbol, result_base, exclude=None):
    """平仓成交后复查并撤掉该美股标的全部残留止盈单（2026-08-23 立，双腿开仓配套，
    同港股版：BRACKETS 一边成交另一边理论自动作废，此处复查兜底——零持仓后残留止盈单
    触发会反向开仓）。撤单失败不阻断输出。"""
    try:
        tc = U.new_trade_client(config)
        target_sym = U.to_tiger_symbol_us(symbol)
        cancelled = []
        for o in (tc.get_orders() or []):
            sym = str(getattr(getattr(o, "contract", None), "symbol", ""))
            if sym != target_sym or _status_str(o) in _TERMINAL:
                continue
            if not _is_profit_order(o):
                continue
            oid = getattr(o, "id", None)
            if exclude is not None and str(oid) == str(exclude):
                continue
            try:
                U.cancel_order_us(config, oid)
                cancelled.append(oid)
            except Exception:
                pass
        if cancelled:
            result_base["residual_profit_orders_cancelled"] = len(cancelled)
            result_base.setdefault("cancelled_order_ids", []).extend(cancelled)
    except Exception as e:
        result_base["residual_profit_cancel_warning"] = f"平仓后撤残留止盈单失败（需手动检查，防日后反向开仓）: {e}"


def _attach_process_metrics(result_base, config, symbol, direction, entry_price, stop_dist,
                            quantity=None):
    """平仓成交后原生补记过程指标 mfe_R / mae_R（2026-08-05 立，review-and-evaluation.md 数据约束
    方案 b 落地）：从盯盘 log 取该标的当日采样极值近似持仓期间 high/low（日内策略当天开当天平，
    当日 log 近似持仓期间；log 由 monitor_segment 按市场交易日 + 模式命名）。无 log / 缺
    entry / stop_dist 则不加字段（复盘按缺失处理、跳过过程指标）。

    口径与 review.py 一致（2026-08-28 起 review 分母改净 max_loss，本处按净口径对齐）：MFE_R =
    有利方向最大幅度 × 仓位 ÷ 净 max_loss（正）、MAE_R = −不利方向最大幅度 × 仓位 ÷ 净
    max_loss（负，越接近 0 防守越好）；做多 fav = high − entry、做空 fav = entry − low。
    净 max_loss = 止损距 × 仓位 + 开仓边费 + 止损价平仓边费（美股按股计费、shares 传入）；
    quantity 缺失时退旧毛口径（极值幅度 ÷ 毛止损距）并标注 process_metric_basis。
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
        stop_price = entry_price - stop_dist
    else:
        fav, adv = entry_price - raw_low, raw_high - entry_price
        stop_price = entry_price + stop_dist
    metrics = {"entry_price": round(entry_price, 4),
               "raw_high": raw_high, "raw_low": raw_low}
    if quantity:
        import fee_schedule as FS
        sec_type = U._sec_type_of(symbol)
        fee_open = FS.fee_per_side("US", sec_type, entry_price * quantity, shares=quantity)
        fee_stop = FS.fee_per_side("US", sec_type, abs(stop_price * quantity), shares=quantity)
        m_net = stop_dist * quantity + fee_open + fee_stop
        metrics.update({
            "mfe_R": round(max(fav, 0.0) * quantity / m_net, 3),
            "mae_R": round(-max(adv, 0.0) * quantity / m_net, 3),
            "process_metric_basis": "net（分母净 max_loss = 止损距×仓位+开仓费+止损价平仓费，2026-08-28 口径）",
        })
    else:
        metrics.update({
            "mfe_R": round(max(fav, 0.0) / stop_dist, 3),
            "mae_R": round(-max(adv, 0.0) / stop_dist, 3),
            "process_metric_basis": "gross（缺 quantity，退毛止损距口径——复盘 CSV 回算以 review.py 为准）",
        })
    result_base.update(metrics)
    result_base["process_metric_note"] = "持仓期间极值 = 当日盯盘 log 采样近似（日内当天开当天平）"



if __name__ == "__main__":
    main()
