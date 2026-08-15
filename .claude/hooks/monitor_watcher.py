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
    3. **采样没在跑 → 读会话 state 文件内容判定会话是否在 Running**：
       - state = running / thinking → 会话 state 显示在 Running；但补丁 012 事件驱动写盘、
         会话进程崩溃后 state 会停在 running 永不更新，故再用 jsonl 活跃度兜一道：
         jsonl 停更 > RUNNING_SILENCE_SECONDS（5 分钟）→ 视作陈旧 running、**报中断**；否则
         jsonl 还在动 = 会话真活着、**不报**。
       - state = waiting_input / idle → 会话没在 Running（等输入 / 空闲）、**报中断**；
       - state 文件不存在（补丁 012 未生效）→ fallback 到 jsonl 停更 > STALE_SECONDS（90 秒）。

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

  为什么 state 文件只看内容、不看 mtime（2026-08-14 修复误报时改，关键）：
  - 补丁 012 的写盘是**事件驱动**——只在会话 state 真正切换（idle→running→thinking→
    waiting_input）时调用 updateSessionState 写盘。一个持续 running 的 AI 回合（采样结束→
    连续读日志/读 csv/分析/重启，期间 state 保持 running 不切换）里，这个方法根本不会再被
    调用，state 文件 mtime 冻结在「最后一次进入 running 的瞬间」、几分钟内必然老化。
  - 旧逻辑据 mtime > STATE_STALE_SECONDS（120s）把「内容写着 running、但 mtime 老化」的
    有效文件判成 None = 作废了最可靠的判定信号，fallback 到脆的 jsonl 停更阈值（90s）——
    于是每段采样之间的分析窗口（jsonl 短暂停更 > 90s）周期性误报「中断」（用户实测：会话
    明明在 Running、state 文件也写着 running，却一直收到中断通知）。
  - 正解：**内容才是地面真相**——state 文件写着 running/thinking 就算在 Running，不看 mtime；
    防「崩溃后 state 停在 running」的陈旧坑改用 jsonl 活跃度兜底（RUNNING_SILENCE_SECONDS，
    300s）：state=running 且 jsonl 长期不动才算中断。jsonl 是会话活动度的地面真相（每个 AI
    回合都追加事件、写得密），比 state mtime 可靠得多。

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

# state=running/thinking 时，允许会话 jsonl 最长静默多久仍视作「真在 Running」。
# 参数依据（2026-08-14 修复误报时立）：
#   补丁 012 的写盘是事件驱动——只在会话 state 真正切换（idle→running→thinking→waiting_input）
#   时调用 updateSessionState 写盘。一个持续 running 的 AI 回合（采样结束→连续读日志/读 csv/
#   分析/重启，期间 state 保持 running 不切换）里，这个方法根本不会再被调用，state 文件 mtime
#   冻结在「最后一次进入 running 的瞬间」、几分钟内必然老化。故 state 文件的 mtime 不能用来判
#   「会话是否还活着」——内容写着 running 才是地面真相。但为防「会话崩溃后 state 停在 running
#   永不更新」的陈旧坑，用 jsonl 活跃度做一道兜底：state=running 且 jsonl 也长期不动（> 此值）
#   = 会话进程已卡死/崩溃、running state 已陈旧，报中断。
#   取 300 秒（5 分钟）：远大于正常段间分析窗口（采样段 40s 期间靠 sampling_running 挡、段间
#   分析读文件+重启 jsonl 写得勤，正常 < 2~3min），又远小于死会话剔除 30min——会话真崩溃后
#   约 5 分钟内能报，不再死等到 30min 才被剔除。
RUNNING_SILENCE_SECONDS = 300

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
    - None：state 文件不存在（补丁 012 未生效，如盯盘会话用的旧 extension.js）。调用方需 fallback。

    为什么不再用 state 文件的 mtime 判过期（2026-08-14 修复误报时改）：
    补丁 012 的写盘是事件驱动——只在会话 state 真正切换（idle→running→thinking→waiting_input）
    时调用 updateSessionState 写盘。一个持续 running 的 AI 回合（采样结束→连续读日志/读 csv/
    分析/重启，期间 state 保持 running 不切换）里，这个方法根本不会再被调用，state 文件 mtime
    冻结在「最后一次进入 running 的瞬间」、几分钟内必然老化。旧逻辑据此（mtime > STATE_STALE_SECONDS）
    把「内容写着 running、但 mtime 老化」的有效文件判成 None = 作废了最可靠的判定信号，fallback
    到脆的 jsonl 停更阈值（90s），于是每段采样之间的分析窗口（jsonl 短暂停更 > 90s）周期性误报。
    正解：**内容才是地面真相**——state 文件写着 running/thinking 就算会话在 Running，不看 mtime；
    防「会话崩溃后 state 停在 running 永不更新」的陈旧坑改用 jsonl 活跃度兜底（见 interrupted_sessions
    的 RUNNING_SILENCE_SECONDS 检查），不再靠 mtime 猜。

    为什么用 state 文件内容作「会话是否在 Running」的判据（2026-08-12 用户立）：
    补丁 002 的 Running 标签源自 busy=state!="idle" && pendingInput=state=="waiting_input"，
    真 Running = busy && !pendingInput = state 是 running/thinking。但该状态只在 extension
    进程内存、原不写盘、外部读不到（见 patch 012 背景）。补丁 012 把 state 写到
    ~/.claude/session_running/<sid>.txt，watcher 读它 = 读会话 Running 标签的地面真相，
    彻底替代「靠 jsonl 停更阈值猜会话死活」的旧逻辑（旧逻辑在采样段间 jsonl 停更时误报）。
    """
    p = os.path.join(STATE_DIR, sid + ".txt")
    if not os.path.isfile(p):
        return None
    try:
        with open(p) as f:
            return f.read().strip()
    except OSError:
        return None


def jsonl_age(sid):
    """会话 jsonl 停更了多少秒。文件不存在返回 None。

    jsonl 是会话活动度的地面真相——AI 每个回合（读日志 / 分析 / 工具调用）都往 jsonl 追加事件，
    正常盯盘期间写得勤（采样段 40s 期间靠 sampling_running 挡、段间分析重启 jsonl 写得也密）。
    state 文件因补丁 012 事件驱动写盘、可能长时间不刷新（见 session_state 说明），故判会话是否
    真活着用 jsonl mtime 比 state mtime 可靠——这是「防崩溃后 state 停在 running 陈旧坑」的兜底信号。
    """
    p = os.path.join(TRANSCRIPT_DIR, sid + ".jsonl")
    if not os.path.isfile(p):
        return None
    try:
        return time.time() - os.path.getmtime(p)
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
        age = jsonl_age(sid)
        if age is None:
            dead.append(sid)  # 会话文件不存在 → 视作已结束
            continue
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
    1. 读会话 state 文件内容（补丁 012，只看内容、不看 mtime——见 session_state 说明）：
       - running / thinking → 会话 state 显示在 Running；但补丁 012 事件驱动写盘、崩溃后 state
         会停在 running 永不更新，故再用 jsonl 活跃度兜一道：jsonl 停更 > RUNNING_SILENCE_SECONDS
         → 视作陈旧 running（会话进程卡死 / 崩溃）、**报中断**；否则 jsonl 还在动 = 会话真活着、**不报**。
       - waiting_input / idle → 会话没在 Running（等输入 / 空闲），**报**。
    2. state 文件不存在（补丁 012 未生效，如会话用旧 extension.js）→ fallback 到
       jsonl 停更 > STALE_SECONDS 判定（旧逻辑的保守回退，过渡期不致误报）。

    仅在采样没在跑时调用（采样在跑时主流程已提前 return）。
    """
    interrupted = []
    for sid in sessions:
        st = session_state(sid)
        if st in ("running", "thinking"):
            # state 显示在 Running，但用 jsonl 活跃度兜底防「崩溃后 state 停在 running」陈旧坑
            # （补丁 012 事件驱动写盘，进程卡死后 state 文件内容不会变回 idle）。
            jage = jsonl_age(sid)
            if jage is None or jage > RUNNING_SILENCE_SECONDS:
                interrupted.append((sid, f"state={st} 但 jsonl 已停更 {int(jage) if jage else '?'}s（陈旧 running，疑似崩溃）"))
            # 否则 jsonl 还在动 = 会话真活着、不报
            continue
        if st in ("waiting_input", "idle"):
            interrupted.append((sid, f"state={st}（没在 Running）"))
            continue
        # st is None：补丁 012 未生效 → fallback jsonl 停更判定
        jage = jsonl_age(sid)
        if jage is None:
            continue  # jsonl 也不在（异常）→ 跳过、不误报
        if jage > STALE_SECONDS:
            interrupted.append((sid, f"jsonl 停更 {int(jage)}s（state 文件无，fallback）"))
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
