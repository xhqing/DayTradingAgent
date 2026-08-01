#!/usr/bin/env python3
"""交易工具库。

封装券商 API 的常用操作：配置加载、报价查询、下单、止损单、
持仓查询、待执行订单查询。供 3 类动作脚本（open_position / close_position / move_stop）共用。

配置来源（按优先级）：
  1. 环境变量 LONGPORT_APP_KEY / LONGPORT_APP_SECRET / LONGPORT_ACCESS_TOKEN
  2. ~/.longbridge/openapi/env-paper（模拟盘）自动加载（load_env_file）

用法：
  from trade_utils import load_config, get_quote, submit_limit_order, ...
"""

import os
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# 配置加载
# ---------------------------------------------------------------------------

def load_env_file(env_path=None):
    """从 ~/.longbridge/openapi/env-paper（或指定路径）加载环境变量。

    env 文件格式：export KEY=VALUE（每行），只加载 LONGPORT_ / LONGBRIDGE_ 前缀。
    LONGBRIDGE_ 自动映射为 LONGPORT_（SDK 需要 LONGPORT_ 前缀）。
    """
    if env_path is None:
        env_path = Path.home() / ".longbridge" / "openapi" / "env-paper"
    env_path = Path(env_path)
    if not env_path.exists():
        return False
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            # 去掉 export 前缀
            if line.startswith("export "):
                line = line[len("export "):]
            if "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            # LONGBRIDGE_ → LONGPORT_ 映射
            if key.startswith("LONGBRIDGE_"):
                lp_key = "LONGPORT_" + key[len("LONGBRIDGE_"):]
                os.environ.setdefault(lp_key, val)
            if key.startswith("LONGPORT_"):
                os.environ.setdefault(key, val)
    return True


def load_config():
    """创建 longport Config 对象。自动从 env 文件 + 环境变量加载凭证。"""
    from longport.openapi import Config
    # 先尝试加载 env 文件（如果环境变量已设则 setdefault 不覆盖）
    load_env_file()
    return Config.from_env()


# ---------------------------------------------------------------------------
# 报价查询
# ---------------------------------------------------------------------------

def get_quote(config, symbol, retries=3, timeout=10):
    """获取标的最新报价。返回 dict：{last, bid, ask, high, low, turnover, ...}。

    symbol 格式：HK.00981 / US.MU（富途格式，长桥也兼容）。
    """
    from longport.openapi import QuoteContext
    for attempt in range(retries):
        try:
            qc = QuoteContext(config)
            quotes = qc.quote([symbol])
            qc.close()
            if not quotes:
                return None
            q = quotes[0]
            return {
                "symbol": symbol,
                "last": float(q.last_done),
                "bid": float(q.bid) if hasattr(q, "bid") and q.bid else None,
                "ask": float(q.ask) if hasattr(q, "ask") and q.ask else None,
                "high": float(q.high) if hasattr(q, "high") and q.high else None,
                "low": float(q.low) if hasattr(q, "low") and q.low else None,
                "volume": int(q.volume) if hasattr(q, "volume") else 0,
                "turnover": float(q.turnover) if hasattr(q, "turnover") else 0,
            }
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(1)
                continue
            raise RuntimeError(f"获取报价失败 {symbol}: {e}") from e
    return None


# ---------------------------------------------------------------------------
# 下单
# ---------------------------------------------------------------------------

def submit_limit_order(config, symbol, side, quantity, price, retries=3):
    """下限价单。返回 (order_id, final_price) 或抛异常。

    side: 'Buy' / 'Sell'
    """
    from longport.openapi import (
        TradeContext, OrderType, OrderSide, TimeInForceType,
    )
    side_enum = OrderSide.Buy if side == "Buy" else OrderSide.Sell
    tc = TradeContext(config)
    try:
        for attempt in range(retries):
            try:
                resp = tc.submit_order(
                    symbol=symbol,
                    order_type=OrderType.LO,
                    side=side_enum,
                    submitted_quantity=int(quantity),
                    submitted_price=price,
                    time_in_force=TimeInForceType.Day,
                )
                return resp.order_id, price
            except Exception as e:
                if attempt < retries - 1:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise RuntimeError(f"限价单提交失败 {symbol} {side} {quantity}@{price}: {e}") from e
    finally:
        tc.close()


def submit_stop_order(config, symbol, side, quantity, trigger_price, retries=3):
    """下止损条件单（STOP 单）。返回 order_id 或抛异常。

    当价格触及 trigger_price 时以市价执行。
    ⚠️ 止损单必须用市价单（MO），禁止用限价单（LO/STP_LMT）：
    触发价就是目标止损价，允许小范围偏差；限价止损单的触发价和委托价难以合理设置，
    委托价设成触发价容易止损失败（价格跳空穿过委托价时无法成交）。
    side: 'Buy' / 'Sell'（做多止损 = Sell，做空止损 = Buy）
    """
    from longport.openapi import (
        TradeContext, OrderType, OrderSide, TimeInForceType,
    )
    side_enum = OrderSide.Buy if side == "Buy" else OrderSide.Sell
    tc = TradeContext(config)
    try:
        for attempt in range(retries):
            try:
                resp = tc.submit_order(
                    symbol=symbol,
                    order_type=OrderType.MO,
                    side=side_enum,
                    submitted_quantity=int(quantity),
                    trigger_price=trigger_price,
                    time_in_force=TimeInForceType.Day,
                )
                return resp.order_id
            except Exception as e:
                if attempt < retries - 1:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise RuntimeError(
                    f"止损单提交失败 {symbol} {side} qty={quantity} trigger={trigger_price}: {e}"
                ) from e
    finally:
        tc.close()


def submit_market_order(config, symbol, side, quantity, retries=3):
    """下市价单。返回 order_id 或抛异常。"""
    from longport.openapi import (
        TradeContext, OrderType, OrderSide, TimeInForceType,
    )
    side_enum = OrderSide.Buy if side == "Buy" else OrderSide.Sell
    tc = TradeContext(config)
    try:
        for attempt in range(retries):
            try:
                resp = tc.submit_order(
                    symbol=symbol,
                    order_type=OrderType.MO,
                    side=side_enum,
                    submitted_quantity=int(quantity),
                    time_in_force=TimeInForceType.Day,
                )
                return resp.order_id
            except Exception as e:
                if attempt < retries - 1:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise RuntimeError(f"市价单提交失败 {symbol} {side} {quantity}: {e}") from e
    finally:
        tc.close()


# ---------------------------------------------------------------------------
# 智能下单（目标：成交价尽可能最优）
# ---------------------------------------------------------------------------

def smart_order(config, symbol, side, quantity, quote, recent_high=None, recent_low=None, wait_sec=5):
    """智能下单：根据价格变化速度和幅度选择下单方式，目标最优成交价。

    判断逻辑：
    - 价格变化快、幅度大（近期 high/low 振幅 > 0.3%）→ 限价单，避免滑点过大
    - 价格变化慢、幅度小（振幅 ≤ 0.3%）→ 市价单，立即成交，允许微小滑点

    限价单策略：
    - 买入：限价挂在 bid（买一），比 ask 更优
    - 卖出：限价挂在 ask（卖一），比 bid 更优
    - 等 wait_sec 秒看是否成交，未成交则改市价单

    参数：
      config: longport Config
      symbol: 标的代码
      side: 'Buy' / 'Sell'
      quantity: 数量
      quote: get_quote 返回的 dict（含 bid, ask, last）
      recent_high: 近期最高价（可选，用于判断振幅；为 None 则从 quote 的 high 取）
      recent_low: 近期最低价（可选；为 None 则从 quote 的 low 取）
      wait_sec: 限价单等待秒数（默认 5 秒）

    返回 (order_id, fill_price, method)：
      order_id: 成交订单 ID
      fill_price: 成交价（限价或市价）
      method: 'limit' 或 'market'
    """
    from longport.openapi import TradeContext, OrderType, OrderSide, TimeInForceType

    bid = quote.get("bid")
    ask = quote.get("ask")
    last = quote.get("last")
    high = recent_high or quote.get("high") or last
    low = recent_low or quote.get("low") or last

    # 判断振幅：(high - low) / last × 100%
    amplitude = (high - low) / last * 100 if last and last > 0 else 0

    side_enum = OrderSide.Buy if side == "Buy" else OrderSide.Sell
    tc = TradeContext(config)

    try:
        # 振幅小（≤ 0.3%）→ 市价单，立即成交
        if amplitude <= 0.3:
            resp = tc.submit_order(
                symbol=symbol,
                order_type=OrderType.MO,
                side=side_enum,
                submitted_quantity=int(quantity),
                time_in_force=TimeInForceType.Day,
            )
            return resp.order_id, last, "market"

        # 振幅大（> 0.3%）→ 限价单，避免滑点
        if side == "Buy":
            limit_price = bid if (bid and bid > 0) else last
        else:
            limit_price = ask if (ask and ask > 0) else last

        resp = tc.submit_order(
            symbol=symbol,
            order_type=OrderType.LO,
            side=side_enum,
            submitted_quantity=int(quantity),
            submitted_price=limit_price,
            time_in_force=TimeInForceType.Day,
        )
        order_id = resp.order_id

        # 等待成交
        time.sleep(wait_sec)

        # 检查是否已成交
        orders = tc.today_orders()
        for o in orders:
            oid = getattr(o, "order_id", None)
            if oid == order_id:
                status = str(getattr(o, "status", "")).lower()
                if "filled" in status:
                    avg = getattr(o, "avg_fill_price", None)
                    return order_id, float(avg) if avg else limit_price, "limit"
                break

        # 未成交，撤销限价单，改市价单
        try:
            tc.cancel_order(order_id)
        except Exception:
            pass

        resp2 = tc.submit_order(
            symbol=symbol,
            order_type=OrderType.MO,
            side=side_enum,
            submitted_quantity=int(quantity),
            time_in_force=TimeInForceType.Day,
        )
        return resp2.order_id, last, "market"

    finally:
        tc.close()


# ---------------------------------------------------------------------------
# 订单管理
# ---------------------------------------------------------------------------

def get_today_orders(config):
    """查询当日所有订单（含条件单）。返回列表。"""
    from longport.openapi import TradeContext
    tc = TradeContext(config)
    try:
        return tc.today_orders()
    finally:
        tc.close()


def get_stock_positions(config):
    """查询持仓。返回持仓列表。"""
    from longport.openapi import TradeContext
    tc = TradeContext(config)
    try:
        return tc.stock_positions()
    finally:
        tc.close()


def cancel_order(config, order_id):
    """取消订单。"""
    from longport.openapi import TradeContext
    tc = TradeContext(config)
    try:
        tc.cancel_order(order_id)
    finally:
        tc.close()


def cancel_all_stop_orders(config, symbol):
    """撤销指定标的的所有未触发止损条件单。

    平仓后必须调用——不撤的止损单会在价格触及触发价时意外执行，
    产生反向持仓。查询当日所有订单，筛选出该标的的待执行止损单并逐个撤销。

    返回 (cancelled_count, cancelled_ids)。
    """
    from longport.openapi import TradeContext
    tc = TradeContext(config)
    try:
        orders = tc.today_orders()
        cancelled = []
        for order in orders:
            # 匹配标的
            order_sym = getattr(order, "symbol", None)
            if order_sym is None:
                continue
            if not (order_sym == symbol or order_sym == symbol.split(".")[-1]):
                continue
            # 筛选止损条件单（有 trigger_price 且未触发）
            trigger = getattr(order, "trigger_price", None)
            if trigger is None or float(trigger) <= 0:
                continue
            status = getattr(order, "status", None)
            # 跳过已成交 / 已撤销 / 已过期的订单
            status_str = str(status).lower() if status else ""
            if any(s in status_str for s in ("filled", "cancelled", "expired", "dead")):
                continue
            order_id = getattr(order, "order_id", None)
            if order_id is None:
                continue
            try:
                tc.cancel_order(order_id)
                cancelled.append(order_id)
            except Exception:
                pass  # 单个撤销失败不影响其他
        return len(cancelled), cancelled
    finally:
        tc.close()


# ---------------------------------------------------------------------------
# 价格范围计算（6 要素核心逻辑）
# ---------------------------------------------------------------------------

def calc_entry_range(direction, entry_ref, stop_loss, target):
    """计算开仓/加仓的可接受价格范围（6 要素中的「价格范围」）。

    价格范围由两部分组成（不对称）：
    - 80% 部分：来自风险距离 R₀ = |entry_ref - stop_loss| 的 80%
    - 3/8 部分：来自参考价本身的 37.5%

    做多：[entry_ref - R₀ × 0.8, entry_ref + entry_ref × 3/8]
    做空：[entry_ref - entry_ref × 3/8, entry_ref + R₀ × 0.8]

    下单时修正预期赔率 = 止盈距离 ÷ 止损距离（用成交价计算），在价格范围内
    的修正预期赔率区间大致在 [0.6, 10] 之间。

    返回 (range_low, range_high, odds_at_ref)：
    - odds_at_ref：参考价处的初始预期赔率
    """
    R0 = abs(entry_ref - stop_loss)  # 参考价处的风险单位 R₀（下单前的基准）
    if R0 < 1e-9:
        raise ValueError("止损价与参考价相同，R₀=0，无法计算价格范围")

    if direction == "long":
        # 做多：[参考价 - 风险距离×80%, 参考价 + 参考价×3/8]
        range_low = entry_ref - R0 * 0.8
        range_high = entry_ref + entry_ref * 3.0 / 8.0
    elif direction == "short":
        # 做空：[参考价 - 参考价×3/8, 参考价 + 风险距离×80%]
        range_low = entry_ref - entry_ref * 3.0 / 8.0
        range_high = entry_ref + R0 * 0.8
    else:
        raise ValueError(f"direction 必须是 'long' 或 'short'，收到 '{direction}'")

    # 参考价处的初始预期赔率
    if direction == "long":
        odds_at_ref = (target - entry_ref) / R0
    else:
        odds_at_ref = (entry_ref - target) / R0

    return range_low, range_high, odds_at_ref


def check_price_in_range(direction, current_price, entry_ref, stop_loss, target):
    """检查当前价格是否在可接受的开仓/加仓价格范围内。

    返回 (in_range, range_low, range_high, odds_at_ref, odds_at_current)：
    - odds_at_ref：参考价处的初始预期赔率
    - odds_at_current：当前价处的修正预期赔率（若按此价成交）
    """
    range_low, range_high, odds_at_ref = calc_entry_range(direction, entry_ref, stop_loss, target)

    in_range = range_low <= current_price <= range_high

    # 当前价处的修正预期赔率（若按此价成交）
    if direction == "long":
        odds_at_current = (target - current_price) / (current_price - stop_loss) if current_price > stop_loss else float("inf")
    else:
        odds_at_current = (current_price - target) / (stop_loss - current_price) if stop_loss > current_price else float("inf")

    return in_range, range_low, range_high, odds_at_ref, odds_at_current


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------

def load_equity(project_root=None):
    """从 signals/equity-log.csv 读取最新 equity。不存在则返回 initial_equity=100000。"""
    import csv
    if project_root is None:
        # 脚本目录 = trade_utils.py 所在目录(scripts)，上四级 = 项目根
        project_root = Path(__file__).resolve().parent.parent.parent.parent
    log_path = Path(project_root) / "signals" / "equity-log.csv"
    if not log_path.exists():
        return 100000.0
    with open(log_path) as f:
        rows = list(csv.DictReader(f))
    if not rows:
        return 100000.0
    return float(rows[-1]["equity_after"])


def calc_position_size(equity, risk_fraction, f_max, stop_distance, lot_size=1):
    """计算仓位（股数）。

    按 config.risk 的参数：B = risk_fraction × equity，
    连续原始仓位 = B / stop_distance，再按 lot_size 离散化。
    选使 max_loss（= 仓位 × stop_distance）最接近 B 的离散档。
    f_max 为硬上限。

    返回 (shares, max_loss, budget_B)。
    """
    B = equity * risk_fraction
    max_loss_cap = equity * f_max
    raw = B / stop_distance if stop_distance > 0 else 0
    # 向下取整到 lot_size 的倍数
    base = int(raw // lot_size) * lot_size
    candidates = []
    for mult in [-1, 0, 1, 2]:
        s = base + mult * lot_size
        if s <= 0:
            continue
        ml = s * stop_distance
        if ml > max_loss_cap:
            continue
        candidates.append((s, ml))
    if not candidates:
        return 0, 0, B
    # 选 max_loss 最接近 B 的
    best = min(candidates, key=lambda x: abs(x[1] - B))
    return best[0], best[1], B
