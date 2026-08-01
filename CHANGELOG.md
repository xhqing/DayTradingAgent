# Changelog

本文件记录 DayTradingAgent 每个版本的主要变更，格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

## [0.1.0] - 2026-07-30

首个正式记录的版本。涵盖项目从初始搭建到 2026-07-30 的全部迭代。

### 新增

- **项目脚手架**：MIT 许可证、双语 README（中/英）、LOGO、shields.io 徽章。
- **Trade skill（`.claude/skills/trade/`）**：日内交易核心执行规范——盯盘、信号、复盘全流程。
- **自动交易模式**（2026-07-30，2026-07-31 扩展到港股）：港股默认用老虎开放平台模拟账户、美股默认用长桥模拟账户自动下单，**3 类动作**（开仓/平仓/移损，禁止加仓减仓）直接调用券商 API 执行。如用户不使用默认账户会特别说明。触发原因：信号模式积累足够样本后，升级到自动执行以减少信号到下单的延迟、消除人工执行误差。核心新增：
  - `scripts/trade_utils.py` — 长桥 API 封装（配置加载、报价查询、限价单、止损条件单、持仓查询、订单查询）、价格范围计算、仓位计算
  - `scripts/open_position.py` — 开仓动作（6 要素校验 + 价格范围检查 + 限价单 + 止损条件单）
  - `scripts/close_position.py` — 平仓动作（限价→市价递进重试；成功后自动撤销所有未触发条件单）
  - `scripts/move_stop.py` — 移动止损（不删旧止损单，新增止损条件单）
  - 价格范围公式：做多 `[参考价 − R₀ × 0.8, 参考价 + 参考价 × 3/8]`、做空 `[参考价 − 参考价 × 3/8, 参考价 + R₀ × 0.8]`（R₀ = |参考价 − 止损价|；80% 来自风险距离，3/8 来自参考价本身）
- **`monitor_segment.py` 每次采样获取最新止损单**（2026-07-30）：新增 `stop_price` 列到采样 log，每次采样查询长桥当日订单提取最新止损条件单的止损价（频率随采样间隔走，不固定 30 秒）。原因：用户可能在券商 App 里手动添加止损单，止损价不能凭记忆，必须实时获取。
- **`actions/` 目录 + `log_action.sh`**（2026-07-30）：美股模拟盘交易动作记录目录，`actions/YYYY-MM-DD-ET-actions.md`。与 `signals/`（港股信号模式）分工——美股记录已执行的交易动作（含 order_id、成交价），港股记录待执行的信号。新增 `log_action.sh` 脚本写入动作文件（内容经 stdin 传入，自动带时间戳）。
- **富途牛牛 + Longbridge 双数据源**：富途为主、Longbridge appkey 模式为备用行情源。
- **信号文件与响铃**：`signals/` 目录记录每日港美信号（`YYYY-MM-DD-HKT-signals.md` / `YYYY-MM-DD-ET-signals.md`）；`alert.sh` 写文件 + 响铃提醒；`log_signal` / `log_ring` 脚本标准化日志。
- **复盘体系（`reviews/`）**：`review.py` 回测统计脚本；每日复盘报告含贝叶斯 P(EV>0) 时序图、累计 R 均值时序图、胜率时序图。
- **监控脚本**：`monitor_segment` / `monitor_summary` 多标的分段监控；`resume.py` 恢复监控；keep-awake skill 防止系统休眠。
- **交易规则迭代**：方向评估 + 置信分级 + 量价入场过滤；半仓轮动（震荡市）；反向 ETF 优先做空规则；按批次信号 + 分数风险定仓。

### 变更

- **港股交易默认用老虎开放平台模拟账户，美股用长桥模拟账户**（2026-07-31 用户规定）：港股账户编号 `21728459786026713`（`is_paper=True`，初始资金 $1,000,000 USD）；美股用长桥 `~/.longbridge/openapi/env-paper`。实盘账户后续添加，用户特别说明时才用。
- **早盘开盘首小时只观察不交易**（2026-07-31）：港股 09:30-10:30 / 美股 09:30-10:30 不发任何交易动作，只观察行情性质（震荡/趋势、多头/空头）。10:30 后才允许开仓。若用户 10:30 后才启动盯盘，先分析前一小时数据再进入交易流程。
- **Trade skill 按 skill-creator 标准结构性重写**（2026-07-31）：主 SKILL.md 从 543 行精简到 301 行（操作主线 + 29 处 references 指针），详细规则按主题拆到 4 个新建 references 文件（`risk-management.md` 90 行、`trading-strategy.md` 276 行、`monitoring.md` 184 行、`account-tools.md` 75 行）；`review-and-evaluation.md` 未动。触发原因：违反 skill-creator 的 Progressive Disclosure 原则（主文件应 <500 行、大块内容拆 references），且全文混杂核心规则、风控细节、几十条交易教训、盯盘工程参数。重写还顺带：去重（3 类动作定义从 3-4 处合并成 1 处）、description 改 pushy（强调触发场景必须激活）、硬约束改「解释 why」（风控红线保持硬约束）、清理残留信号模式措辞（0 残留）。所有交易规则、阈值、公式、反面教训逐字保留，只动结构和措辞。
- AutoMemory 退役：内容提炼进 trade SKILL.md，`autoMemoryDirectory` 配置移除。
- 美股监控延长至全日内（取消 12:00 ET 截断）。
- 信号流程重构：先写文件 + 响铃，再在对话中输出。
- Longbridge 切换到主账户。
- **Trade SKILL.md 信号模式→自动交易模式**（2026-07-30）：SKILL.md 描述、模式总则、脚本库、自查清单、执行反馈、账户工具链、当前阶段等章节全面更新——从「AI 只发信号、用户手动执行」改为「AI 调用脚本自动下单」。涉及文件：`.claude/skills/trade/SKILL.md`、`.claude/skills/trade/scripts/monitor_segment.py`（新增 stop_price 列 + 止损单查询）。
- README 语言切换链接文字统一为「简体中文」；版权人统一为 All Contributors。
- GitHub About 配置为纯英文 description + topics。
- **全局 CLAUDE.md 新增「CHANGELOG 记录纪律」规则**（2026-07-30 用户要求）：要求每次增删改查操作必须在 CHANGELOG.md 中记录原因和解决的问题；涉及已有记录的文件时必须溯源历史条目、评估回归风险，可能导致已解决的问题回归时必须提醒用户。原因：确保变更可追溯、防止后续改动意外破坏已有修复。
- **equity 必须从账户 API 取真实总资产，禁用占位/累加值（2026-07-31 用户立）**：原 `preflight.py`/`resume.py` 读 `signals/equity-log.csv`（旧信号模式手动累加值）+ `config.initial_equity=100000 HKD`（开发期占位）作 equity，显示 [equity-log值已移除] HKD 错值；真实长桥模拟账户 `net_assets=374,590.95 HKD`（差近 4 倍）。改动：① `references/risk-management.md`「权益更新」段重写——默认账户（美股长桥 `account_balance().net_assets` / 港股老虎 `get_assets().net_liquidation`）取真实总资产、用户指定账户从指定账户取、config 占位与 equity-log 累加值均不得当真值；② `scripts/preflight.py`、`scripts/resume.py` 改为优先从长桥账户 API 取 equity（失败才 fallback equity-log 并标记非真实）。原因：自动交易模式下 B / max_loss 标尺错会直接导致仓位失控——用户原话「下次这个总资产不能取错了，我没说用什么账户就是默认账户里面取，我有指定账户就是指定账户里面取」。
- **修复长桥 SDK 连接超时 + TradeContext.close() bug（2026-07-31）**：盯盘启动查账户余额时发现长桥 Python SDK 连接超时。根因有二：① SDK 默认连国际区 `openapi.longportapp.com`（国内被墙、直连 port 443 超时），且 SDK Rust 内核不读系统 `HTTP_PROXY`/`ALL_PROXY`（xray 代理形同虚设）；CLI 按 `~/.longbridge/openapi/region-cache=cn` 连中国区 `openapi.longportapp.cn`（直连可达）故能连。修复：`scripts/trade_utils.py` 的 `load_env_file()` 自动读 region-cache 设 `LONGPORT_REGION=cn`，所有交易脚本经 `load_config()` 自动连中国区（实测不设 7.7s 超时、设后 0.8s 连通）。② `TradeContext` 无 `close()` 方法（与 `QuoteContext` 不对称），原 `trade_utils.py` 8 处 `finally: tc.close()` 会抛 AttributeError 覆盖函数返回值、致所有下单/查询函数失败；改为 `finally: pass`（SDK 自动清理）。两个坑记录在 `references/account-tools.md`「长桥模拟盘交易 API」段。
- **修复长桥止损单 trigger_direction bug + 限价 tick + SDK 升级回滚（2026-07-31）**：做空 MU 时发现 `submit_stop_order` 用 SDK `MO+trigger_price` 被长桥当 MIT 触价单（默认 down 方向=价格跌触发），做空止损 Buy 在当前价<触发价时瞬间误触发、被动平仓亏 $119。根因：长桥 SDK `submit_order` 不支持 `trigger_direction`（签名确认），MIT 默认顺方向不能做止损。修复：`trade_utils.submit_stop_order` 改用 REST API（`POST /v1/trade/order`）提交 MIT + `trigger_direction=up`（做空止损）/`down`（做多止损），签名算法取自长桥 Rust SDK 源（HMAC-SHA256+SHA1）；`SKILL.md` 硬护栏第2条同步更新（MIT+trigger_direction，禁限价止损）。连带修复：`smart_order` 限价取整到美股 tick 0.01（原裸 ask 825.975 报 602035 Wrong bid size）；SDK 一度升级到无 `Config.from_env` 的版本破坏 `load_config`，已回滚 3.0.23。两个坑记录在 `references/account-tools.md`。当天 MU 两笔：bug 被动平仓 -$119、破位失败主动锁亏 -$227.52。
- **开仓改为附加止损单（主单+附加）+ smart_order 修 bug + f_max 提到 10%（2026-07-31）**：① 用户新规——开仓订单附加止损市价单（REST `attached_params`：`attached_order_type=STOP_LOSS`+`stop_loss_price`+`activate_order_type=MIT`），主单成交才激活附加止损（取代原「先挂止损单再开仓」+单独 submit_stop_order 的 trigger_direction REST 封装）；优点：开仓失败止损不残留、STOP_LOSS 语义自动定向无需 trigger_direction。`SKILL.md` 硬护栏第2条同步改。② 修 `smart_order` 两个 bug：振幅判断 high/low 缺失时原 fallback last→amplitude 0→误用市价（第2笔做空 Sell MO 被拒）；市价单原不查成交、拿 last 冒充 fill_price——现 high/low 缺失强制限价、市价单查 today_orders 确认 Filled。③ `config.json` f_max 0.025→0.10（用户立）。④ 事故复盘：做空 MU 时 smart_order 市价 Sell 被拒但误报成交→close_position 反向买入开多 158 股 @ 827.82（误持仓、收盘 823 浮亏约 -$757），下次开盘处置。`open_position.py` 重写为附加订单 pending（下一步）。

- **纠正「反向止损单」错误概念 + 厘清主订单 / 附加订单（2026-08-01 用户纠正）**：原 SKILL.md 硬护栏第 2 条用了「反向止损单 / 不反向的止损单 / 独立反向单」等说法——这些都不是有效概念：止损单默认就和主订单反向（券商设定），根本不存在「反向止损单」这个东西。用户原意「禁止反向订单止损」= 禁止用一笔反向的**主订单**去给另一笔已成交的主订单做止损（主订单是开仓单、会开新仓，不是了结仓位；止损只能用止损单 / 附加止损单）。改动：① `SKILL.md` 规则 2 重写——用「主订单 vs 附加订单」两类区分 + 附加订单状态流转（未提交 → 监控 → 触发）+ 正确表述「禁止反向主订单止损」，删除全部错误措辞，并标注代码现状（见下）；② `references/account-tools.md`「附加订单三类」段升级为「主订单 vs 附加订单 + 附加订单三类」——补全两类订单定义、附加订单状态流转、老虎「止损单」等同附加订单的说明、禁止反向主订单止损的正确含义。原因：用户指出「反向止损单」概念根本不存在，止损单本就和主单反向，必须用主订单 / 附加订单的正确框架描述，否则后续理解下单机制会持续跑偏。⚠️ 连带发现（待办）：`open_position.py` 当前仍是「先挂止损 → 再开仓」两步实现，未接线上 `trade_utils.submit_order_with_stop`（主单 + 附加一次提交）——即代码尚未实现规则 2 描述的附加订单设计；规则 2 已显式标注此代码现状，迁移属待办（完善附加订单代码实现）。

### 移除

- `.claude/memory/` 目录（随 AutoMemory 退役）。
- `settings.json` 中过时的 hooks 配置条目。
- **README 去除写死的 LLM 型号说明**（2026-07-31 用户要求）：删除 `README.md`/`README_cn.md` 的「Built with Claude Code」徽章与「Under the hood / 底层模型」（GLM-5.2）说明段；同步删除项目 `.claude/CLAUDE.md` 的 `agent-llm` 缓存标记。原因：Agent 使用的 LLM 会按情况切换，在 README 写死具体型号会随切换过时、且无谓绑定厂商。为避免下次 `/commit` 把 GLM-5.2 自动写回 README，已一并拆除全局 commit skill 的 `agent-llm` 检测（第 9f 步 + 徽章组合 + 缓存标记 + 汇报模板）与全局 `~/.claude/CLAUDE.md` 的「不强调 Agent 的大脑 LLM」规则段——该全局改动属 CapabilityManagerAgent 底座管辖，需同步回该项目 `.claude/`。
