#!/usr/bin/env python3
"""盘中禁止拉取 JMS 订阅守卫（2026-10-30 用户立，工具强制）。

## 规矩（用户原话要点）

「盘中为交易时段，工具强制盘中禁止拉取 JMS 订阅信息，盘中只在本地 IP 池中做 fallback
 动作……只在两个特定时间段拉取 JMS 最新可用 IP 信息并更新本地可用 IP 池，这两个时间段
 分别是收到用户盯盘指令的时候和收盘后汇报总结之前。」

## 为什么要有这条硬拦

本地 IP 池里的 IP 全部来自 JMS 订阅缓存。盘中真正需要的是「某个 IP 请求失败就在池内换
一个端口」，**完全不需要重新拉订阅**。历史教训（2026-10-29）：订阅拉取卡住时，整条
采样链与下单链一起卡（每轮采样都会顺路调一次），盘中失去交易能力。

## 拦什么 / 放什么

拦（exit 2，命令不执行）：
  - 交易时段内运行 `jms_pool.py`（它天然是「拉订阅」的入口），且命令里没有 `--allow-intraday`；
  - 交易时段内运行 `ip_pool.py` 带 `--refresh` / `--multi-start` 这类会重建订阅池的动作。

放：
  - 两个允许窗口：`preflight.py`（盯盘启动）与 `stop_summary.py`（收盘前总结）——它们
    在进程内调 `jms_pool.refresh(force=True, allow_intraday=True)`，不经过命令行，天然不受拦；
  - 命令里显式带 `--allow-intraday`（人工排查用，用户自己敲）；
  - 非交易时段（盘外拉订阅是正常动作，比如盘后核对、改配置后验证）；
  - 只读类用法（`--multi-status` / `--status` 之类不联网的命令不拦）。

## 局限（诚实）

按命令文本判断，`cd .claude/skills/trade/scripts && python3 jms_pool.py` 这类相对路径写法
仍能命中（脚本名匹配）；但用 `python3 -c "import jms_pool; jms_pool.refresh()"` 这类写法绕得
过去——那不是给 AI 用的形状。**第二道防线在代码层**：`jms_pool.fetch_subscription()` 在
交易时段内本来就返回 `blocked_intraday`（一个网络请求都不发），本 hook 只是把它提到「看得见
的拦截」这一层。两层叠加，绕不过去。

用法（注册在 .claude/settings.json 的 PreToolUse matcher Bash，CC / ZCode / CodeBuddy 共用；
pi 由 .pi/extensions/trade-guards.ts 桥接同一个脚本）：
  python3 .claude/hooks/jms_guard.py pretool
"""
import json
import os
import re
import sys

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..'))
sys.path.insert(0, os.path.join(_PROJECT_ROOT, '.claude', 'skills', 'trade', 'scripts'))

# 会触发「拉订阅」的命令形状
JMS_FETCH_PATTERNS = [
    re.compile(r'\bjms_pool\.py\b'),
    re.compile(r'\bip_pool\.py\b.*--(refresh|multi-start)\b'),
]
# 不联网的子命令（放行）：只读状态查询
READONLY = re.compile(r'--(multi-status|status)\b')

MSG = (
    '⛔ [jms_guard] 拦截：盘中禁止拉取 JMS 订阅（2026-10-30 用户立）。'
    '盘中只用本地 IP 池，某个 IP 失败了就在池内换端口（ip_pool 的 fast_switch 会自动做）。'
    '订阅只在两个窗口刷新：① preflight（收到盯盘指令时）② stop_summary（收盘前写总结时），'
    '两者已在代码里带 allow_intraday=True。'
    '手工排查需要当场拉取时，命令里显式加 --allow-intraday。'
)


def main(argv):
    mode = argv[1] if len(argv) > 1 else 'pretool'
    if mode != 'pretool':
        return 0
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    cmd = ((payload.get('tool_input') or {}).get('command') or '')
    if not cmd:
        return 0
    if not any(p.search(cmd) for p in JMS_FETCH_PATTERNS):
        return 0
    if READONLY.search(cmd) or '--allow-intraday' in cmd:
        return 0
    try:
        import jms_pool
        in_sess, mkt = jms_pool.in_trading_session()
    except Exception:
        return 0          # 判定不了就不拦（fail-open，代码层还有一道）
    if not in_sess:
        return 0
    print(f'{MSG}\n  （当前处于 {mkt} 交易时段）', file=sys.stderr)
    return 2


if __name__ == '__main__':
    sys.exit(main(sys.argv))
