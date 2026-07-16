---
name: longbridge-cli-revoked-signal-mode
description: 2026-07-15 项目转纯信号模式——长桥 CLI 授权撤销、盯盘数据源改富途+老虎、清理全部账户/资金/持仓内容
metadata: 
  node_type: memory
  type: project
  originSessionId: 9443c508-987c-4431-b52d-bbb8c439e3fc
---

2026-07-15 项目重大状态变化（承接「富途+老虎数据足以覆盖长桥」的实测决策）：

**数据源**：盯盘行情从「长桥主力 + 富途/老虎辅助」改为**纯富途（主力）+ 老虎（港股备份）**。富途全面覆盖长桥且更优——资金流含 super 超大单 + 分钟序列 + 十大买卖经纪、分钟 K 5.5 年、WebSocket 毫秒级。美股富途单源（老虎 TBNZ 无美股权限）。

**长桥 CLI 撤销**：本地已删 5 个 OAuth 凭证（`cli-auth` + 主账户/日内融/模拟盘 3 备份 + `cli-registration`），CLI 完全失效。服务端 OAuth 授权理论仍在，但**长桥无公开撤销入口**（open.longbridge.com 开发者中心管的是 app_key 应用、App 账户安全页也未见「已授权应用」管理）；要复活需重新 `auth login` + 用户浏览器确认，用户不主动激活就不会复活——故本地删除已达实质断开效果。

**信号模式纯化**：清理 9 类文件（SKILL.md / accounts.* / config.* / preflight.py / classify / futu-tiger-hklevel2 文档 / README 双语 / archive 归档）里所有长桥账户 / 资金 / 买力 / 持仓 / 融资 / 日内融强平 / 实际盈亏 / 成本核算内容。保留：`max_loss_per_trade` 风控、信号格式、交易策略纪律、胜率/赔率/EV 复盘。classify 的 static 数据源改富途 `get_market_snapshot`（eps 字段名 `earning_per_share`）。

**用户决策**：不再用长桥日内融账户下单；复盘只留信号层面（胜率/赔率/EV），实际账户盈亏用户自算。

trade skill 现状态：纯信号模式 + 富途/老虎数据源。关联 [[cron-replace-delete-old]]（盯盘运维）。

---

**2026-07-16 更新（撤销本条的「撤销」决定）**：长桥 CLI 以**备份行情数据源**身份重新启用——用 **appkey 模式**（OAuth 设备流调 openapi 一律 401003、桥接坏，故改 appkey），凭证存 `~/.longbridge/openapi/env-sg`（新加坡账户）/ `env-paper`（模拟账户）。主力仍是富途 + 老虎，长桥是**第三备份**（富途 + 老虎都掉线时兜底，盘口只 1 档、仅降级用）。详见 [[longbridge-backup-datasource]]。
