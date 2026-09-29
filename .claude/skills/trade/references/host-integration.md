# 宿主集成：CC / ZCode / CodeBuddy / pi

> 本文件说明 trade skill 在四个 agent 宿主上的**工具名映射与机制差异**。开工前先扫一眼本文件；
> 后续 reference 里出现的 `run_in_background` / task-notification / `AskUserQuestion` / `TaskStop` /
> hook 挂载点，按当前宿主落到下表右列或左列即可——**交易策略、脚本调用、护栏判定四宿主完全通用**，
> 差异只在「怎么启动后台段、怎么向用户提问、护栏挂在哪儿」这三类宿主接口。

## 速查表

| 能力 | Claude Code / ZCode / CodeBuddy | pi |
|---|---|---|
| 后台跑密采样段 | `Bash(run_in_background=true)`，段结束 task-notification 唤醒 AI | `background_run` 工具（pi 扩展提供），段结束以 `[后台任务完成]` 消息唤醒 AI（附输出尾部） |
| 查看后台任务 | 任务列表 / TaskOutput | `background_list` |
| 停止后台任务 | `TaskStop` | `background_stop` |
| 结构化提问（实盘授权 / 熔断重启确认） | `AskUserQuestion` | `AskUserQuestion`（扩展同名提供，交互与答案口径对齐） |
| 会话 ID（认领 / 互斥闸 / 实盘解锁绑定） | 环境变量 `CLAUDE_CODE_SESSION_ID` | 环境变量 `PI_SESSION_ID`（脚本已双读，见下方第 4 条） |
| 护栏 hooks | CC：`.claude/settings.json`；ZCode：用户级 `~/.zcode/cli/config.json`（项目级被官方禁用）；CodeBuddy：读 `.claude/settings.json`（另 `.codebuddy/settings.json` 兜底） | `.pi/extensions/trade-guards.ts` 桥接同一批 `.claude/hooks/*.py`（单一源，规则改 Python 脚本即可） |
| 实盘授权见证 | `live_auth_witness.py` 由 PostToolUse(AskUserQuestion) 触发 | 同上——pi 的 `AskUserQuestion` 扩展把用户真实点选合成 PostToolUse payload 触发见证，点击同样不可伪造 |

## pi 宿主细节

1. **后台段用 `background_run`，不换命令内容**。命令与 CC 完全一致（如
   `python3 .claude/skills/trade/scripts/monitor_segment.py HK.00100:330:306 40 10 --mode signal`），
   只是不再有 `run_in_background` 参数——把这个工具名连同命令一起用即可。禁止 nohup / 行尾 `&` /
   拼 `sleep`（这些绕过通知驱动的跑法会被 `monitor_guard` 拦，pi 下同样拦——守卫经桥接脚本生效）。
   段结束时收到 `[后台任务完成] <id>` 消息（含退出码、用时、输出尾部），完整日志在
   `tmp/pi-bg/<sid8>-<id>.log`（按会话分名，多会话并行不互相污染）；**照常先读数据 + 分析 + 写心跳，
   再 `background_run` 下一段**，循环节奏与
   CC 宿主完全一致（40 秒一段、段间不 sleep、不连跑多段）。
2. **提问用 `AskUserQuestion`**（参数：`questions: [{question, header?, options:[{label, description?}], multiSelect?}]`）。
   用户 Esc / 未答 = 无答案，不产生实盘授权凭证；实盘授权问题仍必须含打码号 `67****91` 或
   `--account live` 字样 + 「实盘」字样 + 授权范围（`live_auth_witness.py` 的判定条件四宿主一致）。
3. **护栏由 `.pi/extensions/trade-guards.ts` 桥接**：把 pi 事件合成 CC hook payload、spawn 同一批
   Python 守卫——`secret_guard`（Bash 读凭证拦截；另补 pi `read` 工具读 accounts.json / `~/.tigeropen`
   的拦截，对齐 CC 端 `permissions.deny`）、`monitor_guard`（密采样跑法 / 连跑 / 空转 / 标的池互斥 /
   回合结束断链提醒）、`changelog_guard`（CHANGELOG / TODO 卫生提醒）。
   **改规则只改 `.claude/hooks/*.py`**，pi 端自动生效、不需要动扩展——这是刻意设计的单一源，
   避免四个宿主各维护一份判定。
4. **会话 ID 双读 + pi transcript 探测**：脚本按 `CLAUDE_CODE_SESSION_ID` → `PI_SESSION_ID`
   顺序取值（CC / ZCode / CodeBuddy 注入前者、pi 注入后者）。认领表 `pool_claims.json`、
   互斥闸 `trade_intent.log`、复活注册 `monitor_sessions.txt`、实盘解锁 `live-unlock.<sid>.local` /
   授权凭证 `live-auth.<sid>.local` 的按会话分文件逻辑全部通用。**死会话判定**
   （`pool_claim._session_alive` / `monitor_guard._claim_holder_alive`：会话 transcript 停更 > 30
   分钟 = 已结束、释放认领）同样按宿主探测路径：CC 查 `~/.claude/projects/<slug>/<sid>.jsonl`、
   ZCode 查 `~/.zcode/cli/rollout/`、**pi 查 `<agent_dir>/sessions/--<slug>--/*_<sid>.jsonl`
   （另接 `PI_SESSION_FILE` 应对自定义 session-dir）**——不补这条，pi 会话的认领会被误判成
   死会话释放，多会话互斥失真。
5. **pi 没有的宿主功能**：无内建后台 bash（已由 `background_run` 补齐）；无 CC 的 `permissions.deny`
   （已由桥接的 read 拦截补齐）；无 Stop hook（由 `agent_settled` 事件桥接 `monitor_guard.py stop`，
   且守卫内部已按「盯盘会话」门槛过滤，不会非盯盘会话误唤醒）。除此之外无缺口。

## 验证状态（2026-09-29）

pi 桥接层用 20 项冒烟检查逐条实测通过：read 凭证拦截 / bash 读凭证拦截 / nohup 采样段拦截 /
采样段拼 sleep 拦截 / 干净采样命令放行 / trade 脚本放行 / CHANGELOG 提醒注入 / 回合静止 stop 桥执行 /
`AskUserQuestion` 提问与答案回传 / 实盘授权见证写 `live-auth` 文件 / 非确认答案不写凭证 /
后台任务启动→完成通知→清单→停止。另在真实 pi 会话里做了端到端验证：`read accounts.json` 被拦、
`background_run` 完成通知送达、`write` 后 📌 CHANGELOG 提醒注入。脚本侧全部改动文件编译通过；
会话 ID 双读实测（`PI_SESSION_ID` 生效、`CLAUDE_CODE_SESSION_ID` 优先）；pi transcript 死会话探测
实测（`pool_claim._session_alive` / `monitor_guard._claim_holder_alive` 对本会话返回 True、对不存在
会话返回 False）。**盘中实战验收（采样循环 / 互斥闸并行 / 实盘解锁链）留 TODO T159**。
