# DayTradingAgent

港股 / 美股日内交易的 AI 执行项目。核心执行规范在 trade skill（`.claude/skills/trade/SKILL.md`），涉及盯盘、下单、复盘、分析标的或实盘账户操作时激活。

## 工作规则（对本项目生效，显式引用）

- @.claude/rules/verify-facts-before-stating.md — 陈述事实性 / 数值性结论前必须先验证，禁止把推测当事实
- @.claude/rules/output-and-writing-style.md — 对话输出高信息密度，写文件语义清晰优先
- @.claude/rules/knowledge-sedimentation.md — 知识沉淀：AutoMemory 持久存（项目级 .claude/memory/）+ rules / skills 强约束规范归宿

## commit skill 检测缓存

<!-- commit-skill: readme-standard = ok -->
- README 中英双语 + LOGO + 徽章 + 版权署名：已就绪（2026-07-15 确认）

<!-- commit-skill: license = ok -->
- LICENSE.md：已存在（2026-07-15 确认）

<!-- commit-skill: github-about = ok -->
- GitHub About：已配置（双语 description + topics，2026-07-15）

<!-- commit-skill: agent-persona = ok -->
- Agent 拟人名：已写入 README（Victor，2026-07-15）

<!-- commit-skill: agent-llm = ok -->
- Agent 大脑型号：已写入 README（GLM-5.2 · z.ai，2026-07-15）

<!-- commit-skill: automemory = ok -->
- AutoMemory 目录：已配置到 .claude/memory/（2026-07-15 确认）
