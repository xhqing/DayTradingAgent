#!/usr/bin/env python3
"""被拦决策点反事实回填（2026-08-30 立，T120——开仓净赔率门槛重标定的数据地基）。

为什么做（痛点，2026-08-24 立 T120 时表述、现行不变）：盯盘期间每个「形态成立但净赔率
不足放弃」的决策点，此前在 analysis_beat log 只留一行赔率估值、无后续价格路径——被拦单
到底是好是坏永远无法验证，门槛重标定（T119 之外的长线任务）只能靠拍脑袋。门槛之争
（用户主张低门槛、AI 主张高门槛）双方都拿不出被拦单的真实数据——没有反事实，门槛永远
是拍脑袋对拍脑袋。

做什么：当日停盯后（或次日盘前）用富途分钟 K 回拉该标的决策点后 60 分钟的高低价，
回填进结构化文件 tmp/blocked_decisions.csv（增量跑、同键去重）。攒 ≥30 个被拦点后
按「假想落地 R 分布」重标定门槛终值——若 1.2 口径下被拦带假想 EV 为正则进一步放开，
若为负则回升（重标定动作本身在复盘时做，本脚本只负责攒数据）。

口径与「被拦原因」：2026-08-28 税闸停用后，被拦原因只剩**净赔率门槛**一项（质量闸拒绝）
——本脚本回填的就是这类；与影子交易（shadow_trade.py，互斥闸拦截「别人在场」）互补
不重叠：T120 回填「被门槛拦」，影子仓记录「被互斥闸拦」。

假想落地 R 的算法（毛口径，与开仓时净赔率的分母口径不同、复盘时须注明）：
  R = (到后 60 分钟内最优方向的极值 − 决策价) / |决策价 − 止损价|
  做多：最优 = 区间 high（理想止盈离场）；做空：最优 = 区间 low。
  假设：决策价成交、最优极值止盈离场、止损未触发（乐观口径——若区间也触及止损价，
  记 stop_hit=1，复盘时按止损先触发的 -1R 场景单独看）。R 是「该决策点若放行、走满
  60 分钟的理论上限」，不是可实现 EV——重标定时与真实样本的落地 R 分布对照使用。

用法（增量跑，每次停盯后 / 次日盘前把当日决策点补进去）：
  python3 blocked_backfill.py add HK.00700 long 2026-08-28 10:15 410.0 402.0 425.0 [net_odds]
      参数：symbol direction date(HK/交易日) time(HH:MM) 决策价 止损价 止盈价 [净赔率估值]
      add = 登记一条被拦决策点 + 立即尝试回填（决策点须已过去 ≥60 分钟才有 K 线可拉；
      不到 60 分钟先登记、filled=0，之后跑 backfill 补）
  python3 blocked_backfill.py backfill
      补拉所有 filled=0 且已过 ≥60 分钟的决策点（富途分钟 K）
  python3 blocked_backfill.py stats
      汇总：总条数 / 已回填数 / 假想 R 分布（均值 / 正占比 / 止损触及率）——重标定素材

数据文件：tmp/blocked_decisions.csv（本机运行时数据，tmp/ 已 gitignore）。
列：date,time,symbol,direction,price,stop,target,net_odds,high60,low60,close60,
    stop_hit,hypothetical_r,filled,added_at
"""

import csv
import os
import sys
from datetime import datetime, timedelta

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, "..", "..", "..", ".."))
TMP_DIR = os.path.join(PROJECT_ROOT, "tmp")
CSV_PATH = os.path.join(TMP_DIR, "blocked_decisions.csv")
WINDOW_MIN = 60   # 决策点后回看的分钟数

FIELDS = ["date", "time", "symbol", "direction", "price", "stop", "target", "net_odds",
          "high60", "low60", "close60", "stop_hit", "hypothetical_r", "filled", "added_at"]


def _load_rows():
    if not os.path.exists(CSV_PATH):
        return []
    with open(CSV_PATH) as f:
        return list(csv.DictReader(f))


def _save_rows(rows):
    os.makedirs(TMP_DIR, exist_ok=True)
    with open(CSV_PATH, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)


def _fetch_kline_60m(symbol, t_start_dt):
    """富途 1 分钟 K：拉 [t_start, t_start+60min]（各外扩 1 分钟），返回 DataFrame 或 None。

    与 review.py fetch_hl 同款调用（request_history_kline + KLType.K_1M）。
    symbol 决定时区语义：富途 K 线 time_key 按市场本地时区（港股 HKT、美股 ET）——
    调用方传入的 date/time 就是该市场本地时间，直接拼串查询即可。
    """
    from futu import OpenQuoteContext, KLType
    fmt = "%Y-%m-%d %H:%M:%S"
    s = (t_start_dt - timedelta(minutes=1)).strftime(fmt)
    e = (t_start_dt + timedelta(minutes=WINDOW_MIN + 1)).strftime(fmt)
    ctx = OpenQuoteContext("127.0.0.1", 11111)
    try:
        ret, kd, _ = ctx.request_history_kline(symbol, start=s, end=e,
                                               ktype=KLType.K_1M, max_count=10000)
    finally:
        ctx.close()
    if ret != 0 or kd is None or len(kd) == 0:
        return None
    return kd


def _backfill_row(row):
    """尝试回填单行（决策点已过 ≥60 分钟才拉得到完整窗口）。返回 (ok, msg)。"""
    try:
        t0 = datetime.strptime(f"{row['date']} {row['time']}", "%Y-%m-%d %H:%M")
    except ValueError:
        return False, f"日期/时间格式坏：{row['date']} {row['time']}"
    if datetime.now() < t0 + timedelta(minutes=WINDOW_MIN):
        return False, "决策点后不足 60 分钟，暂无完整 K 线窗口"
    kd = _fetch_kline_60m(row["symbol"], t0)
    if kd is None or len(kd) == 0:
        return False, "富途分钟 K 拉取失败（OpenD 在线？symbol 前缀？）"
    hi = float(kd["high"].max())
    lo = float(kd["low"].min())
    close = float(kd["close"].iloc[-1])
    price, stop = float(row["price"]), float(row["stop"])
    stop_dist = abs(price - stop)
    if stop_dist <= 0:
        return False, "止损距 ≤0，坏数据行"
    direction = row["direction"]
    # 止损触及判定（区间内触及过止损价 = 真实场景会先止损离场）
    stop_hit = int((direction == "long" and lo <= stop) or
                   (direction == "short" and hi >= stop))
    # 假想 R（乐观口径：最优极值止盈离场；见模块 docstring 口径说明）
    best = hi if direction == "long" else lo
    hyp_r = ((best - price) if direction == "long" else (price - best)) / stop_dist
    row.update({
        "high60": round(hi, 3), "low60": round(lo, 3), "close60": round(close, 3),
        "stop_hit": stop_hit, "hypothetical_r": round(hyp_r, 3), "filled": 1,
    })
    return True, f"已回填：high={hi:.3f} low={lo:.3f} 假想R={hyp_r:+.3f}" + ("（止损已触及）" if stop_hit else "")


def cmd_add(args):
    if len(args) < 7:
        print("用法：add <symbol> <long|short> <YYYY-MM-DD> <HH:MM> <price> <stop> <target> [net_odds]")
        sys.exit(1)
    symbol, direction, date, tstr = args[0], args[1], args[2], args[3]
    price, stop, target = float(args[4]), float(args[5]), float(args[6])
    net_odds = args[7] if len(args) > 7 else ""
    if direction not in ("long", "short"):
        print(f"direction 必须是 long/short，收到 {direction}")
        sys.exit(1)
    rows = _load_rows()
    # 去重：同 (date,time,symbol) 已登记即拒绝（同决策点不重复登记）
    for r in rows:
        if r["date"] == date and r["time"] == tstr and r["symbol"] == symbol:
            print(f"❌ 已登记过同键决策点（{date} {tstr} {symbol}），不重复登记")
            sys.exit(1)
    row = {k: "" for k in FIELDS}
    row.update({"date": date, "time": tstr, "symbol": symbol, "direction": direction,
                "price": price, "stop": stop, "target": target, "net_odds": net_odds,
                "filled": 0, "added_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
    ok, msg = _backfill_row(row)
    rows.append(row)
    _save_rows(rows)
    print(f"✅ 已登记被拦决策点 {date} {tstr} {symbol} {direction} @ {price}"
          f"（净赔率估值 {net_odds or '未填'}）——{msg}")


def cmd_backfill(_args):
    rows = _load_rows()
    pending = [r for r in rows if r.get("filled") != "1"]
    if not pending:
        print("无待回填条目（全部 filled=1）")
        return
    print(f"待回填 {len(pending)} 条：")
    n_ok = 0
    for r in pending:
        ok, msg = _backfill_row(r)
        print(f"  {r['date']} {r['time']} {r['symbol']}: {msg}")
        n_ok += int(ok)
    _save_rows(rows)
    print(f"本轮回填成功 {n_ok}/{len(pending)}（不足 60 分钟或拉取失败的下次再跑）")


def cmd_stats(_args):
    rows = _load_rows()
    filled = [r for r in rows if r.get("filled") == "1"]
    print(f"被拦决策点总数 {len(rows)}，已回填 {len(filled)}，待回填 {len(rows) - len(filled)}")
    if len(filled) < 5:
        print("样本 <5，先攒数据（重标定门槛须 ≥30 个被拦点，T120）")
        return
    rs = [float(r["hypothetical_r"]) for r in filled]
    stops = [int(r["stop_hit"]) for r in filled]
    pos = sum(1 for x in rs if x > 0)
    print(f"假想落地 R（乐观口径，60 分钟最优极值离场）：均值 {sum(rs)/len(rs):+.3f}，"
          f"正占比 {pos}/{len(rs)} = {pos/len(rs)*100:.0f}%，"
          f"止损触及率 {sum(stops)}/{len(rs)} = {sum(stops)/len(rs)*100:.0f}%")
    print("注：乐观口径 R 是理论上限；止损触及的行真实场景大概率 -1R 离场，"
          "重标定时按「止损触及行记 -1、其余记假想 R」再算一版保守 EV 对照。")


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ("add", "backfill", "stats"):
        print(__doc__)
        sys.exit(1)
    {"add": cmd_add, "backfill": cmd_backfill, "stats": cmd_stats}[sys.argv[1]](sys.argv[2:])


if __name__ == "__main__":
    main()
