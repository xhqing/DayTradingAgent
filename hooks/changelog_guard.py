#!/usr/bin/env python3
"""CHANGELOG 记录提醒 hook（2026-09-02 立，PostToolUse Edit|Write）。

为什么：全局 CLAUDE.md「CHANGELOG 记录纪律」（2026-07-30 立、2026-08-21 修订「记录不用问」）
规定每次文件增删改查都要顺手记入 CHANGELOG、不先问用户。但散文规定在上下文压缩后会衰减
（2026-08-18 用户对照实验：工具在场全守住、靠 AI 记忆全失守）——2026-09-02 stability 撤销
涉及 4+ 文件改动时，AI 改到收尾把「记 CHANGELOG」搁置、还反问用户「要不要记」，正是该纪律
靠记忆执行的失守现场。本 hook 把「记得记 CHANGELOG」变成每次改代码/文档时的在场提醒。

怎么做：PostToolUse 检测到 Edit / Write 改动了 git 跟踪的代码或文档文件（排除运行时数据
tmp/ signals/ actions/ reviews/ archive/ cache/，排除敏感配置 *.local / accounts.json，排除
CHANGELOG.md / VERSION 本身），且 CHANGELOG.md 的 mtime 早于被改文件（说明该文件比 CHANGELOG
新、很可能还没被记）时，打印一行提醒「按 CHANGELOG 记录纪律顺手记入（不用问用户）」。

为什么是提醒不是硬拦：某次改动是否真的需要记（查操作不记、盯盘高频信号不逐条记、机制变更
必须记）依赖语义判断、无法机械判定「该记 vs 不必记」——故走「决策时刻工具在场打印」层
（项目方法论「文档规定必须尽可能配工具强制」第②层），只提醒不阻断，AI 看到后自行判断本次
是否需记。mtime 启发式是噪声信号：批内改多个文件时可能漏提醒或重复提醒，但提醒无害、AI
自行过滤（同批已记过就忽略）。

用法（由 pi 扩展 .pi/extensions/trade-guards.ts 在 edit / write 工具结果后 spawn）：
  python3 hooks/changelog_guard.py
hook 接收 stdin JSON（tool_name / tool_input / tool_response），exit 0，
stdout 为 JSON hookSpecificOutput{hookEventName:"PostToolUse", additionalContext: 提醒文本}，
由 pi 桥接解析并追加进工具结果（模型下一轮可见）。
"""
import json
import os
import re
import sys
from pathlib import Path

# 运行时数据 / 敏感配置目录：这些不用记 CHANGELOG
_EXCLUDE_DIRS = {'tmp', 'signals', 'actions', 'reviews', 'archive', 'cache'}
# 敏感 / 自动维护文件：不提醒（accounts.json 被 gitignore、CHANGELOG 本身、VERSION）
_EXCLUDE_NAMES = {'accounts.json', 'CHANGELOG.md', 'VERSION', '.commit-cache.md'}
_EXCLUDE_SUFFIXES = ('.local', '.local.json', '.local.yaml', '.local.yml')

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
# ⚠️ 两层 parent：hooks/ → 项目根（2026-11-05 迁到项目根 hooks/ 后由三层改为两层）。
# 历史教训（2026-09-03 修 T138 验证时发现）：曾经多写一层 = 算到某个子目录，
# 项目根下的文件 relative_to 全部抛 ValueError → 一律不提醒（hook 从未真正触发过）。


def _should_remind(file_path):
    """命中「该提醒记 CHANGELOG」的条件：git 跟踪的代码/文档 + CHANGELOG 比它旧。"""
    p = Path(file_path).resolve()
    try:
        rel = p.relative_to(_PROJECT_ROOT)
    except ValueError:
        return False  # 项目外文件不管
    if any(part in _EXCLUDE_DIRS for part in rel.parts):
        return False
    if rel.name in _EXCLUDE_NAMES:
        return False
    if rel.name.endswith(_EXCLUDE_SUFFIXES):
        return False
    if not p.exists():
        return False
    changelog = _PROJECT_ROOT / 'CHANGELOG.md'
    if not changelog.exists():
        return True  # 项目标配缺失 → 提醒（commit skill 也会自动新建）
    # CHANGELOG mtime 早于被改文件 → 该文件很可能还没被记入
    return changelog.stat().st_mtime < p.stat().st_mtime


def _todo_hygiene_issues(file_path):
    """TODO.md 卫生检测（2026-09-18 立，防「历史处理备注堆积 + 重复分节标题」复发）。

    背景：TODO.md 曾在头部与各分节间积累 7 处括号备注——历次批量归档 / 升降级待办时
    顺手留在 TODO.md 里的处理记录（其中头部一条被 7 个时间点续写成巨型段落），违反
    「TODO.md 只放未完成条目、已处理全部归档」的文件自身规矩；另出现过两个重复的 🟠
    分节标题（批量登记新条目时误加）。根因与 CHANGELOG 纪律失守同源：散文规定在长会话
    上下文压缩后衰减，AI 把处理摘要写进 TODO.md 而非（只）写 TODO-archive.md。本检测
    走「决策时刻工具在场打印」层：编辑 TODO.md 后立即扫描，命中即注入提醒让 AI 当场
    自纠（备注迁 TODO-archive.md、合并重复标题），提醒不阻断（与 CHANGELOG 提醒同层）。
    """
    p = Path(file_path).resolve()
    try:
        rel = p.relative_to(_PROJECT_ROOT)
    except ValueError:
        return []  # 项目外文件不管
    if rel.name not in ('TODO.md', 'TODO.local.md'):
        return []
    if not p.exists():
        return []
    issues = []
    lines = p.read_text(encoding='utf-8', errors='replace').splitlines()
    # ① 历史处理备注行：整行以全角「（」开头（典型形态「（2026-09-03 批量处理：…）」
    # 「（空——2026-09-01 …）」「（T4 已于 … 归档…）」）。正常条目以 - [ ] 开头、头部
    # 说明以 > 或 # 开头，均不以（开头，判定无误伤面。
    note_lines = [i for i, l in enumerate(lines, 1) if l.startswith('（')]
    if note_lines:
        shown = ', '.join(str(i) for i in note_lines[:5])
        more = ' 等' if len(note_lines) > 5 else ''
        issues.append(
            f'检测到 {len(note_lines)} 行以「（」开头的历史处理备注（第 {shown} 行{more}）——'
            f'TODO.md 只放未完成条目（`[ ]`），批量处理 / 归档 / 升降级的过程记录一律写'
            f' TODO-archive.md 对应归档节，不要在 TODO.md 留备注；请当场迁移后再继续'
        )
    # ② 重复紧急度分节标题：同一紧急度 emoji 的 `##` 标题出现 ≥2 次（2026-09-16 批量
    # 登记 T151-T154 时误加过第二个 🟠 标题，两节条目被人为隔断）。
    seen = {}
    for i, l in enumerate(lines, 1):
        m = re.match(r'^##+\s*(🔴|🟠|🟡|🟢)', l)
        if m:
            seen.setdefault(m.group(1), []).append(i)
    for emoji, at in seen.items():
        if len(at) > 1:
            issues.append(
                f'检测到 {emoji} 紧急度分节标题出现 {len(at)} 次'
                f'（第 {", ".join(str(x) for x in at)} 行）——每个紧急度只应有一个分节，'
                f'请合并重复标题下的条目到同一节后再继续'
            )
    return issues


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0
    tool_input = data.get('tool_input', {})
    file_path = tool_input.get('file_path') or '' 
    if not file_path:
        return 0
    reminders = []
    if _should_remind(file_path):
        reminders.append(
            f'📌 CHANGELOG 记录提醒（工具强制）：本次改动 {file_path} 属 git 跟踪的代码/文档，'
            f'按「CHANGELOG 记录纪律」（2026-07-30 立、2026-08-21 修订：记录不用问）请顺手记入'
            f' CHANGELOG.md（为什么改 + 改了什么），不要问用户「要不要记」。若本次改动属于'
            f'已记条目（同批改动）可忽略本提醒。'
        )
    # 2026-09-18 立：TODO.md 卫生检测（历史备注行 + 重复分节标题），编辑 TODO.md 后即时提醒。
    for issue in _todo_hygiene_issues(file_path):
        reminders.append(f'📌 TODO.md 卫生提醒（工具强制）：{issue}。')
    # 2026-10-24 立：复盘报告嵌图提醒——写 reviews/*review*.md 时提醒 7 张演化图必须
    # 以 ![]() 嵌在正文 + 收尾跑 check_review_images.py（用户当日重申「7 张序贯演化图
    # 需要放在复盘报告里面」；此前只在产物表列文件名、漏了嵌入）。
    _rp = str(file_path)
    if '/reviews/' in _rp and _rp.endswith('.md') and 'review' in os.path.basename(_rp):
        reminders.append(
            '📌 复盘报告嵌图提醒（工具强制，2026-10-24 用户立）：7 张序贯演化图必须以 '
            '`![说明](文件名.png)` 嵌在本报告正文里（bayes / ev / winrate / pg / psum40 / '
            'pg20 / psum40-20pct），**只在配套产物表里列 png 文件名不算交付**；写完跑 '
            '`python3 .pi/skills/trade/scripts/check_review_images.py <本报告路径>` '
            '校验（逐张比对文件存在 + 已嵌入），不通过不算复盘完成。'
        )
    if reminders:
        # 输出形态：stdout JSON hookSpecificOutput.additionalContext——pi 扩展桥
        # （.pi/extensions/lib/hooks.ts 的 additionalContext()）解析它并追加进工具结果。
        # 多条提醒合并进同一 additionalContext（2026-09-18 起 CHANGELOG 与 TODO 卫生两类可同时命中）。
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": '\n'.join(reminders)
            }
        }, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
