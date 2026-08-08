#!/usr/bin/env python3
"""港股交易工具库（老虎证券开放平台，港股默认账户）。

港股默认账户即老虎（2026-08-05 起港美股均走老虎、不再有备选账户，见 CHANGELOG），本库自包含：
配置加载、港股 symbol / lot_size / tick、行情、下单（开仓 LMT+附加止损、平仓 MKT、独立止损
STP）、持仓 / 资产 / 订单查询、撤单、成交回查。

✅ 实测状态（2026-08-03 paper 三动作全链路开盘实测通过）：
- ✅ 已实测：配置加载、paper 判定、港股 symbol 格式、lot_size / tick、资产 / 持仓 / 订单只读、
  行情，以及下单链路——开仓 LMT+附加止损腿（OrderLeg('LOSS') 落成独立 STP 单、主单成交后
  HELD 监控）、平仓 MKT（Filled、avg_fill_price 真实成交价）、独立止损 STP（modify aux_price
  移损、旧单可独立撤销）。
- 🔧 实测发现并修复 2 个 bug（2026-08-03）：① _make_order 的 order_type 传枚举对象致 place_order
  序列化失败（TypeError: Object of type OrderType is not JSON serializable）——须传字符串
  'LMT'/'MKT'/'STP'；② check_order_filled_tiger 直接 str(OrderStatus 枚举) 得
  'OrderStatus.FILLED'、'Filled' in 它恒 False → 已成交误判未成交并撤已成交单——须取
  status.value（'Filled'）再判断。**券商行为只信直接实测**——本模块订单语义已按实测落地。

本模块要点：
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

三个动作的订单类型：
- 开仓：主单 LMT + 附加止损腿 OrderLeg('LOSS', price)（老虎附加订单仅限价单支持）。
- 移动止损：modify 现有活动 STP 单的 aux_price（2026-08-05 实测单步、无撤单 race；仅
  fallback 才先下新再撤旧）、量严格=持仓量。
- 平仓：**先撤全部未触发止损单、再下 MKT 市价单**（2026-08-03 午后实测：挂着的止损单占用
  持仓可平额度，Buy 平空单被拒「exceeds holdings」；先撤止损再平立即成交。
  平仓脚本 close_position_tiger.py 已按此顺序实现）。
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
    """从价位表按价格查最小报价单位 tick。区间匹配（begin, end]；2026-08-03 paper 实测：
    开仓 LMT 486.2、移损 trigger 484.0 均正确取整合 tick，边界语义验证通过。"""
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
    （2025-08-04 调整版）。"""
    import math
    tick = _tick_from_table(price, tick_sizes)
    if not tick:
        tick = get_tick_hk_fallback(price)
    return round(math.floor(price / tick) * tick, 6)


# 港交所最小报价单位（价位表），2025-08-04 调整版
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
# 权益（老虎账户净值。2026-08-05 实测确认：get_prime_assets(base_currency) 直接返回
# 对应币种的账户净值——港股 base_currency='HKD'、美股 base_currency='USD'，无需外部汇率；
# 见 CHANGELOG 2026-08-05「equity 口径修复」）
# ---------------------------------------------------------------------------

def load_equity_tiger(config=None, base_currency=None):
    """老虎账户净值。返回 (equity, currency)。

    - base_currency='HKD'（港股交易口径）：get_prime_assets(base_currency='HKD') 证券段
      net_liquidation——全账户权益按实时汇率折算 HKD（2026-08-05 实测 7,819,536.41 HKD =
      总净值 996,932.71 USD × 7.843595，汇率来自老虎自身 currency_assets[].forex_rate，
      不依赖外部数据源）。
    - base_currency='USD' / 不传（美股或兼容旧调用）：get_assets summary.net_liquidation
      （USD，实测 996,932.71）。

    ⚠️ 2026-08-02 实测：实盘账户未开通交易/资产权限时 get_assets 返回 summary 全 0 且
    timestamp=None（prime_assets 的 segments 为空）——净值取不到。此时返回 (None, currency)，
    调用方（open_position_tiger 自动算仓位）应拒绝下单，禁止用 0 净值算仓位 B。
    paper 账户接入后能取到真实净值。
    """
    tc = new_trade_client(config)
    try:
        if base_currency is not None:
            # 按币种口径取净值（2026-08-05 修：港股 HKD / 美股 USD，与标的计价一致）
            try:
                pa = tc.get_prime_assets(base_currency=base_currency)
                if pa and getattr(pa, "segments", None):
                    for seg in pa.segments.values():
                        nl = getattr(seg, "net_liquidation", None)
                        if nl is not None:
                            cur = getattr(seg, "currency", None) or base_currency
                            return float(nl), str(cur)
            except Exception as e:
                print(f"⚠️ get_prime_assets(base_currency={base_currency}) 失败（{e}），回退 get_assets",
                      file=sys.stderr)
            return None, base_currency

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

    action: 'BUY' / 'SELL'（老虎订单动作枚举是 BUY/SELL 全大写）。
    order_type: 老虎 OrderType 枚举的**字符串值**（'LMT' / 'MKT' / 'STP'）——Order 构造函数
      原样存 order_type、place_order 序列化订单时 JSON 化该字段，传枚举对象会崩
      （TypeError: Object of type OrderType is not JSON serializable，2026-08-03 paper 实测发现）。
    """
    from tigeropen.common.consts import SecurityType
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
                                 stop_loss_price, order_type="LMT", retries=3):
    """开仓：主单（LMT 限价 / MKT 市价）+ 附加止损腿 OrderLeg('LOSS', stop_loss_price)（一次提交）。

    side: 'Buy'（做多开仓）/ 'Sell'（做空开仓）——注意转老虎 'BUY'/'SELL'。
    order_type: 'LMT'（限价主单，limit_price=submitted_price）/'MKT'（市价主单，不传限价）。
      2026-08-07 改：默认 'LMT' 改为由调用方显式传——高波动标的（如 MINIMAX）限价单 + 8 秒
      超时撤单极易错过成交（当日 5 次开仓全部 Invalid），市价单开仓可立即成交；
      MKT 主单 + LOSS 腿同一次提交，无「先开仓后挂止损」的裸奔空窗。
    附加止损腿的方向与触发语义由券商按主单方向自动定（做多跌触发卖、做空涨触发买）；
    腿 TIF 默认 DAY（日内策略当日有效；跨日场景待实测）。
    返回全局订单 id。
    """
    from tigeropen.trade.domain.order import OrderLeg
    last_err = None
    for attempt in range(retries):
        try:
            tc = new_trade_client(config)
            action = "BUY" if side == "Buy" else "SELL"
            legs = [OrderLeg("LOSS", stop_loss_price)]
            if order_type == "MKT":
                return _make_order(tc, config, symbol, action, "MKT", quantity, order_legs=legs)
            return _make_order(tc, config, symbol, action, "LMT", quantity,
                               limit_price=submitted_price, order_legs=legs)
        except Exception as e:
            last_err = e
            if attempt < retries - 1:
                time.sleep(0.5 * (attempt + 1))
                continue
    raise RuntimeError(
        f"老虎开仓（{order_type}+附加止损）提交失败 {symbol} {side} qty={quantity} "
        f"price={submitted_price} stop={stop_loss_price}: {last_err}"
    )


def submit_market_order_tiger(config, symbol, side, quantity, retries=3):
    """港股市价单 MKT（平仓用）。side: 'Buy' / 'Sell'。返回全局订单 id。"""
    last_err = None
    for attempt in range(retries):
        try:
            tc = new_trade_client(config)
            action = "BUY" if side == "Buy" else "SELL"
            return _make_order(tc, config, symbol, action, "MKT", quantity)
        except Exception as e:
            last_err = e
            if attempt < retries - 1:
                time.sleep(0.5 * (attempt + 1))
                continue
    raise RuntimeError(f"老虎平仓 MKT 提交失败 {symbol} {side} {quantity}: {last_err}")


def submit_stop_order_tiger(config, symbol, side, quantity, trigger_price, retries=3):
    """独立止损单 STP（移损用）。aux_price=触发价。

    side 由调用方定（做多止损 Sell / 做空止损 Buy）；触发方向由券商按 trigger_price
    相对现价自动判定（2026-08-01 实测）。触发后市价成交。
    返回全局订单 id。
    """
    last_err = None
    for attempt in range(retries):
        try:
            tc = new_trade_client(config)
            action = "BUY" if side == "Buy" else "SELL"
            return _make_order(tc, config, symbol, action, "STP", quantity,
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

    老虎状态值（2026-08-02 源码确认）：Filled / PartiallyFilled / Cancelled /
    Inactive（已失效）/ Invalid（非法）等。

    ⚠️ status 是 OrderStatus 枚举（OrderStatus.FILLED），必须取 .value（'Filled'）再判断——
    直接 str(枚举) 得 'OrderStatus.FILLED'，'Filled' in 它恒 False → 已成交误判未成交、
    随后撤单撤已成交的单（持仓实际已建立、附加止损还挂着）。2026-08-03 paper 实测暴露
    （开仓主单实际 FILLED @486.2，脚本却输出「主单未成交、已撤」）。
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
                status_obj = getattr(o, "status", "")
                status = status_obj.value if hasattr(status_obj, "value") else str(status_obj)
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
    """撤销指定港股标的的全部未触发止损单（平仓后防反向开仓；移损 fallback 撤旧用）。

    两类止损都撤：
    - 独立止损单（order_type=STP，aux_price=触发价）——直接撤。
    - 附加止损腿——2026-08-03 paper 实测：OrderLeg('LOSS') 提交后由券商落成**独立 STP 单**
      （order_type=STP，action 按主单方向），主单成交后该单进入 HELD 监控，可像独立止损单一样
      直接撤销（实测：移损撤旧成功撤掉开仓附加腿落成的 STP 单，无需撤主单）。
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
                continue  # 跳过刚下的新止损（移损 fallback「先下新再撤旧」保新止损）
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


def get_today_orders_tiger(config):
    """查老虎当日订单列表（get_orders），供 monitor_segment 每轮采样提取最新止损价。

    用户可能在券商 App 里手动新增止损单，最新止损价不能凭记忆，须每轮采样现查。
    返回订单对象列表（含 order_type=STP 的止损单，触发价在 aux_price）或 []。
    老虎订单对象字段（id / status / contract.symbol / order_type / aux_price /
    order_legs）见 cancel_all_stop_orders_tiger。
    """
    tc = new_trade_client(config)
    try:
        return tc.get_orders() or []
    except Exception as e:
        print(f"⚠️ 老虎当日订单查询失败: {e}", file=sys.stderr)
        return []


# ---------------------------------------------------------------------------
# 持仓期间极值（平仓过程指标素材，2026-08-05 立）
# ---------------------------------------------------------------------------

def calc_position_extremes_tiger(symbol, mode="signal", project_root=None):
    """从盯盘 log 取该标的当日采样极值（持仓期间 high/low 的近似），供平仓时原生记录
    mfe_R / mae_R（review-and-evaluation.md「⚠️ 数据约束」方案 b 落地：复盘直接读、
    不必每次回拉历史 K）。

    读 `tmp/monitor_log_{SYM}_{YYYYMMDD}_{mode}.csv`（log 由 monitor_segment 按市场交易日
    命名——港股北京日期、美股美东交易日，signal/auto 两会话分文件；SYM = 富途格式转下划线，
    如 HK.00981 → HK_00981）。多个日期文件只取最新日期那个（当日）。

    ⚠️ 近似：log 的 high/low 是行情快照的当日 high/low 列、且含开仓前时段（盘前采样点在
    开盘价附近；日内策略当天开当天平，当日 log 近似持仓期间，误差可控）。无 log（未盯盘 /
    停盯后平仓）返回 None，调用方按缺失处理、复盘跳过过程指标。
    返回 (raw_high, raw_low) 或 None。
    """
    import csv
    import glob
    if project_root is None:
        project_root = Path(__file__).resolve().parent.parent.parent.parent.parent
    log_dir = Path(project_root) / "tmp"
    sym_tag = str(symbol).replace(".", "_")  # HK.00981 → HK_00981（monitor_segment 的 log 命名）
    files = sorted(glob.glob(str(log_dir / f"monitor_log_{sym_tag}_*_{mode}.csv")))
    if not files:
        return None
    highs, lows = [], []
    with open(files[-1]) as fh:  # 只取最新日期文件（当日；跨日残留旧文件排除）
        for r in csv.DictReader(fh):
            if r.get("symbol") != symbol:
                continue
            try:
                if r.get("high") not in (None, ""):
                    highs.append(float(r["high"]))
                if r.get("low") not in (None, ""):
                    lows.append(float(r["low"]))
            except ValueError:
                continue
    if not highs or not lows:
        return None
    return max(highs), min(lows)


# ---------------------------------------------------------------------------
# 价格范围 / 仓位计算（纯函数）
# ---------------------------------------------------------------------------

# 单边手续费率（2026-08-04 用户立：盯盘前瞻赔率改净口径，与复盘 review.py 同口径）。
# 港股 18bps/边、美股 3bps/边（1bps=0.0001）；一笔交易 = 开仓 + 平仓 两边各收一次。
# 本 utils 只处理港股（HK.），_fee_per_side 按 symbol 前缀判、与美股 utils / review.py 一致。
def _fee_per_side(symbol):
    s = (symbol or '').upper()
    if s.startswith('HK.'): return 0.0018   # 18bps
    if s.startswith('US.'): return 0.0003   # 3bps
    return 0.0   # 未知市场前缀：保守不扣费（等价毛口径），避免误判


def _net_odds(direction, entry, target, stop, fee_per_side):
    """净前瞻赔率（与复盘 R = P_net / M 同口径）。

    分子 = 到止盈的净盈利 = 止盈距 − 双边费（开仓按 entry、平仓按 target 各收一次单边费率，
    即 fee_per_side × (entry + target)）；分母 = 毛止损距（与复盘 M = shares×止损距 同为毛值、不动）。
    前瞻假设到止盈 target 出场，故「平仓价」用 target（与复盘用实际平仓价算费同结构）。
    做多 stop_dist = entry − stop；做空 stop_dist = stop − entry；≤0 返回 inf（方向错或贴止损）。
    """
    if direction == 'long':
        gross_gain = target - entry
        stop_dist = entry - stop
    else:
        gross_gain = entry - target
        stop_dist = stop - entry
    if stop_dist <= 1e-12:
        return float('inf')
    fee_per_share = fee_per_side * (entry + target)
    return (gross_gain - fee_per_share) / stop_dist


def calc_entry_range(direction, entry_ref, stop_loss, target, symbol=None):
    """开仓价格范围（经验参数、与毛/净赔率无关）：做多 [ref - R0*0.8, ref + ref*3/8]；做空 [ref - ref*3/8, ref + R0*0.8]。
    价格范围用毛 R0 算、不随净口径变；odds_at_ref 为净口径（扣双边费）。"""
    R0 = abs(entry_ref - stop_loss)
    if R0 < 1e-9:
        raise ValueError("止损价与参考价相同，R0=0，无法计算价格范围")
    if direction == "long":
        range_low = entry_ref - R0 * 0.8
        range_high = entry_ref + entry_ref * 3.0 / 8.0
    else:
        range_low = entry_ref - entry_ref * 3.0 / 8.0
        range_high = entry_ref + R0 * 0.8
    odds_at_ref = _net_odds(direction, entry_ref, target, stop_loss, _fee_per_side(symbol))
    return range_low, range_high, odds_at_ref


def check_price_in_range(direction, current_price, entry_ref, stop_loss, target, symbol=None):
    """检查当前价是否在可接受开仓范围内。返回 (in_range, low, high, odds_ref, odds_current)。
    odds_ref / odds_current 均为净口径（扣双边费）。"""
    range_low, range_high, odds_at_ref = calc_entry_range(direction, entry_ref, stop_loss, target, symbol)
    in_range = range_low <= current_price <= range_high
    odds_at_current = _net_odds(direction, current_price, target, stop_loss, _fee_per_side(symbol))
    return in_range, range_low, range_high, odds_at_ref, odds_at_current


def calc_position_size(equity, risk_fraction, f_max, stop_distance, lot_size,
                       entry_price=None, max_leverage=None):
    """按 B = risk_fraction*equity、max_loss 上限 f_max*equity 选最接近 B 的 lot 离散仓位。

    2026-08-08 新增市值杠杆上限约束：开仓市值（= 数量 × 开仓价）不得超过 equity × max_leverage
    （默认 10 倍，取 config.risk.max_leverage；权益 10 万 → 最高开 100 万市值）。与 f_max 是两套
    独立约束——f_max 限 max_loss（风险敞口）、max_leverage 限开仓市值（名义敞口），候选档须同时
    满足两者。

    max_leverage=None 时回退读 skill config.json 的 risk.max_leverage（默认 10）；entry_price 传
    参考价/开仓价（用作市值估算基准）。

    双约束上界：max_loss 上界 = equity×f_max ÷ 止损距；市值上界 = equity×max_leverage ÷ 开仓价
    （有 entry_price 时）。取两者较小者向下取整到整手 = ub_lot；目标档 center = min(按 B 算的
    base 档, ub_lot)——cap 压下来则退到 ub_lot（市值/风险上限内的最大档），再在 center 附近
    ±2 档里选实际 max_loss 最接近 B 的档（剔除超 cap 的档）。"""
    import json
    from pathlib import Path
    if max_leverage is None:
        try:
            _cfg_path = Path(__file__).resolve().parent.parent / "config.json"
            with open(_cfg_path) as _f:
                max_leverage = float(json.load(_f).get("risk", {}).get("max_leverage", 10))
        except Exception:
            max_leverage = 10.0
    B = equity * risk_fraction
    max_loss_cap = equity * f_max
    notional_cap = equity * max_leverage if entry_price else None
    raw = B / stop_distance if stop_distance > 0 else 0
    base = int(raw // lot_size) * lot_size
    # 双约束上界（整手）
    ub = max_loss_cap / stop_distance if stop_distance > 0 else float("inf")
    if notional_cap is not None:
        ub = min(ub, notional_cap / entry_price)
    ub_lot = int(ub // lot_size) * lot_size
    if ub_lot <= 0:
        return 0, 0, B
    center = min(base, ub_lot)
    candidates = []
    for mult in [-2, -1, 0, 1, 2]:
        s = center + mult * lot_size
        if s <= 0:
            continue
        ml = s * stop_distance
        if ml > max_loss_cap:
            continue
        if notional_cap is not None and s * entry_price > notional_cap:
            continue
        candidates.append((s, ml))
    if not candidates:
        return 0, 0, B
    best = min(candidates, key=lambda x: abs(x[1] - B))
    return best[0], best[1], B


def parse_mode(argv=None):
    """从命令行参数解析执行模式 --mode（auto / signal），默认 signal（与 trade skill 一致）。"""
    if argv is None:
        argv = sys.argv[1:]
    for i, a in enumerate(argv):
        if a == "--mode" and i + 1 < len(argv):
            m = argv[i + 1]
            return m if m in ("auto", "signal") else "signal"
        if a.startswith("--mode="):
            m = a.split("=", 1)[1]
            return m if m in ("auto", "signal") else "signal"
    return "signal"


def load_equity(mode='signal', project_root=None, base_currency='HKD'):
    """按执行模式取当前 equity，返回 (equity, currency, source_str)。

    - mode='auto'：老虎账户净值（港股 base_currency='HKD'、美股 base_currency='USD'，与
      标的计价一致，见 load_equity_tiger）；查询失败 fallback signals/equity-log.csv
      （标记非真实、需修复）。
    - mode='signal'：读 signals/equity-log.csv 末行 equity_after（signal 模式不连账户、
      靠累加值；无记录返回 config.risk.initial_equity）。

    auto 模式 equity 必须是账户真实总资产（2026-07-31 用户立）；signal 模式因不碰账户、用
    equity-log 累加假设盈亏（2026-08-01 双模式重构立，见 signal-mode.md「signal 模式权益更新」）。
    2026-08-05 起港美股默认账户均为老虎，本函数随老虎脚本迁移至此（原在已删除的
    trade_utils.py）。
    """
    import csv
    import json
    if project_root is None:
        # trade_utils_tiger.py 在 .claude/skills/trade/scripts/，上五级 = 项目根（signals/equity-log.csv 在项目根 signals/）
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

    # mode == 'auto'：老虎账户（港股 HKD / 美股 USD，与标的计价一致）
    eq, cur = load_equity_tiger(base_currency=base_currency)
    if eq is None:
        eq = _read_equity_log()
        if eq is not None:
            return eq, currency, f"equity-log.csv 末行（⚠️老虎账户查询失败（{base_currency} 口径），旧手动累加值、非真实，需修复）"
        return initial_equity, base_currency, f"config initial_equity={initial_equity:.0f}（⚠️老虎查询失败且 equity-log 无记录，占位非真实）"
    return eq, cur, f"老虎账户 get_prime_assets(base_currency={cur}) 证券段净值（默认账户）"
