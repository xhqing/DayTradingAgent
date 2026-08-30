#!/usr/bin/env python3
"""港股开仓动作脚本（老虎证券模拟账户，港股默认账户）。

主单 LMT 限价单（2026-08-23 用户立「下单必须用限价单」，默认且唯一——--order-type mkt
直接拒单）+ **双腿附加单一次提交**：止损腿 OrderLeg('LOSS', stop_loss) + 止盈腿
OrderLeg('PROFIT', target)（2026-08-23 用户立——开仓必带止盈单；双腿齐发 attach_type=
BRACKETS 括号订单，主单成交后系统自动监控，止损/止盈任一边触发成交、另一边自动作废），
再回查主单成交状态。附加腿随主单一同提交——开仓失败或主单未成交则撤主单，附加腿随之
自动撤销，不残留裸止损 / 裸止盈。
限价 = 盘口对价（做多挂 ask 主动买；做空挂 ask——2026-08-25 修：港股提价规则要求持续交易时段
卖空价 ≥ 当时最好沽盘价 ask，挂 bid 恒违规必被实盘拒，改 max(bid, ask) 实即挂 ask 排队卖；
当日 07709 空单 3 连拒实录见 CHANGELOG 2026-08-25），取整到港股 tick。

✅ 实测状态（2026-08-03）：下单链路已 paper 开盘实测通过——LMT 主单 FILLED @486.2 +
附加止损腿 OrderLeg('LOSS') 一次提交成功、附加腿激活为独立 STP 单进入 HELD 监控（腾讯 100 股）。
⚠️ 订单类型沿革：2026-08-07 曾改市价单默认（高波动标的限价 + 8 秒超时易错过成交）、
2026-08-23 用户立「下单必须用限价单」改回限价唯一——超时未成交场景按降档循环既有
「挂起超时撤单退出、AI 决策」路径处理，不靠市价单兜底。
实测发现并修复 2 个 bug（详见 trade_utils_tiger.py 与 CHANGELOG 2026-08-03）：① create_order 的
order_type 传枚举对象序列化失败（须传字符串 'LMT'）；② 成交回查 status 枚举须取 .value。

用法：
  python3 open_position_tiger.py <symbol> <direction> <entry_ref> <stop_loss> <target> <quantity> [--order-type lmt]
    symbol      港股代码（富途格式 HK.02800，内部转老虎 02800）
    direction   long / short
    entry_ref   参考价
    stop_loss   止损价（附加止损腿触发价）
    target      目标止盈价
    quantity    开仓数量（股数，0=自动算仓位：lot_size 从 get_contract 取真实每手）
    --order-type 主单类型：lmt（默认，限价单=盘口 ask/bid 取整到 tick；mkt 已禁用——拒单）

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
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import trade_utils_tiger as U
from trade_mutex import TradeMutex   # 多会话并行盯盘互斥（方案 A，2026-08-17）


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
    # scripts/ 的上四级 = 项目根（scripts → trade → skills → .claude → 项目根）
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


def _stamp_gate_pct_from_config():
    """读港股个股印花税闸门槛（2026-08-25 立，随硬校验一并进 config 统一调参）。

    阈值含义：个股止损距须 ≥ stamp_tax_gate_pct × 入场价（2026-08-27 用户下调
    1.2% → 0.4%，判据从「单笔税损 ≤ 期望落地 20%」改为组合口径：不影响 P(g>0)
    前提下尽可能放松——基线 P(g>0)=99.6% 贴顶、增量单税后 EV 转负底线 ≈ 0.26%
    止损距、闸 0.4% 时组合 g_hat 仅降 6.4%，推导沿革见 risk-management.md
    「港股个股印花税闸」）。config 为权威源，
    hot_list.py / pool_claim.py 联动读同值（联动调整时改 config 一处 + 同步两脚本
    回退常量，防止三处分叉）。

    ⚠️【已停用 2026-08-28 用户裁定】config.risk.stamp_tax_gate_enabled=false 时
    税闸整体关闭（下单不再验闸拒单；hot_list / pool_claim 联动停用）。印花税
    0.1%/边仍照常计入净赔率与费用（费率链路不受开关影响）。重开改 enabled=true。
    """
    import json as _json
    try:
        _cfg = _json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                            "..", "config.json")))
        return float(_cfg.get("risk", {}).get("stamp_tax_gate_pct", 0.004))
    except Exception:
        return 0.004


def _stamp_gate_enabled_from_config():
    """税闸总开关（2026-08-28 立）：config.risk.stamp_tax_gate_enabled，
    缺失回退 true（向后兼容旧 config）。false = 税闸整体停用。"""
    import json as _json
    try:
        _cfg = _json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                            "..", "config.json")))
        return bool(_cfg.get("risk", {}).get("stamp_tax_gate_enabled", True))
    except Exception:
        return True


def _min_net_odds_from_config():
    """读开仓净赔率门槛（2026-08-25 随税闸同日进 config 统一调参）。

    沿革：2026-08-14 立为 2.4 → 2026-08-24 降 1.8（07709 追高事故）→ 2026-08-25
    用户降 1.2（与税闸 2.22%→1.2% 双降，解锁「当日结构宽度中等」标的的开仓交集）。
    config 为权威源，open_position_tiger_us.py 同读；缺失回退 1.2。
    """
    import json as _json
    try:
        _cfg = _json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                            "..", "config.json")))
        return float(_cfg.get("risk", {}).get("min_net_odds", 1.2))
    except Exception:
        return 1.2


def _enforce_explicit_quantity_risk(quantity, entry_ref, stop_loss, result_base, equity,
                                    currency, lot):
    """显式传量路径的风控校验（2026-08-16 立，修复「显式传量绕过全部风控上限」）。

    原实现：f_max / max_leverage / equity 约束只在 quantity=0 自动算仓位时生效，显式
    传量时唯一护栏是券商保证金拒单（58,400 股 MINIMAX 大单场景正是此路径）——AI 误传
    超限量时实际 max_loss 可远超 f_max 上限、杠杆可远超 max_leverage。

    现校验三项（equity 为 None 时跳过 equity 相关项并 warning；max_loss 2026-08-28 起
    为净口径 = 止损距×量 + 开仓边费 + 止损价平仓边费）：
    1. 净 max_loss ≤ equity × f_max；
    2. 开仓市值 = quantity × entry_ref ≤ equity × max_leverage；
    3. quantity 合 lot（整手）。
    超限**拒绝下单**（ok:false，不只是 warning）——降档循环的被动降档不走此路径
    （降档只会更小、天然更安全）。返回 (ok, error_or_none)。
    """
    import json as _json
    risk_fraction, f_max = _risk_params_from_config()
    stop_distance = abs(entry_ref - stop_loss)
    actual_max_loss = U.net_max_loss("HK", U._sec_type_of(result_base["symbol"]),
                                     quantity, entry_ref, stop_loss)
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
    order_type = "lmt"
    if "--order-type" in args:
        idx = args.index("--order-type")
        if idx + 1 >= len(args):
            print("用法错误：--order-type 需要参数 lmt", file=sys.stderr)
            sys.exit(1)
        order_type = args[idx + 1].lower()
        del args[idx:idx + 2]
    # 市价单硬禁（2026-08-23 用户立「下单必须用限价单」，工具级强制）：
    # --order-type mkt 一律拒单——参数保留解析是为了给出明确报错、引导用限价单。
    if order_type == "mkt":
        print(json.dumps({"ok": False, "blocked_by": "market_order_forbidden",
                          "error": "下单必须用限价单（2026-08-23 用户立）：主单市价单已禁用，"
                                   "不传 --order-type 即默认限价单（做多挂 ask、做空挂 ask，取整 tick）"},
                         ensure_ascii=False))
        sys.exit(1)
    if order_type != "lmt":
        print(json.dumps({"ok": False, "error": f"--order-type 只支持 lmt（市价单已禁用），收到 '{order_type}'"}))
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
    # 实盘解锁前置闸（2026-08-21 立，实盘误开防护检查点①）：--account live 且解锁文件
    # 无效 → blocked_by:"live_locked" 结构化拒单（详见 scripts/live_unlock.py）。
    import live_unlock
    live_unlock.live_gate_for_order_scripts(account, "open_position_tiger")

    if len(args) < 6:
        print(
            "用法: python3 open_position_tiger.py <symbol> <direction> <entry_ref> "
            "<stop_loss> <target> <quantity> [--order-type lmt]\n"
            "  symbol    港股代码（HK.02800）\n"
            "  direction long / short\n"
            "  entry_ref 参考价\n"
            "  stop_loss 止损价（附加止损触发价）\n"
            "  target    目标止盈价\n"
            "  quantity  开仓股数（0=自动算仓位）\n"
            "  --order-type 主单类型：lmt（默认，限价单；mkt 已禁用——拒单）",
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
    # 开仓时间闸（2026-08-18 立）：距停盯 >5 分钟放行、≤5 分钟或盘外拒单。
    # 「距停盯 >5 分钟就仍可开仓」规则的工具级执行点——AI 侧不许自设「临近收盘/午休
    # 不开仓」截止线（2026-08-17、2026-08-18 两次同类违规后用户立工具强制）。
    _gate_ok, _gate_msg = U.check_open_time_gate("HK")
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
        quote = U.get_quote_tiger(config, symbol)
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"获取行情失败: {e}"}))
        sys.exit(1)

    if quote is None:
        print(json.dumps({"ok": False, "error": f"港股报价为空: {symbol}（检查代码/盘后）"}))
        sys.exit(1)
    current_price = quote["last"]

    # ===== 自动算仓位（quantity=0）——2026-08-28 前移到价格范围/赔率校验之前 =====
    # 为什么前移：净赔率与净 max_loss 的公式都依赖股数（费按成交额×股数算、固定费摊每股随
    # 股数变），股数未定时只能用「百分比估费+毛分母」的近似口径校验——小仓位单（固定费
    # 摊薄差）估得偏乐观、会把真实净赔率不达标的单放行（实测 200 股例：近似 1.98 vs 真净
    # 0.97）。用户裁定「先算仓位再校验」：quantity=0 先按风控参数定档，再带着真实股数做
    # 全部校验，两条路径（显式传量 / 自动算仓）同一真实费率+净分母口径。
    # 代价：拒单场景多查一次 equity（只读 API、秒级），换主路径硬校验全程真实口径，值得。
    # equity 取老虎账户净值（港股口径 HKD，与标的止损距同币种——2026-08-05 修：原取 USD
    # 净值直接当 HKD 用，B 被低估 ~7.8 倍；现 get_prime_assets base_currency='HKD' 直接取
    # HKD 净值）、lot_size 从 get_contract 取真实每手股数。risk_fraction / f_max 从
    # config.json 读（2026-08-16 修：原硬编码 0.02/0.10，config「修改本文件即可调参」契约
    # 对这两个参数不生效）。选档用 entry_ref 口径（下单前成交价未知），费与净 max_loss
    # 均为 2026-08-28 净口径。
    lot_size = None
    equity = None
    currency = None
    if quantity == 0:
        tc = U.new_trade_client(config)
        equity, currency = U.load_equity_tiger(config, base_currency='HKD')
        if equity is None:
            result_stub = {"ok": False, "error": "老虎账户净值取不到（未开通交易/资产权限？），无法自动算仓位"}
            print(json.dumps(result_stub, ensure_ascii=False))
            sys.exit(1)
        lot_size = U.get_lot_size_tiger(tc, symbol)
        if not lot_size:
            print(json.dumps({"ok": False, "error": f"查不到 {symbol} 的 lot_size，无法自动算仓位"},
                             ensure_ascii=False))
            sys.exit(1)
        stop_distance = abs(entry_ref - stop_loss)
        risk_fraction, f_max = _risk_params_from_config()
        quantity, max_loss, budget_B, _net_ml = U.calc_position_size(
            equity, risk_fraction, f_max, stop_distance, lot_size, entry_price=entry_ref,
            sec_type=U._sec_type_of(symbol), market="HK", stop_price=stop_loss)
        if quantity <= 0:
            print(json.dumps({"ok": False, "error": "仓位为 0（止损距太大或权益不足）"}, ensure_ascii=False))
            sys.exit(1)

    # 真实费率上下文（2026-08-12；2026-08-17 平台费改固定模式；2026-08-28 两路径统一）：
    # 含 shares / sec_type / market。quantity=0 已在上面临近算出真实股数，显式传量本来就
    # 有——两条路径都用真实费率做价格范围检查与赔率硬校验（净分母口径），旧「quantity=0
    # 退百分比估费」路径废止。
    fee_ctx = U.build_fee_ctx(symbol, quantity, config)
    in_range, range_low, range_high, odds_at_ref, odds_at_current = U.check_price_in_range(
        direction, current_price, entry_ref, stop_loss, target, symbol, fee_ctx
    )
    result_base = {
        "action": "open_position_tiger", "market": "HK", "symbol": symbol, "direction": direction,
        "entry_ref": entry_ref, "stop_loss": stop_loss, "target": target,
        "current_price": current_price, "range_low": round(range_low, 4), "range_high": round(range_high, 4),
        "odds_at_ref": round(odds_at_ref, 2), "odds_at_current": round(odds_at_current, 2),
    }
    # 自动算仓位的落档信息（在费上下文/校验之后写进 result，拒单报错里也带上真实股数与赔率）
    if lot_size is not None:
        result_base.update({"auto_sized": True, "equity": equity, "equity_currency": currency,
                            "lot_size": lot_size, "budget_B": round(budget_B, 2),
                            "max_loss": round(max_loss, 2),
                            "max_loss_basis": ("net（止损距+开仓费+止损价平仓费，2026-08-28 口径）"
                                               if _net_ml else
                                               "gross（净口径参数缺失回退毛值——异常，检查调用参数）")})

    # 港股个股印花税闸硬校验（2026-08-25 立，01888 口径混用教训）：
    # 「个股止损距 ≥ 2.22%×股价才可开」此前只在 AI 自查清单 + hot_list / pool_claim 预检里，
    # 最终裁决靠 AI 临场手算——2026-08-25 上午 01888 实盘盯盘：10:19 定入场框架时税闸用参考价
    # 37.6 算（止损 36.7，距 2.39% 判「过」），入场计划却是回踩 37.1（真实止损距 1.08%、不过闸），
    # 同一框架里净赔率用入场价、税闸用参考价——口径混用让「双闸达标」成假象，空耗 40 分钟等
    # 回踩、直到 11:09 下单前精算才发现死结。落地为脚本硬校验：
    # ① 口径 = 实际入场价 current_price（下单时点真实价格，杜绝参考价失真——与上方净赔率
    #    ≥门槛硬校验同一逻辑：参数口径该用「真实成交口径」而非「计划口径」）；
    # ② 只拦港股个股（HK. 前缀 + sec_type=stock）——ETF 免印花税不受此闸（REIT 落 stock 档、
    #    保守照拦，见 fee_schedule.py 2026-08-17 注）；
    # ③ 拒单给出「止损须放到的最低价」与「赔率死结」提示（止损拉宽过税闸 → 赔率被压缩，
    #    两门槛互斥时只能换标的——2026-08-25 上午 01888 / 00100 两笔均死于这个死结）。
    _sec_type = U._sec_type_of(symbol)
    if _sec_type == "stock" and _stamp_gate_enabled_from_config():
        _gate_pct = _stamp_gate_pct_from_config()
        _stop_dist = abs(current_price - stop_loss)
        _stop_pct = _stop_dist / current_price if current_price > 0 else 0.0
        result_base["stamp_gate_pct"] = round(_stop_pct * 100, 2)
        result_base["stamp_gate_threshold"] = round(_gate_pct * 100, 2)
        if _stop_pct < _gate_pct:
            _min_stop = (current_price * (1 - _gate_pct) if direction == "long"
                         else current_price * (1 + _gate_pct))
            _drain = 0.002 / _stop_pct          # 双边印花税 0.2% ÷ 止损距比例
            _drain_cap = 0.002 / _gate_pct      # 门槛对应的损耗上限（随 config 阈值联动）
            result_base.update({
                "ok": False, "blocked_by": "stamp_tax_gate",
                "error": (
                    f"港股个股印花税闸未过：按实际入场价 {current_price} 算，止损距 "
                    f"{_stop_dist:.2f} = {_stop_pct*100:.2f}%×股价 < 门槛 {_gate_pct*100:.2f}%"
                    f"（双边印花税 R 损耗 {_drain:.3f}R > 上限 {_drain_cap:.3f}R）。"
                    f"过闸须止损至少放到 {_min_stop:.2f}——但止损拉宽会压缩净赔率"
                    f"（当前价口径 {odds_at_current:.2f}），两门槛互斥时只能换同板块 ETF 或放弃该形态"
                    f"（risk-management.md「港股个股印花税闸」；hot_list 预检「低」即此因）。"),
            })
            print(json.dumps(result_base, ensure_ascii=False))
            sys.exit(1)
    # 止盈可达性检查（2026-08-21 立硬闸；2026-08-27 降级为警告——方案 A）：
    # 「目标止盈位必须大概率可达」规则（trading-strategy.md「止盈的决策框架」节）
    # 2026-08-21 MINIMAX 337.2 开仓止盈 360 教训立硬闸（越过当日高点拒单）。2026-08-27 历史回放
    # 复盘（reviews/2026-08-27-gate-review.md）推翻硬闸：50 笔港股样本回放——硬闸会拦掉 35 笔、
    # 净效应 -35.6 万 HKD / -10.6R（被拦笔平均 +0.30R 仍为正期望，错杀利润 +50.3万 远大于避损
    # -14.7万）；92% 的历史交易实际靠移动止损/主动平仓出场、止盈价只是入场预期参考；且强趋势日
    # （利润最肥的日子）创新高瞬间挂单、任何有意义止盈永远在当日高点上方——硬闸与趋势跟随盈利
    # 模式结构性冲突（2026-08-27 早盘 06166 两次被拦后目送 +8% 实录）。用户裁定方案 A：降级为
    # 警告不拒单——保留「止盈距日高 +X%」的信息在场打印（历史数据证明该信号有筛选区分度：
    # 放行组 +0.83R/笔 vs 被拦组 +0.30R/笔），AI 结合趋势强度（是否创新高+放量+VWAP 上方）
    # 自行裁量止盈定多远；止盈定价顺序规矩（先结构目标后赔率、同尺度）不变，仍见
    # trading-strategy.md「止盈定价顺序」。
    day_high = quote.get("high")
    day_low = quote.get("low")
    reachable = True
    unreachable_reason = ""
    if direction == "long" and day_high and target > day_high:
        reachable = False
        unreachable_reason = (
            f"⚠️ 止盈远于当日高点：做多止盈 {target} 越过当日高点 {day_high}"
            f"（+{(target/day_high - 1)*100:.1f}%）——常规时段当日到达概率低，历史该类笔平均落地 "
            f"+0.30R（弱于止盈在日高内的 +0.83R）。趋势日（创新高+放量+VWAP 上方）允许越日高定 "
            f"止盈、让利润奔跑；震荡日应调低到 ≤ 日高或放弃。AI 按形态裁量，本警告不拦单。"
        )
    elif direction == "short" and day_low and target < day_low:
        reachable = False
        unreachable_reason = (
            f"⚠️ 止盈远于当日低点：做空止盈 {target} 跌破当日低点 {day_low}"
            f"（{(1 - target/day_low)*100:.1f}%）——常规时段当日到达概率低。空头趋势日（创新低 "
            f"+放量+VWAP 下方）允许越日低定止盈；震荡日应调高到 ≥ 日低或放弃。AI 按形态裁量，"
            f"本警告不拦单。"
        )
    # 方案 A（2026-08-27）：target_reachable=false 只警告、不拒单（blocked_by 不设——
    # 只有真拒单才带 blocked_by），字段保留供 AI 转录动作记录时注明。
    result_base["target_reachable"] = reachable
    result_base["day_high"] = day_high
    result_base["day_low"] = day_low
    if not reachable:
        result_base["warning_target_unreachable"] = unreachable_reason
    if not in_range:
        result_base.update({"ok": False, "error": (
            f"当前价 {current_price} 不在范围 [{range_low:.4f}, {range_high:.4f}] 内。"
            f"参考价赔率 {odds_at_ref:.2f}，当前价赔率 {odds_at_current:.2f}。")})
        print(json.dumps(result_base, ensure_ascii=False))
        sys.exit(1)

    # 当前价净赔率硬校验（2026-08-21 立为 2.4，2026-08-24 降 1.8（实盘 07709 追高事故教训），
    # 2026-08-25 随税闸双降 1.2 并进 config.min_net_odds 统一调参）：
    # 「初始净预期赔率 ≥ 门槛才开仓」是开仓筛选门槛（单一门槛直接比）——此前脚本只在价格超出
    # [0.6, 10] 机械范围时拒单，AI 用旧参考价算赔率（参考价 40.70 口径 3.82 达标）、实际当前价
    # 40.90 的净赔率 1.41 不达标仍放行（2026-08-21 实盘：07709「VWAP 回踩企稳」信号 40.56 一瞬
    # 贴线当企稳、4 分钟后 40.82 追高成交、2 分钟止损 -1.18R）。规则落地为脚本硬校验：按当前实价
    # 净赔率 < 门槛一律拒单，杜绝「参考价过旧/现价已偏离信号位却按旧参考价通过门槛」。
    # 边界：正常回踩企稳入场（现价≈参考价）odds_at_current≈odds_at_ref≥门槛，不误拦；
    # 突破追高入场按现价算赔率本就该达标才能下，被拦是应有之义。
    # 2026-08-28 重排（先算仓位再校验）：赔率一律按真实股数 × 真实费率 × 净分母算——
    # 自动算仓位（quantity=0）已在上面临近算出股数，小仓位单的固定费摊薄效应如实进赔率
    # （实测 200 股例：近似口径 1.98 放行 vs 真净 0.97 应拦），拒单报错带股数与净 max_loss。
    _min_odds = _min_net_odds_from_config()
    if odds_at_current < _min_odds:
        _ml_note = (f"，仓位 {result_base.get('quantity', quantity)} 股、净 max_loss "
                    f"{result_base.get('max_loss', 0):,.0f}" if result_base.get("auto_sized") else "")
        result_base.update({"ok": False, "error": (
            f"当前价 {current_price} 的净预期赔率 {odds_at_current:.2f} < {_min_odds} 开仓门槛——"
            f"按当前实价 + 真实股数 × 真实费率 × 净分母口径不达标（参考价 {entry_ref} 口径 "
            f"{odds_at_ref:.2f}；参考价与现价偏差 {(current_price/entry_ref - 1)*100:.1f}%）{_ml_note}。"
            f"处理：刷新参考价为最新实价、等回踩到赔率达标位再评估，不得按旧参考价通过门槛；"
            f"小仓位单（固定费占比高）赔率天然更薄，换更大止损距结构或放弃该形态。")})
        print(json.dumps(result_base, ensure_ascii=False))
        sys.exit(1)

    # 企稳维持时长在场打印（2026-08-21 立，配合上方赔率硬校验）：
    # 读采样 log 统计现价附近最近连续维持分钟数——「一瞬贴线（0.x 分钟）当企稳」是今天事故的
    # 判定根因；维持 ≥10 分钟是企稳定义四要素中最可机械核验的一条。本项不硬拦（突破入场等形态
    # 不要求维持时长，硬拦会误伤），只作决策时刻在场打印提醒。
    stability = _stability_minutes(symbol, current_price)
    if stability is not None:
        result_base["stability_minutes"] = round(stability, 1)
        if stability < 10:
            result_base["stability_warning"] = (
                f"⚠️ 现价 {current_price} 在当前价位附近连续维持仅 {stability:.1f} 分钟（< 10 分钟），"
                f"不满足企稳定义「维持 ≥10 分钟」——若本单为回踩企稳入场，请先核验：真支撑 + ≥2 次"
                f"测试不破 + 维持 ≥10 分钟 + 确认事件已发生（右侧入场总则）；突破入场不受此限。"
            )

    # 自动算仓位（quantity=0）——2026-08-28 已前移至取行情之后、价格范围/赔率校验之前
    # （见上方「先算仓位再校验」块），此处不再重复执行。
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

    # 主单提交参数：lmt 限价单（做多取 ask 主动买、做空挂 ask，取整到港股 tick）。
    # 做空限价 = max(bid, ask)（2026-08-25 修 T122）：港股提价规则要求持续交易时段卖空价
    # ≥ 当时最好沽盘价（ask），挂 bid 恒违规——实盘生产网关必拒（实测 07709 空单 3 连拒，
    # reason=short sales cannot be made below the reference price），模拟盘网关宽松不暴露。
    # max(bid, ask) 数学上 = ask（bid ≤ ask 恒成立），写成 max 形式让意图自明：保证 ≥ ask。
    # 挂 ask = 与最低卖单同价排队卖（非主动砸 bid）——提价规则本意就是禁止做空主动打压，
    # 卖空天然只能被动等买方吃上来；取不到盘口时兜底 current_price（last，≥ last 满足规则）。
    side_str = "Buy" if direction == "long" else "Sell"
    if order_type == "lmt":
        if direction == "long":
            lo_price = quote["ask"] if quote.get("ask") else current_price
        else:
            lo_price = max(quote["bid"], quote["ask"]) if (quote.get("bid") and quote.get("ask")) \
                else (quote.get("ask") or current_price)
        tick_sizes = U.get_tick_sizes_tiger(U.new_trade_client(config), symbol)
        lo_price = U.round_to_tick_tiger(lo_price, tick_sizes)
        result_base["lo_price"] = lo_price
    else:
        lo_price = None

    # 购买力上限（2026-08-16 立，2026-08-06 00100 被拒根因闭环）：算出目标量后按单标的
    # 保证金率算可买上限，超了**下单前主动降档**（输出 capped_by_buying_power），被动降档
    # 保留兜底。可买股数上限 = buying_power(USD 折 HKD) × long_initial_margin ÷ 参考价。
    # 2026-08-18 修币种：buying_power 是 USD 口径、直接除 HKD 价会把上限压低 7.8 倍、
    # 小账户直接降档到 0 误拒（实测实盘 bp 折算后远不足 1 手被拒）。
    # 汇率从两币种净值之比推（load_equity_tiger 的 HKD 净值 ÷ get_assets 的 USD 净值
    # = 老虎自身 forex_rate），拿不到时函数内按 7.80 保守兜底。
    _fx_hkd = None
    try:
        _eq_usd = U.load_equity_tiger(config, base_currency='USD')[0]
        _eq_hkd = U.load_equity_tiger(config, base_currency='HKD')[0]
        if _eq_usd and _eq_hkd:
            _fx_hkd = _eq_hkd / _eq_usd
    except Exception:
        pass
    _bp_shares, _bp_val, _bp_margin = U.get_buying_power_tiger(
        config, symbol, entry_ref, tc=_tc_tick, to_hkd=_fx_hkd)
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
    # 多会话单持仓互斥（2026-08-17 立，方案 A）：整个「查单 → 闸门 → 下单 → 确认」临界区
    # 包进 TradeMutex——flock 全局锁把两会话的下单流程在内核级串行；锁内三口径闸门
    # （pending intent / 当日在场敞口 / 活动开仓挂单）任一命中即拒开（先到先得），
    # 输出 blocked_by 结构化 JSON，AI 据此继续盯盘不重试。intent 日志在 submit 前写
    # pending、确认后 settle 终态，崩在「已提交未确认」之间时后到会话拒开。
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

    with TradeMutex(market="HK", symbol=symbol, side=side_str, qty=quantity) as mutex:
        # 锁内三口径闸门（查单与成交确认同数据源 get_orders；拿不到订单时保守拒开——
        # 多会话并行下盲开比误拒危险）。
        try:
            _gate_orders = U.get_today_orders_tiger(config)
        except Exception as ge:
            result_base.update({"ok": False,
                                "error": f"互斥闸门查单失败，保守拒开：{ge}",
                                "blocked_by": "gate_query_failed"})
            print(json.dumps(result_base, ensure_ascii=False))
            sys.exit(1)
        _gate = mutex.blocked(_gate_orders)
        if _gate is not None:
            # 影子交易路径提示（2026-08-27 立，在场打印）：被拦原因是「别人在场」口径时，
            # AI 应把这笔机会按影子交易落纸面记录（shadow_trade.py open），不丢弃样本。
            # 提示打进 blocked JSON 的 shadow_hint 字段——AI 读 JSON 时必然看到（决策点 3：
            # pending_intent 等状态不明口径不给影子提示、先人工排查）。
            _shadow_hint = None
            if _gate["blocked_by"] in ("open_exposure_today", "active_open_order"):
                _shadow_hint = (
                    f"别人先到了——按【影子交易】记录这笔机会再继续盯："
                    f"python3 shadow_trade.py open HKT {symbol} {direction} "
                    f"{entry_ref} {stop_loss} {target} {quantity} "
                    f"--blocked-by {_gate['blocked_by']}"
                    f"（影子=纸面假设成交、不碰账户、同时至多一笔【未平】——平仓结算后额度即释放、"
                    f"可再开新仓；不要凭记忆预判额度、跳过落仓，直接照本行执行、由脚本校验兜底；"
                    f"真实仓优先，详见 auto-mode.md「影子交易」节）")
            result_base.update({"ok": False, "error": _gate["detail"],
                                "blocked_by": _gate["blocked_by"]})
            if _shadow_hint:
                result_base["shadow_hint"] = _shadow_hint
            print(json.dumps(result_base, ensure_ascii=False))
            sys.exit(1)

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
                    profit_price=target, order_type=order_type.upper())
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

        # intent 终态结算（锁内、输出前）：拿到券商确认的状态映射到 filled / cancelled /
        # rejected；模糊失败 / 部分成交保留 pending（后到会话口径 ① 兜住、逼人工确认）。
        if mutex.line_no is not None:
            if filled:
                mutex.settle("filled", order_id=order_id)
            elif part_filled_qty:
                mutex.settle("part_filled", order_id=order_id,
                             extra={"filled_qty": part_filled_qty})
            elif any(f.get("status") == "submit_timeout_ambiguous" for f in failures):
                pass   # 模糊失败：pending 保留（宁可误拦、不可漏拦）
            else:
                # 全部档被拒 / 撤单退出：无在场敞口，结算为 rejected/cancelled 由失败明细判断
                has_cancel = any("cancel_note" in f or f.get("status") not in ("Invalid", "submit_exception")
                                 for f in failures)
                mutex.settle("cancelled" if has_cancel else "rejected", order_id=order_id)

    if not filled:
        if part_filled_qty:
            # 部分成交：如实上报实际成交量（不是失败到零、也不是全额成功）
            result_base.update({"ok": True, "part_filled": True, "quantity": part_filled_qty,
                                "order_id": order_id,
                                "fill_price": fill_price,
                                "warning": f"部分成交 {part_filled_qty}/{quantity} 股（超时未全成）——已按实际成交量上报，请复核持仓与残留附加止损腿",
                                "failures": failures, "main_status": status,
                                "method": f"{order_type}+attached_stop"})
            # 开仓响铃（2026-08-19 立）：部分成交也是已建仓，同样响铃提醒（note 注明）
            U.ring_after_fill("HK", symbol,
                              note=f"auto部分成交{part_filled_qty}股@{fill_price}")
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
        actual_max_loss = U.net_max_loss("HK", U._sec_type_of(symbol),
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
        fill_price = lo_price if lo_price else current_price
        fill_src = "lo_price（成交均价缺失兜底）" if lo_price else "current_price（成交均价缺失兜底）"
    result_base.update({"ok": True, "fill_price": fill_price, "fill_price_source": fill_src,
                        "method": f"{order_type}+attached_stop",
                        "stop": f"attached LOSS @ {stop_loss}", "main_status": status})
    # 开仓响铃（2026-08-19 立，TODO 落地）：成交确认后响一声提醒用户已开仓——脚本内置
    # = 工具强制（auto 模式用户若不盯屏幕感知不到开仓；记录写 ring-log-auto.csv 与
    # signal 隔离，见 U.ring_after_fill docstring）。
    U.ring_after_fill("HK", symbol, note=f"auto成交{result_base['quantity']}股@{fill_price}")
    print(json.dumps(result_base, ensure_ascii=False))


if __name__ == "__main__":
    main()
