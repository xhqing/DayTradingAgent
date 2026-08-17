#!/usr/bin/env python3
"""盯盘密采样守卫 hook（2026-08-04 立，A3 + B4 多层防护）。

为什么：盯盘期间 AI 曾用 CronCreate 设低频扫描 + cron 触发时直接调 snapshot.py/hot_list.py
（不经 monitor_segment.py）绕过 40 秒密采样强制（2026-08-04 违规教训）。脚本护栏
（monitor_segment.py 的 DURATION>40 夹回 40）只在调用该脚本时生效，AI 不调脚本就绕过。
本 hook 在两个关卡补查 monitor_segment 是否在跑，堵绕过路径：

- PreToolUse（matcher Bash）：盘中调 snapshot.py/hot_list.py 但 monitor_segment 未在跑 →
  exit 2 阻断 + stderr 提醒「密采样走 monitor_segment，禁 snapshot/hot_list 替代」。
  （monitor_segment 在跑时的 snapshot 是开仓前正常刷新，不阻。）
- PreToolUse（matcher Bash，A4）：盘中单条 Bash 命令含 ≥2 次 monitor_segment.py 调用
  （&& 连跑多段）→ exit 2 阻断 + stderr 提醒「连跑 = 段间不分析 = 等效降频」。
- PreToolUse（matcher Bash，空转硬门 2026-08-17 立）：盘中重启密采样段（恰好 1 个
  exec_seg）但分析心跳停更 > 180 秒 / 当天采样已跑 ≥180 秒仍零心跳 → exit 2 阻断，
  逼 AI 先补「一行式判断 + 写分析心跳」再重启（堵 2026-08-17 空转实录：52 次纯重启
  采样、0 分析文本——采样链防护全部放行、分析链死亡无人拦）。
- Stop：盘中且回合结束 monitor_segment 未在跑 → stderr 提醒「盯盘期间必须保持
  monitor_segment 循环，立即重启」。

局限（诚实）：AI 仍能 kill 进程或换别的方式绕过——本 hook 只提高绕过成本 + 暴露，
不是 100% 银弹（详见 monitoring.md「2026-08-04 教训」多层防护说明）。

用法（settings.json hooks 注册）：
  PreToolUse matcher Bash → python3 .claude/hooks/monitor_guard.py pretool
  Stop                    → python3 .claude/hooks/monitor_guard.py stop
hook 接收 stdin JSON（tool_name/tool_input 等），exit 0 放行 / exit 2 阻断（PreToolUse）。
"""
import sys
import os
import json
import subprocess
from datetime import datetime

# 项目根 = .claude/hooks 的上两级（.claude/hooks -> .claude -> 项目根）
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.abspath(os.path.join(_SCRIPT_DIR, "..", ".."))
TMP_DIR = os.path.join(_PROJECT_ROOT, "tmp")
# 2026-08-16 分层（原 STALE_SECONDS=300 单层与自述「段间循环 <90 秒」不匹配——进程死后
# log 5 分钟内仍判「在跑」、最长 5 分钟盲窗）：
#   进程在跑（pgrep）或 log <120 秒有更新 → 在跑（确定信号）；
#   log 120-300 秒有更新 → 疑似断了（提示重启但不阻断）；
#   log >300 秒无更新 → 断了（原口径，阻断/提醒照旧）。
RUNNING_SECONDS = 120   # log 近 2 分钟有更新 = 视为在跑
STALE_SECONDS = 300     # 超过 5 分钟无更新 = 确定断

# 空转防护（2026-08-17 立红灯「盯盘空转」修法④）：AI 要重启密采样段时，若分析心跳
# （tmp/analysis_beat_{date}_{mode}.csv，AI 每段分析时追加）停更超过此阈值 → PreToolUse
# 阻断，逼 AI 先补「一行式判断 + 写心跳」再重启采样。为什么做成 hook 硬门：2026-08-17
# 实录空转一下午（52 次纯重启采样、0 分析文本），用户纠正后 10 分钟复发——reference
# 软约束会衰减，工具级阻断才拦得住「只重启不分析」的路径。
# 阈值同 watcher 的 ANALYSIS_BEAT_STALE_SECONDS（180 秒 ≈ 3 个段周期）。
# 边界（防误伤）：① 当天无心跳文件且采样 log 也不足 3 分钟 = 盯盘刚启动，放行（第一段
# 之前本就没有分析）；② 非采样命令不拦（本检查只挂在「重启采样」动作上）。
ANALYSIS_BEAT_STALE_SECONDS = 180


def analysis_beat_status():
    """分析心跳状态（2026-08-17 立）。返回 (state, age_seconds)：
    - ("fresh", 秒)：心跳新鲜（< 阈值）。
    - ("stale", 秒)：心跳停更超阈值（空转形态①：分析过、后来死了）。
    - ("none", None)：当天无心跳文件。调用方结合采样 log 时长判断——log 也不足阈值
      = 刚启动放行；log 已跑 ≥ 阈值仍零心跳 = 从未分析过（空转形态②）。
    signal/auto 任一 mode 的心跳新鲜即 fresh（guard 拿不到本会话 mode，任一路分析
    在跑 = 不是全空转，与 watcher 同口径）。
    """
    import glob
    today = datetime.now().strftime("%Y%m%d")
    beats = glob.glob(os.path.join(TMP_DIR, f"analysis_beat_{today}_*.csv"))
    if not beats:
        return ("none", None)
    newest = max(os.path.getmtime(p) for p in beats)
    age = datetime.now().timestamp() - newest
    return ("fresh", age) if age < ANALYSIS_BEAT_STALE_SECONDS else ("stale", age)


def sampling_log_run_seconds():
    """当天采样已运行多久（秒）：读当天（按文件名日期）monitor_log 文件**首行采样时刻**，
    取最早的距今时长。无当天文件 / 读不出首行返回 None。与 monitor_watcher 的
    _sampling_log_age_seconds 同口径（读文件内容首行而非 ctime——ctime 会因 rename 等
    元数据操作被重置，语义不稳，2026-08-17 实测教训）。"""
    import glob
    today = datetime.now().strftime("%Y%m%d")
    logs = glob.glob(os.path.join(TMP_DIR, f"monitor_log_*_{today}_*.csv"))
    if not logs:
        return None
    from datetime import timedelta
    now = datetime.now()
    oldest_start = None
    for p in logs:
        try:
            with open(p) as lf:
                for line in lf:
                    line = line.strip()
                    if not line or line.startswith("time,"):
                        continue  # 跳表头 / 空行
                    first_t = line.split(",", 1)[0]
                    ft = datetime.strptime(first_t, "%H:%M:%S").time()
                    fdate = (now.date() - timedelta(days=1)) if ft > now.time() else now.date()
                    start = datetime.combine(fdate, ft)
                    if oldest_start is None or start < oldest_start:
                        oldest_start = start
                    break  # 只看首行
        except Exception:
            continue
    if oldest_start is None:
        return None
    return (now - oldest_start).total_seconds()


def _parse_hm(s):
    return datetime.strptime(s, "%H:%M").time()


_HK_AM_START = _parse_hm("09:30")
_HK_AM_END = _parse_hm("12:00")
_HK_PM_START = _parse_hm("13:00")
_HK_PM_END = _parse_hm("16:00")


def in_hk_session(now):
    """港股盘中（HKT 09:30-12:00 / 13:00-16:00，周一至周五）。"""
    if now.weekday() >= 5:  # 周末
        return False
    t = now.time()
    return (_HK_AM_START <= t < _HK_AM_END) or (_HK_PM_START <= t < _HK_PM_END)


def in_us_session(now):
    """美股盘中（美东 09:30-16:00，zoneinfo 自动处理夏令时/冬令时与跨午夜，2026-08-16 修）。

    原实现「夏令时 HKT 21:30-次日 04:00 硬编码 + weekday()>=5 排周末」三处错：
    ① 北京周六 00:00-04:00（美东周五盘中）guard 完全失效 4 小时；
    ② 周一凌晨（美东周日休市）误激活；
    ③ 11 月切冬令时后整体错位 1 小时。
    现按美东本地时间判定（preflight.py 同口径），zoneinfo 不可用时回退旧硬编码。"""
    try:
        from zoneinfo import ZoneInfo
        us = now.astimezone(ZoneInfo("America/New_York"))
        if us.weekday() >= 5:
            return False
        t = us.time()
        return _parse_hm("09:30") <= t < _parse_hm("16:00")
    except Exception:
        # zoneinfo 不可用（极端环境）→ 回退夏令时硬编码
        if now.weekday() >= 5:
            return False
        t = now.time()
        return t >= _parse_hm("21:30") or t < _parse_hm("04:00")


def in_trading_session(now=None):
    now = now or datetime.now()
    return in_hk_session(now) or in_us_session(now)


def monitor_segment_running(now=None):
    """monitor_segment（或 ws_segment / futu_ws_segment，2026-08-16 扩检测面）是否在跑：
    进程在 OR monitor_log 近 RUNNING_SECONDS 有更新。

    分层（2026-08-16 立，原单层 300 秒与自述「段间循环 <90 秒」不匹配）：
    进程在 / log <120s → (True, ...)；log 120-300s → (False, "疑似断了…")——
    调用方 pretool 阻断、stop 提醒时按 False 处理（宁可多提醒一次），文案带疑似标记。

    返回 (running: bool, why: str)。
    """
    now = now or datetime.now()
    # ① 进程检查（三个密采样入口都认——2026-08-07 起港股主力采样是 ws_segment，
    #    只认 monitor_segment 会把正常 ws 采样误判为「未在跑」而阻断正常操作）
    for script in ("monitor_segment.py", "ws_segment.py", "futu_ws_segment.py"):
        try:
            out = subprocess.run(
                ["pgrep", "-f", script],
                capture_output=True, text=True, timeout=3,
            )
            if out.stdout.strip():
                return True, f"进程在跑（{script}）"
        except Exception:
            pass
    # ② monitor_log 新鲜度分层（段间循环 <90 秒；ws 系每秒写一行更密）
    try:
        if os.path.isdir(TMP_DIR):
            newest = 0.0
            for f in os.listdir(TMP_DIR):
                if f.startswith("monitor_log_") and f.endswith(".csv"):
                    mtime = os.path.getmtime(os.path.join(TMP_DIR, f))
                    if mtime > newest:
                        newest = mtime
            if newest > 0:
                age = now.timestamp() - newest
                if age < RUNNING_SECONDS:
                    return True, f"log {int(age)} 秒前更新"
                if age < STALE_SECONDS:
                    return False, f"疑似断了（log {int(age)} 秒未更新，段间循环应 <90 秒）"
            return False, "log 长时间无更新"
    except Exception as e:
        return False, f"检查异常 {e}"
    return False, "无 monitor_log"


def main():
    hook_type = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        payload = json.load(sys.stdin) or {}
    except Exception:
        payload = {}

    # 盘外不干预（周末 / 夜间 / 午休）
    if not in_trading_session():
        sys.exit(0)

    running, why = monitor_segment_running()

    if hook_type == "pretool":
        tool_input = payload.get("tool_input") or {}
        command = tool_input.get("command", "") if isinstance(tool_input, dict) else ""
        # A4（2026-08-05 立；2026-08-16 扩检测面 + 修反向误伤）：单条 Bash 命令连跑多个
        # 密采样段（&& / ; / || 串联）= 等效降频——段间 AI 不醒来分析、段结束通知只在全部段
        # 跑完后触发一次。原实现只数 monitor_segment.py 子串次数：① 2026-08-07 起港股主力
        # 采样已是 ws_segment.py、&& 连跑不被拦；② 反向误伤——同一命令里两次提及文件名但
        # 非连跑（如 `py_compile monitor_segment.py ws_segment.py`）也被拦。现按 shell 串联
        # 操作符（&& ; || 换行）切分子命令、只数「真正独立执行了采样段」的子命令数 ≥2。
        # 密采样唯一合法循环 = 单段 40 秒 → 段结束通知唤醒 AI 分析 → 重启下一段。
        import re as _re
        subcmds = _re.split(r"&&|\|\||;|\n", command)
        seg_subcmds = [sc for sc in subcmds
                       if any(s in sc for s in ("monitor_segment.py", "ws_segment.py",
                                                "futu_ws_segment.py"))]
        # 子命令级再排除「提及但不执行」：子命令须像一次执行（python3 … 脚本名），
        # py_compile / cat / diff / grep 等工具引用脚本文件不算执行采样。
        exec_seg = [sc for sc in seg_subcmds
                    if _re.search(r"(^|\s|/)python3?\s", sc) and "py_compile" not in sc
                    and not _re.search(r"\b(cat|head|tail|diff|grep|ls|rm|mv|cp|less|more)\b", sc)]
        if len(exec_seg) >= 2:
            msg = (
                f"⚠️ 密采样守卫阻断：单条 Bash 命令串联执行 {len(exec_seg)} 个密采样段"
                f"（&&/;/|| 连跑多段）。连跑 = 段间 AI 不醒来分析 = 等效降频，违反密采样规定（2026-08-05 教训："
                f"AI 误把段结束进程归 0 当断链、用 && 连跑 4 段减少断链点，被用户纠正——段结束进程归 0 本就正常，"
                f"连跑才是故障）。请改为单段调用，靠段结束通知驱动循环。"
            )
            print(msg, file=sys.stderr)
            sys.exit(2)  # PreToolUse exit 2 = 阻断 + stderr 反馈给 AI
        # 空转硬门（2026-08-17 立红灯「盯盘空转」修法④）：命令要重启密采样段（恰好 1 个
        # exec_seg）时检查分析心跳——心跳停更 > 3 分钟 = 分析链死了还只顾重启采样，阻断，
        # 逼 AI 先给「一行式判断 + 写心跳」再重启。刚启动（无心跳且采样 log < 3 分钟）放行。
        if len(exec_seg) == 1:
            bstate, bage = analysis_beat_status()
            if bstate == "stale":
                print(
                    f"⚠️ 密采样守卫阻断（空转防护）：检测到重启密采样，但分析心跳已停更 "
                    f"{int(bage)} 秒（> {ANALYSIS_BEAT_STALE_SECONDS}s）——采样链活着、分析链死了"
                    f"（2026-08-17 空转实录形态）。先补做本段分析：① 用一行式模板给判断"
                    f"（现价/关键位/VWAP/结论/下次段时间）；② 追加分析心跳 "
                    f"echo \"$(date '+%H:%M:%S'),<标的>,<判断>\" >> tmp/analysis_beat_{datetime.now().strftime('%Y%m%d')}_<mode>.csv；"
                    f"③ 再重启下一段。模板见 references/monitoring.md「每段最小输出模板 + 分析心跳」。",
                    file=sys.stderr,
                )
                sys.exit(2)
            if bstate == "none":
                run_sec = sampling_log_run_seconds()
                if run_sec is not None and run_sec >= ANALYSIS_BEAT_STALE_SECONDS:
                    print(
                        f"⚠️ 密采样守卫阻断（空转防护）：当天采样已运行约 {int(run_sec)} 秒，"
                        f"但分析心跳为零（analysis_beat 文件不存在）——只采样、从未分析"
                        f"（2026-08-17 空转实录形态）。先给本段一行式判断并写分析心跳"
                        f"（tmp/analysis_beat_YYYYMMDD_<mode>.csv），再重启下一段。"
                        f"模板见 references/monitoring.md「每段最小输出模板 + 分析心跳」。",
                        file=sys.stderr,
                    )
                    sys.exit(2)
        target = ""
        if "snapshot.py" in command:
            target = "snapshot"
        elif "hot_list.py" in command:
            target = "hot_list"
        if target and not running:
            msg = (
                f"⚠️ 密采样守卫阻断：盘中调 {target}，但密采样未在跑（{why}）。"
                f"盯盘密采样的唯一入口是 monitor_segment / ws_segment / futu_ws_segment 40 秒循环，"
                f"禁用 snapshot/hot_list 替代（2026-08-04 违规教训：曾用 cron+snapshot 绕过降频）。"
                f"请先重启密采样循环，再在循环内做开仓前刷新。"
            )
            print(msg, file=sys.stderr)
            sys.exit(2)  # PreToolUse exit 2 = 阻断 + stderr 反馈给 AI
        sys.exit(0)

    if hook_type == "stop":
        if not running:
            msg = (
                f"⚠️ 密采样守卫提醒：回合结束，盘中但密采样未在跑（{why}）。"
                f"盯盘期间必须保持 monitor_segment / ws_segment 40 秒密采样循环（不得擅自停/降频，2026-08-04 教训）。"
                f"请立即重启密采样恢复密盯，或确认已到停盯边界（港股 12:00/16:00、用户喊停）。"
            )
            print(msg, file=sys.stderr)  # Stop hook stderr 作为 feedback 提醒 AI
        sys.exit(0)

    if hook_type == "taskstop":
        # A2（2026-08-04）：TaskStop 提醒（不阻断，避免误伤停出错进程）——
        # 盘中 + monitor_segment 在跑时，提醒 AI 确认停的不是密采样（hook 拿不到 task 命令，用「在跑」代理判断）。
        if running:
            msg = (
                f"⚠️ 密采样守卫提醒：盘中 TaskStop 后台任务，且 monitor_segment 正在跑（{why}）。"
                f"若要停的是 monitor_segment 密采样 = 违规（2026-08-04 教训：盯盘期间不得擅自停密采样），"
                f"除非已到停盯边界（港股 12:00/16:00、用户喊停）。停盯走 trade skill 停盯流程，勿直接 TaskStop 密采样。"
            )
            print(msg, file=sys.stderr)
        sys.exit(0)

    sys.exit(0)


if __name__ == "__main__":
    main()
