# 账户切换链路与额度基线

长桥 Terminal CLI + 老虎 SDK 的账户管理参考。

## 账户分类（2026-07-10 用户定义）

| 类别 | 账户 | 前缀 | 用途 |
|---|---|---|---|
| 模拟盘（1） | 长桥模拟盘 | `LBPT` | 训练 / 策略验证（港股不可做空） |
| 实盘（2） | 长桥主账户 | `H` | 实盘交易（**不强平**，日内须主动平） |
| 实盘（2） | 长桥日内融账户 | `H` | 实盘交易（**收盘强平**，美股可"设止损睡"） |

两实盘**资金可随时互划**。实盘交易日**用哪个实盘账户由 AI 决定**（见 SKILL「实盘账户对比与选择」）。

## 长桥 CLI 包装器

所有长桥命令用包装器，强制走 OAuth 避开环境变量覆盖（`.zshrc` 注释的 `LONGPORT_*` 仍可能被注入，CLI 优先读环境变量报 401003 token expired）：

```bash
# /tmp/lb.sh 内容：
#!/bin/bash
exec env -u LONGPORT_APP_KEY -u LONGPORT_APP_SECRET -u LONGPORT_ACCESS_TOKEN \
     ~/.local/bin/longbridge "$@"
```

## 三账户切换链路（cli-auth 是单一文件，登录即覆盖）

CLI 是单账户会话模型（`auth status` 只返回一个 account，无多账户/切换命令）。三账户 token 备份齐全，切换用 `cp` 还原 + `auth status` 验证 `account_no`：

> ⚠️ **cli-auth 文件性质**：路径 `~/.longbridge/openapi/cli-auth`（注意在 `openapi/` 子目录、无 `.json` 后缀），文件是长桥 CLI 自定义的**加密二进制容器**（magic number `4c 42 01` = "LB\x01"），**不是 JSON 文本**。用文本编辑器打开会显示空白或乱码，`json.load` 会报 `UnicodeDecodeError`——这是正常现象，不是文件损坏。Token 明文只有 CLI 内部用 OAuth client secret 解密后才能拿到，人手不该也不需要直接读改。想知道当前账户跑 `auth status`，想换账户 `cp` 还原备份，想重新拿 token 跑 `auth login`。

| 账户类型 | 还原命令（cp 备份 → cli-auth） |
|---|---|
| 模拟盘 | `cp ~/.longbridge/openapi/cli-auth.papertrading.bak ~/.longbridge/openapi/cli-auth` |
| 主账户 | `cp ~/.longbridge/openapi/cli-auth.real-main.bak ~/.longbridge/openapi/cli-auth` |
| 日内融 | `cp ~/.longbridge/openapi/cli-auth.intraday-margin.bak ~/.longbridge/openapi/cli-auth` |

> 各账户的 `account_no`、净资产、买力、可用资金、融资授信等敏感数据见 `accounts.json`（已 gitignore）。切换后用 `auth status --format json` 核对 `account_no` 与下方 channel。

区分账户看 `auth status --format json` 的 `account.account_channel`：
- 模拟盘 = `lb_papertrading`，account_no 前缀 `LBPT`
- 真实账户 = `lb`，account_no 前缀 `H`

**实盘账户由 AI 决定（2026-07-10 用户授权）**：AI 仍不下单（信号模式不变），但用户让做**实盘交易**时，**用哪个实盘账户（主账户 / 日内融）由 AI 决定**并写入信号「账户」字段。两实盘差异与选择决策见 SKILL「实盘账户对比与选择」（核心：日内融收盘强平可巧妙用于美股"看好设止损睡"；主账户不强平、日内须主动平）。AI 仍按需切换账户查 `positions`/`assets` 核实数据。

## 主账户资金状况

> 净资产、买力、可用港币/美元、融资授信等数值见 `accounts.json`（已 gitignore），**使用前用 `longbridge assets` 实时核实**（资金随时变化，勿直接引用快照数字）。

- 买力近零时主账户下不了单，需等资金到账或用日内融账户。新规则下仓位由"`risk.max_loss_per_trade` ÷ 每股损失"反推，但仍受买力硬约束。
- 融资授信可用但用融资要付利息、美元已负杠杆风险高（具体额度见 `accounts.json` 的 `max_finance_usd`）。
- 行情权限：港股 LV1+LV2、美股 LV1+QBBO、OPRA 期权行情、恒生指数。⚠️ 期权行情开通但禁衍生品硬规矩不变。

## 日内融账户资金状况

> 净资产、买力、可用港币/美元、融资授信等数值见 `accounts.json`（已 gitignore），**使用前用 `longbridge assets` 实时核实**。日内融买力含杠杆、是用户实盘下单的主要可用账户（主账户买力近零）。

- **额度基线表**（2026-07-03 休市实测快照，用 7/2 收盘价、`max-qty --side buy`，**盘中动态变化需实时重查**）：

  | 标的 | 收盘价 | CashMax | MarginMax | 保证金率(初始/维持/强平) |
  |---|---|---|---|---|
  | 2800.HK 盈富ETF | 23.80 | 5000 | 5000 | 0.2/0.18/0.15 |
  | 700.HK 腾讯 | 431.20 | 200 | 200 | 0.3/0.2/0.15 |
  | 3690.HK 美团 | 71.60 | 1600 | 1600 | 0.25/0.2/0.15 |
  | 0823.HK 领展REIT | 37.92 | 3100 | 3100 | 0.3/0.25/0.2 |
  | SPY.US | 744.78 | 0 | 137 | 0.4/0.35/0.3 |
  | TSLA.US | 393.45 | 0 | 260 | 0.45/0.3/0.25 |

  规律：港股 CashMax=MarginMax（融资未加码），美股 CashMax=0 但 MarginMax 有值（美元现金负，纯靠融资买美股）。

- **强制平仓约束**：日内融持仓收盘前 15 分钟被券商强平，但 CLI 查不到（assets/portfolio 无相关字段）。港股 **15:45 HKT** 强平；美股美东 15:45 = **北京夏令时 03:45 / 冬令时 04:45** 强平。⚠️ 主账户**不强平**（日内须主动平，从不过夜）。账户选择与"看好设止损睡"策略见 SKILL「实盘账户对比与选择」。

## 做空权限（实测）

- 港股账户级不可做空（模拟盘 + 真实主账户均报 603301，含盈富 ETF/腾讯）。
- 美股账户级可做空（TSLA 远价单 order sell 成功并秒撤，Order ID 1257651500528758784）。
- CLI 无融券额度字段；`short-positions`/`short-trades` 是市场统计非权限接口。判定做空靠远价实单测试（卖单价远离市价、下单成功立刻 cancel）。
- 日内融做空权限待开盘测。

## CLI 接口坑（实测）

- `max-qty <sym> --side buy --price <P>` 和 `margin-ratio <sym>` 的 JSON 返回是 `[{field,value},...]` 键值对数组**不是对象**。取值：`m={x['field']:x['value'] for x in d}`，key 是 "Cash Max Qty"/"Margin Max Qty"/"Initial Margin Ratio"/"Maintenance Margin Ratio"/"Forced Liquidation Ratio"。别按对象字段直取会得 None。
- `quote` 字段是 `last`（不是 last_done），休市返回最近收盘价，含 pre_market/post_market/overnight 子段。
- 港股代码格式：港股标准代码本就是 **5 位数字**（如 `02800`、`00005`、`03688`），只是大部分第一位是 0。**老虎要求完整 5 位**（`'02800'`），**长桥两者都接受**（`2800.HK` 和 `02800.HK` 均可，返回的 symbol 跟随输入格式）。跨源调用若代码不匹配，规则是补/省前导 0，不是两套不同体系。

## 老虎 SDK（tigeropen v3.6.0）

- 配置文件：`~/.tigeropen/tiger_openapi_config.properties`（权限 600），含 `tiger_id`/`account`/`license`/`private_key` 等（**明文凭证见 `accounts.json` 的 `tiger` 段，已 gitignore**）。
- 加载：`TigerOpenClientConfig(props_path='~/.tigeropen/')`。
- 服务地址：SDK 默认 `https://openapi.tigerfintech.com/gateway`，socket `openapi.tigerfintech.com:9883` ssl。实测 TBNZ 可用，无需手动改 itigerup.com。
- **行情权限**：港股 Level1 免费（已验证 `get_stock_briefs` 可用、与长桥交叉验证一致）。**美股无行情权限**（TBNZ 报 `4000: permission denied`）。✅ **港股 Lv2（10 档）实测可用**（2026-07-10 盘中：`subscribe_depth_quote` 返回 ask/bid 各 10 档，含 price/volume/orderCount；expire=-1 永久免费。depth 未见经纪队列，broker 需富途/长桥）。**此前"海外需购 hkStockQuoteLv2Global"推断已被实测推翻**。
