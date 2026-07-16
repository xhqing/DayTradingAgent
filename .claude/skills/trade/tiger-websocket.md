# 老虎 SDK WebSocket 实时推送

老虎证券 PushClient 是港股实时行情的备用源（交叉验证；免费 Level2 主力是富途 OpenD，见 `futu-opend-level2.md`），突破长桥 CLI 的 sleep 轮询瓶颈。用途：早盘急涨急跌盯盘（呼应密采样需求）。

## 能力（2026-07-03 实测验证链路通）

- `connect` 成功、`subscribe_quote` 发送成功、`subscribed_symbols()` 可查。
- 休市时收到推送 0 条（价格不动无新数据，符合预期）。**盘中实时数据流已实证（2026-07-10 盘中 depth 推送，见「权限边界」）。**
- PushClient 方法极丰富：`subscribe_quote`/`subscribe_tick`/`subscribe_depth_quote`（深度盘口）/`subscribe_transaction`（成交）/`subscribe_kline`/`subscribe_asset`/`subscribe_order`/`subscribe_position` 等。
- 回调：`quote_changed`/`tick_changed`/`full_tick_changed`/`quote_depth_changed`/`transaction_changed` 等。

## 代码骨架

```python
import time, signal, sys
from tigeropen.tiger_open_config import TigerOpenClientConfig
from tigeropen.push.push_client import PushClient

# macOS 无 timeout 命令，用 SIGALRM 做硬超时防 WebSocket 挂起
def handler(signum, frame):
    print('>>> 硬超时触发，强制退出'); sys.exit(0)
signal.signal(signal.SIGALRM, handler)
signal.alarm(30)

received = []
connected = [False]
cfg = TigerOpenClientConfig(props_path='~/.tigeropen/')
protocol, host, port = cfg.socket_host_port  # ('ssl','openapi.tigerfintech.com',9883)

def on_quote(frame):
    received.append({'symbol': frame.symbol, 'latest': getattr(frame, 'latestPrice', None)})

def on_connect(frame):
    connected[0] = True; print('>>> WebSocket 已连接')
def on_disconnect():
    print('>>> WebSocket 断开')
def on_error(frame):
    print(f'>>> 错误: {frame}')
def on_kickout(frame):
    print(f'>>> 被踢出: {frame}')

pc = PushClient(host, port, use_ssl=(protocol == 'ssl'))
pc.quote_changed = on_quote
pc.connect_callback = on_connect
pc.disconnect_callback = on_disconnect
pc.error_callback = on_error
pc.kickout_callback = on_kickout

pc.connect(cfg.tiger_id, cfg.private_key)
# 等连接建立
for _ in range(20):
    time.sleep(0.5)
    if connected[0]: break

if connected[0]:
    pc.subscribe_quote(['02800'])  # 港股代码本就是5位数字,老虎要求完整5位(长桥容忍省略前导0)
    # 等推送...
    time.sleep(15)
    print(f'收到 {len(received)} 条')
    pc.unsubscribe_quote(['02800'])

pc.disconnect()
```

## 权限边界

- 老虎 TBNZ 账户**美股无行情权限**，WebSocket 只能订阅港股。
- 港股 **Lv2 实测可用（✅ 2026-07-10 盘中实证）**：`get_quote_permission()` 返回 `hkStockQuoteLv2`（expire_at=-1，永久有效；A 股送 Lv1）。午市盘中实订 `subscribe_depth_quote(['02800','00700'])`，**25 秒收 24 条推送、0 错误，每条 `QuoteDepthData` 的 `ask`+`bid` 各 10 档**（盈富 ask 24.84→25.02、bid 24.82→24.64；腾讯 ask 462→463.8、bid 461.8→460），每档含 `price`/`volume`/`orderCount`，bid1 volume 实时跳动 → 真·实时流非缓存。**推翻「海外需购 hkStockQuoteLv2Global」推断**：TBNZ 的 `hkStockQuoteLv2` 实际已开通完整 10 档 depth（2026-07-10 盘中实订 `subscribe_depth_quote` 实证，见本节上文的 25 秒 24 条推送实测）。⚠️ depth 推送**未见经纪队列 broker id**（富途港股 Level2 有 broker；老虎 broker 队列若需另有接口，未测）。
- 老虎仅作数据源，**未授权交易**（交易仍走长桥 CLI）。

## 老虎 SDK 能力（长桥 CLI 2026-07-15 已撤销，盯盘走富途 + 老虎）

| 维度 | 老虎 SDK |
|---|---|
| 实时性 | WebSocket 毫秒级推送 |
| 港股 | ✅ Lv2 实测可用（2026-07-10 盘中：10 档 ask/bid，无经纪队列） |
| 美股 | ❌ 无权限（走富途） |
| 定位 | 港股 WebSocket 推送 + 富途的备份 |

## 同步行情查询速查（非 WebSocket，轮询式）

`QuoteClient` 同步接口，与上方 WebSocket 推送互补（WebSocket 拿实时流，同步接口拿快照/状态）：

- **导入**：`from tigeropen.quote.quote_client import QuoteClient, Market`（⚠️ `Market` 在此模块；`tigeropen.common.contract` 路径不存在）。
- `get_market_status(Market.HK/US/CN)`：市场开闭盘 + 下次开盘时间。
- `get_stock_briefs(['02800','00700'])`：港股 OHLCV+买一卖一+量。
- `get_quote_permission()`：查行情权限，返回 `[{'name':权限名,'expire_at':时间戳}]`（-1=永久）。
- ⚠️ **DataFrame 坑（2026-07-07 踩）**：`get_stock_briefs` 返回 **pandas DataFrame 不是对象列表**，用 `df['latest_price']` 取值，别用 `getattr(b,'latest_price')`（全 None）；`for b in df` 遍历的是列名。完整列：symbol/open/high/low/close/pre_close/latest_price/latest_time/ask_price/ask_size/bid_price/bid_size/volume/status/adj_pre_close/change/change_rate/amplitude。
- macOS 无 `timeout` 命令，同步调用也用 `signal.SIGALRM` 硬超时防挂起。
