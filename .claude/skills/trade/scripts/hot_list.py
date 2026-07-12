#!/usr/bin/env python3
"""富途热度榜找标的 (找标的第一步铁律)。用法: python3 hot_list.py [HK|US] [count]
叠 snapshot 查流动性,输出 code/名称/热度/现价/成交额/振幅/换手。
已处理 get_hot_list 嵌套返回 (ret,(total,df)) 的坑。"""
import sys
from futu import OpenQuoteContext, Market

market_str = (sys.argv[1] if len(sys.argv) > 1 else "HK").upper()
count = int(sys.argv[2]) if len(sys.argv) > 2 else 15
market = Market.HK if market_str == "HK" else Market.US

ctx = OpenQuoteContext('127.0.0.1', 11111)

def extract_df(p):
    """get_hot_list 返回嵌套 (ret,(total,df)),递归找出 DataFrame。"""
    if hasattr(p, 'columns'):
        return p
    if isinstance(p, tuple):
        for e in p:
            d = extract_df(e)
            if d is not None:
                return d
    return None

ret, payload = ctx.get_hot_list(market=market, count=count)
if ret != 0:
    print(f"热度榜失败 ret={ret}: {payload}"); ctx.close(); sys.exit(1)
df = extract_df(payload)
if df is None or len(df) == 0:
    print("热度榜为空"); ctx.close(); sys.exit(1)

# 叠 snapshot 查成交额/振幅/换手 (筛流动性,排除盘口薄的小盘题材)
sec_col = 'security' if 'security' in df.columns else df.columns[0]
codes = df[sec_col].tolist()[:count]
ret2, snap = ctx.get_market_snapshot(codes)
ctx.close()
snap_map = {r['code']: r for _, r in snap.iterrows()} if (ret2 == 0 and snap is not None) else {}

print(f"{'代码':12} {'名称':14} {'热度':>9} {'现价':>8} {'额(亿)':>7} {'振幅%':>6} {'换手%':>6}")
for _, r in df.iterrows():
    sec = r[sec_col]
    name = str(r.get('name', ''))[:12]
    heat = r.get('average_heat', r.get('trade_heat', 0))
    s = snap_map.get(sec, {})
    def fmt(v, f=".2f"):
        return f"{v:{f}}" if v is not None and v == v else "-"
    print(f"{sec:12} {name:14} {heat:>9.0f} {fmt(s.get('last_price')):>8} "
          f"{fmt(s.get('turnover',0) and s.get('turnover')/1e8):>7} {fmt(s.get('amplitude')):>6} {fmt(s.get('turnover_rate')):>6}")
