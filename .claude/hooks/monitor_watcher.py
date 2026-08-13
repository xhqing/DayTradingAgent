#!/usr/bin/env python3
"""密采样守护 watcher（2026-08-04 立；2026-08-10 撤销；2026-08-11 按会话 + 60 秒判据恢复；
2026-08-12 改为「采样在跑就不报 + 死会话自动剔除」）。

由 launchd 周期（每 10 秒）触发，独立于 Claude Code session（AI 停了密采样 / session
不在场，watcher 仍能通过系统通知叫醒用户）——外部监督的工程化辅助。

判定逻辑（2026-08-12 用户立：盯盘窗口在 Running 且后台采样脚本在正常采样 → 不报，
两者都不活跃才报；「会话在 Running」用补丁 012 写盘的 state 文件判定，不用阈值猜）：
  盘中对**每个已注册的盯盘会话**独立判定，优先级如下：
    1. **死会话自动剔除（兜底自愈）**：会话 jsonl 停更 > DEAD_SESSION_SECONDS（30 分钟）
       → 视作已结束（停盯没注销、美股会话残留等），自动从注册列表移除、不再报。
    2. **采样在跑 → 正常**：任一密采样脚本（monitor_segment.py / ws_segment.py /
       futu_ws_segment.py）进程存活 → 说明有会话正在采样、盯盘窗口正常 Running
       → 本轮所有注册会话都判正常、一律不报。
    3. **采样没在跑 → 读会话 state 文件判定会话是否在 Running**：
       - state = running / thinking → 会话真在 Running、**不报**；
       - state = waiting_input / idle → 会话没在 Running（等输入 / 空闲）、**报中断**；
       - state 文件不存在 / 过期（补丁 012 未生效）→ fallback 到 jsonl 停更 > STALE_SECONDS。

  为什么用「采样在跑 + 会话 Running 标签（state 文件）」双判据（2026-08-12 用户立）：
  - 旧逻辑只认 jsonl 停更阈值，但盯盘窗口 Running 时 AI 回合正卡在采样脚本的 Bash 调用里
    ——采样段跑 40 秒期间 jsonl 完全不更新，阈值踩在采样段尾端、某段跑到 51s 就误报。
  - 采样在跑 = 盯盘回合里最长工具调用存活 = 窗口在正常 Running 的铁证，绕开「采样期间
    jsonl 停更」的固有盲区——采样段期间（jsonl 停更）靠这条挡住误报。
  - 会话 Running 标签（state 文件）= 会话是否真在响应（非等输入 / 非空闲）的地面真相，
    绕开「靠 jsonl 停更猜会话死活」——采样没在跑的段间 / 分析窗口，靠这条区分「会话还在
    Running（不报）」与「会话也没在 Running（报）」。补丁 002 的 Running 标签即源自此 state
    （busy=state!="idle" && pendingInput=state=="waiting_input"，真 Running = running/thinking）；
    补丁 012 把 state 写盘，watcher 读它 = 读会话 Running 标签的地面真相。

  为什么 state 文件缺失要 fallback 到 jsonl（过渡期兼容）：
  - 补丁 012 应用到 extension.js 后，**会话需重载扩展才生效**（旧会话用的旧 extension.js
    不写盘）。在所有盯盘会话重载前，state 文件不存在 —— 这时若「无 state 就报」会在采样
    没跑的段间窗口误报。故 fallback 到 jsonl 停更 > STALE_SECONDS（90 秒）：过渡期靠 jsonl
    兜底（段间正常窗口 < 90s 不报），012 生效后自然切到精确的 state 判定。
  - STALE_SECONDS 取 90 秒：仅作 fallback 用（012 生效后基本不走这条），正常分析阶段 +
    段间重启 + 工具调用开销 < 90s，> 90s 才视作中断。

  多会话取舍：注册列表里的会话各自独立判定（先统一过死会话剔除），中断只报中断的、
  活跃的不报；未注册的会话不受守护（用户主动停盯的正常路径 = AI 停盯收尾时注销，
  不再被检查、不误报）。

局限（诚实）：watcher 只检查 + 通知，不自动重启采样（重启需当前盯盘标的 / 关键位参数、
每次不同、watcher 无法知道）；注册 / 注销依赖会话内 AI 执行（启动盯盘注册、停盯注销），
忘注册 = 该会话不受守护（漏报，用户监督兜底）；死会话自动剔除兜底了忘注销的残留。

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

# 会话实时 state 文件目录（PatchClaudeAgent 补丁 012 写盘）：每个会话 state 变化时写
# ~/.claude/session_running/<sessionId>.txt，内容 = state 字符串（running/thinking/waiting_input/idle）。
# watcher 读它判定会话是否真在 Running（非 idle、非 waiting_input）——这是会话 Running 标签的
# 地面真相（补丁 002 的 busy/pendingInput 即源自此 state）。补丁 012 经 PatchClaudeAgent
# apply 引擎应用到本机 extension.js 的 updateSessionState，会话重载扩展后生效。
STATE_DIR = os.path.expanduser("~/.claude/session_running")

# state 文件多久视为过期（补丁每次 state 变化都覆盖写，正常 Running 会话几十秒内必然有
# state 刷新——AI 回合响应会触发 running→thinking→waiting_input 等切换）。文件停更超过此值
# = 补丁没在写 = 会话已退出 / 扩展崩，视作无 state（fallback 到 jsonl 判定）。
STATE_STALE_SECONDS = 120

# 会话 jsonl 停更多久视作已结束（死会话自动剔除阈值）。参数依据（2026-08-12 立）：
#   盯盘段长 40 秒、正常段间循环 jsonl 停更必然 < 90 秒（见 STALE_SECONDS）；远大于此的
#   停更 = 会话已停盯没注销 / 美股会话残留 / 会话崩溃后没清理。这类死会话留在注册列表里
#   会让 watcher 永远报「中断」（旧逻辑根因之一），故超此阈值自动从注册列表移除、不再报。
#   30 分钟远大于 90 秒中断阈值、绝不会误删正常长段；真正结束的会话 jsonl 不会再生效，
#   删掉安全。
DEAD_SESSION_SECONDS = 30 * 60

# 采样没在跑时，会话 jsonl 停更多久视作中断。参数依据（2026-08-12 用户纠正后放宽）：
#   旧值 50s 是在「只认 jsonl」逻辑下为压住采样段（40s）设的——阈值踩刀刃、易误报。
#   新逻辑下「采样在跑就不报」已盖住采样段期间，本阈值只在「采样没在跑」（分析阶段 /
#   段间重启 / 其它工具调用）时生效，故可放宽到 90 秒：正常分析 + 重启 + 工具开销 < 90s，
#   > 90s 采样既没跑、jsonl 也没更新 = 确认中断。检查间隔 10s → 最坏检测延迟 100s。
STALE_SECONDS = 90


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


def sampling_running():
    """查是否有密采样脚本进程存活（任一采样在跑 → 盯盘正常 Running、不报）。

    检查三个密采样入口：monitor_segment.py（富途快照）/ ws_segment.py（老虎 WebSocket）/
    futu_ws_segment.py（富途 WebSocket）。任一存活即返回 True。
    用 pgrep -f 按脚本名匹配命令行（python3 .../monitor_segment.py）。
    """
    for script in ("monitor_segment.py", "ws_segment.py", "futu_ws_segment.py"):
        try:
            r = subprocess.run(["pgrep", "-f", script], stdout=subprocess.DEVNULL)
            if r.returncode == 0:
                return True
        except Exception:
            pass
    return False


def session_state(sid):
    """读单个会话的实时 state（补丁 012 写盘）。返回 state 字符串或 None。

    返回值语义：
    - "running" / "thinking"：会话真在 Running（AI 响应中），非中断。
    - "waiting_input"：会话等输入（窗口活着、Idle）——按用户立意「没在 Running」算中断。
    - "idle"：会话空闲——算中断。
    - None：state 文件不存在（补丁 012 未生效，如盯盘会话用的旧 extension.js）或文件过期
      （> STATE_STALE_SECONDS 没刷新 = 会话已退 / 扩展崩）。调用方需 fallback。

    为什么用 state 文件作「会话是否在 Running」的判据（2026-08-12 用户立）：
    补丁 002 的 Running 标签源自 busy=state!="idle" && pendingInput=state=="waiting_input"，
    真 Running = busy && !pendingInput = state 是 running/thinking。但该状态只在 extension
    进程内存、原不写盘、外部读不到（见 patch 012 背景）。补丁 012 把 state 写到
    ~/.claude/session_running/<sid>.txt，watcher 读它 = 读会话 Running 标签的地面真相，
    彻底替代「靠 jsonl 停更阈值猜会话死活」的旧逻辑（旧逻辑在采样段间 jsonl 停更时误报）。
    """
    p = os.path.join(STATE_DIR, sid + ".txt")
    if not os.path.isfile(p):
        return None
    if time.time() - os.path.getmtime(p) > STATE_STALE_SECONDS:
        return None  # 文件过期 = 补丁没在写 = 无有效 state
    try:
        with open(p) as f:
            return f.read().strip()
    except OSError:
        return None


def prune_dead_sessions(sessions):
    """剔除死会话：jsonl 停更 > DEAD_SESSION_SECONDS 的，从注册文件移除并返回剩余活跃会话。

    停盯没注销 / 美股会话残留 / 会话崩溃没清理，都会让死 session id 永久留在注册列表里，
    导致 watcher 一直报中断。超 DEAD_SESSION_SECONDS（30 分钟）= 确定已结束，自动移除。
    jsonl 文件不存在的会话也一并剔除（会话被删 / 异常）。
    返回仍受守护的活跃会话列表（停更 ≤ DEAD_SESSION_SECONDS 的）。
    """
    alive = []
    dead = []
    for sid in sessions:
        p = os.path.join(TRANSCRIPT_DIR, sid + ".jsonl")
        if not os.path.isfile(p):
            dead.append(sid)  # 会话文件不存在 → 视作已结束
            continue
        age = time.time() - os.path.getmtime(p)
        if age > DEAD_SESSION_SECONDS:
            dead.append(sid)
        else:
            alive.append(sid)
    if dead:
        try:
            with open(REG_FILE) as f:
                all_lines = [line.strip() for line in f if line.strip()]
            kept = [s for s in all_lines if s not in set(dead)]
            with open(REG_FILE, "w") as f:
                f.write("\n".join(kept) + ("\n" if kept else ""))
        except OSError:
            pass
    return alive


def interrupted_sessions(sessions):
    """采样没在跑时，判定哪些会话真中断了。返回 [(sid, 原因), ...]。

    判定优先级（用户立：采样没在跑 + 会话没在 Running 才报）：
    1. 读会话 state 文件（补丁 012）：
       - running / thinking → 会话真在 Running，**不报**。
       - waiting_input / idle → 会话没在 Running（等输入 / 空闲），**报**。
    2. state 文件不存在 / 过期（补丁 012 未生效，如会话用旧 extension.js）→ fallback 到
       jsonl 停更 > STALE_SECONDS 判定（旧逻辑的保守回退，过渡期不致误报）。

    仅在采样没在跑时调用（采样在跑时主流程已提前 return）。
    """
    interrupted = []
    for sid in sessions:
        st = session_state(sid)
        if st in ("running", "thinking"):
            continue  # 会话真在 Running → 不报
        if st in ("waiting_input", "idle"):
            interrupted.append((sid, f"state={st}（没在 Running）"))
            continue
        # st is None：补丁 012 未生效 / 文件过期 → fallback jsonl 停更判定
        p = os.path.join(TRANSCRIPT_DIR, sid + ".jsonl")
        if not os.path.isfile(p):
            continue  # jsonl 也不在（异常）→ 跳过、不误报
        age = time.time() - os.path.getmtime(p)
        if age > STALE_SECONDS:
            interrupted.append((sid, f"jsonl 停更 {int(age)}s（state 文件无，fallback）"))
    return interrupted


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

    # ① 先剔除死会话（jsonl 停更 > 30 分钟 = 已结束），自愈残留。
    sessions = prune_dead_sessions(sessions)
    if not sessions:
        return  # 剔除后无活跃会话

    # ② 采样在跑 → 盯盘窗口正常 Running、本轮一律不报（采样 Bash 是盯盘最长工具调用、
    #    它活着 = 一切正常；绕开「采样期间 jsonl 停更」盲区）。
    if sampling_running():
        return

    # ③ 采样没在跑 → 读会话 state 文件（补丁 012 写盘）判定会话是否在 Running：
    #    running/thinking = 在 Running 不报；waiting_input/idle = 没在 Running 报中断；
    #    state 文件无（012 未生效）→ fallback jsonl 停更判定。
    interrupted = interrupted_sessions(sessions)
    if not interrupted:
        return
    parts = [f"{sid[:8]}（{reason}）" for sid, reason in interrupted]
    notify("密采样中断（会话 " + ", ".join(parts) + "）：采样进程不在、会话也没在 Running。"
           "请回 Claude Code 盯盘会话唤醒 AI 并重启采样。")


if __name__ == "__main__":
    main()
