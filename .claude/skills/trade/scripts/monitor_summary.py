#!/usr/bin/env python3
# 读分段采样累积 log，输出【全貌摘要】——判行情性质（震荡/多头/空头）+ 关键位测试 + 买卖比/量能演变。
#
# 为什么需要全貌（2026-07-22 用户立）：判断"今天是震荡还是多/空趋势"必须看开盘到当前的所有数据，
# 只看最近 N 分钟看不出行情性质、也无法随时切换判断。log 累积了全貌数据（10 秒/点，一天约 1440 点），
# 但读原始点会爆上下文，故用本脚本聚合关键统计输出摘要。
#
# AI 分析分两层：①先跑本脚本看【全貌摘要】判行情性质 + 重判随时切换；②再读 log 最近 N 行看即时突破/回踩。
#
# 用法：python3 monitor_summary.py [symbol] [--mode signal|auto]   （默认 HK.00981 + signal，读当日对应模式 log）

import csv
import os
import sys
import statistics
from datetime import datetime, timedelta

# SYMBOL 取第一个非 `--` 开头的位置参数（兼容 `SYM --mode auto` / `--mode auto SYM` 两种传参顺序）。
SYMBOL = next((a for a in sys.argv[1:] if not a.startswith("-")), "HK.00981")
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.abspath(os.path.join(_SCRIPT_DIR, "..", "..", "..", ".."))

from trade_utils_tiger import parse_mode  # 模式标识：log 按 mode 分文件，signal/auto 两会话并行盯盘不互相污染
MODE = parse_mode()

# log 日期按市场对应交易日（与 monitor_segment.py 的 trading_date_str 同口径）：港股用北京日期、
# 美股用美东交易日（北京 -12h 夏令时；冬令时 EST 需 -13h）——否则美股跨北京午夜时 summary 找不到 segment 写的 log。
def _trading_date_str(symbol):
    now = datetime.now()
    if symbol.startswith("US."):
        return (now - timedelta(hours=12)).strftime("%Y%m%d")
    return now.strftime("%Y%m%d")

LOG_FILE = os.path.join(
    _PROJECT_ROOT, "tmp",
    f"monitor_log_{SYMBOL.replace('.', '_')}_{_trading_date_str(SYMBOL)}_{MODE}.csv"
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

# 数值容错：ws_segment（老虎 WebSocket 只推价格）的 log 中 ratio/vr/high/low/turnover 列为空，
# 解析时跳过空值；high/low 全空时回退用价格序列（当日高低以 last 序列近似）。
def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None

lasts = [x for x in (_f(r["last"]) for r in rows) if x is not None]
highs = [x for x in (_f(r["high"]) for r in rows) if x is not None] or lasts
lows = [x for x in (_f(r["low"]) for r in rows) if x is not None] or lasts
ratios = [x for x in (_f(r["ratio"]) for r in rows) if x is not None]
vrs = [x for x in (_f(r["vr"]) for r in rows) if x is not None]
turnovers = [x for x in (_f(r["turnover_yi"]) for r in rows) if x is not None]

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
# 切片可能为空（ws_segment 行 ratio/vr 列为空、前后半分布不均时后半可能无有效值）——
# 空切片直接 None，不能 mean([]) 报错（2026-08-10 修：ws_segment 空列容错）
half = max(1, n // 2)
r_first = statistics.mean(ratios[:half]) if len(ratios[:half]) > 0 else None
r_last = statistics.mean(ratios[half:]) if len(ratios[half:]) > 0 else None
v_first = statistics.mean(vrs[:half]) if len(vrs[:half]) > 0 else None
v_last = statistics.mean(vrs[half:]) if len(vrs[half:]) > 0 else None

# 价格 4 段均价（看全天走势方向）
quart = max(1, n // 4)
seg = [statistics.mean(s) for i in range(4) if (s := lasts[i * quart : (i + 1) * quart])]

# 额增速：最后 5 分钟 vs 全程均速
recent_n = min(30, n)
recent_turnover_rate = (turnovers[-1] - turnovers[-recent_n]) / recent_n if len(turnovers) > 1 and n > recent_n else 0

print(f"=== {SYMBOL} 全貌摘要（{first_t}-{last_t}，{n} 点）===")
print(f"开={open_p} 现={cur_p} ({(cur_p/open_p-1)*100:+.2f}%) | 当日 high={day_high} low={day_low} 振幅={amp:.1f}%")
print(f"箱体测试：顶({day_high})触及 {top_test} 次 / 底({day_low})触及 {bot_test} 次")
print(f"买卖比演变：前半 {r_first:+.0f} → 后半 {r_last:+.0f}（{'恶化↘' if r_last < r_first else '改善↗'}）" if r_first is not None else "买卖比：N/A（ws log 无此字段）")
print(f"量比演变：前半 {v_first:.1f} → 后半 {v_last:.1f}（{'缩量↘' if v_last < v_first else '放量↗'}）" if v_first is not None and v_last is not None else "量比：N/A（ws log 无此字段）")
print(f"价格4段均价：{[round(x, 2) for x in seg]}（{'递增' if seg[-1]>seg[0] else '递减' if seg[-1]<seg[0] else '走平'}）")
print(f"额：当前 {turnovers[-1]:.1f}亿 | 近{recent_n}点均速 {recent_turnover_rate*60:.2f}亿/分" if len(turnovers) > 1 else "额：N/A（ws log 无此字段）")

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
