# 富途 OpenD 港股 Level2 调用骨架

富途 OpenD v10.8.6808 + `futu-api` v10.08，港股**免费 Level2**（10 档盘口 + 经纪队列）。2026-07-06 实测可用，海外账户(moomoo)登录即得。

## 前置：OpenD 登录（11111 端口）

OpenD 必须登录成功才监听 11111。未登录 → 端口不开 → futu-api 报 `ECONNREFUSED`。检查：

```bash
lsof -nP -iTCP:11111 -sTCP:LISTEN   # 有输出 = 已开
```

登录成功标志：`ctx.get_global_state()` 返回 `qot_logined=True`、`trd_logined=True`。

### 改密后配置更新（`~/FutuOpenD/FutuOpenD.xml`）

富途规则（官方注释原文）：「密码密文存在情况下只使用密文」。改密后旧 `<login_pwd_md5>` 失效 → 登录报「账号名与密码不匹配，还有N次机会」。修复（**无需算 MD5**）：

1. 清空 `<login_pwd_md5></login_pwd_md5>`
2. 把 `<login_pwd>` 从 `<!-- -->` 注释里放出（删掉 `<!-- ` 和 ` -->`）并填新明文

无密文 → 富途改用明文登录 → 成功后自动生成新 MD5、清空明文。改文件用 python 正则按标签结构改（捕获组回引保留密码、不读取凭证），**别 Read 全文**（含 rsa_private_key 等）。⚠️ 登录有次数限制（连续错误锁号），别盲目重启 OpenD 撞锁。

## Level2 调用（订阅制）

```python
from futu.common.constant import SubType   # ⚠️ SubType 驼峰，不是 SUBTYPE(那是另一个 list)
from futu import OpenQuoteContext

ctx = OpenQuoteContext('127.0.0.1', 11111)
try:
    # ① 订阅（摆盘+经纪队列都是订阅制，ret=0 = 有 Level2 权限，休市也能测权限）
    ret, err = ctx.subscribe(['HK.00700'], [SubType.ORDER_BOOK, SubType.BROKER])
    assert ret == 0, f'订阅失败：{err}'

    # ② 十档盘口：Bid/Ask 各 10 档，每档 [价格, 挂单量, 订单笔数, 经纪详情{}]
    r, book = ctx.get_order_book('HK.00700', num=10)   # code 是单个字符串，不是 list
    # book 是 dict: {'code','name','Bid':[[px,vol,orders,{}], ...10档], 'Ask':[...], ...}
    for px, vol, orders, _ in book['Bid'][:5]:
        print(f'买 {px}  量{vol}  {orders}笔')

    # ③ 经纪队列（Level2 独有）：返回三元组 (ret, bid_df, ask_df)，不是二元！
    r2, bid_df, ask_df = ctx.get_broker_queue('HK.00700')
    print('买方经纪', bid_df['bid_broker_id'].tolist()[:10])
    print('卖方经纪', ask_df['ask_broker_id'].tolist()[:10])
finally:
    ctx.close()
```

## 实时推送（开盘后早盘盯盘用）

休市返回收盘**静态盘口**（`svr_recv_time_bid/ask` 为空）。开盘后用 handler 接 WebSocket 推送，毫秒级：

```python
from futu import OpenQuoteContext, SubType, OrderBookHandlerBase

class MyOrderBook(OrderBookHandlerBase):
    def on_recv(self, data):   # data 是实时盘口变化
        print(data)

ctx = OpenQuoteContext('127.0.0.1', 11111)
ctx.set_handler(MyOrderBook())
ctx.subscribe(['HK.00700'], [SubType.ORDER_BOOK])
# 主线程保持运行，盘口变化自动回调 on_recv
```

## 接口要点速查

| 接口 | 签名 | 返回 | 注意 |
|---|---|---|---|
| `subscribe` | `(code_list, subtype_list)` | `(ret, err)` | code_list 是 list；ret=0 才有权限 |
| `get_order_book` | `(code, num=10)` | `(ret, dict)` | code **单个字符串**；dict.Bid/Ask 各 list[num档] |
| `get_broker_queue` | `(code)` | **`(ret, bid_df, ask_df)` 三元组** | 别按二元组解包 |
| `get_market_snapshot` | `(code_list)` | `(ret, DataFrame)` | code_list 是 list |
| `get_global_state` | `()` | `(ret, dict)` | 看 qot_logined/trd_logined 判登录 |

## 热度/排行查询（2026-07-06 实测）

富途专用接口（比 `get_stock_filter` 直接，`get_stock_filter` 港股不支持成交额过滤）：

- `get_hot_list(market, count=N)`：**热度榜**（关注度 = `trade_heat`/`search_heat`/`news_heat` 综合，**不含成交额/价格**）。⚠️ **返回结构是嵌套** `r = (ret, (total, df))`——`r[1]` 是 tuple 不是 DataFrame，取 df 要 `r[1][1]` 或递归找有 `.columns` 的元素；直接 `r[1].columns` 会报 `'tuple' object has no attribute 'columns'`。
- `get_top_movers_rank(market, count=N)`：涨跌幅榜（含 `cur_price`/`change_rate`/`amplitude`/`volume_ratio`），同嵌套结构。
- `get_market_snapshot([code_list])`：快照，用来拿成交额/换手率，叠在热度榜上筛流动性。

**选标的流程**：`get_hot_list` 拿热度头部 → `get_market_snapshot` 查成交额/换手率 → 排除「涨幅高但盘口薄」的小盘题材。实测（2026-07-06）：港股热度榜 ETF = 07709 南方两倍海力士 / 07747 南方两倍三星（热度第 1、5）；美股热度榜头部 = MU/SNDK/NVDA/SOXL（半导体存储链）。

## 美股 Level2（2026-07-06 实测）

富途 OpenD **美股深度盘口也免费可用**：`subscribe ORDER_BOOK` + `get_order_book('US.AAPL', num=10)` ret=0，返回 Bid/Ask **各 10 档**，每档 `[价格, 挂单量, 0, {}]`（如 AAPL 307.51 买 / 307.80 卖）。调用方式跟港股完全一样，把 code 换成 `US.AAPL`/`US.MU` 即可。

**美股 vs 港股 Level2 的差异**（市场结构决定，非权限问题）：

| 维度 | 港股 Level2 | 美股 Level2 |
|---|---|---|
| 盘口深度 | 10 档（价格+挂单量+**订单笔数**） | 10 档（价格+挂单量，**订单笔数=0**） |
| 经纪队列 broker id | ✅ 港交所特有 | ❌ 无（美股多交易所结构没有） |
| 完整深度 | HKEX 单一源 | 各交易所簿（NASDAQ TotalView/ARCA Book 等），富途给聚合 10 档 |

→ 美股没有 `get_broker_queue`（港股特有）；美股深度靠 `get_order_book` 的 10 档价格+挂单量。美股盘前是静态盘口（`svr_recv_time` 空），盘中（夏令时 21:30-04:00 / 冬令时 22:30-05:00）实时推送。⚠️ **2026-07-19 修订：美股盯盘覆盖整个盘中**——五类信号在盘中美东 09:30-16:00（北京夏令时 21:30-次日 04:00 / 冬令时 22:30-次日 05:00）均可发，盯到用户喊停或 16:00 收盘（见 SKILL 护栏 10；撤销原「美东 12:00 之前」限制）。富途推送技术上覆盖整个盘中实时段，发信号窗口即整个盘中；盘前 / 盘后为静态盘口。

## 三源对比与选用（2026-07-15 全维度实测，盯盘数据默认富途 + 老虎）

盯盘行情数据**走富途 + 老虎两家**。2026-07-15 实测结论：长桥行情的每一项（盘口 / 资金流 / K 线 / 快照 / 经纪商）富途都有对应，且**资金流富途更丰富**（分布含 super 超大单 + 分钟序列 + 十大买卖经纪）、**分钟 K 富途深 5.5 年**、**实时性富途 / 老虎是 WebSocket 毫秒级**。⚠️ **长桥 CLI 2026-07-15 已撤销**（token 删除），下表「长桥 CLI」列仅作历史对比参考，盯盘实际不再用长桥。一句话：**盯盘数据走富途 + 老虎**。

| 数据维度 | 富途 OpenD | 老虎 SDK(TBNZ) | 长桥 CLI | 默认用谁 / 谁最强 |
|---|---|---|---|---|
| 港股盘口 depth | ✅ 10 档 + 经纪队列 | ✅ 10 档（无经纪队列） | ✅ 10 档 | 富途（带经纪队列） |
| **资金流 capital** | ✅ **分布含 super 超大单 + 分钟序列 + 十大买卖经纪** | ✅ 分布(大中小单) + flow 序列 | ✅ 分布(大中小单) + flow 序列 | **富途最丰富，盯盘默认走富途** |
| 经纪商 | ✅ broker queue + 十大买卖经纪 | ✅ stock_broker | ✅ brokers | 富途 |
| 行情 / 快照 quote | ✅ 快照 142 字段 | ✅ briefs | ✅ | 富途（字段最全） |
| K 线（日 K） | ✅ ~20 年 | ✅ | ✅ 十几年 | 相当 |
| **分钟 K 历史** | ✅ **5.5 年（2021 起）** | — | ⚠️ 1m 仅 3-4 天 | **富途远优** |
| PE / PB / 换手 / 量比 | ✅ 快照含全部 | — | ✅ calc-index | 富途 |
| 板块 / 产业链 / 研报 / 股东 / 做空 | ✅ 富途独有 | — | ❌ | 富途独有 |
| 美股盘前 / 盘后 / 夜盘涨跌榜 | ✅ 富途独有 | ❌ TBNZ 无美股权限 | ❌ | 富途 |
| 市场情绪 | ✅ 涨跌分布（更细） | ❌ | ✅ market-temp 温度 0-100 | 富途有更细分布替代；长桥温度数边缘独有 |
| **美股行情** | ✅ 10 档 + 资金流（实测 ret=0） | ❌ **TBNZ 无权限** | ✅ | **富途单源；长桥作美股备份** |
| 实时性 | **WebSocket 毫秒级** | **WebSocket 毫秒级** | sleep 轮询 10 秒（最慢） | 富途 / 老虎更快 |
| 成本 | 免费 | 免费（实测可用） | LV2 已开通 | — |
| **交易下单 / 持仓 / 对账单** | ❌ 仅数据 | ❌ 仅数据 | ❌ CLI 已撤销 | 用户自决券商 App |

**盯盘默认链路**：行情 / 盘口 / 资金流 / K 线 / 快照 / 热度 → 富途（主力，WebSocket 毫秒级推送）+ 老虎（港股备份 + WebSocket 推送）。交叉验证可富途 + 老虎对比盘口。富途资金流调用：`get_capital_distribution(code)` / `get_capital_flow(code)` / `get_top_ten_buy_sell_brokers(code)`（详见上方「接口要点速查」表）。
