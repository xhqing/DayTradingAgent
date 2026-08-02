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

def _env_file(env_file=None):
    """长桥凭证文件路径：env_file 参数 > 环境变量 LONGBRIDGE_ENV_FILE > 默认 env-paper（模拟盘）。

    实盘扩展：调用方传 env_file，或设环境变量 LONGBRIDGE_ENV_FILE=实盘凭证路径，即可切换到实盘账户，无需改代码。
    """
    if env_file:
        return Path(env_file)
    env_env = os.environ.get("LONGBRIDGE_ENV_FILE")
    if env_env:
        return Path(env_env)
    return Path.home() / ".longbridge" / "openapi" / "env-paper"


def load_env_file(env_path=None):
    """从 ~/.longbridge/openapi/env-paper（或指定路径）加载环境变量。

    env 文件格式：export KEY=VALUE（每行），只加载 LONGPORT_ / LONGBRIDGE_ 前缀。
    LONGBRIDGE_ 自动映射为 LONGPORT_（SDK 需要 LONGPORT_ 前缀）。
    """
    env_path = _env_file(env_path)
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
    # 自动读 region-cache 设 LONGPORT_REGION（2026-07-31 立）：
    # SDK 默认连国际区 openapi.longportapp.com，国内被墙、直连超时（curl 直连 port 443 timeout、
    # 但走系统代理可达）。CLI 按 region-cache 选区域连中国区 openapi.longportapp.cn（直连即可达、
    # 无需代理），故这里读 region-cache 同步设 LONGPORT_REGION，让 SDK 也连对应区域。
    # 实测：不设 → connect timeout；设 LONGPORT_REGION=cn → 0.8s 连接成功。
    region_path = Path.home() / ".longbridge" / "openapi" / "region-cache"
    if region_path.exists():
        region = region_path.read_text().strip()
        if region:
            os.environ.setdefault("LONGPORT_REGION", region)
    return True


def load_config(env_file=None):
    """创建长桥 Config。凭证来源：env_file 参数 > 环境变量 LONGBRIDGE_ENV_FILE > 默认 env-paper（模拟盘）。
    实盘扩展：传 env_file=实盘凭证路径，或设环境变量 LONGBRIDGE_ENV_FILE。"""
    from longport.openapi import Config
    load_env_file(env_file)
    return Config.from_env()


# ---------------------------------------------------------------------------
# symbol 格式转换（项目用富途格式 US.MU / HK.02800，长桥 API 只认原生 MU.US / 2800.HK）
# 2026-08-01 修：长桥 quote/submit 不认富途格式（US.MU 返回空），必须转原生。
# ---------------------------------------------------------------------------

def to_lb_symbol(symbol):
    """富途 → 长桥原生：US.MU→MU.US，HK.02800→2800.HK（港股代码去前导 0）。"""
    if not symbol or "." not in symbol:
        return symbol
    market, code = symbol.split(".", 1)
    if market == "HK":
        code = str(int(code))
    return f"{code}.{market}"


def to_futu_symbol(lb_symbol):
    """长桥原生 → 富途：MU.US→US.MU，2800.HK→HK.02800（港股补前导 0 到 5 位）。"""
    if not lb_symbol or "." not in lb_symbol:
        return lb_symbol
    code, market = lb_symbol.split(".", 1)
    if market == "HK":
        code = code.zfill(5)
    return f"{market}.{code}"


def _key(symbol):
    """symbol 归一化比较键：富途格式 US.MU / HK.02800 与长桥原生 MU.US / 2800.HK 都归一到
    ``market.code``（港股代码去前导 0），供 get_open_position 匹配持仓用。

    2026-08-02 修：get_open_position 原调用 _key(symbol) / _key(p.symbol) 做持仓匹配，但
    _key 此前未定义，传 symbol 查指定标的持仓时 NameError（close_position 一键平仓依赖此）。
    """
    if not symbol or "." not in symbol:
        return symbol
    left, right = symbol.split(".", 1)
    if right in ("US", "HK"):       # 长桥原生 code.market（MU.US / 2800.HK）
        market, code = right, left
    elif left in ("US", "HK"):      # 富途 market.code（US.MU / HK.02800）
        market, code = left, right
    else:
        return symbol
    if market == "HK" and code.isdigit():
        code = str(int(code))       # 港股去前导 0（02800 → 2800）
    return f"{market}.{code}"


# ---------------------------------------------------------------------------
# 报价查询
# ---------------------------------------------------------------------------

def get_quote(config, symbol, retries=3, timeout=10):
    """获取标的最新报价。返回 dict：{last, bid, ask, high, low, turnover, ...}。

    symbol 格式：传入富途格式 HK.00981 / US.MU，内部自动转长桥原生 9988.HK / MU.US（长桥只认原生，2026-08-01 实测）。
    """
    from longport.openapi import QuoteContext
    for attempt in range(retries):
        try:
            qc = QuoteContext(config)
            quotes = qc.quote([to_lb_symbol(symbol)])
            pass  # QuoteContext 无 close()（2026-07-31 修，同 TradeContext），SDK 自动清理
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
                    symbol=to_lb_symbol(symbol),
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
        pass  # TradeContext 无 close()（2026-07-31 修：原 tc.close() 在 finally 抛 AttributeError 会破坏函数返回值）；SDK 自动清理连接，无需显式 close


def submit_stop_order(config, symbol, side, quantity, trigger_price, retries=3):
    """下止损市价单（MIT = Market-If-Touched，触发后市价成交）。返回 order_id 或抛异常。

    裸 MIT 止损单（不是 STOP_LOSS 封装，STOP_LOSS 仅开仓附加订单可用）——side 由调用方
    设定（做多止损 Sell、做空止损 Buy）、trigger_price 也由调用方设定；只有**触发方向
    （涨/跌触发）**由券商按 trigger_price 相对现价的位置自动判定、无需 trigger_direction
    （长桥 MIT 无此字段，2026-08-01 实测 + 2026-08-02 厘清）：
    - 做多止损 side=Sell、trigger 低于现价 → 价格跌到触发价时市价卖出平多。
    - 做空止损 side=Buy、trigger 高于现价 → 价格涨到触发价时市价买回平空。

    实测（2026-08-01 paper 账户盘后）：MIT Sell trigger=700（现价 823）、MIT Buy
    trigger=1000（现价 823），均不带 trigger_direction，提交后 trigger_status=Active
    （监控等待），side 与触发方向均按传参生效、未误触发。

    ⚠️ 历史教训更正：2026-07-31 MU 事故的真因不是「MIT 默认方向错」，而是当时用
    order_type=MO（市价单）+ trigger_price 提交——MO 提交即按市价成交、trigger_price
    被忽略，做空止损 Buy 瞬间按市价买入平仓亏 $119。正确做法是用 order_type=MIT
    （触发单）而非 MO——MIT 等 trigger_price 触发才按调用方传的 side 市价成交（side 仍
    由调用方定、MIT 不自动定向）。故本函数直接用 SDK submit_order(order_type=MIT)，
    无需 REST、无需 trigger_direction（早期为补 trigger_direction 而写的 REST 签名封装已废弃）。

    side: 'Buy'（做空止损，涨触发买回）/ 'Sell'（做多止损，跌触发卖出）。
    """
    from longport.openapi import TradeContext, OrderType, OrderSide, TimeInForceType
    from decimal import Decimal
    side_enum = OrderSide.Buy if side == "Buy" else OrderSide.Sell
    tc = TradeContext(config)
    try:
        last_err = None
        for attempt in range(retries):
            try:
                resp = tc.submit_order(
                    symbol=to_lb_symbol(symbol),
                    order_type=OrderType.MIT,
                    side=side_enum,
                    submitted_quantity=int(quantity),
                    time_in_force=TimeInForceType.Day,
                    trigger_price=Decimal(str(trigger_price)),
                )
                return resp.order_id
            except Exception as e:
                last_err = e
                if attempt < retries - 1:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise RuntimeError(
                    f"止损单提交失败 {symbol} {side} qty={quantity} trigger={trigger_price}: {e}"
                ) from e
    finally:
        pass  # TradeContext 无 close()，SDK 自动清理连接


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
                    symbol=to_lb_symbol(symbol),
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
        pass  # TradeContext 无 close()（2026-07-31 修：原 tc.close() 在 finally 抛 AttributeError 会破坏函数返回值）；SDK 自动清理连接，无需显式 close


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
    high = recent_high or quote.get("high")
    low = recent_low or quote.get("low")

    # 判断振幅：需有效 high/low（长桥 get_quote 的 high/low 有时为 None）。
    # high/low 缺失时强制走限价（amplitude 设大值），避免市价单被拒——2026-07-31 修：
    # 原 high/low None → fallback last → amplitude 0 → 误用市价 → 第2笔做空 Sell MO 被拒。
    if high and low and high > low and last and last > 0:
        amplitude = (high - low) / last * 100
    else:
        amplitude = 100.0  # high/low 缺失 → 强制限价（更安全）
        high, low = last, last

    side_enum = OrderSide.Buy if side == "Buy" else OrderSide.Sell
    tc = TradeContext(config)

    try:
        # 振幅小（≤ 0.3%）→ 市价单，立即成交
        if amplitude <= 0.3:
            resp = tc.submit_order(
                symbol=to_lb_symbol(symbol),
                order_type=OrderType.MO,
                side=side_enum,
                submitted_quantity=int(quantity),
                time_in_force=TimeInForceType.Day,
            )
            order_id = resp.order_id
            time.sleep(1)
            # 查成交状态（2026-07-31 修：市价单可能被拒/未成交，原直接返回 last 冒充成交，
            # 导致 open_position 误以为开仓成功、后续 close 反向开仓——MU 事故持仓 158 多头）
            for o in tc.today_orders():
                if str(getattr(o, "order_id", None)) == str(order_id):
                    status = str(getattr(o, "status", "")).lower()
                    if "filled" not in status:
                        raise RuntimeError(
                            f"市价单 {symbol} {side} 未成交（status={status}），禁止冒充成交"
                        )
                    avg = getattr(o, "avg_fill_price", None)
                    return order_id, float(avg) if avg else last, "market"
            raise RuntimeError(f"市价单 {symbol} {side} 提交后查不到订单 {order_id}")

        # 振幅大（> 0.3%）→ 限价单，避免滑点
        if side == "Buy":
            limit_price = bid if (bid and bid > 0) else last
        else:
            limit_price = ask if (ask and ask > 0) else last
        # 限价取整到 tick（2026-07-31 修）：长桥限价单价格必须合 tick，否则报 602035 Wrong bid size。
        # 美股 tick 0.01；港股价位规则随价格区间变化（暂 round 3 位近似，后续可接富途 price_quote_unit）。
        if ".US" in symbol:
            limit_price = round(limit_price, 2)

        resp = tc.submit_order(
            symbol=to_lb_symbol(symbol),
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
            symbol=to_lb_symbol(symbol),
            order_type=OrderType.MO,
            side=side_enum,
            submitted_quantity=int(quantity),
            time_in_force=TimeInForceType.Day,
        )
        return resp2.order_id, last, "market"

    finally:
        pass  # TradeContext 无 close()（2026-07-31 修：原 tc.close() 在 finally 抛 AttributeError 会破坏函数返回值）；SDK 自动清理连接，无需显式 close


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
        pass  # TradeContext 无 close()（2026-07-31 修：原 tc.close() 在 finally 抛 AttributeError 会破坏函数返回值）；SDK 自动清理连接，无需显式 close


def get_stock_positions(config):
    """查询持仓。返回持仓列表。"""
    from longport.openapi import TradeContext
    tc = TradeContext(config)
    try:
        return tc.stock_positions()
    finally:
        pass  # TradeContext 无 close()（2026-07-31 修：原 tc.close() 在 finally 抛 AttributeError 会破坏函数返回值）；SDK 自动清理连接，无需显式 close


def cancel_order(config, order_id):
    """取消订单。"""
    from longport.openapi import TradeContext
    tc = TradeContext(config)
    try:
        tc.cancel_order(order_id)
    finally:
        pass  # TradeContext 无 close()（2026-07-31 修：原 tc.close() 在 finally 抛 AttributeError 会破坏函数返回值）；SDK 自动清理连接，无需显式 close


def cancel_all_stop_orders(config, symbol, exclude_order_id=None):
    """撤销指定标的的所有未触发止损条件单。

    平仓后必须调用——残留止损单在账户变空仓后若被价格触发，会被长桥接受、反向开仓
    （实测：完全空仓时卖单被接受 NotReported、成交即开空）。查询当日所有订单，
    筛选出该标的的待执行止损单并逐个撤销。

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
            if to_futu_symbol(order_sym) != to_futu_symbol(symbol):
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
            if exclude_order_id is not None and str(order_id) == str(exclude_order_id):
                continue  # 跳过刚下的新止损（移损"先新增后撤旧"顺序，保新止损）
            try:
                tc.cancel_order(order_id)
                cancelled.append(order_id)
            except Exception:
                pass  # 单个撤销失败不影响其他
        return len(cancelled), cancelled
    finally:
        pass  # TradeContext 无 close()（2026-07-31 修：原 tc.close() 在 finally 抛 AttributeError 会破坏函数返回值）；SDK 自动清理连接，无需显式 close


# ---------------------------------------------------------------------------
# 成交回查 + 持仓读取（附加订单开仓回查 / 一键平仓用）
# ---------------------------------------------------------------------------

def check_order_filled(config, order_id, timeout=8, poll_interval=2):
    """轮询查询订单成交状态（附加订单开仓后回查主单成交）。

    返回 (filled, fill_price, status_str)：
    - filled: 已成交（含部分成交）为 True
    - fill_price: 成交均价 avg_fill_price（未成交或缺失为 None）
    - status_str: 订单状态小写字符串

    2026-08-01 立：附加订单开仓后必须实查主单成交状态，禁止用 quote.last 冒充
    成交价——2026-07-31 MU 事故即 smart_order 市价单被拒却冒充成交 → 反向开仓。
    """
    from longport.openapi import TradeContext
    tc = TradeContext(config)
    try:
        deadline = time.time() + timeout
        last_status = ""
        while time.time() < deadline:
            for o in tc.today_orders():
                if str(getattr(o, "order_id", "")) != str(order_id):
                    continue
                status = str(getattr(o, "status", "")).lower()
                last_status = status
                avg = getattr(o, "avg_fill_price", None)
                if "filled" in status:
                    return True, (float(avg) if avg else None), status
                if any(s in status for s in ("cancelled", "expired", "dead", "rejected")):
                    return False, None, status
                break  # 已定位订单但未成交，继续等
            time.sleep(poll_interval)
        return False, None, last_status or "timeout"
    finally:
        pass  # TradeContext 无 close()，SDK 自动清理


def get_open_position(config, symbol=None):
    """查询当前持仓，供一键平仓自动读方向 + 量。

    返回 {'symbol','symbol_name','side','quantity','cost_price'} 或 None。
    - symbol 指定：返回该标的持仓（兼容富途 US.MU 与长桥 MU.US 两种格式）。
    - symbol 为 None：账户恰好一个持仓则返回它，多个或无则 None。

    side 判定：channel.position_side 优先（paper 账户可能为 None）；
    其次 sold_quantity>0 → short；否则 quantity>0 → long。本项目做空走反向 ETF，
    账户层均为多头，故默认 long。
    """
    from longport.openapi import TradeContext

    tc = TradeContext(config)
    try:
        resp = tc.stock_positions()
        collected = []
        for ch in resp.channels:
            ch_side = getattr(ch, "position_side", None)
            for p in ch.positions:
                qty = getattr(p, "quantity", None)
                sold = getattr(p, "sold_quantity", None)
                qty_f = float(qty) if qty is not None else 0.0
                sold_f = float(sold) if sold is not None else 0.0
                if qty_f <= 0 and sold_f <= 0:
                    continue
                if ch_side is not None:
                    side = "short" if "short" in str(ch_side).lower() else "long"
                else:
                    side = "short" if sold_f > 0 else "long"
                collected.append((side, p))
        if not collected:
            return None
        if symbol is not None:
            target = _key(symbol)
            matches = [(s, p) for s, p in collected if _key(getattr(p, "symbol", "")) == target]
            if not matches:
                return None
            _, p = matches[0]
        else:
            if len(collected) != 1:
                return None
            _, p = collected[0]
        p_qty = getattr(p, "quantity", None)
        p_sold = getattr(p, "sold_quantity", None)
        p_qty_f = float(p_qty) if p_qty is not None else 0.0
        p_sold_f = float(p_sold) if p_sold is not None else 0.0
        cost = getattr(p, "cost_price", None)
        return {
            "symbol": to_futu_symbol(getattr(p, "symbol", None)),
            "symbol_name": getattr(p, "symbol_name", None),
            "side": side,
            "quantity": int(p_qty_f if p_qty_f > 0 else p_sold_f),
            "cost_price": float(cost) if cost is not None else None,
        }
    finally:
        pass  # TradeContext 无 close()，SDK 自动清理


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

def parse_mode(argv=None):
    """从命令行参数解析执行模式 --mode（auto / signal），默认 auto。

    支持两种写法：`--mode signal` 或 `--mode=signal`。不传 --mode 或值非法时返回 'auto'
    （与 trade skill 默认自动交易模式一致）。脚本 main 里 `mode = parse_mode(sys.argv[1:])`。
    """
    import sys
    if argv is None:
        argv = sys.argv[1:]
    for i, a in enumerate(argv):
        if a == "--mode" and i + 1 < len(argv):
            m = argv[i + 1]
            return m if m in ("auto", "signal") else "auto"
        if a.startswith("--mode="):
            m = a.split("=", 1)[1]
            return m if m in ("auto", "signal") else "auto"
    return "auto"


def load_equity(mode='auto', project_root=None, env_file=None):
    """按执行模式取当前 equity，返回 (equity, currency, source_str)。

    - mode='auto'（默认）：长桥账户 API account_balance().net_assets 优先取真实总资产；
      查询失败 fallback signals/equity-log.csv（标记非真实、需修复）。
    - mode='signal'：直接读 signals/equity-log.csv 末行 equity_after（signal 模式不连账户、
      靠累加值；无记录返回 config.risk.initial_equity）。

    auto 模式 equity 必须是账户真实总资产（2026-07-31 用户立）；signal 模式因不碰账户、用
    equity-log 累加假设盈亏（2026-08-01 双模式重构立，见 signal-mode.md「signal 模式权益更新」）。
    """
    import csv, json
    if project_root is None:
        # trade_utils.py 在 .claude/skills/trade/scripts/，上五级 = 项目根（signals/equity-log.csv 在项目根 signals/）
        project_root = Path(__file__).resolve().parent.parent.parent.parent.parent
    # config.json 在 skill 根目录（scripts 上一级 = trade/）
    config_path = Path(__file__).resolve().parent.parent / "config.json"
    initial_equity = 100000.0
    currency = "HKD"
    try:
        with open(config_path) as f:
            risk = json.load(f).get("risk", {})
        initial_equity = float(risk.get("initial_equity", 100000))
        currency = risk.get("equity_currency", "HKD")
    except Exception:
        pass

    def _read_equity_log():
        log_path = Path(project_root) / "signals" / "equity-log.csv"
        if not log_path.exists():
            return None
        with open(log_path) as f:
            rows = [r for r in csv.DictReader(f) if not (r.get("date") or "").startswith("#")]
        if not rows:
            return None
        return float(rows[-1]["equity_after"])

    if mode == "signal":
        eq = _read_equity_log()
        if eq is None:
            return initial_equity, currency, f"config initial_equity={initial_equity:.0f}（signal 模式、equity-log 无记录）"
        return eq, currency, "signals/equity-log.csv 末行（signal 模式累加值）"

    # mode == 'auto'：长桥账户 API 优先取真实总资产
    try:
        from longport.openapi import TradeContext
        tc = TradeContext(load_config(env_file))
        for b in tc.account_balance():
            return float(b.net_assets), str(getattr(b, "currency", None) or currency), "长桥账户 account_balance().net_assets（真实总资产）"
    except Exception as le:
        eq = _read_equity_log()
        if eq is not None:
            return eq, currency, f"equity-log.csv 末行（⚠️长桥账户查询失败 {le}，旧手动累加值、非真实，需修复）"
        return initial_equity, currency, f"config initial_equity={initial_equity:.0f}（⚠️长桥查询失败且 equity-log 无记录，占位非真实）"


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


def submit_order_with_stop(symbol, side, quantity, submitted_price, stop_loss_price, retries=3):
    """REST 提交主单(LO 开仓)+附加止损单(STOP_LOSS MIT，主单成交后才激活附加止损)。

    2026-07-31 用户新规 + 2026-08-02 厘清：开仓订单附加止损市价单——把开仓 LO 与止损单
    打包一次提交（主单 = 开仓 LO、附加 = STOP_LOSS 止损单，主单成交才激活附加、主单撤
    则附加自动撤）。优点：① 开仓失败止损不残留（附加随主单）；② STOP_LOSS 的方向与触发
    符号由券商后端按主单方向自动定（与主单相反），无需传 side / trigger_direction。
    SDK submit_order 不支持 attached_params 故走 REST（见 auto-mode.md「订单类型 vs 附加订单」）。

    side: 'Buy'(做多开仓) / 'Sell'(做空开仓)。附加止损方向由 STOP_LOSS 语义自动定（做多跌触发卖、做空涨触发买）。
    返回 order_id 或抛异常。
    """
    import hmac as _hmac, hashlib as _hl, json as _json, time as _time
    from pathlib import Path as _Path
    import requests as _req
    env_path = _env_file(None)
    creds = {}
    with open(env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line.startswith("export "):
                _line = _line[7:]
            if "=" not in _line or _line.startswith("#"):
                continue
            _k, _, _v = _line.partition("=")
            _k = _k.strip(); _v = _v.strip().strip('"').strip("'")
            if _k.endswith("APP_KEY"):
                creds["app_key"] = _v
            elif _k.endswith("APP_SECRET"):
                creds["app_secret"] = _v
            elif _k.endswith("ACCESS_TOKEN"):
                creds["access_token"] = _v
    region_path = _Path.home() / ".longbridge" / "openapi" / "region-cache"
    region = region_path.read_text().strip() if region_path.exists() else ""
    host = "https://openapi.longbridge.cn" if region == "cn" else "https://openapi.longbridge.com"

    def _sha1(b):
        return _hl.sha1(b if isinstance(b, bytes) else b.encode()).hexdigest()

    def _hmac_sha256(key, msg):
        return _hmac.new(key.encode(), msg.encode(), _hl.sha256).hexdigest()

    def _sign(method, path, query, body_str, ts):
        sh = "authorization;x-api-key;x-timestamp"
        sv = f"authorization:{creds['access_token']}\nx-api-key:{creds['app_key']}\nx-timestamp:{ts}\n"
        sts = f"{method}|{path}|{query}|{sv}|{sh}|"
        sts += _sha1(body_str) if body_str else ""
        sts = "HMAC-SHA256|" + _sha1(sts)
        sig = _hmac_sha256(creds["app_secret"], sts)
        return f"HMAC-SHA256 SignedHeaders={sh}, Signature={sig}"

    body_obj = {
        "symbol": to_lb_symbol(symbol),
        "order_type": "LO",
        "side": side,
        "submitted_quantity": str(int(quantity)),
        "submitted_price": str(submitted_price),
        "time_in_force": "Day",
        "attached_params": {
            "attached_order_type": "STOP_LOSS",
            "stop_loss_price": str(stop_loss_price),
            "activate_order_type": "MIT",
            "time_in_force": "GTC",
        },
    }
    path = "/v1/trade/order"
    last_err = None
    for attempt in range(retries):
        try:
            ts = str(int(_time.time()))
            body = _json.dumps(body_obj)
            sig = _sign("POST", path, "", body, ts)
            headers = {
                "X-Api-Key": creds["app_key"], "Authorization": creds["access_token"],
                "X-Timestamp": ts, "X-Api-Signature": sig, "Content-Type": "application/json",
            }
            resp = _req.post(f"{host}{path}", data=body, headers=headers, timeout=15)
            data = resp.json()
            if resp.status_code == 200 and data.get("code") == 0:
                return data["data"]["order_id"]
            last_err = f"HTTP {resp.status_code} code={data.get('code')} msg={data.get('message')}"
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
        _time.sleep(0.5 * (attempt + 1))
    raise RuntimeError(
        f"REST 附加订单提交失败 {symbol} {side} qty={quantity} price={submitted_price} stop={stop_loss_price}: {last_err}"
    )
