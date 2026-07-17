#!/usr/bin/env python3
"""资金流查询（富途）。

用法：
  python3 capital.py HK.07709            # 当前资金分布（大/中/小单 + super 超大单净流向）
  python3 capital.py HK.07709 flow       # 分钟级资金流序列（最近若干根）
  python3 capital.py HK.07709 flow 20    # 最近 20 根分钟流

富途符号格式 = 市场.代码（HK.07709 / US.SOXL）。
"""
import sys
from futu import OpenQuoteContext


def main():
    if len(sys.argv) < 2:
        print("用法: python3 capital.py <HK.07709> [dist|flow] [N]")
        sys.exit(1)
    code = sys.argv[1]
    mode = sys.argv[2] if len(sys.argv) > 2 else "dist"
    n = int(sys.argv[3]) if len(sys.argv) > 3 else 15

    ctx = OpenQuoteContext(host="127.0.0.1", port=11111)
    try:
        if mode == "dist":
            ret, df = ctx.get_capital_distribution(code)
            if ret != 0:
                print(f"ERR ret={ret}")
                return
            print(f"=== {code} 资金分布（当前时点）===")
            print(df.to_string())
        elif mode == "flow":
            ret, df = ctx.get_capital_flow(code)
            if ret != 0:
                print(f"ERR ret={ret}")
                return
            print(f"=== {code} 资金流分钟序列（最近 {n} 根）===")
            print(df.tail(n).to_string())
        else:
            print(f"未知 mode={mode}，用 dist 或 flow")
    finally:
        ctx.close()


if __name__ == "__main__":
    main()
