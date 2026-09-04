# DayTradingAgent

港股 / 美股日内交易的 AI 执行项目。核心执行规范在 trade skill（`.claude/skills/trade/SKILL.md`），涉及盯盘、下单、复盘、分析标的或实盘账户操作时激活。trade skill 有**两种执行模式**：默认**信号模式（signal）**（AI 只发信号、用户手动执行、不碰账户），用户**特别说明要自动交易**（说「自动下单 / 自动交易 / auto」等）时才切**自动交易模式（auto）**（港股老虎模拟账户、美股老虎模拟账户，AI 直接调脚本下单）；两种模式交易策略完全通用，唯一区别 = 是否使用证券账户（详见 `SKILL.md`「模式开关与触发首动作」、`references/auto-mode.md` / `signal-mode.md`）。

## 工作规则（对本项目生效）

以下两条规则原为 `.claude/rules/` 下独立文件（`verify-facts-before-stating.md`、`output-and-writing-style.md`），2026-09-01 迁入本文件内联，rules 目录已删除。

### 陈述前先验证：事实性结论不许凭推测

任何事实性、数值性结论，输出前必须先验证；禁止把"印象 / 推测 / 常识推断"当成已验证事实说出来。**业务上亏钱是风险，可接受；事实性结论出错是流程缺陷（bug），不可接受。**

本条与全局 `verify-before-report.md`（改完文件重读验证）互补：那个管"文件改对没"，本条管"话说对没"。

#### 必须实做的验证

1. **实体归属**：任何代码 / 标识符第一次出现，先用权威查询确认真实身份，再谈分析。绝不凭代码记忆或数字联想归因。
   - 交易场景示例：港股代码是 5 位数字、无语义规律，凭记忆会认错（0823 是领展 REIT 不是汇丰、汇丰是 0005）。接触新标的先富途 `get_market_snapshot([code])` 查 `name` 字段，代码 + 中文名一起写。

2. **算术 / 数值**：涉及"是否倍数、是否整除、占比、费用、盈亏"等数值判断，输出前用算式实算一遍（如 `python3 -c "print(400%200)"`），不许心算后直接断言，更不能凭语感。

3. **日历 / 时段 / 休市**：任何"今天是否工作日、是否半日、几点开盘收盘、某节日是否休市"的判断，查实际日历（交易场景用富途 `request_trading_days`），不凭节日常识推测——节日当天休市 ≠ 节前一天交易；各国各年有调休、半日、连休差异。注意夏令时 / 冬令时切换也要核实。

4. **API / 字段 / 命令行为**：任何关于命令参数、字段名、返回结构的结论，先 `--help` 或实跑一次看输出，不凭记忆描述。

5. **数字必须有据**：费率、汇率、利息等数字必须有出处，禁止估算报数。查不到就标"待查"，绝不编一个数字填进去。

6. **汇报当前时间 / 盘面时点前 `date` 实测**：禁止拿最后一条数据的时间戳当作"现在"。长任务（排查 / 重活）结束后、或中途切去干别的再回来，都要重新 `date "+%Y-%m-%d %H:%M:%S %Z"` 确认真实时间——耗时易被低估，真实时间可能已推进几小时。

#### 通用准则

输出任何事实性或数值性陈述前，自问"这个我验证过吗？"——若没有，要么去验证，要么改用不确定语气（"印象中……，待核实"）并立刻去验证。宁可慢一步验证，不可快一步出错。

### 输出与写作风格：分场景——盯盘精简、文件大白话

**总原则（2026-07-14 用户修订）**：用规范的通用术语表达，不为了短而自创简写；语义清晰始终是底线（不为短牺牲语义，也不为凑字数啰嗦）。在此基础上区分场景。

#### 写文件：大白话说清逻辑，不考虑信息密度

写入文件（skill / 文档 / 配置说明）必须语义明确、无逻辑矛盾，用最标准的普通话大白话把因果解释清楚，不追求精简，宁可多用文字也要让人一遍读懂。

- 写完读一遍，检查「逻辑跳跃 / 表象当核心 / 术语歧义」（例：把「没法止损锁利」写成「过夜」——过夜是表象，无人盯盘、没法及时止损锁利才是核心）。
- 常见陷阱：成本价 vs 成交价（做空成本为负数）、上方 / 下方方向歧义、为短而牺牲准确。
- 文件里同样不要自创简写，用规范完整的表述。

#### 盯盘对话：精简、高密度、直接给

实时盯盘（盘面秒变）的对话输出要精简、信息密度高，不要展开解释——结论直接给、信号直接发，但仍然要语义清晰、容易理解。

- 盯盘报告简短陈述关键事实（价格、变化、盘口关键值）+ 直接给判断，不展开「为什么」的长篇解释（盘面秒变，长解释会让用户收到时情况已变、结论失配）。
- 信号直接发表格 emoji，不啰嗦。
- 精简靠「用通用术语直接陈述」，不靠自创缩写。

#### 复盘与一般对话：大白话说清楚（不精简）

非实时的对话（交易复盘、普通问答等）**不追求精简**，用大白话把事情说清楚，可以详细展开，但同样不啰嗦、不自创简写。

#### 不要自创简写（所有场景通用）

通用术语（行业惯用词、技术名词、既有缩写）正常使用，但不为了短而自造缩略说法（例如「买盘较多」不要压成「偏正」、「继续上涨」不要压成「续涨」）。

#### 与全局规定的关系

本条与全局 `~/.claude/CLAUDE.md` 的「输出风格」一致，以全局规定为准。

## 文档规定必须尽可能配工具强制（2026-08-18 用户立）

凡是本项目以后写在文档（`CLAUDE.md` / `skills/*.md`）里面的**规定**，都要**尽可能通过加工具强制**，最大程度降低不按照规定执行的概率；**实在无法加工具强制的情况要说明理由**。

- **为什么**：2026-08-18 早盘 auto 实盘盯盘连续三起「规矩写在文档里但没执行」（自设开仓截止线 / 段启动误改 nohup+sleep 轮询 / 老虎互踢处置迟缓），且当天对照实验证明——凡是工具在场打印或机械强制的规定（VWAP 检查、分析心跳提示、hook 拦截、互斥闸）全部守住，凡靠 AI 记忆的全部失守。散文规定在上下文压缩后会衰减，工具强制不会。
- **怎么用**：新写 / 修改任何一条规定时，同步评估并落地工具强制手段，按强制力从高到低优先选：① **hook / 脚本硬拦**（PreToolUse 阻断、下单脚本内置校验拒单，如 monitor_guard.py 跑法拦截、open_position 时间闸）；② **决策时刻工具在场打印**（相关脚本在相关时段输出规定原文要点，如段结束 VWAP 检查、停盯边界提醒）；③ **定时重读**（周期性提醒里带「重读规定」动作，如每 10 分钟重估提醒重读硬性护栏）。三层都不适用时，在该规定条文处**显式注明「无法工具强制 + 理由」**（例：依赖外部信息源真实性的规定、纯认知判断类规定）。
- **边界**：本条管「怎么让规定被执行」，不管「规定内容本身是否合理」；工具强制手段的改动照常记 CHANGELOG（含回归风险评估）。

## hooks 三宿主：CC / ZCode / CodeBuddy 同一份脚本（2026-09-02 立；2026-09-03 增补 CodeBuddy 实证；2026-09-03 晚修订：ZCode 项目级挂载官方禁用 → 补救改用户级）

本项目全部 hook 脚本在 `.claude/hooks/`（唯一源，不复制），挂三个宿主：Claude Code 走 `.claude/settings.json` 的 `hooks` 段，ZCode **原走 `.zcode/config.json` 的 `hooks` 段——2026-09-03 晚经 T137 端到端验证发现该路被 ZCode 官方整体禁用（见本节末「ZCode 修订」段），现改为挂用户级 `~/.zcode/cli/config.json`**，**CodeBuddy 直接读同一个 `.claude/settings.json`（官方声明 hook 机制完全兼容 Claude Code 规范，2026-09-03 实证：CodeBuddy 会话里 `secret_guard` 成功拦截了 AI 读 accounts.json 的命令，拦截文案逐字来自本项目的 secret_guard.py，而该 hook 只挂载在项目 `.claude/settings.json` 一处——即 CodeBuddy 无需单独配置文件）**。项目里另有一份 `.codebuddy/settings.json`（2026-09-03 建，matcher 同时写 CLI 与 IDE 两种工具名），与 `.claude/settings.json` 内容等价、作 CodeBuddy 显式挂载兜底——**同一命令两处挂载官方会自动去重，若发现 hook 行为翻倍（同一条提醒出现两次）优先删 `.codebuddy/settings.json`**。全局守卫 `pre-tool-use-guard.sh` 在 CC 挂 `~/.claude/settings.json`、在 ZCode 挂 `~/.zcode/cli/config.json`；**CodeBuddy 的用户级 `~/.codebuddy/settings.json` 只放 `enabledPlugins`（用户 2026-09-03 明确不挂全局守卫，此宿主下杀 VSC 前先问等全局规则靠散文规定执行、无工具强制）**。三个宿主的 hook 协议差异已由脚本自行吸收：阻断类（exit 2 + stderr）三宿主同义；提醒类输出走双通道（stderr 给 CC + stdout JSON `hookSpecificOutput.additionalContext` 给 ZCode / CodeBuddy——两者对 exit 0 的 hook 都忽略 stderr、只解析 stdout JSON）。⚠️ **IDE 宿主（CodeBuddy）的工具名与字段名差异（2026-09-03 修）**：IDE 里 hook 收到的 `tool_name` 是 IDE 风格（`execute_command`/`write_to_file`/`replace_in_file`/`read_file`），matcher 字段双向别名能匹配上、但**脚本内部写死的工具名判断会全部落空**——`live_auth_witness.py`（`AskUserQuestion` ↔ `ask_followup_question`，且 CodeBuddy 的 tool_response 是 XML 字符串非 dict）、`changelog_guard.py`（`file_path` ↔ `filePath`）、`pre-tool-use-guard.sh`（`Bash` ↔ `execute_command`）三处已改双名兼容；`monitor_guard.py` / `secret_guard.py` 本就不看 tool_name（只读 `tool_input.command`），天然跨宿主。ZCode 会话 id 带 `sess_` 前缀（transcript 在 `~/.zcode/cli/rollout/`），CC 会话 id 为裸 UUID（transcript 在 `~/.claude/projects/`），`monitor_guard._claim_holder_alive` 按前缀分路径判活、两套会话可并行认领互认。**改 hook 挂载或脚本时：脚本单源改一处三宿主同享；挂载点改动 CC / ZCode 两处同步**；ZCode / CodeBuddy hooks 只在会话启动时加载、改配置后须新会话才生效。

**ZCode 修订（2026-09-03 晚，T137 验证发现 + 用户裁定 C 方案补救）**：ZCode 官方文档（zcode.z.ai/en/docs/hooks）明文 **"Project-level hooks are not executed in the current version"**——`<workspace>/.zcode/config.json` 的 hooks 无论 `hooks.enabled` **整体忽略**（日志打 `config.project_hooks.pending_trust` / `workspace_hook.feature_disabled`；本项目六条项目级挂载自 09-02 迁移起从未跑过一次，T137 双探针实证），设置页已隐藏 workspace scope，官方建议的共享方式是插件分发。**补救（用户裁定 C 方案）**：`secret_guard`（PreToolUse Bash）与 `changelog_guard`（PostToolUse Edit|Write）两条**上移挂用户级 `~/.zcode/cli/config.json`**（与既有 guard.sh 并列）——secret_guard 拦的是通用凭证路径特征、跨项目安全；changelog_guard 的项目判定从脚本路径推导（三层 parent = DayTradingAgent）、其它项目文件 `relative_to` 抛 ValueError 天然静默，均不影响其它项目的 ZCode 会话。**`monitor_guard` / `live_auth_witness` 在 ZCode 宿主不挂**（monitor_guard 盘中互斥闸对其它项目会话有误伤风险、live_auth_witness 属实盘场景，两者在 CC / CodeBuddy 宿主照常生效）——即 ZCode 会话缺盯盘互斥闸与实盘授权见证，**ZCode 会话不用于盯盘 / 实盘执行**（盯盘 / 实盘用 CC 或 CodeBuddy 会话）。项目 `.zcode/config.json` 六条挂载**保留不删**（未来 ZCode 若放开项目级 hooks 自动生效；当前为死配置、不参与维护同步）。**挂载同步口径相应调整**：ZCode 侧生效挂载 = 用户级 `~/.zcode/cli/config.json` 三条（guard.sh / secret_guard / changelog_guard），改挂载改这里；样例 payload 四分支行为已实测（拦凭证 exit 2 + stderr、普通命令放行、项目内文件出 additionalContext、其它项目静默），**新会话生效后跑同款探针收尾验证**（完整证据链见 `TODO-archive.md`「2026-09-03 T137 完成」节与 CHANGELOG 同日条）。

## 实盘净值不属敏感信息、照常可写（2026-08-20 立旧规，2026-08-29 用户裁定废止）

**现行口径（2026-08-29 用户裁定）**：实盘账户的资产净值（net_liquidation / 总资产）与购买力**不属敏感财务信息，照常写入被 git 跟踪的文件**（CHANGELOG、TODO、SKILL.md、references、accounts.md、脚本注释、`signals/*.md`、`actions/*.md`、`reviews/` 均可）。**裁定理由（用户原话要点）**：实盘账户里的钱只是个人全部财产的一小部分，不能完全代表个人财务状况，故交易账户净值不构成个人财务状况描述。

- **沿革（为什么曾经禁、后来解禁）**：2026-08-20 曾立旧规「实盘净值禁止写入被跟踪文件」（背景：实测净值多次随记录进入 git 历史推上 GitHub）；2026-08-26 随全团队敏感信息大排查收紧到「量级口径同样不写」，并做了 filter-repo 历史清洗（IP / 白名单报文 / 净值数字等替换为占位符）。**2026-08-29 用户重新裁定**：交易账户净值不属于「个人财务状况」敏感类——全局规则禁的是「收入 / 本金 / 资产负债全貌」等描述性财务状况，账户净值只是个人总财产的一小部分、不能代表整体，予以豁免。本次裁定**同时废止**配套的 `equity_guard.py` hook（含基准黑名单自动维护、preflight / resume 在场打印）与 `signals/equity-log.csv` 的 gitignore 忽略。
- **仍然禁止（裁定不豁免的类别，照旧执行）**：① 账户号打码（实盘 / 模拟账户号一律打码口径，AI 上下文禁读 accounts.json / `~/.tigeropen/` 完整号）；② 密钥 / token / 盐值；③ 代理节点 IP 清单、订阅商域名体系、白名单拒绝报文原文、地域规避叙述（2026-08-23 中性化口径不变——这些涉及外部服务配置与绕行链路，与净值豁免无关）。
- **边界**：模拟账户净值（假钱）本就不受限；全局「敏感信息禁止写入未被 .gitignore 忽略的文件」规则中**除账户净值外的其它敏感类别**（含个人 / 公司财务状况的描述性信息——收入目标、资产负债全貌、财务紧迫程度等）**不受本裁定影响、照旧禁写**。

## signals 目录归属（2026-07-18 立）

所有交易信号记录统一放**项目根 `signals/`**：港美每日信号（`signals/YYYY-MM-DD-HKT-signals.md` 港股 / `signals/YYYY-MM-DD-ET-signals.md` 美股）+ 响铃 log（`signals/ring-log.csv`）。**不在 `.claude/skills/trade/signals/` 下**（2026-07-18 已从该旧路径全量迁出到根 `signals/`，统一存放、便于复盘、避免 skill 目录与信号数据混杂）。发信号记录、响铃 log、复盘读取均走根 `signals/`。

## reviews/ 目录归属（2026-07-21 立）

复盘报告与配套数据/图统一放**项目根 `reviews/`**：主报告 `reviews/YYYY-MM-DD-review.md`（港美混合复盘直接用；港美分开复盘仿信号文件加 `-HKT`/`-ET`）+ 同日附件 `reviews/YYYY-MM-DD-*.{csv,png}`（输入数据、统计图）。**与 `signals/`、`archive/` 分工**：`signals/` 记信号事实（复盘数据源、只记事实不写分析避免污染）；`reviews/` 放事后复盘分析（今后复盘新家）；`archive/` 留更早的历史归档（含旧复盘，如模拟盘复盘、MU 事后复盘，不再新增）。复盘读取数据走 `signals/`、产物写入 `reviews/`。目录说明见 `reviews/README.md`。

## 交易逻辑建议必须有数理统计基础（2026-09-02 用户立，方法论硬约束）

**凡给出本项目的交易逻辑方面的建议（开仓 / 平仓 / 止损 / 止盈 / 移损 / 移盈 / 仓位 / 护栏 / 策略规则的新增、修改、撤销、调参），必须有数理统计的数学基础支撑**——即基于**全体历史交易数据**的统计分析与回放验证（如胜率 / 赔率 / EV / P(EV>0) / 有闸 vs 无闸的回放对比），不能建立在单笔交易的复盘决策上。

- **为什么**：交易策略能否盈利靠的是**全部交易的总和**，不是单笔。单笔事故复盘只能**触发**规则（发现问题、提出护栏假设），不能**证明**规则长期成立——证明的唯一依据是全样本统计。stability 护栏（2026-08-21 立）正是反面典型：三层来源（MINIMAX / 07709 / 09988 事故）全是单笔复盘驱动、从未做全样本回放，最终 2026-09-02 发现它连拦三笔净赔率 1.47/1.37/1.73 的正期望入场（长飞 06869 当日 +10.7%）而撤销。同类先例：2026-08-27 止盈可达性硬闸全样本回放（50 笔）后被拦笔平均 +0.30R 仍正期望、闸整体负贡献，降级为警告。
- **怎么用**：提出交易逻辑建议时，先问「这个建议有没有统计依据」——有，列出具体数据（样本量、统计量、回放对比）；没有，明确标注「建议基于单笔复盘 / 未做统计验证」，并说明它只是待验证的假设、不是既定结论。**护栏类（硬拦 / 拒单 / 强制降档）的设立与撤销尤其必须过统计关**——它直接改变交易是否执行，影响面最大。
- **工具强制评估**：本条规定无法加脚本硬拦（属认知判断类：建议是否「有统计基础」依赖对建议内容的评估，无机械化判据），按「文档规定必须尽可能配工具强制」第③层「决策时刻工具在场打印」落地——trading-strategy.md「护栏的统计依据与撤销」节已固化该原则，复盘 / 决策时读该节即触发提醒。
- **边界**：本条管「交易逻辑建议」的提出；数据获取、行情分析、执行类操作（是否在盘中执行某个既定信号）不受此限——后者按既有护栏判断。单笔事故复盘仍有价值（触发规则假设），只是不构成规则长期成立的依据。

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
