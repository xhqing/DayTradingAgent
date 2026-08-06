# 行情数据源（详细参考，公共）

> 本文件是 trade skill 的**行情数据源**详细参考——富途 OpenD / 老虎 SDK 的行情配置与坑、数据源分工、标的类型判定、做空能力查询、港美股代码格式。**公共内容，auto 与 signal 两种模式共用**（两种模式都要盯盘取行情、都要核实标的类型排除衍生品）。
>
> **下单 API、订单机制、下单账户配置**是 **auto 模式专属**（signal 模式不下单、不需要），见 `references/auto-mode.md`（交易动作脚本 + auto 专属硬护栏）。本文件不再讲下单，只讲行情数据源。

## 行情数据源分工

盯盘行情数据走富途 + 老虎两家（详见下方「数据源分工总则」）。auto 模式的下单账户（港股、美股均默认老虎开放平台模拟）见 `SKILL.md`「模式开关」与 `auto-mode.md`；行情数据两模式一致。

### 富途 OpenD（盯盘行情主力源）

`futu-api` v10.08 + OpenD v10.8.6808 本地网关，127.0.0.1:11111。**盯盘行情主力源（港股 + 美股全覆盖）**：

- 港股免费 Level2（10 档盘口 Bid/Ask 各 10 + 经纪队列 broker id）
- **资金流**（`get_capital_distribution` 含 super 超大单 + `get_capital_flow` 分钟序列 + `get_top_ten_buy_sell_brokers` 十大买卖经纪，实测均 ret=0、维度最丰富）
- **美股免费 10 档深度盘口 + 美股资金流**（实测可用）
- **分钟 K 5.5 年** + 板块 / 产业链 / 研报 / 股东 / 美股盘前盘后夜盘涨跌榜（富途独有）。海外账户(moomoo)登录即得。

⚠️ OpenD 需登录成功才开端口（未登录 11111 不监听，futu-api 报 ECONNREFUSED）；改密后更新 `~/FutuOpenD/FutuOpenD.xml`（清空 `<login_pwd_md5>` + 放出 `<login_pwd>` 填明文，富途规则「密文存在只用密文」）。摆盘 / 经纪队列是订阅制，调 `get_order_book` / `get_broker_queue` 前必须先 `subscribe`，代码骨架见 `futu-opend-level2.md`。

### 老虎证券 SDK（盯盘行情港股备份源 + WebSocket 毫秒级推送主力）

`tigeropen` v3.6.0，配置在 `~/.tigeropen/`，账户凭证见 `accounts.json`。**盯盘行情港股备份源 + WebSocket 推送主力**（突破轮询瓶颈）：

- 港股 `get_depth_quote` / `subscribe_depth_quote` 返回 ask/bid 各 10 档（**实测可用**）
- `get_capital_distribution` 港股资金分布可用（实测，含 net_inflow + 大中小单）
- `get_stock_broker` 个股经纪

⚠️ 老虎 TBNZ 账户**美股无行情权限**（故美股只能靠富途单源）。⚠️ 同步 `QuoteClient` 初始化须 `TigerOpenClientConfig(props_path='~/.tigeropen/tiger_openapi_config.properties')` 构造（私钥自动加载，直接 `QuoteClient()` 会报 private key empty）；WebSocket 用 `PushClient`。代码骨架见 `tiger-websocket.md`。

## 数据源分工总则（最高优先级）

盯盘行情数据**走富途 + 老虎两家**。富途是主力（港股 + 美股全覆盖）、老虎是港股备份 + WebSocket 推送源。实测依据：**资金流富途最丰富**（分布含 super 超大单 + 分钟序列 + 十大买卖经纪）、**分钟 K 富途深 5.5 年**、**实时性富途 / 老虎是 WebSocket 毫秒级**。⚠️ 老虎 TBNZ 账户**美股无行情权限**，故美股只靠富途单源。完整对比表见 `futu-opend-level2.md`「双源对比与选用」。

## 做空能力以实际账户查询为准（auto 模式）

接入交易账户后，标的能否做空、券源是否充足，由 AI 开仓前查询账户的做空信息（老虎 API 的标的做空状态、可借券数量）确认，不凭假设。查询不到或不可做空则放弃该标的的做空、换标的或换方向。做空工具（反向 ETF vs 融券正股）按胜率优先选择，哪个胜率更高就用哪个。

> signal 模式不查账户，默认所有标的可做空、发信号不问（用户在自家 App 执行时自决可行性），用户反馈「某标的不可空」时才排除。

## 标的判定（`classify_hk_security.py`，目的：排除衍生品）

富途快照 / quote 无 type 字段，需用 `classify_hk_security.py` 判定标的类型。**判定的唯一目的是排除衍生品**（期权/窝轮/CBBC/牛熊证，禁交易）——ETF、个股、REIT 都可交易，没有白名单/黑名单限制。脚本用四层启发式识别类型：

1. **衍生品代码段 + name 关键词**（call/put/牛熊/窝轮）→ 判衍生品，禁交易。港股衍生品段：1xxxx/2xxxx/27xxx。**BULL/BEAR 特判**：代码在 ETF 段 → 视为杠杆/反向 ETF（允许）；不在 ETF 段 → 牛熊证/CBBC（禁）。
2. **HKEX ETF 名单**（脚本内置，含 02800/02828 等主力 ETF）→ 识别为 ETF 的高置信信号（仅辅助识别，不是交易许可）。
3. **name 含 ETF/FUND/TRACKER/INDEX 关键词** → 识别为 ETF（盈富 02800 是已知异常：name=TRACKER FUND 不含 ETF，靠第 2 层名单命中）。
4. **ETF 代码段启发式**（028xx/030xx/031xx/07xxx）→ 疑似 ETF，结合 name 关键词确认。

**REIT**：name 含 REIT/TRUST（如 00823 LINK REIT）→ REIT，港股免印花税（与 ETF 同），靠 name 关键词识别。

**-W/-S/-SW 后缀**：同股不同权（-W）/第二上市（-S），仍为个股（如 09988 BABA-W、09618 JD-SW）。

**执行**：开仓前 `python3 .claude/skills/trade/classify_hk_security.py <标的>`。**只有判定为衍生品时才不执行**；判定为 ETF/个股/REIT（任意置信度）都可交易。港股优先在 ETF 池找顺势机会（免税），个股仅当 ETF 无法覆盖该板块时考虑。

## 标的范围与衍生品禁令

仅个股（含 ADR）+ ETF。**术语习惯**：用户说「港股」「美股」时**默认含 ETF**（港股市场交易的 ETF + 个股都算），除非单独强调「个股」。**禁止衍生品**（期权 / 窝轮 / CBBC / 期货），不碰。港股偏好 ETF（**免港式印花税，仅港股**——美股无 stamp duty、ETF 与个股费用结构相同、无税收优势，勿把港股免税理由误用到美股），美股个股 ETF 均可。**杠杆/反向 ETF（含 2x/3x）允许**（形态是 ETF，内部虽用衍生品实现杠杆，仍视为 ETF 允许交易，如两倍做多海力士/三星电子 ETF、SOXL 三倍做多半导体 ETF）。

## 港美股代码格式

- **富途 / 脚本统一前缀格式** = `市场.代码`：港股 `HK.02800`、美股 `US.SPY`。美股写裸 `SPY` 报 `format of code SPY is wrong`（实测）。
- **富途指数代码 ≠ 股票简称**：查指数趋势用 `HK.800000`（恒生指数 HSI）/ `HK.800700`（恒生科技 HSTECH）——富途里指数是 `8xxxxx` 编码、不是股票简称，写 `HK.HSI`/`HK.HSTECH` 报「未知股票」。指数 `volume=0`、`turnover` 为成分股成交额属正常。
- 港股代码是 5 位数字（如 02800、00005、03688），无语义规律，凭记忆会认错（0823 是领展 REIT 不是汇丰、汇丰是 0005），接触新标的第一件事用富途 `get_market_snapshot([code])` 查 `name` 字段确认真身。

## anysearch skill 定位（不进实时盯盘链路）

anysearch（`.claude/skills/anysearch/`，全局权威副本同步）是通用搜索 skill，含 finance 垂直域 / 批量搜索 / URL 全文提取。⚠️ **实测：`finance.news`(flash) 返回的多源价格互相矛盾且非实时**（CNBC 显阿里 90.10 −5.16% vs 实际 113.40 +2.9%、方向都反；退化为各站网页摘要，非财联社实时快讯流），**绝不可作价格 / 实时事实依据**（违反「第一原则」——富途实时 quote 才是准的）。`finance.calendar`(economic) 结构化字段好（Event/Country/Impact/Previous/Estimate）但覆盖需精准查询（默认 period=7d 返回多为低影响事件、本周关键数据未必命中）。

**定位：批量搜索 + URL 全文提取 + 结构化日历的便利工具，不进实时盯盘链路**——价格 / 资金流走富途，新闻 / 事件核实首选 WebSearch（中文财经经 Z.ai 后端可用、信息可交叉验证），anysearch 仅在「一次查多标的事件」「读某条新闻全文」「拉结构化经济日历」时辅助用。
