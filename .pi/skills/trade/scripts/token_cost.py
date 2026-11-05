#!/usr/bin/env python3
"""停盯 token 费用统计（DeepSeek 官方 deepseek-flash 口径）。

用途：统计一段会话（jsonl）内的 LLM token 消耗，按 DeepSeek 官方定价换算为人民币
费用，供停盯总结把 token 费用计入「总净赔率」，并把结果追加进
`reviews/token-cost-log.csv`（含价格快照，官方价格低频变化时按记录快照追溯）。

## 定价口径（2026-10-16 查官方页；官方价格会低频变化，以官方页为准）

来源：https://api-docs.deepseek.com/zh-cn/quick_start/pricing
模型名：`deepseek-flash`（DeepSeek-V4.1-Flash；旧名 deepseek-v4-flash 已于官方下线、自动路由）

| 项目（元 / 百万 tokens） | 空闲时段 | 高峰时段 |
|---|---|---|
| 输入（缓存命中） | 0.02 | 0.04 |
| 输入（缓存未命中） | 1 | 2 |
| 输出 | 4 | 8 |

高峰时段 = 北京时间 周一至周五 9:00-12:00、14:00-18:00（不含中国法定节假日）；
其余时段（含周末与法定节假日全天）按空闲价（高峰价的一半）。
⚠️ 本脚本按「周一至周五」近似判定高峰/空闲，不识别中国法定节假日——节假日期间会把
高峰错算为高峰价（偏保守、高估），如需精确可人工核对当日是否法定节假日并修正。

## 计费映射（pi 会话记录 usage 口径）

pi 的 usage 四字段相加 = totalTokens（已实测 868/868 条全量一致）：
- usage.input       = 缓存未命中输入（计输入未命中价）
- usage.cacheRead   = 缓存命中输入（计缓存命中价）
- usage.output      = 输出（计输出价）
- usage.cacheWrite  = 缓存写入（deepseek 免费、按 0 计）

每条消息用其记录时刻（北京时间）判定高峰/空闲档，分档累计、分档计价——港股盯盘
跨高峰/空闲（北京时间白天），美股盯盘基本全在空闲档（北京时间深夜）。

## 用法

  python3 token_cost.py <session.jsonl> [--before ISO] [--after ISO]
                        [--fx CNY_PER_USD] [--note "..."] [--write-csv [PATH]]

  --before/--after  时间过滤（UTC ISO，如 "2026-10-15T20:18:34.665Z" 或 "2026-10-15T20:18:34"）；
                    用于把会话切出「盯盘段」（例如截止到停盯总结发出的时刻）。
  --fx              CNY→USD 汇率（人民币）：用于把费用折算成美元、与美股盈亏同口径；
                    不传则只输出人民币费用。汇率由调用者提供（记录时点汇率）。
  --note            记录备注（写入 CSV 的 note 列）。
  --write-csv [PATH] 把结果追加到 CSV（默认 reviews/token-cost-log.csv，相对项目根）。
"""
import argparse
import csv
import json
import os
import sys
from datetime import datetime, timedelta, timezone

# ---------------------------------------------------------------- 官方价格快照
PRICES = {
    "model": "deepseek-flash",
    "asof": "2026-10-16",
    "source": "https://api-docs.deepseek.com/zh-cn/quick_start/pricing",
    # 元 / 百万 tokens
    "input_miss": {"offpeak": 1.0, "peak": 2.0},
    "input_hit": {"offpeak": 0.02, "peak": 0.04},
    "output": {"offpeak": 4.0, "peak": 8.0},
}

CSV_HEADER = [
    "date", "session", "span", "model", "price_asof",
    "tok_miss_off", "tok_miss_peak", "tok_hit_off", "tok_hit_peak", "tok_out_off", "tok_out_peak",
    "p_miss_off", "p_miss_peak", "p_hit_off", "p_hit_peak", "p_out_off", "p_out_peak",
    "cost_cny", "fx_cny_usd", "cost_usd",
    "trades_pnl_usd", "total_max_loss_usd", "odds_excl_cost", "odds_incl_cost", "note",
]


def to_beijing(dt_utc):
    return dt_utc + timedelta(hours=8)


def is_peak(dt_bj):
    """高峰时段：周一至周五 9:00-12:00、14:00-18:00（不识别中国法定节假日，见模块 docstring）。"""
    if dt_bj.weekday() >= 5:  # 周六周日
        return False
    h = dt_bj.hour + dt_bj.minute / 60.0
    return (9.0 <= h < 12.0) or (14.0 <= h < 18.0)


def parse_iso(s):
    s = s.strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def main():
    ap = argparse.ArgumentParser(description="停盯 token 费用统计（deepseek-flash 官方口径）")
    ap.add_argument("session", help="会话 jsonl 路径")
    ap.add_argument("--before", help="只统计该时刻之前（UTC ISO）")
    ap.add_argument("--after", help="只统计该时刻之后（UTC ISO）")
    ap.add_argument("--fx", type=float, help="CNY per USD 汇率（用于折算美元）")
    ap.add_argument("--note", default="", help="备注（写入 CSV）")
    ap.add_argument("--write-csv", nargs="?", const="reviews/token-cost-log.csv",
                    help="追加到 CSV（默认 reviews/token-cost-log.csv）")
    args = ap.parse_args()

    before = parse_iso(args.before) if args.before else None
    after = parse_iso(args.after) if args.after else None

    tok = {("miss", "off"): 0, ("miss", "peak"): 0,
           ("hit", "off"): 0, ("hit", "peak"): 0,
           ("out", "off"): 0, ("out", "peak"): 0}
    n_msgs = 0
    first_ts = last_ts = None

    with open(args.session) as f:
        for line in f:
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get("type") != "message":
                continue
            m = d.get("message") or {}
            if m.get("role") != "assistant" or "usage" not in m:
                continue
            ts = d.get("timestamp")
            if not ts:
                continue
            dt = parse_iso(ts)
            if before and dt >= before:
                continue
            if after and dt <= after:
                continue
            u = m["usage"]
            band = "peak" if is_peak(to_beijing(dt)) else "off"
            tok[("miss", band)] += u.get("input", 0)
            tok[("hit", band)] += u.get("cacheRead", 0)
            tok[("out", band)] += u.get("output", 0)
            n_msgs += 1
            first_ts = dt if first_ts is None else min(first_ts, dt)
            last_ts = dt if last_ts is None else max(last_ts, dt)

    P = PRICES
    cost_cny = (
        tok[("miss", "off")] * P["input_miss"]["offpeak"] / 1e6
        + tok[("miss", "peak")] * P["input_miss"]["peak"] / 1e6
        + tok[("hit", "off")] * P["input_hit"]["offpeak"] / 1e6
        + tok[("hit", "peak")] * P["input_hit"]["peak"] / 1e6
        + tok[("out", "off")] * P["output"]["offpeak"] / 1e6
        + tok[("out", "peak")] * P["output"]["peak"] / 1e6
    )
    cost_usd = (cost_cny / args.fx) if args.fx else None

    span = ""
    if first_ts and last_ts:
        span = f"{to_beijing(first_ts).strftime('%Y-%m-%d %H:%M')}~{to_beijing(last_ts).strftime('%Y-%m-%d %H:%M')} 北京"

    result = {
        "session": os.path.basename(args.session),
        "span": span,
        "n_messages": n_msgs,
        "tokens": {
            "miss_off": tok[("miss", "off")], "miss_peak": tok[("miss", "peak")],
            "hit_off": tok[("hit", "off")], "hit_peak": tok[("hit", "peak")],
            "out_off": tok[("out", "off")], "out_peak": tok[("out", "peak")],
        },
        "prices_cny_per_million": {
            "input_miss": P["input_miss"], "input_hit": P["input_hit"], "output": P["output"],
            "asof": P["asof"], "source": P["source"],
        },
        "cost_cny": round(cost_cny, 4),
        "fx_cny_usd": args.fx,
        "cost_usd": round(cost_usd, 4) if cost_usd is not None else None,
    }
    print(json.dumps(result, ensure_ascii=False, indent=1))

    if args.write_csv:
        path = args.write_csv
        if not os.path.isabs(path):
            # 相对项目根（从脚本位置回溯：scripts → trade → skills → .claude → 项目根）
            root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
            path = os.path.join(root, path)
        new_file = not os.path.exists(path)
        with open(path, "a", newline="") as f:
            w = csv.writer(f)
            if new_file:
                w.writerow(CSV_HEADER)
            # date 列取盯盘段起始的北京日期：美股盯盘跨北京午夜时与美东交易日同日、
            # 港股盯盘与当日一致，便于按交易日对齐复盘
            day = to_beijing(first_ts).strftime("%Y-%m-%d") if first_ts else ""
            w.writerow([
                day, result["session"], span, P["model"], P["asof"],
                tok[("miss", "off")], tok[("miss", "peak")], tok[("hit", "off")], tok[("hit", "peak")],
                tok[("out", "off")], tok[("out", "peak")],
                P["input_miss"]["offpeak"], P["input_miss"]["peak"],
                P["input_hit"]["offpeak"], P["input_hit"]["peak"],
                P["output"]["offpeak"], P["output"]["peak"],
                f"{cost_cny:.4f}", args.fx if args.fx else "", f"{cost_usd:.4f}" if cost_usd is not None else "",
                "", "", "", "",  # trades_pnl / max_loss / odds：由 AI 在停盯总结时回填
                args.note,
            ])
        print(f"\n已追加 → {path}", file=sys.stderr)


if __name__ == "__main__":
    main()
