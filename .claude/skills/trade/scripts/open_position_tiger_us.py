#!/usr/bin/env python3
"""美股开仓动作脚本（老虎证券模拟账户，美股默认账户）。

主单 LMT 限价单（2026-08-23 用户立「下单必须用限价单」，改回限价——2026-08-07 曾改市价单，
沿革见 trade_utils_tiger_us.py submit_order_with_stop_us docstring）+ **双腿附加单一次提交**：
止损腿 OrderLeg('LOSS', stop_loss) + 止盈腿 OrderLeg('PROFIT', target)（2026-08-23 用户立——
开仓必带止盈单；双腿齐发 attach_type=BRACKETS 括号订单，止损/止盈任一边触发成交、另一边
自动作废），再回查主单成交状态。开仓失败或主单未成交则撤主单、附加腿随之自动撤销，不残留
裸止损 / 裸止盈。限价 = 盘口对价（做多挂 ask 主动买；做空挂 max(bid, ask)——2026-08-25 随港股
T122 同步改，美股无提价规则但同口径无副作用），取整到美股 tick 0.01。

✅ 实测状态（2026-08-05 美股盘中，当时主单即 LMT）：下单链路已 paper 端到端
实测通过（SPY 2 股：LMT 主单 Filled @773.68 + 附加止损腿 OrderLeg('LOSS') 激活为独立 STP 监控）。
行情走富途 OpenD 单源（老虎美股无行情权限、get_stock_briefs 报 4000 permission denied）。

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
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import trade_utils_tiger_us as U
import trade_utils_tiger as T   # 2026-08-16：降档循环用 _is_ambiguous_timeout_error 判模糊失败
from trade_mutex import TradeMutex   # 多会话并行盯盘互斥（方案 A，2026-08-17，港美全局一把锁）


def _stability_minutes(symbol, price, window_pct=0.003):
    """读当日采样 log 统计「现价 ±window_pct 区间最近连续维持分钟数」（2026-08-21 立）。

    企稳定义（trading-strategy.md「右侧入场总则」）四要素之一「维持 ≥10 分钟」的机械化核验：
    从 log 末尾向前数「价格在 [price*(1-w), price*(1+w)] 内」的连续行数（futu_ws_segment 每秒一行
    ≈秒数）。log 不存在/为空返回 None（无法核验时静默，AI 自行判断）；窗口默认 ±0.3%。
    2026-08-21 实盘教训：07709 VWAP 回踩信号 40.56 在 log 里仅持续 1 秒即被当「企稳」确认——
    本函数把「一瞬贴线」变成可机械核验的数字（维持 0.x 分钟 → 警告）。
    """
    import glob
    from pathlib import Path
    log_dir = Path(__file__).resolve().parent.parent.parent.parent.parent / "tmp"
    stem = symbol.replace(".", "_")
    today8 = datetime.now().strftime("%Y%m%d")
    today_dash = datetime.now().strftime("%Y-%m-%d")
    files = sorted(
        glob.glob(str(log_dir / f"monitor_log_{stem}_{today8}_*.csv"))
        + glob.glob(str(log_dir / f"monitor_log_{stem}_{today_dash}_*.csv"))
    )
    if not files:
        return None
    lo, hi = price * (1 - window_pct), price * (1 + window_pct)
    try:
        rows = []
        with open(files[-1]) as f:
            for line in f:
                parts = line.strip().split(",")
                if len(parts) >= 3 and parts[0] != "time":
                    try:
                        rows.append(float(parts[2]))
                    except ValueError:
                        pass
        seconds = 0
        for p in reversed(rows):
            if lo <= p <= hi:
                seconds += 1
            else:
                break
        return seconds / 60.0
    except Exception:
        return None


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


def _min_net_odds_from_config():
    """读开仓净赔率门槛（2026-08-25 随港股版进 config 统一调参）。

    沿革：2026-08-14 立 2.4 → 2026-08-24 降 1.8 → 2026-08-25 用户降 1.2（与港股税闸
    2.22%→1.2% 同日双降）。config.risk.min_net_odds 为唯一权威源，港股版
    open_position_tiger.py 同读；缺失回退 1.2。美股无印花税闸（只有赔率门槛）。
    """
    import json as _json
    try:
        _cfg = _json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                            "..", "config.json")))
        return float(_cfg.get("risk", {}).get("min_net_odds", 1.2))
    except Exception:
        return 1.2


def _enforce_explicit_quantity_risk(quantity, entry_ref, stop_loss, equity, symbol=None):
    """显式传量路径的风控校验（2026-08-16 立，同港股版：原显式传量绕过 f_max /
    max_leverage 全部上限、唯一护栏是券商保证金拒单）。max_loss 2026-08-28 起净口径
    （止损距×量 + 开仓边费 + 止损价平仓边费，symbol 缺失时退毛值）。超限拒绝下单。
    返回 (ok, error_or_none)。"""
    import json as _json
    _, f_max = _risk_params_from_config()
    stop_distance = abs(entry_ref - stop_loss)
    if symbol:
        actual_max_loss = U.net_max_loss("US", U._sec_type_of(symbol),
                                         quantity, entry_ref, stop_loss)
    else:
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
    # 实盘解锁前置闸（2026-08-21 立，实盘误开防护检查点①）：--account live 且解锁文件
    # 无效 → blocked_by:"live_locked" 结构化拒单（详见 scripts/live_unlock.py）。
    import live_unlock
    live_unlock.live_gate_for_order_scripts(account, "open_position_tiger_us")

    if not symbol.startswith("US."):
        print(json.dumps({"ok": False, "error": f"本脚本只处理美股（US.xxx），收到 {symbol}"}))
        sys.exit(1)
    # 开仓时间闸（2026-08-18 立）：距美东 16:00 收盘 >5 分钟放行、≤5 分钟或盘外拒单。
    # 「距停盯 >5 分钟就仍可开仓」规则的工具级执行点（同港股脚本，函数在
    # trade_utils_tiger.py，本脚本 import 为 T）。
    _gate_ok, _gate_msg = T.check_open_time_gate("US")
    print(f"⏰ 开仓时间闸：{_gate_msg}")
    if not _gate_ok:
        print(json.dumps({"ok": False, "blocked_by": "time_gate", "error": _gate_msg}))
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

    # ===== 自动算仓位（quantity=0）——2026-08-28 前移到价格范围/赔率校验之前 =====
    # 与港股版同构（用户裁定「先算仓位再校验」）：股数未定时净赔率/净 max_loss 只能用
    # 近似口径，估得偏乐观会把真实不达标的单放行；先定档再校验，两条路径统一真实费率+
    # 净分母。equity 取老虎账户 USD 净值、美股 lot_size 默认 1（可零股）、选档用 entry_ref
    # 口径，费与净 max_loss 均为 2026-08-28 净口径。
    lot_size = None
    if quantity == 0:
        tc = U.new_trade_client(config)
        equity, currency = U.load_equity_us(config)
        if equity is None:
            print(json.dumps({"ok": False, "error": "老虎账户净值取不到（未开通交易/资产权限？），无法自动算仓位"},
                             ensure_ascii=False))
            sys.exit(1)
        lot_size = U.get_lot_size_us(tc, symbol)
        if not lot_size:
            lot_size = 1  # 美股默认 1 股/手
        stop_distance = abs(entry_ref - stop_loss)
        risk_fraction, f_max = _risk_params_from_config()
        quantity, max_loss, budget_B, _net_ml = U.calc_position_size(
            equity, risk_fraction, f_max, stop_distance, lot_size, entry_price=entry_ref,
            sec_type=U._sec_type_of(symbol), market="US", stop_price=stop_loss)
        if quantity <= 0:
            print(json.dumps({"ok": False, "error": "仓位为 0（止损距太大或权益不足）"}, ensure_ascii=False))
            sys.exit(1)

    # 真实费率上下文（2026-08-12；2026-08-17 平台费改固定模式；2026-08-28 两路径统一）：
    # quantity=0 已在上面算出真实股数，显式传量本来就有——统一真实费率（美股按股计费，
    # shares 必传），旧「quantity=0 退百分比估费」路径废止。
    fee_ctx = U.build_fee_ctx(symbol, quantity, config)
    in_range, range_low, range_high, odds_at_ref, odds_at_current = U.check_price_in_range(
        direction, current_price, entry_ref, stop_loss, target, symbol, fee_ctx
    )
    result_base = {
        "action": "open_position_tiger_us", "market": "US", "symbol": symbol, "direction": direction,
        "entry_ref": entry_ref, "stop_loss": stop_loss, "target": target,
        "current_price": current_price, "range_low": round(range_low, 4), "range_high": round(range_high, 4),
        "odds_at_ref": round(odds_at_ref, 2), "odds_at_current": round(odds_at_current, 2),
    }
    if lot_size is not None:
        result_base.update({"auto_sized": True, "equity": equity, "equity_currency": currency,
                            "lot_size": lot_size, "budget_B": round(budget_B, 2),
                            "max_loss": round(max_loss, 2),
                            "max_loss_basis": ("net（止损距+开仓费+止损价平仓费，2026-08-28 口径）"
                                               if _net_ml else
                                               "gross（净口径参数缺失回退毛值——异常，检查调用参数）")})
    if not in_range:
        result_base.update({"ok": False, "error": (
            f"当前价 {current_price} 不在范围 [{range_low:.4f}, {range_high:.4f}] 内。"
            f"参考价赔率 {odds_at_ref:.2f}，当前价赔率 {odds_at_current:.2f}。")})
        print(json.dumps(result_base, ensure_ascii=False))
        sys.exit(1)

    # 当前价净赔率硬校验（2026-08-21 立为 2.4、2026-08-24 降 1.8（对齐港股版，实盘 07709 追高事故教训）、
    # 2026-08-25 随税闸双降 1.2 并进 config.risk.min_net_odds 统一调参）：
    # 按当前实价净赔率 < 门槛一律拒单，杜绝「参考价过旧/现价已偏离信号位却按旧参考价通过门槛」。
    # 2026-08-28 重排（先算仓位再校验，同港股版）：赔率一律按真实股数 × 真实费率 × 净分母算，
    # 拒单报错带股数与净 max_loss。
    _min_odds = _min_net_odds_from_config()
    if odds_at_current < _min_odds:
        _ml_note = (f"，仓位 {result_base.get('quantity', quantity)} 股、净 max_loss "
                    f"{result_base.get('max_loss', 0):,.0f}" if result_base.get("auto_sized") else "")
        result_base.update({"ok": False, "error": (
            f"当前价 {current_price} 的净预期赔率 {odds_at_current:.2f} < {_min_odds} 开仓门槛——"
            f"按当前实价 + 真实股数 × 真实费率 × 净分母口径不达标（参考价 {entry_ref} 口径 "
            f"{odds_at_ref:.2f}；参考价与现价偏差 {(current_price/entry_ref - 1)*100:.1f}%）{_ml_note}。"
            f"处理：刷新参考价为最新实价、等回踩到赔率达标位再评估，不得按旧参考价通过门槛；"
            f"小仓位单（按股费 + 每笔最低费占比高）赔率天然更薄，换更大止损距结构或放弃该形态。")})
        print(json.dumps(result_base, ensure_ascii=False))
        sys.exit(1)

    # 企稳维持时长在场打印（2026-08-21 立，对齐港股版）：读当日采样 log 统计现价附近
    # 最近连续维持分钟数，< 10 分钟打印「不满足企稳定义『维持 ≥10 分钟』」提醒。
    stability = _stability_minutes(symbol, current_price)
    if stability is not None:
        result_base["stability_minutes"] = round(stability, 1)
        if stability < 10:
            result_base["stability_warning"] = (
                f"⚠️ 现价 {current_price} 在当前价位附近连续维持仅 {stability:.1f} 分钟（< 10 分钟），"
                f"不满足企稳定义「维持 ≥10 分钟」——若本单为回踩企稳入场，请先核验：真支撑 + ≥2 次"
                f"测试不破 + 维持 ≥10 分钟 + 确认事件已发生（右侧入场总则）；突破入场不受此限。"
            )

    # 自动算仓位（quantity=0）：equity 取老虎账户 USD 净值、lot_size 从 get_contract 取（美股默认 1）。
    # risk_fraction / f_max 从 config.json 读（2026-08-16 修，同港股版：原硬编码 0.02/0.10）。
    # 自动算仓位（quantity=0）——2026-08-28 已前移至取行情之后、价格范围/赔率校验之前
    # （见上方「先算仓位再校验」块），此处不再重复执行。
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
        ok, err = _enforce_explicit_quantity_risk(quantity, entry_ref, stop_loss, equity,
                                                  symbol=symbol)
        if not ok:
            result_base.update({"ok": False, "error": err,
                                "equity": equity, "equity_currency": currency})
            print(json.dumps(result_base, ensure_ascii=False))
            sys.exit(1)
        result_base["risk_checked"] = True

    # 购买力上限（2026-08-16 立，同港股版；2026-08-21 修：原误调港股版
    # T.get_buying_power_tiger——其 to_tiger_symbol 只认 HK.xxx、美股代码报「老虎脚本
    # 只支持港股」、主动降档恒失效。改调本市场版 U.get_buying_power_us，口径同构、
    # USD 同币种无汇率换算：可买股数上限 = buying_power × long_initial_margin ÷ 参考价）。
    _bp_shares, _bp_val, _bp_margin = U.get_buying_power_us(config, symbol, entry_ref)
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

    # 限价 = 盘口对价（2026-08-23 用户立「下单必须用限价单」，主单改回 LMT）：
    # 做多取 ask 主动买 / 做空挂 max(bid, ask)（2026-08-25 随港股 T122 同步改——美股无
    # 提价规则但同口径无副作用：挂 ask 排队卖与挂 bid 主动卖在点差 1 tick 时成交价相同，
    # 且未来若券商侧加风控也不踩线），取整到美股 tick 0.01
    if direction == "long":
        lo_price = quote["ask"] if quote.get("ask") else current_price
    else:
        lo_price = max(quote["bid"], quote["ask"]) if (quote.get("bid") and quote.get("ask")) \
            else (quote.get("ask") or current_price)
    lo_price = U.round_to_tick_us(lo_price)
    side_str = "Buy" if direction == "long" else "Sell"

    # 主单 LMT + 附加止损腿 OrderLeg('LOSS')（一次提交）
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

    # 多会话单持仓互斥（2026-08-17 立，方案 A，同港股版）：整个「查单 → 闸门 → 下单 →
    # 确认」临界区包进 TradeMutex——**全局一把锁、不分市场**（分市场两把锁会放过「港股
    # 持仓违规过夜 + 美股会话开新仓」的跨市场叠加路径；港美时段不重叠使全局锁代价为零）。
    with TradeMutex(market="US", symbol=symbol, side=side_str, qty=quantity) as mutex:
        try:
            _gate_orders = U.get_today_orders_us(config)
        except Exception as ge:
            result_base.update({"lo_price": lo_price, "ok": False,
                                "error": f"互斥闸门查单失败，保守拒开：{ge}",
                                "blocked_by": "gate_query_failed"})
            print(json.dumps(result_base, ensure_ascii=False))
            sys.exit(1)
        _gate = mutex.blocked(_gate_orders)
        if _gate is not None:
            # 影子交易路径提示（2026-08-27 立，在场打印，同港股版）：被拦原因是「别人在场」
            # 口径时，AI 应把这笔机会按影子交易落纸面记录（shadow_trade.py open），不丢弃样本。
            _shadow_hint = None
            if _gate["blocked_by"] in ("open_exposure_today", "active_open_order"):
                _shadow_hint = (
                    f"别人先到了——按【影子交易】记录这笔机会再继续盯："
                    f"python3 shadow_trade.py open ET {symbol} {direction} "
                    f"{entry_ref} {stop_loss} {target} {quantity} "
                    f"--blocked-by {_gate['blocked_by']}"
                    f"（影子=纸面假设成交、不碰账户、同时至多一笔【未平】——平仓结算后额度即释放、"
                    f"可再开新仓；不要凭记忆预判额度、跳过落仓，直接照本行执行、由脚本校验兜底；"
                    f"真实仓优先，详见 auto-mode.md「影子交易」节）")
            result_base.update({"lo_price": lo_price, "ok": False,
                                "error": _gate["detail"], "blocked_by": _gate["blocked_by"]})
            if _shadow_hint:
                result_base["shadow_hint"] = _shadow_hint
            print(json.dumps(result_base, ensure_ascii=False))
            sys.exit(1)

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
                order_id = U.submit_order_with_stop_us(config, symbol, side_str, qty, lo_price,
                                                       stop_loss, profit_price=target)
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

        # intent 终态结算（锁内、输出前；同港股版口径）
        if mutex.line_no is not None:
            if filled:
                mutex.settle("filled", order_id=order_id)
            elif part_filled_qty:
                mutex.settle("part_filled", order_id=order_id,
                             extra={"filled_qty": part_filled_qty})
            elif any(f.get("status") == "submit_timeout_ambiguous" for f in failures):
                pass   # 模糊失败：pending 保留（宁可误拦、不可漏拦）
            else:
                has_cancel = any("cancel_note" in f or f.get("status") not in ("Invalid", "submit_exception")
                                 for f in failures)
                mutex.settle("cancelled" if has_cancel else "rejected", order_id=order_id)

    if not filled:
        if part_filled_qty:
            result_base.update({"lo_price": lo_price,
                                "ok": True, "part_filled": True, "quantity": part_filled_qty,
                                "order_id": order_id,
                                "fill_price": fill_price,
                                "warning": f"部分成交 {part_filled_qty}/{quantity} 股（超时未全成）——已按实际成交量上报，请复核持仓与残留附加止损腿",
                                "failures": failures, "main_status": status,
                                "method": "market+attached_stop"})
            # 开仓响铃（2026-08-19 立，同港股版）：部分成交也是已建仓，同样响铃提醒
            U.ring_after_fill("US", symbol,
                              note=f"auto部分成交{part_filled_qty}股@{fill_price}")
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
        actual_max_loss = U.net_max_loss("US", U._sec_type_of(symbol),
                                         result_base["quantity"], entry_ref, stop_loss)
        result_base["actual_max_loss"] = round(actual_max_loss, 2)
        result_base["actual_max_loss_basis"] = "net（止损距+开仓费+止损价平仓费，2026-08-28 口径）"
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
    # 开仓响铃（2026-08-19 立，同港股版，TODO「auto 开仓成功后响铃提醒」）：成交确认后
    # 响一声；记录写 ring-log-auto.csv 与 signal 隔离（见 trade_utils_tiger.ring_after_fill）
    U.ring_after_fill("US", symbol, note=f"auto成交{result_base['quantity']}股@{fill_price}")
    print(json.dumps(result_base, ensure_ascii=False))


if __name__ == "__main__":
    main()
