#!/usr/bin/env python3
"""港股 / 美股真实交易费率表（老虎证券，复盘 + 盯盘前瞻共用）。

2026-08-17 改：平台费口径全面改**固定模式**、弃阶梯式（此前 2026-08-12 用的阶梯式口径废止）
——固定模式不需要查当月累计订单数，所有费项只由「市场 + 标的类型 + 成交额/股数」决定。
港美两市场的佣金与平台费**计费结构不同**，分开实现：

港股（按额 + 按笔）：
  佣金      = max(15, 成交额 × 0.029%)          老虎佣金，最低 15 港元/笔
  平台费    = 15 港元/笔（固定式）               2026-08-17 老虎官网核实
  印花税    = 成交额 × 0.1%                       港股个股收（买卖各 0.1%）；港股 ETF / 窝轮 / CBBC 免
  交易费    = 成交额 × 0.00565%                   港交所
  结算交收费 = 成交额 × 0.0042%                   香港结算所（2025-06-30 起调价）
  交易征费  = 成交额 × 0.0027%                    证监会 SFC
  财汇局征费 = 成交额 × 0.00015%                  财务汇报局 FRC

美股（按股，2026-08-17 按官网核改到位——旧「佣金 0.029% 最低 15 按额」是简化、结构错配）：
  佣金      = 0.0039 USD/股，上限为总交易额 × 0.5%（官网佣金行未列最低收费，按无最低实现）
  平台费    = 0.004 USD/股，每笔最低 1 USD，上限为总交易额 × 0.5%（固定式；低价股 < 0.8
              USD/股 才触发 0.5% 封顶，正常股票按股值 + 最低收费走）
  代收费    = 外部机构费及交易活动费 0.00396 USD/股 每笔最低 0.99 + 证监会规费 0.0000206×额
              （仅卖单、每次成交最低 0.01）+ CAT 0.000003/股——三项微小，本项目以
              0.00396 USD/股（无最低）近似并入单边费、不区分买卖方向（保守方向：多算一点）。

大单 / 小单自动分流（港股由佣金 max() 自然实现；美股由按股线性实现、天然无分流）：
  港股成交额 > 51,724（= 15 ÷ 0.029%）→ 佣金按 0.029% 收，个股单边费率收敛 ≈ 14.32+15/额 bps、
    ETF ≈ 4.32+15/额 bps；成交额 ≤ 51,724 → 佣金按最低 15 收，小单口径费率显著更高。
  美股按股线性（佣金+平台费+代收 ≈ 0.0087 USD/股 + 每笔平台费最低 1），费率随股价变化。

⚠️ 币种：港股返回港元；美股返回**美元**（股数 × 每股费）。本项目复盘 CSV 美股行以 USD 计价
（entry/exit/max_loss 同币种），net 口径自洽；跨市场混合样本时两币种各自独立、不互相换算。

费率来源（老虎官网，2026-08-17 核实）：
  港股 https://www.itigerup.com/hans/help/detail/68697332
  美股 https://www.itigerup.com/hans/help/detail/74820992

⚠️ 本模块是复盘 review.py / bayes_evolution.py 与盯盘 trade_utils_tiger.py 的共同费率源，
三处共享同一逻辑、避免分叉。费率变更只改本文件。
"""

# ---------------- 港股 ----------------
# 老虎佣金（按额计、最低按笔）
HK_COMM_RATE = 0.00029     # 0.029%
HK_COMM_MIN = 15.0         # 最低 15 港元/笔（佣金）
# 固定平台费（按笔）：2026-08-17 全面改固定模式，15 港元/笔
HK_PLATFORM_FIXED = 15.0

# 港股政府 / 交易所代收费率（单边，买卖各收一次）
# 2026-08-19 补两条真实规则（用当日实盘 01810 四笔成交单 SDK commission 逐笔对账校准）：
#   ① 印花税向上取整到元（每笔向上取整到 1 HKD，实缴口径）；
#   ② 交收费（CCASS）每笔最低 2 HKD。
# 对账结果（新规则 vs 实测 commission）：BUY 27.50×5000=210.34 零差、BUY 27.76×5000=211.88
# 零差、SELL(STP) 27.94×5000=213.25 零差；SELL(STP) 27.68×5000 实测 215.74、新规则 211.71
# 仍差 +4.03——该 STP 平仓单另有费项（同笔 App 侧费用合计 426.08 与 SDK commission 之和
# 一致，说明差在券商实际收费、非本表漏项；TODO 记录「疑组合单，待账单核」，核实前按
# -0 差最优模型保留）。旧纯比例模型对四笔分别差 +0.50 / +4.63 / +0.20 / +0.30。
STAMP = 0.001              # 印花税 0.1%（港股个股；ETF 免），每笔向上取整到元
TRADE_FEE = 0.0000565      # 交易费（HKEX）
CCASS = 0.000042           # 结算交收费（HKSCC），每笔最低 2 HKD
CCASS_MIN = 2.0            # 交收费最低收费（2026-08-19 实测对账补）
LEVY = 0.000027            # 交易征费（SFC）
FRC = 0.0000015            # 财汇局交易征费（FRC）
HK_GOV_RATE = TRADE_FEE + LEVY + FRC   # 除印花税与交收费外的代收合计（单边纯比例；CCASS
                                      # 带 max(最低2, 比例) 后在 fee_per_side 里单独算、
                                      # 不在此合计内防重复计，2026-08-19 修）

# ---------------- 美股（按股结构，2026-08-17 核改） ----------------
US_COMM_PER_SHARE = 0.0039    # 佣金 0.0039 USD/股
US_PLAT_PER_SHARE = 0.004     # 平台费固定式 0.004 USD/股
US_PLAT_MIN = 1.0             # 平台费每笔最低 1 USD
US_CAP_RATE = 0.005           # 佣金与平台费各自最多收总交易额 × 0.5%
US_GOV_PER_SHARE = 0.00396    # 代收费近似：外部机构费及交易活动费 0.00396/股
                              #（实际每笔最低 0.99；此处不设最低、直接按股线性近似）

# ---------------- 阶梯式（已弃用，备查） ----------------
# 2026-08-12 立的阶梯式口径 2026-08-17 废弃：固定模式不需要当月订单数。表值保留备查、
# 不再被任何调用方使用（港股按笔表 + 美股直接复用港股表本就是结构错配）。
PLATFORM_TIERED_HK = [
    (5, 30), (20, 15), (50, 10), (100, 9), (500, 8),
    (1000, 7), (2000, 6), (3000, 5), (4000, 4), (5000, 3),
    (6000, 2), (999999, 1),
]
HK_PLATFORM_FIXED_LEGACY_ALIAS = HK_PLATFORM_FIXED   # 旧名 PLATFORM_FIXED 兼容


def _platform_fee_hk_fixed():
    """港股固定平台费：15 港元/笔。"""
    return HK_PLATFORM_FIXED


def _commission_us(shares, amount):
    """美股佣金：0.0039 USD/股，上限 cap 总交易额 × 0.5%（官网未列最低、按无最低）。

    cap 是**上限**（官网说明第 6 条：低价股场景按股计费可能超总交易额 0.5%，此时压到
    0.5% 封顶）——正常股价（> 0.78 USD/股，= 0.0039 ÷ 0.5%）按股值远低于 cap、不触发。
    """
    if shares <= 0:
        return 0.0
    return min(US_COMM_PER_SHARE * shares, amount * US_CAP_RATE)


def _platform_fee_us(shares, amount):
    """美股平台费（固定式）：0.004 USD/股，每笔最低 1 USD，上限 cap 总交易额 × 0.5%。

    取值顺序：先按股算（0.004×股）、不足每笔最低 1 补到 1，再与 cap（0.5%×成交额）
    取小——cap 只在低价股（股价 < 0.8 USD，= 0.004 ÷ 0.5%）场景触发，正常股票不触。
    """
    if shares <= 0:
        return 0.0
    per_share = US_PLAT_PER_SHARE * shares
    return min(max(per_share, US_PLAT_MIN), amount * US_CAP_RATE)


def fee_per_side(market, sec_type, amount, shares=None, order_index_in_month=None,
                 platform_mode="fixed"):
    """单边费（一次成交的费用）= 佣金 + 印花税(港股个股) + 各征费 + 平台费。

    参数：
      market: 'HK' / 'US'（两市场计费结构不同，见模块 docstring）
      sec_type: 'stock' / 'etf'（决定港股印花税是否收；美股忽略）
      amount: 该边成交额（价位 × 股数，与市场对应币种）。amount <= 0 时返回 0
      shares: 该边股数（**美股必传**——佣金/平台费/代收均按股计；港股不用、缺省时
              以 amount 反推近似的整数股数仅用于兜底，正常调用都应显式传）
      order_index_in_month / platform_mode: 阶梯口径遗留参数，已废弃——固定模式不查
              当月订单数，传入值被忽略（保留签名兼容旧调用，避免调用方批量改）。

    返回：单边费（港元 / 美元，随市场）。
    """
    if amount is None or amount <= 0:
        return 0.0
    if market == "US":
        if shares is None:
            shares = max(int(round(amount / 100.0)), 1)   # 兜底近似：按 100 USD/股估股数
        comm = _commission_us(shares, amount)
        plat = _platform_fee_us(shares, amount)
        gov = US_GOV_PER_SHARE * shares
        return comm + plat + gov
    # HK
    comm = max(HK_COMM_MIN, amount * HK_COMM_RATE)
    # REIT 暂无对应档、落 stock 档收印花税——方向保守（多算一点费、不会低估成本）；
    # 港股 REIT 印花税实际免征与否待核实，核实后再决定是否加 'reit' 档（2026-08-17 注）。
    # 印花税向上取整到元 + 交收费最低 2 元（2026-08-19 实测对账补，见模块顶部注释）；
    # 交收费带最低收费后单独算（max(CCASS_MIN, 比例)），HK_GOV_RATE 已剔除 CCASS 比例
    # 部分（防重复计，2026-08-19 修）。
    import math
    stamp = math.ceil(amount * STAMP) if sec_type == "stock" else 0.0   # ETF 免印花税
    gov_ex_ccass = amount * HK_GOV_RATE
    ccass = max(CCASS_MIN, amount * CCASS)
    plat = _platform_fee_hk_fixed()
    return comm + stamp + gov_ex_ccass + ccass + plat


def fee_per_side_rate(market, sec_type, amount, shares=None, order_index_in_month=None,
                      platform_mode="fixed"):
    """单边费率（bps = 万分之几）= fee_per_side / amount × 10000。便于口径对照（如 14.32 bps）。"""
    if amount is None or amount <= 0:
        return 0.0
    return fee_per_side(market, sec_type, amount, shares, order_index_in_month,
                        platform_mode) / amount * 10000


if __name__ == "__main__":
    # 自检：固定模式口径下的大单 / 小单单边费
    for mkt, typ, amt, sh in [("HK", "stock", 1_000_000, None), ("HK", "etf", 1_000_000, None),
                              ("HK", "stock", 10_000, None), ("HK", "etf", 10_000, None),
                              ("US", "stock", 100_000, 100), ("US", "etf", 2_000, 10)]:
        fee = fee_per_side(mkt, typ, amt, sh)
        bps = fee_per_side_rate(mkt, typ, amt, sh)
        print(f"{mkt} {typ} 成交额{amt:>10,} 股数{sh} → 单边费 {fee:>9.2f} = {bps:.2f} bps")
