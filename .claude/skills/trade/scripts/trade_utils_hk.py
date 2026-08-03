#!/usr/bin/env python3
"""港股交易工具库（长桥模拟盘，备选账户）。

与美股 `trade_utils.py` **解耦**（分而治之，2026-08-01 用户立）：港股长桥单独一套，
不 import 美股模块、不影响美股代码。凭证复用同一长桥账户 `~/.longbridge/openapi/env-paper`
（该账户 HKD 计价、有港股权限，实测可查港股行情/lot_size）。

港股相对美股的差异点（本模块封装）：
- **symbol 格式**：项目用富途格式 `HK.02800`，长桥 API 只认原生 `2800.HK`（含去前导 0）。
- **每手股数 lot_size**：因标的而异（盈富 500、腾讯/阿里 100、中芯 500），从长桥 static_info 取，不硬编码。
- **价位 tick**：港交所价位表随价格区间变化（非固定 0.01），按 2025-08-04 调整版价位表取整。
- **币种**：账户与标的均 HKD，equity 直接取 net_assets（无需汇率换算）。

三个动作的订单类型（与美股规范一致）：
- 开仓：主单 LO + 附加 STOP_LOSS MIT（一次 REST 提交）。
- 移动止损：反向 MIT（SDK），撤旧再新增，量严格=持仓量。
- 平仓：MO（市价）+ 撤未触发 MIT 止损单（防反向开仓）。

✅ 实测状态（2026-08-03 paper 三动作实测通过，HK.00700 腾讯做多 100 股）：开仓 LO FILLED
@487.0 + REST 附加 STOP_LOSS 提交接受但**模拟盘不激活**（REST 查主单 trigger_status=NOT_USED、
不生成独立订单——模拟盘附加单静默无效，实盘前需验证）；移损 MIT @485 提交成功（SDK 状态
OrderStatus.VarietiesNotReported = 模拟盘「品种未上报」等待态，属活动单、可撤）；平仓 MO Filled
@487.6（长桥订单 avg_fill_price 取不到、last 兜底——观察点）+ 撤止损。修复 1 个 bug：
get_open_position_hk / cancel_all_stop_orders_hk 对传入的富途格式 symbol 误调 to_futu_symbol
（"HK.00700"→"00700.HK" 错乱）致持仓 / 止损匹配永远失败——已改为直接比较。
"""

import os
import sys
import time
from pathlib import Path


# ---------------------------------------------------------------------------
# 配置加载（自包含，复用 env-paper 凭证）
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
    """从 ~/.longbridge/openapi/env-paper 加载长桥凭证 + region。"""
    env_path = _env_file(env_path)
    if not env_path.exists():
        return False
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):]
            if "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key.startswith("LONGBRIDGE_"):
                os.environ.setdefault("LONGPORT_" + key[len("LONGBRIDGE_"):], val)
            if key.startswith("LONGPORT_"):
                os.environ.setdefault(key, val)
    region_path = Path.home() / ".longbridge" / "openapi" / "region-cache"
    if region_path.exists():
        region = region_path.read_text().strip()
        if region:
            os.environ.setdefault("LONGPORT_REGION", region)
    return True


def load_config(env_file=None):
    """创建长桥 Config（港股）。凭证来源：env_file 参数 > 环境变量 LONGBRIDGE_ENV_FILE > 默认 env-paper（模拟盘）。
    实盘扩展：传 env_file=实盘凭证路径，或设环境变量 LONGBRIDGE_ENV_FILE。"""
    from longport.openapi import Config
    load_env_file(env_file)
    return Config.from_env()


# ---------------------------------------------------------------------------
# symbol 格式转换（富途 HK.02800 ↔ 长桥 2800.HK）
# ---------------------------------------------------------------------------

def to_lb_symbol(symbol):
    """富途 → 长桥原生：HK.02800 → 2800.HK（港股代码去前导 0）。"""
    if not symbol or "." not in symbol:
        return symbol
    market, code = symbol.split(".", 1)
    if market == "HK":
        code = str(int(code))  # 02800 → 2800
    return f"{code}.{market}"


def to_futu_symbol(lb_symbol):
    """长桥原生 → 富途：2800.HK → HK.02800（港股补前导 0 到 5 位）。"""
    if not lb_symbol or "." not in lb_symbol:
        return lb_symbol
    code, market = lb_symbol.split(".", 1)
    if market == "HK":
        code = code.zfill(5)
    return f"{market}.{code}"


# ---------------------------------------------------------------------------
# 行情 / lot_size / tick
# ---------------------------------------------------------------------------

def get_quote_hk(config, symbol, retries=3):
    """港股最新报价。返回 dict {last, bid, ask, high, low, ...} 或 None。"""
    from longport.openapi import QuoteContext
    lb = to_lb_symbol(symbol)
    for attempt in range(retries):
        try:
            qc = QuoteContext(config)
            quotes = qc.quote([lb])
            if not quotes:
                return None
            q = quotes[0]
            def _f(v):
                return float(v) if (hasattr(v, "__float__") and v) else None
            return {
                "symbol": symbol,
                "last": _f(getattr(q, "last_done", None)),
                "bid": _f(getattr(q, "bid", None)),
                "ask": _f(getattr(q, "ask", None)),
                "high": _f(getattr(q, "high", None)),
                "low": _f(getattr(q, "low", None)),
                "volume": int(getattr(q, "volume", 0) or 0),
                "turnover": _f(getattr(q, "turnover", None)),
            }
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(1)
                continue
            raise RuntimeError(f"港股报价失败 {symbol}: {e}") from e
    return None


def get_lot_size_hk(config, symbol):
    """港股每手股数（从长桥 static_info 取）。返回 int 或 None。"""
    from longport.openapi import QuoteContext
    qc = QuoteContext(config)
    try:
        info = qc.static_info([to_lb_symbol(symbol)])
        if info:
            ls = getattr(info[0], "lot_size", None)
            if ls:
                return int(ls)
    except Exception as e:
        print(f"⚠️ 查 lot_size 失败 {symbol}: {e}", file=sys.stderr)
    return None


# 港交所最小报价单位（价位表），2025-08-04 调整版（源：富途/港交所）
# (价格上界 HKD, tick)；价格落在 (上一上界, 本上界] 用本 tick
_HK_TICK_TABLE = [
    (0.25, 0.001),
    (0.50, 0.005),
    (10.00, 0.010),
    (20.00, 0.010),   # 2025-08-04 从 0.020 下调为 0.010
    (100.00, 0.050),
    (200.00, 0.100),
    (500.00, 0.200),
    (1000.00, 0.500),
    (2000.00, 1.000),
    (5000.00, 2.000),
    (float("inf"), 5.000),
]


def get_tick_hk(price):
    """按价格查港股最小报价单位。"""
    if price is None or price <= 0:
        return 0.001
    for upper, tick in _HK_TICK_TABLE:
        if price <= upper:
            return tick
    return 5.000


def round_to_tick_hk(price, symbol=None):
    """把价格向下取整到港股 tick（限价单必须合 tick，否则报 602035）。"""
    tick = get_tick_hk(price)
    import math
    return round(math.floor(price / tick) * tick, 6)


# ---------------------------------------------------------------------------
# 权益（港股账户 HKD，直接取 net_assets）
# ---------------------------------------------------------------------------

def load_equity_hk(config):
    """港股 equity = 长桥账户 net_assets（HKD）。"""
    from longport.openapi import TradeContext
    tc = TradeContext(config)
    try:
        bal = tc.account_balance()
        for c in (bal if isinstance(bal, list) else [bal]):
            if str(getattr(c, "currency", "")) == "HKD":
                na = getattr(c, "net_assets", None)
                if na:
                    return float(na)
        # 没有 HKD 行则取第一行
        first = (bal if isinstance(bal, list) else [bal])[0]
        na = getattr(first, "net_assets", None)
        return float(na) if na else 0.0
    finally:
        pass


# ---------------------------------------------------------------------------
# 下单
# ---------------------------------------------------------------------------

def submit_market_order_hk(config, symbol, side, quantity, retries=3):
    """港股市价单 MO（平仓用）。返回 order_id。"""
    from longport.openapi import TradeContext, OrderType, OrderSide, TimeInForceType
    tc = TradeContext(config)
    side_enum = OrderSide.Buy if side == "Buy" else OrderSide.Sell
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
                raise RuntimeError(f"港股 MO 提交失败 {symbol} {side} {quantity}: {e}") from e
    finally:
        pass


def submit_stop_order_hk(config, symbol, side, quantity, trigger_price, retries=3):
    """港股止损市价单 MIT（移损用）。side 由调用方定（做多 Sell / 做空 Buy）；只有触发方向（涨/跌）由券商按 trigger_price 相对现价自动判定、无需 trigger_direction。"""
    from longport.openapi import TradeContext, OrderType, OrderSide, TimeInForceType
    from decimal import Decimal
    tc = TradeContext(config)
    side_enum = OrderSide.Buy if side == "Buy" else OrderSide.Sell
    try:
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
                if attempt < retries - 1:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise RuntimeError(f"港股 MIT 提交失败 {symbol} {side} {quantity}@{trigger_price}: {e}") from e
    finally:
        pass


def submit_order_with_stop_hk(symbol, side, quantity, submitted_price, stop_loss_price, retries=3):
    """港股开仓：REST 一次提交主单(LO) + 附加止损(STOP_LOSS MIT)。返回 order_id。

    港股主单 LO 价必须合 tick（调用方应先 round_to_tick_hk），否则报 602035。
    SDK submit_order 不支持 attached_params 故走 REST。
    """
    import hmac as _hmac, hashlib as _hl, json as _json, time as _time
    from pathlib import Path as _Path
    try:
        import requests as _req
    except ImportError as _e:
        raise RuntimeError("REST 附加订单需要 requests 库：pip install requests") from _e

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
        f"港股 REST 附加订单提交失败 {symbol} {side} qty={quantity} price={submitted_price} stop={stop_loss_price}: {last_err}"
    )


# ---------------------------------------------------------------------------
# 成交回查 / 持仓 / 撤单
# ---------------------------------------------------------------------------

def check_order_filled_hk(config, order_id, timeout=8, poll_interval=2):
    """轮询订单成交状态。返回 (filled, fill_price, status_str)。"""
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
                break
            time.sleep(poll_interval)
        return False, None, last_status or "timeout"
    finally:
        pass


def get_open_position_hk(config, symbol=None):
    """查港股持仓。返回 {symbol,symbol_name,side,quantity,cost_price} 或 None。

    side 判定：channel.position_side 优先；其次 sold_quantity>0→short；否则 long。
    （本项目做空走反向 ETF，账户层均为多头。）
    """
    from longport.openapi import TradeContext

    tc = TradeContext(config)
    try:
        resp = tc.stock_positions()
        collected = []
        for ch in resp.channels:
            ch_side = getattr(ch, "position_side", None)
            for p in ch.positions:
                if not str(getattr(p, "symbol", "")).endswith(".HK"):
                    continue  # 港股模块只管港股持仓，跳过美股等（同账户可能有美股）
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
            target = symbol  # 传入即富途格式（HK.00700）；collected 内已是 to_futu_symbol 转换后的富途格式，直接比
            matches = [(s, p) for s, p in collected if to_futu_symbol(getattr(p, "symbol", "")) == target]
            if not matches:
                return None
            _, p = matches[0]
        else:
            if len(collected) != 1:
                return None
            _, p = collected[0]
        p_qty = getattr(p, "quantity", None)
        cost = getattr(p, "cost_price", None)
        return {
            "symbol": to_futu_symbol(getattr(p, "symbol", None)),
            "symbol_name": getattr(p, "symbol_name", None),
            "side": side,
            "quantity": int(float(p_qty)) if p_qty else 0,
            "cost_price": float(cost) if cost else None,
        }
    finally:
        pass


def cancel_order_hk(config, order_id):
    """撤销订单。"""
    from longport.openapi import TradeContext
    tc = TradeContext(config)
    try:
        tc.cancel_order(order_id)
    finally:
        pass


def cancel_all_stop_orders_hk(config, symbol, exclude_order_id=None):
    """撤销指定港股标的的全部未触发 MIT 止损单（平仓后防反向开仓）。返回 (n, ids)。"""
    from longport.openapi import TradeContext
    tc = TradeContext(config)
    try:
        target = symbol  # 传入即富途格式（HK.00700）；订单 symbol（700.HK）经 to_futu_symbol 转后与其比较
        orders = tc.today_orders()
        cancelled = []
        for order in orders:
            order_sym = getattr(order, "symbol", None)
            if order_sym is None or to_futu_symbol(order_sym) != target:
                continue
            trigger = getattr(order, "trigger_price", None)
            if trigger is None or float(trigger) <= 0:
                continue
            status = str(getattr(order, "status", "")).lower()
            if any(s in status for s in ("filled", "cancelled", "expired", "dead")):
                continue
            order_id = getattr(order, "order_id", None)
            if order_id is None:
                continue
            if exclude_order_id is not None and str(order_id) == str(exclude_order_id):
                continue  # 跳过刚下的新止损
            try:
                tc.cancel_order(order_id)
                cancelled.append(order_id)
            except Exception:
                pass
        return len(cancelled), cancelled
    finally:
        pass


# ---------------------------------------------------------------------------
# 价格范围 / 仓位计算（纯函数，与美股同逻辑、独立实现以解耦）
# ---------------------------------------------------------------------------

def calc_entry_range(direction, entry_ref, stop_loss, target):
    """开仓价格范围：做多 [ref - R0*0.8, ref + ref*3/8]；做空 [ref - ref*3/8, ref + R0*0.8]。"""
    R0 = abs(entry_ref - stop_loss)
    if R0 < 1e-9:
        raise ValueError("止损价与参考价相同，R0=0，无法计算价格范围")
    if direction == "long":
        range_low = entry_ref - R0 * 0.8
        range_high = entry_ref + entry_ref * 3.0 / 8.0
        odds_at_ref = (target - entry_ref) / R0
    else:
        range_low = entry_ref - entry_ref * 3.0 / 8.0
        range_high = entry_ref + R0 * 0.8
        odds_at_ref = (entry_ref - target) / R0
    return range_low, range_high, odds_at_ref


def check_price_in_range(direction, current_price, entry_ref, stop_loss, target):
    """检查当前价是否在可接受开仓范围内。返回 (in_range, low, high, odds_ref, odds_current)。"""
    range_low, range_high, odds_at_ref = calc_entry_range(direction, entry_ref, stop_loss, target)
    in_range = range_low <= current_price <= range_high
    if direction == "long":
        odds_at_current = (target - current_price) / (current_price - stop_loss) if current_price > stop_loss else float("inf")
    else:
        odds_at_current = (current_price - target) / (stop_loss - current_price) if stop_loss > current_price else float("inf")
    return in_range, range_low, range_high, odds_at_ref, odds_at_current


def calc_position_size(equity, risk_fraction, f_max, stop_distance, lot_size):
    """按 B = risk_fraction*equity、max_loss 上限 f_max*equity 选最接近 B 的 lot 离散仓位。"""
    B = equity * risk_fraction
    max_loss_cap = equity * f_max
    raw = B / stop_distance if stop_distance > 0 else 0
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
    best = min(candidates, key=lambda x: abs(x[1] - B))
    return best[0], best[1], B
