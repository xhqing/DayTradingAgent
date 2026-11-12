#!/usr/bin/env bash
# 安装 / 卸载「插电源提醒」定时任务（2026-11-12 立）。
#
# 这个脚本做两件 plist 模板做不到的事：把模板里写死的 Python 路径与项目根路径，换成
# **当前这台机器上实际的值**（MacBook 是 /opt/homebrew/bin/python3 与 /Users/xhq/...，
# macmini 是 /usr/local/bin/python3 与 /Users/mac/...），再加载进当前用户的 launchd 域。
# 重复执行是安全的：先卸载同名服务，再重新生成 plist 并加载。
#
#   bash hooks/install_power_reminder.sh             # 安装（或更新）并立即启用
#   bash hooks/install_power_reminder.sh --unload   # 卸载（plist 文件保留，便于恢复）
#
# 只应在常开的机器（macmini）上安装一次。发送标记与去重锁放在本机与 macmini 同步的
# 私有状态目录里，所以即便别处也装了同一个任务，正常也不会重复发；但同步有延迟，
# 同一分钟内两地同时触发仍可能各发一条，故仍以「只装一台」为准。

set -euo pipefail

LABEL="com.daytrading.power-reminder"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$ROOT/hooks/${LABEL}.plist"
DST="$HOME/Library/LaunchAgents/${LABEL}.plist"
DOMAIN="gui/$(id -u)"

if [[ "${1:-}" == "--unload" ]]; then
    launchctl bootout "${DOMAIN}/${LABEL}" 2>/dev/null || launchctl unload -w "$DST" 2>/dev/null || true
    echo "✅ 已卸载 ${LABEL}（plist 保留在 ${DST}）"
    exit 0
fi

if [[ ! -f "$SRC" ]]; then
    echo "⛔ 找不到 plist 模板：${SRC}" >&2
    exit 1
fi
PY="$(command -v python3)"
if [[ -z "$PY" ]]; then
    echo "⛔ 找不到 python3" >&2
    exit 1
fi

mkdir -p "$(dirname "$DST")"
python3 - "$SRC" "$DST" "$PY" "$ROOT" <<'PY'
import sys
src, dst, py, root = sys.argv[1:5]
with open(src, encoding="utf-8") as fh:
    text = fh.read()
text = text.replace("/opt/homebrew/bin/python3", py)
text = text.replace("/Users/xhq/Developer/trading-agent", root)
with open(dst, "w", encoding="utf-8") as fh:
    fh.write(text)
print(f"✅ 已生成 {dst}（python={py}，项目根={root}）")
PY

plutil -lint "$DST"

# 先卸载同名旧服务（不存在时忽略），再加载新的；bootstrap 失败时退回传统 load。
launchctl bootout "${DOMAIN}/${LABEL}" 2>/dev/null || true
if ! launchctl bootstrap "$DOMAIN" "$DST" 2>/dev/null; then
    launchctl load -w "$DST"
fi

if launchctl list | grep -q "$LABEL"; then
    echo "✅ ${LABEL} 已启用（每 60 秒巡检一次，美东 09:30 发一条；RunAtLoad 已立即跑一次）"
    echo "   日志：/tmp/daytrading-power-reminder.log 与 .err"
else
    echo "⚠️ 未在 launchctl list 里看到 ${LABEL}，请手工确认：launchctl bootstrap ${DOMAIN} ${DST}" >&2
    exit 1
fi
