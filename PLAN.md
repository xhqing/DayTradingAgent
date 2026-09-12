# 成品化计划（PLAN）

> 本文档是 DayTradingAgent 从「个人开发环境直接使用」走向「成品软件（任何人拿到、配置好就能用）」的工程设计方案与分阶段路线。**写为什么这样设计、设计成什么样**；具体执行条目（什么时候做、做完没）在 `TODO.md` 对应编号条目里，两边不重复维护执行状态。

---

## 一、定位与边界（先定性，决定后面所有设计）

本项目不是传统单机软件，而是**两层混合体**：

1. **确定性工具链**：36 个 Python 脚本（费率计算、算仓位、风控闸、统计复盘、hook 守卫）。这部分是标准软件，可以工程化、可以测试、可以发版——成品化的对象就是它。
2. **AI 决策层**：`.claude/skills/trade/` 下的 SKILL.md 与 references 规则文本，由 AI agent 宿主（Claude Code / CodeBuddy / ZCode）加载执行。这部分**永远无法用软件测试保证**——LLM 决策不可 100% 复现（项目复盘体系已明确立过这条），它的质量保证体系是**统计复盘**（review.py 的 EV / P(EV>0) / 熔断机制持续验证），不是单元测试。

**边界声明（必须写进 README 与 Release notes）**：测试保证的是工具链的正确性——费不算错、闸不误拦、统计不偏；**不保证 AI 决策赚钱**。给 clone 者的预期管理：成品 = 确定性工具链经过测试与发版管理，AI 决策质量靠统计体系持续评估，两者不可混为一谈。

## 二、现状盘点（2026-09-12 实测事实）

- ❌ **无依赖声明**：用了 futu / tigeropen / scipy / matplotlib 等，但无 requirements.txt / pyproject.toml，clone 者无从知道要装什么。
- ❌ **无正式测试**：无 `tests/` 目录；`tmp/` 下有 9 个一次性验证脚本（历次改动后手写验证用），不是体系、也不进 git。
- ❌ **从未发版**：git tag 数为 0；VERSION 0.1.0 与 CHANGELOG 一直在维护，但从未打 tag / 发 Release——版本号只是文本、没有发布事实。
- ❌ **配置静默降级（最危险的一条）**：`config.json` 在 .gitignore 里（clone 者拿不到），而脚本读它几乎全是 `try: read except: pass` 静默用硬编码默认值——参数文件缺失不报错，直接用默认 risk_fraction 2% 等参数开仓。对自己用是便利，对别人用是事故温床。
- ❌ **无上手引导**：README 有项目定位说明与 Windows 提示，但没有 Quick Start（从 clone 到跑通第一步的路径）。
- ✅ 已就绪：双语 README + LOGO + 徽章、CHANGELOG.md、VERSION、GitHub About、LICENSE、凭证隔离硬约束（permissions.deny + secret_guard hook）、敏感信息扫描纪律。

## 三、成品定义（验收口径）

**任何人拿到仓库，走完五步就能用**：

```
clone → 装宿主与依赖 → 复制 example 模板填配置 → doctor 自检全绿 → 模拟盘试跑一笔
```

验收标准就一句话：**doctor 全绿 = 环境就绪**（依赖齐、凭证在、OpenD 通、时区对），模拟盘试跑一笔成功 = 工具链通。

## 四、设计模块

### 4.1 分发形态：轻路线起步，留重路线后门

**轻路线（现在做）**：仓库即产品。clone + 配置 + 装 agent 宿主就能用；发版 = git tag + GitHub Release（本机 `/bump` + `/release` skill 链现成，缺的只是首次发版与发版前清单）。升级体验：clone 者看 Release notes + CHANGELOG 决定升不升。

**重路线（远期，出现明确触发信号再做）**：把核心引擎（费率 / 算仓 / 风控闸 / 统计）抽成独立 Python 包（如 `daytrading-core`），skill 只留编排层与 AI 规则，包走独立版本。触发信号（满足其一才动）：① 出现第二个消费者（如 QSAgent 要复用同一套费率与统计口径）；② skill 目录结构开始限制测试组织。**现在不做**——单仓库单用户阶段，抽包只增加跨仓库同步成本。

### 4.2 配置体系（「配置好就能用」的核心）

**三层配置分层**，每层缺失时的行为必须显式设计：

| 层 | 内容 | 存放 | 缺失时的行为 |
|---|---|---|---|
| 凭证层 | 老虎私钥与账户号、富途 OpenD 登录 | `accounts.json`（gitignore） | **启动即报错并指路**，绝不静默 |
| 策略层 | risk_fraction、min_net_odds、circuit_breaker、杠杆兜底等 | `config.json`（gitignore） | **报错**（打印缺什么、模板在哪），绝不静默用默认值开仓 |
| 机器层 | 代理端口、路径 | config 可选节 / 环境变量 | 有安全默认值，可缺省 |

配套三件事：

1. **example 模板入库**：`config.example.json` / `accounts.example.json`（占位符如 `<TIGER_ACCOUNT>`，不含真实值），clone 者复制改名填值；
2. **config 读取 fail-fast 化**：把所有 `try: read config except: pass` 改为显式校验——config 缺失 / 关键字段缺失时打印指引并拒跑。这是从「自己用」到「给别人用」的分水岭改动，**对日常使用也有价值**（参数没配上会明确知道，而不是拿默认值默默跑）；
3. **doctor 命令**（扩展现有 preflight.py 或独立 `scripts/doctor.py`）：一条命令检查全部外部依赖——Python 版本、pip 依赖齐不齐、富途 OpenD 端口、凭证文件存在且 SDK 能连通（账户号打码输出）、代理状态、时区——输出 ✅/❌ 清单与补救指引。

### 4.3 测试体系（四层，按「能不能进 CI」划分）

| 层 | 被测对象 | 运行方式 | 依据 |
|---|---|---|---|
| ① 纯函数单测 | **直接算钱的函数**：fee_schedule 费率、calc_position_size 选档、net_max_loss、_net_odds、check_price_in_range、review.py 统计量（胜率 / EV / 贝叶斯 / 熔断判据） | pytest，**进 CI** | 历史上真实出过的三个 bug——平台费反向 max()（MU 被算 458 USD）、币种 7.8 倍偏差、汇率直除——**全部在这一层、全部可被单测拦住** |
| ② 脚本干跑 | 开仓脚本拒单矩阵（blocked_by: losing_streak / circuit_breaker / 赔率不达标 / 购买力降档；熔断期平仓照常放行） | monkeypatch 老虎 / 富途 SDK + 脱敏 fixture，**进 CI** | 2026-09-12 解耦时的临时 monkeypatch 验证就该固化成这层；拒单闸是安全件，改一次测一次 |
| ③ 端到端冒烟 | 模拟盘小仓位开 → 移损 → 平全链路 | **不进 CI**，发版前手动清单 | 依赖真实凭证与市场时段、会污染模拟盘样本统计；已有「✅ 实测状态」惯例，制度化成发版清单即可 |
| ④ AI 层防护 | hook 拦截行为（monitor_guard / secret_guard / changelog_guard 探针 payload）+ trade skill 行为断言（eval） | hook 探针**进 CI**；skill eval 发版前手动跑 | hook 是纯脚本可测；AI 行为不可单测，只能用 eval 集（skill-creator 的 eval 体系）做行为断言（如「复盘指令不亮模式提醒」「实盘无授权不下单」） |

**CI 设计**：GitHub Actions 跑 ①② + hook 探针（零凭证依赖，任何人 clone 都能跑通 CI），push / PR 触发。③④ 永远不进 CI。

### 4.4 版本管理与发版

1. **首次发版**：`/bump` 对齐 0.1.0 → `/release` 打 tag + GitHub Release（CHANGELOG 的 [Unreleased] 段直接做 notes）；
2. **版本语义约定（0.x 阶段）**：
   - **minor**（0.1 → 0.2）：策略参数语义 / 护栏行为 / 费率口径变更——clone 者必须读 CHANGELOG 再决定升级；
   - **patch**：纯 bug 修复、工具补强、文档。
   - 本质是把「升级风险」显式化：minor = 交易行为可能变，patch = 不会变；
3. **发版前清单**（写进发版流程文档）：全量 pytest 通过 ✅ + doctor 全绿 ✅ + 模拟盘冒烟一笔 ✅ + CHANGELOG 无 [Unreleased] 残留 ✅；
4. **开发流程升级**：第 ① 层测试落地后，本项目满足 dev-workflow 的「存在测试用例的软件开发项目」标准——核心脚本改动走功能分支 + 测试门禁 + 验收合并；文档 / 配置类杂事仍直改 main。补测试的隐性收益：纪律从散文规定升级为测试门禁；
5. **运行 / 开发隔离（个人阶段务实做法）**：不搞双目录；做到「使用在 main 的 Release tag、开发在分支」即可。对外（README）写明「stable = 最新 Release tag，main = 开发中」。

### 4.5 上手引导（README 补 Quick Start）

五步式，中英同步（中文版为权威方向）：

```
① clone 仓库 + 装 agent 宿主（写明宿主名称与版本要求）
② pip install -r requirements.txt
③ 复制 config.example.json / accounts.example.json 为正式文件并填值
④ 跑 doctor 自检，按 ❌ 项补齐到全绿
⑤ 模拟盘试跑一笔（signal 模式发一次信号 / auto 模式模拟开平一小单）
```

现有 README 的「Prerequisites」节扩写成 Quick Start；风险声明（一节的边界声明）随发版写进 Release notes。

## 五、分阶段落地路线

> 执行条目在 `TODO.md`（T144 / T145 / T146），此处只留阶段设计与验收。

| 阶段 | 内容 | 交付物 | 验收 |
|---|---|---|---|
| **一：测试地基** | tests/ + pytest + requirements.txt + 收编 tmp/ 存量验证脚本 + 算钱函数单测 + GitHub Actions | `tests/`（①②层 + hook 探针）、`requirements.txt`、CI workflow | CI 在干净环境全绿；三个历史 bug 各有一条回归测试 |
| **二：配置工程化** | example 模板入库 + config 读取 fail-fast 化 + doctor 命令 + README Quick Start | `config.example.json`、`accounts.example.json`、`scripts/doctor.py`、双语 Quick Start | 删掉 config.json 后脚本明确报错指路（而非静默默认）；doctor 覆盖全部外部依赖 |
| **三：发版流程** | 发版清单文档 + 首次 tag/Release + 版本语义写入 CONTRIBUTING 或 README | 首个 GitHub Release（v0.1.0）、发版前清单文档 | clone 者 GitHub 上可见 Release 与升级说明 |
| **四：远期可选** | 核心抽包 / skill eval 集 / 重路线 | 视触发信号定 | 触发信号出现才启动 |

**顺序与并行**：一、二可并行，总量约两三个工作日；三依赖一、二（没测试不敢发版、没 doctor 不敢说「配置好就能用」）；四不排期。

**与日常使用的兼容**：阶段一、二全部是加法（新增 tests/、模板、doctor、文档），不动现有脚本行为——盯盘照常。唯一的**行为变更**是阶段二的 config fail-fast 化（静默默认 → 报错），单独做、配迁移说明、做完当轮验证。

## 六、设计原则（做的过程中不跑偏的锚）

1. **先测算钱的，再测其它的**——测试优先级 = 出错代价（费率 / 选档 / 拒单闸 > 统计工具 > 文档脚本），不为覆盖率数字而测；
2. **静默降级零容忍**——任何「配置缺失就用默认值继续跑」的路径都要改成显式报错，交易软件的默认值必须来自显式配置；
3. **CI 零凭证原则**——进 CI 的测试必须做到「clone 即跑」，任何需要真实凭证的验证归入发版前手动清单；
4. **文档与工具同步**——每条规矩落地时同步工具强制（doctor / CI / hook），延续项目「文档规定必须尽可能配工具强制」的既有原则；
5. **不追求一步到位**——轻路线够用就停在轻路线，重路线按触发信号决策，避免为架构而架构。
