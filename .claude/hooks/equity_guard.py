#!/usr/bin/env python3
"""实盘净值守卫 hook（2026-08-20 立，PreToolUse：Write / Edit / NotebookEdit）。

⚠️⚠️ 已退役（2026-08-29 用户裁定，勿再注册）⚠️⚠️
2026-08-29 用户裁定：实盘账户净值不属敏感财务信息（账户资金只是个人全部财产的
一小部分、不能代表个人财务状况），照常可写入被跟踪文件——「实盘净值禁止写入被
跟踪文件」旧规废止，本 hook 随之从 settings.json 摘除、基准黑名单机制
（equity-baseline.local 自动维护）一并停用（见 CLAUDE.md「实盘净值不属敏感信息」
节与 CHANGELOG 2026-08-29 条）。本文件保留仅作沿革记录，勿再挂回 hooks——挂回
会拦正常记录。账户号打码、密钥、节点 IP 等其它敏感类别的防护（secret_guard 等）
不受本次裁定影响、照旧生效。

为什么（历史背景，2026-08-20 立规时的原文）：本仓库开源，实盘账户的资产净值
（net_liquidation / 总资产 / 购买力）曾被认定为敏感财务信息，不得写入任何被 git
跟踪的文件（CLAUDE.md「实盘净值禁止写入被跟踪文件」节）。存量已多次泄漏
（CHANGELOG / SKILL.md / actions/ 把实测净值写进了正文），散文规定靠 AI 记忆会
衰减（同 secret_guard 的教训），本 hook 在写入关卡补机械拦截。

拦什么（满足其一即拦）：
1. 目标文件未被 .gitignore 忽略（会被 git 跟踪），且写入内容命中基准黑名单里的
   实盘净值特征串（equity-baseline.local 维护，脚本查询实盘净值时自动追加）；
2. 写入内容含「实盘净值 / 总资产」与具体数字同现的形态（如「实盘净值 116,xxx」）。

基准黑名单（.claude/hooks/equity-baseline.local，本机文件、已 gitignore）：
  - 每行一个特征串（净值数字的多种写法：带逗号 / 不带逗号 / 保留两位）；
  - preflight / resume / 任何脚本以 --account live 查询到实盘净值时自动 append，
    保证「刚查到的最新净值」必然在黑名单里（当前值拦得住、不用人工维护）；
  - 权益重设类的「权益重设（总资产改为 X）」也涵盖（X 在黑名单即拦）。

放什么（不误伤正常链路）：
  - 写入 .gitignore 忽略的文件（equity-log.csv、config.json、accounts.json 等）——
    本机敏感数据本来就该写那里；
  - 只读操作（本 hook 只挂 PreToolUse 的写类工具，读不经过这里）；
  - 净值数字本身不在黑名单里的内容（黑名单没覆盖的新值拦不住——已知局限，
    靠「脚本查到即自动追加基准」把最新值补进黑名单来兜底）。

局限（与 secret_guard 同口径的诚实声明）：黑名单特征串匹配不是语义识别——AI 若
把净值拆成算式或取整写成量级概述可绕过。本 hook 是抬高成本 +
兜底拦截，不是 100% 银弹；第一道防线仍是「写之前想到规定」。

用法（settings.json hooks 注册）：
  PreToolUse matcher Write|Edit|NotebookEdit → python3 .claude/hooks/equity_guard.py
hook 接收 stdin JSON（tool_name / tool_input），exit 0 放行 / exit 2 阻断 + stderr 提醒。
"""
import json
import os
import subprocess
import sys

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..'))
_BASELINE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'equity-baseline.local')


def _load_baseline():
    """读基准黑名单（每行一个特征串；# 开头注释行忽略；文件不存在返回空表）。"""
    if not os.path.exists(_BASELINE_PATH):
        return []
    out = []
    with open(_BASELINE_PATH, encoding='utf-8') as f:
        for line in f:
            s = line.strip()
            if s and not s.startswith('#'):
                out.append(s)
    return out


def _git_ignored(path):
    """目标文件是否被 .gitignore 忽略（忽略 = 本机敏感数据文件，放行）。"""
    if not path:
        return False
    try:
        r = subprocess.run(['git', 'check-ignore', '-q', path],
                           cwd=_PROJECT_ROOT, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
        return r.returncode == 0
    except Exception:
        return False   # git 不可用时不放行本条（保守：按未忽略处理）


def _hit_baseline(content, baseline):
    for s in baseline:
        if s and s in content:
            return s
    return None


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)   # 解析失败放行（hook 不该因自身问题卡死流程）
    tool_input = data.get('tool_input') or {}
    file_path = tool_input.get('file_path') or tool_input.get('notebook_path') or ''
    content = tool_input.get('content') or tool_input.get('new_string') or ''
    if not file_path or not content:
        sys.exit(0)
    if _git_ignored(file_path):
        sys.exit(0)
    hit = _hit_baseline(content, baseline=_load_baseline())
    if hit:
        sys.stderr.write(
            "⛔ 实盘净值守卫：本次写入的文件会被 git 跟踪，内容命中实盘净值特征串"
            f"（{hit}）。本仓库开源，实盘账户净值 / 总资产 / 购买力禁止写入任何被跟踪文件——"
            "改用打码口径（量级描述也不写——财务状况描述一律禁写，只写占位符或「见本机实查」），精确值只写进被 .gitignore "
            "忽略的本机文件（如 signals/equity-log.csv）。\n")
        sys.exit(2)
    sys.exit(0)


if __name__ == '__main__':
    main()
