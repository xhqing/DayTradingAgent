#!/usr/bin/env python3
"""停盯收尾一键脚本（2026-10-20 立，把「停盯总结」从靠 AI 记着改成跑一条命令）。

## 为什么要有这个脚本（2026-10-19 事故）

美股 2026-10-19 交易日（ET）做了 6 笔，AI 在美东 14:29 因上下文耗尽停止盯盘：
① 停盯总结没写进 actions/、② 飞书没发（用户直到次日才发现 Linda 没收到）、
③ 北京日期对照没写（用户查 App 只看到 1 笔，以为账目少了 —— 因为 5 笔在
北京 10-19 22:04~23:20、第 6 笔在北京 10-20 01:20，App 按北京日期切成两天）。
三件事都有同一根因：**靠 AI 记得做，一旦收尾匆忙就漏**。本脚本把它们合成
一条命令，谁跑都不会漏。

## 做什么（一次生成四样东西）

1. **券商对账**：直接查券商 API 该 ET 交易日的全部订单，按父单（开仓腿）配对
   子单（止损腿 / 止盈腿），得到每笔：方向 / 开平价 / 平仓价 / 量 / 净盈亏
   （券商 realized_pnl，权威口径）/ 修正 max_loss（净口径，fee_schedule 实算）。
   —— 这是「账面事实」，不是 AI 记忆。
2. **北京日期对照表**：每笔按 ET→北京（zoneinfo 换算，自动处理夏令时）列出
   开平时间与「App 归日」，直接解释「为什么 App 里只看到一笔」。
3. **停盯总结**：自动追加进 `actions/<date>-<market>-actions.md`（已存在当日
   总结则拒绝追加，防重复）。
4. **飞书业务版**：生成 `tmp/feishu_stop_<et_date>-<market>.md` 并用
   notify_feishu.py 发送（业务内容，无技术细节），成功后写收尾标记
   `tmp/stop_done_<market>_<et_date>.txt`（供 monitor_guard / preflight 检查
   「当天停了盯却没发收尾」）。

## 用法

  python3 scripts/stop_summary.py --market us            # 最近有成交的 ET 交易日
  python3 scripts/stop_summary.py --market us --date 2026-10-19
  python3 scripts/stop_summary.py --market us --dry-run   # 只打印，不写文件不发飞书
  python3 scripts/stop_summary.py --market hk --fx 7.10   # token 费用需要汇率

options:
  --market      us（默认）/ hk
  --date        ET 交易日 YYYY-MM-DD；省略 = 最近 7 天内最近一个有成交的交易日
  --fx          CNY/USD 汇率（可选；给了就顺手跑 token_cost.py 记 token 费用）
  --no-feishu   跳过飞书推送（仅调试用；正常停盯必须发）
  --dry-run     只打印，不落盘、不发飞书、不写标记
  --account     us 专用：paper（默认）/ live（实盘需已解锁）

## 与 skill 的关系

SKILL.md「停盯总结」节已改为：**停盯第一个动作就跑本脚本**，AI 只负责补
「要点 / 教训」文字段落，不再手写明细表与统计（手写必错、必漏）。
"""

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

ET = ZoneInfo("America/New_York")
BJ = ZoneInfo("Asia/Shanghai")


def project_root():
    """项目根 = .claude/skills/trade/scripts 上溯四层（scripts→trade→skills→.claude→根）。"""
    return os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", ".."))


def current_sid8():
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID") or os.environ.get("PI_SESSION_ID") or ""
    return sid[:8]


def owned_entries(actions_path, sid8):
    """从 actions 里取出【本会话】写的动作条目 → [(symbol, entry_price, market_prefix)]。

    依据是 log_action.sh 2026-10-20 起为每条动作追加的「> 🆔 会话：<sid8>」行。
    多会话并行时用它判定「哪些交易属于本会话」（停盯总结只总结本会话自己，T127）。
    """
    out = []
    if not sid8 or not os.path.exists(actions_path):
        return out
    try:
        txt = open(actions_path, encoding="utf-8").read()
    except Exception:
        return out
    for m in re.finditer(r"🆔\s*会话：\s*([0-9a-zA-Z]+)", txt):
        if m.group(1) != sid8:
            continue
        window = txt[m.start(): m.start() + 1600]
        m_sym = re.search(r"\|\s*标的\s*\|\s*([A-Z]{1,6})\.([A-Z0-9]{1,8})", window)
        m_entry = re.search(r"\|\s*成交价\s*\|[^|]*?\$?([\d.]+)", window)
        if m_sym and m_entry:
            try:
                out.append((m_sym.group(2), float(m_entry.group(1)), m_sym.group(1).lower()))
            except ValueError:
                continue
    return out


def detect_market_for_session():
    """给 keepawake_off.sh / monitor_unregister.sh 用：按本会话最近的动作记录判断盯的是哪个市场。

    返回 'us' / 'hk' / ''（空 = 判断不出，调用方不要传 --market，保持旧的保守行为）。
    优先看今天与昨天的 actions 文件（跨北京午夜），取本会话最后一条动作的市场前缀。
    """
    sid8 = current_sid8()
    if not sid8:
        return ""
    root = project_root()
    candidates = []
    for back in range(0, 2):
        d = (datetime.now(BJ) - timedelta(days=back)).strftime("%Y-%m-%d")
        for suffix in ("ET", "HKT"):
            fp = os.path.join(root, "actions", f"{d}-{suffix}-actions.md")
            if os.path.exists(fp):
                candidates.append(fp)
    found = []
    for fp in candidates:
        for _, _, mk in owned_entries(fp, sid8):
            found.append(mk)
    if not found:
        return ""
    last = found[-1]
    return "us" if last == "us" else ("hk" if last == "hk" else "")


def fetch_orders(market, account_mode, start_dt, end_dt):
    """拉该时间窗内全部订单。返回 (orders, masked_account)。"""
    if market == "us":
        import trade_utils_tiger_us as U
        cfg = U.load_config(account=None if account_mode == "paper" else account_mode)
        acc = cfg.account
        client = U.new_trade_client()
    else:
        import trade_utils_tiger as U
        cfg = U.load_config(account=None if account_mode == "paper" else account_mode)
        acc = cfg.account
        client = U.new_trade_client()
    s = int(start_dt.timestamp() * 1000)
    e = int(end_dt.timestamp() * 1000)
    orders = client.get_orders(account=acc, start_time=s, end_time=e)
    masked = acc[:2] + "****" + acc[-2:]
    return orders, masked


def status_str(o):
    return str(getattr(o, "status", "")).split(".")[-1].upper()


def build_trades(orders):
    """按 parent_id 配对：父单 = 开仓腿（parent_id is None 且有子单），子单 = 止损/止盈腿。"""
    by_parent = {}
    roots = []
    for o in orders:
        pid = getattr(o, "parent_id", None)
        if pid:
            by_parent.setdefault(str(pid), []).append(o)
        else:
            roots.append(o)
    trades = []
    for r in roots:
        sym = getattr(getattr(r, "contract", None), "symbol", "?")
        kids = by_parent.get(str(r.id), [])
        exit_leg = next((k for k in kids if status_str(k) == "FILLED"), None)
        stop_leg = next((k for k in kids if str(getattr(k, "order_type", "")).upper() == "STP"), None)
        tgt_leg = next((k for k in kids if str(getattr(k, "order_type", "")).upper() == "LMT"), None)
        trades.append({
            "symbol": sym,
            "direction": "long" if str(r.action).upper() == "BUY" else "short",
            "qty": int(r.filled or r.quantity or 0),
            "entry": float(r.avg_fill_price or 0),
            "entry_time": getattr(r, "trade_time", None) or getattr(r, "order_time", None),
            "exit": float(exit_leg.avg_fill_price) if exit_leg and exit_leg.avg_fill_price else None,
            "exit_time": (getattr(exit_leg, "trade_time", None) or getattr(exit_leg, "order_time", None)) if exit_leg else None,
            "pnl": float(getattr(exit_leg, "realized_pnl", 0) or 0) if exit_leg else 0.0,
            "stop_leg": float(getattr(stop_leg, "aux_price", 0) or 0) if stop_leg else None,
            "target": float(getattr(tgt_leg, "limit_price", 0) or 0) if tgt_leg else None,
            "entry_filled": status_str(r) == "FILLED",
        })
    trades.sort(key=lambda x: x["entry_time"] or 0)
    return trades


def original_stops(actions_path):
    """从 actions 的开仓记录解析每笔的【原始止损价】（移损前的）。

    为什么必须从 actions 取：止损腿的 aux_price 会被 move_stop 改成最新值（本日 6 笔里
    有 3 笔移过损），拿它算 max_loss 会把分母压小、赔率虚高（2026-10-19 实测：第 6 笔
    被算成 +4.30R，真实 +1.88R）。修正 max_loss 的口径是「开仓时点算死、移损不改基准」。

    解析方式：逐条找表格里的「| 标的 |」，要求其【前置 400 字符内含「开仓」】（排除移损 /
    平仓表），再在其【后置 1200 字符窗口】里取「成交价」「止损」两行。不按 ═══ 切块——
    标题行与表格之间正好隔着一条 ═══，按它切会把两者分开（2026-10-20 实测踩过）。
    返回 [(symbol, entry_price, stop_price)]。
    """
    out = []
    if not os.path.exists(actions_path):
        return out
    try:
        txt = open(actions_path, encoding="utf-8").read()
    except Exception:
        return out
    for m in re.finditer(r"\|\s*标的\s*\|([^|\n]*)\|", txt):
        head = txt[max(0, m.start() - 400):m.start()]
        if "开仓" not in head or "移损" in head.split("开仓")[-1]:
            continue
        window = txt[m.start(): m.end() + 1200]
        m_entry = re.search(r"\|\s*成交价\s*\|[^|]*?\$?([\d.]+)", window)
        m_stop = re.search(r"\|\s*止损\s*\|[^|]*?\$?([\d.]+)", window)
        m_sym = re.search(r"[A-Z]{1,6}\.([A-Z]{1,6})", m.group(1))
        sym = m_sym.group(1) if m_sym else None
        if sym and m_entry and m_stop:
            try:
                out.append((sym, float(m_entry.group(1)), float(m_stop.group(1))))
            except ValueError:
                continue
    return out


def net_max_loss(t, market):
    """净口径修正 max_loss = 仓位×|开平-止损| + 开仓边费 + 止损价平仓边费。"""
    import fee_schedule as F
    if not t["stop"] or not t["entry"] or not t["qty"]:
        return None
    side = "etf" if market == "us" else "etf"
    mkt = "US" if market == "us" else "HK"
    dist = abs(t["entry"] - t["stop"])
    f_open = F.fee_per_side(mkt, side, t["entry"] * t["qty"], shares=t["qty"])
    f_stop = F.fee_per_side(mkt, side, t["stop"] * t["qty"], shares=t["qty"])
    return abs(dist * t["qty"]) + f_open + f_stop


def bj(ts_ms):
    if not ts_ms:
        return "-"
    return datetime.fromtimestamp(ts_ms / 1000, ET).astimezone(BJ).strftime("%m-%d %H:%M")


def et_hm(ts_ms):
    if not ts_ms:
        return "-"
    return datetime.fromtimestamp(ts_ms / 1000, ET).strftime("%m-%d %H:%M")


def parse_stage_odds(actions_path):
    """从 actions 文件解析「三阶段赔率」列，返回 {序号: '初始→修正'} 供拼表；解析不到返回 {}。"""
    out = {}
    if not os.path.exists(actions_path):
        return out
    try:
        txt = open(actions_path, encoding="utf-8").read()
    except Exception:
        return out
    for m in re.finditer(r"初始预期赔率\s*\|\s*([\d.]+)R", txt):
        pass
    rows = re.findall(r"\|\s*初始预期赔率\s*\|\s*([\d.]+)R.*?\|", txt)
    fixes = re.findall(r"修正预期赔率\s*\|\s*([\d.]+)R", txt)
    for i, init in enumerate(rows, 1):
        fix = fixes[i - 1] if i - 1 < len(fixes) else "-"
        out[i] = f"{init} → {fix}"
    return out


def detect_trade_date(market, account_mode):
    """最近 7 天内最近一个有成交的 ET 交易日。"""
    for back in range(0, 7):
        d = (datetime.now(ET) - timedelta(days=back)).date()
        s = datetime(d.year, d.month, d.day, 0, 0, tzinfo=ET)
        e = s + timedelta(days=1)
        try:
            orders, _ = fetch_orders(market, account_mode, s, e)
        except Exception:
            continue
        if any(status_str(o) == "FILLED" for o in orders):
            return d.isoformat()
    return datetime.now(ET).date().isoformat()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", default="us", choices=["us", "hk"])
    ap.add_argument("--date", default=None, help="ET 交易日 YYYY-MM-DD")
    ap.add_argument("--fx", type=float, default=None, help="CNY/USD 汇率（可选，用于 token 费用）")
    ap.add_argument("--no-feishu", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--account", default="paper", choices=["paper", "live"])
    ap.add_argument("--scope", default=None, choices=["session", "account"],
                    help="统计范围：session=只统计本会话（默认，多会话并行必须用）；account=全账户")
    ap.add_argument("--detect-market", action="store_true",
                    help="只输出本会话盯的市场（us/hk/空），给 shell 脚本用，不做其它动作")
    a = ap.parse_args()

    if a.detect_market:
        print(detect_market_for_session())
        return 0

    root = project_root()
    sid8 = current_sid8()
    date = a.date or detect_trade_date(a.market, a.account)
    y, m, d = [int(x) for x in date.split("-")]
    start = datetime(y, m, d, 0, 0, tzinfo=ET)
    end = start + timedelta(days=1)

    print(f"=== 停盯收尾：{a.market.upper()} ET 交易日 {date} ===")
    orders, masked = fetch_orders(a.market, a.account, start, end)
    trades = [t for t in build_trades(orders) if t["entry_filled"]]
    scope = a.scope or ("session" if sid8 else "account")
    scope_note = ""
    if scope == "session":
        suffix_s = "ET" if a.market == "us" else "HKT"
        _ap = os.path.join(root, "actions", f"{date}-{suffix_s}-actions.md")
        owned = owned_entries(_ap, sid8)
        if owned:
            kept = []
            for t in trades:
                if any(o[0] == t["symbol"] and t["entry"] and abs(o[1] - t["entry"]) / t["entry"] < 0.01
                       for o in owned):
                    kept.append(t)
            print(f"会话口径：本会话（{sid8}）命中 {len(kept)}/{len(trades)} 笔")
            trades = kept
            scope_note = f"本会话 {sid8}"
        else:
            scope = "account(fallback)"
            scope_note = f"未找到本会话({sid8 or 'anon'})的动作归属记录，退化为全账户口径"
            print(f"⚠️ {scope_note}")
    if not trades:
        print(f"⚠️ 该交易日无成交订单（{len(orders)} 条订单但无开仓成交）——仍会生成 0 交易收尾。")
    print(f"账户 {masked}｜订单 {len(orders)} 条｜成笔 {len(trades)} 笔")

    suffix0 = "ET" if a.market == "us" else "HKT"
    acts_path = os.path.join(root, "actions", f"{date}-{suffix0}-actions.md")
    orig = original_stops(acts_path)

    rows = []
    total_pnl = 0.0
    total_ml = 0.0
    wins = 0
    for i, t in enumerate(trades, 1):
        # 原始止损优先（与成交价价差 <1% 视为同一笔）
        cand = sorted(
            [o for o in orig if o[0] == t["symbol"] and t["entry"] and abs(o[1] - t["entry"]) / t["entry"] < 0.01],
            key=lambda o: abs(o[1] - t["entry"]))
        if cand:
            t["stop"] = cand[0][2]   # 取成交价最接近的一条（同日同标的可能有多笔、价差 <1%）
            t["stop_src"] = "actions(原始止损)"
        else:
            t["stop"] = t.get("stop_leg")
            t["stop_src"] = "leg(含移损,可能偏小)"
        ml = net_max_loss(t, a.market)
        r = (t["pnl"] / ml) if (ml and t["pnl"] is not None) else None
        total_pnl += t["pnl"] or 0
        if ml:
            total_ml += ml
        if (t["pnl"] or 0) > 0:
            wins += 1
        rows.append({
            "i": i, "t": t, "ml": ml, "r": r,
            "in_bj": bj(t["entry_time"]), "out_bj": bj(t["exit_time"]),
            "in_et": et_hm(t["entry_time"]), "out_et": et_hm(t["exit_time"]),
        })

    overall_r = (total_pnl / total_ml) if total_ml else None
    print(f"净盈亏 {total_pnl:+.2f} USD｜总 max_loss {total_ml:.2f}｜整体赔率 "
          f"{('%.2fR' % overall_r) if overall_r is not None else 'N/A'}｜胜率 "
          f"{(wins / len(trades) * 100 if trades else 0):.0f}%")

    # ---- 汇总 Markdown ----
    L = []
    L.append("")
    L.append("════════════════════════════════════")
    L.append(f"🔵🔵🔵 停盯总结 · {a.market.upper()} {date} 交易日（auto 模式 · 账户 {masked}）🔵🔵🔵")
    L.append(f"> ⏰ 脚本生成：{datetime.now(BJ).strftime('%Y-%m-%d %H:%M')}（北京）｜数据源：券商 API 实际成交（非 AI 记忆）")
    L.append(f"> 🆔 会话：{sid8 or 'anon'}｜口径：{scope_note or scope}")
    L.append("════════════════════════════════════")
    L.append("")
    L.append("## 一、交易明细（券商对账口径）")
    L.append("")
    L.append("| # | 标的 | 方向 | 量 | 开仓价 | 平仓价 | 净盈亏 | 原始止损 | 修正 max_loss | 实际落地赔率 | 止损来源 |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|")
    for rw in rows:
        t = rw["t"]
        L.append("| {i} | {sym} | {d} | {q} | {e:.4f} | {x} | {p} | {s} | {ml} | {r} | {src} |".format(
            i=rw["i"], sym=t["symbol"], d=("做多" if t["direction"] == "long" else "做空"),
            q=t["qty"], e=t["entry"],
            x=("%.4f" % t["exit"]) if t["exit"] else "—",
            p=("%+.2f" % t["pnl"]) if t["pnl"] else "0.00",
            s=("%.2f" % t["stop"]) if t.get("stop") else "—",
            ml=("%.2f" % rw["ml"]) if rw["ml"] else "—",
            r=("%+.2fR" % rw["r"]) if rw["r"] is not None else "—",
            src=t.get("stop_src", "-")))
    L.append("")
    L.append("## 二、统计汇总")
    L.append("")
    L.append("| 指标 | 值 |")
    L.append("|---|---|")
    L.append(f"| 净盈亏 | **{total_pnl:+.2f}** |")
    L.append(f"| 笔数 / 胜率 | {len(trades)} 笔 / **{(wins / len(trades) * 100 if trades else 0):.0f}%** |")
    L.append(f"| 总 max_loss（净口径） | {total_ml:.2f} |")
    L.append(f"| **整体实际落地赔率** | **{('%.2fR' % overall_r) if overall_r is not None else 'N/A'}**（总盈亏 ÷ 总 max_loss） |")
    L.append("")
    L.append("## 三、北京日期对照（App 对账用）")
    L.append("")
    L.append("⚠️ 老虎 App 的「今日」按**北京日期**切分，本表按**美东交易日**切分——跨北京午夜时同一交易日会被 App 拆成两天，对账先看这张表。")
    L.append("")
    L.append("| # | 开仓（美东） | 平仓（美东） | 开仓（北京） | 平仓（北京） | App 归日 |")
    L.append("|---|---|---|---|---|---|")
    for rw in rows:
        L.append(f"| {rw['i']} | {rw['in_et']} | {rw['out_et']} | {rw['in_bj']} | {rw['out_bj']} | {rw['in_bj'][:5]} |")
    L.append("")
    by_bj = {}
    for rw in rows:
        by_bj.setdefault(rw["in_bj"][:5], []).append(rw)
    if len(by_bj) > 1:
        parts = []
        for k, v in sorted(by_bj.items()):
            parts.append(f"北京 {k} 见 {len(v)} 笔（净 {sum(x['t']['pnl'] or 0 for x in v):+.2f}）")
        L.append("**对照结论**：" + "；".join(parts) + f"，合计 {total_pnl:+.2f}。")
        L.append("")
    L.append("## 四、要点与教训（AI 手写补充区）")
    L.append("")
    L.append("（此处由 AI 补写：行情性质 / 打法得失 / 是否回归风险 / 下一步建议）")
    L.append("")
    summary_md = "\n".join(L)
    print(summary_md)

    # ---- 飞书业务版 ----
    F = []
    F.append(f"# {a.market.upper()} {date} 交易日小结（模拟盘）")
    F.append("")
    F.append("## 交易明细")
    F.append("")
    F.append("| # | 标的 | 方向 | 开仓价 | 平仓价 | 净盈亏 |")
    F.append("|---|---|---|---|---|---|")
    for rw in rows:
        t = rw["t"]
        F.append("| {i} | {sym} | {d} | {e:.4f} | {x} | {p} |".format(
            i=rw["i"], sym=t["symbol"], d=("做多" if t["direction"] == "long" else "做空"),
            e=t["entry"], x=("%.4f" % t["exit"]) if t["exit"] else "—",
            p=("%+.2f" % t["pnl"]) if t["pnl"] else "0.00"))
    F.append("")
    F.append("## 汇总")
    F.append("")
    F.append(f"- 笔数：{len(trades)} 笔（{wins} 胜 {len(trades) - wins} 负），胜率 {(wins / len(trades) * 100 if trades else 0):.0f}%")
    F.append(f"- 净盈亏：**{total_pnl:+.2f}**")
    F.append(f"- 整体实际落地赔率：**{('%.2fR' % overall_r) if overall_r is not None else 'N/A'}**（总盈亏 ÷ 总 max_loss {total_ml:.2f}）")
    F.append(f"- 收工时状态：空仓（无隔夜持仓）")
    if len(by_bj) > 1:
        F.append("")
        F.append("## 对账提示")
        F.append("")
        F.append("老虎 App 按北京日期分组，本交易日跨北京午夜，会被拆成两天显示：")
        for k, v in sorted(by_bj.items()):
            F.append(f"- 北京 {k}：{len(v)} 笔（净 {sum(x['t']['pnl'] or 0 for x in v):+.2f}）")
    feishu_md = "\n".join(F)

    if a.dry_run:
        print("\n=== 飞书业务版（dry-run，不发送）===")
        print(feishu_md)
        print("\n[dry-run] 未落盘、未发送、未写标记")
        return 0

    # 会话短 id 后缀（多会话并行时区分飞书文件 / 收尾标记；2026-10-20 修：原定义在步骤 3、
    # 步骤 2 飞书文件先引用导致 UnboundLocalError 崩溃）
    mid = f"_{sid8}" if sid8 else ""

    # 1) 追加进 actions 文件
    suffix = "ET" if a.market == "us" else "HKT"
    actions_path = os.path.join(root, "actions", f"{date}-{suffix}-actions.md")
    os.makedirs(os.path.dirname(actions_path), exist_ok=True)
    _exists_same = False
    if os.path.exists(actions_path):
        _txt = open(actions_path, encoding="utf-8").read()
        if "停盯总结" in _txt:
            if not sid8:
                _exists_same = True
            else:
                # 只认【本会话】已写过总结；其它会话的总结不阻止本会话追加（多会话并行）
                for mm in re.finditer(r"🆔 会话：([0-9a-zA-Z]+)", _txt):
                    tail = _txt[mm.end(): mm.end() + 200]
                    if mm.group(1) == sid8 and "停盯总结" in _txt[max(0, mm.start() - 400): mm.end() + 200]:
                        _exists_same = True
                        break
    if _exists_same:
        print(f"⚠️ {actions_path} 已有【本会话】的停盯总结，跳过追加（防重复）")
    else:
        # 多会话并行：两个会话可能几乎同时停盯并追加同一文件，用 flock 串行化防交错写入
        import fcntl
        with open(actions_path, "a+", encoding="utf-8") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                f.write(summary_md + "\n")
                f.flush()
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        print(f"✅ 停盯总结已写入 {actions_path}")

    # 2) 飞书
    sent_id = None
    if not a.no_feishu:
        feishu_file = os.path.join(root, "tmp", f"feishu_stop_{date}-{a.market}{mid}.md")
        os.makedirs(os.path.dirname(feishu_file), exist_ok=True)
        with open(feishu_file, "w", encoding="utf-8") as f:
            f.write(feishu_md + "\n")
        r = subprocess.run([sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "notify_feishu.py"),
                            "--file", feishu_file, "--market", a.market],
                           capture_output=True, text=True)
        print(r.stdout.strip()[-300:] or r.stderr.strip()[-300:])
        mt = re.search(r"message_id=(\S+?)[）)]", r.stdout)
        sent_id = mt.group(1) if mt else None
        if "已发送" not in r.stdout:
            print("⚠️ 飞书推送未确认成功——收尾标记仍会写，但请人工核对")
    else:
        print("⚠️ --no-feishu：跳过飞书推送")

    # 3) 收尾标记（供 monitor_guard / preflight 检查）
    marker = os.path.join(root, "tmp", f"stop_done_{a.market}_{date}{mid}.txt")
    with open(marker, "w", encoding="utf-8") as f:
        f.write(json.dumps({
            "market": a.market, "et_date": date, "account": masked,
            "scope": scope, "session": sid8 or None,
            "trades": len(trades), "net_pnl": round(total_pnl, 2),
            "total_max_loss": round(total_ml, 2),
            "overall_r": round(overall_r, 4) if overall_r is not None else None,
            "feishu_message_id": sent_id,
            "feishu_sent": bool(sent_id) or a.no_feishu,
            "token_cost_logged": bool(a.fx),
            "written_at_bj": datetime.now(BJ).strftime("%Y-%m-%d %H:%M:%S"),
        }, ensure_ascii=False))
    print(f"✅ 收尾标记已写 {marker}")

    # 4) token 费用（可选）
    if a.fx:
        sid = os.environ.get("PI_SESSION_FILE") or ""
        if sid:
            r = subprocess.run([sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "token_cost.py"),
                                sid, "--before", datetime.now(BJ).isoformat(), "--fx", str(a.fx), "--write-csv"],
                               capture_output=True, text=True)
            print((r.stdout or r.stderr).strip()[-300:])
    else:
        print("ℹ️ 未传 --fx：token 费用未记账（需 CNY/USD 实值，勿估算）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
