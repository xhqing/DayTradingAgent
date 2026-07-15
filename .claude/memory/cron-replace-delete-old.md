---
name: cron-replace-delete-old
description: 建新盯盘 cron 前必须 CronList+删所有旧盯盘 cron，避免遗留重复触发
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 85cdfcd9-59d5-4411-b6af-8d1ab319fd6f
---

2026-07-15 盯盘实战：每次规则变化（持仓变化 / 职责边界 / signals 路径 HKT-ET）都重建盯盘 cron，但建新版时只删「上一个」、漏删最早的 9a98401d，导致 9a98401d 与后续 ba4491b9 / c5f128b1 / 10d350f0 / 59c040f2 / 70ce0017 / 6c01e356 / 24cc91e8 / 1d39d377 等**并存、同节奏（5 分钟）重复触发**同一盯盘。用户「暂停盯盘」时删了最新的 1d39d377，但 9a98401d 遗留、仍按旧 prompt（模拟盘盯 07709、回踩 74-75/突破 79.74）触发，直到 CronList 才发现。

**Why**：session-only recurring cron 不会自动清除，建新不删旧就遗留；多个 cron 同节奏重复触发 = 浪费 token + 信号混乱（旧 prompt 与新规则冲突）。

**How to apply**：① 每次 CronCreate 新盯盘 cron 前，先 `CronList` 看所有活跃任务；② `CronDelete` **所有**旧盯盘 cron（不只最新那个），再建新；③ 暂停 / 结束盯盘时 `CronList` 确认返回 "No scheduled jobs" 才算彻底停。关联 trade skill「盯盘节奏」的 cron 循环机制。
