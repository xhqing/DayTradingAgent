#!/usr/bin/env python3
"""序贯贝叶斯 P(EV>0) 折线图：按交易时间顺序逐笔累积，画点估计+下界+上界三条线。
复用 review.py 的完整贝叶斯 NIG（t 后验）+ σ 不确定下敏感性区间算法。"""
import csv, math, sys, os
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

# 读累积 trades CSV（单一数据源，与 review.py 同源；每次复盘更新此 CSV）
trades = []
with open('reviews/2026-07-29-trades.csv') as fh:
    for r in csv.DictReader(fh):
        sign = 1 if r['direction'].strip().lower() in ('long', '做多') else -1
        P = (float(r['exit_price']) - float(r['entry_price'])) * float(r['shares']) * sign
        trades.append([r['date'], r['symbol'], P / float(r['max_loss'])])
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
plt.tight_layout()
out = 'reviews/2026-07-29-bayes-evolution.png'
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
plt.tight_layout()
out2 = 'reviews/2026-07-29-ev-evolution.png'
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
plt.tight_layout()
out3 = 'reviews/2026-07-29-winrate-evolution.png'
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
for fi, f in enumerate(FS):
    pg_pts = [p_g_pos(r_cum[:i], f)['P_pos'] * 100 for i in pg_xs]
    ax4.plot(pg_xs, pg_pts, '-o', color=colors[fi], linewidth=1.8, markersize=4.5,
             label=f'f={f*100:.1f}%')
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
plt.tight_layout()
out4 = 'reviews/2026-07-29-pg-evolution.png'
plt.savefig(out4, dpi=120)
print(f'\n✅ 图已存 {out4}')

# ⑤ P(∑_{i=1}^{40} Y_i ≥ 0) 演化图（固定 n=40）
fig5, ax5 = plt.subplots(figsize=(13, 6.5))
for fi, f in enumerate(FS):
    ps_pts = [p_sum_y_pos(r_cum[:i], f, 40)['P_pos'] * 100 for i in pg_xs]
    ax5.plot(pg_xs, ps_pts, '-o', color=colors[fi], linewidth=1.8, markersize=4.5,
             label=f'f={f*100:.1f}%')
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
plt.tight_layout()
out5 = 'reviews/2026-07-29-psum40-evolution.png'
plt.savefig(out5, dpi=120)
print(f'✅ 图已存 {out5}')

# ⑥⑦ P(累计收益率 ≥ target) 演化（多 f 折线）——对应④⑤的「不亏」版，阈值从 0 提到 ln(1+target)
#   ⑥ P(g ≥ ln(1+target)/n)：n 笔累计≥target 所需 g 的后验概率（参数不确定性）
#   ⑦ P(∑ₙ Y ≥ ln(1+target))：未来 n 笔累计≥target 的预测概率（含个体方差）
TARGET = 0.20   # 累计收益率目标 20%

# ⑥ P(g ≥ ln(1+target)/40) 演化图
fig6, ax6 = plt.subplots(figsize=(13, 6.5))
for fi, f in enumerate(FS):
    pts = [p_g_target(r_cum[:i], f, 40, TARGET)['P_pos'] * 100 for i in pg_xs]
    ax6.plot(pg_xs, pts, '-o', color=colors[fi], linewidth=1.8, markersize=4.5,
             label=f'f={f*100:.1f}%')
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
plt.tight_layout()
out6 = f'reviews/2026-07-29-pg{int(TARGET*100):02d}-evolution.png'
plt.savefig(out6, dpi=120)
print(f'✅ 图已存 {out6}')

# ⑦ P(∑_{1}^{40} Y ≥ ln(1+target)) 演化图（n=40 固定）
fig7, ax7 = plt.subplots(figsize=(13, 6.5))
for fi, f in enumerate(FS):
    pts = [p_sum_y_target(r_cum[:i], f, 40, TARGET)['P_pos'] * 100 for i in pg_xs]
    ax7.plot(pg_xs, pts, '-o', color=colors[fi], linewidth=1.8, markersize=4.5,
             label=f'f={f*100:.1f}%')
ax7.axhline(50, color='gray', linestyle=':', alpha=0.5, label='50% neutral')
ax7.set_xlabel('Trade # (chronological)', fontsize=11)
ax7.set_ylabel(f'P(next 40 trades return ≥ {TARGET*100:.0f}%)  %', fontsize=11)
ax7.set_title(r'P($\sum_{i=1}^{40} Y_i \geq \ln(1+$' + f'{TARGET*100:.0f}%)) Evolution by f  N=2~{len(trades)}  (n=40, t-predictive)', fontsize=12)
ax7.set_ylim(0, 105)
ax7.set_xticks(pg_xs)
ax7.set_xticklabels([f'{i}\n{trades[i-1][0][5:]}' for i in pg_xs], fontsize=7.5)
ax7.legend(loc='lower right', fontsize=9, ncol=2)
ax7.grid(alpha=0.3)
plt.tight_layout()
out7 = f'reviews/2026-07-29-psum40-{int(TARGET*100):02d}pct-evolution.png'
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
