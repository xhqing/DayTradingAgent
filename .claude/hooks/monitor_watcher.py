#!/usr/bin/env python3
"""密采样守护 watcher（2026-08-04 立；2026-08-10 撤销；2026-08-11 按会话 + 60 秒判据恢复）。

由 launchd 周期（每 120 秒）触发，独立于 Claude Code session（AI 停了密采样 / session
不在场，watcher 仍能通过系统通知叫醒用户）——外部监督的工程化辅助。

判定逻辑（2026-08-11 用户立，按会话独立守护）：
  盘中对**每个已注册的盯盘会话**独立判定：
    - 该会话 jsonl（~/.claude/projects/<项目>/<session_id>.jsonl）停更 ≤ 60 秒 → 正常
    - 停更 > 60 秒 → 该会话中断 → 系统通知（合并所有中断会话为一条）

  为什么按会话 + 只认 jsonl 停更、不再查采样进程：
  - **按会话**（2026-08-11 用户立）：盯盘会话可能不止一个（auto/signal 并行），必须每个会话
    独立守护——会话 A 中断不能被会话 B 的活跃掩盖。会话在启动盯盘时注册
    （scripts/monitor_register.sh 写 CLAUDE_CODE_SESSION_ID 到 tmp/monitor_sessions.txt）、
    停盯时注销（scripts/monitor_unregister.sh），watcher 只检查注册列表里的会话。
  - **60 秒阈值**（2026-08-11 用户立）：采样一段 40 秒，正常段间循环里会话无输出的最长
    时长 ≈ 采样 40 秒（AI 重启采样后回合结束、等待段结束通知唤醒），给 20 秒冗余 →
    超过 60 秒无输出 = 大概率中断（段结束通知失效 / AI 忘重启 / 会话死）。原来 180 秒
    太宽，真中断要 3 分钟才报。
  - **不查采样进程**：采样进程不区分会话（无法关联到具体会话）；且「jsonl 停更 > 60 秒」
    已覆盖全部中断形态——AI 忘重启采样 / 通知失效 / 会话死都表现为 jsonl 停更；连跑多段
    （违规降频，段间 AI 不输出）也会因 jsonl 停更 > 60 秒被报。采样在跑的正常段间
    jsonl 停更必然 < 40 秒、不会误报。

  多会话取舍：注册列表里的会话各自独立判定，中断只报中断的、活跃的不报；未注册的会话
  不受守护（用户主动停盯的正常路径 = AI 停盯收尾时注销，不再被检查、不误报）。

局限（诚实）：watcher 只检查 + 通知，不自动重启采样（重启需当前盯盘标的 / 关键位参数、
每次不同、watcher 无法知道）；注册 / 注销依赖会话内 AI 执行（启动盯盘注册、停盯注销），
忘注册 = 该会话不受守护（漏报，用户监督兜底）、忘注销 = 停盯后多报一次通知（可接受）。

部署（macOS launchd）：
  1. 复制 .claude/hooks/com.daytrading.monitor-watcher.plist 到 ~/Library/LaunchAgents/
  2. launchctl load ~/Library/LaunchAgents/com.daytrading.monitor-watcher.plist
  卸载：launchctl unload ...；盘外 watcher 自动跳过（in_trading_session 判断）。
手动测试：python3 .claude/hooks/monitor_watcher.py（盘外直接退出、无输出）
"""
import os
import time
import subprocess
import datetime

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
# ~/.claude/projects/<slug>/ 的 slug 生成规则：路径去斜杠、段间用 - 连接、前加 -
_SLUG = "-" + os.path.abspath(_PROJECT_ROOT).strip(os.sep).replace(os.sep, "-")
TRANSCRIPT_DIR = os.path.expanduser(os.path.join("~/.claude/projects", _SLUG))

# 注册文件：每行一个盯盘会话的 CLAUDE_CODE_SESSION_ID（盯盘启动注册、停盯注销）
REG_FILE = os.path.join(_PROJECT_ROOT, "tmp", "monitor_sessions.txt")

# 会话 jsonl 停更多久视为中断。参数依据（2026-08-11 用户纠正 + 实测）：
#   「无输出内容的时间间隔」= 采样段运行的时长（采样脚本跑 40s 期间 AI 回合结束、jsonl 停更、
#   无输出）——正常无输出间隔最少 ≈ 40s（采样段 40s + 段启动/结束开销）。实测 log 段间
#   gap 6-25s 是「分析时间」（段结束 → AI 分析 → 重启采样，jsonl 活跃），**不是**无输出间隔，
#   不能用作阈值依据（2026-08-11 用户纠正：25s 是最大分析时间，不是无输出时间间隔）。
#   判定阈值取 50s（> 正常无输出 40s+，留 ~10s 余量防误报正常采样段）。
#   检查间隔（launchd StartInterval）取 10s → 最坏检测延迟 = 阈值 + 间隔 = 50 + 10 = 60s，
#   恰好满足用户要求「真实中断 ≤ 60s」（2026-08-11 用户立：真实中断时间不要超过 60s）。
STALE_SECONDS = 50


def in_trading_session():
    """**仅港股盘中**（09:30-12:00 / 13:00-16:00，周一至周五）。

    2026-08-11 用户立：美股盘中不守护——美股时段（夏令时北京 21:30-次日 04:00）用户
    在休息、不希望被打扰，watcher 不发通知。故只检查港股时段。
    """
    now = datetime.datetime.now()
    if now.weekday() >= 5:  # 周末
        return False
    t = now.time()
    return (datetime.time(9, 30) <= t < datetime.time(12, 0)) or \
           (datetime.time(13, 0) <= t < datetime.time(16, 0))


def registered_sessions():
    """读注册文件，返回 session id 列表；无注册文件 / 空 → []。"""
    if not os.path.isfile(REG_FILE):
        return []
    try:
        with open(REG_FILE) as f:
            return [line.strip() for line in f if line.strip()]
    except OSError:
        return []


def stale_sessions(sessions):
    """对每个注册会话算 jsonl 停更秒数，返回 [(session_id, 停更秒数), ...]（停更 > STALE_SECONDS 的）。"""
    stale = []
    for sid in sessions:
        p = os.path.join(TRANSCRIPT_DIR, sid + ".jsonl")
        if not os.path.isfile(p):
            continue  # 会话文件不存在（异常）→ 跳过、不误报
        age = time.time() - os.path.getmtime(p)
        if age > STALE_SECONDS:
            stale.append((sid, int(age)))
    return stale


def notify(msg):
    """发 macOS 系统通知（独立于 Claude Code，用户能看到）。"""
    try:
        subprocess.run(
            ["osascript", "-e",
             f'display notification "{msg}" with title "DayTradingAgent 密采样守护" sound name "Basso"'],
            timeout=5,
        )
    except Exception:
        pass


def main():
    if not in_trading_session():
        return  # 盘外不检查
    sessions = registered_sessions()
    if not sessions:
        return  # 无注册盯盘会话，不检查
    stale = stale_sessions(sessions)
    if not stale:
        return
    parts = [f"{sid[:8]}…{age}s" for sid, age in stale]
    notify("密采样中断（会话 " + ", ".join(parts) + "）：jsonl 停更 > 60 秒、无输出（等待唤醒中）。"
           "请回 Claude Code 盯盘会话唤醒 AI 并重启采样。")


if __name__ == "__main__":
    main()
