# Memory Index

- [cron-replace-delete-old](cron-replace-delete-old.md) — 建新盯盘 cron 前先 CronList + 删所有旧盯盘 cron，避免遗留重复触发（2026-07-15 教训）
- [longbridge-cli-revoked-signal-mode](longbridge-cli-revoked-signal-mode.md) — 2026-07-15 转信号模式曾撤销长桥 CLI；⚠️ 2026-07-16 已撤销该决定、长桥重新启用为备份源，见 longbridge-backup-datasource
- [longbridge-backup-datasource](longbridge-backup-datasource.md) — 长桥 sg/paper 作富途+老虎的备份行情源（appkey 模式，OAuth 桥接坏）；盘口只 1 档仅降级兜底（2026-07-16 立）
- [intraday-signal-window](intraday-signal-window.md) — 2026-07-16 发信号/盯盘时段规则：四类信号只盘中发、美股限美东12点前、撤销24h盯盘、盯盘用户指令启动、三强制停止边界、复盘用户指令触发
- [longbridge-day-margin-account](longbridge-day-margin-account.md) — 2026-07-16 用户日内交易统一用长桥日内融账户：港股15:45 HKT/美股15:45 ET 收盘前15分钟强制平仓、持仓不过夜，影响止盈/留单决策
- [trade-early-session-entry](trade-early-session-entry.md) — 盯盘要盘前/开盘接入，大涨日主升浪在早盘，午市后接入错过顺势机会（2026-07-16 复盘）
- [stop-loss-trigger-no-signal](stop-loss-trigger-no-signal.md) — 止损/止盈被动触发不发四类信号(不响铃)，只在 signals 记录标记；🔴平仓信号仅用于AI主动平仓（2026-07-16 立）
- [open-position-thresholds](open-position-thresholds.md) — 开仓门槛(2026-07-16修订)：仅🟢高置信才发开仓信号(中/低不发)，赔率门槛降至≥1.5（原≥2）
- [signal-is-real-trading](signal-is-real-trading.md) — 信号=真实交易(用户真实下单)非演练；质量优先，不为积累样本发不完美信号(2026-07-16立)
- [verify-signal-price-after-send](verify-signal-price-after-send.md) — 发完开仓/加仓信号立即重新采样验证参考价是否仍可成交(发信号过程中价格在变、限价单可能已不撮合)，偏离大就告知用户并按实际可成交价重算(2026-07-17立)
