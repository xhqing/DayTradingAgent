#!/usr/bin/env python3
"""港股交易工具库（老虎证券开放平台，港股默认账户）。

与长桥（美股 `trade_utils.py` / 港股备选 `trade_utils_hk.py`）**解耦**（分而治之，2026-08-01
用户立）：港股默认走老虎模拟账户，老虎单独一套、不 import 长桥模块。自包含：配置加载、港股
symbol / lot_size / tick、行情、下单（开仓 LMT+附加止损、平仓 MKT、独立止损 STP）、持仓 /
资产 / 订单查询、撤单、成交回查。

⚠️ 实测状态（2026-08-02 盘后研究 + 只读实测完成；下单 / 平仓 / 止损机制待开盘 paper 实测）：
- ✅ 已实测：配置加载、paper 判定机制（17 位账户号）、港股 symbol 格式（只认 5 位裸数字）、
  lot_size（get_contract）、tick（get_contract.tick_sizes）、资产 / 持仓 / 订单只读查询、
  行情（get_stock_briefs）。
- ⏳ 待开盘实测：下单链路（开仓 LMT+附加止损、平仓 MKT、独立止损 STP 的提交与触发行为）。
  **券商行为只信直接实测，不能从长桥外推**——本模块的订单语义以 paper 实测为准，实测后修订。

老虎相对长桥的差异点（本模块封装）：
- **配置加载**：`TigerOpenClientConfig(props_path=...)` 构造（私钥自动从 properties 的
  private_key_pk1/pk8 读取）。⚠️ 不要用 `get_client_config(props_path=...)`——该函数会先
  硬读 `private_key_path` 参数（None 直接 TypeError，2026-08-02 实测复现），必须显式传
  private_key_path 或走 TigerOpenClientConfig。
- **paper 判定**：account 为 17 位纯数字账户号即自动判为模拟账户（is_paper=True），网关域名
  自动走 license-PAPER（domain_conf 已含 TBNZ-PAPER / TBSG-PAPER，实测确认）。paper 账户号
  由用户提供后写入 properties 的 account 字段即可切换。
- **港股 symbol 格式**：老虎只认 5 位带前导 0 的裸数字代码（'02800' / '00700'，2026-08-02
  实测：HK.02800 / 2800.HK / 700.HK 均报「We don't support trading of this」）。富途格式
  HK.02800 → 取 '.' 后 5 位。
- **每手股数 lot_size**：因标的而异（盈富 500、腾讯/阿里 100），从 get_contract.lot_size 取。
- **价位 tick**：港交所价位表，从 get_contract.tick_sizes 取（实测返回完整区间表）。
- **币种**：港股 HKD，equity 取 get_assets().summary.net_liquidation（currency 同源）。

三个动作的订单类型（与长桥规范一致，券商语义不同）：
- 开仓：主单 LMT + 附加止损腿 OrderLeg('LOSS', price)（老虎附加订单仅限价单支持；长桥是
  LO + attached STOP_LOSS MIT，2026-08-02 源码确认 OrderLeg 对应）。
- 移动止损：独立 STP 止损单（aux_price=触发价；长桥是 MIT），先下新再撤旧、量严格=持仓量。
- 平仓：MKT（市价）+ 撤未触发止损单（防反向开仓）。
"""

import os
import sys
import time
from pathlib import Path


# ---------------------------------------------------------------------------
# 配置加载（自包含；props_path 默认 ~/.tigeropen/）
# ---------------------------------------------------------------------------

def load_config(props_path=None):
    """创建老虎 SDK 客户端配置。

    props_path：properties 目录或文件（默认 ~/.tigeropen/）。SDK 支持环境变量
    TIGEROPEN_TIGER_ID / TIGEROPEN_ACCOUNT / TIGEROPEN_PRIVATE_KEY / TIGEROPEN_PROPS_PATH
    等覆盖（优先级：参数 > 环境变量 > properties 文件）。

    ⚠️ 不走 get_client_config(props_path=...)（2026-08-02 实测：它先硬读 private_key_path
    参数、None 即 TypeError，读不到 properties 内嵌私钥）；TigerOpenClientConfig 会从
    properties 的 private_key_pk1/pk8 自动读私钥。
    """
    from tigeropen.tiger_open_config import TigerOpenClientConfig
    if props_path is None:
        props_path = os.path.expanduser("~/.tigeropen/")
    return TigerOpenClientConfig(props_path=props_path)


def new_trade_client(config=None):
    """创建老虎 TradeClient（下单 / 持仓 / 资产 / 订单查询）。"""
    from tigeropen.trade.trade_client import TradeClient
    return TradeClient(config if config is not None else load_config())


def new_quote_client(config=None):
    """创建老虎 QuoteClient（行情查询）。"""
    from tigeropen.quote.quote_client import QuoteClient
    return QuoteClient(config if config is not None else load_config())


# ---------------------------------------------------------------------------
# symbol 格式转换（富途 HK.02800 ↔ 老虎 02800）
# ---------------------------------------------------------------------------

def to_tiger_symbol(symbol):
    """富途 → 老虎：HK.02800 → 02800（老虎只认 5 位带前导 0 的裸数字代码，2026-08-02 实测：
    HK.02800 / 2800.HK / 700.HK 均不支持，'02800' / '00700' 可用）。美股不支持（老虎美股无权限）。
    """
    if not symbol or "." not in symbol:
        return symbol
    market, code = symbol.split(".", 1)
    if market != "HK":
        raise ValueError(f"老虎脚本只支持港股（HK.xxx），收到 {symbol}")
    return code  # 已是 5 位带前导 0（富途格式）；如传入无前导 0 则补足
    # 注：上面直接返回 code；如需兜底补前导 0，可改为 code.zfill(5)


def to_futu_symbol_tiger(tiger_symbol):
    """老虎 → 富途：02800 → HK.02800（补前导 0 到 5 位）。"""
    code = str(tiger_symbol)
    if "." in code:
        code = code.split(".")[0]
    return f"HK.{code.zfill(5)}"


# ---------------------------------------------------------------------------
# 合约查询：lot_size / tick / 名称（get_contract，2026-08-02 实测通过）
# ---------------------------------------------------------------------------

def get_contract_tiger(tc, symbol):
    """查港股合约（get_contract）。返回 Contract 对象（含 lot_size / tick_sizes / name /
    shortable / shortable_count）或 None。symbol 传富途格式 HK.02800。"""
    from tigeropen.common.consts import SecurityType
    return tc.get_contract(to_tiger_symbol(symbol), sec_type=SecurityType.STK)


def get_lot_size_tiger(tc, symbol):
    """港股每手股数（get_contract.lot_size，实测 02800=500、00700=100）。返回 int 或 None。"""
    try:
        c = get_contract_tiger(tc, symbol)
        if c is not None:
            ls = getattr(c, "lot_size", None)
            if ls:
                return int(ls)
    except Exception as e:
        print(f"⚠️ 查 lot_size 失败 {symbol}: {e}", file=sys.stderr)
    return None


def get_tick_sizes_tiger(tc, symbol):
    """港股价位表（get_contract.tick_sizes，港交所规则：随价格区间变化）。返回区间列表
    [{'begin','end','type','tick_size'}, ...] 或 None。"""
    try:
        c = get_contract_tiger(tc, symbol)
        if c is not None:
            return getattr(c, "tick_sizes", None)
    except Exception as e:
        print(f"⚠️ 查 tick 价位表失败 {symbol}: {e}", file=sys.stderr)
    return None


def _tick_from_table(price, tick_sizes):
    """从价位表按价格查最小报价单位 tick。区间匹配（begin, end]；边界语义待开盘实测确认。"""
    if not tick_sizes:
        return None
    p = float(price)
    for row in tick_sizes:
        try:
            begin = float(row.get("begin", 0))
            end = float(row.get("end", float("inf")))
            if p > begin and p <= end:
                return float(row.get("tick_size"))
        except (TypeError, ValueError):
            continue
    return None


def round_to_tick_tiger(price, tick_sizes=None):
    """把价格向下取整到港股 tick（限价单必须合 tick）。tick_sizes 缺失时 fallback 固定价位表
    （与长桥 trade_utils_hk 同表，2025-08-04 调整版）。"""
    import math
    tick = _tick_from_table(price, tick_sizes)
    if not tick:
        tick = get_tick_hk_fallback(price)
    return round(math.floor(price / tick) * tick, 6)


# 港交所最小报价单位（价位表），2025-08-04 调整版（与长桥 trade_utils_hk 同表）
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


def get_tick_hk_fallback(price):
    """按价格查港股最小报价单位（tick_sizes 缺失时兜底）。"""
    if price is None or price <= 0:
        return 0.001
    for upper, tick in _HK_TICK_TABLE:
        if price <= upper:
            return tick
    return 5.000


# ---------------------------------------------------------------------------
# 行情（QuoteClient.get_stock_briefs，2026-08-02 实测通过；返回 DataFrame）
# ---------------------------------------------------------------------------

def get_quote_tiger(config, symbol, retries=3):
    """港股最新报价。返回 dict {symbol, last, bid, ask, high, low, volume, latest_time} 或 None。

    ⚠️ get_stock_briefs 返回 pandas DataFrame（不是对象列表），按列名取
    （df['latest_price']），用 getattr 会得 None（2026-07-07 实测踩坑）。latest_time 为
    毫秒 Unix 时间戳。
    """
    qc = new_quote_client(config)
    tig = to_tiger_symbol(symbol)
    for attempt in range(retries):
        try:
            df = qc.get_stock_briefs([tig])
            if df is None or len(df) == 0:
                return None
            row = df.iloc[0]

            def _f(v):
                try:
                    return float(v) if v is not None and str(v) not in ("nan", "None") else None
                except (TypeError, ValueError):
                    return None

            return {
                "symbol": symbol,
                "last": _f(row.get("latest_price")),
                "bid": _f(row.get("bid_price")),
                "ask": _f(row.get("ask_price")),
                "high": _f(row.get("high")),
                "low": _f(row.get("low")),
                "volume": int(row.get("volume") or 0),
                "latest_time": row.get("latest_time"),
            }
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(1)
                continue
            raise RuntimeError(f"老虎港股行情失败 {symbol}: {e}") from e
    return None


# ---------------------------------------------------------------------------
# 权益（老虎港股账户 HKD，get_assets().summary.net_liquidation）
# ---------------------------------------------------------------------------

def load_equity_tiger(config=None):
    """港股 equity = 老虎账户净值（summary.net_liquidation，HKD）。返回 (equity, currency)。

    ⚠️ 2026-08-02 实测：当前实盘账户未开通交易/资产权限时，get_assets 返回 summary 全 0 且
    timestamp=None（prime_assets 的 segments 为空）——净值取不到。此时返回 (None, currency)，
    调用方（open_position_tiger 自动算仓位）应拒绝下单，禁止用 0 净值算仓位 B。
    paper 账户接入后应能取到真实净值。
    """
    tc = new_trade_client(config)
    try:
        assets = tc.get_assets()
        if not assets:
            return None, "HKD"
        summary = assets[0].summary
        na = getattr(summary, "net_liquidation", None)
        ts = getattr(summary, "timestamp", None)
        currency = getattr(summary, "currency", None) or "HKD"
        if na is None or (float(na) <= 0 and ts is None):
            print("⚠️ 老虎资产查询异常（net_liquidation=0 且无时间戳）——账户未开通交易/资产权限？",
                  file=sys.stderr)
            return None, currency
        return float(na), currency
    finally:
        pass  # SDK 客户端无显式 close


# ---------------------------------------------------------------------------
# 下单
# ---------------------------------------------------------------------------

def _make_order(tc, config, symbol, action, order_type, quantity,
                limit_price=None, aux_price=None, order_legs=None):
    """创建订单对象（create_order）并提交（place_order）。返回全局订单 id。

    action: 'BUY' / 'SELL'（老虎枚举是 BUY/SELL，与长桥 Buy/Sell 不同，注意转换）。
    order_type: 老虎 OrderType 枚举（LMT / MKT / STP 等）。
    """
    from tigeropen.common.consts import OrderType, SecurityType
    if isinstance(order_type, str):
        order_type = OrderType[order_type.upper()]
    contract = tc.get_contract(to_tiger_symbol(symbol), sec_type=SecurityType.STK)
    if contract is None:
        raise RuntimeError(f"查不到老虎合约 {symbol}（代码格式须 5 位数字，如 02800）")
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


def submit_order_with_stop_tiger(config, symbol, side, quantity, submitted_price,
                                 stop_loss_price, retries=3):
    """开仓：主单 LMT + 附加止损腿 OrderLeg('LOSS', stop_loss_price)（一次提交）。

    side: 'Buy'（做多开仓）/ 'Sell'（做空开仓）——注意转老虎 'BUY'/'SELL'。
    附加止损腿的方向与触发语义由券商按主单方向自动定（做多跌触发卖、做空涨触发买），
    与长桥 attached STOP_LOSS 一致；腿 TIF 默认 DAY（日内策略当日有效；跨日场景待实测）。
    返回全局订单 id。
    """
    from tigeropen.common.consts import OrderType
    from tigeropen.trade.domain.order import OrderLeg
    last_err = None
    for attempt in range(retries):
        try:
            tc = new_trade_client(config)
            action = "BUY" if side == "Buy" else "SELL"
            legs = [OrderLeg("LOSS", stop_loss_price)]
            return _make_order(tc, config, symbol, action, OrderType.LMT, quantity,
                               limit_price=submitted_price, order_legs=legs)
        except Exception as e:
            last_err = e
            if attempt < retries - 1:
                time.sleep(0.5 * (attempt + 1))
                continue
    raise RuntimeError(
        f"老虎开仓（LMT+附加止损）提交失败 {symbol} {side} qty={quantity} "
        f"price={submitted_price} stop={stop_loss_price}: {last_err}"
    )


def submit_market_order_tiger(config, symbol, side, quantity, retries=3):
    """港股市价单 MKT（平仓用）。side: 'Buy' / 'Sell'。返回全局订单 id。"""
    from tigeropen.common.consts import OrderType
    last_err = None
    for attempt in range(retries):
        try:
            tc = new_trade_client(config)
            action = "BUY" if side == "Buy" else "SELL"
            return _make_order(tc, config, symbol, action, OrderType.MKT, quantity)
        except Exception as e:
            last_err = e
            if attempt < retries - 1:
                time.sleep(0.5 * (attempt + 1))
                continue
    raise RuntimeError(f"老虎平仓 MKT 提交失败 {symbol} {side} {quantity}: {last_err}")


def submit_stop_order_tiger(config, symbol, side, quantity, trigger_price, retries=3):
    """独立止损单 STP（移损用；长桥对应 MIT）。aux_price=触发价。

    side 由调用方定（做多止损 Sell / 做空止损 Buy）；触发方向由券商按 trigger_price
    相对现价自动判定（与长桥 MIT 一致，2026-08-01 实测）。触发后市价成交。
    返回全局订单 id。
    """
    from tigeropen.common.consts import OrderType
    last_err = None
    for attempt in range(retries):
        try:
            tc = new_trade_client(config)
            action = "BUY" if side == "Buy" else "SELL"
            return _make_order(tc, config, symbol, action, OrderType.STP, quantity,
                               aux_price=trigger_price)
        except Exception as e:
            last_err = e
            if attempt < retries - 1:
                time.sleep(0.5 * (attempt + 1))
                continue
    raise RuntimeError(
        f"老虎独立止损 STP 提交失败 {symbol} {side} qty={quantity} trigger={trigger_price}: {last_err}"
    )


# ---------------------------------------------------------------------------
# 成交回查 / 持仓 / 资产 / 订单 / 撤单
# ---------------------------------------------------------------------------

def check_order_filled_tiger(config, order_id, timeout=8, poll_interval=2):
    """轮询订单成交状态（get_orders）。返回 (filled, fill_price, status_str)。

    老虎状态值与长桥不同（2026-08-02 源码确认）：Filled / PartiallyFilled / Cancelled /
    Inactive（已失效）/ Invalid（非法）等；长桥是 filled / cancelled / expired / dead / rejected。
    """
    tc = new_trade_client(config)
    try:
        deadline = time.time() + timeout
        last_status = ""
        while time.time() < deadline:
            for o in (tc.get_orders() or []):
                if str(getattr(o, "id", "")) != str(order_id) and \
                   str(getattr(o, "order_id", "")) != str(order_id):
                    continue
                status = str(getattr(o, "status", ""))
                last_status = status
                avg = getattr(o, "avg_fill_price", None)
                if "Filled" in status:
                    return True, (float(avg) if avg else None), status
                if any(s in status for s in ("Cancelled", "Inactive", "Invalid",
                                             "PendingCancel")):
                    return False, None, status
                break  # 已定位订单但未成交，继续等
            time.sleep(poll_interval)
        return False, None, last_status or "timeout"
    finally:
        pass


def get_open_position_tiger(config, symbol=None):
    """查老虎港股持仓。返回 {'symbol','symbol_name','side','quantity','cost_price'} 或 None。

    side 判定：Position 无方向字段（2026-08-02 源码确认），quantity 正=多、负=空
    （港股融券做空）。本项目做空走反向 ETF、账户层均为多头，默认 long。
    """
    tc = new_trade_client(config)
    try:
        positions = tc.get_positions() or []
        collected = []
        for p in positions:
            qty_f = float(getattr(p, "quantity", 0) or 0)
            if qty_f == 0:
                continue
            side = "short" if qty_f < 0 else "long"
            collected.append((side, p, abs(qty_f)))
        if not collected:
            return None
        if symbol is not None:
            target = to_tiger_symbol(symbol)
            matches = [(s, p, q) for s, p, q in collected
                       if str(getattr(p.contract, "symbol", "")) == target]
            if not matches:
                return None
            _, p, qty = matches[0]
        else:
            if len(collected) != 1:
                return None
            _, p, qty = collected[0]
        cost = getattr(p, "average_cost", None)
        return {
            "symbol": to_futu_symbol_tiger(getattr(p.contract, "symbol", None)),
            "symbol_name": getattr(p.contract, "name", None),
            "side": side,
            "quantity": int(qty),
            "cost_price": float(cost) if cost else None,
        }
    finally:
        pass


def cancel_order_tiger(config, order_id):
    """撤销订单（cancel_order(id=全局 id)）。"""
    tc = new_trade_client(config)
    try:
        tc.cancel_order(id=order_id)
    finally:
        pass


def cancel_all_stop_orders_tiger(config, symbol, exclude_order_id=None):
    """撤销指定港股标的的全部未触发止损单（平仓后防反向开仓；移损撤旧用）。

    两类止损都撤：
    - 独立止损单（order_type=STP，aux_price=触发价）——直接撤。
    - 附加止损腿（order_legs 含 LOSS 腿的订单）——腿不能单独撤，撤其主单（或父单）；
      主单成交后腿的行为待开盘实测，保守处理：能定位到腿所属订单则撤之。
    状态已 Filled / Cancelled / Inactive / Invalid / PendingCancel 的跳过。
    返回 (n, ids)。
    """
    tc = new_trade_client(config)
    try:
        target = to_tiger_symbol(symbol)
        orders = tc.get_orders() or []
        cancelled = []
        for order in orders:
            contract = getattr(order, "contract", None)
            order_sym = getattr(contract, "symbol", None) if contract else None
            if order_sym is None or str(order_sym) != target:
                continue
            oid = getattr(order, "id", None) or getattr(order, "order_id", None)
            if oid is None:
                continue
            if exclude_order_id is not None and str(oid) == str(exclude_order_id):
                continue  # 跳过刚下的新止损（移损「先新增后撤旧」保新止损）
            status = str(getattr(order, "status", ""))
            if any(s in status for s in ("Filled", "Cancelled", "Inactive", "Invalid",
                                         "PendingCancel")):
                continue
            otype = str(getattr(order, "order_type", ""))
            legs = getattr(order, "order_legs", None) or []
            is_stop = otype == "STP" or any(
                str(getattr(leg, "leg_type", "")).upper() == "LOSS" for leg in legs)
            if not is_stop:
                continue
            try:
                tc.cancel_order(id=oid)
                cancelled.append(oid)
            except Exception:
                pass  # 单个撤销失败不影响其他
        return len(cancelled), cancelled
    finally:
        pass


# ---------------------------------------------------------------------------
# 价格范围 / 仓位计算（纯函数，与长桥同逻辑、独立实现以解耦）
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


def parse_mode(argv=None):
    """从命令行参数解析执行模式 --mode（auto / signal），默认 auto（与 trade skill 一致）。"""
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
