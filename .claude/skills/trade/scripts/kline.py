#!/usr/bin/env python3
"""富途 K 线 + 斐波那契回撤位。用法: python3 kline.py CODE [days]
输出近 N 日 OHLCV + 近期高低点的回撤位(0.382/0.5/0.618/0.786)。
注意 API 是 request_history_kline(不是 get_history_kline)。"""
import sys, datetime
from futu import OpenQuoteContext, KLType

code = sys.argv[1]
days = int(sys.argv[2]) if len(sys.argv) > 2 else 8
ctx = OpenQuoteContext('127.0.0.1', 11111)
end = (datetime.datetime.now() + datetime.timedelta(days=1)).strftime("%Y-%m-%d")
start = (datetime.datetime.now() - datetime.timedelta(days=days + 5)).strftime("%Y-%m-%d")  # 范围收紧到最近,避免 max_count 截断取最早
ret, kd, _ = ctx.request_history_kline(code, start=start, end=end, ktype=KLType.K_DAY, max_count=days + 2)
ctx.close()
if ret != 0:
    print(f"K线失败: {kd}"); sys.exit(1)

print(f"=== {code} 近 {len(kd)} 日 ===")
print(kd[['time_key','open','high','low','close','volume','turnover']].to_string())

if len(kd) >= 2:
    hi = float(kd['high'].max()); lo = float(kd['low'].min()); span = hi - lo
    # 2026-08-17 修：斐波那契位区分趋势方向——原来恒输出 lo+span×r（从低点向上的回调支撑位），
    # 对下跌趋势语义颠倒（下跌段要的是从高点向下的反弹阻力位 hi−span×r）。方向判据：最新收盘
    # 与区间中点比较（> 中点 = 上涨趋势 → 回调支撑位；< 中点 = 下跌趋势 → 反弹阻力位）。
    close = float(kd['close'].iloc[-1]); mid = (hi + lo) / 2
    if close >= mid:
        direction, kind = "↑", "回调支撑位（上涨趋势，回撤买点参考）"
        print(f"\n近期 高 {hi:.2f} 低 {lo:.2f} 区间 {span:.2f} 收盘 {close:.2f} ≥ 中点 {mid:.2f} → 上涨趋势 → 斐波那契{kind}: {direction}")
        for r in (0.382, 0.5, 0.618, 0.786):
            print(f"  {r}: {lo + span * r:.2f}")
    else:
        direction, kind = "↓", "反弹阻力位（下跌趋势，反抽卖点参考）"
        print(f"\n近期 高 {hi:.2f} 低 {lo:.2f} 区间 {span:.2f} 收盘 {close:.2f} < 中点 {mid:.2f} → 下跌趋势 → 斐波那契{kind}: {direction}")
        for r in (0.382, 0.5, 0.618, 0.786):
            print(f"  {r}: {hi - span * r:.2f}")
