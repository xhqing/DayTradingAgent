---
name: longbridge-backup-datasource
description: 长桥 sg/paper 账户作富途+老虎的备份行情数据源（appkey 模式），主力掉线时降级切换；盘口只 1 档仅兜底
metadata: 
  node_type: memory
  type: project
  originSessionId: e9ac4821-66c1-4a18-b3fa-466a3940fb8c
---

2026-07-16 用户立：把长桥两个账户作为**富途 + 老虎的备份行情数据源**——当富途、老虎都取不到数据时，降级切换到长桥 OpenAPI 取行情。

**为什么**：富途（免费十档 + super 超大单资金流）、老虎（十档 + WebSocket 毫秒推送）是主力，数据深度远胜长桥；但万一两家都掉线 / 取不到，长桥兜底，避免数据全断。

**配置（appkey 模式，不是 OAuth）**：长桥 CLI 的 OAuth 设备流登录后，调 openapi 一律 `401003 token expired`（OAuth→openapi 桥接坏，0.24.0 已是最新、无新版可修），**必须用 appkey 模式**。凭证按账户分文件存（source 切换）：
- `~/.longbridge/openapi/env-sg` — 新加坡账户（`account_channel=lb_sg`，真实账户，当前空）
- `~/.longbridge/openapi/env-paper` — 模拟账户（`account_channel=lb_papertrading`，有模拟资金 + 7709 持仓）

**用法**：
```bash
source ~/.longbridge/openapi/env-sg   # 或 env-paper，两账户行情数据等价
longbridge quote 0700.HK              # 实时报价
longbridge depth 0700.HK              # 盘口
longbridge capital 0700.HK            # 资金流（大/中/小单）
longbridge kline 0700.HK 8            # K线
```
两账户行情数据**逐字节等价**（2026-07-16 实测 quote/depth/capital 完全相同，paper 看到的就是真实行情），任一可用。

**限制（重要，仅降级用）**：长桥 OpenAPI 盘口浅——
- 港股只 **1 档**盘口（`HK_L1_OpenAPI`；十档 `HK_L2_*_OpenAPI` 未开通，十档只在移动端 App）
- 美股只 **1 档**（纳斯达克 QBBO；`US_Totalview` 60 档未开通）
- 资金流只**大/中/小三档**（无 super 超大单 / 分钟序列 / 十大经纪，远不如富途细）

→ 故**仅降级备份，不作主力**。主力仍是富途 + 老虎。

**账户号查法**：`source env-xxx && longbridge auth status --format json` 看 `account.account_no`（注意：OAuth 模式下 `account_no` 是 null，因为 401003 填不进；**appkey 模式才填上**）。

撤销了 2026-07-15 的「长桥 CLI 撤销」决定（见 [[longbridge-cli-revoked-signal-mode]]），长桥以备份身份重新启用。
