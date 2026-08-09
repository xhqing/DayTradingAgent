#!/bin/bash
# 跨平台 hook 命令 wrapper（2026-08-09 立，Windows 适配）。
#
# 为什么：settings.json 的 hooks 命令需要同时适配 macOS 与 Windows——
#   · 项目根不能写本机绝对路径（clone 到别的机器/平台即失效），Claude Code 对 hook
#     进程导出 CLAUDE_PROJECT_DIR 环境变量（macOS 与 Windows 的 Git Bash 都可用），
#     本 wrapper 用它定位项目根；
#   · Python 解释器命令名两平台不同（macOS 是 python3、Windows 的 Git for Windows
#     环境里是 python），本 wrapper 内部探测，hooks 配置本身不再写解释器名。
#
# 用法（settings.json hooks 里这样写）：
#   bash $CLAUDE_PROJECT_DIR/.claude/hooks/run_hook.sh .claude/hooks/monitor_guard.py pretool
#
# 前提：Windows 需安装 Git for Windows（安装时勾选 Add to PATH），Claude Code 的
# hook 才由 Git Bash 执行（无 Git Bash 时落 PowerShell，$CLAUDE_PROJECT_DIR 语法失效）。
set -euo pipefail

ROOT="${CLAUDE_PROJECT_DIR:-$(cd "$(dirname "$0")/../.." && pwd)}"
SCRIPT="${1:-}"
if [ -z "$SCRIPT" ]; then
  echo "用法: run_hook.sh <脚本相对路径> [hook 参数...]" >&2
  exit 1
fi
shift

# 探测解释器：python3（macOS/Linux）→ python（Windows Git Bash）；都没有则报错
PY="$(command -v python3 || command -v python || true)"
if [ -z "$PY" ]; then
  echo "run_hook.sh: 找不到 python3 / python 解释器（macOS 需 python3、Windows 需 Python 3 并加入 PATH）" >&2
  exit 1
fi

exec "$PY" "$ROOT/$SCRIPT" "$@"
