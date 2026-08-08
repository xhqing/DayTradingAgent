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
        if 1290 <= hhmm < 1440 or hhmm < 240:
            return "美股盘中(夏令时估·zoneinfo不可用)·可发信号"
        if 1200 <= hhmm < 1290:
            return "美股盘前(夏令时估·zoneinfo不可用)·只预热"
        return "美股盘外(夏令时估·zoneinfo不可用)·不发信号"
    us_wd = us_now.weekday()
    us_hhmm = us_now.hour * 60 + us_now.minute
    tag = "EDT夏令时" if bool(us_now.dst()) else "EST冬令时"
    if us_wd >= 5:
        return "周末休市"
    if 570 <= us_hhmm < 960:
        return f"美股盘中({tag})·可发信号"
    if 240 <= us_hhmm < 570:
        return f"美股盘前({tag})·只预热不发信号"
    return f"美股盘外({tag})·不发信号"


print(f"📈 港股：{hk_status()}")
print(f"📈 美股：{us_status()}")


# ---------- 时间断层检测（核心）----------
# 上次活动时间 = 今日 monitor_log 最后采样时间 与 ring-log 最后响铃时间 的最近者。
# 断层（断网/暂停/故障）期间既不采样也不响铃，这两个时间戳会停在断层前，距 now 的间隔就能暴露断层。
def last_monitor_time():
    """今日所有 monitor_log_*.csv 里最晚的一条采样时间。"""
    date_str = now.strftime("%Y%m%d")
    logs = glob.glob(os.path.join(TMP_DIR, f"monitor_log_*_{date_str}_{MODE}.csv"))
    best = None
    for lg in logs:
        try:
            with open(lg) as f:
                rows = list(csv.reader(f))
            if len(rows) > 1:
                t = rows[-1][0]  # "HH:MM:SS"
                dt = datetime.datetime.strptime(f"{now.strftime('%Y-%m-%d')} {t}", "%Y-%m-%d %H:%M:%S")
                if best is None or dt > best:
                    best = dt
        except Exception:
            continue
    return best


def last_ring_time():
    """ring-log.csv 最后一条响铃时间（含日期；仅 signal 模式读——auto 不响铃，读 signal 会话的 ring-log 会把别会话响铃误当本会话活动时间）。"""
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
        return datetime.datetime.strptime(rows[-1][0].strip(), "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


print(f"\n🔍 时间断层检测：")
mt = last_monitor_time()
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
        in_session = ("盘中" in hk_status()) or ("盘中" in us_status())
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
try:
    sys.path.insert(0, SCRIPT_DIR)
    from trade_utils_tiger import load_equity as _le
    mode = MODE
    with open(os.path.join(SCRIPT_DIR, "..", "config.json")) as f:
        risk = json.load(f).get("risk", {})
    frac = risk.get("risk_fraction")
    fmax = risk.get("f_max", frac)
    lev = risk.get("max_leverage", 10)
    eq_now, cur, eq_src = _le(mode)
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
_positional = [a for a in sys.argv[1:] if not a.startswith("--mode")]
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


print(
    f"\n✅ 恢复协议完成。AI 据以上判断：是否在盘中可继续盯盘 / 是否需读今日 signals 重建持仓 / "
    f"有无悬空项要处理；发信号前再过「发信号硬前置」（距上次 date/snapshot >2min 必须刷新）。"
)
