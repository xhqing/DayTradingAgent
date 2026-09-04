#!/usr/bin/env python3
"""盯盘恢复协议（Resume Protocol）——AI 重新获得执行权时的第一动作。

为什么需要：盯盘要求连续（盘面秒变、信号时效以秒计），但执行环境会断层——断网、用户主动暂停
再重启、工具故障、会话压缩、长任务占用，任一都可能让 AI 失去时间感知 + 数据过时 + 上下文丢失。
最危险的是用过时数据发"看起来成立"的信号（2026-07-28 中芯事故根因：13:45 的 snapshot 数据 →
15:34 才响铃发信号，参考价过时 1h49m，响铃实测价也因取价动作被挡而错过）。本脚本把"恢复检查点"
从软规则变成跑一次就行的硬动作：强制 date + 判市场 + 检测时间断层 + 读持久化状态重建上下文 +
刷新现价，绝不用断层前的旧数据。

核心原则：AI 的内存（对话上下文）不可信，文件才是地面真相。状态从文件重建——
  signals/YYYY-MM-DD-HKT-signals.md / -ET-signals.md  当天信号（含假设持仓的开仓/加仓批次）
  signals/equity-log.csv 末行                          当前 equity（算单笔预算 B）
  signals/ring-log.csv 末行                            最后一次响铃（判断有无响铃未取实测价的悬空项）
  tmp/monitor_log_*_{mode}.csv 尾部                      上次最后采样时间（按 mode 分；时间断层的断点）

用法：
  python3 resume.py                    基础恢复：时间 + 市场状态 + 断层检测 + equity + 今日信号摘要
  python3 resume.py HK.00981,HK.09988  额外 snapshot 刷新指定标的现价（验证持仓 / 刷新参考价）

跑完 AI 据「恢复结论」决定下一步：在盘中 → 可继续盯 / 发信号（发信号前再过 snapshot 硬前置）；
盘外 → 不发信号；有断层 → 先读今日 signals 重建持仓认知、处理悬空项、刷新现价后再继续。
"""
import csv
import os
import sys
import glob
import json
import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, "..", "..", "..", ".."))
SIGNALS_DIR = os.path.join(PROJECT_ROOT, "signals")
TMP_DIR = os.path.join(PROJECT_ROOT, "tmp")

sys.path.insert(0, SCRIPT_DIR)
from trade_utils_tiger import parse_mode
MODE = parse_mode()  # 运行时 log 按 mode 分文件（signal/auto 两会话并行盯盘不互相污染）；ring-log 仅 signal 读

# 账户选择（2026-08-20 立，实盘盯盘配套）：--account live 切实盘账户取净值——
# 2026-08-20 事故：实盘盯盘时 resume 固定走默认模拟账户（约 789 万 HKD）算 B、
# 实盘净值远小于模拟净值（量级见本机实查）、偏差 68 倍。修复 = 取净值与下单同账户（load_equity 透传）。
ACCOUNT = None
for _i, _a in enumerate(sys.argv[1:]):
    if _a == '--account' and _i + 1 < len(sys.argv[1:]):
        _cand = sys.argv[1:][_i + 1].lower()
        ACCOUNT = _cand if _cand in ('live', 'paper') else None

# 时间断层警告阈值（分钟）：正常段间循环 < 1 分钟，超过这个值基本可断定断网/暂停/故障致断层。
GAP_WARN_MIN = 5

now = datetime.datetime.now()
hhmm = now.hour * 60 + now.minute
wd = now.weekday()  # 0=Mon..6=Sun
print(f"⏰ 现在 {now.strftime('%Y-%m-%d %H:%M:%S %A')}")


# ---------- 市场状态（与 preflight.py 同口径）----------
def hk_status():
    if wd >= 5:
        return "周末休市"
    if 570 <= hhmm < 720:
        return "港股早市 09:30-12:00·盘中可发信号"
    if 780 <= hhmm < 960:
        return "港股午市 13:00-16:00·盘中可发信号"
    if 720 <= hhmm < 780:
        return "港股午休 12:00-13:00·不可交易"
    return "港股盘外·不发信号"


def us_status():
    try:
        from zoneinfo import ZoneInfo
        us_now = now.astimezone(ZoneInfo("America/New_York"))
    except Exception:
        if wd >= 5:
            return "周末休市"
        if 960 <= hhmm < 1440 or hhmm < 240:
            return "美股可交易(夏令时估·zoneinfo不可用)·盘前+盘中(美东04:00-16:00)"
        return "美股盘外(夏令时估·zoneinfo不可用)·不发信号"
    us_wd = us_now.weekday()
    us_hhmm = us_now.hour * 60 + us_now.minute
    tag = "EDT夏令时" if bool(us_now.dst()) else "EST冬令时"
    if us_wd >= 5:
        return "周末休市"
    if 240 <= us_hhmm < 960:
        phase = "盘前" if us_hhmm < 570 else "盘中"
        return f"美股可交易({phase}·{tag})·可发信号"
    return f"美股盘外({tag})·不发信号"


print(f"📈 港股：{hk_status()}")
print(f"📈 美股：{us_status()}")


# ---------- 时间断层检测（核心）----------
# 上次活动时间 = 今日 monitor_log 最后采样时间 与 ring-log 最后响铃时间 的最近者。
# 断层（断网/暂停/故障）期间既不采样也不响铃，这两个时间戳会停在断层前，距 now 的间隔就能暴露断层。
def _log_date_tag(path):
    """从 monitor_log 文件名解析市场标签（HK_/US_ 前缀在 symbol 段），用于按市场对齐日期口径。"""
    base = os.path.basename(path)
    if "_US_" in base:
        return "US"
    return "HK"


def last_monitor_time(market=None):
    """今日 monitor_log_*.csv 里最晚的一条采样时间（2026-08-17 加 market 参数：
    HK 只查港股 log、US 只查美股 log——多会话并行盯盘下，港股会话的断层检测不再被
    美股会话的采样掩盖、反之亦然；None 保持旧的全市场口径，向后兼容）。

    2026-08-16 修复：日期口径按市场对齐——美股 log 按美东交易日命名
    （实锤：08-11 会话跨午夜后仍写 20260811 文件、末行 01:56）。原实现用北京日期 glob，
    北京 00:00-04:00（美股后半场）glob 不到任何美股 log → 输出「今日无采样记录」假安心，
    恰在最需要断层检测的时段失明。现美股按美东交易日日期 glob。
    2026-08-17 修：美东日期改 zoneinfo 直接转美东时区取（与 preflight/monitor_guard 同法）——
    原「北京 −12h」是夏令时硬编码，11 月切冬令时（EST=UTC-5 需 −13h）会取错日、glob 落空。"""
    hk_date = now.strftime("%Y%m%d")
    try:
        from zoneinfo import ZoneInfo
        us_date = now.astimezone(ZoneInfo("America/New_York")).strftime("%Y%m%d")
    except Exception:
        us_date = (now - datetime.timedelta(hours=12)).strftime("%Y%m%d")  # zoneinfo 不可用：夏令时估兜底
    logs = []
    if market in (None, "HK"):
        for lg in glob.glob(os.path.join(TMP_DIR, f"monitor_log_HK_*_{hk_date}_{MODE}.csv")):
            logs.append(lg)
    if market in (None, "US"):
        for lg in glob.glob(os.path.join(TMP_DIR, f"monitor_log_US_*_{us_date}_{MODE}.csv")):
            logs.append(lg)
    best = None
    for lg in logs:
        try:
            with open(lg) as f:
                rows = list(csv.reader(f))
            if len(rows) > 1:
                t = rows[-1][0]  # "HH:MM:SS"
                # 美股 log 跨午夜：行内时分若「大于」当前时分，说明末行是昨天/今天凌晨写的
                # （美东交易日内、北京已跨日），按北京今天拼会把它推到未来 → 判为跨午夜、
                # 把日期回退一天。与 monitor_segment 哨兵的跨午夜推断同口径。
                t_time = datetime.datetime.strptime(t, "%H:%M:%S").time()
                row_date = now.date()
                if t_time > now.time():
                    row_date = now.date() - datetime.timedelta(days=1)
                dt = datetime.datetime.combine(row_date, t_time)
                if best is None or dt > best:
                    best = dt
        except Exception:
            continue
    return best


def last_ring_time():
    """ring-log.csv 最后一条响铃时间（含日期；仅 signal 模式读——auto 不响铃，读 signal 会话的
    ring-log 会把别会话响铃误当本会话活动时间）。

    2026-08-16 修复：末行日期校验——原实现取物理末行不校验日期，周一开盘会把周五最后
    响铃当「上次活动」必报假断层（狼来了）。现末行日期非今日则返回 None（非当日响铃
    不作为断层判据；ring-log 含日期字段、直接比对）。"""
    if MODE != "signal":
        return None
    ring = os.path.join(SIGNALS_DIR, "ring-log.csv")
    if not os.path.exists(ring):
        return None
    try:
        with open(ring) as f:
            rows = [r for r in csv.reader(f) if r and r[0] and not r[0].startswith("timestamp")]
        if not rows:
            return None
        last = datetime.datetime.strptime(rows[-1][0].strip(), "%Y-%m-%d %H:%M:%S")
        if last.date() != now.date():
            return None   # 非当日响铃：不当断层判据（周一开盘不拿周五响铃报假断层）
        return last
    except Exception:
        return None


print(f"\n🔍 时间断层检测：")
# 市场scope（2026-08-17 立，多会话并行盯盘）：传了标的参数时按主标的市场查本市场 log
# （港股会话不被美股采样掩盖、反之亦然）；无参数时保持旧全市场口径（默认启动场景）。
_scope_syms = [a for a in sys.argv[1:] if not a.startswith("--mode")]
_market_scope = None
if _scope_syms:
    _first = _scope_syms[0].split(",")[0].strip().upper()
    _market_scope = "US" if _first.startswith("US.") else ("HK" if _first.startswith("HK.") else None)
if _market_scope:
    print(f"   （按市场 scope={_market_scope}，只查本市场 log——多会话并行盯盘互不掩盖）")
mt = last_monitor_time(market=_market_scope)
rt_ = last_ring_time()
if mt:
    print(f"   上次采样：{mt:%H:%M:%S}")
if rt_:
    print(f"   上次响铃：{rt_:%Y-%m-%d %H:%M:%S}")
candidates = [t for t in [mt, rt_] if t]
if candidates:
    last_activity = max(candidates)
    gap = (now - last_activity).total_seconds() / 60
    if gap >= GAP_WARN_MIN:
        # B5（2026-08-04）：盘中断层额外强调「疑似主动停密采样」+ 立即重启 monitor_segment
        # （盯盘纪律：盘中不得擅自停/降频，2026-08-04 教训）；盘外断层才按断网/故障处理。
        # 2026-08-18 起 us_status 盘前也返回「可交易」，美股判据同步含「盘前」段。
        in_session = ("盘中" in hk_status()) or ("盘中" in us_status()) or ("盘前" in us_status())
        stop_hint = (
            " → 盘中疑似【主动停密采样】（非断网/故障）！立即重启 monitor_segment 40 秒循环恢复密盯"
            "（2026-08-04 教训：盯盘期间不得擅自停/降频，唯一停盯途径是用户喊停或撞上 12:00/16:00 边界）"
            if in_session else ""
        )
        print(
            f"   ⚠️ 距上次活动 {gap:.0f} 分钟（≥ {GAP_WARN_MIN}min）→ 疑似时间断层（断网/暂停/故障）{stop_hint}！"
            f"绝不用断层前的旧数据发信号；先读今日 signals 重建持仓认知、确认无悬空（响铃未取实测价）信号，"
            f"再 snapshot 刷新现价。"
        )
    else:
        print(f"   ✅ 距上次活动 {gap:.1f} 分钟，无明显断层。")
else:
    print("   今日无采样/响铃记录（首次启动或休市日）——无需断层判断。")


# ---------- 当前 equity + 单笔预算 B（按模式取：auto 走账户 API、signal 走 equity-log；与 preflight.py 同口径）----------
# 2026-08-01 双模式重构：equity 按 mode 取（auto 账户 API / signal equity-log）。
# 2026-09-03 修订：auto + 模拟盘账户时算仓位的 equity 恒取实盘口径（当日参考快照，对齐实盘），
#   见下方快照联动块与 trade_utils_tiger.py「实盘参考快照」节。
try:
    sys.path.insert(0, SCRIPT_DIR)
    from trade_utils_tiger import load_equity as _le
    mode = MODE
    with open(os.path.join(SCRIPT_DIR, "..", "config.json")) as f:
        risk = json.load(f).get("risk", {})
    frac = risk.get("risk_fraction")
    fmax = risk.get("f_max", frac)
    lev = risk.get("max_leverage", 10)
    eq_now, cur, eq_src = _le(mode, account=ACCOUNT)
    # auto 模式实盘参考快照联动（2026-09-03 立，auto 模拟盘恒开对齐实盘，与 preflight 同口径）：
    # 实盘会话（--account live）顺手刷新当日快照供同日模拟盘用；模拟盘会话快照缺失/非当日时
    # 打印警示（B 已按实盘口径 fail-closed 返回 None）并自动尝试刷新一次（无解锁则失败含指引）。
    if mode == "auto":
        try:
            from trade_utils_tiger import fetch_live_reference as _flr
            from trade_utils_tiger import read_live_reference as _rlr
            from trade_utils_tiger import is_live_reference_fresh as _ilf
            if ACCOUNT == "live":
                # 实盘会话：快照缺失 / 非当日才刷（当天已刷过就不再打实盘 API）。
                _rl = _rlr()
                if _rl is None or not _ilf(_rl):
                    _ok_r, _msg_r = _flr(verbose=False)
                    if _ok_r:
                        print(f"🪞 实盘参考快照已刷新（供 auto 模拟盘对齐实盘，取数 {_msg_r.get('fetched_at')}）")
            elif eq_now is None:
                print(f"🚨 auto 模拟盘算仓位须用实盘口径（2026-09-03 恒开）：{eq_src}")
                print(f"   刷新前模拟盘开仓会被拒（blocked_by: live_reference_required）——"
                      f"在已实盘解锁的会话执行 python3 scripts/trade_utils_tiger.py "
                      f"--refresh-live-reference 即可。")
                _ok_r, _msg_r = _flr(verbose=False)
                if _ok_r:
                    print(f"🪞 实盘参考快照已刷新（取数 {_msg_r.get('fetched_at')}）")
                    eq_now, cur, eq_src = _le(mode, account=ACCOUNT)
        except Exception as _er:
            print(f"⚠️ 实盘参考快照联动检查失败（{_er}）")
    if frac is not None and eq_now is not None:
        print(
            f"\n💰 模式 {mode} | 当前 equity {eq_now:,.2f} {cur} × {frac*100:.1f}% = 单笔预算 B {frac*eq_now:,.2f}"
            f"（f_max 硬上限 {fmax*100:.1f}%）| 来源：{eq_src}"
        )
        print(f"⚖️  开仓市值上限 = equity × {lev} 倍杠杆 = {eq_now * lev:,.2f} {cur}（选仓位时 max_loss 与市值两约束同时满足）")
except Exception as e:
    print(f"\n💰 ⚠️ 读 config/equity 失败（{e}）——执行前手动确认 B")


# ---------- 今日信号摘要（让 AI 知道今天发生了什么、有无悬空）----------
date_yyyy = now.strftime("%Y-%m-%d")
found_any = False
for tag, name in [("HKT", "港股"), ("ET", "美股")]:
    f = os.path.join(SIGNALS_DIR, f"{date_yyyy}-{tag}-signals.md")
    if not os.path.exists(f):
        continue
    found_any = True
    with open(f) as ff:
        content = ff.read()
    voided = ("已撤销" in content) or ("作废" in content)
    opens = content.count("🟢🟢🟢 开仓") + content.count("🔵🔵🔵 加仓")
    closes = content.count("🔴🔴🔴 平仓") + content.count("🟠🟠🟠 减仓")
    flag = " ⚠️含作废信号" if voided else ""
    print(f"\n📋 今日{name}信号：{f}")
    print(
        f"   开仓/加仓 {opens} 条、平仓/减仓 {closes} 条{flag}"
        f" → AI 细读判断：① 当前未平仓假设持仓（开仓/加仓批次 + 入场价 + 止损 + 止盈）"
        f" ② 有无响铃未取实测价的悬空信号 ③ 作废信号不算样本"
    )
if not found_any:
    print("\n📋 今日无信号文件（尚无开仓/加仓）——无假设持仓、无悬空项。")


# ---------- 可选：snapshot 刷新指定标的现价（验证持仓 / 刷新参考价）----------
# 过滤 --mode / --account 及其取值，剩余的位置参数才是标的（2026-08-20 修：
# 原只过滤 --mode 前缀，--mode auto 的 auto 被当成标的、--account live 的 live 同样——
# 实盘盯盘传 --account live 时 snapshot 刷新必炸「format of code live is wrong」）。
_positional = []
_skip_next = False
for _a in sys.argv[1:]:
    if _skip_next:
        _skip_next = False
        continue
    if _a in ("--mode", "--account"):   # 带取值的选项：跳过取值
        _skip_next = True
        continue
    if _a.startswith("--mode=") or _a.startswith("--account="):
        continue
    _positional.append(_a)
if _positional:
    syms = [s.strip() for s in _positional[0].split(",") if s.strip()]
    print(f"\n📊 刷新现价 {syms}：")
    try:
        from futu import OpenQuoteContext
        ctx = OpenQuoteContext(host="127.0.0.1", port=11111)
        ret, df = ctx.get_market_snapshot(syms)
        if ret == 0 and df is not None and len(df) > 0:
            for _, r in df.iterrows():
                print(
                    f"   {r.get('code')} {r.get('name')}: 现价 {r['last_price']} | "
                    f"今开 {r.get('open_price')} 高 {r.get('high_price')} 低 {r.get('low_price')} | "
                    f"买卖比 {r.get('bid_ask_ratio')} | update {r.get('update_time')}"
                )
        else:
            print(f"   snapshot 失败 ret={ret} {df}")
        ctx.close()
    except Exception as e:
        print(f"   ⚠️ snapshot 失败（{e}）——富途 OpenD 未登录/未启动？盘中发信号前务必先手动 snapshot")


# ---------- actions 开平闭环检查（2026-08-31 T132）----------
# 盯盘中断（断层警告 / 会话切换 / 长任务返回）恢复后，除重建上下文外追加回查当日
# actions 是否有未闭环开仓（open 无对应 close）——BRACKETS 止损/止盈腿自动触发的
# 平仓不经 close_position 脚本、无 AI 在场转录，中断期间发生的自动平仓会漏记
# （2026-08-27 01888 PROFIT 腿 15:34 自动触发、漏记 4 天的教训）。已平仓未补记时
# 子脚本输出补记指引并退出码 1；此处不中断 resume，AI 照指引补记后再继续。
try:
    import subprocess
    _ac = subprocess.run(
        [sys.executable, os.path.join(SCRIPT_DIR, "actions_check.py")] + sys.argv[1:],
        capture_output=True, text=True, timeout=90)
    print("\n🔁 actions 开平闭环检查（T132，中断恢复固定动作）：")
    print(_ac.stdout.rstrip())
    if _ac.returncode != 0:
        print("   ⚠️ 闭环异常——按上方处置指引补记（log_action.sh + update_losing_streak.py）后再继续盯盘。")
except Exception as e:
    print(f"\n🔁 actions 闭环检查未跑成（{e}）——手动跑 python3 actions_check.py")

print(
    f"\n✅ 恢复协议完成。AI 据以上判断：是否在盘中可继续盯盘 / 是否需读今日 signals 重建持仓 / "
    f"有无悬空项要处理；发信号前再过「发信号硬前置」（距上次 date/snapshot >2min 必须刷新）。"
)
