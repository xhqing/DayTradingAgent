# Memory Index

- [cron-replace-delete-old](cron-replace-delete-old.md) — 建新盯盘 cron 前先 CronList + 删所有旧盯盘 cron，避免遗留重复触发（2026-07-15 教训）
- [longbridge-cli-revoked-signal-mode](longbridge-cli-revoked-signal-mode.md) — 2026-07-15 转信号模式曾撤销长桥 CLI；⚠️ 2026-07-16 已撤销该决定、长桥重新启用为备份源，见 longbridge-backup-datasource
- [longbridge-backup-datasource](longbridge-backup-datasource.md) — 长桥 sg/paper 作富途+老虎的备份行情源（appkey 模式，OAuth 桥接坏）；盘口只 1 档仅降级兜底（2026-07-16 立）
- [intraday-signal-window](intraday-signal-window.md) — 发信号/盯盘时段规则(2026-07-19大改)：五类信号只盘中发、美股盯到用户喊停或16:00收盘(撤销12:00固定停盯)、港股16:00收盘、撤销留单策略(停前一律主动平)、盯盘用户指令启动、复盘用户指令触发
- [longbridge-main-account](longbridge-main-account.md) — 2026-07-19 用户日内交易改用长桥主账户(原日内融作废)：主账户持仓可过夜，但用户坚持"持仓不过夜"作AI主动纪律，收盘前(港16:00HKT/美16:00ET)主动平仓
- [trade-early-session-entry](trade-early-session-entry.md) — 盯盘要盘前/开盘接入，大涨日主升浪在早盘，午市后接入错过顺势机会（2026-07-16 复盘）
- [stop-loss-trigger-no-signal](stop-loss-trigger-no-signal.md) — 止损/止盈被动触发不发五类信号(不响铃)，只在 signals 记录标记；🔴平仓信号仅用于AI主动平仓（2026-07-16 立）
- [open-position-thresholds](open-position-thresholds.md) — 开仓门槛(2026-07-16修订)：仅🟢高置信才发开仓信号(中/低不发)，赔率门槛降至≥1.5（原≥2）
- [signal-is-real-trading](signal-is-real-trading.md) — 信号=真实交易(用户真实下单)非演练；质量优先，不为积累样本发不完美信号(2026-07-16立)
- [verify-signal-price-after-send](verify-signal-price-after-send.md) — 发完开仓/加仓信号立即重新采样验证参考价是否仍可成交(发信号过程中价格在变、限价单可能已不撮合)，偏离大就告知用户并按实际可成交价重算(2026-07-17立)
- [range-market-avoid-infinite-loop](range-market-avoid-infinite-loop.md) — 区间震荡市禁密采样(会死循环)，改触发条件+cron间歇检查；判市况(趋势vs震荡)是密采样前提(2026-07-17教训)
- [position-size-leave-buffer](position-size-leave-buffer.md) — 开仓算仓位留buffer(20-30%)或按范围下沿算，覆盖成交价偏离参考价，避免事后减仓补救(2026-07-17立)
- [direction-bias-dynamic-revise](direction-bias-dynamic-revise.md) — 开盘前用位置(≥4个月日线通道)预判盘中方向(下沿超跌做多/上沿超买做空/中段顺势)+盘中动态修正兜底，不执念开盘方向(7-17教训,7-18深化)
- [head-shoulder-reversal-pattern](head-shoulder-reversal-pattern.md) — 转势形态:头肩(有头)/双底双顶(无头)，打破"高更低+低更低"(跌)或"高更高+低更高"(涨)=转势信号，close持平+破颈线确认，比连破阻力更早(7-17 MU 09:48确认 vs 11+点)
- [signal-abbreviation-mobile-stop](signal-abbreviation-mobile-stop.md) — 用户授权"移动止损"可简写为"移损"(紧凑列举五类信号时用，如"开仓/加仓/减仓/平仓/移损")，完整写法仍可用(2026-07-19立)
