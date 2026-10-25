#!/usr/bin/env python3
"""三阶段赔率折线图（初始预期 → 修正预期 → 实际落地），按时间先后逐笔。

2026-10-24 用户立：「增加一幅图，10月份的，把每笔交易的三阶段赔率按时间先后画出折线图」。

数据来源分工（口径必须一致）：
  - **初始预期 R / 修正预期 R**：从 `actions/*-ET-actions.md` 的开仓记录解析——它们是开仓
    当时由脚本实算并落盘的值（含降档后真实费），无法从 trades.csv 反推。
    优先读「三阶段赔率回填」行（初始 X → 修正 Y → 实际落地 Z），退回读「初始预期赔率」+
    「修正预期赔率」两个独立字段。
  - **实际落地 R**：一律用 trades.csv + `fee_schedule` 按 review.py 同口径实算
    （净盈亏 ÷ 净 max_loss），**不抄 actions 里手写的落地值**——保证与复盘统计同源可比。
  - **时间排序**：取 actions 里的动作时间戳（`> ⏰ 动作时间：YYYY-MM-DD HH:MM:SS`），
    同一秒缺失时退回日期 + 文件内出现顺序（csv 的 date+symbol 排序不等于真实开仓顺序）。

用法：
  python3 r_phases_chart.py --csv reviews/2026-10-24-trades-us.csv --since 2026-10-01 \
      --out reviews/2026-10-24-us-from-2026-10-01-rphases.png
"""
import argparse
import csv as csvmod
import glob
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fee_schedule as FS   # noqa: E402

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                            "..", "..", "..", ".."))
MARKET_SUFFIX = {"US": "-ET-actions.md", "HK": "-HKT-actions.md"}


def _num(pat, text):
    m = re.search(pat, text)
    if not m:
        return None
    return float(m.group(1).replace("−", "-").replace("+", ""))


def parse_actions(market, since=None, until=None):
    """解析开仓记录 → [{dt, date, symbol, entry, initial_r, fixed_r}]，按时间升序。"""
    out = []
    pattern = os.path.join(PROJECT_ROOT, "actions", f"*{MARKET_SUFFIX[market]}")
    for path in sorted(glob.glob(pattern)):
        date = os.path.basename(path)[:10]
        if since and date < since:
            continue
        if until and date > until:
            continue
        txt = open(path, encoding="utf-8").read()
        # 逐个「开仓 · 市场.代码」标题切段；段内取成交价 / 赔率 / 时间戳
        heads = list(re.finditer(r"^[^\n]*?开仓\s*·\s*([A-Z]{1,6})\.([A-Z0-9]{1,6})[^\n]*$", txt, re.M))
        for idx, h in enumerate(heads):
            body = txt[h.end(): heads[idx + 1].start() if idx + 1 < len(heads) else len(txt)]
            entry = _num(r"\|\s*\**\s*成交价\s*\**\s*\|\s*\**\s*\\{0,2}\$?([\d.]+)", body)
            # 三阶段回填行：初始 X → 修正 Y → 实际落地 Z
            tri = re.search(r"三阶段赔率回填[^\n]*", body)
            init = fixed = None
            if tri:
                t = tri.group(0)
                init = _num(r"初始\s*\**\s*([-+]?[\d.]+)", t)
                fixed = _num(r"修正\s*\**\s*([-+]?[\d.]+)", t)
            if init is None:
                init = _num(r"\|\s*\**\s*初始预期赔率\s*\**\s*\|\s*\**\s*R?\s*([-+]?[\d.]+)", body)
            if fixed is None:
                fixed = _num(r"\|\s*\**\s*修正预期赔率\s*\**\s*\|\s*\**\s*R?\s*([-+]?[\d.]+)", body)
            tm = re.search(r"动作时间[：:]\s*(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})", body)
            ts = tm.group(1) if tm else f"{date} 00:00:{idx:02d}"
            out.append({"dt": ts, "date": date, "symbol": f"{market}.{h.group(2)}",
                        "entry": entry, "initial_r": init, "fixed_r": fixed})
    out.sort(key=lambda x: x["dt"])
    return out


def load_csv(path, since=None, until=None):
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        for r in csvmod.DictReader(line for line in f if not line.startswith("#")):
            if not r.get("symbol"):
                continue
            d = r["date"]
            if since and d < since:
                continue
            if until and d > until:
                continue
            rows.append(r)
    return rows


def actual_r(row):
    """实际落地 R（净口径，与 review.py 完全一致）。"""
    e, x, q = float(row["entry_price"]), float(row["exit_price"]), int(float(row["shares"]))
    sign = 1 if str(row["direction"]).lower() in ("long", "做多", "多") else -1
    mkt = row["symbol"].split(".")[0]
    sec = (row.get("type") or "stock").strip().lower()
    ml = float(row["max_loss"])
    so, sc = ("buy", "sell") if sign > 0 else ("sell", "buy")
    fo = FS.fee_per_side(mkt, sec, e * q, shares=q, side=so)
    fc = FS.fee_per_side(mkt, sec, x * q, shares=q, side=sc)
    stop = e - (ml / q) * sign
    fstop = FS.fee_per_side(mkt, sec, abs(stop * q), shares=q, side=sc)
    P = (x - e) * q * sign - fo - fc
    M = ml + fo + fstop
    return P / M if M else 0.0


def match(opens, rows):
    """开仓记录 ↔ csv 行 按 (日期, 成交价±0.02) 一对一匹配，返回带 actual_r 的序列。"""
    from datetime import date as _d

    def _day_diff(a, b):
        try:
            return abs((_d.fromisoformat(a) - _d.fromisoformat(b)).days)
        except Exception:
            return 99

    used = set()
    seq, miss = [], []
    for o in opens:
        cand, best = None, None
        for i, r in enumerate(rows):
            if i in used or r["symbol"] != o["symbol"] or o["entry"] is None:
                continue
            # 日期：ET 交易日与 actions 文件日期可能差 1 天（会话跨北京午夜，记录写进次日文件）
            if _day_diff(r["date"], o["date"]) > 1:
                continue
            px = float(r["entry_price"])
            # 价格：actions 记录值 vs 券商成交均价可能有滑点/记录偏差，取相对容差 0.6%
            tol = max(0.02, abs(o["entry"]) * 0.006)
            diff = abs(px - o["entry"])
            if diff > tol:
                continue
            # 多候选时取价格最接近的（同日同标的多笔交替开平的场景）
            if best is None or diff < best:
                cand, best = i, diff
        if cand is None:
            miss.append(o)
            continue
        if cand is None:
            miss.append(o)
            continue
        used.add(cand)
        row = rows[cand]
        seq.append({**o, "actual_r": actual_r(row),
                    "exit": float(row["exit_price"]), "qty": int(float(row["shares"]))})
    rest = [r for i, r in enumerate(rows) if i not in used]
    return seq, miss, rest


def draw(seq, out_path, title_suffix=""):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    try:
        import matplotlib.font_manager as fm
        avail = {f.name for f in fm.fontManager.ttflist}
        for cand in ("Arial Unicode MS", "PingFang HK", "Heiti TC", "STHeiti",
                     "Hiragino Sans GB", "Songti SC"):
            if cand in avail:
                matplotlib.rcParams["font.sans-serif"] = [cand] + list(
                    matplotlib.rcParams["font.sans-serif"])
                matplotlib.rcParams["axes.unicode_minus"] = False
                break
    except Exception:
        pass

    xs = list(range(1, len(seq) + 1))
    init = [p["initial_r"] if p["initial_r"] is not None else float("nan") for p in seq]
    fixd = [p["fixed_r"] if p["fixed_r"] is not None else float("nan") for p in seq]
    act = [p["actual_r"] for p in seq]

    fig, ax = plt.subplots(figsize=(15, 6.8))
    ax.plot(xs, init, "-o", color="#7F8C8D", lw=1.8, ms=5, alpha=0.9, label="Initial expected R (at reference price)")
    ax.plot(xs, fixd, "-s", color="#E67E22", lw=1.8, ms=4.5, alpha=0.95, label="Revised expected R (at fill price)")
    ax.plot(xs, act, "-^", color="#2980B9", lw=2.2, ms=6, label="Actual realized R")
    # 零线 + 开仓门槛 1.2
    ax.axhline(0, color="gray", ls=":", alpha=0.7)
    ax.axhline(1.2, color="#27AE60", ls="--", alpha=0.65, lw=1.2,
               label="Entry threshold (net odds = 1.2)")
    ax.set_xlabel("Trade # (chronological)", fontsize=11)
    ax.set_ylabel("R multiple", fontsize=11)
    ax.set_title(f"Three-stage Odds per Trade {title_suffix}  N={len(seq)}", fontsize=13)
    ax.set_xticks(xs)
    ax.set_xticklabels([p["date"][5:] for p in seq], fontsize=7, rotation=90)
    ax.legend(fontsize=9, loc="upper right")
    ax.grid(alpha=0.3)

    # 终局均值标注
    def mean(v):
        vals = [x for x in v if x == x]  # 去 NaN
        return sum(vals) / len(vals) if vals else float("nan")
    txt = (f"mean initial = {mean(init):+.2f}R   "
           f"mean revised = {mean(fixd):+.2f}R   "
           f"mean actual = {mean(act):+.2f}R")
    ax.text(0.01, 0.02, txt, transform=ax.transAxes, fontsize=9.5,
            bbox=dict(boxstyle="round,pad=0.3", fc="#FEF9E7", ec="#B7950B", alpha=0.9))

    fig.tight_layout()
    fig.savefig(out_path, dpi=120)
    print(f"✅ 图已存 {out_path}")
    print(f"   {txt}")
    return {"n": len(seq), "init": mean(init), "fixed": mean(fixd), "actual": mean(act)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True, help="trades CSV 路径")
    ap.add_argument("--market", default="US", choices=["US", "HK"])
    ap.add_argument("--since", default=None)
    ap.add_argument("--until", default=None)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    opens = parse_actions(a.market, a.since, a.until)
    rows = load_csv(a.csv, a.since, a.until)
    seq, miss, rest = match(opens, rows)
    print(f"开仓记录 {len(opens)} 条｜csv 样本 {len(rows)} 笔｜配对成功 {len(seq)}｜"
          f"actions 未配对 {len(miss)}｜csv 未配对 {len(rest)}")
    for o in miss:
        print(f"  ⚠️ actions 未配对：{o['dt']} {o['symbol']} entry={o['entry']}")
    for r in rest:
        print(f"  ⚠️ csv 未配对：{r['date']} {r['symbol']} entry={r['entry_price']}")
    if not seq:
        raise SystemExit("❌ 无可画数据")
    no_init = sum(1 for p in seq if p["initial_r"] is None)
    no_fix = sum(1 for p in seq if p["fixed_r"] is None)
    print(f"字段缺漏：初始预期 R 缺 {no_init} 笔、修正预期 R 缺 {no_fix} 笔（缺处以 NaN 断线）")
    draw(seq, a.out, title_suffix=(f"({' ≥'.join(x for x in (a.since, a.until) if x)}".rstrip("≥") + ")") if (a.since or a.until) else "")


if __name__ == "__main__":
    main()
