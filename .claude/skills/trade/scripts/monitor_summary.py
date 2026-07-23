#!/usr/bin/env python3
# 读分段采样累积 log，输出【全貌摘要】——判行情性质（震荡/多头/空头）+ 关键位测试 + 买卖比/量能演变。
#
# 为什么需要全貌（2026-07-22 用户立）：判断"今天是震荡还是多/空趋势"必须看开盘到当前的所有数据，
# 只看最近 N 分钟看不出行情性质、也无法随时切换判断。log 累积了全貌数据（10 秒/点，一天约 1440 点），
# 但读原始点会爆上下文，故用本脚本聚合关键统计输出摘要。
#
# AI 分析分两层：①先跑本脚本看【全貌摘要】判行情性质 + 重判随时切换；②再读 log 最近 N 行看即时突破/回踩。
#
# 用法：python3 monitor_summary.py [symbol]   （默认 HK.00981，读当日 log）

import csv
import os
import sys
import statistics
from datetime import datetime

SYMBOL = sys.argv[1] if len(sys.argv) > 1 else "HK.00981"
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.abspath(os.path.join(_SCRIPT_DIR, "..", "..", "..", ".."))
LOG_FILE = os.path.join(
    _PROJECT_ROOT, "tmp", f"monitor_log_{SYMBOL.replace('.', '_')}_{datetime.now().strftime('%Y%m%d')}.csv"
)

if not os.path.exists(LOG_FILE):
    print(f"无 log：{LOG_FILE}（盯盘尚未累积数据）")
    sys.exit(0)

with open(LOG_FILE) as f:
    rows = [r for r in csv.DictReader(f)]
n = len(rows)
if n == 0:
    print("log 空")
    sys.exit(0)

lasts = [float(r["last"]) for r in rows]
highs = [float(r["high"]) for r in rows]
lows = [float(r["low"]) for r in rows]
ratios = [float(r["ratio"]) for r in rows]
vrs = [float(r["vr"]) for r in rows]
turnovers = [float(r["turnover_yi"]) for r in rows]

day_high = max(highs)
day_low = min(lows)
open_p = lasts[0]
cur_p = lasts[-1]
amp = (day_high - day_low) / day_low * 100 if day_low else 0
first_t, last_t = rows[0]["time"], rows[-1]["time"]

# 箱顶/箱底测试次数：last 接近当日 high/low（距 0.4% 内）的点数 ≈ 触及顶/底的采样次数
top_test = sum(1 for p in lasts if p >= day_high * 0.996)
bot_test = sum(1 for p in lasts if p <= day_low * 1.004)

# 买卖比 / 量比：前半 vs 后半（看演变方向）
half = max(1, n // 2)
r_first, r_last = statistics.mean(ratios[:half]), statistics.mean(ratios[half:])
v_first, v_last = statistics.mean(vrs[:half]), statistics.mean(vrs[half:])

# 价格 4 段均价（看全天走势方向）
quart = max(1, n // 4)
seg = [statistics.mean(s) for i in range(4) if (s := lasts[i * quart : (i + 1) * quart])]

# 额增速：最后 5 分钟 vs 全程均速
recent_n = min(30, n)
recent_turnover_rate = (turnovers[-1] - turnovers[-recent_n]) / recent_n if n > recent_n else 0

print(f"=== {SYMBOL} 全貌摘要（{first_t}-{last_t}，{n} 点）===")
print(f"开={open_p} 现={cur_p} ({(cur_p/open_p-1)*100:+.2f}%) | 当日 high={day_high} low={day_low} 振幅={amp:.1f}%")
print(f"箱体测试：顶({day_high})触及 {top_test} 次 / 底({day_low})触及 {bot_test} 次")
print(f"买卖比演变：前半 {r_first:+.0f} → 后半 {r_last:+.0f}（{'恶化↘' if r_last < r_first else '改善↗'}）")
print(f"量比演变：前半 {v_first:.1f} → 后半 {v_last:.1f}（{'缩量↘' if v_last < v_first else '放量↗'}）")
print(f"价格4段均价：{[round(x, 2) for x in seg]}（{'递增' if seg[-1]>seg[0] else '递减' if seg[-1]<seg[0] else '走平'}）")
print(f"额：当前 {turnovers[-1]:.1f}亿 | 近{recent_n}点均速 {recent_turnover_rate*60:.2f}亿/分")

# VWAP（富途 avg_price）——日内多空分界 + 趋势日判断，看全貌必看（2026-07-22 用户立）
try:
    from futu import OpenQuoteContext
    _q = OpenQuoteContext('127.0.0.1', 11111)
    _ret, _df = _q.get_market_snapshot([SYMBOL])
    _q.close()
    if _ret == 0 and len(_df) > 0 and 'avg_price' in _df.columns:
        _vwap = float(_df['avg_price'].iloc[0])
        _diff = cur_p - _vwap
        _pos = "上方" if _diff > 0 else ("下方" if _diff < 0 else "贴合")
        _who = "多头占优" if _diff > 0 else ("空头占优" if _diff < 0 else "多空均衡")
        print(f"VWAP={_vwap:.2f} | 现价 {_pos} VWAP {_diff:+.2f}（{_who}；VWAP 是日内多空分界 + 趋势日判断关键，必看）")
    else:
        print(f"VWAP 获取失败：ret={_ret}（富途 OpenD 未登录或无该标的）")
except Exception as _e:
    print(f"VWAP 获取异常：{_e}")

# 行情性质判别（启发式，供 AI 参考、非定论）
price_drift = abs(seg[-1] - seg[0])
range_width = day_high - day_low
is_range = top_test >= 4 and bot_test >= 4 and price_drift < range_width * 0.4
if is_range:
    print(f"→ 偏【震荡】（顶底各测试≥4次 + 价格未单边走出箱体 {price_drift:.2f} < {range_width*0.4:.2f}）→ 切区间交易")
elif seg[-1] - seg[0] > range_width * 0.05:
    print(f"→ 偏【多头】（价格递增）→ 顺势做多")
elif seg[0] - seg[-1] > range_width * 0.05:
    print(f"→ 偏【空头】（价格递减）→ 顺势做空")
else:
    print(f"→ 偏【中性走平】（价格未单边）→ 观望/区间思路")
