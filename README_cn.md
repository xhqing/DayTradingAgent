<p align="center">
  <img src="assets/logo.svg" width="160" alt="Victor logo" />
</p>

<h1 align="center">Victor —— 港股 / 美股日内交易 Agent</h1>

<p align="center">
  <a href="LICENSE.md"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="License: MIT" /></a>
  <a href="https://claude.com/claude-code"><img src="https://img.shields.io/badge/built%20with-Claude%20Code-7C3AED.svg" alt="Built with Claude Code" /></a>
  <img src="https://img.shields.io/badge/markets-HK%20%2F%20US-16C784.svg" alt="Markets: HK / US" />
  <img src="https://img.shields.io/badge/mode-signal-FF8C00.svg" alt="Mode: Signal" />
</p>

<p align="center">🌐 <a href="README.md">English</a></p>

**Victor** 是一个港股 / 美股日内交易的 AI 执行 agent，基于 [Claude Code](https://claude.com/claude-code) 构建。它负责盯盘、分析标的、计算仓位与止损位、输出结构化交易信号——而每一笔订单都由人在券商 App 里手动执行。

> 这**不是**一个传统意义上的软件项目：没有可供 `npm install` 或 `cargo run` 的应用。**这个仓库本身就是 agent**——它的全部行为方式都由 `.claude/` 下的 skills 和 rules 塑造，Claude Code 加载它们，作为 Victor 的工作纪律。

---

## Victor 是谁？

这个 agent 有一个拟人化的名字——**Victor**（维克多），一位负责港股 / 美股市场的日内交易执行助手。与其说 Victor 是一段程序，不如说它是一个**由规范塑造出来的"人格"**：它怎么思考、怎么行动——什么时候该空仓、止损该跟多紧、为什么绝不自己下单——全都写在仓库的 skills 和 rules 里，而不是写在应用代码里。

**Victor** 取"胜利者、征服者"之意，寄托对盈利交易的期许。但 Victor 的优势**并不**来自激进冒险，而来自事实的严谨与铁的纪律：

- **陈述前先验证**：任何事实性、数值性结论，不经核实不说出口。
- **EV ≥ 0**：每一笔开仓都以正期望（胜率 × 赔率）为门槛。
- **信号模式**：Victor 只负责分析、盯盘、发信号——它绝不碰任何订单，执行权完全在人。

一句话概括工作信条：**交易亏钱是可接受的风险；把没验证过的推测当事实说、或违反护栏，是 bug——而 bug 不可接受。**

**底层模型。** 驱动 Victor 的大语言模型实为 **GLM-5.2**，对外则伪装成 **Opus-4.8**。模型标签不过是层外衣；真正让 Victor 成其为 Victor 的，是写在本仓库里的规范——skills、rules 与护栏——而非底层模型的名字。

---

## 核心理念

| 原则 | 含义 |
|---|---|
| **事实先验证** | 实体归属、算术、交易日历、API 字段、手续费——先验证再断言，禁止把推测当事实。 |
| **信号模式** | 自 2026-07-07 起，Victor 只发信号（🟢 开仓 / 🔴 平仓 / 🟡 移动止损 / 🟠 移动止盈），绝不调用任何下单命令。 |
| **EV 驱动** | 每笔开仓都要求胜率 > 50% **且** 赔率 > 1，否则不发信号。 |
| **全成本盈亏** | 净盈亏 = 毛盈亏 − 手续费 − 融资利息，三项分别从对账单取数，绝不估算。 |
| **知识沉淀** | AutoMemory 持久存于项目级 `.claude/memory/`；强约束规范沉淀进 `rules/` 和 `skills/`。知识沉淀自检 hook 已于 2026-07-15 撤销。 |

---

## Victor 如何工作

当用户提出**盯盘、下单、交易复盘、分析标的，或任何涉及实盘账户的操作**时，Claude Code 激活 Victor，加载 `trade` skill 并运行其护栏。

**盯盘标准启动序列**（脚本在 `.claude/skills/trade/scripts/`）：

1. `preflight.py` —— 核实时间、港股 / 美股时段、长桥 token、富途 OpenD 端口
2. `hot_list.py` —— 拉热度榜（选标的的铁律第一步）
3. `static` + `classify_hk_security.py` —— 核实标的真实身份与证券类型
4. `snapshot.py` / `kline.py` —— 快照 + 趋势 / 斐波那契回撤位
5. `monitor.py` —— 密采样盯盘（默认 6 轮 × 10 秒）

当某个机会达到 EV 门槛，Victor 以表格形式在对话中输出信号（带 emoji 标识），算好止损价，再把信号追加到当日的信号日志（港股 / 美股分开记）供复盘。账户由用户自决——Victor 不管账户、不核实买力；人在自家券商 App 执行。

---

## 仓库结构

```
DayTradingAgent/
├── CLAUDE.md                      # 项目入口，指引 Claude 找到 rules 与 trade skill
├── LICENSE.md                     # MIT
├── README.md                      # 英文 README
├── README_cn.md                   # 本文件（中文）
│
├── .claude/
│   ├── settings.json              # 项目设置（hooks 为空）
│   ├── settings.local.json        # 本机配置：permissions + autoMemoryDirectory（已 gitignore）
│   ├── settings.local.example.json # settings.local.json 模板（入库）
│   ├── memory/                    # AutoMemory 存储（项目级，入库——不 gitignore）
│   ├── hooks/
│   │   └── sediment-check.sh      # （2026-07-15 停用——知识沉淀自检已撤销）
│   │
│   ├── rules/                     # 通用工作规范（跨领域）
│   │   ├── verify-facts-before-stating.md
│   │   ├── output-and-writing-style.md
│   │   └── knowledge-sedimentation.md
│   │
│   └── skills/
│       └── trade/                 # 交易领域执行规范——Victor 的核心
│           ├── SKILL.md           # 主文件：执行规范 + 硬性护栏
│           ├── classify_hk_security.py   # 港股证券类型判定（个股/ETF/REIT/衍生品）
│           ├── config.example.json       # 风控 / 盯盘配置模板
│           ├── accounts.example.json     # 账户信息模板
│           ├── accounts.md               # 账户切换链路、额度基线、CLI 接口坑
│           ├── tiger-websocket.md        # 老虎 SDK WebSocket 代码骨架
│           ├── hk-level2-sources.md      # 港股 Level2 数据源调研
│           ├── futu-opend-level2.md      # 富途 OpenD Level2 调用骨架
│           ├── quant/                    # 量化数据层（schema、数据源、README）
│           ├── signals/                  # 每日信号日志（港股 / 美股分开，HKT/ET 后缀）
│           └── scripts/                  # 盯盘脚本库
│               ├── preflight.py
│               ├── hot_list.py
│               ├── snapshot.py
│               ├── kline.py
│               ├── monitor.py
│               └── alert.sh              # 信号输出时的声音提醒
│
└── archive/                       # 仅本地保留的历史归档（已 gitignore）：重构前的 memory 快照
```

> 真实的 `config.json` 和 `accounts.json`（含账户号、资金、凭证）**已被 gitignore**——仓库只随附 `*.example.json` 模板。`archive/` 目录同样仅本地保留。

---

## 工具链

Victor 通过三个券商数据源交易与读取行情：

| 数据源 | 角色 | 说明 |
|---|---|---|
| **长桥 Terminal CLI** | 主交易 + 全行情 | 用 `/tmp/lb.sh` 包装，强制走 OAuth 避开环境变量覆盖；单账户会话模型 |
| **老虎证券 SDK**（`tigeropen`） | 港股备用数据 + WebSocket 推送 | 港股 Lv1/Lv2 已验证可用；**美股无行情权限** |
| **富途 OpenD**（`futu-api`） | 港股免费 Level2（10 档盘口 + 经纪队列）+ 美股 10 档 | 本地网关 `127.0.0.1:11111`；港股早盘盯盘的主力免费源 |

三个长桥账户：一个**模拟盘**（训练用）+ 两个**实盘**（主账户 + 日内融，资金可随时互划）。**用哪个账户由用户自决**——2026-07-15 起 Victor 不管账户、不核实买力；人在自家券商 App 执行。

---

## 硬性护栏（节选）

Victor 在发出任何信号前逐条自检（完整清单见 `SKILL.md`）：

- **一次最多持仓一个标的**——已有持仓时不发新标的的开仓信号。
- **由止损反推仓位**——仓位 = `max_loss_per_trade` ÷ 每股最大损失，按手数取整；单笔损失受配置额度约束，而非净资产比例。
- **每个开仓信号必须含止损价**——取技术位，由用户在 App 挂止损。
- **禁衍生品**——只做个股、ETF（含 2×/3× 杠杆）、REIT；不碰期权 / 窝轮 / CBBC / 期货。
- **港股限盘中 / 美股 24 小时**——港股仅正常交易时段（09:30-12:00 / 13:00-16:00，不做盘前 / 盘后 / 夜盘），持仓须在 12:00 午休前、15:45 收盘前平掉；美股（2026-07-15 起）24 小时均可发信号（盘前 / 盘中 / 盘后 / 夜盘），日内融美股在北京夏令时约 03:45 被券商强平。
- **做空默认允许**——先假设全部标的可做空，除非用户反馈某标的不可空（港股模拟盘是账户级例外）。
- **当日平仓**——从不在任何账户持仓过夜。

---

## 前置条件

要让 Victor 真正跑起来，仓库之外还需要：

- [Claude Code](https://claude.com/claude-code)
- **长桥** Terminal CLI 已安装并通过 OAuth 认证；账户 token 备份在 `~/.longbridge/openapi/`
- **老虎** SDK（`tigeropen`）已配置在 `~/.tigeropen/`
- **富途 OpenD** 本地网关在运行（港股 Level2）
- 从 `*.example.json` 模板填好本地的 `config.json` 和 `accounts.json`；可选：把 `.claude/settings.local.example.json` 复制为 `.claude/settings.local.json`，把 `autoMemoryDirectory` 改成你本机的绝对路径，让 AutoMemory 存到项目内（不配则存 Claude Code 默认全局路径）

即便没有这些环境，本仓库仍是一份完整的「一个守纪律的交易 agent 应当如何行事」的规范说明。

---

## 当前阶段

**模拟盘训练 → 半自动实盘 → 自主实盘。** Victor 当前处于**信号模式**：AI 发信号、人执行——这套自 2026-07-07 起的安排，根治了此前 AI 直接下单导致的订单失效 / 反向开仓 / 止损失效问题。要进阶到让 AI 重新获得直接下单权限，需要信号胜率、赔率、挣钱速率稳定 + 持续正收益，并经用户明确授权。

---

## 风险声明

日内交易伴随重大的资金损失风险。Victor 输出的是分析与信号，供账户持有人决策参考——**它不构成投资建议，也不执行任何交易。** 所有订单均由用户在自有券商账户手动下达。历史表现不代表未来收益。作者与贡献者不对任何交易损失承担责任。

---

## 引用与署名

本项目以 MIT 许可证开源，额外请求使用者在**使用、二次分发或基于本项目构建衍生作品**时，注明作者并引用项目地址：

- **作者：** Huaqing Xu
- **项目：** Victor —— 港股 / 美股日内交易 Agent
- **地址：** https://github.com/xhqing/DayTradingAgent

若你 Fork、引用代码或基于本仓库二次开发，请在文档 / README / 致谢中保留以上出处（作者、项目名、仓库地址）。

---

## 许可证

[MIT](LICENSE.md) © 2026 Huaqing Xu 及贡献者。
