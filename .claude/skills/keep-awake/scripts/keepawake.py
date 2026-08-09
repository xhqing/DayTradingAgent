#!/usr/bin/env python3
"""Windows 防系统睡眠常驻进程（caffeinate -s 的 Windows 等价物，2026-08-09 立）。

为什么：macOS 用 caffeinate -s（PreventSystemSleep assertion）防盯盘期间系统睡眠；
Windows 没有 caffeinate，等价做法是 P/Invoke 调用 kernel32 的 SetThreadExecutionState，
带 ES_CONTINUOUS | ES_SYSTEM_REQUIRED 标志，阻止系统进入自动睡眠
（效果等同 caffeinate -s 的 assertion；对「合盖即睡眠」这类电源设置动作同样有效）。

用法：
  python keepawake.py          # 常驻运行，直至进程被终止（Ctrl+C / off.sh 停止）

进程退出（正常/被杀）时 OS 自动回收该 assertion 的防睡眠效果，无需显式解除；
本脚本也在 finally 里显式解除（ES_CONTINUOUS）一次，双保险。

调用方：
  - keep-awake/scripts/on.sh（Windows 分支）启动本脚本（nohup 后台常驻）
  - keep-awake/scripts/off.sh（Windows 分支）按命令行匹配终止本脚本
  - trade/scripts/preflight.py（Windows 分支）用 sys.executable 启动本脚本
"""
import sys
import time

# SetThreadExecutionState 标志（kernel32）
ES_CONTINUOUS = 0x80000000       # 持续生效，直到下一次调用清除
ES_SYSTEM_REQUIRED = 0x00000001  # 阻止系统进入睡眠


def main():
    if sys.platform != "win32":
        print("keepawake.py 仅用于 Windows（macOS 用 caffeinate，无需本脚本）", file=sys.stderr)
        sys.exit(1)

    import ctypes
    kernel32 = ctypes.windll.kernel32
    if not kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED):
        print("⚠️ SetThreadExecutionState 调用失败，未能阻止系统睡眠", file=sys.stderr)
        sys.exit(1)
    print("🪟 防系统睡眠已启用（SetThreadExecutionState ES_SYSTEM_REQUIRED；进程退出即解除）", flush=True)
    try:
        while True:
            time.sleep(60)
            # 周期重申：正常情况下 ES_CONTINUOUS 持续有效，重申是防其它程序/设置清掉它
            kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
    except KeyboardInterrupt:
        pass
    finally:
        kernel32.SetThreadExecutionState(ES_CONTINUOUS)  # 解除防睡眠（进程正常退出时兜底）


if __name__ == "__main__":
    main()
