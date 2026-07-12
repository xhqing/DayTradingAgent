#!/usr/bin/env bash
# Stop hook：每个会话第一次停止时提醒 Claude 自检——本次会话产生的、有复用价值的
# 内容（事实结论 / 用户立的规则 / 踩坑 / 接口实测）是否已提炼到
# .claude/rules/ 或 .claude/skills/。
#
# 去重：在 TMPDIR 下用 per-session flag 文件，确保每会话最多提醒一次。
# 第一次停止 → 置 flag + block 提醒，Claude 收到 reason 后自检；再次停止时 flag 已存在 → 放行。
# 这样既触发自检，又不会每次停止都打断 / 无限循环。
#
# 落实 .claude/rules/knowledge-sedimentation.md。
set -u

input=$(cat)
sid=$(printf '%s' "$input" | jq -r '.session_id // empty' 2>/dev/null)
flag="${TMPDIR:-/tmp}/claude-sediment-${sid}.flag"

# 无 session_id 或本会话已提醒过 → 放行，正常停止
if [ -z "$sid" ] || [ -f "$flag" ]; then
  exit 0
fi

# 第一次停止 → 置 flag + 输出 block，reason 反馈给 Claude
: > "$flag"
cat <<'EOF'
{"decision":"block","reason":"⏰ 知识沉淀自检（本会话仅提醒一次）：本次会话产生的、有复用价值的内容（事实结论 / 用户立的规则 / 踩坑 / 接口实测）是否已提炼到 .claude/rules/ 或 .claude/skills/trade/？已落盘或本次无此类内容，直接正常结束即可，不必汇报。规则见 .claude/rules/knowledge-sedimentation.md。"}
EOF
