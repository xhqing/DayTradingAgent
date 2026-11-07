#!/usr/bin/env python3
"""持仓看护（外部叫醒）——2026-11-05 立，T157 主体修复。

## 为什么需要这个脚本（它要解决的那个真实事故）

2026-09-16 夜盘（ET）：22:19 开仓 SPCX → 22:31 盯盘会话的采样链断了（会话死亡 / 上下文
压缩 / 被外部终止，死因当时无日志可证）→ 22:52 券商侧止损单被自动触发成交 −0.99R，**没有
任何人知道**：会话已经不在场，没有采样、没有平仓记录、连败计数也漏记 → 直到次日 11:45
排查才发现。

问题的本质不是「止损单没挂」（挂了、并且正常触发了），而是**止损触发之后没有任何人/任何
进程去感知并记录这件事**。项目里所有「持仓状态核对 / 被动平仓检测」逻辑（
`account_status.position_status`）都挂**在采样段里**——采样由 AI 会话驱动，会话一死，
这些检查全部静默失联。这就是「无外部叫醒」的死角。

本脚本补的就是这个死角：一个**跟 AI 会话完全解耦**的外部看护进程（launchd 每 60 秒触发），
只要「交易时段 + 有持仓」，就持续核对两件事：

1. **有仓无人盯**：账户 / 记录里还有持仓，但密采样已经停更超过阈值（默认 5 分钟）——
   说明盯盘会话已经不在场，持仓处于「无人看、只能靠券商止损单兜底」的状态。
2. **有开仓无平仓**：actions 记录里还挂着持仓，但账户实持已经归零 —— 典型就是止损 / 止盈
   条件单被动成交（2026-09-16 事故形态），需要立刻补记平仓与连败、复盘口径才不失真。

外加一项**收盘收尾检查**（对应 T157 修复方向 ③）：该市场刚收盘的一小段时间窗口内，如果
「还有持仓未平」或「当日有开仓却无平仓记录」，就弹窗提醒——收盘那一刻是最后的补救窗口
（跨夜持仓的止损单不触发、跳空风险全暴露）。

## 明确不做（重要，别再回退成被删掉的旧功能）

- **不做「空转警报」**：2026-08-21 用户立规矩删掉的那条是「采样在跑、但 AI 分析心跳停更
  ≥180 秒就报警」——那是**空仓也会报**的打扰源。本脚本**只在真有持仓时**才关心采样是否
  活着，空仓日完全静默（连账户都不查）。
- **不做自动交易**：不撤单、不下单、不平仓。它只负责「叫醒用户」，处置永远由人 / AI 会话做。
- **不改任何状态文件**：不写 actions、不动连败计数、不清 intent。只读 + 通知 + 写自己的日志。

## 判据口径（每个数字都有出处）

- 交易时段：港股 09:30-12:00 / 13:00-16:00（Asia/Hong_Kong）；美股 09:30-16:00
  （America/New_York）。**一律用 zoneinfo 换算，不硬编码时差**（2026-11-05 用户立：夏令时
  切换必须自动跟上，凭记忆算时差必然出错）。
- 「有持仓」来源两条，取并集：① 当日 actions 记录（`account_status._holding_from_actions`，
  全局视角、不分会话）；② 账户实况（模拟账户 + 实盘账户，实盘未解锁查不到就跳过）。
- 「采样活着」= 该市场当日的 `tmp/monitor_log_{HK|US}_*` 系列文件里最新 mtime 距现在 ≤ 阈值
  （默认 300 秒，40 秒一段的 7 倍余量，不会因单段慢一拍就误报）。
- 通知冷却：同类事件每个标的 10 分钟（写 `state/position_watchdog.json`），防 launchd
  每分钟刷屏。

## 用法

- `python3 position_watchdog.py`            正常巡检（launchd 每 60 秒调）
- `python3 position_watchdog.py --dry-run`  只打印判定结果，不弹窗、不响铃、不写状态
- `python3 position_watchdog.py --json`     输出结构化结果（排查用）
- `--stale-seconds N` / `--close-grace-min N` 覆盖默认阈值（调试用）

退出码：0 = 无事（含盘外静默）；2 = 有告警（便于外部编排）。
"""
import argparse
import datetime
import json
import os
import subprocess
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, "..", "..", "..", ".."))
TMP_DIR = os.path.join(PROJECT_ROOT, "tmp")
STATE_DIR = os.path.join(PROJECT_ROOT, "state")
LOG_PATH = os.path.join(TMP_DIR, "position_watchdog.log")
STATE_PATH = os.path.join(STATE_DIR, "position_watchdog.json")
PANEL_BIN = os.path.join(TMP_DIR, "alert_panel")

DEFAULT_STALE_SECONDS = 300       # 采样停更阈值（40 秒一段 × 7 倍余量）
DEFAULT_CLOSE_GRACE_MIN = 20      # 收盘后收尾检查窗口（分钟）
NOTIFY_COOLDOWN_SECONDS = 600     # 同类事件每标的 10 分钟冷却
DEFAULT_RESUME_COOLDOWN = 90      # 断链自愈冷却（秒）：同市场不重复拉起，防进程堆积

HK_TZ = "Asia/Hong_Kong"
US_TZ = "America/New_York"


# ---------------------------------------------------------------------------
# 时段判定（zoneinfo 换算，不硬编码时差）
# ---------------------------------------------------------------------------
def _now(tz_name):
    from zoneinfo import ZoneInfo
    return datetime.datetime.now(ZoneInfo(tz_name))


def _in_window(now, windows):
    if now.weekday() >= 5:
        return False
    t = now.time()
    return any(a <= t < b for a, b in windows)


HK_WINDOWS = [(datetime.time(9, 30), datetime.time(12, 0)),
              (datetime.time(13, 0), datetime.time(16, 0))]
US_WINDOWS = [(datetime.time(9, 30), datetime.time(16, 0))]


def market_phase(market, close_grace_min):
    """返回 'open'（盘中）/ 'closing'（刚收盘的收尾窗口内）/ 'closed'。"""
    tz, windows = (HK_TZ, HK_WINDOWS) if market == "HK" else (US_TZ, US_WINDOWS)
    now = _now(tz)
    if _in_window(now, windows):
        return "open", now
    if now.weekday() < 5:
        for a, b in windows:
            b_dt = datetime.datetime.combine(now.date(), b, tzinfo=now.tzinfo)
            if b_dt <= now < b_dt + datetime.timedelta(minutes=close_grace_min):
                return "closing", now
    return "closed", now


# ---------------------------------------------------------------------------
# 数据来源
# ---------------------------------------------------------------------------
def recorded_holdings():
    """当日 actions 记录口径的持仓（全局、不分会话）。失败返回 {}。"""
    try:
        import account_status as A
        return A._holding_from_actions(only_session=None) or {}
    except Exception as e:
        _log(f"读取 actions 持仓失败：{e}")
        return {}


def account_holdings():
    """账户实况：返回 {'paper': {code: qty}, 'live': {code: qty}}（查不到的账户缺键）。

    实盘账户在没有解锁文件时会查询失败（老虎连接层物理拒），属于默认态——安静跳过，
    不当作错误（这也是「实盘不可达」这条护栏的预期表现）。
    """
    out = {}
    try:
        import account_status as A
    except Exception as e:
        _log(f"导入 account_status 失败：{e}")
        return out
    for acct, tag in ((None, "paper"), ("live", "live")):
        try:
            positions, _stops, _tag, _err = A._query_account(acct)
        except Exception:
            continue
        if positions is None:
            continue   # 查询失败（含实盘未解锁）→ 跳过，不当空仓
        pos = {}
        for p in positions:
            code = A._pos_symbol(p)
            if not code:
                continue
            try:
                qty = int(getattr(p, "quantity", 0) or 0)
            except (TypeError, ValueError):
                qty = 0
            if qty:
                pos[code] = pos.get(code, 0) + qty
        out[tag] = pos
    return out


def latest_sample_age(market):
    """该市场采样日志距现在多少秒（无任何日志返回 None）。

    只看文件名前缀 `monitor_log_{HK|US}_`，不按日期筛选——取最新一个文件的 mtime 就是
    「最后一次采样的时刻」；当日完全没采样时，前一天文件的旧 mtime 会让 age 很大，
    这正是「有仓但根本没在盯」该有的判定。
    """
    prefix = f"monitor_log_{market}_"
    newest = None
    try:
        for name in os.listdir(TMP_DIR):
            if not name.startswith(prefix):
                continue
            try:
                mt = os.path.getmtime(os.path.join(TMP_DIR, name))
            except OSError:
                continue
            newest = mt if newest is None else max(newest, mt)
    except OSError:
        return None
    if newest is None:
        return None
    return max(0, int(datetime.datetime.now().timestamp() - newest))


# ---------------------------------------------------------------------------
# 告警去重 / 通知 / 日志
# ---------------------------------------------------------------------------
def load_state():
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(STATE_PATH, "w") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
            f.write("\n")
    except OSError:
        pass


def _cooldown_left(state, key):
    import time
    last = (state.get("last_notify") or {}).get(key) or 0
    return max(0, NOTIFY_COOLDOWN_SECONDS - (time.time() - last))


def _mark(state, key):
    import time
    state.setdefault("last_notify", {})[key] = time.time()


def _log(line):
    try:
        os.makedirs(TMP_DIR, exist_ok=True)
        with open(LOG_PATH, "a") as f:
            f.write(datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S") + "\t" + line + "\n")
    except OSError:
        pass


def _resume_cooldown_left(state, key, cooldown=None):
    import time
    cd = DEFAULT_RESUME_COOLDOWN if cooldown is None else cooldown
    last = (state.get("last_resume") or {}).get(key) or 0
    return max(0, cd - (time.time() - last))


def _mark_resume(state, key):
    import time
    state.setdefault("last_resume", {})[key] = time.time()


def _resume_sampling(market, symbols, state, dry_run=False):
    """采样断链自愈（T145，2026-11-07 立）：盘中有持仓且采样停更时，主动拉起一段采样。

    为什么需要：持仓看护（T157）只能**喊人**；若盯盘会话因上下文压缩 / 死亡而掉线、
    又没人看到弹窗，采样就一直断着——2026-09-11 正是这样错过港股午休平仓边界
    （500 股 HK.00700 持仓跨进午休，12:00-13:00 不可交易、止损单也不触发）。
    本条让守护进程不只是喊，还**顺手把数据采集接上**：拉一段 40 秒的
    `monitor_segment.py`（下一次 watchdog 60 秒后再续），把「数据断流」从无限长压到
    最长 ~20 秒空档。

    三条边界（勿改）：
      ① **只在「盘中有持仓」时触发**（由 `evaluate()` 的 unattended 告警驱动）——
         2026-08-21 用户立规删掉的「空仓也报」空转警报不得回归；
      ② **不重启 AI 会话、不改任何交易状态**，只补数据采集（AI 醒来后照样读 log 分析）；
      ③ 同一市场 `DEFAULT_RESUME_COOLDOWN` 秒内不重复拉起（避免 watchdog 每 60 秒
         堆积多个进程）。
    返回是否真的拉起了一段。
    """
    if not symbols:
        return False
    key = f"resume_{market}"
    left = _resume_cooldown_left(state, key)
    if left > 0:
        _log(f"[{market}] 断链自愈冷却中（{int(left)}s），跳过")
        return False
    script = os.path.join(SCRIPT_DIR, "monitor_segment.py")
    if not os.path.exists(script):
        _log(f"[{market}] 断链自愈失败：找不到 {script}")
        return False
    cmd = [sys.executable, "-u", script, ",".join(sorted(symbols)), "40", "10", "--mode", "auto"]
    if dry_run:
        _log(f"[{market}] 断链自愈（dry-run）：{' '.join(cmd)}")
        _mark_resume(state, key)   # dry-run 也记冷却，保证行为与真跑一致（否则测试/预演会连拉）
        return True
    try:
        os.makedirs(TMP_DIR, exist_ok=True)
        logf = open(os.path.join(TMP_DIR, f"watchdog_resume_{market.lower()}.log"), "a")
        subprocess.Popen(cmd, cwd=PROJECT_ROOT, stdout=logf, stderr=subprocess.STDOUT,
                         start_new_session=True, close_fds=True)
        _mark_resume(state, key)
        _log(f"[{market}] 断链自愈：已拉起一段采样 {' '.join(sorted(symbols))}")
        return True
    except Exception as e:
        _log(f"[{market}] 断链自愈失败：{e}")
        return False


def notify(title, message, dry_run=False):
    """响铃 + 弹窗（面板优先、回退 osascript；与 monitor_watcher 同载体）。"""
    if dry_run:
        print(f"  [dry-run] 将弹窗：{title}｜{message}")
        return
    try:
        subprocess.Popen(["afplay", "/System/Library/Sounds/Basso.aiff"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass
    try:
        if os.path.isfile(PANEL_BIN) and os.access(PANEL_BIN, os.X_OK):
            pending = os.path.join(TMP_DIR, "position_watchdog_pending.json")
            result = os.path.join(TMP_DIR, "position_watchdog_result.json")
            os.makedirs(TMP_DIR, exist_ok=True)
            with open(pending, "w") as f:
                json.dump({"title": title, "message": message,
                           "timeout_secs": 90}, f, ensure_ascii=False)
            # 双背景用户：「持仓无人看」是直接亏钱面，面板常驻到点击 / 90 秒超时
            subprocess.Popen([PANEL_BIN, pending, result],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
            return
    except Exception:
        pass
    try:
        esc = message.replace("\\", "\\\\").replace('"', '\\"')
        subprocess.run(["osascript", "-e",
                        f'display dialog "{esc}" with title "{title}" '
                        f'buttons {{"知道了"}} default button "知道了" '
                        f'giving up after 120 with icon caution'],
                       timeout=130, capture_output=True)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 主判定
# ---------------------------------------------------------------------------
def global_reconcile():
    """全局对账（不看时段）：返回 (lines, data)。

    用途（T157 修复方向 ②）：断层恢复、停盯收尾这些「需要一次性看清全局」的场景——
    以前只能看本会话自己开的仓（`account_status.position_status` 按会话过滤），
    会话一旦断层 / 重开，就没人有全局视角了。

    输出四项对照：① 当日 actions 记录的全局持仓（不限会话）；② 模拟账户实持；
    ③ 实盘账户实持（未解锁查不到时明说，不当空仓）；④ 记录有仓但账户全空 = 疑似被动成交。
    """
    recorded = recorded_holdings()
    acct = account_holdings()
    lines = []
    lines.append(f"📒 记录持仓（当日 actions 全局）："
                 + (", ".join(f"{s} {v['qty']}股({v.get('dir', '?')})"
                               for s, v in recorded.items()) or "无"))
    if not acct:
        lines.append("⚠️ 账户实况：查不到（代理 / 网络 / 未解锁）——先排除查询链路故障、"
                     "不要当成空仓（空仓误判会导致漏平仓 / 漏记复盘）")
    else:
        for tag, pos in acct.items():
            label = "实盘" if tag == "live" else "默认（模拟）"
            lines.append(f"🏦 账户实持（{label}）："
                         + (", ".join(f"{k} {v}股" for k, v in pos.items()) or "空仓"))
        if "live" not in acct:
            lines.append("ℹ️ 实盘账户未查到（无解锁文件 / 连接被拒属默认态）——实盘有仓与否需人工核对")
    missing_marks = []
    for s in recorded:
        code = s.split(".")[-1]
        if not any(pos.get(code) for pos in acct.values()):
            missing_marks.append(s)
    if acct and missing_marks:
        lines.append(f"🚨 记录里有、账户里没有：{', '.join(missing_marks)}"
                     f"—— 疑似止损/止盈条件单被动成交（无人下单平仓）。先查当日订单确认成交价，"
                     f"再补写平仓记录 + update_losing_streak，复盘按真实成交价算 R。")
    extra_marks = []
    for tag, pos in acct.items():
        for code, qty in pos.items():
            if not any(s.split(".")[-1] == code for s in recorded):
                extra_marks.append(f"{code} {qty}股（{tag}）")
    if extra_marks:
        lines.append(f"⚠️ 账户有仓但 actions 无记录：{', '.join(extra_marks)}"
                     f"—— 用户手动开仓 / 记录漏写？按单持仓护栏判断如何处理。")
    return lines, {"recorded": recorded, "account": acct}


def evaluate(stale_seconds, close_grace_min):
    """返回 (alerts, info)。alerts 每项 = {key, title, message, market}。"""
    alerts = []
    info = {"markets": {}}
    recorded = recorded_holdings()
    acct = None   # 懒查：只有真有开仓记录才查账户（空仓日零 API 调用）

    def _market_symbols(market):
        return {s: v for s, v in recorded.items() if s.upper().startswith(market + ".")}

    for market in ("HK", "US"):
        phase, now = market_phase(market, close_grace_min)
        syms = _market_symbols(market)
        m_info = {"phase": phase, "recorded": {s: v["qty"] for s, v in syms.items()},
                  "sample_age": None, "account": {}}
        info["markets"][market] = m_info
        if phase == "closed":
            continue

        age = latest_sample_age(market)
        m_info["sample_age"] = age

        if recorded:
            if acct is None:
                acct = account_holdings()
            held = {}
            for tag, pos in acct.items():
                for s, v in syms.items():
                    code = s.split(".")[-1]
                    if pos.get(code):
                        held.setdefault(tag, {})[s] = pos[code]
            m_info["account"] = held
            still_held = bool(held) or not acct   # 账户查不到时按「可能还有仓」处理（保守）

            # ① 有仓无人盯（盘中才判定；收尾窗口内已无需再采样）
            if phase == "open" and still_held and age is not None and age > stale_seconds:
                alerts.append({
                    "key": f"unattended_{market}",
                    "market": market,
                    "title": "DayTradingAgent · 持仓无人盯",
                    "message": (
                        f"⚠️ {market} 盘中有持仓，但密采样已停更 {age // 60} 分 {age % 60} 秒"
                        f"（阈值 {stale_seconds} 秒）。\n\n"
                        f"记录持仓：{', '.join(f'{s} {v}股' for s, v in m_info['recorded'].items())}\n"
                        f"账户实持：{held or '未查到'}\n\n"
                        f"这通常意味着盯盘会话已不在场（死亡 / 被关 / 上下文中断）——"
                        f"现在只剩券商侧止损单兜底。请立刻检查会话是否还在跑，没跑就重开并重新采样；"
                        f"若不想继续盯，直接按记录平仓离场。")})

            # ② 有开仓无平仓：账户实持归零（被动成交），盘中 / 收尾窗口都要报
            if acct:
                zeroed = [s for s in syms
                          if not any(pos.get(s.split(".")[-1]) for pos in acct.values())]
                if zeroed:
                    alerts.append({
                        "key": f"passive_close_{market}",
                        "market": market,
                        "title": "DayTradingAgent · 疑似被动平仓",
                        "message": (
                            f"🔔 {market} 记录里还有持仓，但账户实持已归零：{', '.join(zeroed)}\n\n"
                            f"这通常是止损 / 止盈条件单被动成交（无人下单平仓）。请立即：\n"
                            f"1) 查当日订单确认成交价与类型；\n"
                            f"2) 补写平仓记录（actions），跑 update_losing_streak 记连败；\n"
                            f"3) 复盘时按真实成交价算 R（不要漏记这笔）。")})

        # ③ 收盘收尾检查：有开仓但账户仍有持仓（跨夜敞口）/ 记录未闭环
        if phase == "closing" and recorded:
            if acct is None:
                acct = account_holdings()
            still = {}
            if acct:
                for tag, pos in acct.items():
                    for s, v in syms.items():
                        if pos.get(s.split(".")[-1]):
                            still.setdefault(tag, {})[s] = pos.get(s.split(".")[-1])
            if still or not acct:
                alerts.append({
                    "key": f"close_exposure_{market}",
                    "market": market,
                    "title": "DayTradingAgent · 收盘仍有持仓",
                    "message": (
                        f"🚨 {market} 已收盘，但账户仍有持仓未平："
                        f"{still or '（账户未查到，按记录判断仍有仓）'}\n\n"
                        f"记录持仓：{', '.join(f'{s} {v}股' for s, v in m_info['recorded'].items())}\n\n"
                        f"跨夜持仓的止损单不触发、跳空风险全暴露。请确认：这是有意保留的，"
                        f"还是收盘前漏平了？若漏平，次日开盘第一时间处置，并补记停盯收尾说明。")})
    return alerts, info


def main(argv=None):
    ap = argparse.ArgumentParser(description="持仓看护（T157）")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--stale-seconds", type=int, default=DEFAULT_STALE_SECONDS)
    ap.add_argument("--close-grace-min", type=int, default=DEFAULT_CLOSE_GRACE_MIN)
    ap.add_argument("--reconcile", action="store_true",
                    help="只跑全局对账（记录持仓 vs 账户实持），不看时段、不弹窗")
    args = ap.parse_args(argv)

    if args.reconcile:
        lines, data = global_reconcile()
        if args.json:
            print(json.dumps(data, ensure_ascii=False, indent=2))
        else:
            for ln in lines:
                print(ln)
        bad = any(ln.startswith("🚨") for ln in lines)
        return 2 if bad else 0

    alerts, info = evaluate(args.stale_seconds, args.close_grace_min)
    state = load_state()
    shown = []
    for a in alerts:
        left = _cooldown_left(state, a["key"])
        if left > 0 and not args.dry_run:
            continue
        shown.append(a)
        # T145 断链自愈（2026-11-07）：有仓无人盯时，除喊人外顺手把采样接上（只补数据、
        # 不重启会话、不碰交易状态；同一市场 90 秒冷却防堆积）。
        resumed = False
        msg = a["message"]
        if a["key"].startswith("unattended_"):
            mk = a["market"]
            syms = list(((info.get("markets") or {}).get(mk) or {}).get("recorded") or {})
            resumed = _resume_sampling(mk, syms, state, dry_run=args.dry_run)
            if resumed:
                msg = msg + ("\n\n🔄 守护已自动拉起一段采样（40 秒）把数据接上；"
                             "但**盯盘会话本身仍需人工检查 / 重开**——自愈只补数据采集，不恢复 AI 分析。")
        notify(a["title"], msg, dry_run=args.dry_run)
        if not args.dry_run:
            _mark(state, a["key"])
        _log(f"[{a['market']}] {a['title']}：{a['message'].splitlines()[0]}")
    if not args.dry_run and shown:
        save_state(state)
    if args.json:
        print(json.dumps({"info": info, "alerts": shown}, ensure_ascii=False, indent=2))
    else:
        phases = ", ".join(f"{m}={v['phase']}" for m, v in info["markets"].items())
        ages = ", ".join(f"{m}={v['sample_age']}s" for m, v in info["markets"].items()
                         if v["sample_age"] is not None)
        if not args.dry_run:
            print(f"🐕 持仓看护：{phases}｜采样停更 {ages or '无采样日志'}｜告警 {len(shown)} 条")
        for a in shown:
            print(f"  ⚠️ {a['title']}")
    return 2 if shown else 0


if __name__ == "__main__":
    sys.exit(main())
