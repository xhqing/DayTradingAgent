# 宿主集成：pi

> 本项目**只使用 pi 作为 agent 宿主**（2026-11-05 用户立：Claude Code / ZCode / CodeBuddy 的
> 配置文件与兼容层已全部移除）。本文件说明 trade skill 在 pi 上的工具名与机制，开工前先扫一眼；
> 后续 reference 里出现的 `background_run` / task-notification / `AskUserQuestion` / `background_stop` /
> hook 桥接，都按下表落到 pi 上即可。

## 速查表

| 能力 | pi |
|---|---|
| 后台跑密采样段 | `background_run` 工具（**全局** pi 扩展提供，2026-10-11 自项目扩展上移），段结束以 `[后台任务完成]` 消息唤醒 AI（附输出尾部） |
| 查看后台任务 | `background_list` |
| 停止后台任务 | `background_stop` |
| 结构化提问（实盘授权 / 熔断重启确认） | `AskUserQuestion`（项目扩展 `.pi/extensions/ask-question.ts` 同名提供） |
| 会话 ID（认领 / 互斥闸 / 实盘解锁绑定） | 环境变量 `PI_SESSION_ID`（会话记录文件另有 `PI_SESSION_FILE`） |
| 护栏 hooks | `.pi/extensions/trade-guards.ts` 把 pi 事件合成 hook payload、spawn 项目根 `hooks/*.py`（单一源，规则改 Python 脚本即可） |
| 实盘授权见证 | `live_auth_witness.py` 由 pi 的 `AskUserQuestion` 扩展把用户真实点选合成 PostToolUse payload 触发见证，点击不可伪造 |

## pi 宿主细节

1. **后台段用 `background_run`，不换命令内容**。完整路径写法：
   `python3 .pi/skills/trade/scripts/monitor_segment.py HK.00100:330:306 40 10 --mode signal`。
   禁止 nohup / 行尾 `&` / 拼 `sleep`（这些绕过通知驱动的跑法会被 `monitor_guard` 拦，守卫经桥接脚本生效）。
   段结束时收到 `[后台任务完成] <id>` 消息（含退出码、用时、输出尾部），完整日志在
   `tmp/pi-bg/<sid8>-<id>.log`（按会话分名，多会话并行不互相污染）；**照常先读数据 + 分析 + 写心跳，
   再 `background_run` 下一段**（40 秒一段、段间不 sleep、不连跑多段）。
2. **提问用 `AskUserQuestion`**（参数：`questions: [{question, header?, options:[{label, description?}], multiSelect?}]`）。
   用户 Esc / 未答 = 无答案，不产生实盘授权凭证；实盘授权问题仍必须含打码号 `67****91` 或
   `--account live` 字样 + 「实盘」字样 + 授权范围（`live_auth_witness.py` 的判定条件）。
3. **护栏由 `.pi/extensions/trade-guards.ts` 桥接**：把 pi 事件合成 hook payload、spawn 同一批
   Python 守卫——`secret_guard`（bash / background_run 读凭证拦截；另由扩展直接拦 pi `read` 工具
   读 accounts.json / `~/.tigeropen`）、`monitor_guard`（密采样跑法 / 连跑 / 空转 / 标的池互斥 /
   回合结束断链提醒）、`changelog_guard`（CHANGELOG / TODO 卫生提醒）、`jms_guard`（盘中禁拉订阅）。
   **改规则只改 `hooks/*.py`**，pi 端自动生效、不需要动扩展——刻意设计的单一源。
4. **会话 ID 与死会话判定**：脚本按环境变量 `PI_SESSION_ID` 取值；认领表 `pool_claims.json`、
   互斥闸 `trade_intent.log`、复活注册 `monitor_sessions.txt`、实盘解锁 `live-unlock.<sid>.local` /
   授权凭证 `live-auth.<sid>.local` 的按会话分文件逻辑全部按它来。**死会话判定**
   （`pool_claim._session_alive` / `monitor_guard._claim_holder_alive`：会话 transcript 停更 > 30
   分钟 = 已结束、释放认领）查 `<agent_dir>/sessions/--<slug>--/*_<sid>.jsonl`，并以
   `PI_SESSION_FILE` 兜底。
5. **pi 能力的对应关系**：后台 bash 由 `background_run` 提供；凭证读拦截由桥接的 `read` 拦截补齐；
   回合结束提醒由 `agent_settled` 事件桥接 `monitor_guard.py stop`（守卫内部已按「盯盘会话」门槛过滤，
   不会对非盯盘会话误唤醒）。

## 验证状态

**2026-09-29 盘外**：pi 桥接层 20 项冒烟检查逐条实测通过——read 凭证拦截 / bash 读凭证拦截 /
nohup 采样段拦截 / 采样段拼 sleep 拦截 / 干净采样命令放行 / trade 脚本放行 / CHANGELOG 提醒注入 /
回合静止 stop 桥执行 / `AskUserQuestion` 提问与答案回传 / 实盘授权见证写 `live-auth` 文件 /
非确认答案不写凭证 / 后台任务启动→完成通知→清单→停止。真实 pi 会话端到端：`read accounts.json` 被拦、
`background_run` 完成通知送达、`write` 后 📌 CHANGELOG 提醒注入。会话 ID 与 pi transcript 死会话探测
实测正常（对本会话返回 True、对不存在会话返回 False）。

**盘中实战验收（采样循环 / 互斥闸并行 / 实盘解锁链）留 TODO T159**。
