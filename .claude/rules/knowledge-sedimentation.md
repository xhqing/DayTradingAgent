# 知识沉淀：AutoMemory 持久存 + rules/skills 规范归宿

本项目的跨会话内容分两类归宿，各司其职（2026-07-15 修订：撤销 sediment hook，AutoMemory 升为项目级持久存储）。

## AutoMemory（.claude/memory/）—— 持久记忆归宿

AutoMemory 存项目级 `.claude/memory/`（配置见 `.claude/settings.local.json` 的 `autoMemoryDirectory`），**不再只是临时缓冲**，而是本项目跨会话记忆的持久归宿：用户偏好、反馈、项目上下文等。**不加入 .gitignore**，memory 内容随仓库公开、跨会话复用。

## rules / skills —— 强约束规范归宿

需要「强约束、可执行」的规范仍沉淀到：

- **通用工作规范**（怎么验证事实、怎么输出、怎么操作文件等，不限于某个领域）→ `.claude/rules/`
- **领域执行规范**（交易规则、CLI / SDK 实测结论、踩坑、市场结构等）→ `.claude/skills/trade/`（主文件 `SKILL.md` 写简明结论 + 触发规则，详细数据 / 代码骨架放参考文件）

rules/skills 与 memory 的区别：rules/skills 是「必须遵守的规范 / 执行步骤」（纪律），memory 是「背景记忆 / 偏好」（上下文）。

## 关于「提炼」（2026-07-15 撤销 hook）

2026-07-10 曾用 `sediment-check.sh` Stop hook 提醒把 memory 内容提炼到 rules/skills。**2026-07-15 用户撤销该 hook**：memory 自己持久存即可，不再强制提炼。遇到确实属于「强约束规范」的内容（不是个人偏好 / 上下文，而是可执行的纪律），仍主动写 rules/skills；否则留在 memory。
