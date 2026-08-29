#!/usr/bin/env python3
"""港股平仓动作脚本（老虎证券模拟账户，港股默认账户）。

**唯一平仓机制：「止损/止盈触发价交替逼近现价」循环（2026-08-23 用户立）——平仓不下
普通订单。** 用户口径：情况不对立马跑路，市价成交才做得到；而平仓的市价成交靠**止损单
触发**——直接用止损单、把触发价设为现价即可；**没有止损单的情况就再设置一个止损单
并把止损价设为现价**。循环四步：

  ① 把止损单触发价改为现价 → 检查是否触发平仓成功（**通常第一次改止损价就能触发**，
     市价成交、立即离场）；
  ② 没触发（小概率：券商要求严格穿越 / 价格瞬时反向离开）→ 把止盈单触发价也设为现价、
     让价格触发止盈单平仓 → 再检查；
  ③ 还没触发（更小概率）→ 再改止损单触发价 → **不断循环，止损价与止盈价不断接近来
     逼迫平仓**；
  ④ 止损单失效（瞬时插针触发但平仓失败 → 无止损单状态）或开仓时就未带止损单 →
     **立刻重新设置止损单、触发价 = 现价**（等于立刻触发市价平仓）。

安全设计：每轮先复查持仓（条件单已实际成交而轮询滞后时按已平收尾，杜绝超卖反向开仓
——2026-08-07 MINIMAX 事故修复保留）；循环上限 8 轮，耗尽未平如实上报、**不撤条件单**
（止损保护必须在场）；平仓成交后撤全部残留止损单 + 止盈单（防日后零持仓触发反向开仓）。

沿革：2026-08-05「modify 止损触发价」单步方案（消除撤单 race）→ 2026-08-23 扩为交替
逼近循环（止盈单入列 + 失效重设 + 无止损单补设）。同日曾短暂改过「直发对价限价单 /
fallback 限价平仓」路径，按用户当日修订口径**删除**——平仓一律走条件单触发，不下普通订单。

用法：
  python3 close_position_tiger.py [symbol] [direction] [quantity]
    不给参数 = 一键平账户唯一港股持仓
    HK.02800 / HK.02800 long 500（显式）
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import trade_utils_tiger as U
import fee_schedule as FS   # 净 max_loss 过程指标用（2026-08-28 净口径）


def _is_stop_order(o):
    """止损单判定（STP/STOP/TRAIL/LOSS 附加腿，2026-08-16 对齐美股版口径；
    2026-08-23 增补排除止盈单——PROFIT 腿落成单不能被止损路径误抓误撤）。"""
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
    """止盈单判定（2026-08-23 立，口径同 move_target_tiger）：非止损 + PROFIT 腿标记 /
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
    skip_next = False  # 跳过 --mode 后跟的值
    expect_account = False  # 捕获 --account 后跟的值（空格形式，2026-08-16 修复：此前只跳过未赋值）
    # 2026-08-17 修：--account 是最后一个 token（缺值）时原来静默保持 None → 悄悄用默认
    # paper 账户——传了 --account 说明想选账户、值丢了不该静默兜底，补明确报错。
    if argv and argv[-1] == "--account":
        print("用法错误：--account 需要一个值：live / paper", file=sys.stderr)
        sys.exit(1)
    for a in argv:
        if expect_account:
            account = a.lower()
            expect_account = False
            continue
        if skip_next:
            skip_next = False
            continue
        if a == "--mode":
            skip_next = True
            continue
        if a.startswith("--mode="):
            continue
        if a == "--account":
            expect_account = True
            continue
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

    if symbol and not symbol.startswith("HK."):
        print(json.dumps({"ok": False, "error": f"本脚本只处理港股（HK.xxx），收到 {symbol}"}))
        sys.exit(1)
    if account not in (None, "live", "paper"):
        print(json.dumps({"ok": False, "error": f"--account 必须是 live/paper，收到 '{account}'"}))
        sys.exit(1)
    # 实盘解锁前置闸（2026-08-21 立，实盘误开防护检查点①）：--account live 且解锁文件
    # 无效 → blocked_by:"live_locked" 结构化拒单（详见 scripts/live_unlock.py）。
    import live_unlock
    live_unlock.live_gate_for_order_scripts(account, "close_position_tiger")

    try:
        config = U.load_config(account=account)
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"老虎配置加载失败: {e}"}))
        sys.exit(1)

    # 读持仓补全 symbol / direction / quantity（一键平仓核心）
    if symbol is None or direction is None or quantity is None:
        pos = U.get_open_position_tiger(config, symbol)
        if pos is None:
            hint = "账户无港股持仓" if symbol is None else f"未找到港股 {symbol} 持仓"
            print(json.dumps({"ok": False, "error": hint}, ensure_ascii=False))
            sys.exit(1)
        if symbol is None:
            symbol = pos["symbol"]
        if direction is None:
            direction = pos["side"]
        if quantity is None:
            quantity = pos["quantity"]
    else:
        # 显式传参路径复核持仓（2026-08-16 立，修复「凭空开多」）：原实现不读持仓、不校验
        # 方向匹配——direction=short 而账户实际无空仓时，按错误方向平仓会凭空反向开仓
        # （一键分支有持仓复核、显式分支没有，不对称）。现与一键分支同一道闸：持仓不存在
        # 或方向不匹配即拒绝；quantity 超持仓也拒绝（超量平仓会反向开仓）。
        pos = U.get_open_position_tiger(config, symbol)
        if pos is None:
            print(json.dumps({"ok": False, "error": f"未找到港股 {symbol} 持仓——显式传参平仓拒绝执行（防止凭空反向开仓）"},
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
                f"平仓量 {quantity} 超过持仓量 {pos['quantity']}——超量平仓会反向开仓，拒绝执行")},
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

    result_base = {"action": "close_position_tiger", "market": "HK", "symbol": symbol,
                   "direction": direction, "quantity": quantity, "close_side": close_side}

    quote = U.get_quote_tiger(config, symbol)
    if quote is None:
        result_base.update({"ok": False, "error": f"港股报价为空: {symbol}"})
        print(json.dumps(result_base, ensure_ascii=False))
        sys.exit(1)
    current_price = quote["last"]

    tc = U.new_trade_client(config)
    target_sym = U.to_tiger_symbol(symbol)

    # 查活动止损单 / 止盈单（该标的、各自口径、非终结状态）——2026-08-23 起开仓带
    # LOSS+PROFIT 双腿（BRACKETS），两类单都在场，平仓循环两者都用得上。
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
    # 多余兄弟止损/止盈单（历史残留）记录下来，平仓成交后统一撤（_cancel_residual_stops）
    profit_o = profits[0] if profits else None

    # 平仓过程指标素材（2026-08-05 立）：开仓价 = 持仓 cost_price、止损距 = |开仓价 − 活动
    # 止损触发价|（M = 仓位 × 止损距、毛值，与复盘口径一致）。平仓成交后由
    # _attach_process_metrics 原生补记 mfe_R / mae_R（复盘过程指标直接读、不必回拉历史 K）。
    entry_price = None
    stop_dist = None
    try:
        pos = U.get_open_position_tiger(config, symbol)
        if pos and pos.get("cost_price"):
            entry_price = float(pos["cost_price"])
            if stp is not None:
                aux = float(getattr(stp, "aux_price", 0) or 0)
                if aux > 0:
                    stop_dist = abs(entry_price - aux)
    except Exception:
        pass

    # ===== 唯一平仓机制：「止损/止盈触发价交替逼近现价」循环（2026-08-23 用户立）=====
    # 用户口径（当日修订）：**平仓不下普通订单**——情况不对立马跑路靠止损单触发后的市价
    # 成交；直接用止损单、把触发价设为现价即可；没有止损单的情况就再设置一个止损单并
    # 把止损价设为现价。循环逻辑：
    #   ① 先把止损单触发价改为现价 → 检查是否触发平仓成功（通常第一次就触发）；
    #   ② 没触发（小概率）→ 把止盈单触发价也设为现价、让价格触发止盈单平仓 → 再检查；
    #   ③ 还没触发（更小概率）→ 再改止损单触发价为现价 → 不断循环，止损价与止盈价
    #      不断接近来逼迫平仓；
    #   ④ 止损单失效（瞬时插针触发但平仓失败 → 处于无止损单状态）或开仓时就无止损单
    #      → **立刻重新设置止损单、触发价 = 现价**（等于立刻触发市价平仓）。
    # 每轮循环先复查持仓——任一条件单已实际成交（轮询滞后误判）则持仓已平，直接按已平收尾。
    result_base["path"] = "converge_loop（止损/止盈触发价交替逼近现价）"
    tick_sizes = U.get_tick_sizes_tiger(tc, symbol)
    close_rounds = []
    max_rounds = 8   # 循环上限（每轮含 modify/下新 + 12s 回查；8 轮 ≈ 2 分钟，超出如实上报）
    filled = False
    fill_price = None
    fill_order_id = None
    fill_status = ""
    for rnd in range(1, max_rounds + 1):
        # 每轮先复查持仓：上一步的条件单可能已实际成交（轮询滞后），已平即收尾
        try:
            pos_now = U.get_open_position_tiger(config, symbol)
        except Exception:
            pos_now = None
        if pos_now is None or abs(pos_now.get("quantity", 0)) == 0:
            # 持仓已空：条件单已触发成交（轮询误判未触发）——按已平收尾
            filled = True
            fill_price = fill_price or current_price
            close_rounds.append({"round": rnd, "event": "持仓复查为空——条件单已实际触发成交（轮询滞后误判）"})
            break

        # 刷新现价（逼近目标必须是最新价）
        quote_now = U.get_quote_tiger(config, symbol)
        if quote_now is None or quote_now.get("last") is None:
            close_rounds.append({"round": rnd, "event": "行情取不到，跳过本轮逼近（下轮再试）"})
            continue
        px_now = float(quote_now["last"])
        trig_now = U.round_to_tick_tiger(px_now, tick_sizes)

        # 该轮逼近对象：奇数轮 = 止损单、偶数轮 = 止盈单（交替）
        want_stop = (rnd % 2 == 1)
        # 止损单失效 / 无止损单（用户指令 ④）：止损轮发现无活动止损单（插针触发但平仓
        # 失败后券商已终结该单，或开仓时就未带）→ 立刻重新下止损单、触发价 = 现价
        # （等于立刻触发市价平仓——这是「没有止损单」场景的唯一处理方式）
        active_stps, active_profits = _find_active_orders()
        if want_stop and not active_stps:
            close_rounds.append({"round": rnd, "event": f"无活动止损单 → 立刻重设止损 STP 触发价={trig_now}"})
            try:
                stp_side = "Sell" if direction == "long" else "Buy"
                new_stp_id = U.submit_stop_order_tiger(config, symbol, stp_side, quantity, trig_now)
                filled, fill_price, fill_status, _r = U.check_order_filled_tiger(config, new_stp_id, timeout=12)
                fill_order_id = new_stp_id
                if filled:
                    break
            except Exception as e:
                close_rounds.append({"round": rnd, "event": f"重设止损单异常: {e}"})
            continue
        if (not want_stop) and not active_profits:
            # 止盈轮无活动止盈单（已触发/未带止盈开仓）→ 下一轮回止损轮
            close_rounds.append({"round": rnd, "event": "无活动止盈单（未带止盈开仓或已失效），切回止损轮"})
            continue

        target_o = (active_stps[0] if active_stps else None) if want_stop else (active_profits[0] if active_profits else None)
        if target_o is None:
            continue
        t_id = getattr(target_o, "id", None)
        kind = "止损" if want_stop else "止盈"
        try:
            # 止损单价格字段 aux_price；止盈单（PROFIT 腿落成 LMT 形态）limit_price——两个都传
            tc.modify_order(target_o, aux_price=trig_now, limit_price=trig_now)
        except Exception as e:
            close_rounds.append({"round": rnd, "event": f"modify {kind}单异常: {e}"})
            continue
        close_rounds.append({"round": rnd, "event": f"modify {kind}单触发价 → {trig_now}（现价 {px_now}）"})

        # 等该条件单触发成交（触发价=现价，通常立即触发）
        filled_r, fill_price_r, status_r, _reason = U.check_order_filled_tiger(config, t_id, timeout=12)
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
        # 平仓成交后撤残留止损单 + 止盈单（2026-08-16 立残留止损清理；2026-08-23 扩展：
        # BRACKETS 一边成交另一边通常自动作废，此处复查兜底——防任何残留条件单日后
        # 零持仓触发反向开仓）
        _cancel_residual_stops(config, symbol, result_base, exclude=fill_order_id)
        _cancel_residual_profits(config, symbol, result_base, exclude=fill_order_id)
        U.attach_net_pnl_app(result_base, config, target_sym, direction, quantity,
                             fill_price, close_order_id=fill_order_id, entry_price=entry_price)
        _attach_process_metrics(result_base, config, symbol, direction, entry_price, stop_dist,
                                quantity=quantity)
        print(json.dumps(result_base, ensure_ascii=False))
        sys.exit(0)
    else:
        # 循环耗尽仍未平：不撤条件单（止损保护必须在场），如实上报由 AI 决策
        result_base.update({"ok": False,
                            "error": f"交替逼近循环 {max_rounds} 轮未成交（价格快速离开或券商要求穿越），"
                                     f"止损/止盈条件单仍在场——请 AI 决策重跑平仓或人工处理",
                            "rounds": close_rounds})
        print(json.dumps(result_base, ensure_ascii=False))
        sys.exit(1)


def _cancel_residual_stops(config, symbol, result_base, exclude=None):
    """平仓成交后复查并撤掉该标的全部残留止损单（2026-08-16 立）。

    背景：主路径只处理第一个活动止损单，若存在多个（兄弟止损单），其余不撤——残留单
    日后零持仓触发时将反向开仓（与 2026-08-03 反向开空事故同类）。move_stop 已有 ≥2
    撤多余的清理、close 没有；且查询时点（平仓前）与撤单时点（平仓后）之间单据状态会
    变化，平仓后须复查。撤单失败不阻断输出（warning 提示手动处理）。"""
    try:
        n, ids = U.cancel_all_stop_orders_tiger(config, symbol, exclude_order_id=exclude)
        if n > 0:
            result_base["residual_stop_orders_cancelled"] = n
            result_base["cancelled_order_ids"] = ids
    except Exception as e:
        result_base["residual_stop_cancel_warning"] = f"平仓后撤残留止损单失败（需手动检查，防日后反向开仓）: {e}"


def _cancel_residual_profits(config, symbol, result_base, exclude=None):
    """平仓成交后复查并撤掉该标的全部残留止盈单（2026-08-23 立，双腿开仓配套）。

    BRACKETS 括号订单理论上一边成交另一边自动作废，但「自动作废」到账有时滞、且若平仓
    走的是主动循环（modify 触发价逼近）另一边不一定被联动——零持仓后残留止盈单触发会
    反向开仓（与残留止损同理）。撤单失败不阻断输出（warning 提示手动处理）。"""
    try:
        tc = U.new_trade_client(config)
        target_sym = U.to_tiger_symbol(symbol)
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
                U.cancel_order_tiger(config, oid)
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
    净 max_loss = 止损距 × 仓位 + 开仓边费 + 止损价平仓边费（费按 entry/stop 各自价格算）；
    quantity 缺失时退旧毛口径（极值幅度 ÷ 毛止损距）并标注 process_metric_basis。
    """
    if not entry_price or not stop_dist or stop_dist <= 0:
        return
    try:
        extremes = U.calc_position_extremes_tiger(symbol, mode=U.parse_mode())
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
        sec_type = U._sec_type_of(symbol)
        fee_open = FS.fee_per_side("HK", sec_type, entry_price * quantity, shares=quantity)
        fee_stop = FS.fee_per_side("HK", sec_type, abs(stop_price * quantity), shares=quantity)
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
