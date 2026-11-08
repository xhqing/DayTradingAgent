<p align="center">
  <img src="assets/logo.svg" width="160" alt="Victor logo" />
</p>

<h1 align="center">Victor —— 港股 / 美股日内交易智能体</h1>

<p align="center">
  <a href="LICENSE.md"><img src="https://img.shields.io/badge/License-MIT-blue.svg" alt="License: MIT" /></a>
  <img src="https://img.shields.io/badge/Markets-HK%20%2F%20US-16C784.svg" alt="Markets: HK / US" />
  <img src="https://img.shields.io/badge/Mode-Signal-FF8C00.svg" alt="Mode: Signal" />
  <img src="https://img.shields.io/badge/Mode-Auto-2ECC71.svg" alt="Mode: Auto" />
</p>

<p align="center">🌐 <a href="README.md">English</a></p>

**Victor** 是一个港股 / 美股日内交易的 AI 执行智能体。它负责盯盘、分析标的、计算仓位与止损位、输出结构化交易信号——而每一笔订单都由人在券商 App 里手动执行。

> 这**不是**一个传统意义上的软件项目：没有可供 `npm install` 或 `cargo run` 的应用。**这个仓库本身就是智能体**——它的全部行为方式都由 `.pi/skills/` 下的 `trade` 技能与 `AGENTS.md` 里的项目规则塑造，由 **pi** 运行框架加载，作为 Victor 的工作纪律。

---

## Victor 是谁？

这个智能体有一个拟人化的名字——**Victor**（维克多），一位负责港股 / 美股市场的日内交易执行助手。与其说 Victor 是一段程序，不如说它是一个**由规范塑造出来的"人格"**：它怎么思考、怎么行动——什么时候该空仓、止损该跟多紧、为什么绝不自己下单——全都写在仓库的 skills 和 rules 里，而不是写在应用代码里。

**Victor** 取"胜利者、征服者"之意，寄托对盈利交易的期许。但 Victor 的优势**并不**来自激进冒险，而来自事实的严谨与铁的纪律：

- **陈述前先验证**：任何事实性、数值性结论，不经核实不说出口。
- **EV ≥ 0**：每一笔开仓都以正期望（胜率 × 赔率）为门槛。
- **信号模式**：Victor 只负责分析、盯盘、发信号——它绝不碰任何订单，执行权完全在人。

一句话概括工作信条：**交易亏钱是可接受的风险；把没验证过的推测当事实说、或违反护栏，是 bug——而 bug 不可接受。**

---

## 核心理念

| 原则 | 含义 |
|---|---|
| **事实先验证** | 实体归属、算术、交易日历、API 字段、手续费——先验证再断言，禁止把推测当事实。 |
| **信号模式** | 自 2026-07-07 起，Victor 只发信号（🟢 开仓 / 🔵 加仓 / 🟠 减仓 / 🔴 平仓 / 🟡 移动止损），绝不调用任何下单命令。 |
| **EV 驱动** | 每笔开仓都要求胜率 > 50% **且** 赔率 ≥ 1.2，否则不发信号。 |
| **信号层面复盘** | 统计胜率 / 赔率 / EV（信号层面，不依赖账户）；实际账户盈亏由用户自算。 |
| **知识沉淀** | 强约束规范沉淀进 `AGENTS.md` 与 `trade` 技能的 references。AutoMemory 已于 2026-07-20 废弃（内容全量提炼进 SKILL.md）。 |

---

## Victor 如何工作

当用户提出**盯盘、发信号、交易复盘、分析标的**时，pi 激活 Victor，加载 `trade` skill 并运行其护栏。

**盯盘标准启动序列**（脚本在 `.pi/skills/trade/scripts/`）：

1. `preflight.py` —— 核实时间、港股 / 美股时段、富途 OpenD 端口
2. `hot_list.py` —— 拉热度榜（选标的的铁律第一步）
3. `classify_hk_security.py`（富途快照）—— 核实标的真实身份与证券类型
4. `snapshot.py` / `kline.py` —— 快照 + 趋势 / 斐波那契回撤位
5. `monitor.py` —— 密采样盯盘（默认 6 轮 × 10 秒）

当某个机会达到 EV 门槛，Victor 以表格形式在对话中输出信号（带 emoji 标识），算好止损价，再把信号追加到当日的信号日志（港股 / 美股分开记）供复盘。Victor 只发信号、不管账户、不下单；人在自家券商 App 执行。

---

## 仓库结构

```
DayTradingAgent/
├── AGENTS.md                      # 项目规则——智能体的工作纪律（入口）
├── LICENSE.md                     # MIT
├── README.md                      # 英文版 README
├── README_cn.md                   # 本文件（中文）
│
├── .pi/                           # pi 运行框架配置
│   ├── settings.json              # 服务商 / 模型默认值
│   ├── extensions/                # 项目级 pi 扩展
│   │   ├── trade-guards.ts        # 把 pi 事件桥接到 hooks/ 下的 Python 护栏
│   │   └── ask-question.ts        # AskUserQuestion 结构化提问工具
│   └── skills/
│       └── trade/                 # 领域执行规范——Victor 的核心
│           ├── SKILL.md           # 主文件：执行规范 + 硬性护栏
│           ├── references/        # 策略、风控、盯盘、复盘、宿主集成
│           ├── config.example.json       # 风控 / 盯盘配置模板
│           ├── accounts.example.json     # 老虎凭证模板
│           └── scripts/           # 盯盘 / 下单 / 复盘脚本库
│
├── hooks/                         # 护栏脚本（唯一源，由 pi 扩展桥接 spawn）
│   ├── secret_guard.py            # 凭证读取拦截
│   ├── monitor_guard.py           # 密采样纪律 / 互斥闸 / 断链提醒
│   ├── changelog_guard.py         # CHANGELOG / TODO 卫生提醒
│   ├── jms_guard.py               # 盘中禁止刷新订阅
│   └── live_auth_witness.py       # 实盘授权见证
│
├── signals/                       # 每日信号记录（港美分开、HKT/ET 后缀）+ ring-log.csv（+ 权益账本，仅本机、已 gitignore）
├── reviews/                       # 复盘报告 + 配套 CSV/PNG 附件
├── notes/                         # 长篇推导（凯利仓位方案、累计收益数学）
├── state/                         # 持久运行状态（已 gitignore）：熔断、标的池认领、互斥 WAL
├── tmp/                           # 一次性运行文件（已 gitignore）：采样日志、心跳、后台任务日志
└── archive/                       # 本机历史留存（已 gitignore）：重构前 memory 快照 + 旧复盘
```

> 真实的 `config.json` 和 `accounts.json`（含老虎凭证）**已被 gitignore**——仓库只随附 `*.example.json` 模板。`archive/` 目录同样仅本地保留。

---

## 工具链

Victor 通过三个券商数据源交易与读取行情：

| 数据源 | 角色 | 说明 |
|---|---|---|
| **富途 OpenD**（`futu-api`） | 盯盘行情主力（港股 + 美股） | 港股免费 Level2（10 档盘口 + 经纪队列 + 资金流）+ 美股 10 档 + 美股资金流；本地网关 `127.0.0.1:11111` |
| **老虎证券 SDK**（`tigeropen`） | 港股备用数据 + WebSocket 推送 | 港股 Lv1/Lv2 已验证可用；**美股无行情权限** |

Victor 只发信号、不下单。**用哪个券商 / 账户由用户自决**；人在自家券商 App 执行。行情数据来自上表的富途 + 老虎。

---

## 硬性护栏（节选）

Victor 在发出任何信号前逐条自检（完整清单见 `SKILL.md`）：

- **一次最多持仓一个标的**——已有持仓时不发新标的的开仓信号。
- **由止损反推仓位（固定比例）**——单笔预算 `B = risk_fraction × equity`（默认 2%）；仓位 = 按手数取整后、实际 max_loss 最接近 B 的那档。max_loss 可略超 B 但不得超过 `equity × f_max`（默认 2.5%）——绝对上限就是权益比例。
- **每个开仓信号必须含止损价**——取技术位，由用户在 App 挂止损。
- **禁衍生品**——只做个股、ETF（含 2×/3× 杠杆）、REIT；不碰期权 / 窝轮 / CBBC / 期货。
- **限可交易时段、收盘前平仓**——港股仅正常交易时段（09:30-12:00 / 13:00-16:00），持仓须在 12:00 午休前、16:00 收盘前平掉；美股可交易时段 = 盘前 + 盘中（美东 04:00-16:00），盯到用户喊停或收盘——无盘后 / 夜盘信号、不留仓过夜。
- **做空默认允许**——先假设全部标的可做空，除非用户反馈某标的不可空。
- **当日平仓**——从不在任何账户持仓过夜。

---

## 前置条件

要让 Victor 真正跑起来，仓库之外还需要：

- **pi** 智能体运行框架——加载本仓库 `.pi/` 下的技能、扩展与 `AGENTS.md` 规则的运行时
- **老虎** SDK（`tigeropen`）已配置在 `~/.tigeropen/`
- **富途 OpenD** 本地网关在运行（港股 Level2 + 美股深度）
- 从 `*.example.json` 模板填好本地的 `config.json` 和 `accounts.json`（accounts.json 只需 tiger 段）
- **Windows 用户**（2026-08-09 起脚本与护栏双平台适配）：需安装 Git for Windows（安装时勾选 Add to PATH）+ Python 3 加入 PATH；本文档命令示例按 macOS 写 `python3`，Windows 上执行时用 `python`（`settings.json` 两套都已预授权）

即便没有这些环境，本仓库仍是一份完整的「一个守纪律的交易智能体应当如何行事」的规范说明。

---

## 当前阶段

Victor 当前处于**信号模式**：AI 发信号、人执行——这套自 2026-07-07 起的安排，根治了此前 AI 直接下单导致的订单失效 / 反向开仓 / 止损失效问题。要进阶到让 AI 重新获得直接下单权限，需要信号胜率、赔率、EV 稳定为正 + 持续进步，并经用户明确授权。

---

## 风险声明

日内交易伴随重大的资金损失风险。Victor 输出的是分析与信号，供账户持有人决策参考——**它不构成投资建议，也不执行任何交易。** 所有订单均由用户在自有券商账户手动下达。历史表现不代表未来收益，任何回测或模拟盘结果亦不代表实盘表现。用户对每笔交易决策及其造成的损失承担全部责任。作者与贡献者不对任何交易损失承担责任。

---

## 引用与署名

本项目以 MIT 许可证开源，额外请求使用者在**使用、二次分发或基于本项目构建衍生作品**时，注明作者并引用项目地址：

- **作者：** xhqing <https://github.com/xhqing>
- **项目：** Victor —— 港股 / 美股日内交易智能体
- **地址：** https://github.com/xhqing/DayTradingAgent

若你 Fork、引用代码或基于本仓库二次开发，请在文档 / README / 致谢中保留以上出处（作者、项目名、仓库地址）。

---

## 许可证

[MIT](LICENSE.md) © 2026 xhqing <https://github.com/xhqing>。
