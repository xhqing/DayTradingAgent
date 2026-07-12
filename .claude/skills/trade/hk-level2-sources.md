# 港股 Level2 数据源调研结论

2026-07-03 全网调研免费/便宜的港股 Level2（定义=10 档深度盘口）。经 anysearch + WebFetch 核实，结论如下。

## 核心定价锚点（HKEX 官方，从 hkex.com.hk 核实）

港交所"10 档深度盘口"官方名称 = **Level 2 Data**（产品 OMD Securities Premium）。描述：最优 10 档买卖盘的订单数 + 聚合量。**还包含经纪队列**（超出 10 档定义的一层）。HKEX **不直接对个人零售**，通过 Information vendors 批发。

港交所向信息供应商收的批发价（HK$/订阅者/月）：

| 产品 | 内容 | 批发价 |
|---|---|---|
| Level 1 | 最优一档 | $120/月 |
| **Level 2 (SP)** | **10 档深度 + 经纪队列** | **$200/月** |
| Level 2+One (SF) | 10 档 + One | $240/月 |
| Full Book (SF) | 全部订单 | $400/月 |

**关键推论**：任何券商"免费"的 Level2 = 券商替你付了这 $200/月（补贴获客）。

## 候选源核实

### 富途证券 OpenAPI（OpenD）—— 最确定的免费路径 ★
- 富途首家免费提供港股 Level2，与港交所达成协议、由富途购买行情免费给用户（年费超 1000 万港币）。
- OpenAPI 文档明确：**无需开户**，注册富途牛牛号/moomoo 号、装 OpenD 本地网关即用。WebSocket 订阅推送模式。美股深度也免费了。
- ✅ **已实测（2026-07-06）**：海外账户(moomoo)免费，港股 10 档盘口 + 经纪队列齐全，`subscribe ORDER_BOOK/BROKER` ret=0 即有权限。详见 `futu-opend-level2.md`。
- 行动：注册 moomoo 账号 → 装 OpenD → 实测 depth 订阅。零成本试。

### 老虎证券 OpenAPI（已接入）—— TBNZ 需购买
- `hkStockQuoteLv2Global` = 10 档买卖盘 + 逐笔成交 + 经纪队列（符合定义）。
- 地域限制：大陆用户免费（`hkStockQuoteLv2`），**非大陆用户需购买**（`hkStockQuoteLv2Global`），价格未公开。
- 实情：TBNZ = 新西兰 = 非大陆 = 需购买。与美股 permission denied 一致。
- 行动：联系老虎客服问 Global 版价格，便宜则在已通的老虎 SDK 上激活（subscribe_depth_quote 链路已验证）。

### CTradeExchange/free-quote（alltick.co）—— 合规红旗 ⚠️
- 标榜免费实时 10 档港股盘口，126 stars，需申请 token。
- ⚠️ 数据来源完全不透明，无来源说明/免责声明/使用条款，极可能爬取券商/交易所，违反数据使用条款。
- 仅适合随手测试，**不建议进实盘链路**。

### 排除的
- **东方财富 emquant**：仅 5 档，主要 A 股。
- **AKShare**：开源合规，但港股实时盘口档数有限、非实时推送。适合盘后复盘拉历史，不适合盯盘。
- **开源爬虫**（futu_tick_downloader 等）：本质依赖富途数据源，合规取决于富途条款。

## 按场景推荐

| 场景 | 首选 | 备选 |
|---|---|---|
| 早盘盯盘（实时 10 档推送） | 富途 OpenD（免费 WebSocket） | 老虎 Lv2 Global（若购买） |
| 多源交叉验证 | 富途 + 长桥 + 老虎三源对比盘口 | free-quote（仅测试不实盘） |
| 盘后复盘（历史逐笔） | AKShare 历史 + 富途逐笔 / 长桥 statement | futu_tick_downloader 存盘 |

## 优先行动序列

1. 先试富途 OpenD（零成本）：注册 moomoo → 装 OpenD → 实测港股 depth 订阅能否 10 档 + 海外是否免费。最可能直接解决。
2. 若富途海外不免费 → 问老虎客服 `hkStockQuoteLv2Global` 价格，便宜则在已通的老虎 SDK 激活。
3. free-quote 仅作临时对照测试，不进实盘。
4. 长桥 CLI 的 LV2 + broker queue 命令继续用，补充经纪队列维度。
