#!/usr/bin/env python3
"""港股 / 美股真实交易费率表（老虎证券，复盘 + 盯盘前瞻共用）。

2026-08-12 立：取代旧「港股 18bps/边、美股 3bps/边」笼统单值口径，改用老虎真实费率——
按市场 + 标的类型（个股 / ETF）+ 成交额（大单 / 小单）精确计算，平台费按阶梯式（当月累计订单数）。

费率构成（单边 = 一次成交：开仓算 1 边、平仓算 1 边，各自按各自成交额计费）：
  佣金      = max(15, 成交额 × 0.029%)          老虎佣金，最低 15 港元/笔
  印花税    = 成交额 × 0.1%                       港股个股收（买卖各 0.1%）；港股 ETF / 窝轮 / CBBC 免；美股无
  交易费    = 成交额 × 0.00565%                   港交所
  结算交收费 = 成交额 × 0.0042%                   香港结算所（2025-06-30 起调价）
  交易征费  = 成交额 × 0.0027%                    证监会 SFC
  财汇局征费 = 成交额 × 0.00015%                  财务汇报局 FRC
  平台费    = 阶梯式（按当月累计订单数，见 PLATFORM_TIERED）  老虎收；本账户用阶梯式

大单 / 小单自动分流（由 max() 自然实现，无需显式判断）：
  成交额 > 51,724（= 15 ÷ 0.029%）→ 佣金按 0.029% 收，单边费率收敛到：
    港股个股 ≈ 14.17 bps/边、港股 ETF ≈ 4.17 bps/边（不含平台费）
  成交额 ≤ 51,724 → 佣金按最低 15 收，单边费率显著更高（小单口径）。

平台费（阶梯式，老虎官网 2026-08-12 核实）：**港股 / 美股完全独立、各自按市场单独计阶梯档**（不是账户合计），按当月【该市场】累计成交订单数分档，开仓 / 平仓各算 1 笔订单：
  第 1-5 笔 30、6-20 笔 15、21-50 笔 10、51-100 笔 9、101-500 笔 8、
  501-1000 笔 7、1001-2000 笔 6、2001-3000 笔 5、3001-4000 笔 4、
  4001-5000 笔 3、5001-6000 笔 2、6001+ 笔 1（单位港元）。
阶梯式优于固定式的临界点 = 月 36 笔订单（约 18 T）。

美股口径（简化）：老虎美股佣金 0.029% 最低 15（与港股同结构），无印花税，征费用港股同结构近似
（美股 SEC 费 / TAF 等微小项略去，对净赔率影响可忽略）。

⚠️ 本模块是复盘 review.py / bayes_evolution.py 与盯盘 trade_utils_tiger.py 的共同费率源，
三处共享同一逻辑、避免分叉。费率变更只改本文件。
"""

# 老虎佣金
COMM_RATE = 0.00029      # 0.029%
COMM_MIN = 15            # 最低 15 港元/笔（佣金）

# 港股政府 / 交易所代收费率（单边，买卖各收一次）
STAMP = 0.001            # 印花税 0.1%（港股个股；ETF 免）
TRADE_FEE = 0.0000565    # 交易费（HKEX）
CCASS = 0.000042         # 结算交收费（HKSCC）
LEVY = 0.000027          # 交易征费（SFC）
FRC = 0.0000015          # 财汇局交易征费（FRC）
# 美股代收简化：无印花税，征费用港股同结构近似
GOV_RATE = TRADE_FEE + CCASS + LEVY + FRC   # 除印花税外的代收合计（单边）

# 阶梯式平台费（老虎，按当月累计订单数分档，每档 cap 为「累计到第几笔」、rate 为该笔平台费）
PLATFORM_TIERED = [
    (5, 30), (20, 15), (50, 10), (100, 9), (500, 8),
    (1000, 7), (2000, 6), (3000, 5), (4000, 4), (5000, 3),
    (6000, 2), (999999, 1),
]
PLATFORM_FIXED = 15      # 固定式备查（本账户用阶梯式）


def platform_fee(order_index_in_month, mode="tiered"):
    """当月第 order_index_in_month 笔订单（1-based）的平台费。

    mode='tiered'（默认，本账户）→ 按阶梯表；mode='fixed' → 固定 15。
    """
    if mode == "fixed":
        return PLATFORM_FIXED
    for cap, rate in PLATFORM_TIERED:
        if order_index_in_month <= cap:
            return rate
    return 1


def fee_per_side(market, sec_type, amount, order_index_in_month=None, platform_mode="tiered"):
    """单边费（一次成交的费用）= 佣金 + 印花税 + 各征费 + 平台费。

    参数：
      market: 'HK' / 'US'
      sec_type: 'stock' / 'etf'（决定港股印花税是否收；美股忽略）
      amount: 该边成交额（价位 × 股数）。amount <= 0 时返回 0
      order_index_in_month: 该订单在所属自然月的累计序号（1-based），给则计平台费、不给则不计
      platform_mode: 'tiered'（默认）/ 'fixed'

    返回：单边费（港元，港股；美元口径下数值同结构近似）。
    """
    if amount <= 0:
        return 0.0
    comm = max(COMM_MIN, amount * COMM_RATE)
    if market == "US":
        stamp = 0.0
    else:  # HK
        stamp = amount * STAMP if sec_type == "stock" else 0.0   # ETF 免印花税
    gov = amount * GOV_RATE
    plat = platform_fee(order_index_in_month, platform_mode) if order_index_in_month else 0.0
    return comm + stamp + gov + plat


def fee_per_side_rate(market, sec_type, amount, order_index_in_month=None, platform_mode="tiered"):
    """单边费率（bps = 万分之几）= fee_per_side / amount × 10000。便于口径对照（如 14.17 bps）。"""
    if amount <= 0:
        return 0.0
    return fee_per_side(market, sec_type, amount, order_index_in_month, platform_mode) / amount * 10000


if __name__ == "__main__":
    # 自检：大单个股 / ETF 单边费率应分别为 ≈14.17 / 4.17 bps（不含平台费）
    for mkt, typ, amt in [("HK", "stock", 1_000_000), ("HK", "etf", 1_000_000),
                          ("HK", "stock", 10_000), ("HK", "etf", 10_000)]:
        fee = fee_per_side(mkt, typ, amt)  # 不传 order_index → 不计平台费
        bps = fee_per_side_rate(mkt, typ, amt)
        print(f"{mkt} {typ} 成交额{amt:>10,} → 单边费 {fee:>9.2f} = {bps:.2f} bps（无平台费）")
    print("阶梯平台费（当月第 N 笔）：", [platform_fee(i) for i in (1, 5, 6, 20, 21, 50, 51)])
