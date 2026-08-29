#!/usr/bin/env python3
"""美股交易工具库（老虎证券开放平台，美股默认账户）。

2026-08-05 立：美股默认账户为老虎模拟账户（与港股同账户），专门为美股开发独立一套：
复用 trade_utils_tiger 的**不分市场**
基础设施（SDK 配置、撤单、费率、赔率、仓位纯函数、成交回查）+ 美股特定适配（symbol 裸代码、
lot 1、tick 0.01、USD 净值、附加止损 OrderLeg）。平仓走「modify 止损触发价=现价」无 race 路径
（同港股 close_position_tiger 2026-08-05 改造）。

✅ 实测状态（2026-08-05 美股盘中，当时开仓主单还是 LMT、2026-08-07 起已改 MKT）：下单链路全链路
已 paper 端到端实测通过（SPY 2 股小仓位：开仓 LMT Filled @773.68 + 附加止损腿 LOSS 激活
→ 移损 modify aux_price 770.61→771.71 验证成功
→ 平仓 modify 触发价=现价、止损单触发 MO Filled @773.44、持仓归零无残留止损单）。行情数据源
修复：老虎 TBNZ 账户**美股无行情权限**（get_stock_briefs 实测报 code=4 msg=4000 permission
denied US market），get_quote_us 改富途 OpenD 单源（美股行情只有富途可用）。

老虎美股关键差异（vs 港股）：
- **symbol = 裸代码**（MU / AAPL，2026-08-05 实测：MU.US / US.MU 报「don't support trading」，
  富途格式 US.MU → 取 '.' 后裸代码 MU）。
- **lot_size = 1**（美股 1 股/手，从 get_contract.lot_size 取、fallback 1）。
- **tick = 0.01**（美股统一最小报价单位、无价位表）。
- **币种 USD**：账户 currency=USD，equity 取 net_liquidation 直用（无需港股的 HKD 保守口径换算）。
- **费率**：真实费率（2026-08-12 改，复用 fee_schedule / trade_utils_tiger 的 _market_of + _sec_type_of + build_fee_ctx；2026-08-17 美股改**按股结构**）：佣金 0.0039 USD/股（cap 0.5%×额）+ 平台费 0.004 USD/股（每笔最低 1、cap 0.5%×额）+ 代收近似 0.00396 USD/股，≈0.0087 USD/股线性、无印花税。
- 交易时段：美东 04:00-16:00 = 盘前 + 盘中（2026-08-18 立规；夏令时北京 16:00-次日 04:00 / 冬令时 17:00-次日 05:00）。
  订单统一带 outside_rth=True（盘前可成交，见 _make_order_us），时间闸按 04:00 起判（trade_utils_tiger.minutes_to_session_end）。
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import trade_utils_tiger as T  # 复用不分市场的基础设施


# ---------------------------------------------------------------------------
# 配置 / 客户端（复用老虎通用，同账户）
# ---------------------------------------------------------------------------

load_config = T.load_config
new_trade_client = T.new_trade_client
new_quote_client = T.new_quote_client


# ---------------------------------------------------------------------------
# symbol 格式转换（富途 US.MU ↔ 老虎 MU 裸代码）
# ---------------------------------------------------------------------------

def to_tiger_symbol_us(symbol):
    """富途 → 老虎美股：US.MU → MU（裸代码，2026-08-05 实测）。

    老虎美股只认裸代码（MU / AAPL），MU.US / US.MU 报「don't support trading」。
    """
    if not symbol or "." not in symbol:
        return symbol
    market, code = symbol.split(".", 1)
    if market != "US":
        raise ValueError(f"美股 tiger 脚本只支持美股（US.xxx），收到 {symbol}")
    return code


def to_futu_symbol_us(tiger_symbol):
    """老虎 → 富途：MU → US.MU。

    2026-08-17 修：带类别后缀的多类股（BRK.B / BF.B）——原 `split(".")[0]` 把后缀吃掉、
    BRK.B 错转 US.BRK（错标的）；富途格式是去掉点、类别字母直接拼在代码后：BRK.B → US.BRKB。"""
    code = str(tiger_symbol)
    if "." in code:
        code = code.replace(".", "")
    return f"US.{code}"


# ---------------------------------------------------------------------------
# 合约查询：lot_size / tick / 名称
# ---------------------------------------------------------------------------

def get_contract_us(tc, symbol):
    """查美股合约（get_contract，sec_type=STK）。返回 Contract 对象或 None。"""
    from tigeropen.common.consts import SecurityType
    return tc.get_contract(to_tiger_symbol_us(symbol), sec_type=SecurityType.STK)


def get_lot_size_us(tc, symbol):
    """美股每手股数（get_contract.lot_size）。美股通常 1 股/手；fallback 1。"""
    try:
        c = get_contract_us(tc, symbol)
        if c is not None:
            ls = getattr(c, "lot_size", None)
            if ls:
                return int(ls)
    except Exception as e:
        print(f"⚠️ 查美股 lot_size 失败 {symbol}: {e}", file=sys.stderr)
    return 1  # 美股默认 1 股/手


def round_to_tick_us(price, tick_sizes=None):
    """美股价格向下取整到 tick（限价单必须合 tick）。美股统一 0.01；若 get_contract 返回
    tick_sizes 则按价位表取（一般美股不用价位表、走 0.01）。

    ⚠️ 恒向下取整（floor）、方向性后果要心里有数（历史一致、非 bug，不改行为）：
    - 卖方限价：取整后比原价低最多一个 tick → 低于当前 bid 也能成交（更激进、可能贱卖一点）；
    - 做多止损触发价：被压低最多一个 tick → 实际触发更晚、实际 max_loss 略大于计划值。"""
    import math
    tick = 0.01
    if tick_sizes:
        t = T._tick_from_table(price, tick_sizes)
        if t:
            tick = t
    return round(math.floor(price / tick) * tick, 2)


# ---------------------------------------------------------------------------
# 行情（富途 OpenD 单源——老虎 TBNZ 账户美股无行情权限，2026-08-05 盘中实测
# get_stock_briefs 报 code=4 msg=4000 permission denied(US market)；美股行情只能走富途）
# ---------------------------------------------------------------------------

def get_quote_us(config, symbol, retries=3):
    """美股最新报价——富途 OpenD 单源（老虎账户美股无行情权限、美股行情只有富途可用）。

    返回 dict {symbol, last, bid, ask, high, low, volume, latest_time} 或 None。"""
    from futu import OpenQuoteContext, RET_OK
    for attempt in range(retries):
        try:
            qc = OpenQuoteContext("127.0.0.1", 11111)
            try:
                ret, df = qc.get_market_snapshot([symbol])
                if ret != RET_OK or df is None or len(df) == 0:
                    return None
                row = df.iloc[0]

                def _f(v):
                    try:
                        return float(v) if v is not None and str(v) not in ("nan", "None") else None
                    except (TypeError, ValueError):
                        return None

                return {
                    "symbol": symbol,
                    "last": _f(row.get("last_price")),
                    "bid": _f(row.get("bid_price")),
                    "ask": _f(row.get("ask_price")),
                    "high": _f(row.get("high_price")),
                    "low": _f(row.get("low_price")),
                    "volume": int(row.get("volume") or 0),
                    "latest_time": row.get("update_time"),
                }
            finally:
                qc.close()
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(1)
                continue
            raise RuntimeError(f"富途美股行情失败 {symbol}: {e}") from e
    return None


# ---------------------------------------------------------------------------
# 权益（老虎美股账户 USD，get_assets().summary.net_liquidation）
# ---------------------------------------------------------------------------

def load_equity_us(config=None):
    """美股 equity = 老虎账户净值（summary.net_liquidation，USD）。返回 (equity, currency)。

    美股账户 currency=USD，净值直用（无需港股的 HKD 保守口径换算）。"""
    tc = new_trade_client(config)
    try:
        assets = tc.get_assets()
        if not assets:
            return None, "USD"
        summary = assets[0].summary
        na = getattr(summary, "net_liquidation", None)
        ts = getattr(summary, "timestamp", None)
        currency = getattr(summary, "currency", None) or "USD"
        if na is None or (float(na) <= 0 and ts is None):
            print("⚠️ 老虎资产查询异常（net_liquidation=0 且无时间戳）——账户未开通交易/资产权限？",
                  file=sys.stderr)
            return None, currency
        return float(na), currency
    finally:
        pass


def get_buying_power_us(config, symbol, ref_price, tc=None):
    """美股版购买力上限查询（2026-08-21 立，修橙色待办「美股开仓脚本购买力降档查询报
    老虎脚本只支持港股」）。

    背景：open_position_tiger_us.py 原来误调港股版 T.get_buying_power_tiger——其内部
    to_tiger_symbol 只认 HK.xxx、遇美股代码抛「老虎脚本只支持港股」，主动降档失效
    （购买力查询恒失败、只剩被动降档兜底、正常路径烧降档轮次）。

    口径（与港股版 get_buying_power_tiger 同构，美股更简单——无币种换算）：
    - buying_power 取 get_assets().summary.buying_power（USD 计价）；
    - 保证金率取 get_contract(symbol).long_initial_margin（美股同字段；查不到按 1.0 全额、
      最保守）；
    - 可买市值上限 = buying_power × long_initial_margin；可买股数上限 = 市值上限 ÷ ref_price
      （美股价格即 USD、buying_power 即 USD，同币种直接相除）。

    返回 (max_shares, bp, margin_rate) 或 (None, None, None)（查询失败）。
    """
    try:
        if tc is None:
            tc = new_trade_client(config)
        assets = tc.get_assets()
        if not assets:
            return None, None, None
        s = assets[0].summary
        bp = getattr(s, "buying_power", None)
        if not bp or float(bp) <= 0:
            return None, None, None
        c = get_contract_us(tc, symbol)
        margin_rate = getattr(c, "long_initial_margin", None)
        if not margin_rate or float(margin_rate) <= 0:
            margin_rate = 1.0   # 查不到保证金率按 1.0 全额（最保守）
        if not ref_price or ref_price <= 0:
            return None, None, None
        bp_usd = float(bp)
        notional_cap = bp_usd * float(margin_rate)
        return int(notional_cap / float(ref_price)), bp_usd, float(margin_rate)
    except Exception as e:
        print(f"⚠️ 美股购买力查询失败 {symbol}: {e}", file=sys.stderr)
        return None, None, None


# ---------------------------------------------------------------------------
# 下单（开仓 MKT+附加止损 / 平仓 MKT / 独立止损 STP）——美股用 to_tiger_symbol_us
# ---------------------------------------------------------------------------

def _make_order_us(tc, config, symbol, action, order_type, quantity,
                   limit_price=None, aux_price=None, order_legs=None, outside_rth=True):
    """创建并提交订单（美股 symbol 用裸代码）。order_type 传字符串值 'LMT'/'MKT'/'STP'。

    outside_rth（2026-08-18 立规，美股交易窗口扩到盘前）：是否允许盘前盘后交易——
    老虎 create_order 原生参数（SDK 实测签名含 outside_rth，语义「是否允许盘前盘后
    交易(美股专属)」）。默认 True：本项目美股可交易时段 = 美东 04:00-16:00（盘前 +
    盘中），不传该参数时 SDK 默认 False、盘前下的单会被拒或挂到 09:30 开盘才成交，
    与「盘前可交易」矛盾。盘后（16:00 后）本来就不下单（时间闸拦），该标志对盘中
    单无影响，统一 True 无副作用。"""
    from tigeropen.common.consts import SecurityType
    contract = tc.get_contract(to_tiger_symbol_us(symbol), sec_type=SecurityType.STK)
    if contract is None:
        raise RuntimeError(f"查不到老虎美股合约 {symbol}（代码格式须裸代码如 MU）")
    order = tc.create_order(
        account=config.account,
        contract=contract,
        action=action,
        order_type=order_type,
        quantity=int(quantity),
        limit_price=limit_price,
        aux_price=aux_price,
        order_legs=order_legs,
        time_in_force="DAY",
        outside_rth=outside_rth,
    )
    if order is None:
        raise RuntimeError(f"创建订单对象失败 {symbol} {action} qty={quantity}")
    return tc.place_order(order)


def submit_order_with_stop_us(config, symbol, side, quantity, submitted_price,
                              stop_loss_price, profit_price=None, retries=3):
    """美股开仓：主单 LMT 限价单 + 附加腿一次提交——止损腿 OrderLeg('LOSS', stop_loss_price) +
    止盈腿 OrderLeg('PROFIT', profit_price)（2026-08-23 用户立双腿；profit_price=None 时退回单止损腿）。

    2026-08-23 主单改回限价单（用户立「下单必须用限价单」，与港股版同日对齐）：submitted_price
    即限价（调用方取盘口对价：做多取 ask 主动买；做空挂 max(bid, ask)，2026-08-25 随港股
    T122 同步改——美股无提价规则但同口径无副作用，取整到美股 tick 0.01）。
    ⚠️ 附加订单仅限价主单支持（SDK demo 注释明示），市价主单已禁用。
    双腿语义（SDK 源码 model.py + 官方帮助中心，2026-08-23 查证）：LOSS+PROFIT 齐发时
    attach_type='BRACKETS'（括号订单）——主单成交后系统自动监控，止损/止盈任一边触发成交、
    另一边自动作废；主单撤单则两腿同步取消。附加腿方向与触发语义由券商按主单方向自动定。
    美股腿可传 outside_rth（允许盘前盘后触发）；当前默认 DAY / 不限 outside_rth，待 paper 实测后按需开。

    ⚠️ 超时类模糊失败不自动重试（2026-08-16 修，同港股版）：请求已达券商但响应超时时，
    重试=真实重复下单路径。此类异常直接抛出并注明「须先查当日订单确认」。"""
    from tigeropen.trade.domain.order import OrderLeg
    last_err = None
    legs_desc = "止损+止盈双腿" if profit_price is not None else "止损单腿"
    for attempt in range(retries):
        try:
            tc = new_trade_client(config)
            action = "BUY" if side == "Buy" else "SELL"
            legs = [OrderLeg("LOSS", stop_loss_price)]
            if profit_price is not None:
                legs.append(OrderLeg("PROFIT", profit_price))
            return _make_order_us(tc, config, symbol, action, "LMT", quantity,
                                  limit_price=submitted_price, order_legs=legs)
        except Exception as e:
            last_err = e
            if T._is_ambiguous_timeout_error(e):
                raise RuntimeError(
                    f"美股开仓（LMT+{legs_desc}）提交超时（模糊失败，订单可能已在券商侧受理）"
                    f" {symbol} {side} qty={quantity} price={submitted_price} stop={stop_loss_price}"
                    f"{' profit=' + str(profit_price) if profit_price is not None else ''}: {e}"
                    f"——禁止盲目重试，须先查当日订单确认是否已成交，未确认前不得再下单") from e
            if attempt < retries - 1:
                time.sleep(0.5 * (attempt + 1))
                continue
    raise RuntimeError(
        f"美股开仓（LMT+{legs_desc}）提交失败 {symbol} {side} qty={quantity} "
        f"price={submitted_price} stop={stop_loss_price}: {last_err}"
    )


def submit_market_order_us(config, symbol, side, quantity, retries=3):
    """美股市价单 MKT。返回 order_id。

    ⚠️ 2026-08-23 用户立「下单必须用限价单」后本函数不再是平仓默认路径（平仓改走
    submit_limit_order_us 对价限价单）；函数保留供应急人工显式调用，AI 例行下单不得使用。

    超时类模糊失败不自动重试（2026-08-16，同港股版）。"""
    raise RuntimeError(
        "下单必须用限价单（2026-08-23 用户立）：市价单已禁用——平仓用 submit_limit_order_us"
        "（对价限价、取整 tick），确需市价单须经用户明确同意后临时改回")


def submit_limit_order_us(config, symbol, side, quantity, limit_price, retries=3):
    """美股限价单 LMT（平仓用，2026-08-23 用户立「下单必须用限价单」落地）。side: 'Buy' /
    'Sell'，limit_price 由调用方取整到美股 tick 0.01 后传入（平多挂 bid 主动卖、平空挂
    ask 主动买）。返回 order_id。

    与 MKT 的行为差异：限价单可能不成交（价格快速离开时）——调用方须有超时撤单 + 如实
    上报的处理，不得假设必成交。超时类模糊失败不自动重试（2026-08-16 同款：重试=真实
    重复下单路径，错误信息注明须先查订单确认）。"""
    last_err = None
    for attempt in range(retries):
        try:
            tc = new_trade_client(config)
            action = "BUY" if side == "Buy" else "SELL"
            return _make_order_us(tc, config, symbol, action, "LMT", quantity,
                                  limit_price=limit_price)
        except Exception as e:
            last_err = e
            if T._is_ambiguous_timeout_error(e):
                raise RuntimeError(
                    f"美股平仓 LMT 提交超时（模糊失败，订单可能已在券商侧受理）"
                    f" {symbol} {side} {quantity} @ {limit_price}: {e}"
                    f"——禁止盲目重试，须先查当日订单确认") from e
            if attempt < retries - 1:
                time.sleep(0.5 * (attempt + 1))
                continue
    raise RuntimeError(f"美股平仓 LMT 提交失败 {symbol} {side} {quantity} @ {limit_price}: {last_err}")
    last_err = None
    for attempt in range(retries):
        try:
            tc = new_trade_client(config)
            action = "BUY" if side == "Buy" else "SELL"
            return _make_order_us(tc, config, symbol, action, "MKT", quantity)
        except Exception as e:
            last_err = e
            if T._is_ambiguous_timeout_error(e):
                raise RuntimeError(
                    f"美股 MKT 提交超时（模糊失败，订单可能已在券商侧受理）"
                    f" {symbol} {side} {quantity}: {e}——禁止盲目重试，须先查当日订单确认") from e
            if attempt < retries - 1:
                time.sleep(0.5 * (attempt + 1))
                continue
    raise RuntimeError(f"美股 MKT 提交失败 {symbol} {side} {quantity}: {last_err}")


def submit_stop_order_us(config, symbol, side, quantity, trigger_price, retries=3):
    """美股独立止损单 STP（移损 fallback 用；aux_price=触发价）。返回 order_id。

    超时类模糊失败不自动重试（2026-08-16，同港股版——重试可能产生重复止损单）。"""
    last_err = None
    for attempt in range(retries):
        try:
            tc = new_trade_client(config)
            action = "BUY" if side == "Buy" else "SELL"
            return _make_order_us(tc, config, symbol, action, "STP", quantity,
                                  aux_price=trigger_price)
        except Exception as e:
            last_err = e
            if T._is_ambiguous_timeout_error(e):
                raise RuntimeError(
                    f"美股独立止损 STP 提交超时（模糊失败，订单可能已在券商侧受理）"
                    f" {symbol} {side} qty={quantity} trigger={trigger_price}: {e}"
                    f"——禁止盲目重试，须先查当日订单确认") from e
            if attempt < retries - 1:
                time.sleep(0.5 * (attempt + 1))
                continue
    raise RuntimeError(
        f"美股独立止损 STP 提交失败 {symbol} {side} qty={quantity} trigger={trigger_price}: {last_err}"
    )


# ---------------------------------------------------------------------------
# 成交回查 / 持仓 / 撤单（复用通用 + 美股 symbol 过滤）
# ---------------------------------------------------------------------------

# 成交回查（不分市场，复用港股——2026-08-16 起含部分成交严格匹配 / 轮询异常捕获）
check_order_filled_us = T.check_order_filled_tiger
# 已成交数量查询（部分成交复查，2026-08-16 立；不分市场复用）
get_order_filled_qty_us = T.get_order_filled_qty_tiger
# 撤单（不分市场，复用）
cancel_order_us = T.cancel_order_tiger


def has_active_open_order_us(config, symbol, side=None):
    """重复下单防抖检查（2026-08-16 立，同港股版）：该美股标的当日已有活动开仓方向
    委托单（非止损单）时返回 True。symbol 过滤用美股裸代码。返回 (has, order_ids)。"""
    tc = new_trade_client(config)
    target = to_tiger_symbol_us(symbol)
    active_ids = []
    for o in (tc.get_orders() or []):
        contract = getattr(o, "contract", None)
        order_sym = getattr(contract, "symbol", None) if contract else None
        if order_sym is None or str(order_sym) != target:
            continue
        raw_status = getattr(o, "status", "")
        status = raw_status.value if hasattr(raw_status, "value") else str(raw_status)
        if any(s in status for s in ("Filled", "Cancelled", "Inactive", "Invalid",
                                     "Expired", "PendingCancel")):
            continue
        raw_otype = getattr(o, "order_type", "")
        otype = (raw_otype.value if hasattr(raw_otype, "value") else str(raw_otype) or "")
        legs = getattr(o, "order_legs", None) or []
        is_stop = str(otype).upper() in ("STP", "STOP", "TRAIL") or any(
            str(getattr(leg, "leg_type", "")).upper() == "LOSS" for leg in legs)
        if is_stop:
            continue   # 止损单不算开仓委托
        action = str(getattr(o, "action", "")).upper()
        if side is not None:
            want = "BUY" if side == "Buy" else "SELL"
            if action != want:
                continue
        active_ids.append(getattr(o, "id", None) or getattr(o, "order_id", None))
    return (len(active_ids) > 0), active_ids


def get_today_orders_us(config):
    """查老虎当日订单列表（get_orders），美股侧（2026-08-17 立，多会话互斥闸门用）。

    与港股版 get_today_orders_tiger 同口径：返回订单对象列表或 []。开仓闸门
    （trade_mutex.py）以当日订单流判「开仓成交且无对应平仓」的在场敞口——与成交
    确认同数据源，规避持仓接口传播延迟窗口。
    """
    tc = new_trade_client(config)
    try:
        return tc.get_orders() or []
    except Exception as e:
        print(f"⚠️ 老虎当日订单查询失败: {e}", file=sys.stderr)
        # 与港股版（返回 [] 供采样降级）不同：开仓闸门口径须区分「查无订单」与「查询
        # 失败」——失败时返回 [] 会让闸门把该拒的放行，故向上抛、由调用方保守拒开。
        raise


def get_open_position_us(config, symbol=None):
    """查老虎美股持仓。返回 {'symbol','symbol_name','side','quantity','cost_price'} 或 None。

    只看美股持仓（symbol 含 '.' 转富途 US. 格式后过滤），不碰港股。quantity 正=多、负=空。"""
    tc = new_trade_client(config)
    try:
        positions = tc.get_positions() or []
        collected = []
        for p in positions:
            qty_f = float(getattr(p, "quantity", 0) or 0)
            if qty_f == 0:
                continue
            sym_raw = str(getattr(getattr(p, "contract", None), "symbol", ""))
            # 美股 symbol 是裸代码（MU），排除港股（5 位数字）
            if sym_raw.isdigit():
                continue
            side = "short" if qty_f < 0 else "long"
            collected.append((side, p, abs(qty_f), sym_raw))
        if not collected:
            return None
        if symbol is not None:
            target = to_tiger_symbol_us(symbol)
            matches = [(s, p, q, sr) for s, p, q, sr in collected if sr == target]
            if not matches:
                return None
            _, p, qty, sym_raw = matches[0]
        else:
            if len(collected) != 1:
                return None
            _, p, qty, sym_raw = collected[0]
        cost = getattr(p, "average_cost", None)
        name = getattr(getattr(p, "contract", None), "name", None)
        return {
            "symbol": to_futu_symbol_us(sym_raw),
            "symbol_name": name,
            "side": side,
            "quantity": int(qty),
            "cost_price": float(cost) if cost else None,
        }
    finally:
        pass


def cancel_all_stop_orders_us(config, symbol, exclude_order_id=None):
    """撤销指定美股标的的全部未触发止损单（STP/LOSS/TRAIL 等止损类型）。返回 (n, ids)。

    含 TRAIL 跟踪止损单（2026-08-05 中芯残留事故教训：cancel 只撤 STP/LOSS 漏 TRAIL，
    致 salable=0 平仓被拒）。"""
    tc = new_trade_client(config)
    try:
        target = to_tiger_symbol_us(symbol)
        orders = tc.get_orders() or []
        cancelled = []
        for order in orders:
            contract = getattr(order, "contract", None)
            order_sym = str(getattr(contract, "symbol", "") if contract else "")
            if order_sym != target:
                continue
            oid = getattr(order, "id", None) or getattr(order, "order_id", None)
            if oid is None:
                continue
            if exclude_order_id is not None and str(oid) == str(exclude_order_id):
                continue
            status_obj = getattr(order, "status", "")
            status_val = status_obj.value if hasattr(status_obj, "value") else str(status_obj)
            if any(s in status_val for s in ("Filled", "Cancelled", "Inactive", "Invalid",
                                              "Expired", "PendingCancel")):
                continue
            otype_obj = getattr(order, "order_type", "")
            otype_val = otype_obj.value if hasattr(otype_obj, "value") else str(otype_obj)
            legs = getattr(order, "order_legs", None) or []
            is_stop = ("STP" in otype_val.upper() or "STOP" in otype_val.upper()
                       or "TRAIL" in otype_val.upper()
                       or any(str(getattr(leg, "leg_type", "")).upper() == "LOSS" for leg in legs))
            if not is_stop:
                continue
            try:
                tc.cancel_order(id=oid)
                cancelled.append(oid)
            except Exception:
                pass
        return len(cancelled), cancelled
    finally:
        pass


# ---------------------------------------------------------------------------
# 价格范围 / 仓位计算 / 模式（复用 trade_utils_tiger 纯函数；真实费率走 fee_ctx /
# build_fee_ctx——2026-08-17 起固定平台费 + 美股按股结构，fee_ctx = {shares, sec_type,
# market}；旧 _fee_per_side 已随 2026-08-12 真实费率改造删除——此处别名同步删，
# 修复 import 即崩的存量破损）
# ---------------------------------------------------------------------------

_net_odds = T._net_odds
net_max_loss = T.net_max_loss          # 净 max_loss（2026-08-28 分母净口径，市场参数化、两市场通用）
calc_entry_range = T.calc_entry_range
check_price_in_range = T.check_price_in_range
calc_position_size = T.calc_position_size
parse_mode = T.parse_mode
# build_fee_ctx（2026-08-19 补）：开仓脚本 open_position_tiger_us.py 184 行调
# U.build_fee_ctx，但此前只删了旧别名没补新别名（2026-08-17 注释说「复用
# trade_utils_tiger 的 build_fee_ctx」却没真接上）——美股开仓一调即 AttributeError
# 崩溃（当日 MU 开仓实测）。纯属性打包、与市场无关（美股按股费率在 _net_odds
# 内按 market='US' 分支处理），直接转发港股版即可。
build_fee_ctx = T.build_fee_ctx
# _sec_type_of（2026-08-28 补）：开仓脚本 _enforce_explicit_quantity_risk 等三处调
# U._sec_type_of(symbol)，但 trade_utils_tiger_us 此前未转发该私有函数——美股显式
# 仓位开仓路径一进风控校验即 AttributeError 崩溃（当日 NVDA 开仓实测）。纯代码归一
# （HK.00700 / US.MU → STK）、与市场无关，直接转发港股版即可。
_sec_type_of = T._sec_type_of
# ring_after_fill（2026-08-28 补）：开仓脚本成交后调 U.ring_after_fill 响开仓铃，
# 但美股版未转发——订单已成交、仅响铃环节 AttributeError 崩（当日 NVDA 开仓实测，
# 成交回报已在日志里但脚本 exit 1）。纯提醒件、与市场无关，直接转发港股版。
ring_after_fill = T.ring_after_fill
# 持仓期间极值（平仓过程指标素材，2026-08-05 立）：复用港股实现——按 symbol.replace('.','_')
# 匹配盯盘 log（US.MU → monitor_log_US_MU_*_{mode}.csv）、逻辑与市场无关，返回
# (raw_high, raw_low) 或 None（无盯盘 log）。
calc_position_extremes_us = T.calc_position_extremes_tiger
