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

    print(
        f"=== 分段结束 {datetime.now():%H:%M:%S}"
        f"（AI 读各标的 log 最近 N 行分析：{' / '.join(state[s]['log_file'] for s in syms)}）===",
        flush=True,
    )
    ctx.close()


if __name__ == "__main__":
    main()
