#!/usr/bin/env python3
"""富途密采样盯盘。用法: python3 monitor.py CODE [rounds] [interval_sec]
每轮输出: 时间/现价/bid/ask/买卖比/量比/成交额/今低/今高。
默认 6 轮 × 10 秒 (与 config monitoring.early_session 一致, 60s < harness 120s 超时)。"""
import sys, time
from futu import OpenQuoteContext

code = sys.argv[1]
rounds = int(sys.argv[2]) if len(sys.argv) > 2 else 6
interval = int(sys.argv[3]) if len(sys.argv) > 3 else 10

ctx = OpenQuoteContext('127.0.0.1', 11111)
for i in range(rounds):
    ret, df = ctx.get_market_snapshot([code])
    if ret != 0 or df is None or len(df) == 0:
        print(f"r{i} 失败 {df}")
    else:
        r = df.iloc[0]
        t = str(r['update_time'])[11:19]
        ba = r['bid_ask_ratio']; vr = r['volume_ratio']
        ba_s = f"{ba:.0f}" if ba == ba else "NA"
        vr_s = f"{vr:.2f}" if vr == vr else "NA"
        to = r['turnover'] / 1e8 if r['turnover'] == r['turnover'] else 0
        print(f"r{i} {t} last={r['last_price']} bid={r['bid_price']} ask={r['ask_price']} "
              f"买卖比={ba_s} 量比={vr_s} 额={to:.1f}亿 低={r['low_price']} 高={r['high_price']}")
    if i < rounds - 1:
        time.sleep(interval)
ctx.close()
