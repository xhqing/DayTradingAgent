#!/usr/bin/env python3
# 多标的批量分段采样（2026-07-22 改造：一个进程同时采多只 + 关键位参数化）。
#
# 为什么一个进程采多只：富途 get_market_snapshot 接受标的列表、一次调用批量返回多只快照，
# 一个进程一轮循环里采完所有标的，比每只票各开一个 OpenQuoteContext 连接 / 各起一个进程
# 更省资源、更快（连接与进程开销远大于多几次毫秒级 API 调用）。多标的并行盯盘不该靠多进程，
# 而是靠单进程批量采样——这是本脚本的设计出发点。
#
# 为什么关键位参数化：原版 UP_BREAK/DN_BREAK 写死成澜起 06809 的 330/306，换标的就失配、
# 更没法一只进程同时给多只票各设各的关键位。现在每个标的的关键位（阻力 up / 支撑 dn）
# 由 AI 盯盘启动做方向研判时定好、随命令行传入——谁的 high 创本段新高且 >= 它自己的 up 阻力位，
# 或 low 创新低且 <= 它自己的 dn 支撑位，立即打标记 + 整段提前退出通知 AI
# （任一只破位都叫 AI 来看，突破响应延迟从段时长压到约一个采样间隔）。
#
# 每只票各写各的连续 log（tmp/monitor_log_{symbol}_{date}.csv，按标的分文件、累积不丢），
# monitor_summary.py 按标的读——多标的不混在同一个 CSV 里、分析时各读各的。
#
# 用法：
#   python3 monitor_segment.py <targets> <duration_sec> [interval_sec]
#     targets      标的列表，逗号分隔；每项格式 SYM[:up[:dn]]，冒号后是关键位（阻力 up / 支撑 dn）
#                  HK.00981:330:306,US.MU:950:890     两只、各带关键位
#                  HK.00981:330:306,HK.06809          第二只不检测突破（只采样）
#                  HK.00981:330                       只检测向上破阻力
#                  HK.00981                           单只、不带关键位（只采样，向后兼容旧用法）
#     duration_sec 本段采样时长（秒），到点退出触发通知；建议 40
#     interval_sec 采样间隔（秒），默认 10
#
# log 文件：每只票 tmp/monitor_log_{SYM}_{YYYYMMDD}.csv，CSV 列：
#   time,symbol,last,bid,ask,ratio,vr,high,low,turnover_yi
# AI 分析时读各标的 log 最近 N 行（如 tail -60 tmp/monitor_log_<SYM>_*.csv）。

import csv
import os
import sys
import time
from datetime import datetime

from futu import OpenQuoteContext


def parse_targets(raw):
    """解析 'SYM[:up[:dn]][,SYM...]' 成 [{sym, up, dn}, ...]。

    up / dn 缺省为 None = 不检测该方向突破（只采样）。这样不带冒号的裸标的、
    只写 up 不写 dn 等情况都能正常解析，向后兼容旧的单标的用法。
    """
    targets = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        parts = item.split(":")
        sym = parts[0].strip()
        if not sym:
            continue
        up = float(parts[1]) if len(parts) > 1 and parts[1].strip() else None
        dn = float(parts[2]) if len(parts) > 2 and parts[2].strip() else None
        targets.append({"sym": sym, "up": up, "dn": dn})
    return targets


def main():
    if len(sys.argv) < 2:
        print(
            "用法：python3 monitor_segment.py <targets> <duration_sec> [interval_sec]\n"
            "  targets 格式 SYM[:up[:dn]][,SYM[:up[:dn]]...]\n"
            "  例：HK.00981:330:306,US.MU:950:890  （两只、各带关键位）\n"
            "      HK.00981:330:306,HK.06809        （第二只只采样不检测突破）\n"
            "      HK.00981                         （单只、向后兼容）",
            flush=True,
        )
        sys.exit(1)

    targets = parse_targets(sys.argv[1])
    if not targets:
        print(f"未解析出任何标的：{sys.argv[1]}", flush=True)
        sys.exit(1)

    DURATION = int(sys.argv[2]) if len(sys.argv) > 2 else 40
    INTERVAL = int(sys.argv[3]) if len(sys.argv) > 3 else 10
    syms = [t["sym"] for t in targets]

    # 每只票的连续 log 路径 + 突破检测状态。项目根 = 脚本目录(scripts)上四级。
    _SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
    _PROJECT_ROOT = os.path.abspath(os.path.join(_SCRIPT_DIR, "..", "..", "..", ".."))
    LOG_DIR = os.path.join(_PROJECT_ROOT, "tmp")
    date_str = datetime.now().strftime("%Y%m%d")
    LOG_FIELDS = ["time", "symbol", "last", "bid", "ask", "ratio", "vr", "high", "low", "turnover_yi"]

    # state[sym] = 该标的的 log 路径 + 上一轮 high/low（用于判创新高/新低）+ 它自己的关键位
    state = {}
    for t in targets:
        sym = t["sym"]
        log_file = os.path.join(LOG_DIR, f"monitor_log_{sym.replace('.', '_')}_{date_str}.csv")
        state[sym] = {"log_file": log_file, "last_high": None, "last_low": None, "up": t["up"], "dn": t["dn"]}

    def append_log(log_file, row):
        os.makedirs(LOG_DIR, exist_ok=True)
        write_header = not os.path.exists(log_file)
        with open(log_file, "a", newline="") as f:
            w = csv.writer(f)
            if write_header:
                w.writerow(LOG_FIELDS)
            w.writerow(row)

    # 时间断层哨兵（2026-07-28 立）：启动时读各标的 log 最后采样时间，距现在 ≥ 5 分钟
    # （正常段间循环 < 1 分钟）= 断网/暂停/故障致断层——警告 AI 先跑恢复协议再继续，别用过时
    # 数据发信号。2026-07-28 中芯事故根因：断层期未察觉、用过时参考价发信号（13:45 数据 →
    # 15:34 才响铃）。哨兵让 AI 即便没主动意识到断层，脚本也强制提醒。
    try:
        for sym in syms:
            log_file = state[sym]["log_file"]
            if not os.path.exists(log_file):
                continue
            with open(log_file) as lf:
                rows = list(csv.reader(lf))
            if len(rows) <= 1:
                continue
            last_t = rows[-1][0]  # "HH:MM:SS"
            last_dt = datetime.strptime(f"{date_str} {last_t}", "%Y%m%d %H:%M:%S")
            gap_min = (datetime.now() - last_dt).total_seconds() / 60
            if gap_min >= 5:
                print(
                    f"⚠️ 时间断层哨兵：{sym} 距上次采样 {gap_min:.0f} 分钟（上次 {last_t} → 现在），"
                    f"疑似断网/暂停/故障致断层！先跑 `python3 scripts/resume.py` 重建上下文、"
                    f"刷新现价，禁止直接用断层前旧数据发信号（发信号前必过「发信号硬前置」："
                    f"距上次 date/snapshot >2min 必须刷新）。",
                    flush=True,
                )
    except Exception as e:
        print(f"[时间断层哨兵 err:{e}]", flush=True)

    ctx = OpenQuoteContext(host="127.0.0.1", port=11111)
    start = time.time()
    print(
        f"=== 多标的分段采样 {syms} duration={DURATION}s interval={INTERVAL}s "
        f"开始 {datetime.now():%H:%M:%S} ===",
        flush=True,
    )
    for t in targets:
        sym = t["sym"]
        if t["up"] is None and t["dn"] is None:
            level_desc = "无关键位（只采样）"
        else:
            level_desc = f"阻力up={t['up']} 支撑dn={t['dn']}"
        print(f"    {sym}: {level_desc} | log={state[sym]['log_file']}", flush=True)

    broke = []  # 本段若提前退出，记录哪些标的破了什么位，供结尾汇总
    while time.time() - start < DURATION:
        try:
            ret, df = ctx.get_market_snapshot(syms)
            if ret != 0 or df is None or len(df) == 0:
                print(f"[{datetime.now():%H:%M:%S}] snapshot 失败 ret={ret} {df}", flush=True)
                time.sleep(INTERVAL)
                continue
            # 富途一次返回多行、每行一只标的。优先按 code 列匹配；无 code 列则按请求顺序兜底
            # （富途返回行序通常与请求一致，兜底仅为防御列名差异）。
            if "code" in df.columns:
                rows = {r["code"]: r for _, r in df.iterrows()}
            else:
                rows = {syms[i]: df.iloc[i] for i in range(min(len(syms), len(df)))}

            ts = datetime.now().strftime("%H:%M:%S")
            for t in targets:
                sym = t["sym"]
                row = rows.get(sym)
                if row is None:
                    print(f"[{ts}] [{sym}] 无快照（snapshot 未返回该标的）", flush=True)
                    continue
                last = row["last_price"]
                high = row["high_price"]
                low = row["low_price"]
                bid = row["bid_price"]
                ask = row["ask_price"]
                ratio = row.get("bid_ask_ratio") or 0
                vr = row.get("volume_ratio") or 0
                turnover = row.get("turnover") or 0

                # 突破检测：每只票用自己的关键位 + 自己的上一轮 high/low。
                # high 创本段新高且 >= 阻力 = 向上突破；low 创新低且 <= 支撑 = 向下突破。
                st = state[sym]
                tags = []
                if st["last_high"] is not None:
                    if st["up"] is not None and high > st["last_high"] + 1e-9 and high >= st["up"]:
                        tags.append(f"[↑破阻力{high}≥{st['up']}]")
                    if st["dn"] is not None and low < st["last_low"] - 1e-9 and low <= st["dn"]:
                        tags.append(f"[↓破支撑{low}≤{st['dn']}]")
                st["last_high"], st["last_low"] = high, low

                # ① append 该标的自己的连续 log（累积，AI 分析时读最近 N 行）
                append_log(
                    state[sym]["log_file"],
                    [ts, sym, last, bid, ask, f"{ratio:.0f}", f"{vr:.1f}", high, low, f"{turnover / 1e8:.2f}"],
                )
                # ② stdout 仍 print（段通知 + 突破标记），每只票一行带 [sym] 前缀
                print(
                    f"[{ts}] [{sym}] last={last} bid={bid} ask={ask} "
                    f"买卖比={ratio:.0f} 量比={vr:.1f} high={high} low={low} "
                    f"额={turnover / 1e8:.1f}亿 {' '.join(tags)}",
                    flush=True,
                )
                if tags:
                    broke.append((sym, " ".join(tags)))
            # 任一只破关键位即整段提前结束、触发通知（突破响应从≤duration压到~interval）
            if broke:
                print(f"!!! 有标的破关键位，提前结束本段以即时通知 AI：{broke} !!!", flush=True)
                break
        except Exception as e:
            print(f"[{datetime.now():%H:%M:%S}] err:{e}", flush=True)
        time.sleep(INTERVAL)

    # 每 10 分钟重估方向提醒（2026-07-28 立，原 60 分钟、同日用户要求加密）：每段结束时
    # 读 log 首条时间算累计盯盘时长，每满 10 分钟输出一次重估提醒（标记文件去重、避免跨段重复）。
    # 为什么：盘中方向/趋势/行情变化快，1 小时重估太慢、错过转向；10 分钟强制重估一次。
    # 盯盘容易固守开盘方向、忘记 skill 的「动态修正方向」规定（2026-07-27 MU 午盘守偏空 4 小时
    # 没重估、错过 ~9700 HKD）。靠工具强制提醒，不靠记忆。
    try:
        marker_file = os.path.join(LOG_DIR, f"reassess_marker_{date_str}.txt")
        reminded = set()
        if os.path.exists(marker_file):
            with open(marker_file) as mf:
                reminded = {int(x) for x in mf.read().split() if x.strip().isdigit()}
        first_log = state[syms[0]]["log_file"]
        elapsed_min = None
        if os.path.exists(first_log):
            with open(first_log) as lf:
                rdr = csv.reader(lf)
                next(rdr, None)  # 跳表头
                first_row = next(rdr, None)
                if first_row:
                    first_dt = datetime.strptime(f"{date_str} {first_row[0]}", "%Y%m%d %H:%M:%S")
                    elapsed_min = (datetime.now() - first_dt).total_seconds() / 60
        if elapsed_min is not None and elapsed_min >= 10:
            ten_mark = int(elapsed_min // 10) * 10  # 对齐到 10 倍数（10/20/30…）
            if ten_mark not in reminded:
                reminded.add(ten_mark)
                with open(marker_file, "w") as mf:
                    mf.write(" ".join(str(h) for h in sorted(reminded)))
                print(
                    f"⏰ 重估方向提醒（已盯盘 {int(elapsed_min)} 分钟、满 {ten_mark} 分钟）："
                    f"过动态修正方向 5 触发（①破阻力 ②破支撑 ③站上/跌破VWAP ④箱体假突破≥2次 "
                    f"⑤持续单向运动≥1h）+ 自问「方向/趋势/行情是否仍与开盘一致」，不固守开盘判断。",
                    flush=True,
                )
    except Exception as e:
        print(f"[重估提醒检查 err:{e}]", flush=True)

    print(
        f"=== 分段结束 {datetime.now():%H:%M:%S}"
        f"（AI 读各标的 log 最近 N 行分析：{' / '.join(state[s]['log_file'] for s in syms)}）===",
        flush=True,
    )
    ctx.close()


if __name__ == "__main__":
    main()
