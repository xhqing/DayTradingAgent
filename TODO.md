# DayTradingAgent 待办清单

> 本文件是项目**唯一**的待办存放处（全局 CLAUDE.md「待办统一存项目根 TODO.md」规矩，2026-08-02 修订）。
> 待办处理后（原条目一律保留不删、内容不动，只在开头挂标记）：**完成**（事情做完）开头 `[ ]` 换成 `✅**已完成**`、补 `（完成：YYYY-MM-DD HH:MM）`；**更新**（表述被新决定取代、事没做完）原条目开头 `[ ]` 换成 `✅**已更新**`、补 `（更新：YYYY-MM-DD HH:MM）`，**另起一段新增** `[ ]` 条目写最新内容——「已完成」「已更新」加粗区分两种状态。并把已做出的改动（增 / 删 / 改）记入 `CHANGELOG.md`。
> 每条待办带记录时间戳（精确到分钟）；前后矛盾的待办以最新时间戳为准（全局 CLAUDE.md 2026-08-01 补充规矩）。

## 交易 / 持仓处置

- [ ] **关闭 MU 误开多头仓位**（记录：2026-08-01 22:54）：2026-07-31 smart_order 市价单被拒却误报成交 → close_position 反向买入开多 158 股 @ 828.575 USD（实测 2026-08-01 持仓确认）。计划下一个美股开盘（21:30 HKT）用 `close_position.py`（一键平仓）平掉，开盘前重新 `get_open_position` 核实持仓再执行。

## 代码 / 机制

- ✅**已完成**（完成：2026-08-02 18:59）**`load_equity` 改用账户 API 取真实总资产**（记录：2026-08-01 22:54）：`trade_utils.load_equity`（open_position 自动算仓位用）仍读 `signals/equity-log.csv`（旧信号模式累加值、不真实）；应改为像 `preflight.py` / `resume.py` 那样从长桥 `account_balance().net_assets` 取（2026-07-31 已修 preflight/resume，open_position 的自动仓位路径漏改）。
- [ ] **实盘实测 `submit_order_with_stop`（开仓附加订单）**（记录：2026-08-01 22:54）：该函数（REST 主单 LO + attached_params STOP_LOSS）签名逻辑已对照文档核对、REST 签名复用已验证的可工作实现，但尚未端到端实跑过一笔真实（paper）开仓。计划在美股开盘时用 paper 账户实测一笔小仓位开仓（确认主单成交 + 附加止损进入监控状态 + 成交回查拿到 fill_price），测完即平。

- [ ] **港股长桥三动作开盘实测**（记录：2026-08-01 22:54）：`open_position_hk.py` / `close_position_hk.py` / `move_stop_hk.py` 已建（解耦独立一套）+ 盘后只读功能实测通过；下单链路（开仓 LO+附加MIT、移损先新增后撤旧、平仓 MO）需港股开盘时用 paper 账户端到端实测一笔小仓位。仅当用户特别说明用长桥时执行。
- [ ] **美股 open_position 端到端实测**（记录：2026-08-01 22:54）：美股 symbol bug 修复后（to_lb_symbol），`open_position.py` 全链路（get_quote→submit_order_with_stop→成交回查）需开盘时实测一笔小仓位确认（此前因 symbol bug 未成功跑过）。
- ✅**已完成**（完成：2026-08-02 18:59）**港股 lot_size 从行情接口取真实每手股数（当前硬编码 100）**（记录：2026-08-02 13:54）：`open_position.py` 自动算仓位时港股 lot_size 硬编码 100（line 130-132），应改为从行情接口取真实每手股数（原代码注释提及富途 snapshot；港股转老虎后需确认实际行情源与取数字段）。

## 港股老虎模拟账户三动作（默认账户，独立解耦一套）

> 港股默认老虎，要实现和长桥一样的三动作，与港股长桥（备选）+ 美股代码分开（分而治之，2026-08-01 用户立）。建好后才能做「港股 default 从 signal 切 auto」（signal 模式保留，两者共存）。

- ✅**已更新**（更新：2026-08-02 18:59）**研究老虎 SDK 港股细节**（记录：2026-08-01 22:54）（开盘 + paper 实测才能确认，盘后做不了）：
  - 配置加载：`get_client_config(props_path=...)` 失败，要研究老虎 paper 配置正确加载方式（`~/.tigeropen/tiger_openapi_config.properties` + 私钥）。
  - 港股 symbol 格式：`stock_contract` 接受 `02800`/`HK.02800`/`2800.HK`，但老虎 API 实际认哪个要实测（行情/下单）。
  - lot_size：老虎 `get_contract` 查字段；tick：`get_contract` 或港交所价位表。
  - 附加止损 `order_leg('LOSS', price)` 实测（主单 LMT + 附加止损，对应长桥 attached STOP_LOSS）。
  - 老虎下单/平仓/止损机制逐一实测，**不能从长桥外推**（verify-facts：券商行为只信直接实测）。
- ✅**已完成**（完成：2026-08-02 18:59）**建 `trade_utils_tiger.py`**（记录：2026-08-01 22:54）（老虎封装，自包含不依赖长桥/美股）：配置加载、港股 symbol/lot/tick、place_order（开仓 LMT+附加止损）、平仓 MO、移损止损单（先新增后撤旧）、查持仓/资产/订单、撤单、成交回查。
- ✅**已完成**（完成：2026-08-02 18:59）**建 `open_position_tiger.py` / `close_position_tiger.py` / `move_stop_tiger.py`**（记录：2026-08-01 22:54）（与规范一致：开仓 LO+附加止损、平仓 MO+撤止损、移损先新增后撤旧且量=持仓）。
- [ ] **开盘实测港股老虎三动作全链路**（记录：2026-08-01 22:54）（paper 账户小仓位）。

- ✅**已更新**（更新：2026-08-02 19:06）**老虎 paper 接入：17 位模拟账户号 + 开盘实测三动作下单链路**（记录：2026-08-02 18:59）：2026-08-02 盘后已完成「研究老虎 SDK 港股细节」的可盘后部分（详见上条 `✅**已更新**` 标记与 CHANGELOG 2026-08-02 记录）：① 配置加载根因 = `get_client_config` 必须先传 private_key_path（None 直接 TypeError），正确姿势 `TigerOpenClientConfig(props_path=~/.tigeropen/)`（私钥自动从 private_key_pk1/pk8 读，实测加载成功）；② paper 判定 = account 为 17 位纯数字账户号即 is_paper=True、网关自动走 license-PAPER 域名（domain_conf 已含 TBNZ-PAPER，实测确认）；③ 港股 symbol 只认 5 位带前导 0 裸代码（'02800'/'00700' 实测 OK，HK.02800/2800.HK/700.HK 均拒）；④ lot_size / tick 从 `get_contract` 取（实测 02800=500、00700=100、tick_sizes 完整价位表）；⑤ TradeClient API 结构与只读链路（get_assets→summary.net_liquidation / get_positions / get_orders / cancel_order / create_order+place_order / OrderLeg('LOSS') 附加腿仅限价单支持）全部实测通过；`trade_utils_tiger.py` + 三动作脚本已建（下单链路标注待实测）。**剩余**：a) 用户提供 17 位 paper 模拟账户号写入 `~/.tigeropen/tiger_openapi_config.properties` 的 account 字段（当前是 7 位实盘号 <HK_LIVE_ACCOUNT>，且该实盘账户未开通交易/资产权限——get_assets 返回全 0）；b) 港股开盘时用 paper 账户实测三动作全链路（开仓 LMT+附加止损腿提交与激活、平仓 MKT、移损 STP 先新增后撤旧、成交回查），实测结果回写脚本与文档。

- [ ] **老虎 paper 账户开盘实测三动作下单链路（账户号已接入，2026-08-02 19:06 完成接入）**（记录：2026-08-02 19:06）：17 位模拟账户号 `<HK_PAPER_ACCOUNT>`（从 git 历史 2514d76 恢复）已写入 `~/.tigeropen/tiger_openapi_config.properties` 的 account 字段（原 7 位实盘号备份在 `.bak`），实测：is_paper=True、网关自动走 sandbox 域名、`get_assets` 净值 1,000,007.56 USD（初始资金 $1,000,000）、`get_positions` 有 **2 笔历史持仓**（02800 盈富多 500 股 @26.3639、07709 三星 2x 杠杆空 200 股 @43.0638）——开盘实测时先核实这两笔怎么处置（平掉 / 保留）。**剩余**：港股开盘时用 paper 账户实测三动作全链路——开仓 LMT+附加止损腿（OrderLeg('LOSS')）提交与激活、平仓 MKT、移损 STP 先新增后撤旧、成交回查；实测结果回写 `trade_utils_tiger.py` 三脚本与 `auto-mode.md` 实测状态标注（实测前标注为「待实测、不得真实下单」）。

## 港股 default 从 signal 切 auto（依赖港股老虎三动作建好；signal 模式保留，两者共存）

- ✅**已更新**（更新：2026-08-02 14:14）**港股完全转自动交易**（记录：2026-08-01 22:54）：港股从信号模式（AI 发信号 `signals/`、手动执行）转自动交易（AI 调脚本下单、`actions/` 记动作）。**信号模式描述已随 2026-08-01 双模式重构处理**——signal 已作为独立可切换 reference（`references/signal-mode.md`）恢复、主 `SKILL.md` 改双模式（默认 auto + 可切 signal），不再是「要改掉的旧模式」。剩余待办：港股老虎三动作建好后把港股 default 切 auto（港股目前仍 signal——老虎三动作未建，见上方「港股老虎模拟账户三动作」节）。**依赖港股老虎三动作先建好**（否则港股 auto 文档 ahead of 代码）。
- [ ] **港股 default 从 signal 切 auto（signal 模式保留，两者共存）**（记录：2026-08-02 14:14）：当前决定 = 信号模式与自动交易模式二者共存，不是放弃 signal 改纯 auto。港股目前 default 仍为 signal（AI 发信号 `signals/`、手动执行）；待港股老虎三动作建好后把港股 default 切 auto（AI 调脚本下单、`actions/` 记动作）。**signal 模式保留为可切换模式**——用户说「只发信号 / 手动执行」时仍切 signal；两种模式唯一区别 = 是否使用证券账户，交易策略完全通用（主 `SKILL.md` 已是双模式公共骨架 + `references/auto-mode.md` + `references/signal-mode.md`，2026-08-01 重构完成，此处不重复）。**依赖港股老虎三动作先建好**（否则港股 auto 文档 ahead of 代码）。
