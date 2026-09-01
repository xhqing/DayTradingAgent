#!/usr/bin/env python3
"""当日 actions 开平闭环检查（2026-08-31 T132 落地）。

为什么需要：BRACKETS 止损/止盈腿自动触发的平仓不经 close_position 脚本、也无 AI 在场
转录——2026-08-27 01888 开仓后盯盘 13:47 中断（多会话切换）、PROFIT 腿 15:34 自动触发
平仓，actions 直到 2026-08-31 全量复盘才发现并补录，**平仓事件漏记 4 天**（期间持仓
推导、连败计数、复盘样本全部失真）。本脚本把「开→平闭环检查」做成机械动作，两个触发
点：① 盯盘中断恢复（resume.py 收尾自动跑）；② 停盯收尾（monitor_unregister.sh 联动）。

做什么：
  1. 解析当日 actions 文件（HKT + ET）的开仓/平仓事件（标题行 + ⏰ 时间 + 量），
     按标的做数量配平——开仓量 > 平仓量 = 未闭环开仓；
  2. 对每个未闭环标的查**账户实持**（auto 模式订单的真实持仓）：
     - 账户有仓 → 仍持仓（正常，盯盘继续，闭环检查通过）；
     - 账户无仓 → 已被自动触发平仓但 actions 未记 → **需补记**（输出补记指引：
       log_action.sh 补记平仓 + 无成交价按当日 K 线推定并标注口径 + 跑
       update_losing_streak.py 把该笔计入连败）；
  3. 平仓量 > 开仓量（多平）同样报出——记录口径异常。

用法：
  python3 actions_check.py [--account live|paper] [--market HK|US] [--no-position]
    --account       查实持的账户（默认 paper；实盘日传 live——与当日下单同账户）
    --market        只查该市场（默认 HK+US 都查）
    --no-position   只做 actions 配平、跳过账户实持核对（老虎 API 不可用时）

退出码：0 = 全闭环或未闭环但账户仍持仓；1 = 存在「已平仓未补记」或多平异常。
（resume 场景由 AI 读输出处置；monitor_unregister 场景靠非零退出码醒目警示。）
"""

import json
import os
import re
import sys
from datetime import date

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, "..", "..", "..", ".."))
ACTIONS_DIR = os.path.join(PROJECT_ROOT, "actions")

sys.path.insert(0, SCRIPT_DIR)

# 标题分节口径与 account_status._parse_actions 同源（2026-08-23 修：兼容「## 」前缀与
# 裸标题两种历史格式）；代码提取扩展认美股（account_status 版只认港股 5 位数字）。
_SECTION_RE = re.compile(
    r'\n(?=(?:#+\s*)?(?:🟢🟢🟢|🔴🔴🔴|🟡🟡🟡|🔵🔵🔵)\s*(?:开仓|平仓|移动止损|移动止盈))')
_SYM_RE = re.compile(r'(HK\.\d{5}|US\.[A-Z]{1,6}(?![A-Z])|(?<![A-Z.\d])\d{5}(?!\d))')
_QTY_ROW_RE = re.compile(r'\|\s*量\s*\|\s*\*{0,2}(\d+)')
# 平仓记录转录模板无量行（2026-09-01 实测：开仓有「| 量 | 4700 股 |」、平仓表格是
# 标的/方向/开仓价/平仓价/盈亏——当日真实检查误报未闭环 4700 股）。回退提取
# 「开仓价 | …（4700 股…」括号内股数；都无 → None（配平按全平核销）。
_QTY_OPEN_RE = re.compile(r'开仓价[^（(\n]*[（(](\d+)\s*股')
_TS_RE = re.compile(r'⏰\s*(?:动作|补录)时间：(\S+)')


def _parse_events(files):
    """解析动作文件 → [{ts, type(open/close/move), sym, qty}]。move 不参与配平（只顺带解析）。

    qty 三态：正数 = 表格/开仓价行解析到；None = 无任何量信息（close 配平时按全平核销——
    close_position 转录模板平仓节不带量行是常态格式）。"""
    events = []
    for fp in files:
        try:
            with open(fp, encoding="utf-8") as f:
                text = f.read()
        except Exception:
            continue
        market = "US" if "-ET-actions" in os.path.basename(fp) else "HK"
        for sec in _SECTION_RE.split("\n" + text):
            title = sec.split("\n", 1)[0]
            if "开仓" in title:
                etype = "open"
            elif "平仓" in title:
                etype = "close"
            elif "移" in title:
                etype = "move"
            else:
                continue
            m = _SYM_RE.search(title)
            sym = m.group(1) if m else ""
            if sym and not sym.startswith(("HK.", "US.")):
                sym = f"{'HK' if market == 'HK' else 'US'}.{sym}"
            mq = _QTY_ROW_RE.search(sec)
            qty = int(mq.group(1)) if mq else None
            if qty is None:
                mo = _QTY_OPEN_RE.search(sec)
                qty = int(mo.group(1)) if mo else None
            mt = _TS_RE.search(sec)
            events.append({"market": market, "ts": mt.group(1) if mt else "",
                           "type": etype, "sym": sym, "qty": qty, "file": os.path.basename(fp)})
    events.sort(key=lambda e: e["ts"] if e["ts"] else "")
    return events


def _account_position(sym, account):
    """查账户实持该标的的数量（0 = 无仓）。查询失败返回 None（区别于无仓）。"""
    try:
        if sym.startswith("HK."):
            import trade_utils_tiger as U
            config = U.load_config(account=account)
            pos = U.get_open_position_tiger(config, sym)
        else:
            import trade_utils_tiger_us as UU
            import trade_utils_tiger as U
            config = U.load_config(account=account)
            pos = UU.get_open_position_us(config, sym)
        return abs(pos["quantity"]) if pos else 0
    except Exception:
        return None


def main():
    account = None
    market_filter = None
    no_position = False
    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--account" and i + 1 < len(args):
            account = args[i + 1].lower()
            i += 2
        elif args[i].startswith("--account="):
            account = args[i].split("=", 1)[1].lower()
            i += 1
        elif args[i] == "--market" and i + 1 < len(args):
            market_filter = args[i + 1].upper()
            i += 2
        elif args[i].startswith("--market="):
            market_filter = args[i].split("=", 1)[1].upper()
            i += 1
        elif args[i] == "--no-position":
            no_position = True
            i += 1
        else:
            i += 1
    if account not in (None, "live", "paper"):
        print(json.dumps({"ok": False, "error": f"--account 必须是 live/paper，收到 '{account}'"}))
        sys.exit(1)

    today = date.today().strftime("%Y-%m-%d")
    files = [os.path.join(ACTIONS_DIR, f"{today}-{tag}-actions.md")
             for tag in ("HKT", "ET")]
    files = [f for f in files if os.path.exists(f)]
    if not files:
        print(f"✅ 当日（{today}）无 actions 文件——无 auto 交易记录，闭环检查通过。")
        sys.exit(0)

    events = _parse_events(files)
    if market_filter:
        events = [e for e in events if e["market"] == market_filter]
    opens = [e for e in events if e["type"] == "open"]
    closes = [e for e in events if e["type"] == "close"]

    # 按 sym 配平（sym 为空的补充记录无法归属标的、跳过配平仅提示）：
    # open 累加 qty（None 按 0——开仓无量信息本身是记录缺陷，配平后若未闭环会显式报出）；
    # close 的 qty 为 None 时按「全平核销」处理（close_position 转录模板平仓节不带量行）。
    balance = {}
    for e in opens:
        if not e["sym"]:
            continue
        balance.setdefault(e["sym"], {"open": 0, "close": 0})["open"] += e["qty"] or 0
    for e in closes:
        if not e["sym"]:
            continue
        b = balance.setdefault(e["sym"], {"open": 0, "close": 0})
        if e["qty"] is None:
            b["close"] = b["open"]   # 无量信息的平仓记录：按全平核销
        else:
            b["close"] += e["qty"]

    problems = []
    no_sym = sum(1 for e in events if not e["sym"] and e["type"] in ("open", "close"))
    for sym, b in sorted(balance.items()):
        if not sym:
            continue
        net = b["open"] - b["close"]
        if net <= 0:
            continue
        # 未闭环：查账户实持判「仍持仓」还是「已平仓未补记」
        if no_position:
            problems.append({"sym": sym, "net_qty": net, "position": None,
                             "verdict": "unknown"})
            continue
        held = _account_position(sym, account)
        if held is None:
            problems.append({"sym": sym, "net_qty": net, "position": None,
                             "verdict": "query_failed"})
        elif held >= net:
            pass   # 账户仍有仓：正常持仓中，闭环检查通过
        else:
            problems.append({"sym": sym, "net_qty": net, "position": held,
                             "verdict": "closed_unrecorded"})
    # 平仓多于开仓（多平）——记录口径异常，直接报
    for sym, b in sorted(balance.items()):
        if sym and b["close"] > b["open"]:
            problems.append({"sym": sym, "net_qty": b["open"] - b["close"],
                             "position": None, "verdict": "over_closed"})

    result = {"ok": not problems, "date": today, "opens": len(opens), "closes": len(closes),
              "unclosed": problems,
              "note_no_sym": (f"有 {no_sym} 条开/平记录标题无代码、未参与配平" if no_sym else "")}
    print(json.dumps(result, ensure_ascii=False, indent=1))
    if problems:
        print("\n⚠️ actions 开平闭环异常处置（T132）：")
        for p in problems:
            if p["verdict"] == "closed_unrecorded":
                print(
                    f"  🚨 {p['sym']}：actions 记开仓净量 {p['net_qty']} 股、账户实持 {p['position']} 股"
                    f"——差额已被止损/止盈腿【自动触发平仓】但未记录（2026-08-27 01888 漏记 4 天同型）。"
                    f"处置：① 核对账户口径（本检查按 --account 所指账户查实持——当日交易若在另一"
                    f"账户，先加对应 --account live/paper 复查再定，防止跨账户误报）；② 查当日订单"
                    f"（get_today_orders）拿实际触发价与成交量，无成交价则按当日 K 线推定并标注"
                    f"「推定口径」；③ 经 log_action.sh 补记平仓；④ 跑 update_losing_streak.py"
                    f" 把该笔计入连败（净亏损会影响一级降频线）。")
            elif p["verdict"] == "query_failed":
                print(f"  ⚠️ {p['sym']}：未闭环 {p['net_qty']} 股、账户实持查询失败——手动查账户确认是否已平。")
            elif p["verdict"] == "over_closed":
                print(f"  ⚠️ {p['sym']}：平仓量多于开仓量 {abs(p['net_qty'])} 股——记录口径异常，核对当日记录。")
            else:
                print(f"  ⚠️ {p['sym']}：未闭环 {p['net_qty']} 股（实持核对被跳过）——人工确认是否已平。")
        sys.exit(1)
    held_note = "；未闭环标的账户实持均正常（仍持仓）" if any(
        b["open"] > b["close"] for s, b in balance.items() if s) else ""
    print(f"✅ 当日 actions 开平闭环检查通过（开仓 {len(opens)} 条 / 平仓 {len(closes)} 条{held_note}）。")
    sys.exit(0)


if __name__ == "__main__":
    main()
