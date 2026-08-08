#!/usr/bin/env python3
"""序贯贝叶斯 P(EV>0) 折线图：按交易时间顺序逐笔累积，画点估计+下界+上界三条线。
复用 review.py 的完整贝叶斯 NIG（t 后验）+ σ 不确定下敏感性区间算法。

用法（2026-08-05 起支持 --date，不再手动改路径）：
  python3 bayes_evolution.py                     # 自动找 reviews/ 下最新 *-trades.csv
  python3 bayes_evolution.py --date 2026-08-04   # 指定复盘日期（读 reviews/2026-08-04-trades.csv、
                                                 # 输出 reviews/2026-08-04-*.png）
"""
import csv, math, sys, os, glob, re
from statistics import mean, stdev
from scipy import stats as ss
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # 自身目录（scripts/），与 review.py 同目录
from review import p_g_pos, p_sum_y_pos, p_g_target, p_sum_y_target

def bayes_nig(R, prior=(0.0, 1.0, 1.0, 1.0)):
    m0, k0, a0, b0 = prior
    n = len(R); xbar = mean(R)
    S = sum((r - xbar) ** 2 for r in R)
    mn = (k0 * m0 + n * xbar) / (k0 + n)
    kn = k0 + n; an = a0 + n / 2
    bn = b0 + 0.5 * S + 0.5 * k0 * n * (xbar - m0) ** 2 / (k0 + n)
    df = 2 * an; scale = math.sqrt(bn / (an * kn))
    return mn, scale, df, ss.t.cdf(mn / scale, df)

def sigma_ci(s, n):
    lo = math.sqrt((n - 1) * s ** 2 / ss.chi2.ppf(0.975, n - 1))
    hi = math.sqrt((n - 1) * s ** 2 / ss.chi2.ppf(0.025, n - 1))
    return lo, hi

def ppos_emp(xbar, n, sigma):
    pp, pd = 1.0, n / sigma ** 2
    mu = pd * xbar / (pp + pd)
    return ss.norm.cdf(mu * math.sqrt(pp + pd))

# 单边手续费率（2026-08-03 用户立：复盘 R / EV / 胜率等一律用扣费净盈亏 net）
# 港股 18bps/边、美股 3bps/边；一笔交易 = 开仓 + 平仓 两边各收一次。
def _fee_per_side(symbol):
    s = symbol.upper()
    if s.startswith('HK.'): return 0.0018   # 18bps
    if s.startswith('US.'): return 0.0003   # 3bps
    raise ValueError(f"未知市场前缀、无法定费率: {symbol!r}（只支持 HK. / US.）")

def _resolve_date(argv=None):
    """解析 --date YYYY-MM-DD（+可选 --suffix，如 -hk / -us，用于港美股分开出图）；
    不传 --date 自动找 reviews/ 下最新 *-trades.csv（按文件名日期取最大，忽略 -hk/-us 后缀文件）。

    2026-08-05 立：日期抽成变量、支持命令行参数，避免每次手改。
    2026-08-07 立：支持 --suffix 分市场出图——读 reviews/{DATE}-trades{suffix}.csv、
    输出 reviews/{DATE}{suffix}-*.png（如 --date 2026-08-07 --suffix -hk）。
    """
    date, suffix = None, ""
    if argv:
        i = 0
        while i < len(argv):
            a = argv[i]
            if a == "--date" and i + 1 < len(argv):
                date = argv[i + 1]
                i += 2
                continue
            if a.startswith("--date="):
                date = a.split("=", 1)[1]
            if a == "--suffix" and i + 1 < len(argv):
                suffix = argv[i + 1]
                i += 2
                continue
            if a.startswith("--suffix="):
                suffix = a.split("=", 1)[1]
            i += 1
    if not date:
        cand = []
        for p in glob.glob(os.path.join("reviews", "*-trades.csv")):
            m = re.search(r"(\d{4}-\d{2}-\d{2})-trades\.csv$", os.path.basename(p))
            if m:
                cand.append((m.group(1), p))
        if not cand:
            raise SystemExit("未找到 reviews/*-trades.csv，请用 --date YYYY-MM-DD 指定")
        cand.sort(key=lambda x: x[0])
        date = cand[-1][0]
    return date, suffix


DATE, SUFFIX = _resolve_date(sys.argv[1:])
CSV_PATH = f"reviews/{DATE}-trades{SUFFIX}.csv"

# 读累积 trades CSV（单一数据源，与 review.py 同源；每次复盘更新此 CSV）
# R = 扣双边手续费后的净 R（与 review.py net 口径一致）
trades = []
with open(CSV_PATH) as fh:
    for r in csv.DictReader(fh):
        sign = 1 if r['direction'].strip().lower() in ('long', '做多') else -1
        entry, exit_, shares = float(r['entry_price']), float(r['exit_price']), float(r['shares'])
        P_gross = (exit_ - entry) * shares * sign                       # 毛盈亏
        fee = _fee_per_side(r['symbol']) * (entry + exit_) * shares     # 开 + 平 两边手续费
        R = (P_gross - fee) / float(r['max_loss'])                      # 净 R（分母 max_loss 保持毛值）
        trades.append([r['date'], r['symbol'], R])
trades.sort(key=lambda x: (x[0], x[1]))

# 序贯累积：P(EV>0) 从 N>=2 起；EV（累计 R 均值 ± 频率派 95% CI）从 N>=1 起、CI 从 N>=2 起
xs, ppos, los, his, r_cum = [], [], [], [], []
ev_xs, ev_mean, ev_lo, ev_hi = [], [], [], []
# 胜率演化（Beta-伯努利 共轭，先验 Beta(1,1)；后验均值+频率对照+95%CI 均从 N=1 起画）
wr_xs, wr_bayes, wr_freq, wr_lo, wr_hi = [], [], [], [], []
alpha_p, beta_p = 1.0, 1.0  # 先验 Beta(1,1)，不预设胜率偏向
for i, t in enumerate(trades, 1):
    r_cum.append(t[2])
    xbar = mean(r_cum)
    # EV 演化（累计 R 均值 = 序贯 EV 点估计；频率派 95% CI = ±1.96·s/√n，与 review.py 频率派对照同口径）
    ev_xs.append(i); ev_mean.append(xbar)
    if i >= 2:
        se = stdev(r_cum) / math.sqrt(i)
        ev_lo.append(xbar - 1.96 * se); ev_hi.append(xbar + 1.96 * se)
    else:
        ev_lo.append(xbar); ev_hi.append(xbar)  # N=1 算不出 s、无 CI，退化为点
    # 胜率演化（Beta-伯努利 共轭）：R>0 记胜，Beta(1,1) 先验逐笔更新
    if t[2] > 0:
        alpha_p += 1
    else:
        beta_p += 1
    wr_xs.append(i)
    wr_bayes.append(alpha_p / (alpha_p + beta_p) * 100)
    wins = sum(1 for r in r_cum if r > 0)
    wr_freq.append(wins / i * 100)
    wr_lo.append(ss.beta.ppf(0.025, alpha_p, beta_p) * 100)
    wr_hi.append(ss.beta.ppf(0.975, alpha_p, beta_p) * 100)
    print(f'WINRATE N={i:2d} {t[1]:14s} R={t[2]:+.2f} 贝叶斯={wr_bayes[-1]:5.1f}% 频率={wr_freq[-1]:5.1f}% CI=[{wr_lo[-1]:4.0f},{wr_hi[-1]:3.0f}]')
    # P(EV>0) 演化（N>=2 起：N=1 算不出 σ）
    if i == 1:
        continue
    mn, scale, df, pp = bayes_nig(r_cum)
    s = stdev(r_cum)
    clo, chi = sigma_ci(s, i)
    plo, phi = ppos_emp(mean(r_cum), i, clo), ppos_emp(mean(r_cum), i, chi)
    xs.append(i); ppos.append(pp * 100)
    los.append(min(plo, phi) * 100); his.append(max(plo, phi) * 100)
    print(f'N={i:2d} R={t[2]:+.3f} 累计均值={xbar:+.3f} EV95%CI=[{ev_lo[-1]:+.2f},{ev_hi[-1]:+.2f}] P={pp*100:5.1f}% [{min(plo,phi)*100:.0f}~{max(plo,phi)*100:.0f}%]')

# 画图：① P(EV>0) 演化（贝叶斯）② EV 演化（累计 R 均值 ± 频率派 95% CI）
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.transforms import blended_transform_factory


def mark_end_single(ax, xs, ys, color='#E67E22', unit='%', x_name='N', val_fmt='{:.2f}'):
    """单线图末点标注：十字虚线（末点→x 轴 / 末点→y 轴）+ 末点高亮 + 右上方带框坐标文本。
    虚线用深灰色（与橙色主线对比，避免与主线末段重合时看不清）；末点圆点加大白边突出。"""
    lx, ly = xs[-1], ys[-1]
    ylim = ax.get_ylim(); xlim = ax.get_xlim()
    ybase = 0 if ylim[0] <= 0 <= ylim[1] else ylim[0]
    dash = dict(color='#333333', ls='--', alpha=0.8, lw=1.2, zorder=4)
    ax.plot([lx, lx], [ybase, ly], **dash)
    ax.plot([xlim[0], lx], [ly, ly], **dash)
    ax.plot(lx, ly, 'o', color=color, ms=9, mec='white', mew=1.5, zorder=5)
    ax.annotate(f'({x_name}={lx:g}, {val_fmt.format(ly)}{unit})',
                xy=(lx, ly), xytext=(10, 10), textcoords='offset points',
                fontsize=9.5, color=color, fontweight='bold', ha='left', va='bottom', zorder=6,
                bbox=dict(boxstyle='round,pad=0.3', fc='white', ec=color, alpha=0.9))


def mark_end_multi(ax, xs, ends, unit='%'):
    """多 f 图末点标注：1 条垂直虚线对齐横坐标 + 每线水平虚线对齐纵坐标 + 右侧避让 end-labels。
    ends = [(color, label, y_end), ...]（按时间序）；图需已 set_ylim。
    调用方需 plt.tight_layout(rect=[0, 0, 0.80, 1]) 留右侧 20% 空间放 end-labels。"""
    lx = xs[-1]
    ylim = ax.get_ylim(); xlim = ax.get_xlim()
    ybase = 0 if ylim[0] <= 0 <= ylim[1] else ylim[0]
    ymax = max(e[2] for e in ends)
    ax.plot([lx, lx], [ybase, ymax], color='#333333', ls='--', alpha=0.6, lw=1.1, zorder=3)  # 横坐标对齐
    ax_yx = blended_transform_factory(ax.transAxes, ax.transData)
    for color, label, y_end in ends:
        ax.plot([xlim[0], lx], [y_end, y_end], color=color, ls='--', alpha=0.55, lw=1, zorder=3)  # 纵坐标对齐
        ax.plot(lx, y_end, 'o', color=color, ms=6, mec='white', mew=1, zorder=4)
    # 右侧 end-labels：按 y 降序、相邻太近则下压 + 短引导线，避免重叠
    gap = (ylim[1] - ylim[0]) * 0.045
    xa = 1.015
    prev_y = None
    for color, label, y_end in sorted(ends, key=lambda e: -e[2]):
        ly = y_end
        if prev_y is not None and prev_y - ly < gap:
            ly = prev_y - gap
        ly = min(max(ly, ylim[0] + gap * 0.4), ylim[1] - gap * 0.4)
        if abs(ly - y_end) > 0.05:
            ax.plot([xa - 0.006, xa - 0.006], [y_end, ly], color=color, lw=0.8, alpha=0.55,
                    transform=ax_yx, clip_on=False, zorder=4)
        ax.text(xa, ly, f'{label}: {y_end:.1f}{unit}', transform=ax_yx,
                ha='left', va='center', fontsize=8.5, color=color, fontweight='bold', zorder=5)
        prev_y = ly
    ax.text(0.99, 0.97, f'endpoint: N={lx:g}', transform=ax.transAxes,
            ha='right', va='top', fontsize=8.5, color='gray', alpha=0.85)


# ① P(EV>0) 演化图
fig, ax = plt.subplots(figsize=(13, 6.5))
ax.fill_between(xs, los, his, color='#3498DB', alpha=0.18, label='σ-uncertain range')
ax.plot(xs, his, '--', color='#3498DB', alpha=0.55, linewidth=1)
ax.plot(xs, los, '--', color='#3498DB', alpha=0.55, linewidth=1)
ax.plot(xs, ppos, '-o', color='#E67E22', linewidth=2.2, markersize=6, label='P(EV>0) point est.')
ax.axhline(50, color='gray', linestyle=':', alpha=0.5, label='50% neutral')
ax.axhline(95, color='green', linestyle=':', alpha=0.6, label='95% confirmed')
ax.set_xlabel('Trade # (chronological)', fontsize=11)
ax.set_ylabel('P(EV>0)  %', fontsize=11)
ax.set_title(f'Bayesian P(EV>0) Evolution  N=2~{len(trades)}  (full NIG t-posterior)', fontsize=13)
ax.set_ylim(0, 105)
ax.set_xticks(xs)
ax.set_xticklabels([f'{i}\n{trades[i-1][0][5:]}' for i in xs], fontsize=7.5)
ax.legend(loc='lower right', fontsize=9)
ax.grid(alpha=0.3)
mark_end_single(ax, xs, ppos, val_fmt='{:.1f}')
plt.tight_layout()
out = f'reviews/{DATE}{SUFFIX}-bayes-evolution.png'
plt.savefig(out, dpi=120)
print(f'\n✅ 图已存 {out}')

# ② EV 演化图（累计 R 均值 ± 频率派 95% CI；与①同源同横轴，纵轴换 R 倍数）
fig2, ax2 = plt.subplots(figsize=(13, 6.5))
ax2.fill_between(ev_xs, ev_lo, ev_hi, color='#3498DB', alpha=0.18, label='freq. 95% CI (±1.96·s/√n)')
ax2.plot(ev_xs, ev_hi, '--', color='#3498DB', alpha=0.55, linewidth=1)
ax2.plot(ev_xs, ev_lo, '--', color='#3498DB', alpha=0.55, linewidth=1)
ax2.plot(ev_xs, ev_mean, '-o', color='#E67E22', linewidth=2.2, markersize=6, label='cumulative mean R (EV)')
ax2.axhline(0, color='gray', linestyle=':', alpha=0.6, label='EV=0 neutral')
ax2.set_xlabel('Trade # (chronological)', fontsize=11)
ax2.set_ylabel('EV  (R multiple)', fontsize=11)
ax2.set_title(f'Cumulative EV Evolution  N=1~{len(trades)}  (mean R ± freq. 95% CI)', fontsize=13)
ax2.set_xticks(ev_xs)
ax2.set_xticklabels([f'{i}\n{trades[i-1][0][5:]}' for i in ev_xs], fontsize=7.5)
ax2.legend(loc='lower right', fontsize=9)
ax2.grid(alpha=0.3)
mark_end_single(ax2, ev_xs, ev_mean, unit='R', val_fmt='{:+.2f}')
plt.tight_layout()
out2 = f'reviews/{DATE}{SUFFIX}-ev-evolution.png'
plt.savefig(out2, dpi=120)
print(f'✅ 图已存 {out2}')

# ③ 胜率演化图（Beta(1,1) 后验均值 + 频率对照 + 95% 可信区间；与①②同源同横轴，纵轴换胜率%）
fig3, ax3 = plt.subplots(figsize=(13, 6.5))
ax3.fill_between(wr_xs, wr_lo, wr_hi, color='#9B59B6', alpha=0.16, label='Bayes 95% credible (Beta)')
ax3.plot(wr_xs, wr_hi, '--', color='#9B59B6', alpha=0.5, linewidth=1)
ax3.plot(wr_xs, wr_lo, '--', color='#9B59B6', alpha=0.5, linewidth=1)
ax3.plot(wr_xs, wr_bayes, '-o', color='#E67E22', linewidth=2.2, markersize=6, label='Bayes posterior mean (Beta(1,1))')
ax3.plot(wr_xs, wr_freq, '-s', color='#3498DB', linewidth=1.6, markersize=4.5, alpha=0.85, label='frequentist wins/N')
ax3.axhline(50, color='gray', linestyle=':', alpha=0.5, label='50% random neutral')
ax3.set_xlabel('Trade # (chronological)', fontsize=11)
ax3.set_ylabel('Win rate  %', fontsize=11)
ax3.set_title(f'Bayesian Win-Rate Evolution  N=1~{len(trades)}  (Beta(1,1) prior, Beta-Bernoulli)', fontsize=13)
ax3.set_ylim(0, 105)
ax3.set_xticks(wr_xs)
ax3.set_xticklabels([f'{i}\n{trades[i-1][0][5:]}' for i in wr_xs], fontsize=7.5)
ax3.legend(loc='lower right', fontsize=9)
ax3.grid(alpha=0.3)
mark_end_single(ax3, wr_xs, wr_bayes, val_fmt='{:.1f}')
plt.tight_layout()
out3 = f'reviews/{DATE}{SUFFIX}-winrate-evolution.png'
plt.savefig(out3, dpi=120)
print(f'✅ 图已存 {out3}')

# ④⑤ P(g>0) / P(∑_{1}^{40} Y≥0) 演化（多 f 折线）
#   g = E[ln(1+fR)]，累计收益率 ≈ e^(n·g)-1；P(g>0) 回答「长期是否有 edge」
#   P(∑_{1}^{40} Y≥0) 回答「接下来 40 笔不亏」（有限 n 预测，n=40 固定）
#   两者都随样本逐笔累积，不同 f 各一条线（f 越大方差惩罚越重）
FS = [0.005, 0.02, 0.10, 0.25, 0.50]   # 保守→f_max→激进→凯利f*→2f*临界（覆盖到能看出 f 对 P 的影响）
pg_xs = list(range(2, len(trades) + 1))   # N>=2 起（N=1 算不出 σ）
colors = plt.cm.tab10.colors

# ④ P(g>0) 演化图
fig4, ax4 = plt.subplots(figsize=(13, 6.5))
ends4 = []
for fi, f in enumerate(FS):
    pg_pts = [p_g_pos(r_cum[:i], f)['P_pos'] * 100 for i in pg_xs]
    ax4.plot(pg_xs, pg_pts, '-o', color=colors[fi], linewidth=1.8, markersize=4.5,
             label=f'f={f*100:.1f}%')
    ends4.append((colors[fi], f'f={f*100:.1f}%', pg_pts[-1]))
ax4.axhline(50, color='gray', linestyle=':', alpha=0.5, label='50% neutral')
ax4.axhline(95, color='green', linestyle=':', alpha=0.6, label='95% confirmed')
ax4.set_xlabel('Trade # (chronological)', fontsize=11)
ax4.set_ylabel('P(g>0)  %', fontsize=11)
ax4.set_title(f'Bayesian P(g>0) Evolution by risk fraction f  N=2~{len(trades)}'
              '  (g=E[ln(1+fR)], cumulative return ≈ $e^{ng}$-1)', fontsize=12)
ax4.set_ylim(0, 105)
ax4.set_xticks(pg_xs)
ax4.set_xticklabels([f'{i}\n{trades[i-1][0][5:]}' for i in pg_xs], fontsize=7.5)
ax4.legend(loc='lower right', fontsize=9, ncol=2)
ax4.grid(alpha=0.3)
mark_end_multi(ax4, pg_xs, ends4)
plt.tight_layout(rect=[0, 0, 0.80, 1])
out4 = f'reviews/{DATE}{SUFFIX}-pg-evolution.png'
plt.savefig(out4, dpi=120)
print(f'\n✅ 图已存 {out4}')

# ⑤ P(∑_{i=1}^{40} Y_i ≥ 0) 演化图（固定 n=40）
fig5, ax5 = plt.subplots(figsize=(13, 6.5))
ends5 = []
for fi, f in enumerate(FS):
    ps_pts = [p_sum_y_pos(r_cum[:i], f, 40)['P_pos'] * 100 for i in pg_xs]
    ax5.plot(pg_xs, ps_pts, '-o', color=colors[fi], linewidth=1.8, markersize=4.5,
             label=f'f={f*100:.1f}%')
    ends5.append((colors[fi], f'f={f*100:.1f}%', ps_pts[-1]))
ax5.axhline(50, color='gray', linestyle=':', alpha=0.5, label='50% neutral')
ax5.set_xlabel('Trade # (chronological)', fontsize=11)
ax5.set_ylabel('P(next 40 trades ≥ break-even)  %', fontsize=11)
ax5.set_title(r'P($\sum_{i=1}^{40} Y_i \geq 0$) Evolution by f  N=2~'
              + f'{len(trades)}  (n=40 fixed, t-predictive)', fontsize=12)
ax5.set_ylim(0, 105)
ax5.set_xticks(pg_xs)
ax5.set_xticklabels([f'{i}\n{trades[i-1][0][5:]}' for i in pg_xs], fontsize=7.5)
ax5.legend(loc='lower right', fontsize=9, ncol=2)
ax5.grid(alpha=0.3)
mark_end_multi(ax5, pg_xs, ends5)
plt.tight_layout(rect=[0, 0, 0.80, 1])
out5 = f'reviews/{DATE}{SUFFIX}-psum40-evolution.png'
plt.savefig(out5, dpi=120)
print(f'✅ 图已存 {out5}')

# ⑥⑦ P(累计收益率 ≥ target) 演化（多 f 折线）——对应④⑤的「不亏」版，阈值从 0 提到 ln(1+target)
#   ⑥ P(g ≥ ln(1+target)/n)：n 笔累计≥target 所需 g 的后验概率（参数不确定性）
#   ⑦ P(∑ₙ Y ≥ ln(1+target))：未来 n 笔累计≥target 的预测概率（含个体方差）
TARGET = 0.20   # 累计收益率目标 20%

# ⑥ P(g ≥ ln(1+target)/40) 演化图
fig6, ax6 = plt.subplots(figsize=(13, 6.5))
ends6 = []
for fi, f in enumerate(FS):
    pts = [p_g_target(r_cum[:i], f, 40, TARGET)['P_pos'] * 100 for i in pg_xs]
    ax6.plot(pg_xs, pts, '-o', color=colors[fi], linewidth=1.8, markersize=4.5,
             label=f'f={f*100:.1f}%')
    ends6.append((colors[fi], f'f={f*100:.1f}%', pts[-1]))
ax6.axhline(50, color='gray', linestyle=':', alpha=0.5, label='50% neutral')
ax6.set_xlabel('Trade # (chronological)', fontsize=11)
ax6.set_ylabel(f'P(40-trade return ≥ {TARGET*100:.0f}%)  %', fontsize=11)
ax6.set_title(f'P(g ≥ ln(1+{TARGET*100:.0f}%)/40) Evolution by f  N=2~{len(trades)}'
              '  (g-posterior, n=40)', fontsize=12)
ax6.set_ylim(0, 105)
ax6.set_xticks(pg_xs)
ax6.set_xticklabels([f'{i}\n{trades[i-1][0][5:]}' for i in pg_xs], fontsize=7.5)
ax6.legend(loc='lower right', fontsize=9, ncol=2)
ax6.grid(alpha=0.3)
mark_end_multi(ax6, pg_xs, ends6)
plt.tight_layout(rect=[0, 0, 0.80, 1])
out6 = f'reviews/{DATE}{SUFFIX}-pg{int(TARGET*100):02d}-evolution.png'
plt.savefig(out6, dpi=120)
print(f'✅ 图已存 {out6}')

# ⑦ P(∑_{1}^{40} Y ≥ ln(1+target)) 演化图（n=40 固定）
fig7, ax7 = plt.subplots(figsize=(13, 6.5))
ends7 = []
for fi, f in enumerate(FS):
    pts = [p_sum_y_target(r_cum[:i], f, 40, TARGET)['P_pos'] * 100 for i in pg_xs]
    ax7.plot(pg_xs, pts, '-o', color=colors[fi], linewidth=1.8, markersize=4.5,
             label=f'f={f*100:.1f}%')
    ends7.append((colors[fi], f'f={f*100:.1f}%', pts[-1]))
ax7.axhline(50, color='gray', linestyle=':', alpha=0.5, label='50% neutral')
ax7.set_xlabel('Trade # (chronological)', fontsize=11)
ax7.set_ylabel(f'P(next 40 trades return ≥ {TARGET*100:.0f}%)  %', fontsize=11)
ax7.set_title(r'P($\sum_{i=1}^{40} Y_i \geq \ln(1+$' + f'{TARGET*100:.0f}%)) Evolution by f  N=2~{len(trades)}  (n=40, t-predictive)', fontsize=12)
ax7.set_ylim(0, 105)
ax7.set_xticks(pg_xs)
ax7.set_xticklabels([f'{i}\n{trades[i-1][0][5:]}' for i in pg_xs], fontsize=7.5)
ax7.legend(loc='lower right', fontsize=9, ncol=2)
ax7.grid(alpha=0.3)
mark_end_multi(ax7, pg_xs, ends7)
plt.tight_layout(rect=[0, 0, 0.80, 1])
out7 = f'reviews/{DATE}{SUFFIX}-psum40-{int(TARGET*100):02d}pct-evolution.png'
plt.savefig(out7, dpi=120)
print(f'✅ 图已存 {out7}')

# 打印终局（N=全部）关键值，便于写进复盘文字
print('\n=== 终局（全部样本）P(g>0) 与 P(S₄₀≥0) ===')
for f in FS:
    G = p_g_pos(r_cum, f); S40 = p_sum_y_pos(r_cum, f, 40)
    print(f'f={f*100:>4.1f}%: ĝ={G["g_hat"]*100:+.3f}%/笔  '
          f'P(g>0)={G["P_pos"]*100:5.1f}%[σ不确定 {G["lo"]*100:.0f}~{G["hi"]*100:.0f}%]  '
          f'P(∑40Y≥0)={S40["P_pos"]*100:5.1f}%')

print(f'\n=== 终局（全部样本）P(累计≥{TARGET*100:.0f}%) ===')
for f in FS:
    G20 = p_g_target(r_cum, f, 40, TARGET); S20 = p_sum_y_target(r_cum, f, 40, TARGET)
    print(f'f={f*100:>4.1f}%: P(g≥ln1.2/40)={G20["P_pos"]*100:5.1f}%[σ不确定 {G20["lo"]*100:.0f}~{G20["hi"]*100:.0f}%]  '
          f'P(∑40Y≥ln1.2)={S20["P_pos"]*100:5.1f}%[σ不确定 {S20["lo"]*100:.0f}~{S20["hi"]*100:.0f}%]')
