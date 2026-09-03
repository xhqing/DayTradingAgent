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

用法（hooks 注册，2026-09-02 起双宿主：CC settings.json + ZCode .zcode/config.json 同挂）：
  PostToolUse matcher "Edit|Write" → python3 .claude/hooks/changelog_guard.py
hook 接收 stdin JSON（tool_name / tool_input / tool_response，两宿主字段同名），exit 0，
stdout 为 JSON hookSpecificOutput{hookEventName:"PostToolUse", additionalContext: 提醒文本}
（CC / ZCode 的 PostToolUse 均注入对话，两宿主行为一致）。
"""
import json
import sys
from pathlib import Path

# 运行时数据 / 敏感配置目录：这些不用记 CHANGELOG
_EXCLUDE_DIRS = {'tmp', 'signals', 'actions', 'reviews', 'archive', 'cache'}
# 敏感 / 自动维护文件：不提醒（accounts.json 被 gitignore、CHANGELOG 本身、VERSION）
_EXCLUDE_NAMES = {'accounts.json', 'CHANGELOG.md', 'VERSION', '.commit-cache.md'}
_EXCLUDE_SUFFIXES = ('.local', '.local.json', '.local.yaml', '.local.yml')

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
# ⚠️ 三层 parent：hooks/ → .claude/ → 项目根（2026-09-03 修 T138 验证时发现的 bug）。
# 原来只写两层 = 算到 .claude/，项目根下的文件 relative_to 全部抛 ValueError → 一律不提醒，
# 即本 hook 自 2026-09-02 上线以来对项目根 / 非 .claude 路径的文件从未真正触发过。


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


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0
    tool_input = data.get('tool_input', {})
    file_path = tool_input.get('file_path') or tool_input.get('filePath') or ''
    # 2026-09-03 T138：CodeBuddy IDE 宿主的 replace_in_file / write_to_file 用驼峰
    # filePath（CC 的 Edit / Write 用蛇形 file_path），两写法都收、跨宿主单源兼容。
    if not file_path:
        return 0
    if _should_remind(file_path):
        # 输出形态（2026-09-02 双宿主适配）：stdout JSON hookSpecificOutput——两宿主官方
        # 支持的注入通道（CC 源码实证 PostToolUse 的 hookSpecificOutput.additionalContext
        # 会注入模型上下文；ZCode schema 同构支持）。原纯文本 print 在 ZCode 下被静默丢弃
        # （exit 0 只解析 "{" 开头的 stdout），在 CC 下也只进 transcript 不注入——统一改
        # 标准形态，两宿主行为一致且均为「提醒真正到达 AI」。
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext":
                    f'📌 CHANGELOG 记录提醒（工具强制）：本次改动 {file_path} 属 git 跟踪的代码/文档，'
                    f'按「CHANGELOG 记录纪律」（2026-07-30 立、2026-08-21 修订：记录不用问）请顺手记入'
                    f' CHANGELOG.md（为什么改 + 改了什么），不要问用户「要不要记」。若本次改动属于'
                    f'已记条目（同批改动）可忽略本提醒。'
            }
        }, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
