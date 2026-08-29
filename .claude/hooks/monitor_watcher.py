#!/usr/bin/env python3
"""密采样守护 watcher（2026-08-04 立；2026-08-10 撤销；2026-08-11 恢复；2026-08-12 判定
逻辑重构；2026-08-17 补空转检查；2026-08-20 用户立砍掉「中断警报 + 掩蔽提示」两个功能；
2026-08-21 用户立删除空转警报功能——现仅剩「老虎 IP 白名单漂移检测」一类提醒）。

由 launchd 周期（每 10 秒）触发，独立于 Claude Code session（AI 停了密采样 / session
不在场，watcher 仍能通过系统通知叫醒用户）——外部监督的工程化辅助。

**2026-08-21 空转警报删除（用户立）**：原「采样在跑但分析心跳停更 ≥180 秒 → 弹窗报警」
的空转检测（2026-08-17 立）整个移除——含空转检查逻辑（analysis_beat 停更检查 / 无心跳
兜底）、相关通知冷却适配、注释与文件头描述同步清理。删除后 watcher 的盯盘类提醒全部
退场，仅存一个功能：

  1. **老虎 IP 白名单漂移检测（2026-08-19 立，唯一保留）**：调 proxy_guard.py——当前节点
     出口漂出白名单时自动切回白名单内节点（恢复服务）并响铃弹窗给加白 IP 串。

原「死会话自动剔除」「state 文件 / jsonl 会话活跃度判定」等逻辑随中断警报一并删除——
watcher 不再读会话注册表、不再逐会话判定。monitor_sessions.txt 注册文件仍保留（caffeinate
引用计数 off.sh 与 preflight 的「在场会话数」打印仍在用，与 watcher 无关）。

局限（诚实）：watcher 只检查 + 通知，不自动重启采样（重启需当前盯盘标的 / 关键位参数、
每次不同、watcher 无法知道）。

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

# 同一类事件的通知冷却（秒，2026-08-16 立；2026-08-20 中断警报删除后适用于空转警报与
# 白名单漂移提醒；2026-08-21 空转警报删除后仅适用于白名单漂移提醒）。背景：launchd
# 每 10 秒触发一次 watcher，条件持续成立时旧实现每轮都发 macOS 通知 = 每分钟 6 条通知
# 风暴。冷却期内不重复报同一类事件；冷却文件写在 tmp/ 下（mtime 即上次通知时间，
# 内容为原因摘要）。
NOTIFY_COOLDOWN_SECONDS = 300   # 同一类事件 5 分钟内不重复报


def in_trading_session():
    """**仅港股盘中**（09:30-12:00 / 13:00-16:00，周一至周五）。

    2026-08-11 用户立：美股时段不检查——美股时段（夏令时北京 21:30-次日 04:00）用户
    在休息、不希望被打扰，watcher 不发通知。故只检查港股时段。
    """
    now = datetime.datetime.now()
    if now.weekday() >= 5:  # 周末
        return False
    t = now.time()
    return (datetime.time(9, 30) <= t < datetime.time(12, 0)) or \
           (datetime.time(13, 0) <= t < datetime.time(16, 0))


def notify(msg, key=None):
    """发 macOS 系统通知（独立于 Claude Code，用户能看到）。

    2026-08-16 立通知冷却：launchd 每 10 秒触发一次，条件持续成立时旧实现每轮都发通知
    = 每分钟 6 条通知风暴。key 给定时（形如 "<事件类别>"），冷却文件
    tmp/watcher_notify_<key>.stamp 的 mtime 距今 < NOTIFY_COOLDOWN_SECONDS 则跳过本次。

    2026-08-19 立持久通知日志：每次**实际发出**的通知追加写 tmp/watcher_notify.log
    （时间戳 + key + 消息全文，"a" 模式不覆盖）——背景：此前唯一留痕是 stamp 文件
    （"w" 模式写、同 key 再触发整体覆盖上一条）+ launchd 日志恒 0 字节，提醒历史
    不可追溯（复盘无从查「今天响了几次、各在几点」）。写盘失败静默忽略（与 stamp
    同容错，不影响发通知主路径）；stamp 冷却机制本身不动（其职责是冷却计时、不是日志）。

    2026-08-19 立强制确认弹窗（用户立「横幅 → 屏幕中心弹窗」）：display notification
    横幅数秒自动消失、易被忽略或被勿扰压制——有持仓时 = 漏移损/漏平仓风险。改为
    display dialog 屏幕中心弹窗 + 「知道了」按钮（用户点击才关），铃声 Basso 不变。
    阻塞处理：watcher 由 launchd 每 10 秒触发、display dialog 会阻塞到点击——
    带 `giving up after 30`（30 秒无操作自动关闭、视为一次已触达），subprocess
    timeout 调到 35 秒兜底；弹窗在冷却机制下最多 5 分钟一次，不叠加成窗口风暴。

    2026-08-21 立铃声去横幅（用户立「密采样守卫提醒不弹横幅」）：原铃声经
    `display notification "" sound name "Basso"` 发出——该调用在响铃的同时会弹一条
    空内容横幅，与「横幅 → 弹窗」初衷相悖（主提醒已由弹窗承载，横幅纯属多余）。
    改用 afplay 直接播放系统音（不产生任何横幅 / 通知），弹窗与持久日志照旧。"""
    if key is not None:
        import re as _re
        safe_key = _re.sub(r"[^A-Za-z0-9_-]", "_", key)
        stamp = os.path.join(_PROJECT_ROOT, "tmp", f"watcher_notify_{safe_key}.stamp")
        try:
            if os.path.isfile(stamp) and (time.time() - os.path.getmtime(stamp)) < NOTIFY_COOLDOWN_SECONDS:
                return   # 冷却期内，不重复报
        except OSError:
            pass
        try:
            os.makedirs(os.path.dirname(stamp), exist_ok=True)
            with open(stamp, "w") as f:
                f.write(msg[:200])
        except OSError:
            pass
    # 持久通知日志（追加、不覆盖；失败静默——见 docstring 2026-08-19 段）
    try:
        log_path = os.path.join(_PROJECT_ROOT, "tmp", "watcher_notify.log")
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(log_path, "a") as f:
            f.write(f"{ts}\t{key or '-'}\t{msg}\n")
    except OSError:
        pass
    try:
        # 弹窗文案转义（消息含双引号会破 AppleScript 字符串字面量）
        esc = msg.replace("\\", "\\\\").replace('"', '\\"')
        subprocess.run(
            ["osascript", "-e",
             f'display dialog "{esc}" with title "DayTradingAgent 密采样守护" '
             f'buttons {{"知道了"}} default button "知道了" giving up after 30 '
             f'with icon caution'],
            timeout=35,
        )
        # 铃声（弹窗弹出时响；2026-08-21 起改 afplay 直接播放——不弹横幅，见 docstring）
        subprocess.run(
            ["afplay", "/System/Library/Sounds/Basso.aiff"],
            timeout=5,
        )
    except Exception:
        pass


def check_proxy_whitelist():
    """老虎 IP 白名单漂移检测（2026-08-19 立，盘中兜底）：调 trade skill 的
    proxy_guard.py——当前节点出口漂出白名单时自动切回白名单内节点（恢复服务）并
    响铃弹窗给加白 IP 串；节点 IP 有变化（漂移）也弹窗提醒。preflight 启动时已跑一次，
    这里是盘中兜底（盯盘中途订阅刷新 / 节点漂移，AI 不在场也能自动处置）。

    为什么独立于盯盘会话状态：节点漂移影响的是网络层（老虎 API 全断），与「采样在不在
    跑」正交——无论盯盘状态如何都该检测。proxy_guard 自带
    5 分钟通知冷却，launchd 每 10 秒触发不会弹窗风暴。失败静默（检测链自身故障不应
    影响主守护流程）。
    """
    guard = os.path.join(_PROJECT_ROOT, ".claude", "skills", "trade", "scripts", "proxy_guard.py")
    try:
        subprocess.run(["python3", guard], capture_output=True, timeout=90)
    except Exception:
        pass


def main():
    if not in_trading_session():
        return  # 盘外不检查
    # 唯一保留的检查：白名单漂移检测 + 自动切换（2026-08-19 立；2026-08-21 空转警报
    # 删除后为 watcher 仅存功能，与盯盘会话状态无关）。
    check_proxy_whitelist()


if __name__ == "__main__":
    main()
