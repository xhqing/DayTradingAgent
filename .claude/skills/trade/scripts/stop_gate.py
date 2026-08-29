#!/usr/bin/env python3
"""停盯边界时间闸（2026-08-24 立，TODO T118 落地）。

为什么：2026-08-24 早盘实录——AI 在 11:48（距 12:00 收盘还有 12 分钟）以「空仓 + 无信号」
为由自行停盯收尾（unregister + caffeinate off + 解锁 revoke 三件套），违反 SKILL.md
「盯到用户喊停或收盘（取先到）、没让停就不停」。空仓 / 无信号 / 市场无聊都不是停盯理由，
散文规定靠 AI 记忆执行会衰减（2026-08-18 三起违规同根因）——本闸把「盘中不许停盯」
变成机械强制：收尾脚本（monitor_unregister.sh 等）盘中调用本闸，距收盘 >5 分钟且
无 --force 直接拒绝执行。

判定口径（与 monitor_guard.py / preflight.py 同源）：
  - 港股盘中：周一至周五 09:30-12:00 / 13:00-16:00（北京时间）；
  - 美股可交易时段：美东 04:00-16:00（盘前 + 盘中，zoneinfo 处理夏令时 / 跨午夜）。

放行条件（满足任一）：
  ① 当前不在任何可交易时段（盘外 / 周末 / 午休 12:00-13:00 / 收盘后）——正常停盯时点；
  ② 在可交易时段内、但距该时段收盘 ≤5 分钟（收盘边界窗口，临近收盘停盯合法）；
  ③ 显式 --force（用户喊停 / 用户确认就此停盯——用户指令优先于一切机械闸）。

用法：
  python3 stop_gate.py check          # 返回 JSON {allowed, reason, minutes_to_close, market}
  python3 stop_gate.py check --force  # 显式放行（用户喊停场景）
退出码：0 = 放行；2 = 拦截（调用方应中止收尾并输出提示）。
"""

import json
import sys
from datetime import datetime

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None


def _parse_hm(s):
    h, m = s.split(":")
    return int(h) * 60 + int(m)


# 各市场可交易时段（分钟制）：(开, 收) 列表
_HK_SESSIONS = [(_parse_hm("09:30"), _parse_hm("12:00")),
                (_parse_hm("13:00"), _parse_hm("16:00"))]
_CLOSE_WINDOW_MIN = 5  # 距收盘 ≤5 分钟 = 收盘边界窗口，停盯合法


def _hk_sessions_today(now):
    """港股今天是否交易日（简化：周一至周五；节假日按 preflight 同口径不查日历——
    节日当天盘外时段本来就判放行，盘中时段若误放行也只是多拦一道，方向安全）。"""
    return now.weekday() < 5


def _us_sessions_now(now):
    """美股当前可交易时段的收盘时刻（美东），返回 (in_session, minutes_to_close)。

    美东 04:00-16:00 = 盘前 + 盘中（2026-08-18 立规）。跨午夜（北京 16:00-次日 04:00）
    由 astimezone 天然处理。"""
    if ZoneInfo is None:
        return False, None
    us = now.astimezone(ZoneInfo("America/New_York"))
    if us.weekday() >= 5:
        return False, None
    t = us.hour * 60 + us.minute
    open_m, close_m = _parse_hm("04:00"), _parse_hm("16:00")
    if open_m <= t < close_m:
        return True, (close_m - t)
    return False, None


def check(force=False):
    now = datetime.now()
    # 港股判定
    if _hk_sessions_today(now):
        t = now.hour * 60 + now.minute
        for open_m, close_m in _HK_SESSIONS:
            if open_m <= t < close_m:
                mins = close_m - t
                if mins <= _CLOSE_WINDOW_MIN:
                    return True, "hk_close_window", mins, "HK"
                if force:
                    return True, "forced_by_user", mins, "HK"
                return (False,
                        f"港股盘中（距收盘 {mins} 分钟 > {_CLOSE_WINDOW_MIN}）——盯盘终止条件只有"
                        f"用户喊停或收盘（取先到），空仓 / 无信号 / 市场无聊都不是停盯理由；"
                        f"确属用户喊停请加 --force",
                        mins, "HK")
    # 美股判定
    in_us, us_mins = _us_sessions_now(now)
    if in_us:
        if us_mins <= _CLOSE_WINDOW_MIN:
            return True, "us_close_window", us_mins, "US"
        if force:
            return True, "forced_by_user", us_mins, "US"
        return (False,
                f"美股可交易时段（距收盘 {us_mins} 分钟 > {_CLOSE_WINDOW_MIN}）——盯盘终止条件只有"
                f"用户喊停或收盘（取先到），空仓 / 无信号 / 市场无聊都不是停盯理由；"
                f"确属用户喊停请加 --force",
                us_mins, "US")
    # 盘外（周末 / 夜间 / 午休 / 收盘后）：正常停盯时点，放行
    return True, "outside_trading_hours", None, None


def main():
    args = sys.argv[1:]
    if not args or args[0] != "check":
        print(__doc__)
        return 0
    force = "--force" in args
    allowed, reason, mins, market = check(force)
    print(json.dumps({
        "allowed": allowed,
        "reason": reason,
        "detail": "" if allowed else reason,
        "minutes_to_close": mins,
        "market": market,
        "force": force,
    }, ensure_ascii=False))
    return 0 if allowed else 2


if __name__ == "__main__":
    sys.exit(main())
