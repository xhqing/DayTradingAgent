# 数据源配置参考（老虎 SDK + 港股代码格式）

> 2026-07-15 信号模式 + 长桥 CLI 撤销后精简：AI 只发信号、不管账户 / 资金 / 持仓；盯盘数据走富途 + 老虎。本文件仅保留**老虎 SDK 配置**与**港股代码格式**两条数据源相关参考（长桥账户切换 / 资金 / 持仓内容已清理，长桥 CLI token 已删除）。

## 港股代码格式（跨源调用）

港股标准代码本就是 **5 位数字**（如 `02800`、`00005`、`03688`），只是大部分第一位是 0。**老虎要求完整 5 位**（`'02800'`），**富途用 `市场.代码` 前缀**（`HK.02800`）。跨源调用若代码不匹配，规则是补 / 省前导 0，不是两套不同体系。

## 老虎 SDK（tigeropen v3.6.0）

- 配置文件：`~/.tigeropen/tiger_openapi_config.properties`（权限 600），含 `tiger_id` / `account` / `license` / `private_key` 等（明文凭证见 `accounts.json` 的 `tiger` 段，已 gitignore）。
- 加载：`TigerOpenClientConfig(props_path='~/.tigeropen/tiger_openapi_config.properties')`（**须传文件路径非目录**，否则 `QuoteClient()` 报 private key empty）。
- 服务地址：SDK 默认 `https://openapi.tigerfintech.com/gateway`，socket `openapi.tigerfintech.com:9883` ssl。实测 TBNZ 可用，无需手动改 itigerup.com。
- **行情权限**：港股 Level1 免费（`get_stock_briefs` 可用、与富途交叉验证一致）。**美股无行情权限**（TBNZ 报 `4000: permission denied`）。✅ **港股 Lv2（10 档）实测可用**（2026-07-10 盘中：`subscribe_depth_quote` 返回 ask/bid 各 10 档，含 price/volume/orderCount；expire=-1 永久免费。depth 未见经纪队列，broker 需富途）。代码骨架见 `tiger-websocket.md`。
