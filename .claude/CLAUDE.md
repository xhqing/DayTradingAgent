# DayTradingAgent

港股 / 美股日内交易的 AI 执行项目。核心执行规范在 trade skill（`.claude/skills/trade/SKILL.md`），涉及盯盘、下单、复盘、分析标的或实盘账户操作时激活。

## 工作规则（对本项目生效，显式引用）

- @.claude/rules/verify-facts-before-stating.md — 陈述事实性 / 数值性结论前必须先验证，禁止把推测当事实
- @.claude/rules/output-and-writing-style.md — 对话输出高信息密度，写文件语义清晰优先
- @.claude/rules/knowledge-sedimentation.md — 知识沉淀：AutoMemory 持久存（项目级 .claude/memory/）+ rules / skills 强约束规范归宿

## signals 目录归属（2026-07-18 立）

所有交易信号记录统一放**项目根 `signals/`**：港美每日信号（`signals/YYYY-MM-DD-HKT-signals.md` 港股 / `signals/YYYY-MM-DD-ET-signals.md` 美股）+ 响铃 log（`signals/ring-log.csv`）。**不在 `.claude/skills/trade/signals/` 下**（2026-07-18 已从该旧路径全量迁出到根 `signals/`，统一存放、便于复盘、避免 skill 目录与信号数据混杂）。发信号记录、响铃 log、复盘读取均走根 `signals/`。

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

<!-- commit-skill: attribution-name = ok -->
- 版权人/署名引用名字：已归一为 All Contributors（2026-07-17 确认）
