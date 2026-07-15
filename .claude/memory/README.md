# AutoMemory（项目级，2026-07-15 起）

本目录是 Claude Code 的 AutoMemory 持久存储位置（项目级），跨会话保存本项目的记忆（用户偏好、反馈、项目上下文）。

## 配置

AutoMemory 路径由 `.claude/settings.local.json` 的 `autoMemoryDirectory` 字段指定（本机绝对路径；`settings.local.json` 不入库，见全局 gitignore `**/.claude/settings.local.json`）。

> ⚠️ `autoMemoryDirectory` 只接受**绝对路径**，不支持 `$CLAUDE_PROJECT_DIR` 或相对路径（Claude Code 硬限制）。clone 本项目后，若要把 memory 存到项目内，请在自己的 `.claude/settings.local.json` 里把 `autoMemoryDirectory` 改成你本机的绝对路径（如 `/Users/<你>/.../DayTradingAgent/.claude/memory`）；不配则 memory 存 Claude Code 默认的全局路径。

## 入库

本目录**不加入 .gitignore**（2026-07-15 用户明确）——memory 内容随仓库公开、跨会话复用。
