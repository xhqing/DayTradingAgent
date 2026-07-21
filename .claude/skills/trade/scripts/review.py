#!/usr/bin/env python3
"""交易复盘统计脚本：读已平仓交易清单 CSV，输出全套评估指标。
用法: python3 review.py <trades.csv>

CSV 字段（首行表头，逗号分隔；# 开头行当注释跳过）:
  date, symbol, direction, entry_price, exit_price, shares, max_loss
  [, entry_time, exit_time, raw_high, raw_low]
  - symbol: 富途格式带市场前缀（HK.03690 / US.SOXL），--fetch-futu 时直接传富途
  - direction: long / short（也接受 做多/做空/多/空）
  - max_loss: 该笔最大损失金额（本币，config 风险额度向下取整后的实际值）
  - entry_time / exit_time: 开/平仓时刻 'YYYY-MM-DD HH:MM:SS'，写该标的【交易所当地时区】
    （港股 HKT、美股 ET）；仅 --fetch-futu 时需要，用于拉持仓期间分钟 K
  - raw_high / raw_low: 持仓期间最高/最低价，可选；提供则直接用（优先于 --fetch-futu 拉取），
    缺失则 --fetch-futu 时自动从富途拉、否则跳过过程指标

用法:
  python3 review.py <trades.csv>                  # 用 CSV 自带 raw_high/raw_low
  python3 review.py <trades.csv> --fetch-futu     # 缺 high/low 时连富途按时间戳自动拉

输出（R-multiple 体系，与 SKILL「复盘分析」一致）:
  1. 样本明细
  2. 终局统计量（胜率 / 败率 / 胜赔率 / 败赔率 / EV / EV% / 平均每单盈亏）
  3. 过程指标分盈利单 / 亏损单各一组（MAE / MFE / 回吐 / 锁利效率 η）
  4. 贝叶斯 P(EV>0)（完整贝叶斯 NIG，t 后验）+ σ 不确定下敏感性区间（一律带区间）
  5. 频率派 EV 的 95% CI（对照）
  6. 样本量规划（代入当前 s）

⚠️ 盈亏按信号参考价与 max_loss 实算，不涉及真实账户资金（信号模式：AI 不管账户）。
依赖: scipy（无则退化：t→正态近似、χ²→Wilson-Hilferty，小样本偏乐观；建议装 scipy）。
"""
import sys, csv, math, argparse
from statistics import mean, stdev


# ---------- 分布函数（scipy 优先，无则退化）----------
def _norm_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))

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
        t['MAE_R'] = -mae_amt / t['M']            # 浮亏峰值（负值，越接近 0 越好）
        t['MFE_R'] =  mfe_amt / t['M']            # 浮盈峰值（正值，越大越好）
        t['tuhui'] = max(t['MFE_R'] - t['R'], 0)  # 回吐（越小越好）
        t['eta']   = t['R'] / t['MFE_R'] if t['MFE_R'] > 0 else None  # 锁利效率（仅盈利单）
    else:
        t['MAE_R'] = t['MFE_R'] = t['tuhui'] = t['eta'] = None


def load_trades(path):
    with open(path, newline='') as f:
        rows = list(csv.DictReader(_strip_comments(f)))
    if not rows: sys.exit("❌ CSV 无数据行")
    trades = []
    for i, r in enumerate(rows, 1):
        try:
            t = dict(date=r['date'].strip(), symbol=r['symbol'].strip(),
                     sign=_direction(r['direction']),
                     entry=float(r['entry_price']), exit=float(r['exit_price']),
                     shares=float(r['shares']), M=float(r['max_loss']),
                     hi=_opt_float(r.get('raw_high')), lo=_opt_float(r.get('raw_low')),
                     entry_time=(r.get('entry_time') or '').strip() or None,
                     exit_time=(r.get('exit_time') or '').strip() or None)
        except Exception as e:
            sys.exit(f"❌ 第{i}行解析失败: {e}\n  {r}")
        t['P'] = (t['exit'] - t['entry']) * t['shares'] * t['sign']
        t['R'] = t['P'] / t['M']
        compute_process(t)
        trades.append(t)
    return trades


# ---------- 统计 ----------
def summarize(trades):
    R = [t['R'] for t in trades]
    P = [t['P'] for t in trades]
    N = len(R)
    W = [r for r in R if r > 0]
    L = [r for r in R if r < 0]
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
    args = ap.parse_args()

    if not _HAVE_SCIPY:
        print("⚠️ 未装 scipy：t 分布用正态近似(小样本偏乐观)、χ²用 Wilson-Hilferty 近似。建议 pip install scipy。\n")

    trades = load_trades(args.csv)

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
    print("\n【样本明细】")
    print(f"{'#':<3}{'日期':<11}{'标的':<16}{'向':<4}{'entry→exit':<18}{'P':>9}{'max_loss':>10}{'R=P/M':>9}")
    for i, t in enumerate(trades, 1):
        d = '多' if t['sign'] > 0 else '空'
        ee = f"{t['entry']}→{t['exit']}"
        print(f"{i:<3}{t['date']:<11}{t['symbol']:<16}{d:<4}{ee:<18}{t['P']:>+9.1f}{t['M']:>10.0f}{t['R']:>+9.3f}")

    # 2. 终局统计量
    print("\n【终局统计量】")
    print(f"  N={N}   胜率 p={S['p']:.3f}   败率 q={S['q']:.3f}")
    print(f"  胜赔率 R_W={S['RW']:.3f}   败赔率 R_L={S['RL']:.3f}")
    print(f"  EV={S['EV']:+.4f} (EV%={S['EV'] * 100:+.2f})   平均每单 P̄={S['Pbar']:+.1f}")
    print(f"  R 样本标准差 s={S['s']:.3f}")

    # 3. 过程指标
    if trades[0]['MAE_R'] is not None:
        Wd = [t for t in trades if t['R'] > 0]
        Ld = [t for t in trades if t['R'] < 0]
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

    print("\n⚠️ 盈亏按信号参考价与 max_loss 实算，不涉及真实账户资金（信号模式）。")


if __name__ == '__main__':
    main()
