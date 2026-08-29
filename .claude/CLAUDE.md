# DayTradingAgent

港股 / 美股日内交易的 AI 执行项目。核心执行规范在 trade skill（`.claude/skills/trade/SKILL.md`），涉及盯盘、下单、复盘、分析标的或实盘账户操作时激活。trade skill 有**两种执行模式**：默认**信号模式（signal）**（AI 只发信号、用户手动执行、不碰账户），用户**特别说明要自动交易**（说「自动下单 / 自动交易 / auto」等）时才切**自动交易模式（auto）**（港股老虎模拟账户、美股老虎模拟账户，AI 直接调脚本下单）；两种模式交易策略完全通用，唯一区别 = 是否使用证券账户（详见 `SKILL.md`「模式开关与触发首动作」、`references/auto-mode.md` / `signal-mode.md`）。

## 工作规则（对本项目生效，显式引用）

- @.claude/rules/verify-facts-before-stating.md — 陈述事实性 / 数值性结论前必须先验证，禁止把推测当事实
- @.claude/rules/output-and-writing-style.md — 对话输出高信息密度，写文件语义清晰优先

## 文档规定必须尽可能配工具强制（2026-08-18 用户立）

凡是本项目以后写在文档（`CLAUDE.md` / `rules/*.md` / `skills/*.md`）里面的**规定**，都要**尽可能通过加工具强制**，最大程度降低不按照规定执行的概率；**实在无法加工具强制的情况要说明理由**。

- **为什么**：2026-08-18 早盘 auto 实盘盯盘连续三起「规矩写在文档里但没执行」（自设开仓截止线 / 段启动误改 nohup+sleep 轮询 / 老虎互踢处置迟缓），且当天对照实验证明——凡是工具在场打印或机械强制的规定（VWAP 检查、分析心跳提示、hook 拦截、互斥闸）全部守住，凡靠 AI 记忆的全部失守。散文规定在上下文压缩后会衰减，工具强制不会。
- **怎么用**：新写 / 修改任何一条规定时，同步评估并落地工具强制手段，按强制力从高到低优先选：① **hook / 脚本硬拦**（PreToolUse 阻断、下单脚本内置校验拒单，如 monitor_guard.py 跑法拦截、open_position 时间闸）；② **决策时刻工具在场打印**（相关脚本在相关时段输出规定原文要点，如段结束 VWAP 检查、停盯边界提醒）；③ **定时重读**（周期性提醒里带「重读规定」动作，如每 10 分钟重估提醒重读硬性护栏）。三层都不适用时，在该规定条文处**显式注明「无法工具强制 + 理由」**（例：依赖外部信息源真实性的规定、纯认知判断类规定）。
- **边界**：本条管「怎么让规定被执行」，不管「规定内容本身是否合理」；工具强制手段的改动照常记 CHANGELOG（含回归风险评估）。

## 实盘净值不属敏感信息、照常可写（2026-08-20 立旧规，2026-08-29 用户裁定废止）

**现行口径（2026-08-29 用户裁定）**：实盘账户的资产净值（net_liquidation / 总资产）与购买力**不属敏感财务信息，照常写入被 git 跟踪的文件**（CHANGELOG、TODO、SKILL.md、references、accounts.md、脚本注释、`signals/*.md`、`actions/*.md`、`reviews/` 均可）。**裁定理由（用户原话要点）**：实盘账户里的钱只是个人全部财产的一小部分，不能完全代表个人财务状况，故交易账户净值不构成个人财务状况描述。

- **沿革（为什么曾经禁、后来解禁）**：2026-08-20 曾立旧规「实盘净值禁止写入被跟踪文件」（背景：实测净值多次随记录进入 git 历史推上 GitHub）；2026-08-26 随全团队敏感信息大排查收紧到「量级口径同样不写」，并做了 filter-repo 历史清洗（IP / 白名单报文 / 净值数字等替换为占位符）。**2026-08-29 用户重新裁定**：交易账户净值不属于「个人财务状况」敏感类——全局规则禁的是「收入 / 本金 / 资产负债全貌」等描述性财务状况，账户净值只是个人总财产的一小部分、不能代表整体，予以豁免。本次裁定**同时废止**配套的 `equity_guard.py` hook（含基准黑名单自动维护、preflight / resume 在场打印）与 `signals/equity-log.csv` 的 gitignore 忽略。
- **仍然禁止（裁定不豁免的类别，照旧执行）**：① 账户号打码（实盘 / 模拟账户号一律打码口径，AI 上下文禁读 accounts.json / `~/.tigeropen/` 完整号）；② 密钥 / token / 盐值；③ 代理节点 IP 清单、订阅商域名体系、白名单拒绝报文原文、地域规避叙述（2026-08-23 中性化口径不变——这些涉及外部服务配置与绕行链路，与净值豁免无关）。
- **边界**：模拟账户净值（假钱）本就不受限；全局「敏感信息禁止写入未被 .gitignore 忽略的文件」规则中**除账户净值外的其它敏感类别**（含个人 / 公司财务状况的描述性信息——收入目标、资产负债全貌、财务紧迫程度等）**不受本裁定影响、照旧禁写**。

## signals 目录归属（2026-07-18 立）

所有交易信号记录统一放**项目根 `signals/`**：港美每日信号（`signals/YYYY-MM-DD-HKT-signals.md` 港股 / `signals/YYYY-MM-DD-ET-signals.md` 美股）+ 响铃 log（`signals/ring-log.csv`）。**不在 `.claude/skills/trade/signals/` 下**（2026-07-18 已从该旧路径全量迁出到根 `signals/`，统一存放、便于复盘、避免 skill 目录与信号数据混杂）。发信号记录、响铃 log、复盘读取均走根 `signals/`。

## reviews/ 目录归属（2026-07-21 立）

复盘报告与配套数据/图统一放**项目根 `reviews/`**：主报告 `reviews/YYYY-MM-DD-review.md`（港美混合复盘直接用；港美分开复盘仿信号文件加 `-HKT`/`-ET`）+ 同日附件 `reviews/YYYY-MM-DD-*.{csv,png}`（输入数据、统计图）。**与 `signals/`、`archive/` 分工**：`signals/` 记信号事实（复盘数据源、只记事实不写分析避免污染）；`reviews/` 放事后复盘分析（今后复盘新家）；`archive/` 留更早的历史归档（含旧复盘，如模拟盘复盘、MU 事后复盘，不再新增）。复盘读取数据走 `signals/`、产物写入 `reviews/`。目录说明见 `reviews/README.md`。

## actions/ 目录归属（2026-07-30 立，2026-07-31 扩展到港股）

港股和美股的模拟盘交易动作记录统一放**项目根 `actions/`**：港股 `actions/YYYY-MM-DD-HKT-actions.md`、美股 `actions/YYYY-MM-DD-ET-actions.md`。`actions/` 记录已执行的交易动作（含 order_id、成交价）。`signals/` 目录保留历史信号记录，新交易不再写入。**复盘数据源 = `signals/` + `actions/` 两个目录的交易记录（2026-08-03 用户立：复盘数据源是这两个目录）**——signal 模式交易在 `signals/`、auto 模式交易在 `actions/`。**默认复盘范围 = 港股（2026-08-13 用户立）**：复盘默认只遍历港股文件（`*-HKT-*`）、合并为样本；美股文件（`*-ET-*`）默认不纳入，用户明确要求复盘美股时才遍历。

## commit skill 检测缓存

<!-- commit-skill: readme-standard = ok -->
- README 中英双语 + LOGO + 徽章 + 版权署名：已就绪（2026-07-15 确认）

<!-- commit-skill: license = ok -->
- LICENSE.md：已存在（2026-07-15 确认）

<!-- commit-skill: github-about = ok -->
- GitHub About：已配置（中英双语 description + topics，2026-08-02 修订：由纯英文改回中英双语以对齐当前 9c 规则）

<!-- commit-skill: agent-persona = ok -->
- Agent 拟人名：已写入 README（Victor，2026-07-15）

<!-- commit-skill: automemory = disabled -->
- AutoMemory：**已废弃不用**（2026-07-20）——memory 内容全量提炼进 SKILL.md、`.claude/memory/` 目录已删、`autoMemoryDirectory` 配置已移除；commit skill 跳过 AutoMemory 目录检测。

<!-- commit-skill: attribution-name = ok -->
- 版权人/署名引用名字：已归一为 All Contributors（2026-07-17 确认）

<!-- commit-skill: readme-link-text = ok -->
- 英文版 README 跳转中文版链接文字：已统一为「简体中文」（2026-07-18 确认）

<!-- commit-skill: repo-sponsors = ok -->
- 仓库 Sponsors 按钮：已就绪（xhqing/.github 全局默认 FUNDING.yml，2026-07-19 确认）

<!-- commit-skill: readme-no-stars-badge = ok -->
- README 徽章：已不含 GitHub Stars 数量徽章（2026-08-03 确认）

<!-- commit-skill: changelog-version = ok -->
- CHANGELOG.md 与 VERSION 文件：已存在（2026-08-03 确认）
