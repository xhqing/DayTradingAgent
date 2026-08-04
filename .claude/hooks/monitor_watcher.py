#!/usr/bin/env python3
"""monitor_segment 外部守护 watcher（B6，2026-08-04 多层防护）。

由 launchd 周期（每 2 分钟）触发：盘中检查 monitor_segment 是否在跑，没跑则发 macOS 系统
通知提醒用户。**独立于 Claude Code session**——AI 停了密采样 / session 不在场，watcher 仍能
通过系统通知叫醒用户（外部监督的工程化辅助，弥补 hook 只在 session 内生效的局限）。

局限（诚实）：watcher 只检查 + 通知，不自动重启 monitor_segment（重启需当前盯盘标的/关键位
参数，每次不同、watcher 无法知道）；自动重启需 AI 启动时写「当前 targets 配置文件」+ watcher
读它重启，工程更重、待 TODO。当前 watcher = 检查 + 系统通知用户，由用户决定是否回 session 重启。

部署（macOS launchd）：
  1. 复制 .claude/hooks/com.daytrading.monitor-watcher.plist 到 ~/Library/LaunchAgents/
  2. 编辑 plist 里 <string>/路径/到/python3</string> 与项目路径（占位需替换）
  3. launchctl load ~/Library/LaunchAgents/com.daytrading.monitor-watcher.plist
  卸载：launchctl unload ...；盘外 watcher 自动跳过（in_trading_session 判断）。

手动测试：python3 .claude/hooks/monitor_watcher.py
"""
import subprocess
import datetime


def in_trading_session():
    """港股 09:30-12:00/13:00-16:00 或美股 21:30-04:00（夏令时，周一至周五）。"""
    now = datetime.datetime.now()
    if now.weekday() >= 5:  # 周末
        return False
    t = now.time()
    hk = (datetime.time(9, 30) <= t < datetime.time(12, 0)) or \
         (datetime.time(13, 0) <= t < datetime.time(16, 0))
    us = (t >= datetime.time(21, 30)) or (t < datetime.time(4, 0))  # 夏令时；冬令时 22:30-05:00
    return hk or us


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
    try:
        r = subprocess.run(
            ["pgrep", "-f", "monitor_segment.py"],
            capture_output=True, text=True, timeout=3,
        )
        if not r.stdout.strip():
            notify("盘中 monitor_segment 未在跑！密采样可能被停/降频，请检查 Claude Code 盯盘会话。")
    except Exception:
        pass


if __name__ == "__main__":
    main()
