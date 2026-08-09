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
- Stop：盘中且回合结束 monitor_segment 未在跑 → stderr 提醒「盯盘期间必须保持
  monitor_segment 循环，立即重启」。

局限（诚实）：AI 仍能 kill 进程或换别的方式绕过——本 hook 只提高绕过成本 + 暴露，
不是 100% 银弹（详见 monitoring.md「2026-08-04 教训」多层防护说明）。

用法（settings.json hooks 注册，2026-08-09 起经跨平台 wrapper run_hook.sh——
wrapper 内探测 python3/python 解释器、用 $CLAUDE_PROJECT_DIR 定位项目根，macOS/Windows 通吃）：
  PreToolUse matcher Bash → bash $CLAUDE_PROJECT_DIR/.claude/hooks/run_hook.sh .claude/hooks/monitor_guard.py pretool
  TaskStop                → bash $CLAUDE_PROJECT_DIR/.claude/hooks/run_hook.sh .claude/hooks/monitor_guard.py taskstop
  Stop                    → bash $CLAUDE_PROJECT_DIR/.claude/hooks/run_hook.sh .claude/hooks/monitor_guard.py stop
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
STALE_SECONDS = 300  # monitor_log 5 分钟内有更新 = 视为 monitor_segment 在跑


def _parse_hm(s):
    return datetime.strptime(s, "%H:%M").time()


_HK_AM_START = _parse_hm("09:30")
_HK_AM_END = _parse_hm("12:00")
_HK_PM_START = _parse_hm("13:00")
_HK_PM_END = _parse_hm("16:00")
# 美股夏令时 HKT 21:30-次日 04:00（冬令时 22:30-05:00，需手动调；当前 8 月夏令时）
_US_START = _parse_hm("21:30")
_US_END = _parse_hm("04:00")


def in_hk_session(now):
    """港股盘中（HKT 09:30-12:00 / 13:00-16:00，周一至周五）。"""
    if now.weekday() >= 5:  # 周末
        return False
    t = now.time()
    return (_HK_AM_START <= t < _HK_AM_END) or (_HK_PM_START <= t < _HK_PM_END)


def in_us_session(now):
    """美股盘中（夏令时 HKT 21:30-次日 04:00，跨午夜，周一至周五）。"""
    if now.weekday() >= 5:
        return False
    t = now.time()
    return t >= _US_START or t < _US_END


def in_trading_session(now=None):
    now = now or datetime.now()
    return in_hk_session(now) or in_us_session(now)


def _proc_running(keyword):
    """跨平台进程检测（2026-08-09 适配）：macOS/Linux 用 pgrep，Windows 用
    PowerShell CIM 按命令行匹配（Windows 无 pgrep）。返回 bool。"""
    if os.name == "nt":
        ps = ("powershell", "-NoProfile", "-Command",
              f"if (Get-CimInstance Win32_Process | Where-Object {{ $_.CommandLine -like '*{keyword}*' }}) {{ exit 0 }} else {{ exit 1 }}")
    else:
        ps = ("pgrep", "-f", keyword)
    try:
        return subprocess.run(ps, capture_output=True, timeout=15).returncode == 0
    except Exception:
        return False


def monitor_segment_running(now=None):
    """monitor_segment 是否在跑：进程在 OR 近 STALE_SECONDS monitor_log 有更新。

    返回 (running: bool, why: str)。
    """
    now = now or datetime.now()
    # ① 进程检查
    if _proc_running("monitor_segment.py"):
        return True, "进程在跑"
    # ② monitor_log 近 STALE_SECONDS 有更新（段间循环 < 90 秒，5 分钟无更新 = 断了）
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
                if age < STALE_SECONDS:
                    return True, f"log {int(age)} 秒前更新"
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
        # A4（2026-08-05）：单条 Bash 命令连跑多个 monitor_segment（&& 串联）= 等效降频——
        # 段间 AI 不醒来分析、段结束通知只在全部段跑完后触发一次，采样间隔被拉到几分钟。
        # 密采样唯一合法循环 = 单段 40 秒 → 段结束通知唤醒 AI 分析 → 重启下一段。
        if command.count("monitor_segment.py") >= 2:
            msg = (
                f"⚠️ 密采样守卫阻断：单条 Bash 命令出现 {command.count('monitor_segment.py')} 次 monitor_segment 调用"
                f"（&& 连跑多段）。连跑 = 段间 AI 不醒来分析 = 等效降频，违反密采样规定（2026-08-05 教训："
                f"AI 误把段结束进程归 0 当断链、用 && 连跑 4 段减少断链点，被用户纠正——段结束进程归 0 本就正常，"
                f"连跑才是故障）。请改为单段调用，靠段结束通知驱动循环。"
            )
            print(msg, file=sys.stderr)
            sys.exit(2)  # PreToolUse exit 2 = 阻断 + stderr 反馈给 AI
        target = ""
        if "snapshot.py" in command:
            target = "snapshot"
        elif "hot_list.py" in command:
            target = "hot_list"
        if target and not running:
            msg = (
                f"⚠️ 密采样守卫阻断：盘中调 {target}，但 monitor_segment 未在跑（{why}）。"
                f"盯盘密采样的唯一入口是 monitor_segment.py 40 秒循环，禁用 snapshot/hot_list 替代"
                f"（2026-08-04 违规教训：曾用 cron+snapshot 绕过降频）。请先重启 monitor_segment 密采样循环，"
                f"再在循环内做开仓前刷新。"
            )
            print(msg, file=sys.stderr)
            sys.exit(2)  # PreToolUse exit 2 = 阻断 + stderr 反馈给 AI
        sys.exit(0)

    if hook_type == "stop":
        if not running:
            msg = (
                f"⚠️ 密采样守卫提醒：回合结束，盘中但 monitor_segment 未在跑（{why}）。"
                f"盯盘期间必须保持 monitor_segment 40 秒密采样循环（不得擅自停/降频，2026-08-04 教训）。"
                f"请立即重启 monitor_segment 恢复密盯，或确认已到停盯边界（港股 12:00/16:00、用户喊停）。"
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
