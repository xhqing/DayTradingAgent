# 数据源配置参考（老虎 SDK + 港股代码格式）

> 2026-07-15 信号模式精简：AI 只发信号、不管账户 / 资金 / 持仓；盯盘数据走富途 + 老虎。本文件仅保留**老虎 SDK 配置**与**港股代码格式**两条数据源相关参考。

## 港股代码格式（跨源调用）

港股标准代码本就是 **5 位数字**（如 `02800`、`00005`、`03688`），只是大部分第一位是 0。**老虎要求完整 5 位**（`'02800'`），**富途用 `市场.代码` 前缀**（`HK.02800`）。跨源调用若代码不匹配，规则是补 / 省前导 0，不是两套不同体系。

## 老虎 SDK（tigeropen v3.6.0）

- 配置文件：`~/.tigeropen/tiger_openapi_config.properties`（权限 600），含 `tiger_id` / `account` / `license` / `private_key` 等（明文凭证见 `accounts.json` 的 `tiger` 段，已 gitignore）。
- 加载：`TigerOpenClientConfig(props_path='~/.tigeropen/tiger_openapi_config.properties')`（**须传文件路径非目录**，否则 `QuoteClient()` 报 private key empty）。
- 服务地址：SDK 默认 `https://openapi.tigerfintech.com/gateway`，socket `openapi.tigerfintech.com:9883` ssl。实测 TBNZ 可用，无需手动改 itigerup.com。
- **实盘下单须境外网络环境 + SDK 代理配置（2026-08-14 立，重要）**：2026-06-12 监管新规起，老虎按**指令发出的网络环境**判定——境内 IP 下实盘开仓/加仓被 code=1200 拒（"Under regulatory requirements for existing Mainland China investors, while located in Mainland China, you may only close, reduce, or transfer out positions..."），仅允许平仓/减仓/转出；境外出口则正常受理（2026-08-14 实盘实测：同一账户同一凭证，境内拒单、境外受理，拒单原因从监管拦截变为普通资金不足）。老虎客服确认判定依据是「指令的网络环境」（非账户属性——此前误判为账户属性，已修正）。**⚠️ 关键修正（2026-08-14 当日，App 端实测坐实）：判定「位置」按流量出口，但必须「全部流量」走境外——仅代理 API 网关一个域名不够**。手机 App / Mac 桌面端在「只给 `openapi.tigerfintech.com` 加代理规则」时仍显示受限，**误判为「客户端账户层受限」**；后手机 v2rayNG 切**全局模式**（所有流量走境外节点）→ App 立即放行，坐实真相：**老虎 App 用多个域名，分流规则只覆盖一个、其它域名走直连中国出口 → 被判境内**。故客户端（App/桌面端）使用时须**全局代理**（或把老虎 App 实际用到的全部域名都加进代理规则）；API 端（SDK）同理须保证所有老虎域名走境外（当前 xpilot 白名单只加了 `domain:openapi.tigerfintech.com`，SDK 的请求目标就是它，够用；若日后 SDK 目标域名增多需同步补）。**配置三件套**：① 本机代理路由白名单加 `domain:openapi.tigerfintech.com`（⚠️ 必须带 `domain:` 前缀——裸域名会被 xpilot 生成 Xray 配置时**静默丢弃**、规则不落地，2026-08-14 踩过）→ 改后 `xpilot restart`；② SDK 代码层挂代理（`trade_utils_tiger.py` 的 `apply_proxy`，config.json 的 `proxy` 节控制，默认 live_only 仅实盘走代理——**SDK 用 urllib3.PoolManager、不读 HTTP(S)_PROXY 环境变量**，环境变量对 SDK 无效，curl/requests 认、urllib3 不认）；③ 盘前检查 `xpilot status` + 实盘下单前实跑 `get_assets` 确认不报 1200。**socket 长连接（9883 行情推送）不走 HTTP 代理**，如需代理须另行处理（当前未用到）。
- **订单时间字段三件套（2026-08-13 立，复盘 / 时点核对必看）**：老虎订单对象（`get_orders()` 返回）有**三个**时间字段，含义不同，**复盘核对成交时点必须取 `trade_time`、严禁取 `update_time`**：
  - `order_time`：下单（提交）时刻。
  - `trade_time`：**成交时刻**——复盘算持仓时长、核对「是否盘中成交」、填 CSV 的 `entry_time`/`exit_time` 都该取这个（毫秒 Unix 时间戳，换算用 `datetime.fromtimestamp(ts/1000, tz=UTC+8)` 港股 / 美股按交易所时区）。
  - `update_time`：订单**最后一次被系统更新**的时刻（含日内结算、批处理、状态回写），**不是成交时间**——实测曾显示凌晨 0:07（港股根本不交易），用它当成交时间必错。
  - **踩坑经过（2026-08-13 实盘账户查 4 条历史订单）**：手动换算时误用 `update_time`，把 2025-05-14 港股盘中 09:44 成交的阿里单算成了 05-16 凌晨 00:07；改用 `trade_time` 后时间全部正确落在港股盘中（09:44 买、11:39 卖）。`review.py` 的 `entry_time`/`exit_time` 是从复盘 CSV **手填**读的（不从订单对象自动取），故脚本本身无此 bug，但人工填时务必从 `trade_time` 取、勿取 `update_time`。
  - **资产查询的 `summary.timestamp` 是另一回事**（`get_assets()` summary 段的时间戳，用于判「账户是否开通资产权限」：`net_liquidation=0 且 timestamp=None` → 未开通），与订单成交时间无关，勿混淆。
- **行情权限**：港股 Level1 免费（`get_stock_briefs` 可用、与富途交叉验证一致）。**美股无行情权限**（TBNZ 报 `4000: permission denied`）。✅ **港股 Lv2（10 档）实测可用**（2026-07-10 盘中：`subscribe_depth_quote` 返回 ask/bid 各 10 档，含 price/volume/orderCount；expire=-1 永久免费。depth 未见经纪队列，broker 需富途）。代码骨架见 `tiger-websocket.md`。
