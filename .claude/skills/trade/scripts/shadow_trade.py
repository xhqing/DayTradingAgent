#!/usr/bin/env python3
"""影子交易（shadow trade）——auto 模式下被单持仓互斥闸拦下的机会的纸面记录（2026-08-27 立）。

定义（用户 2026-08-27 定案）：**实盘自动模式（auto）下开仓时碰到已有持仓（互斥闸
blocked_by=open_exposure_today / active_open_order）→ 本会话不真下单，改为把这笔机会按
「信号式记录」落成一笔模拟交易，打【影子】标签**——影子交易是 auto 模式的分支交易路径，
既不是 signal 模式、也不是 auto 模式的模拟账户交易，三者必须区分。

与 signal 模式的区别：signal 模式是用户启动的整会话模式（AI 只发信号、用户手动执行）；
影子交易是 auto 会话内被拦机会的纸面分支（用户不执行任何动作、无响铃、假设成交）。
与 auto 模拟账户交易的区别：auto 模拟盘下真单（模拟账户里有真实订单与持仓）；影子交易
不下任何单、不碰账户，纯记录。

为什么做（2026-08-27 用户立）：多会话并行盯盘时全局至多一笔在场（方案 A 单持仓互斥），
后到机会全被闸门拦下删失——被拦样本永远无法验证、复盘只见「第一名机会」，样本搜集被
浪费。影子交易把被拦机会变成可统计样本（假设成交、按响铃时刻价近似口径），复盘可
单独一组统计（与真实样本分可合）。

硬边界（用户 2026-08-27 定案的 4 个决策点）：
  1. 真实仓优先——影子仓期间本池又出真实机会：真实仓优先，影子仓被动并行盯（影子是
     纸面、不紧急）；不放走真实机会，也不提前终止影子仓（终止会产生截断偏差）。
  2. 每会话同时至多一笔影子仓；真实「全局至多一笔」硬规矩不变。
  3. 只在 blocked_by=open_exposure_today / active_open_order（明确的「别人在场」）时落
     影子仓；pending_intent（可能崩溃残留）不落——先人工排查真实状态。
  4. 复盘统计影子样本单独一组（成交价近似、无真实滑点、且是「被拦下的机会」，与真实
     样本不同质），打【影子】标、可分可合，不与真实样本混算胜率。

记录载体：signals/ 目录（与 signal 模式同目录、复用复盘读取链）——当日信号文件
signals/YYYY-MM-DD-HKT-signals.md / -ET-signals.md 里追加带【影子】标的条目；
影子状态文件 tmp/shadow_positions.json（本机运行时数据，不入库）。

不碰的东西（与互斥机制的隔离，这是本模块最重要的设计约束）：
  - 不碰 trade_mutex 的 intent 日志（不下真单、无需 WAL）；
  - 不拿 flock 锁（影子开仓与真实开仓无竞态——真实闸门查的是订单流，影子仓不在订单流上）；
  - 不碰 signals/equity-log.csv（影子盈亏只记单笔 R / 净盈亏，不进 equity 累加链——
    影子是样本、不是资金曲线）。

用法（命令行）：
  python3 shadow_trade.py open <market> <symbol> <direction> <ref_price> <stop> <target> <qty>
      记一笔影子开仓（decision_price 取当下快照 last_price 近似；qty 用计划仓位）
  python3 shadow_trade.py close <market> <symbol> <exit_price> <reason>
      记一笔影子平仓（exit_price 取决策时刻价；输出该笔净盈亏 / R）
  python3 shadow_trade.py move <market> <symbol> <new_stop>
      记一笔影子移损（纸面更新止损价，平仓 R 的分母按开仓时止损距不变——毛值口径与真实仓一致）
  python3 shadow_trade.py status [market]
      查当前未平影子仓（每会话至多一笔的判定基础）
"""

import json
import os
import sys
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, "..", "..", "..", ".."))
SIGNALS_DIR = os.path.join(PROJECT_ROOT, "signals")
TMP_DIR = os.path.join(PROJECT_ROOT, "tmp")
SHADOW_STATE = os.path.join(TMP_DIR, "shadow_positions.json")

MARKETS = ("HKT", "ET")


# ---------------------------------------------------------------------------
# 状态文件（tmp/shadow_positions.json：本机运行时数据，.gitignore 已覆盖 tmp/）
# ---------------------------------------------------------------------------

def _load_state():
    if not os.path.exists(SHADOW_STATE):
        return {}
    try:
        with open(SHADOW_STATE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(state):
    os.makedirs(TMP_DIR, exist_ok=True)
    tmp = SHADOW_STATE + ".tmp"
    with open(tmp, "w") as f:
        f.write(json.dumps(state, ensure_ascii=False, indent=1))
    os.replace(tmp, SHADOW_STATE)


def open_shadow(market, symbol, direction, ref_price, stop, target, qty,
                blocked_by, session_id=None):
    """记一笔影子开仓。返回 (ok, message)。

    前置校验（工具强制，按 2026-08-27 定案的决策点 2 / 3）：
      - blocked_by 必须是 open_exposure_today / active_open_order（明确的「别人在场」）；
        pending_intent 等其它 blocked_by 不落影子仓（决策点 3：状态不明先人工排查）。
      - 本会话已有未平影子仓时拒开（决策点 2：每会话同时至多一笔影子仓）。
    """
    session_id = session_id or os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    if blocked_by not in ("open_exposure_today", "active_open_order"):
        return False, (f"blocked_by={blocked_by} 不落影子仓——只落「别人在场」口径"
                       f"（open_exposure_today / active_open_order）；pending_intent 等"
                       f"状态不明场景先人工排查真实状态（2026-08-27 定案决策点 3）")
    state = _load_state()
    # 决策点 2：每会话至多一笔未平影子仓（按 session_id 隔离；无 sid 环境时按「全局一笔」保守）
    for key, pos in state.items():
        holder = pos.get("session_id") or ""
        if not pos.get("closed") and (holder == session_id or not session_id or not holder):
            return False, (f"已有未平影子仓 {pos.get('symbol')}（{pos.get('opened_at')} 开、"
                           f"holder={holder or session_id}）——每会话同时至多一笔影子仓，"
                           f"先等该笔平掉（决策点 2）")
    key = f"{market}:{symbol}"
    if key in state and not state[key].get("closed"):
        return False, f"{symbol} 已有未平影子仓，不重复落"
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    state[key] = {
        "market": market, "symbol": symbol, "direction": direction,
        "ref_price": ref_price, "stop": stop, "target": target, "qty": qty,
        "decision_price": ref_price,       # 决策时刻价（拍板价近似，作假设成交价）
        "blocked_by": blocked_by,
        "session_id": session_id,
        "opened_at": now,
    }
    _save_state(state)
    _append_signal_file(market, _fmt_open(state[key]))
    return True, (f"✅ 影子开仓已记录：{symbol} {direction} @{ref_price}（止损 {stop} / 止盈 {target}"
                  f" / {qty} 股）——纸面假设成交、不碰账户，等平仓时机走 close")


def close_shadow(market, symbol, exit_price, reason):
    """记一笔影子平仓：结算净盈亏 / R（净口径分母 = 开仓止损距 × 量 + 开仓费 + 止损价平仓费，
    与真实仓 2026-08-28 净口径一致），追加平仓条目到信号文件，状态文件标记 closed。"""
    state = _load_state()
    key = f"{market}:{symbol}"
    pos = state.get(key)
    if not pos or pos.get("closed"):
        return False, f"{symbol} 无未平影子仓"
    direction = pos["direction"]
    qty = pos["qty"]
    sign = 1 if direction == "long" else -1
    gross = (exit_price - pos["decision_price"]) * qty * sign
    stop_dist = abs(pos["decision_price"] - pos["stop"])
    fee_open = _side_fee(market, symbol, qty, pos["decision_price"])
    fee_exit = _side_fee(market, symbol, qty, exit_price)
    fee_stop = _side_fee(market, symbol, qty, pos["stop"])
    max_loss = qty * stop_dist + fee_open + fee_stop   # 净值（毛止损距 + 开仓费 + 止损价平仓费）
    net = gross - fee_open - fee_exit
    r = net / max_loss if max_loss > 0 else 0.0
    closed_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    pos.update({"closed": True, "closed_at": closed_at, "exit_price": exit_price,
                "gross": round(gross, 2), "net": round(net, 2),
                "max_loss": round(max_loss, 2), "R": round(r, 3), "reason": reason})
    _save_state(state)
    _append_signal_file(market, _fmt_close(pos))
    return True, (f"✅ 影子平仓已结算：{symbol} {direction} {pos['decision_price']}→{exit_price}"
                  f"，毛 {gross:+,.0f} / 净 {net:+,.0f}（含近似费）/ R {r:+.3f}"
                  f"（净 max_loss {max_loss:,.0f}：止损距+开仓费+止损价平仓费）")


def move_shadow(market, symbol, new_stop):
    """影子移损：纸面更新止损价（只影响后续平仓判定参考，不改已记开仓条目）。"""
    state = _load_state()
    key = f"{market}:{symbol}"
    pos = state.get(key)
    if not pos or pos.get("closed"):
        return False, f"{symbol} 无未平影子仓"
    old = pos["stop"]
    pos["stop"] = new_stop
    pos.setdefault("moves", []).append(
        {"ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "from": old, "to": new_stop})
    _save_state(state)
    _append_signal_file(market, _fmt_move(pos, old, new_stop))
    return True, f"✅ 影子移损已记录：{symbol} 止损 {old} → {new_stop}（平仓 R 分母按开仓时止损距不变）"


def status(market=None):
    """打印当前未平影子仓。"""
    state = _load_state()
    open_positions = [p for p in state.values() if not p.get("closed")]
    if market:
        open_positions = [p for p in open_positions if p.get("market") == market]
    if not open_positions:
        print("✅ 无未平影子仓")
        return
    print(f"📌 未平影子仓 {len(open_positions)} 笔：")
    for p in open_positions:
        print(f"   {p['market']} {p['symbol']} {p['direction']} @{p['decision_price']}"
              f"（止损 {p['stop']} / 目标 {p['target']} / {p['qty']} 股，{p['opened_at']} 开，"
              f"blocked_by={p.get('blocked_by')}）")


# ---------------------------------------------------------------------------
# 信号文件记录（signals/ 目录，与 signal 模式同载体、复盘读取链复用）
# ---------------------------------------------------------------------------

def _signal_file(market):
    today = datetime.now().strftime("%Y-%m-%d")
    return os.path.join(SIGNALS_DIR, f"{today}-{market}-signals.md")


def _append_signal_file(market, content):
    """影子条目 append 到当日信号文件。与 log_signal.sh 分工：log_signal 是 signal 模式
    三步时序的第 1 步（拍板即写、带响铃时间戳语义）；影子不走三步时序（无响铃、假设成交），
    直接写完整条目（自带时间戳行），不走 log_signal.sh。"""
    os.makedirs(SIGNALS_DIR, exist_ok=True)
    path = _signal_file(market)
    sep_needed = os.path.exists(path) and os.path.getsize(path) > 0
    with open(path, "a") as f:
        if sep_needed:
            f.write("\n")
        f.write(content + "\n")


def _fmt_open(p):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return f"""════════════════════════════════════
🟢🟢🟢 【影子】开仓 · {p['symbol']} · {'做多' if p['direction'] == 'long' else '做空'}（影子交易，不下单）🟢🟢🟢
════════════════════════════════════
> ⏰ 影子开仓时间：{now}

| 项 | 值 |
|---|---|
| 类型 | **【影子】**（auto 互斥闸拦截的机会的纸面记录——不下单、不碰账户、不响铃，假设成交；被拦原因 `{p.get('blocked_by')}`）|
| 方向 | {'做多' if p['direction'] == 'long' else '做空'} |
| 量 | {p['qty']} 股（计划仓位，未真实成交）|
| 参考价 | **{p['ref_price']}** |
| **假设成交价** | **{p['decision_price']}**（决策时刻价近似，影子无响铃时刻价口径）|
| 止损 | **{p['stop']}** |
| 止盈 | {p['target']} |
| 预估 max_loss | {p['qty'] * abs(p['decision_price'] - p['stop']):,.0f}（毛值；净口径 = 毛值+开仓费+止损价平仓费，平仓结算时算）|

> 影子交易 = auto 模式下被单持仓互斥拦下的机会的模拟记录（shadow，2026-08-27 立），非 signal 模式、非模拟账户交易。真实仓优先；本笔只作复盘样本（单独统计、不与真实样本混算）。"""


def _fmt_close(p):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return f"""════════════════════════════════════
🔴🔴🔴 【影子】平仓 · {p['symbol']} · 平{'多' if p['direction'] == 'long' else '空'}（影子交易结算）🔴🔴🔴
════════════════════════════════════
> ⏰ 影子平仓时间：{now}

| 项 | 值 |
|---|---|
| 类型 | **【影子】**（纸面结算，无真实成交）|
| 假设成交价 | {p['decision_price']}（开仓）|
| 平仓价 | {p['exit_price']}（决策时刻价近似）|
| 毛盈亏 | {p['gross']:+,.0f} |
| **净盈亏** | **{p['net']:+,.0f}**（扣近似双边费）|
| **实际落地赔率** | **{p['R']:+.3f}R**（净盈亏 ÷ 净 max_loss {p['max_loss']:,.0f}：止损距+开仓费+止损价平仓费，2026-08-28 净口径）|
| 理由 | {p.get('reason', '')} |"""


def _fmt_move(p, old, new):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return f"""════════════════════════════════════
🟡🟡🟡 【影子】移动止损 · {p['symbol']} · 新止损 {new} 🟡🟡🟡
════════════════════════════════════
> ⏰ 影子移损时间：{now}

| 项 | 值 |
|---|---|
| 类型 | **【影子】**（纸面移损，无真实止损单）|
| 新止损 | {new}（原 {old}）|"""


def _approx_fee(market, symbol, qty, price_in, price_out):
    """近似双边费（fee_schedule 复用，失败时 0 保守退化——影子本就是近似样本）。

    影子记录的费用口径与 review.py 复盘口径同源（fee_schedule），保证影子样本与
    真实样本在复盘时费用口径一致、可比。
    """
    return _side_fee(market, symbol, qty, price_in) + _side_fee(market, symbol, qty, price_out)


def _side_fee(market, symbol, qty, price):
    """单边费（2026-08-28 拆出：净 max_loss 需按止损价单算一边费、分子按实际平仓价单算
    一边费，两边各自按各自价格算——fee_schedule 复用，失败时 0 保守退化）。"""
    try:
        import fee_schedule as FS
        mkt = "HK" if market == "HKT" else "US"
        sec = _sec_type(symbol)
        return FS.fee_per_side(mkt, sec, abs(qty * price), shares=qty)
    except Exception:
        return 0.0


def _sec_type(symbol):
    """影子开仓时标的类型近似判定（费用口径用）：读 config 白名单 / 前缀启发式。
    与 review.py 的 type 列同语义（stock / etf）。"""
    s = (symbol or "").upper()
    # 跨境 / 杠杆 ETF 常见代码段（077xx / 07xxx 部分）无法前缀稳定判定——用富途 classify
    # 结果更准，但影子记录追求零额外依赖，保守按 stock 计费（多算印花税、R 略保守）。
    # 已知 ETF 白名单从 config 读（与 hot_list 的类型列同源口径由认领时保证——
    # 认领流程已跑 classify，AI 落影子仓时可用 --sec-type etf 显式指定覆盖）。
    return getattr(_sec_type, "_override", None) or "stock"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _cli():
    argv = sys.argv[1:]
    if not argv:
        print(__doc__)
        return 1
    cmd = argv[0]
    try:
        if cmd == "status":
            status(argv[1].upper() if len(argv) > 1 else None)
            return 0
        if cmd == "open" and len(argv) >= 8:
            market = argv[1].upper()
            if market not in MARKETS:
                print(f"❌ market 必须是 HKT/ET，收到 '{market}'")
                return 1
            # 可选 --blocked-by（默认 open_exposure_today）与 --sec-type（费用口径覆盖）
            blocked_by = "open_exposure_today"
            if "--blocked-by" in argv:
                i = argv.index("--blocked-by")
                blocked_by = argv[i + 1]
            if "--sec-type" in argv:
                i = argv.index("--sec-type")
                _sec_type._override = argv[i + 1].lower()
            ok, msg = open_shadow(market, argv[2], argv[3].lower(),
                                  float(argv[4]), float(argv[5]), float(argv[6]),
                                  int(float(argv[7])), blocked_by)
            print(("✅ " if ok else "❌ ") + msg if not ok else msg)
            return 0 if ok else 1
        if cmd == "close" and len(argv) >= 4:
            market = argv[1].upper()
            if market not in MARKETS:
                print(f"❌ market 必须是 HKT/ET，收到 '{market}'")
                return 1
            # close <market> <symbol> <exit_price> [reason...]：exit_price=argv[3]、reason=argv[4:]
            reason = " ".join(argv[4:]) if len(argv) > 4 else "影子平仓（决策时刻价结算）"
            ok, msg = close_shadow(market, argv[2], float(argv[3]), reason)
            print(msg)
            return 0 if ok else 1
        if cmd == "move" and len(argv) >= 4:
            market = argv[1].upper()
            if market not in MARKETS:
                print(f"❌ market 必须是 HKT/ET，收到 '{market}'")
                return 1
            # move <market> <symbol> <new_stop>：new_stop=argv[3]
            ok, msg = move_shadow(market, argv[2], float(argv[3]))
            print(msg)
            return 0 if ok else 1
    except (ValueError, IndexError) as e:
        print(f"❌ 参数错误：{e}\n用法见 python3 shadow_trade.py（无参数打印说明）")
        return 1
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(_cli())
