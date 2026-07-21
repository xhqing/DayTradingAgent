# DayTradingAgent

港股 / 美股日内交易的 AI 执行项目。核心执行规范在 trade skill（`.claude/skills/trade/SKILL.md`），涉及盯盘、下单、复盘、分析标的或实盘账户操作时激活。

## 工作规则（对本项目生效，显式引用）

- @.claude/rules/verify-facts-before-stating.md — 陈述事实性 / 数值性结论前必须先验证，禁止把推测当事实
- @.claude/rules/output-and-writing-style.md — 对话输出高信息密度，写文件语义清晰优先

## signals 目录归属（2026-07-18 立）

所有交易信号记录统一放**项目根 `signals/`**：港美每日信号（`signals/YYYY-MM-DD-HKT-signals.md` 港股 / `signals/YYYY-MM-DD-ET-signals.md` 美股）+ 响铃 log（`signals/ring-log.csv`）。**不在 `.claude/skills/trade/signals/` 下**（2026-07-18 已从该旧路径全量迁出到根 `signals/`，统一存放、便于复盘、避免 skill 目录与信号数据混杂）。发信号记录、响铃 log、复盘读取均走根 `signals/`。

## reviews/ 目录归属（2026-07-21 立）

复盘报告与配套数据/图统一放**项目根 `reviews/`**：主报告 `reviews/YYYY-MM-DD-review.md`（港美混合复盘直接用；港美分开复盘仿信号文件加 `-HKT`/`-ET`）+ 同日附件 `reviews/YYYY-MM-DD-*.{csv,png}`（输入数据、统计图）。**与 `signals/`、`archive/` 分工**：`signals/` 记信号事实（复盘数据源、只记事实不写分析避免污染）；`reviews/` 放事后复盘分析（今后复盘新家）；`archive/` 留更早的历史归档（含旧复盘，如模拟盘复盘、MU 事后复盘，不再新增）。复盘读取数据走 `signals/`、产物写入 `reviews/`。目录说明见 `reviews/README.md`。

## commit skill 检测缓存

<!-- commit-skill: readme-standard = ok -->
- README 中英双语 + LOGO + 徽章 + 版权署名：已就绪（2026-07-15 确认）

<!-- commit-skill: license = ok -->
- LICENSE.md：已存在（2026-07-15 确认）

<!-- commit-skill: github-about = ok -->
- GitHub About：已配置（纯英文 description + topics，2026-07-18 修订：原双语 description 按 9c 规则改写为纯英文）

<!-- commit-skill: agent-persona = ok -->
- Agent 拟人名：已写入 README（Victor，2026-07-15）

<!-- commit-skill: agent-llm = ok -->
- Agent 大脑型号：已写入 README（GLM-5.2 · z.ai，2026-07-15）

<!-- commit-skill: automemory = disabled -->
- AutoMemory：**已废弃不用**（2026-07-20）——memory 内容全量提炼进 SKILL.md、`.claude/memory/` 目录已删、`autoMemoryDirectory` 配置已移除；commit skill 跳过 AutoMemory 目录检测。

<!-- commit-skill: attribution-name = ok -->
- 版权人/署名引用名字：已归一为 All Contributors（2026-07-17 确认）

<!-- commit-skill: readme-link-text = ok -->
- 英文版 README 跳转中文版链接文字：已统一为「简体中文」（2026-07-18 确认）

<!-- commit-skill: repo-sponsors = ok -->
- 仓库 Sponsors 按钮：已就绪（xhqing/.github 全局默认 FUNDING.yml，2026-07-19 确认）
