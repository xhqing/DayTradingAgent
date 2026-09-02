#!/usr/bin/env python3
"""交易互斥公共模块（多会话并行盯盘·方案 A，2026-08-17 立，港美共用）。

设计出处：PLAN-parallel-monitor-HK.md 第三节（美股 Plan 复用本文档、不重复设计）。
目的：多会话各盯各的标的池并行盯盘时，保证**全局任意时刻至多一笔在场**（单持仓互斥，
风控语义不变、仍是单笔口径）。三层防线 + 第四层监控兜底：

  第一层 flock 文件锁：把「查账户 → 下单 → 确认成交」整个临界区在内核级序列化，
            两会话进程在同一台 Mac 上排队等锁，check-then-act 竞态从根上消除；
  第二层 开仓闸门（锁内执行）：三口径任一命中即拒开——
            ① intent 日志有未决意图（崩溃残留的在途订单）；
            ② 当日订单流有「开仓方向成交且无对应平仓」（与成交确认同数据源，
              规避持仓接口传播延迟窗口）；
            ③ 有任一活动开仓方向挂单（坑已被占）；
  第三层 intent 日志：提交下单请求前写 pending、拿到券商确认后更新终态——进程崩在
            「已提交未确认」之间时 pending 永久残留，后到会话被口径 ① 拒开；
  第四层（不在本模块）：monitor_segment.py 每轮采样加持仓数检测兜底，白名单外
            持仓 ≥2 或与当日开仓链矛盾 → 段输出告警 + AI 处置。

**全局一把锁、不分市场**——分市场两把锁会放过「港股持仓违规过夜 + 美股会话开新仓」的
跨市场叠加路径；港美时段不重叠使全局锁的实际代价为零。

常驻历史持仓处理：账户有 2026-08-03 之前的长持历史持仓（如 02800 多 / 07709 空，保留不动）。
闸门**不用**「账户已有任一持仓即拒开」的裸口径（会永远拒开），只看本系统当日的开仓意图链，
历史持仓不进链、不拦截。白名单记录在 skill config.json 的 risk.resident_positions。

用法（开仓脚本入口）：
    with TradeMutex(market="HK", symbol=symbol, side=side_str, qty=quantity) as m:
        m.check_gate(...)      # 三口径闸门，任一命中抛 GateBlocked（或用 blocked() 拿结构化原因）
        ... 下单流程（intent 自动在 submit 前写 pending、确认后写终态）...

命令行小工具：
    python3 trade_mutex.py status                     # 看互斥状态（intent pending、当日链）
    python3 trade_mutex.py --clear-intent <行号>       # 人工确认无在途单后清掉残留 intent
"""

import fcntl
import json
import os
import sys
import time
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, "..", "..", "..", ".."))
TMP_DIR = os.path.join(PROJECT_ROOT, "tmp")
LOCK_FILE = os.path.join(TMP_DIR, "trade_mutex.lock")
INTENT_FILE = os.path.join(TMP_DIR, "trade_intent.log")
CONFIG_PATH = os.path.join(SCRIPT_DIR, "..", "config.json")


class GateBlocked(Exception):
    """开仓闸门拦截：任一口径命中。args[0] 是结构化原因 dict（blocked_by + detail）。"""


def _read_config():
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def resident_positions():
    """读 config 的常驻持仓白名单（risk.resident_positions），返回 ['02800', ...]。

    常驻历史持仓不进开仓意图链、不拦截闸门；第四层监控检测用它区分「历史长持」与
    「本系统当日新开」。增删常驻持仓时改 config.json。
    """
    cfg = _read_config()
    rp = cfg.get("risk", {}).get("resident_positions", [])
    return [str(x) for x in rp]


# ---------------------------------------------------------------------------
# intent 日志（第三层：防崩溃窗口的写前日志 WAL）
# ---------------------------------------------------------------------------

def _read_intents():
    """读全部 intent 行，返回 [(行号 1-based, dict), ...]；文件不存在返回 []。"""
    if not os.path.exists(INTENT_FILE):
        return []
    out = []
    with open(INTENT_FILE) as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                out.append((i, json.loads(line)))
            except Exception:
                continue   # 坏行跳过（不因一条坏行废掉整个闸门）
    return out


def pending_intents():
    """当前 status=pending 的 intent 行（崩溃残留的在途意图），返回 [(行号, dict), ...]。"""
    return [(ln, d) for ln, d in _read_intents() if d.get("status") == "pending"]


def append_intent(session_id, market, symbol, side, qty, account=None):
    """提交下单请求前追加一条 pending intent（WAL）。返回行号。

    必须在发请求**之前**写——进程若崩在「已提交未确认」之间，该行停在 pending，
    后到会话闸门口径 ① 命中拒开。

    account（2026-09-02 立，T136）：本笔下单的账户口径（'live' / None=默认模拟账户）。
    写进 intent 记录后，account_status / monitor_segment 等查询方能按「这笔开仓用的是
    哪个账户」精确选账户查持仓与订单——此前查询方只能靠当日 actions 的「| 账户 | 实盘」
    行粗判，记录漏写账户行就错查模拟账户（2026-09-02 实盘首单 00100 后持仓状态行
    连续误报「账户已无持仓」）。"""
    os.makedirs(TMP_DIR, exist_ok=True)
    rec = {
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "session_id": session_id or "unknown",
        "market": market,
        "symbol": symbol,
        "side": side,
        "qty": qty,
        "account": account,
        "status": "pending",
    }
    with open(INTENT_FILE, "a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return sum(1 for _ in open(INTENT_FILE))   # 行号（刚追加的是最后一行）


def settle_intent(line_no, status, order_id=None, extra=None):
    """把指定行 intent 更新为终态（filled / rejected / cancelled）。

    重写整个文件（intent 文件行数很少、不是热路径）。找不到行号时静默跳过
    （崩溃后行号漂移的极端情形，闸门口径 ① 会兜住残留 pending）。
    """
    rows = []
    if os.path.exists(INTENT_FILE):
        with open(INTENT_FILE) as f:
            rows = [l.rstrip("\n") for l in f if l.strip()]
    if not (1 <= line_no <= len(rows)):
        return
    try:
        rec = json.loads(rows[line_no - 1])
    except Exception:
        return
    rec["status"] = status
    rec["settled_ts"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if order_id is not None:
        rec["order_id"] = order_id
    if extra:
        rec.update(extra)
    rows[line_no - 1] = json.dumps(rec, ensure_ascii=False)
    tmp = INTENT_FILE + ".tmp"
    with open(tmp, "w") as f:
        f.write("\n".join(rows) + "\n")
    os.replace(tmp, INTENT_FILE)


def clear_intent(line_no):
    """人工清掉残留 pending intent（确认无在途单后）。返回是否清到。"""
    pend = pending_intents()
    if not any(ln == line_no for ln, _ in pend):
        return False
    rows = []
    if os.path.exists(INTENT_FILE):
        with open(INTENT_FILE) as f:
            rows = [l.rstrip("\n") for l in f if l.strip()]
    if not (1 <= line_no <= len(rows)):
        return False
    try:
        rec = json.loads(rows[line_no - 1])
    except Exception:
        return False
    rec["status"] = "cleared"
    rec["cleared_ts"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rows[line_no - 1] = json.dumps(rec, ensure_ascii=False)
    tmp = INTENT_FILE + ".tmp"
    with open(tmp, "w") as f:
        f.write("\n".join(rows) + "\n")
    os.replace(tmp, INTENT_FILE)
    return True


# ---------------------------------------------------------------------------
# 当日开仓链（第二层口径 ②③：查订单流而非持仓接口——与成交确认同数据源）
# ---------------------------------------------------------------------------

def _order_status(o):
    raw = getattr(o, "status", "")
    return raw.value if hasattr(raw, "value") else str(raw)


def _order_symbol(o):
    contract = getattr(o, "contract", None)
    return str(getattr(contract, "symbol", "") if contract else "")


def _is_stop_order(o):
    """止损类单：STP / STOP / TRAIL / LOSS 附加腿（持仓保护、不是开仓单）。"""
    raw_otype = getattr(o, "order_type", "")
    otype = (raw_otype.value if hasattr(raw_otype, "value") else str(raw_otype)) or ""
    legs = getattr(o, "order_legs", None) or []
    return (str(otype).upper() in ("STP", "STOP", "TRAIL")
            or any(str(getattr(leg, "leg_type", "")).upper() == "LOSS" for leg in legs))


def _order_local_date(o):
    """订单 order_time（毫秒时间戳）→ 本地日期字符串（'YYYY-MM-DD'）。无时间戳返回 ''。"""
    tm = getattr(o, "order_time", None)
    try:
        return datetime.fromtimestamp(int(tm) / 1000).strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        return ""


def _today_orders(orders):
    """从 get_orders() 返回里只留「今日（本地日期）」订单（2026-08-20 立）。

    为什么：老虎 get_orders() 不带过滤时返回近两周订单，而开仓闸门口径②③设计上都
    只应看**当日**订单流（「当日已有在场敞口」）。2026-08-20 实录：get_orders() 返回
    2026-08-05 港股 00981 的历史开平链（当天反复开平、净计数 +1 残留）与 07709 的
    8-03 历史买单，被 today_open_exposure 误判为「今日在场敞口」、把合法的美股开仓
    拦下（blocked_by=open_exposure_today）——函数名叫 today 而实际算的是两周，口径
    错位。修法：闸门调用前统一过滤本地今日订单。本地时区（北京）下港股当日订单与
    美股当日订单（跨夜到北京次日凌晨）均按各自实际下单日过滤，正确归组。
    """
    today = datetime.now().strftime("%Y-%m-%d")
    return [o for o in (orders or []) if _order_local_date(o) == today]


def today_open_exposure(orders):
    """从当日订单流算「开仓方向成交且无对应平仓」的在场笔数（第二层口径 ② 的核心）。

    口径：对每个标的，开仓方向成交笔数 − 平仓方向成交笔数（含止损单触发成交与主动
    平仓单成交），>0 即该标的有在场敞口。历史常驻持仓不经订单流（买入在 2026-08-03 前）、
    自然不在链上，不拦截——这正是用订单流而非持仓裸口径的原因。
    orders 传老虎 get_orders() 的当日订单对象列表。返回在场标的集合 {'02800', ...}。
    """
    net = {}
    for o in orders or []:
        sym = _order_symbol(o)
        if not sym:
            continue
        status = _order_status(o)
        if "Filled" not in status:
            continue   # 只看已成交（PartiallyFilled 也算建仓，含 Filled 子串会被计入）
        if _is_stop_order(o):
            # 止损单成交 = 平掉一笔（触发平仓）；方向与 action 相反记账
            action = str(getattr(o, "action", "")).upper()
            # STP 触发成交：BUY 止损平空仓（减空头敞口）、SELL 止损平多仓（减多头敞口）。
            # 净口口径上统一记 −1（该标的的一笔在场敞口被止损单了结）。
            net[sym] = net.get(sym, 0) - 1
            continue
        action = str(getattr(o, "action", "")).upper()
        if action == "BUY":
            net[sym] = net.get(sym, 0) + 1
        elif action == "SELL":
            net[sym] = net.get(sym, 0) - 1
    return {s for s, n in net.items() if n > 0}


def active_open_orders(orders):
    """当日有活动（非终结）的开仓方向挂单（第二层口径 ③：坑已被占，成交只是时间问题）。

    排除止损类单（STP/STOP/TRAIL/LOSS——持仓保护不是开仓）与已终结状态
    （Filled/Cancelled/Inactive/Invalid/Expired/PendingCancel）。返回 [(symbol, order_id), ...]。
    """
    out = []
    for o in orders or []:
        status = _order_status(o)
        if any(s in status for s in ("Filled", "Cancelled", "Inactive", "Invalid",
                                     "Expired", "PendingCancel")):
            continue
        if _is_stop_order(o):
            continue
        out.append((_order_symbol(o), getattr(o, "id", None) or getattr(o, "order_id", None)))
    return out


# ---------------------------------------------------------------------------
# TradeMutex：第一层锁 + 第二层闸门的组合入口
# ---------------------------------------------------------------------------

class TradeMutex:
    """flock 全局锁（阻塞等待）+ 锁内三口径开仓闸门 + intent 生命周期。

    with TradeMutex(market, symbol, side, qty) as m:
        m.blocked(orders)     # 返回 None（放行）或 {'blocked_by': ..., 'detail': ...}（拒开）
        m.line_no             # 闸门放行后本笔的 intent 行号（submit 前已写 pending）
        m.settle('filled', order_id)   # 拿到券商确认后写终态
    """

    def __init__(self, market, symbol, side, qty, session_id=None, account=None):
        self.market = market
        self.symbol = symbol
        self.side = side
        self.qty = qty
        self.session_id = session_id or os.environ.get("CLAUDE_CODE_SESSION_ID", "")
        self.account = account   # 'live' / None（默认模拟账户）——写入 intent 供查询方按账户查（T136）
        self.line_no = None
        self._fd = None

    def __enter__(self):
        os.makedirs(TMP_DIR, exist_ok=True)
        self._fd = open(LOCK_FILE, "a")
        # 阻塞等锁：开仓机会不因对方在下单而丢弃——等对方下完、闸门判定后再决定。
        fcntl.flock(self._fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            # 异常退出（闸门拒开后的 raise / 崩溃）时 intent 若已写 pending 未结算，
            # 保留 pending 让后到会话拒开（宁可误拦、不可漏拦）；正常路径已 settle。
            if self._fd is not None:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
                self._fd.close()
        finally:
            self._fd = None
        return False

    def blocked(self, orders):
        """三口径闸门（在锁内调用）。放行返回 None 并写本笔 pending intent；拒开返回
        结构化原因 dict（含 blocked_by 字段，AI 据此继续盯盘不重试）。

        orders：当日订单对象列表（get_orders 结果，与成交确认同数据源）。传 None 时
        跳过口径 ②③（查单失败场景——保守起见此时只过口径 ①，由调用方决定是否放行；
        常规调用方应先查单、查不到就拒在更早处）。
        """
        # 口径 ①：intent 有未决 pending（崩溃残留）
        pend = pending_intents()
        if pend:
            ln, d = pend[-1]
            return {"blocked_by": "pending_intent",
                    "detail": f"发现未决 intent（第 {ln} 行 {d.get('ts')} {d.get('market')}:"
                              f"{d.get('symbol')} {d.get('side')} {d.get('qty')}），可能有崩溃残留的在途订单——"
                              f"先查当日订单确认是否有在途成交；确认无在途单后 "
                              f"`python3 {os.path.basename(__file__)} --clear-intent {ln}` 清掉再开"}
        if orders is None:
            return None   # 查单失败的保守路径由调用方把关
        # 只看本地今日订单（2026-08-20 修：get_orders() 返回近两周，历史残留链会被误判为今日敞口）
        orders = _today_orders(orders)
        # 口径 ②：当日订单流有开仓成交且无对应平仓（在场敞口）
        exposure = today_open_exposure(orders)
        if exposure:
            return {"blocked_by": "open_exposure_today",
                    "detail": f"当日已有在场敞口 {sorted(exposure)}（开仓成交且无对应平仓）——"
                              f"单持仓硬规矩：先到先得，本会话继续盯、等敞口了结后再开"}
        # 口径 ③：有活动开仓方向挂单（非终结 LMT 挂单，坑已被占）
        actives = active_open_orders(orders)
        if actives:
            return {"blocked_by": "active_open_order",
                    "detail": f"已有活动开仓方向挂单 {actives}——坑已被占（成交只是时间问题），拒开"}
        # 三口径全过：写本笔 pending intent（submit 前的 WAL），放行
        self.line_no = append_intent(self.session_id, self.market, self.symbol,
                                     self.side, self.qty, account=self.account)
        return None

    def settle(self, status, order_id=None, extra=None):
        """拿到券商确认（filled / rejected / cancelled）后更新 intent 终态。"""
        if self.line_no is not None:
            settle_intent(self.line_no, status, order_id=order_id, extra=extra)


# ---------------------------------------------------------------------------
# 命令行小工具
# ---------------------------------------------------------------------------

def _cli():
    if len(sys.argv) >= 2 and sys.argv[1] == "status":
        print(f"🔒 互斥锁文件：{LOCK_FILE}")
        pend = pending_intents()
        if pend:
            print(f"⚠️ 有 {len(pend)} 条未决 intent（pending）——后到开仓会被拒：")
            for ln, d in pend:
                print(f"   第 {ln} 行：{json.dumps(d, ensure_ascii=False)}")
            print("   人工确认无在途单后：python3 trade_mutex.py --clear-intent <行号>")
        else:
            print("✅ 无未决 intent（intent 链干净，开仓闸门口径 ① 放行）")
        rp = resident_positions()
        print(f"📌 常驻持仓白名单（不进开仓链、不拦截）：{rp if rp else '（空）'}")
        return 0
    if len(sys.argv) >= 3 and sys.argv[1] == "--clear-intent":
        ln = int(sys.argv[2])
        if clear_intent(ln):
            print(f"✅ 第 {ln} 行 intent 已清（cleared）")
            return 0
        print(f"❌ 第 {ln} 行不是 pending intent（或行号不存在），未清")
        return 1
    print("用法：python3 trade_mutex.py status | python3 trade_mutex.py --clear-intent <行号>",
          file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(_cli())
