# DayTradingAgent 待办清单（活跃）

> 本文件**只放未完成**的待办（`[ ]` 开头）——「还有哪些没做」一眼全览（项目根文件制，2026-08-05 撤销 todo-skill 定案；方法论见全局 CLAUDE.md「待办（TODO）管理」节，本文件与 `TODO-archive.md` 同处项目根）。
> 已完成 / 已更新的条目移入 `TODO-archive.md` 归档保留（勿删）。
> **优先级分类（2026-08-16 用户立）**：条目按 🔴 高危 / 🟡 中危 / 🟢 轻危三级分节排列（高危在前），每条待办写入时必须判断并标注类别；判断拿不准往高靠。分级标准：🔴 不修会直接亏钱 / 下错单 / 安全事故 / 数据统计结论错误 / 核心功能不可用；🟡 边界情况出错 / 防护缺口 / 口径不一致可能演化为实际损失；🟢 文档措辞 / 格式漂移 / 卫生问题 / 不影响正确性的优化。
> 每条待办带记录时间戳（精确到分钟）；前后矛盾的待办以最新时间戳为准。
> 本批条目来源：2026-08-15 全项目逻辑审计（规范文档逐字审读 + 四路子代理脚本审计 + 统计公式实算复核，高危发现均已亲自复现验证）。

## 🔴 高危（不修会亏钱 / 下错单 / 统计结论错 / 核心功能不可用，须优先处理）

（2026-08-16 15:42：本节 11 条高危待办已全部处理完毕、归档至 `TODO-archive.md`「🔴 高危批量处理」节；当前无新增高危。）

## 🟡 中危（边界情况出错 / 防护缺口 / 口径不一致，排在高危之后计划处理）

### 下单脚本

- [ ] 🟡 **`check_order_filled_tiger` 把 PartiallyFilled 当全额成交**（记录：2026-08-16 14:27）：`trade_utils_tiger.py:587` 的 `"Filled" in status` 对 "PartiallyFilled" 也为 True（港美共用）——大额市价单部分成交时开仓侧把下单量当全成量上报、平仓侧误信已平干净，持仓认知与账户脱节。改法：部分成交单独处理（读实际成交量 filled_qty 字段，或至少标注 part-filled 让 AI 复查持仓）。
- [ ] 🟡 **显式传 quantity 的开仓绕过全部风控上限 + risk_fraction / f_max 硬编码**（记录：2026-08-16 14:27）：① `calc_position_size` 的 f_max / max_leverage 约束只在 quantity=0 自动算仓位时生效，显式传量时唯一护栏是券商保证金拒单，降档成交后 `actual_max_loss` 只 warning 不拦截（58,400 股 MINIMAX 大单场景正是此路径）。② `open_position_tiger.py:174-175` 与 `open_position_tiger_us.py:141-142` 把 `risk_fraction=0.02 / f_max=0.10` 硬编码，config.json「修改本文件即可调参」契约对这两个参数不生效（当前数值恰好一致、属定时炸弹；max_leverage 已正确从 config 读）。改法：① 两参数改读 config.json（与 max_leverage 同法）；② 显式传量路径也过 f_max / max_leverage / equity 校验（超限拒绝或降档，不只 warning）。
- [ ] 🟡 **下单提交异常盲重试 3 次：模糊失败可重复下单、双倍持仓**（记录：2026-08-16 14:27）：`trade_utils_tiger_us.py:212-242` 与 `trade_utils_tiger.py:489-533` 对任何提交异常盲目重试 3 次（港美同），含「请求已达券商但响应超时」的模糊失败——MKT 主单即时成交，第二笔不会被 cross-trading 挡住；外层降档循环又叠加一层重试。live 模式下这是真实的重复开仓路径。改法：区分「确定未到达」（可重试）与「超时模糊」（先查当日订单确认是否已成交，未确认不重试）；项目全无重复下单防抖，建议加「同标的当日已有活动开仓单则拒绝」检查。
- [ ] 🟡 **开仓附加止损触发价不做 tick 取整（三脚本不一致）**（记录：2026-08-16 14:27）：`open_position_tiger.py:216-218` 止损价原样传入（限价取整了、止损没取整）；`move_stop_tiger.py:103` 与 `close_position_tiger.py:160` 都取整。传入不合 tick 的 stop_loss → 主单+附加腿整体被拒，降档循环用同一个坏止损价把所有档全部烧完。改法：开仓脚本对 stop_loss 做 `round_to_tick_tiger` 取整。
- [ ] 🟡 **close_position 主路径只处理第一个活动止损单：兄弟止损单残留可反向开仓**（记录：2026-08-16 14:27）：`close_position_tiger.py:126-137` 与 `close_position_tiger_us.py:129-137` 查到多个活动 STP 时只 modify 第一个触发平仓，其余不撤（撤全部只在 fallback 做）；残留单日后零持仓触发时 Buy 侧将反向开多（与 2026-08-03 反向开空事故同类）。move_stop 对同一异常做了清理（≥2 撤多余）、close 没有——脚本间不一致。改法：close 主路径平仓成功后复查并撤掉全部残留止损单。
- [ ] 🟡 **move_stop fallback 异常归因错误：新 STP 已挂上却报 ok:false**（记录：2026-08-16 14:27）：`move_stop_tiger.py:146-157`（港美同）fallback「先下新 STP 再撤旧」——新单提交成功、撤旧失败时整体报「modify 失败且 fallback 也失败」，实际新止损已活着；AI 误信「无止损」再补挂 → 多重止损。改法：fallback 内部分步报告（new_submitted / old_cancelled 各自状态）。
- [ ] 🟡 **港股脚本缺 TRAIL / LOSS 腿识别：2026-08-05 中芯事故修复只落在美股版**（记录：2026-08-16 14:27）：美股 `_is_stop_order` 含 TRAIL/LOSS（`close_position_tiger_us.py:30-37`）、`cancel_all_stop_orders_us` 含 TRAIL；港股版 `close_position_tiger.py:33-36` / `move_stop_tiger.py:35-39` / `trade_utils_tiger.py:681` 都只认 STP/STOP——港股账户若存在 TRAIL 单（用户 App 手动挂）：close 看不见、撤不掉 → 平仓后残留。改法：港股两脚本与 cancel 函数对齐美股口径（补 TRAIL / LOSS 腿）。
- [ ] 🟡 **`filled` 变量未初始化：全部 submit 抛异常时 UnboundLocalError**（记录：2026-08-16 14:27）：`open_position_tiger.py:248` / `open_position_tiger_us.py:206` 降档循环内只有 `check_order_filled_tiger` 被调用才赋值 filled；全部档位在 submit 抛异常（如持续网络故障）时循环耗尽后 `if not filled:` 直接崩溃——traceback 代替干净 JSON，AI 拿不到失败详情。改法：循环前 `filled = False` 初始化。
- [ ] 🟡 **check_order_filled 轮询无异常捕获 + 平仓显式传参不校验持仓**（记录：2026-08-16 14:27）：① `trade_utils_tiger.py:553-596` 轮询 `get_orders` 网络异常带崩主流程（订单已实际在场，AI 看不到输出可能重开仓）。② `close_position_tiger.py:191-213` 显式传参路径不读持仓、不校验方向匹配——direction=short 而无空仓时直接 Buy MO **凭空开多**（一键分支有持仓复核、显式分支没有）。③ `get_open_position_tiger` 不过滤市场（美股持仓也会被收进，港股一键平仓可能拿到 US.xxx 报 ValueError 而非 JSON 错误）。改法：轮询加 try/except 返回错误 JSON；显式传参路径也先复核持仓存在与方向；持仓查询按市场过滤。

### 监控链路

- [ ] 🟡 **resume.py 美股断层检测跨午夜失明：与 monitor_segment 日期口径矛盾**（记录：2026-08-16 14:27）：monitor_segment 美股 log 按美东交易日命名（北京 −12h；已用真实文件实锤：08-11 会话跨午夜后仍写 `20260811` 文件、末行 01:56），`resume.py:95-96` 用北京日期 glob——北京 00:00-04:00（美股后半场）glob 不到任何美股 log，输出「今日无采样记录」**假安心**，恰在最需要断层检测的时段。另 `resume.py:112-126` 取 ring-log 物理末行不校验日期：周一开盘会把周五最后响铃当「上次活动」必报假断层。改法：resume 按市场对齐日期口径（美股 −12h）；ring-log 末行校验当日。
- [ ] 🟡 **monitor_summary 买卖比行 r_last=None 时 TypeError 崩溃（量比行守卫正确、同文件不一致）**（记录：2026-08-16 14:27）：`monitor_summary.py:96` 只判 `r_first is not None` 就进 f-string（内部 `r_last < r_first` 在 r_last=None 时抛 TypeError，已实测复现）；第 97 行量比行正确判了 v_first and v_last 双非 None。触发场景现实存在：同一 symbol+date+mode 文件上午 monitor_segment 写（ratio 有值）、下午 ws/futu_ws 接管写（ratio 空）→ 当日全貌摘要必崩。改法：第 96 行补 `and r_last is not None` 守卫。
- [ ] 🟡 **monitor_guard 美股时段硬编码夏令时 + 周末一刀切：guard 窗口漏洞**（记录：2026-08-16 14:27）：`monitor_guard.py:46-64` 的 `in_us_session` 用夏令时 21:30-04:00 硬编码 + `weekday()>=5` 排周末——① 北京周六 00:00-04:00（美东周五盘中、已实测此刻美东=周五 14:00）guard 完全失效 4 小时；② 周一凌晨（周日 ET 休市）误激活；③ 11 月切冬令时后整体错位 1 小时。preflight.py 已用 zoneinfo 自动 DST、guard 没跟上，口径分裂。改法：guard 改用 zoneinfo 美东判定（对齐 preflight）。另 `STALE_SECONDS=300` 与自述「段间循环 <90 秒」不匹配：进程死后 log 5 分钟内仍判「在跑」，最长 5 分钟盲窗——建议收紧或分层。
- [ ] 🟡 **断层哨兵无午休感知：每个交易日 13:00 重启必误报**（记录：2026-08-16 14:27）：`monitor_segment.py:194-225` 港股 12:00 停段、13:00 重启时 gap ≈ 61 分钟 ≥ 5 → 每天午后第一段必报「疑似断网/暂停/故障」，狼来了效应削弱真警告。改法：哨兵加港股午休感知（12:00-13:00 的 gap 不报或降级提示）。
- [ ] 🟡 **`stop_prices` 缓存只增不清：撤掉的止损单仍显示旧触发价**（记录：2026-08-16 14:27）：`monitor_segment.py:246-249` 的 `stop_prices.update(fresh)` 只增不清——止损单撤销/成交后 fresh 不再含该标的，字典保留旧价，后续每轮采样持续显示已不存在的止损（误导「持仓有止损保护」判断），与「每轮现查、不凭记忆」的设计意图相反。改法：update 前先清掉 fresh 中不存在的标的键。
- [ ] 🟡 **watcher 无告警冷却 + 采样进程存活掩蔽一切会话**（记录：2026-08-16 14:27）：`monitor_watcher.py:248-282` ① 无冷却机制——waiting_input / jsonl 停更 >90s / 陈旧 running 持续成立时 launchd 每 10 秒发一次 macOS 通知（每分钟 6 条通知风暴）。② `:309-312` 任一采样脚本进程存活 → 所有注册会话一律不报——signal/auto 双会话并行时一个崩溃、另一个在采样 → 崩溃会话被永久掩蔽。改法：告警加冷却（如同一会话 5 分钟内不重复报）；掩蔽问题至少在输出中标注「有会话可能被采样进程掩蔽」。
- [ ] 🟡 **bayes_evolution 的 direction 解析只认 long/做多：别名静默当空头、R 符号翻转**（记录：2026-08-16 14:27）：`bayes_evolution.py:100` 只认 `('long','做多')`、其余一律当空头；`review.py:66-70` 接受 long/short/buy/sell/做多/做空/多/空/买入/卖出 十种。CSV 出现「多」「buy」等别名时 bayes_evolution 该笔 R 符号翻转、全部演化图错（当前数据只有 long/short 未触发、属埋雷）。改法：两脚本 direction 解析统一（bayes_evolution 复用 review 的口径）。
- [ ] 🟡 **monitor_guard A4 连跑检测只数 monitor_segment：ws_segment 连跑不被拦**（记录：2026-08-16 14:27）：`monitor_guard.py:126` 只匹配 `monitor_segment.py` 子串——2026-08-07 起港股主力采样已是 `ws_segment.py`，`&&` 连跑 ws_segment 同样是等效降频但不被拦（检测面过时）；反向可能误伤同一命令里两次提及文件名的场景。改法：检测扩到 monitor_segment / ws_segment / futu_ws_segment 三脚本。
- [ ] 🟡 **ws_segment 不认 `--mode` 参数（只读环境变量）+ log 日期口径与 monitor_segment 不一致**（记录：2026-08-16 14:27）：① `ws_segment.py:37` 的 MODE 只读环境变量（默认 signal），auto 会话按 SKILL.md 惯例传 `--mode auto` 不生效——log 写进 signal 文件污染两会话隔离（2026-08-04 专设的按 mode 分文件机制被绕过），且 `--mode` 字符串还会被 parse_targets 当成一个标的。② `ws_segment.py:38,81` / `futu_ws_segment.py:40,117` 用 `date.today()` 北京日历日命名，monitor_segment/monitor_summary 对 US 标的用美东交易日——美股跨午夜后 ws 系写今天文件、summary 读昨天文件，午夜后采样在 summary 里消失。③ `ws_segment.py:96-107,197-200` 跨段累计 + 无「创新高才报」条件：某段破位后之后每段都重复报「破关键位」（与 monitor_segment「创新高才报」、futu_ws「每段重置」两种口径都不同，告警疲劳）。④ ws_segment 声称每秒记录、实际每 tick 一行（每秒多条、点数虚增，futu_ws 才真按秒聚合）。改法：①② 补 `--mode` 解析 + 日期按市场对齐；③ 破位告警加「创新高/新低」条件；④ 按秒聚合。
- [ ] 🟡 **monitor_segment 段结束统计空列表除零：纯 ws 采样文件崩溃且静默**（记录：2026-08-16 14:27）：`monitor_segment.py:419` 的 `sum(ratios)/len(ratios)`、`sum(vrs)/len(vrs)` 无空列表守卫——纯 ws 采样写出的 log（ratio/vr 列全空，已实测 `tmp/monitor_log_HK_00100_20260814_auto.csv` 19099 行全空 ratio）触发 ZeroDivisionError，异常被外层 except 吞掉后该块对剩余标的统计一并丢失。改法：空列表跳过该指标。

### 文档与口径

- [ ] 🟡 **risk-management.md「权益更新」绝对禁令与 signal 模式 equity-log 矛盾**（记录：2026-08-16 14:27）：`references/risk-management.md:60-64` 写「equity 必须从账户 API 取真实总资产、禁止用 equity-log 手动累加值」，无 signal 模式例外；而 SKILL.md 与 signal-mode.md 明确 signal 模式 equity 走 equity-log（signal 不连账户下单是模式定义）；2026-08-12 signal 模式又允许只读查询账户（费率算订单数确实连了账户），这条禁令处于半新半旧状态。改法：该节开头补模式分叉声明（auto 账户 API / signal equity-log，signal 的只读查询例外）。
- [ ] 🟡 **港股脚本 ETF 白名单仅 1 个成员 vs classify 30 个：注释声称一致、事实不一致**（记录：2026-08-16 14:27）：`trade_utils_tiger.py:778` 的 `_HK_ETF_WHITELIST` 只有 `HK.07709`，注释声称「与 classify_hk_security.py 一致」，classify 实际有 30 个官方 ETF（02800 盈富、02800/02828/03032/07300/07500 等）——交易 02800 等主流 ETF 时 `_sec_type_of` 判 stock、赔率计算多收 0.1% 印花税（保守方向、错杀交易而非亏钱）。改法：白名单对齐 classify（或直接 import classify 的名单）。
- [ ] 🟡 **平台费查询失败：代码不计费（乐观）与注释「保守档」方向相反**（记录：2026-08-16 14:27）：`trade_utils_tiger.py:817-824` 查询失败置 order_idx=None → `fee_per_side` 平台费记 0（少算、赔率偏乐观——会把不达标的交易算成达标），`:844` stderr 却提示「平台费将用保守档」（多算）——注释说保守、行为是乐观，方向相反。另 docstring 称「signal/auto 都从**实盘账户**取本月订单数」，实际查 config 当前账户（默认 paper）。改法：查询失败改按最高档 30 计（真保守）或至少修正注释；docstring 与行为对齐。
- [ ] 🟡 **同一笔交易两套费率两套 R 并存（源记录 vs review.py 复算）：需裁定权威口径**（记录：2026-08-16 14:27）：逐笔对照最大差 08-10 药明——源记录费用 65,486 → +0.48R，review.py/fee_schedule 复算 51,572 → +0.63R（差 13,914 HKD）；08-12 旭创 779.76→−1.28R vs 633.8→−1.23R 等约 8 笔存在两套数值。复盘报告自身按 review.py 口径自洽，但读者拿复盘表对源文件会得出「同一笔 R 不同」。另 `actions/2026-08-13-HKT-actions.md` 开仓条目「实际 max_loss 80,000（16000×止损距 5）」用的参考价口径却标「实际」（按成交价应为 4.6×16000=73,600，同文件平仓条目与复盘 CSV 均用 73,600）；HKT 副本赔率表述「×0.5 折扣后踩线达标」掩盖净口径 2.12×0.5=1.06<1.2 未过闸的事实（ET 副本如实写明）。改法：裁定 fee_schedule 为唯一权威费率口径（源记录以脚本输出为准）、修正 08-13 开仓条目的 max_loss 标签；写入记录规范（动作记录的费用字段直接抄脚本输出、不手算）。
- [ ] 🟡 **08-11 MU 两笔记录数字混用**（记录：2026-08-16 14:27）：`actions/2026-08-11-ET-actions.md` 第 1 笔开仓表成交价 855.33、平仓表开仓价 855.31——盈亏毛 −12,969 按 855.31 算才对、max_loss 14,865 按 855.33 算才对，平仓表分母又写 14,895；第 2 笔 max_loss「10,186（2394×止损距 4.255）」——止损距 858.5−854.25=4.25、4.255 无来源（复盘 CSV 用 10,175）；equity 第 1 笔后 1,042,636−13,804=1,028,832 ≠ 第 2 笔记的 1,029,647（差 +815 无解释）。改法：裁定成交价与 max_loss 的单一来源（脚本 JSON 输出），修正或标注留痕。
- [ ] 🟡 **既有存量待办（购买力校验）**（记录：2026-08-12 10:14，2026-08-16 并入分级）：开仓前按单标的保证金率算可买上限 + 主动降档（2026-08-06 00100 被拒根因闭环）——① `trade_utils_tiger.py` 加 `get_buying_power_tiger(config, symbol, ref_price)`（读 `get_prime_assets` 的 `buying_power` + `get_contract` 的 `long_initial_margin`，返回本标的可买股数上限，按 lot 向下取整；美股版先验证 `get_contract` 是否返回该字段）；② `open_position_tiger.py` / `open_position_tiger_us.py` 算出目标股数后与上限取 min、超了下单前主动降档（输出 `capped_by_buying_power: true`），被动降档保留兜底；③ `auto-mode.md`「下单失败处置」节补「购买力不足 = 单标的保证金率约束」一条（与 cross-trading 并列），CHANGELOG 8-06 条目的 8-11 旧推断修正为 8-12 实测结论（00100 保证金率 0.75、可买上限 817.5 万÷0.75=1090 万=37,484 股）。
- [ ] 🟡 **既有存量待办（三阶段赔率标定）**（记录：2026-08-11 19:25，2026-08-16 并入分级）：三阶段赔率样本积累 ≥30 笔后重新标定「预估赔率打折系数」——2026-08-05 用 12 笔粗标定 ×0.5（落地/初始 中位数 −0.22、75% 落地 ≤0）；2026-08-14 补 9 笔执行后样本（初始均值 3.61R、落地 +0.89R、78% 落地为正、兑现率 25%）显示门槛生效但仍是小样本；样本 20 → 需 ≥30 笔按合并分布重标定并更新 trading-strategy.md 对应节（注意 2026-08-14 起门槛已改单一 2.4、标定结论的表述口径同步）。

## 🟢 轻危（文档措辞 / 格式漂移 / 卫生问题，有空再处理）

### 文档

- [ ] 🟢 **signal-mode.md L16 残留长桥 MIT 术语**（记录：2026-08-16 14:27）：「auto 由脚本挂附加 / 独立 **MIT** 止损单」——auto 已全转老虎（STP），MIT 是 2026-08-05 已删除的长桥术语。改为「STP / 附加腿 LOSS」。
- [ ] 🟢 **actions/README.md 整体过时**（记录：2026-08-16 14:27）：仍写「美股模拟盘交易动作记录」「signals/ = 港股信号模式」，与 2026-07-31 已扩展的双市场双目录现实不符（实际有 6 个 HKT 文件、signals 含多个 ET 文件）。按项目 CLAUDE.md「actions/ 目录归属」节重写。
- [ ] 🟢 **README 宣传「A 股」但 skill 无任何 A 股支持**（记录：2026-08-16 14:27）：中英 README 标题与徽章都是「HK / US / A-Share」，trade skill 全文（时段、代码格式、费率、数据源）只有港美。改法：或删 A 股表述、或在 README 标注「规划中未实现」。
- [ ] 🟢 **SKILL.md 6 要素第 3 条参考价定义有歧义**（记录：2026-08-16 14:27）：「参考价 = 净初始预期赔率 ≥ 2.4 时的入场价」——2.4 是筛选门槛不是参考价的定义属性，全项目其余 15+ 处「参考价」都是「snapshot 实价作基准」。改写为「参考价 = snapshot 实价（须满足净初始预期赔率 ≥ 2.4 才用它开仓）」。
- [ ] 🟢 **signals/README 格式约定失效**（记录：2026-08-16 14:27）：第 18 行要求「每条信号含 ═══ 框线」，实际 08-04 / 08-07 / 08-12 / 08-13 最新信号文件均无框线（用 ## 标题）——格式已漂移但 README 未更新；另第 19 行「不写账户 / 资金」与实际内容冲突（08-13 信号写 equity 115,974.69 与「当前总资产」）。改法：README 按当前实际格式更新、或信号文件恢复框线，二选一。
- [ ] 🟢 **美股脚本 docstring 与代码矛盾（LMT 残留）**（记录：2026-08-16 14:27）：`open_position_tiger_us.py:4` 文件头仍写「主单 LMT（控价、限价取整）」、`trade_utils_tiger_us.py:224` 报错文案写「LMT+附加止损」——实际 2026-08-07 起已是 MKT。改为 MKT 表述。
- [ ] 🟢 **2026-08-13-HKT-review.md 小错**（记录：2026-08-16 14:27）：第 12 行「全部 17 个 HKT 文件」实际两目录共 23 个（signals 17 + actions 6）；第 374 行用「08-07 MINIMAX 落地 1.96R」（源口径）与第 230 行 8.3 表「+2.01R」（复盘口径）混用未标注。修正计数 + 统一口径标注。
- [ ] 🟢 **08-12 / 08-10 记录小错**（记录：2026-08-16 14:27）：08-12 信号预估 max_loss 2,600 按参考价 1077 计、响铃后实测 1076 未按 README 要求修正（CSV 与平仓注已用 2,800）；08-10 actions 平仓「止损触发价 201.2」vs 移损「modify 到 201.2（验证后为 201.3）」两说。修正或标注。
- [ ] 🟢 **trade_utils 注释与代码矛盾（symbol 补零）**（记录：2026-08-16 14:27）：`trade_utils_tiger.py:241-242` 注释称「如传入无前导 0 则补足」，代码不 zfill（HK.700 会在券商侧报不支持）。注释改为陈述实际行为。

### 脚本卫生

- [ ] 🟢 **round_to_tick 恒 floor 的方向性偏差未说明**（记录：2026-08-16 14:27）：`trade_utils_tiger.py:307-313` 恒向下取整——卖方限价取整后低于 bid（更激进）、做多止损触发价被压低一个 tick（实际 max_loss 略大于计划）。非紧急，但应在 docstring 说明方向含义。
- [ ] 🟢 **monitor_summary 细节三处**（记录：2026-08-16 14:27）：① `:87` 的 4 段均价切片 `quart=n//4` 不整除时丢弃尾部最多 3 个最新点（末段偏向旧数据）；② `:91` 守卫用 `n > recent_n`（总行数）而非 `len(turnovers)>recent_n`（稀疏列窗口跨度失真）；③ `:102-107` VWAP 段 `_q.close()` 在 try 内、异常时连接泄漏。顺手修。
- [ ] 🟢 **bayes_evolution 卫生三处**（记录：2026-08-16 14:27）：① `:288` 的 `FS = [0.005,...]` 遮蔽 `import fee_schedule as FS` 模块别名（当前无功能影响、后续在该行后调 FS.fee_per_side 即崩）；② `:175-176,236` 样本仅 1 笔时 `xs[-1]` IndexError 整脚本崩；③ `math.log(1+fR)` 无定义域保护（f=0.50 档、任一笔净 R≤−2 时 ValueError 全脚本崩，当前实测最差 −1.2 未触发）。
- [ ] 🟢 **review.py 过程指标分组口径**（记录：2026-08-16 14:27）：`:367-368` 的 `Wd=[R>0] / Ld=[R<0]` 使 R=0 的笔从两组都消失，而 `:177-179` summarize 把 R=0 归败——同文件两处口径不一致（R=0 存在时过程指标 n 加总 < N）。统一归败。
- [ ] 🟢 **kline.py 斐波那契不区分趋势方向**（记录：2026-08-16 14:27）：`:22-25` 只输出 `lo+span×r`（从低点向上的回调位），下跌趋势时应输出 `hi−span×r`（反弹阻力位）——现输出对下跌段语义颠倒。
- [ ] 🟢 **其余低危**（记录：2026-08-16 14:27）：`log_action.sh` note/ring.sh 逗号不转义破坏 CSV 列结构；`monitor_log_gap_check.py` 午休 60 分钟 gap 会照标「降频疑点」；美东时差 −12h 夏令时硬编码三处（11 月切冬令时要手动改 −13h，guard 还要改 21:30→22:30）；`hot_list.py` market 参数拼写错静默落美股；`fee_schedule.py` 美股佣金最低 15 按美元计系简化（保守方向）；REIT 在费率侧无对应档（落 stock 收印花税，方向保守、待核实港股 REIT 免征与否）；`to_futu_symbol_us` 的 `split(".")[0]` 吃掉 BRK.B 类别后缀；close 平仓脚本 `_parse_args` 的 `--account` 缺值时报错文案显示 'None'；交收费无最低收费、印花税未按笔取整到元（历史规则、待对 Tiger 账单核实）。
