#!/usr/bin/env python3
"""美股交易工具库（老虎证券开放平台，美股默认账户）。

2026-08-05 立：美股默认账户为老虎模拟账户（与港股同账户），专门为美股开发独立一套：
复用 trade_utils_tiger 的**不分市场**
基础设施（SDK 配置、撤单、费率、赔率、仓位纯函数、成交回查）+ 美股特定适配（symbol 裸代码、
lot 1、tick 0.01、USD 净值、附加止损 OrderLeg）。平仓走「modify 止损触发价=现价」无 race 路径
（同港股 close_position_tiger 2026-08-05 改造）。

✅ 实测状态（2026-08-05 美股盘中）：下单链路全链路已 paper 端到端实测通过（SPY 2 股小仓位：
开仓 LMT Filled @773.68 + 附加止损腿 LOSS 激活 → 移损 modify aux_price 770.61→771.71 验证成功
→ 平仓 modify 触发价=现价、止损单触发 MO Filled @773.44、持仓归零无残留止损单）。行情数据源
修复：老虎 TBNZ 账户**美股无行情权限**（get_stock_briefs 实测报 code=4 msg=4000 permission
denied US market），get_quote_us 改富途 OpenD 单源（美股行情只有富途可用）。

老虎美股关键差异（vs 港股）：
- **symbol = 裸代码**（MU / AAPL，2026-08-05 实测：MU.US / US.MU 报「don't support trading」，
  富途格式 US.MU → 取 '.' 后裸代码 MU）。
- **lot_size = 1**（美股 1 股/手，从 get_contract.lot_size 取、fallback 1）。
- **tick = 0.01**（美股统一最小报价单位、无价位表）。
- **币种 USD**：账户 currency=USD，equity 取 net_liquidation 直用（无需港股的 HKD 保守口径换算）。
- **费率 3bps/边**（_fee_per_side 按 US. 前缀判，复用 trade_utils_tiger）。
- 交易时段：美东 09:30-16:00（夏令时北京 21:30-次日 04:00 / 冬令时 22:30-次日 05:00）。
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
    """老虎 → 富途：MU → US.MU。"""
    code = str(tiger_symbol)
    if "." in code:
        code = code.split(".")[0]
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
    tick_sizes 则按价位表取（一般美股不用价位表、走 0.01）。"""
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


# ---------------------------------------------------------------------------
# 下单（开仓 LMT+附加止损 / 平仓 MKT / 独立止损 STP）——美股用 to_tiger_symbol_us
# ---------------------------------------------------------------------------

def _make_order_us(tc, config, symbol, action, order_type, quantity,
                   limit_price=None, aux_price=None, order_legs=None):
    """创建并提交订单（美股 symbol 用裸代码）。order_type 传字符串值 'LMT'/'MKT'/'STP'。"""
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
    )
    if order is None:
        raise RuntimeError(f"创建订单对象失败 {symbol} {action} qty={quantity}")
    return tc.place_order(order)


def submit_order_with_stop_us(config, symbol, side, quantity, submitted_price,
                              stop_loss_price, retries=3):
    """美股开仓：主单 MKT 市价单 + 附加止损腿 OrderLeg('LOSS', stop_loss_price)。返回全局订单 id。

    2026-08-07 主单改市价单（与港股版同日改造对齐）：高波动标的（MU 等）限价单 + 8 秒超时撤单
    极易错过成交（22:08 实测：MU 做空 LMT 挂 bid 859.27、现价快速下探 8 秒内未成交被撤，开仓失败），
    市价单立即成交、附加止损腿同一次提交无裸奔空窗。submitted_price 保留参数作参考（不再作限价）。
    附加止损腿方向与触发语义由券商按主单方向自动定（与港股一致）。"""
    from tigeropen.trade.domain.order import OrderLeg
    last_err = None
    for attempt in range(retries):
        try:
            tc = new_trade_client(config)
            action = "BUY" if side == "Buy" else "SELL"
            legs = [OrderLeg("LOSS", stop_loss_price)]
            return _make_order_us(tc, config, symbol, action, "MKT", quantity, order_legs=legs)
        except Exception as e:
            last_err = e
            if attempt < retries - 1:
                time.sleep(0.5 * (attempt + 1))
                continue
    raise RuntimeError(
        f"美股开仓（LMT+附加止损）提交失败 {symbol} {side} qty={quantity} "
        f"price={submitted_price} stop={stop_loss_price}: {last_err}"
    )


def submit_market_order_us(config, symbol, side, quantity, retries=3):
    """美股市价单 MKT（平仓 fallback 用）。返回 order_id。"""
    last_err = None
    for attempt in range(retries):
        try:
            tc = new_trade_client(config)
            action = "BUY" if side == "Buy" else "SELL"
            return _make_order_us(tc, config, symbol, action, "MKT", quantity)
        except Exception as e:
            last_err = e
            if attempt < retries - 1:
                time.sleep(0.5 * (attempt + 1))
                continue
    raise RuntimeError(f"美股 MKT 提交失败 {symbol} {side} {quantity}: {last_err}")


def submit_stop_order_us(config, symbol, side, quantity, trigger_price, retries=3):
    """美股独立止损单 STP（移损 fallback 用；aux_price=触发价）。返回 order_id。"""
    last_err = None
    for attempt in range(retries):
        try:
            tc = new_trade_client(config)
            action = "BUY" if side == "Buy" else "SELL"
            return _make_order_us(tc, config, symbol, action, "STP", quantity,
                                  aux_price=trigger_price)
        except Exception as e:
            last_err = e
            if attempt < retries - 1:
                time.sleep(0.5 * (attempt + 1))
                continue
    raise RuntimeError(
        f"美股独立止损 STP 提交失败 {symbol} {side} qty={quantity} trigger={trigger_price}: {last_err}"
    )


# ---------------------------------------------------------------------------
# 成交回查 / 持仓 / 撤单（复用通用 + 美股 symbol 过滤）
# ---------------------------------------------------------------------------

# 成交回查（不分市场，复用港股）
check_order_filled_us = T.check_order_filled_tiger
# 撤单（不分市场，复用）
cancel_order_us = T.cancel_order_tiger


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
# 价格范围 / 仓位计算 / 模式（复用 trade_utils_tiger 纯函数；_fee_per_side 已支持 US.）
# ---------------------------------------------------------------------------

_fee_per_side = T._fee_per_side              # 按 symbol 前缀判（US. → 3bps）
_net_odds = T._net_odds
calc_entry_range = T.calc_entry_range
check_price_in_range = T.check_price_in_range
calc_position_size = T.calc_position_size
parse_mode = T.parse_mode
# 持仓期间极值（平仓过程指标素材，2026-08-05 立）：复用港股实现——按 symbol.replace('.','_')
# 匹配盯盘 log（US.MU → monitor_log_US_MU_*_{mode}.csv）、逻辑与市场无关，返回
# (raw_high, raw_low) 或 None（无盯盘 log）。
calc_position_extremes_us = T.calc_position_extremes_tiger
