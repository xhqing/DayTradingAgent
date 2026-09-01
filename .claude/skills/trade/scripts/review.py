#!/usr/bin/env python3
"""交易复盘统计脚本：读已平仓交易清单 CSV，输出全套评估指标。
用法: python3 review.py <trades.csv>

CSV 字段（首行表头，逗号分隔；# 开头行当注释跳过）:
  date, symbol, direction, entry_price, exit_price, shares, max_loss
  [, entry_time, exit_time, raw_high, raw_low, type, mode, shadow]
  - symbol: 富途格式带市场前缀（HK.03690 / US.SOXL），--fetch-futu 时直接传富途
  - direction: long / short（也接受 做多/做空/多/空）
  - max_loss: 该笔毛最大损失金额（本币，仓位×每股止损距；净 max_loss = 毛值+开仓费+止损价
    平仓费，脚本内部反推止损价自动算，CSV 仍记毛值、口径不变）
  - entry_time / exit_time: 开/平仓时刻 'YYYY-MM-DD HH:MM:SS'，写该标的【交易所当地时区】
    （港股 HKT、美股 ET）；仅 --fetch-futu 时需要，用于拉持仓期间分钟 K
  - raw_high / raw_low: 持仓期间最高/最低价，可选；提供则直接用（优先于 --fetch-futu 拉取），
    缺失则 --fetch-futu 时自动从富途拉、否则跳过过程指标
  - shadow: 影子样本标记，可选（1=影子）；2026-08-27 立——auto 模式被互斥闸拦下的机会的
    纸面记录（见 auto-mode.md「影子交易」节）。主统计默认剔除 shadow=1 行（真实样本口径
    不变），--shadow-only 反向只统计影子子集
  - mode: 交易模式标签（2026-08-31 T134 立，四值）：signal（信号模式，AI 发信号用户手动
    执行）/ auto_paper（auto 模式模拟账户）/ auto_live（auto 模式实盘账户）/ shadow（影子
    纸面样本）。复盘报告的「分阶段统计」按本列分列（取代按日期近似切分——旧口径每期
    复盘都要重新推断边界、换人重跑会得出不同分段）。老 CSV 无 mode 列时兼容推断：
    shadow=1 → shadow，其余标 unknown（unknown 不进任何 --mode 子集、主统计照常计入，
    打印提醒补 mode 列）

用法:
  python3 review.py <trades.csv>                  # 用 CSV 自带 raw_high/raw_low
  python3 review.py <trades.csv> --fetch-futu     # 缺 high/low 时连富途按时间戳自动拉
  python3 review.py <trades.csv> --mode auto_live # 只统计某一模式的子集（T134）

输出（R-multiple 体系，与 SKILL「复盘分析」一致；2026-08-03 起改 net 口径）:
  1. 样本明细
  2. 终局统计量（胜率 / 败率 / 胜赔率 / 败赔率 / EV / EV% / 平均每单盈亏）
  3. 过程指标分盈利单 / 亏损单各一组（MAE / MFE / 回吐 / 锁利效率 η）
  4. 贝叶斯 P(EV>0)（完整贝叶斯 NIG，t 后验）+ σ 不确定下敏感性区间（一律带区间）
  5. 频率派 EV 的 95% CI（对照）
  6. 样本量规划（代入当前 s）

费率扣费口径（net，2026-08-03 用户立；2026-08-12 改真实费率；2026-08-17 平台费改固定模式 + 美股按股结构；
2026-08-28 分母改净口径——用户立「max_loss 加止损价成交的手续费，最大亏损不深于 −1R」）:
  盈亏 P 与 R 一律扣双边手续费后再算：P_net = P_gross − fee，
  fee = 开仓边费 + 平仓边费（两边各自按各自成交额/股数计费）。
  港股单边费 = 佣金(max(15, 成交额×0.029%)) + 印花税(个股0.1%,ETF免) + 各征费 + 固定平台费(15/笔)；
  美股单边费 = 佣金(0.0039/股, cap 0.5%×额) + 平台费(0.004/股, 最低1/笔, cap 0.5%×额) + 代收近似(0.00396/股)。
  按「市场 + 标的类型(type列 stock/etf) + 成交额/股数」精确算，见 fee_schedule.py。
  R 分母 = 净 max_loss = 毛 M + 开仓边费 + 止损价平仓边费（止损价由 M/shares 反推）——
  止损价精确成交的笔净 R 恰好 −1.000，最大亏损不深于 −1R（仅剩滑点一层）。
  旧「分母毛值、止损 R 略低于 −1」口径废止（2026-08-28 前）。
  EV / 胜率 / 赔率 / 贝叶斯 P(EV>0) 全部基于净 R，自动跟随净口径。
  旧「港股 18bps / 美股 3bps」笼统单值口径已废（高估个股、严重高估 ETF 成本）；
  2026-08-12~08-17 间用的阶梯平台费口径亦废（固定模式不需要当月订单数）。

⚠️ 盈亏按信号参考价与 max_loss 实算、扣双边手续费，不涉及真实账户资金（信号模式：AI 不管账户）。
依赖: scipy（无则退化：t→正态近似、χ²→Wilson-Hilferty，小样本偏乐观；建议装 scipy）。
"""
import sys, csv, math, argparse, os
from statistics import mean, stdev

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fee_schedule as FS   # 真实费率（市场+类型+成交额/股数+固定平台费），2026-08-12 立、08-17 改固定模式


# ---------- 分布函数（scipy 优先，无则退化）----------
def _norm_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _log1p_fR(R, f):
    """Y = ln(1+fR)，带定义域保护（2026-08-17 立）：1+fR ≤ 0（如 f=0.50 档、净 R ≤ −2 的
    爆亏单）时 math.log 抛 ValueError 会把整个复盘 / 演化脚本崩掉。保护方式：把 1+fR 压到
    极小正数下限（1e-12）再取对数——对应 Y ≈ −27.6 的极端亏损样本，方向正确（重度惩罚该 f）、
    数值有界（不产生 -inf 污染后续统计），且正常样本（1+fR > 0）完全不受影响。"""
    _EPS = 1e-12
    return [math.log(max(1 + f * r, _EPS)) for r in R]

try:
    from scipy import stats as _ss
    def _t_cdf(x, df): return _ss.t.cdf(x, df)
    def _chi2_ppf(p, df): return _ss.chi2.ppf(p, df)
    _HAVE_SCIPY = True
except ImportError:
    def _t_cdf(x, df): return _norm_cdf(x)  # 正态近似（df 小时偏乐观）
    def _chi2_ppf(p, df):  # Wilson-Hilferty 近似（仅 p=0.025/0.975 调用）
        z = -1.959963984540054 if p < 0.5 else 1.959963984540054
        return df * (1 - 2 / (9 * df) + z * math.sqrt(2 / (9 * df))) ** 3
    _HAVE_SCIPY = False


# ---------- 解析 ----------
def _direction(s):
    s = (s or '').strip().lower()
    if s in ('long', '做多', '多', 'buy', '买入'): return 1
    if s in ('short', '做空', '空', 'sell', '卖出'): return -1
    raise ValueError(f"direction 无法解析: {s!r}")

# 单边费率（2026-08-12 改真实费率，复用 fee_schedule；2026-08-17 平台费改固定模式 + 美股按股）：
# 港股按额计（佣金 max(15,×0.029%) + 印花税(个股0.1%/ETF免) + 征费 + 平台费 15/笔）、
# 美股按股计（佣金 0.0039/股 + 平台费 0.004/股 + 代收近似 0.00396/股）。
def _market_of(symbol):
    s = (symbol or '').upper()
    if s.startswith('HK.'): return 'HK'
    if s.startswith('US.'): return 'US'
    raise ValueError(f"未知市场前缀、无法定费率: {symbol!r}（只支持 HK. / US.）")

def _opt_float(v):
    if v is None: return None
    v = v.strip()
    return float(v) if v else None

def _strip_comments(f):
    for line in f:
        s = line.strip()
        if not s or s.startswith('#'): continue
        yield line

def compute_process(t):
    """据 t['hi']/t['lo'] 算过程指标写回 t；hi/lo 缺失则置 None。"""
    if t['hi'] is not None and t['lo'] is not None:
        if t['sign'] > 0:   # 做多：跌不利 / 涨有利
            adv, fav = t['entry'] - t['lo'], t['hi'] - t['entry']
        else:               # 做空：涨不利 / 跌有利
            adv, fav = t['hi'] - t['entry'], t['entry'] - t['lo']
        mae_amt, mfe_amt = max(0, adv) * t['shares'], max(0, fav) * t['shares']
        t['MAE_R'] = -mae_amt / t['M_net']        # 浮亏峰值（负值，越接近 0 越好；净分母同 R 口径）
        t['MFE_R'] =  fav * t['shares'] / t['M_net']  # 浮盈峰值（正值，越大越好）
        t['tuhui'] = max(t['MFE_R'] - t['R'], 0)  # 回吐（越小越好）
        t['eta']   = t['R'] / t['MFE_R'] if t['MFE_R'] > 0 else None  # 锁利效率（仅盈利单）
    else:
        t['MAE_R'] = t['MFE_R'] = t['tuhui'] = t['eta'] = None


def load_trades(path):
    with open(path, newline='') as f:
        rows = list(csv.DictReader(_strip_comments(f)))
    if not rows: sys.exit("❌ CSV 无数据行")
    trades = []
    shadow_any = False
    mode_missing = 0
    _VALID_MODES = ('signal', 'auto_paper', 'auto_live', 'shadow')
    for i, r in enumerate(rows, 1):
        try:
            _is_shadow = (r.get('shadow') or '').strip() not in ('', '0', 'false', 'False')
            shadow_any = shadow_any or _is_shadow
            # mode 列（2026-08-31 T134）：signal / auto_paper / auto_live / shadow 四值；
            # 老无该列时兼容推断——shadow=1 → shadow，其余 unknown（不进 --mode 子集、
            # 主统计照常计入 + 提醒补标）
            _mode = (r.get('mode') or '').strip().lower()
            if not _mode:
                _mode = 'shadow' if _is_shadow else 'unknown'
                mode_missing += 1
            if _mode not in _VALID_MODES and _mode != 'unknown':
                sys.exit(f"❌ 第{i}行 mode 非法 '{_mode}'（合法值：{'/'.join(_VALID_MODES)}）")
            t = dict(date=r['date'].strip(), symbol=r['symbol'].strip(),
                     sign=_direction(r['direction']),
                     entry=float(r['entry_price']), exit=float(r['exit_price']),
                     shares=float(r['shares']), M=float(r['max_loss']),
                     hi=_opt_float(r.get('raw_high')), lo=_opt_float(r.get('raw_low')),
                     entry_time=(r.get('entry_time') or '').strip() or None,
                     exit_time=(r.get('exit_time') or '').strip() or None,
                     sec_type=(r.get('type') or r.get('sec_type') or '').strip().lower() or None,
                     shadow=_is_shadow, mode=_mode)
        except Exception as e:
            sys.exit(f"❌ 第{i}行解析失败: {e}\n  {r}")
        # 跨日交易校验（2026-08-12 用户立：本项目纯日内、不存在跨日；entry_time/exit_time
        # 都存在且日期不同 = 数据问题，报错）。CSV 无时间列时跳过此校验。
        if t['entry_time'] and t['exit_time']:
            d_open, d_close = t['entry_time'][:10], t['exit_time'][:10]
            if d_open != d_close:
                sys.exit(f"❌ 第{i}行 {t['symbol']} 跨日交易（开 {d_open} / 平 {d_close}）："
                         f"本项目纯日内、不存在跨日，请检查数据。")
        t['market'] = _market_of(t['symbol'])
        # type 列缺失：港股个股是多数、ETF 少数，缺 type 时默认 stock（保守收印花税），
        # 但打印提醒让用户补 type 列以精算。ETF（如 HK.07709）若漏标会多算印花税。
        if not t['sec_type']:
            t['sec_type'] = 'stock'
            t['_type_missing'] = True
        else:
            t['_type_missing'] = False
        t['P_gross'] = (t['exit'] - t['entry']) * t['shares'] * t['sign']  # 毛盈亏（未扣费）
        trades.append(t)
    # 先按 (date, symbol) 排序（样本明细编号 = 序贯图横轴，与 bayes_evolution.py 同排序）
    trades.sort(key=lambda t: (t['date'], t['symbol']))
    # 平台费 2026-08-17 改固定模式：费项与订单数无关，不再按自然月累计订单序号分档。
    # 2026-08-28 分母改净口径（用户立「max_loss 加止损价成交的手续费」）：M_net = 毛 M
    # + 开仓边费 + 止损价平仓边费。CSV 无止损价列，由 M/shares（每股止损距）反推：
    # 做多 stop = entry − M/shares、做空 stop = entry + M/shares——毛 M 精确记录时反推
    # 数学上精确（与开仓记录同源）。止损价精确成交的笔净 R 恰好 −1.000（最大亏损不深于
    # −1R，仅滑点除外）。
    type_missing_any = False
    for t in trades:
        amt_open = t['entry'] * t['shares']
        amt_close = t['exit'] * t['shares']
        fee_open = FS.fee_per_side(t['market'], t['sec_type'], amt_open, shares=t['shares'])
        fee_close = FS.fee_per_side(t['market'], t['sec_type'], amt_close, shares=t['shares'])
        t['fee_open'] = fee_open
        t['fee_close'] = fee_close
        t['fee'] = fee_open + fee_close  # 开仓 + 平仓 两边手续费（各自按成交额/股数计费）
        t['P'] = t['P_gross'] - t['fee']  # 净盈亏（扣双边手续费）——复盘所有盈亏 / R 口径
        # 净 max_loss：反推止损价 → 算止损价平仓边费（注意与 fee_close 区分：fee_close 按实际
        # 平仓价算、用于分子；止损边费按止损价算、只进分母的最大亏损场景）
        per_share_dist = t['M'] / t['shares'] if t['shares'] > 0 else 0.0
        stop_implied = t['entry'] - per_share_dist * t['sign']   # 做多止损在下方（sign=+1）、做空上方（−1）
        fee_stop = FS.fee_per_side(t['market'], t['sec_type'],
                                   abs(stop_implied * t['shares']), shares=t['shares'])
        t['M_net'] = t['M'] + fee_open + fee_stop
        t['fee_stop'] = fee_stop
        t['R'] = t['P'] / t['M_net']    # 净 R；分母净 max_loss（毛 M + 开仓费 + 止损价平仓费）
        compute_process(t)
        type_missing_any = type_missing_any or t['_type_missing']
    if type_missing_any:
        sys.stderr.write("⚠️ 部分/全部行缺 type 列，已按 stock（收印花税）计费；"
                         "ETF 漏标会多算印花税，建议补 type 列（stock/etf）精算。\n")
    t['shadow_any'] = shadow_any   # 无 shadow 列时 False（老 CSV 兼容，行为不变）
    t['mode_missing'] = mode_missing
    return trades


# ---------- 统计 ----------
def summarize(trades):
    R = [t['R'] for t in trades]
    P = [t['P'] for t in trades]
    N = len(R)
    W = [r for r in R if r > 0]
    L = [r for r in R if r <= 0]  # 平手(R=0)算败：与 SKILL「胜率演化图」R_i≤0 记败同口径，
    #                              保 N=N_W+N_L、q=1-p 严格成立、EV=pR_W+qR_L=mean(R) 恒等。
    #                              副作用：R_L 含 R=0 笔会被向 0 拉低（反映「没赢」），日内平手极罕见。
    return dict(N=N, R=R, P=P, W=W, L=L,
                p=len(W) / N, q=len(L) / N,
                RW=mean(W) if W else float('nan'),
                RL=mean(L) if L else float('nan'),
                EV=mean(R), Pbar=mean(P),
                s=stdev(R) if N > 1 else 0.0)

def bayes_nig(R, prior=(0.0, 1.0, 1.0, 1.0)):
    """完整贝叶斯 NIG 共轭：μ 与 σ² 联合估计，μ 边缘后验为 t 分布。"""
    m0, k0, a0, b0 = prior
    n = len(R); xbar = mean(R)
    S = sum((r - xbar) ** 2 for r in R)
    mn = (k0 * m0 + n * xbar) / (k0 + n)
    kn = k0 + n; an = a0 + n / 2
    bn = b0 + 0.5 * S + 0.5 * k0 * n * (xbar - m0) ** 2 / (k0 + n)
    df = 2 * an; scale = math.sqrt(bn / (an * kn))
    return dict(mn=mn, scale=scale, df=df,
                P_pos=_t_cdf(mn / scale, df))  # t 对称：P(μ>0) = T(mn/scale)

def sigma_ci(s, n):
    if n < 2: return None
    lo = math.sqrt((n - 1) * s ** 2 / _chi2_ppf(0.975, n - 1))
    hi = math.sqrt((n - 1) * s ** 2 / _chi2_ppf(0.025, n - 1))
    return lo, hi

def ppos_empirical(xbar, n, sigma, mu0=0.0, tau0=1.0):
    """固定 σ 的正态后验 P(EV>0)——用于敏感性区间两端近似。"""
    pp, pd = 1 / tau0 ** 2, n / sigma ** 2
    mu = (pp * mu0 + pd * xbar) / (pp + pd)
    return _norm_cdf(mu * math.sqrt(pp + pd))  # 后验 sd=1/√(pp+pd)，故 mu/sd = mu·√(pp+pd)


def p_g_pos(R, f):
    """P(g>0)：固定风险比例 f 下，每笔对数增长率 g=E[ln(1+fR)]>0 的后验概率（频率派 t）。
    Y=ln(1+fR)，P_pos = t_cdf(√N·ȳ_Y/sY, df=N-1)；σ 不确定区间用 ppos_empirical 两端正态。
    累计收益率 ≈ e^(n·g)-1，故 g>0 才长期复利增长——比 P(EV>0) 更贴合复利判据。
    注：不用 NIG(b0=1)——该先验假设 σ²~1，对 R（σ²~5）几乎无影响；但小 f 时 Y 被压缩到
    σ²~1e-4，b0=1 先验会主导后验、严重扭曲 P(g>0)（实测 f=0.5% 把 P 从~94% 压到 52%）。
    频率派 t 用样本 sY、不受先验尺度扭曲；与 p_sum_y_pos 同框架。"""
    Y = _log1p_fR(R, f)   # 带定义域保护（1+fR≤0 时压下限，防 ValueError 崩脚本）
    N = len(Y); ybar = mean(Y)
    if N < 2:
        return dict(P_pos=float('nan'), lo=None, hi=None, g_hat=ybar, s_Y=0.0)
    sY = stdev(Y)
    out = dict(g_hat=ybar, s_Y=sY,
               P_pos=_t_cdf(math.sqrt(N) * ybar / sY, N - 1))
    ci = sigma_ci(sY, N)
    a = ppos_empirical(ybar, N, ci[0]); b = ppos_empirical(ybar, N, ci[1])
    out['lo'], out['hi'] = min(a, b), max(a, b)
    return out


def p_sum_y_pos(R, f, n=40):
    """P(∑_{i=1}^n Y_i ≥ 0)：固定 f、未来 n 笔对数收益和 ≥0 的预测概率（有限 n 不亏概率）。
    频率派 t 预测近似（NIG 后验预测的简化）：t_stat=√(nN/(N+n))·ȳ_Y/sY, df=N-1。
    σ 不确定区间用正态（固定 σ）两端。N<2 返回 nan。
    与 p_g_pos 的区别：p_g_pos 回答「长期是否有 edge」(N→∞)，本函数回答「接下来 n 笔不亏」(有限 n)。"""
    Y = _log1p_fR(R, f)   # 带定义域保护（1+fR≤0 时压下限，防 ValueError 崩脚本）
    N = len(Y); ybar = mean(Y)
    if N < 2:
        return dict(P_pos=float('nan'), lo=None, hi=None, g_hat=ybar, s_Y=0.0)
    sY = stdev(Y)
    k = math.sqrt(n * N / (N + n))
    out = dict(g_hat=ybar, s_Y=sY, P_pos=_t_cdf(k * ybar / sY, N - 1))
    ci = sigma_ci(sY, N)
    a = _norm_cdf(k * ybar / ci[0]); b = _norm_cdf(k * ybar / ci[1])
    out['lo'], out['hi'] = min(a, b), max(a, b)
    return out


def p_g_target(R, f, n, target):
    """P(g ≥ ln(1+target)/n)：每笔对数增长率 g 的后验，「n 笔累计收益率 ≥ target 所需 g」成立的概率。
    g 后验（频率派 t）~ t(ȳ_Y, s_Y/√N)，P(g≥c)=t_cdf(√N(ȳ-c)/s_Y, N-1)，c=ln(1+target)/n。
    target=0 时 c=0、退化为 P(g>0)（但本函数绑 n；p_g_pos 不绑 n、语义是「长期不亏」）。"""
    Y = _log1p_fR(R, f)   # 带定义域保护（1+fR≤0 时压下限，防 ValueError 崩脚本）
    N = len(Y); ybar = mean(Y)
    c = math.log(1 + target) / n
    if N < 2:
        return dict(P_pos=float('nan'), lo=None, hi=None, g_hat=ybar, s_Y=0.0, c=c)
    sY = stdev(Y)
    out = dict(g_hat=ybar, s_Y=sY, c=c, target=target, n=n,
               P_pos=_t_cdf(math.sqrt(N) * (ybar - c) / sY, N - 1))
    ci = sigma_ci(sY, N)
    a = _norm_cdf(math.sqrt(N) * (ybar - c) / ci[0]); b = _norm_cdf(math.sqrt(N) * (ybar - c) / ci[1])
    out['lo'], out['hi'] = min(a, b), max(a, b)
    return out


def p_sum_y_target(R, f, n, target):
    """P(∑_{i=1}^n Y_i ≥ ln(1+target))：未来 n 笔累计收益率 ≥ target 的预测概率（有限 n）。
    预测和 ∑Y ~ t(n·ȳ_Y, s_Y·√(n(1+n/N)), df=N-1)。target=0 退化为 p_sum_y_pos。"""
    Y = _log1p_fR(R, f)   # 带定义域保护（1+fR≤0 时压下限，防 ValueError 崩脚本）
    N = len(Y); ybar = mean(Y)
    c = math.log(1 + target)
    if N < 2:
        return dict(P_pos=float('nan'), lo=None, hi=None, g_hat=ybar, s_Y=0.0, c=c)
    sY = stdev(Y)
    scale = sY * math.sqrt(n * (1 + n / N))
    out = dict(g_hat=ybar, s_Y=sY, c=c, target=target, n=n,
               P_pos=_t_cdf((n * ybar - c) / scale, N - 1))
    ci = sigma_ci(sY, N)
    a = _norm_cdf((n * ybar - c) / (ci[0] * math.sqrt(n * (1 + n / N))))
    b = _norm_cdf((n * ybar - c) / (ci[1] * math.sqrt(n * (1 + n / N))))
    out['lo'], out['hi'] = min(a, b), max(a, b)
    return out


def fetch_hl(ctx, code, t_start, t_end):
    """连富途拉 [t_start, t_end]（±1 分钟外扩）的 1 分钟 K，返回 (high, low, err)。"""
    import datetime as _dt
    from futu import KLType
    fmt = '%Y-%m-%d %H:%M:%S'
    try:
        s = (_dt.datetime.strptime(t_start, fmt) - _dt.timedelta(minutes=1)).strftime(fmt)
        e = (_dt.datetime.strptime(t_end,   fmt) + _dt.timedelta(minutes=1)).strftime(fmt)
        ret, kd, _ = ctx.request_history_kline(code, start=s, end=e, ktype=KLType.K_1M, max_count=10000)
    except Exception as ex:
        return None, None, f"请求异常 {ex}"
    if ret != 0 or kd is None or len(kd) == 0:
        return None, None, f"拉取失败 ret={ret}（检查 OpenD 在线 / symbol 前缀 / 时区 / 时段）"
    return float(kd['high'].max()), float(kd['low'].min()), None


# ---------- 输出 ----------
def main():
    ap = argparse.ArgumentParser(description="交易复盘统计")
    ap.add_argument('csv', help="已平仓交易清单 CSV 路径")
    ap.add_argument('--fetch-futu', action='store_true',
                    help="缺 raw_high/raw_low 时连富途按 entry_time/exit_time 拉分钟 K 取 high/low")
    ap.add_argument('--shadow-only', action='store_true',
                    help="只统计 shadow 列=1 的影子样本（auto 互斥闸被拦机会的纸面记录，"
                         "2026-08-27 立）——单独跑影子子集，与真实样本分开统计"
                         "（等价 --mode shadow，保留向后兼容）")
    ap.add_argument('--mode', choices=('signal', 'auto_paper', 'auto_live', 'shadow'),
                    help="只统计某一交易模式的子集（T134，2026-08-31 立）——signal=信号模式 / "
                         "auto_paper=auto 模拟 / auto_live=auto 实盘 / shadow=影子纸面样本；"
                         "分阶段统计从按日期近似切分改为按 mode 列分列")
    args = ap.parse_args()
    if args.shadow_only and args.mode and args.mode != 'shadow':
        sys.exit("❌ --shadow-only 与 --mode 冲突（--shadow-only 等价 --mode shadow）")
    if args.shadow_only:
        args.mode = 'shadow'   # 统一到 mode 口径

    if not _HAVE_SCIPY:
        print("⚠️ 未装 scipy：t 分布用正态近似(小样本偏乐观)、χ²用 Wilson-Hilferty 近似。建议 pip install scipy。\n")

    trades = load_trades(args.csv)

    # 影子样本口径（2026-08-27 立，auto-mode.md「影子交易」决策点 4）：
    # CSV 有 shadow=1 行时默认剔除（主统计 = 真实样本口径不变）；--mode shadow 反向只留影子。
    # 模式过滤（2026-08-31 T134）：--mode X 只留 mode=X 的行；CSV 无 mode 列（全部 unknown）
    # 时报错退出（先打标再分组）；部分行 unknown 时跳过该几行并提醒。
    _n_shadow = sum(1 for t in trades if t['shadow'])
    if args.mode == 'shadow':
        if _n_shadow == 0:
            sys.exit("❌ --mode shadow 但 CSV 无 shadow=1 行——先按 auto-mode.md「影子交易」落影子样本")
        trades = [t for t in trades if t['shadow']]
        print(f"【影子子集】shadow=1 共 {_n_shadow} 笔（决策时刻价近似、无真实滑点、被拦机会样本，"
              f"与真实样本不同质——只单独看、不并入主统计）\n")
    elif args.mode:
        _sub = [t for t in trades if t['mode'] == args.mode]
        if not _sub:
            _have_mode_col = any(t['mode'] != 'unknown' for t in trades)
            sys.exit(f"❌ --mode {args.mode} 无匹配行——"
                     + ("先给 CSV 补 mode 列（signal/auto_paper/auto_live/shadow，转录时按当日执行模式打标）"
                        if not _have_mode_col else "检查 mode 列取值"))
        if len(_sub) < len(trades):
            _unk = sum(1 for t in trades if t['mode'] == 'unknown')
            print(f"【模式子集】--mode {args.mode}：{_sub}/{len(trades)} 笔"
                  + (f"（{_unk} 笔 unknown 未计入——补 mode 列后重跑）" if _unk else "") + "\n")
        trades = _sub
    elif _n_shadow:
        _n_real = len(trades) - _n_shadow
        print(f"【影子剔除】CSV 含 shadow=1 影子样本 {_n_shadow} 笔，主统计已剔除（剩真实样本 {_n_real} 笔）——"
              f"影子单独跑：python3 review.py <csv> --mode shadow\n")
        trades = [t for t in trades if not t['shadow']]

    if args.fetch_futu:
        try:
            from futu import OpenQuoteContext
        except ImportError:
            sys.exit("❌ --fetch-futu 需要 futu-api（pip install futu-api）")
        print("【--fetch-futu】连富途 OpenD 拉持仓期间 high/low ...")
        ctx = OpenQuoteContext('127.0.0.1', 11111)
        try:
            for t in trades:
                if t['hi'] is not None and t['lo'] is not None:
                    print(f"  · {t['symbol']}：CSV 已提供 raw_high/raw_low，跳过")
                    continue
                if not t.get('entry_time') or not t.get('exit_time'):
                    print(f"  ⚠️ {t['symbol']}：缺 entry_time/exit_time，跳过")
                    continue
                hi, lo, err = fetch_hl(ctx, t['symbol'], t['entry_time'], t['exit_time'])
                if err:
                    print(f"  ⚠️ {t['symbol']} {t['entry_time']}→{t['exit_time']}：{err}")
                    continue
                t['hi'], t['lo'] = hi, lo
                compute_process(t)
                print(f"  ✓ {t['symbol']} {t['entry_time']}→{t['exit_time']}：high={hi:.3f} low={lo:.3f}")
        finally:
            ctx.close()
        print()

    S = summarize(trades)
    N = S['N']

    print("=" * 66)
    print(f"交易复盘 · N={N} 笔已平仓")
    print("=" * 66)

    # 1. 样本明细
    print("\n【样本明细】（P / R 均为扣双边手续费后的净额；fee = 开+平两边手续费；"
          "净M = 毛 M + 开仓费 + 止损价平仓费（2026-08-28 分母净口径））")
    print(f"{'#':<3}{'日期':<11}{'标的':<16}{'向':<4}{'模式':<11}{'entry→exit':<18}{'P净':>9}{'fee':>8}{'净M':>10}{'R净':>9}")
    for i, t in enumerate(trades, 1):
        d = '多' if t['sign'] > 0 else '空'
        ee = f"{t['entry']}→{t['exit']}"
        print(f"{i:<3}{t['date']:<11}{t['symbol']:<16}{d:<4}{t['mode']:<11}{ee:<18}{t['P']:>+9.1f}{t['fee']:>8.1f}{t['M_net']:>10.0f}{t['R']:>+9.3f}")

    # 2. 终局统计量
    print("\n【终局统计量】")
    print(f"  N={N}   胜率 p={S['p']:.3f}   败率 q={S['q']:.3f}")
    print(f"  胜赔率 R_W={S['RW']:.3f}   败赔率 R_L={S['RL']:.3f}")
    print(f"  EV={S['EV']:+.4f} (EV%={S['EV'] * 100:+.2f})   平均每单 P̄={S['Pbar']:+.1f}")
    print(f"  R 样本标准差 s={S['s']:.3f}")

    # 2b. 按模式分组（2026-08-31 T134：分阶段统计从按日期近似切分改为按 mode 列分列——
    # 复盘报告的「分阶段统计」「按模式分组」表直接引用本节输出；--mode 单子集模式也打印
    # 全模式对照（信息不丢）。CSV 无 mode 列（全 unknown）时跳过 + 提醒补标。
    _by_mode = {}
    for t in trades:
        _by_mode.setdefault(t['mode'], []).append(t)
    if any(m != 'unknown' for m in _by_mode):
        print("\n【按模式分组】（mode 列分列，复盘报告「分阶段统计」以此为准——日期近似切分口径废止）")
        print(f"  {'模式':<12}{'N':>4}{'胜率':>9}{'EV(R)':>10}{'平均R':>9}")
        for m in ('signal', 'auto_paper', 'auto_live', 'shadow', 'unknown'):
            if m not in _by_mode:
                continue
            sub = _by_mode[m]
            R_m = [t['R'] for t in sub]
            p_m = sum(1 for r in R_m if r > 0) / len(R_m)
            ev_m = mean(R_m)
            print(f"  {m:<12}{len(R_m):>4}{p_m:>9.3f}{ev_m:>+10.4f}{ev_m:>+9.3f}")
        if 'unknown' in _by_mode:
            print(f"  ⚠️ unknown {len(_by_mode['unknown'])} 笔（缺 mode 列/未打标）——转录 CSV 时按当日执行模式补标")
    elif trades and trades[0].get('mode_missing'):
        print("\n【按模式分组】⚠️ CSV 缺 mode 列（无法分组）——转录 CSV 时加 mode 列"
              "（signal/auto_paper/auto_live/shadow，T134）")

    # 3. 过程指标
    if trades[0]['MAE_R'] is not None:
        # 分组口径与 summarize 一致（2026-08-17 修）：R=0（平手）归败——原来 Ld 用 R<0，
        # R=0 的笔从两组都消失，过程指标 n 加总 < N、与终局统计（summarize 把 R=0 归败）打架。
        Wd = [t for t in trades if t['R'] > 0]
        Ld = [t for t in trades if t['R'] <= 0]
        def g(td, k):
            v = [t[k] for t in td if t[k] is not None]
            return mean(v) if v else float('nan')
        wl = f"盈利单 W(n={len(Wd)})"
        ll = f"亏损单 L(n={len(Ld)})"
        print("\n【过程指标 · 分盈利单 / 亏损单】")
        print(f"  {'子集':<16}{'MAE(防守)':>12}{'MFE(进攻)':>12}{'回吐(出场)':>12}{'锁利效率η':>12}")
        print(f"  {wl:<16}{g(Wd,'MAE_R'):>12.3f}{g(Wd,'MFE_R'):>12.3f}{g(Wd,'tuhui'):>12.3f}{g(Wd,'eta'):>12.3f}")
        print(f"  {ll:<16}{g(Ld,'MAE_R'):>12.3f}{g(Ld,'MFE_R'):>12.3f}{g(Ld,'tuhui'):>12.3f}{'—':>12}")
        print("  (MAE→0 防守越好；MFE 越大进攻越好；回吐越小出场越好；η→1 锁利越充分；η 仅盈利单)")
    else:
        print("\n【过程指标】⚠️ CSV 未提供 raw_high / raw_low，跳过。")
        print("   算过程指标需持仓期间最高/最低价：富途分钟K按开/平仓时间戳回拉，或平仓时原生记录 mfe_R / mae_R。")

    # 4. 贝叶斯 P(EV>0)
    print("\n【贝叶斯 P(EV>0) · 完整贝叶斯 NIG（t 后验）】")
    B = bayes_nig(S['R'])
    print(f"  先验 NIG(m0=0, k0=1, a0=1, b0=1) 弱信息")
    print(f"  后验: μ ~ t{B['df']:.0f}(位置 {B['mn']:+.3f}, 尺度 {B['scale']:.3f})")
    ci = sigma_ci(S['s'], N)
    if ci:
        pa, pb = ppos_empirical(S['EV'], N, ci[0]), ppos_empirical(S['EV'], N, ci[1])
        slo, shi = min(pa, pb), max(pa, pb)
        span = (shi - slo) * 100
        print(f"  P(EV>0) = {B['P_pos'] * 100:.1f}%（σ 不确定下 {slo * 100:.1f}%~{shi * 100:.1f}%）  ← 一律带区间")
        print(f"  σ 的 95% CI（n={N}, 卡方）: [{ci[0]:.3f}, {ci[1]:.3f}]（跨 {ci[1] / ci[0]:.1f} 倍）")
        if span > 5 or abs(B['P_pos'] - 0.5) < 0.30:
            dr = '正' if B['P_pos'] > 0.5 else '负'
            print(f"  判读: 跨度 {span:.0f}pp(>5pp)或点值近 50% → 小样本，只取方向(略偏{dr})，不作加仓/改策略决策。")
        else:
            print(f"  判读: 跨度 {span:.0f}pp(≤5pp)，点值有参考；区间整体 >95% = {shi > 0.95}。")
        print("  跨度随 N 按 1/√n 收窄：N=4~10 不可确认、N=20~30 边界、N≈50+ 才有确认价值（与「样本量规划」一致）。")
    else:
        print(f"  P(EV>0) = {B['P_pos'] * 100:.1f}%（N<2，无法算 σ CI / 敏感性区间）")

    # 5. 频率派对照
    print("\n【频率派对照】")
    se = S['s'] / math.sqrt(N)
    lo, hi = S['EV'] - 1.96 * se, S['EV'] + 1.96 * se
    print(f"  EV 95% CI = {S['EV']:+.3f} ± {1.96 * se:.3f} = [{lo:+.3f}, {hi:+.3f}]")
    print(f"  {'跨过 0 → 不足以判断 EV 正负（与贝叶斯一致）' if lo < 0 < hi else '区间全正 / 全负'}")

    # 6. 样本量规划
    s = S['s']
    print(f"\n【样本量规划 · 代入当前 s={s:.3f}】")
    z2 = 1.96 ** 2
    print(f"  胜率(p=0.5): ±5%→{z2 * 0.25 / 0.05 ** 2:.0f} 笔   ±3%→{z2 * 0.25 / 0.03 ** 2:.0f} 笔")
    print(f"  EV 估计: ±0.2R→{z2 * s ** 2 / 0.2 ** 2:.0f} 笔   ±0.1R→{z2 * s ** 2 / 0.1 ** 2:.0f} 笔")
    f80 = (1.645 + 0.8416) ** 2
    print(f"  确认 EV>0(80%把握): 真实 EV=0.10R→{f80 * s ** 2 / 0.10 ** 2:.0f} 笔   0.20R→{f80 * s ** 2 / 0.20 ** 2:.0f} 笔")
    print("  (鸡生蛋：基于当前 N 估的 s，初步规划、非定论；每次复盘用当下样本重算 s 重填)")

    print("\n⚠️ 盈亏按信号参考价与 max_loss 实算、扣双边手续费（真实费率：港股佣金 max(15,×0.029%) + 印花税(个股0.1%/ETF免) + 征费 + 固定平台费15/笔；美股佣金0.0039/股 + 平台费0.004/股(最低1/笔) + 代收0.00396/股，见 fee_schedule.py），不涉及真实账户资金（信号模式）。R 分母 = 净 max_loss（毛 M + 开仓费 + 止损价平仓费，2026-08-28 净口径）——止损价精确成交的笔净 R 恰好 −1.000。")


if __name__ == '__main__':
    main()
