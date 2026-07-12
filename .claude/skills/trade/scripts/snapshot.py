#!/usr/bin/env python3
"""富途标的快照。用法: python3 snapshot.py CODE1,CODE2,...
输出关键列: 现价/开/高/低/昨收/lot/成交额/振幅/买卖比/量比/bid/ask。"""
import sys
from futu import OpenQuoteContext
import pandas as pd

codes = sys.argv[1].split(",")
ctx = OpenQuoteContext('127.0.0.1', 11111)
ret, df = ctx.get_market_snapshot(codes)
ctx.close()
if ret != 0:
    print(f"快照失败 ret={ret}: {df}"); sys.exit(1)

cols = ['code','name','last_price','open_price','high_price','low_price','prev_close_price',
        'lot_size','turnover','turnover_rate','amplitude','bid_price','ask_price',
        'bid_ask_ratio','volume_ratio','update_time']
have = [c for c in cols if c in df.columns]
pd.set_option('display.width', 220)
pd.set_option('display.max_columns', 20)
print(df[have].to_string())
