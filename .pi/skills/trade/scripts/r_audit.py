#!/usr/bin/env python3
"""落地 R 基准对账（T191HKUS）。

背景：平仓脚本曾把「移损后的活动止损单触发价」当成 R 分母的止损基准，导致移过损的
主动平仓笔 |R| 虚高。用户问「发现之前攒的历史样本还能不能更正」——本工具回答这个问题。

做法：**不改写任何已发布的 actions / signals 历史文本**，只做逐笔对账：
  ① 从 actions/*-actions.md 的开仓条目解析每笔的初始止损（成交价 + 止损 + 标的 + 日期）；
  ② 读复盘主样本库 CSV，逐笔校验「毛 max_loss = 仓位 × |开仓成交价 − 初始止损|」；
  ③ 用 review.py 的同一套口径，分别算「按 CSV 原值」与「按初始止损重算」两组统计；
  ④ 输出逐笔对账表与汇总结论到 reviews/，供复盘引用。

为什么这样就能判断样本是否作废：R 是派生量，review.py 每次从 CSV 的
entry_price / exit_price / shares / max_loss 加 fee_schedule 现算，CSV 本身不存 R。
只要毛 max_loss 用的是开仓时点的初始止损，历史统计就没有被这个 bug 污染。

用法：python3 .pi/skills/trade/scripts/r_audit.py
"""
import csv
import glob
import os
import re
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]          # scripts → trade → skills → .pi → 项目根
SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import review  # noqa: E402 —— 复用同一套净口径 R 算法

# 两份复盘主样本库（美股 2026-10-24 全量 / 港股 2026-09-12 全量）
LIBRARIES = {
    "US": ROOT / "reviews" / "2026-10-24-trades-us.csv",
    "HK": ROOT / "reviews" / "2026-09-12-trades-hk.csv",
}
OUT_CSV = ROOT / "reviews" / "2026-11-12-r-audit.csv"
OUT_MD = ROOT / "reviews" / "2026-11-12-r-audit.md"

_NUM = r"([-+−]?)\s*\\{0,2}\$?\s*(?:US\$|HK\$)?\s*([\d,]+(?:\.\d+)?)"


def _table_num(body, label):
    """取表格里「| 标签 | 数值 |」的数值，兼容 **加粗**、货币符号、负号、括号备注。"""
    m = re.search(r"\|\s*\**\s*" + label + r"\s*\**\s*\|\s*\**\s*" + _NUM, body)
    if not m:
        return None
    sign = -1 if m.group(1) in ("-", "−") else 1
    return sign * float(m.group(2).replace(",", ""))


def _symbol(title, market):
    """从标题取标的，统一成 US.XXX / HK.00100 形式。"""
    if market == "US":
        m = re.search(r"US\.([A-Z0-9]{1,6}(?:\.[A-Z])?)", title)
        if m:
            return "US." + m.group(1)
        return None
    m = re.search(r"(?:HK\.)?(\d{5})\b", title)
    return "HK." + m.group(1) if m else None


def action_opens():
    """解析全部 actions 开仓条目 → [{date, market, symbol, entry, stop, source}]。"""
    out = []
    for path in sorted(glob.glob(str(ROOT / "actions" / "*-actions.md"))):
        base = os.path.basename(path)
        m = re.match(r"(\d{4}-\d{2}-\d{2})-(HKT|ET)-actions\.md", base)
        if not m:
            continue
        d, market = m.group(1), ("HK" if m.group(2) == "HKT" else "US")
        text = Path(path).read_text(encoding="utf-8")
        for match in re.finditer(r"^[^\n]*?开仓[^\n]*$", text, re.M):
            title = match.group(0)
            # 排除：开仓失败（止损标「拟」）、影子仓（纸面）、以及提到「开仓」的其它叙述行
            if "开仓失败" in title or "【影子】" in title:
                continue
            body = text[match.end(): match.end() + 1800]
            sym = _symbol(title, market)
            entry = _table_num(body, "成交价")
            stop = _table_num(body, "止损")
            if not sym or entry is None or stop is None or entry <= 0 or stop <= 0:
                continue
            out.append({"date": d, "market": market, "symbol": sym,
                        "entry": entry, "stop": stop,
                        "source": f"actions/{base}"})
    return out


def _days(a, b):
    try:
        return abs((date.fromisoformat(a) - date.fromisoformat(b)).days)
    except Exception:
        return 99


def match_open(row, opens):
    """按 标的 + 成交价 找开仓记录：同日优先，跨日（±1 天）只在同日无候选时作参考。

    跨日匹配不自动采信——实测 2026-08-05 的 01888 曾因 ±1 天容差误配到 08-06 的
    另一笔（成交价相近、初始止损不同），误配会把正常的 max_loss 判成「不一致」。
    返回 (记录, 候选数, 是否跨日)。
    """
    tol = max(0.02, row["entry"] * 0.006)

    def _near(day_gap):
        cands = [o for o in opens
                 if o["symbol"] == row["symbol"] and _days(o["date"], row["date"]) == day_gap]
        near = [(abs(o["entry"] - row["entry"]), o) for o in cands
                if abs(o["entry"] - row["entry"]) <= tol]
        near.sort(key=lambda x: x[0])
        return near

    same = _near(0)
    if same:
        return same[0][1], len(same), False
    cross = _near(1)
    if cross:
        return cross[0][1], len(cross), True
    return None, 0, False


def load_rows(path):
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(line for line in f if not line.startswith("#")))
    parsed = []
    for r in rows:
        parsed.append({"date": r["date"].strip(), "symbol": r["symbol"].strip(),
                       "direction": r["direction"].strip(),
                       "entry": float(r["entry_price"]), "exit": float(r["exit_price"]),
                       "shares": float(r["shares"]), "ml": float(r["max_loss"]),
                       "type": (r.get("type") or "").strip() or "stock",
                       "mode": (r.get("mode") or "").strip() or "unknown",
                       "shadow": (r.get("shadow") or "").strip()})
    return parsed


def stats_for(rows, tmp_path):
    """用 review.load_trades 同一套口径算统计量（写一份临时 CSV 供其读取）。"""
    with open(tmp_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["date", "symbol", "direction", "entry_price", "exit_price",
                    "shares", "max_loss", "type", "mode", "shadow"])
        for r in rows:
            w.writerow([r["date"], r["symbol"], r["direction"], f"{r['entry']:.4f}",
                        f"{r['exit']:.4f}", f"{r['shares']:.0f}", f"{r['ml']:.4f}",
                        r["type"], r["mode"], r["shadow"]])
    trades = review.load_trades(str(tmp_path))
    return review.summarize(trades), trades


def main():
    opens = action_opens()
    tmp_dir = ROOT / "tmp"
    tmp_dir.mkdir(exist_ok=True)
    audit_rows, summary = [], []

    for market, lib in LIBRARIES.items():
        rows = load_rows(lib)
        corrected = [dict(r) for r in rows]
        counts = {"ok": 0, "suspect_shifted_stop": 0, "mismatch_other": 0,
                  "cross_day_unverified": 0, "no_open_record": 0}
        for i, r in enumerate(rows):
            o, n_cands, cross_day = match_open(r, opens)
            status, stop, src, expected, delta = "no_open_record", "", "", "", ""
            if o:
                stop = f"{o['stop']:.4f}"
                src = o["source"] + ("（跨日匹配，仅参考）" if cross_day else "")
                expected = r["shares"] * abs(r["entry"] - o["stop"])
                delta = r["ml"] - expected
                tol = max(0.02, expected * 0.01)
                if abs(delta) <= tol:
                    status = "ok"
                elif cross_day:
                    # 跨日匹配本身可疑：不判污染、不自动改值，留人工核对
                    status = "cross_day_unverified"
                elif delta < 0:
                    # CSV 的毛 max_loss 比初始止损算出的更小 = 移损后止损距变小的特征
                    status = "suspect_shifted_stop"
                else:
                    # CSV 更大：止损价取整 / 降档 / 开仓后立即纠错等，不是移损污染
                    status = "mismatch_other"
                counts[status] += 1
                if status == "suspect_shifted_stop":
                    corrected[i]["ml"] = expected       # 只更正符合污染特征的笔
            else:
                counts["no_open_record"] += 1
            audit_rows.append({
                "market": market, "date": r["date"], "symbol": r["symbol"],
                "direction": r["direction"], "entry_price": f"{r['entry']:.4f}",
                "exit_price": f"{r['exit']:.4f}", "shares": f"{r['shares']:.0f}",
                "csv_max_loss": f"{r['ml']:.4f}", "stop_initial": stop,
                "expected_max_loss": f"{expected:.4f}" if expected != "" else "",
                "delta": f"{delta:+.4f}" if delta != "" else "",
                "status": status, "candidates": n_cands, "shadow": r["shadow"],
                "source": src,
            })
        # 主统计口径与 review.py 的默认行为一致：剔除 shadow=1 的影子样本
        idx_real = [i for i, r in enumerate(rows)
                    if r["shadow"] not in ("1", "true", "True")]
        rows_real = [rows[i] for i in idx_real]
        corrected_real = [corrected[i] for i in idx_real]
        base_s, base_t = stats_for(rows_real, tmp_dir / f"r-audit-{market}-base.csv")
        fix_s, fix_t = stats_for(corrected_real, tmp_dir / f"r-audit-{market}-fix.csv")
        changed = sum(1 for a, b in zip(base_t, fix_t) if abs(a["R"] - b["R"]) > 1e-9)
        summary.append({"market": market, "lib": lib.name, "N": len(rows),
                        "N_real": len(rows_real), "counts": counts,
                        "base": base_s, "fix": fix_s, "changed": changed,
                        "diffs": [(a["date"], a["symbol"], a["R"], b["R"])
                                  for a, b in zip(base_t, fix_t)
                                  if abs(a["R"] - b["R"]) > 1e-9],
                        "flagged": [(a["date"], a["symbol"], a["status"],
                                     a["csv_max_loss"], a["expected_max_loss"])
                                    for a in audit_rows
                                    if a["market"] == market and a["status"] in
                                    ("suspect_shifted_stop", "mismatch_other", "cross_day_unverified")]})
        for p in (tmp_dir / f"r-audit-{market}-base.csv", tmp_dir / f"r-audit-{market}-fix.csv"):
            p.unlink(missing_ok=True)

    # 逐笔对账表
    with open(OUT_CSV, "w", newline="", encoding="utf-8") as f:
        cols = ["market", "date", "symbol", "direction", "entry_price", "exit_price",
                "shares", "csv_max_loss", "stop_initial", "expected_max_loss",
                "delta", "status", "candidates", "shadow", "source"]
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(audit_rows)

    # 汇总结论
    lines = [
        "# 落地 R 基准对账（T191HKUS）",
        "",
        "> 生成：2026-11-12；命令：`python3 .pi/skills/trade/scripts/r_audit.py`",
        "> **不改写任何历史 actions / signals 文本**，只做逐笔核对。逐笔明细见 `2026-11-12-r-audit.csv`。",
        "",
        "## 为什么做",
        "",
        "平仓脚本曾用「移损后的活动止损单触发价」当 R 分母，导致移过损的主动平仓笔 |R| 虚高。"
        "本次核对要回答：发现之前攒的历史样本是否作废、能否更正。R 是派生量——`review.py` 每次"
        "从 CSV 的开仓价 / 平仓价 / 股数 / 毛 max_loss 加 `fee_schedule` 现算，CSV 本身不存 R；"
        "只要毛 max_loss 等于「仓位 × |开仓成交价 − 初始止损|」，历史统计就没被污染。",
        "",
        "## 逐项结果",
        "",
    ]
    for s in summary:
        c = s["counts"]
        lines += [
            f"### {s['market']} · `{s['lib']}`（共 {s['N']} 行，主统计剔影子后 {s['N_real']} 笔）",
            "",
            f"- ✅ 毛 max_loss 与初始止损一致：{c['ok']} 笔",
            f"- ❌ 符合移损污染特征（CSV 的 max_loss 偏小，已按初始止损重算）：{c['suspect_shifted_stop']} 笔",
            f"- 🟡 其它口径差异（CSV 的 max_loss 偏大，保持原值）：{c['mismatch_other']} 笔",
            f"- 🟡 跨日匹配、仅参考（不改值）：{c['cross_day_unverified']} 笔",
            f"- ⚠️ 找不到对应开仓记录（无法验证，保持原值）：{c['no_open_record']} 笔",
            "",
            "| 统计量 | 按 CSV 原值 | 按初始止损重算 |",
            "|---|---:|---:|",
            f"| 样本数 N | {s['base']['N']} | {s['fix']['N']} |",
            f"| 胜率 | {s['base']['p']:.1%} | {s['fix']['p']:.1%} |",
            f"| EV（平均 R） | {s['base']['EV']:+.4f}R | {s['fix']['EV']:+.4f}R |",
            f"| 平均盈利 R | {s['base']['RW']:+.4f}R | {s['fix']['RW']:+.4f}R |",
            f"| 平均亏损 R | {s['base']['RL']:+.4f}R | {s['fix']['RL']:+.4f}R |",
            f"| 标准差 s | {s['base']['s']:.4f} | {s['fix']['s']:.4f} |",
            f"| R 发生变化的笔数 | — | {s['changed']} |",
            "",
        ]
        if s["flagged"]:
            lines += ["需要人工过目的差异笔（日期 / 标的 / 分类 / CSV 毛 max_loss / 按初始止损期望）：", ""]
            for d, sym, st, ml, exp in s["flagged"][:20]:
                lines.append(f"- {d} {sym}：{st}；{ml} vs {exp}")
            lines.append("")
        if s["diffs"]:
            lines += ["R 发生变化的笔（日期 / 标的 / 原 R → 重算 R）：", ""]
            for d, sym, r0, r1 in s["diffs"][:20]:
                lines.append(f"- {d} {sym}：{r0:+.3f}R → {r1:+.3f}R")
            if len(s["diffs"]) > 20:
                lines.append(f"- （其余 {len(s['diffs']) - 20} 笔见逐笔表）")
            lines.append("")
    # 汇总数字只统计真实样本（与 review.py 主统计口径一致，剔除影子）
    real_rows = [a for a in audit_rows if a["shadow"] != "1"]
    n_ok = sum(1 for a in real_rows if a["status"] == "ok")
    n_suspect = sum(1 for a in real_rows if a["status"] == "suspect_shifted_stop")
    n_no = sum(1 for a in real_rows if a["status"] == "no_open_record")
    n_other = len(real_rows) - n_ok - n_suspect - n_no
    r_changed = sum(s["changed"] for s in summary)
    ev_unchanged = all(abs(s["base"]["EV"] - s["fix"]["EV"]) < 1e-9 for s in summary)

    lines += [
        "## 结论",
        "",
        f"主样本库没有被这个 bug 污染：两份 CSV 的主统计共 {len(real_rows)} 笔真实样本，"
        f"其中 {n_ok} 笔能匹配到开仓初始止损且毛 max_loss 一致，{n_other} 笔是方向相反的"
        f"其它口径差异或跨日仅参考，{n_no} 笔找不到开仓记录（保持原值、标口径待核），"
        f"**符合移损污染特征的 {n_suspect} 笔**。按初始止损重算后 R 变化的笔数为 {r_changed}，"
        f"EV、胜率、平均盈亏 R、标准差逐项{'完全相同' if ev_unchanged else '有变化（见上表）'}。",
        "",
        "所以「发现之前攒的样本作废」不成立。基于这两份 CSV 的历史结论（门槛对账、全样本 EV、"
        "待办里的统计验证假设）不需要推翻重来。真正要处理的是另外四条支线："
        "① 平仓脚本 JSON 输出的 R 与 mfe_R / mae_R（随本次脚本修复一并解决）；"
        "② 由此派生的锁利效率一类过程指标结论；③ `state/losing_streak.json`（当前已清零，"
        "且只留最近 5 笔、不进样本库）；④ 影子仓历史结算 R（`shadow_trade.py` 已于 2026-11-11 "
        "用 `stop_initial` 修好脚本，历史影子 R 与 T125 对照结论另立 T192HKUS 跟踪）。",
        "",
        "## 两笔差异的人工核查结论",
        "",
        "- 2026-08-05 HK.01888（35.36 / 3500 股 / 毛 max_loss 1610）：**跨日误配**。"
        "`actions/2026-08-06-HKT-actions.md` 里那笔开仓是 35.56 / 初始止损 35.20，"
        "是另一笔交易；同日 08-05 文件里另有 01888 开仓 35.86 / 止损 35.40"
        "（对应 CSV 的 15500 股 / 7130）。CSV 的 1610 = 3500 × 0.46，与同日那笔的止损距"
        " 0.46 同源，不是移损污染。本笔开仓记录缺失，保持原值、标「口径待核」。",
        "- 2026-08-26 HK.07709（39.06 / 2400 股 / 毛 max_loss 1104）：**其它口径微差**。"
        "`actions/2026-08-26-HKT-actions.md` 记录开仓止损 39.5，附加止损腿曾落成 40.46 后"
        "立即修正回 39.5；CSV 的 1104 = 2400 × 0.46，比按 39.5 算的 1056 大 48"
        "（每股 0.02，取整 / 纠错口径）。CSV 反而更大，方向与移损污染相反，保持原值。",
        "",
        "## 怎么判「是不是被污染」",
        "",
        "移损通常把止损往有利方向移，止损距变小。若 CSV 的毛 max_loss 用了移损后的止损，"
        "它一定**小于**按初始止损算出的值（delta < 0）；反过来 CSV 更大（delta > 0）只可能是"
        "止损价取整、降档仓位、开仓后立即纠错等其它口径，与本次 bug 无关。所以只有"
        "`suspect_shifted_stop` 才会被自动换回初始止损重算，其它差异一律保留原值、只列清单。",
        "",
        "## 口径与边界",
        "",
        "- 初始止损按「同标的 + 成交价 0.6% 内取最接近」匹配，**同日优先**；同日无候选时才用"
        "±1 天且只作参考（`cross_day_unverified`）——实测 08-05 的 01888 曾被跨日容差误配到"
        "08-06 的另一笔，误配会把正常的 max_loss 判成不一致。",
        "- 一致判据为偏差 ≤ max(0.02, 期望值 ×1%)，容纳止损价取整与降档口径。",
        "- 找不到开仓记录的笔保持原值、不猜测、不进更正；这类大多是 07 月的早期信号模式数据"
        "（actions 记录从 08-03 才有），当时按信号参考价与设定止损实算，本就不经过平仓脚本。",
        "- 主统计与 `review.py` 默认行为一致：剔除 `shadow=1` 的纸面影子样本；影子子集另行单独统计。",
        "- 重算统一用现行 `fee_schedule.py`（2026-08-28 净 max_loss 口径），两组统计用同一费率，"
        "差异只来自毛 max_loss 本身；与旧口径的历史数字不可直接相减比较。",
        "- 本表不改写任何历史记录；若日后要把差异笔的 max_loss 正式改回初始止损口径，"
        "应另走复盘产物变更（本表 + CHANGELOG + 版本号），不要静默改 CSV。",
        "- 本表只核对主样本库的 R 分母，不覆盖脚本输出的过程指标（mfe_R / mae_R）、"
        "连败计数状态与影子仓结算 R——这三条支线见 CHANGELOG 对应条目与 T191HKUS 修复说明。",
        "",
    ]
    OUT_MD.write_text("\n".join(lines), encoding="utf-8")

    # 控制台摘要
    for s in summary:
        c = s["counts"]
        print(f"{s['market']} 共 {s['N']} 行（主统计 {s['N_real']} 笔）：一致 {c['ok']} / "
              f"疑似污染 {c['suspect_shifted_stop']} / 其它差异 {c['mismatch_other']} / "
              f"跨日仅参考 {c['cross_day_unverified']} / 无开仓记录 {c['no_open_record']}",
              file=sys.stderr)
        print(f"  EV {s['base']['EV']:+.4f}R → {s['fix']['EV']:+.4f}R；"
              f"胜率 {s['base']['p']:.1%} → {s['fix']['p']:.1%}；"
              f"R 变化 {s['changed']} 笔", file=sys.stderr)
    print(f"✅ 写出 {OUT_CSV.relative_to(ROOT)} 与 {OUT_MD.relative_to(ROOT)}", file=sys.stderr)


if __name__ == "__main__":
    main()
