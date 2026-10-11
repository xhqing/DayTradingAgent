/**
 * DayTradingAgent pi 宿主守卫桥（2026-09-29 立）。
 *
 * 职责：把 pi 的扩展事件合成成 Claude Code 钩子协议的 payload，spawn `.claude/hooks/`
 * 下的同一批 Python 守卫（单一源），执行结果按 pi 的语义落地：
 *
 * 1. `tool_call`（bash / background_run 命令）→ `secret_guard.py pretool`（凭证读取拦截）
 *    + `monitor_guard.py pretool`（密采样跑法 / 连跑 / 空转 / 标的池互斥拦截）。
 *    guard 退出码 2 = pi 返回 { block: true, reason: stderr }，工具调用被拦下、原因回给 AI。
 * 2. `tool_call`（read 工具）→ 补 CC 端 `permissions.deny` 的等价物：accounts.json /
 *    ~/.tigeropen 等凭证文件的 Read 在 pi 里没有 settings 层禁用，这里直接拦。
 *    （secret_guard 只管 Bash 命令行读数，管不到 pi 的 read 工具——不补的话 AI 在 pi
 *    会话里可以直接 read 到完整账户号与私钥，是实打实的防线缺口。）
 * 3. `tool_result`（edit / write）→ `changelog_guard.py`（CHANGELOG / TODO 卫生提醒），
 *    提醒文本追加进工具结果内容（模型下一轮就能看到，不额外触发回合）。
 * 4. `agent_settled`（回合静止、不再自动继续时）→ `monitor_guard.py stop`：盘中盯盘
 *    会话若密采样断链，把提醒作为自定义消息注入并唤醒一轮（对应 CC 的 Stop hook）。
 *
 * 规则本身一行都不在本文件里——改规则改 `.claude/hooks/*.py`，CC / ZCode / CodeBuddy /
 * pi 四宿主同步生效（为什么不让 pi 端读 `.claude/settings.json`：pi 没有 CC 的 hooks
 * 配置机制，扩展是 pi 的官方扩展点，桥接脚本是最小重复的方案）。
 */
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { additionalContext, runHook } from "./lib/hooks.ts";

// —— 廉价预筛（严格超集，命中才 spawn Python，省掉绝大多数命令的进程开销）——
// secret_guard 只在命令文本出现凭证路径特征时可能拦。
const SECRET_TRIGGER = /\.tigeropen|accounts\.json|private_key|TIGEROPEN|tiger_openapi_config/;
// monitor_guard pretool 只在命令出现采样相关脚本名时可能拦（跑法 / 连跑 / 空转 / 池互斥）。
const MONITOR_TRIGGER = /monitor_segment|ws_segment|futu_ws_segment|snapshot|hot_list/;
// read 工具禁止直接读的凭证文件（对齐 CC 端 permissions.deny 的口径并覆盖 ~/.tigeropen）。
// 路径写法兼容绝对 / 相对（`accounts.json` 裸文件名也算）与 Windows 反斜杠。
const READ_DENY = [
	/(^|[\\/])accounts\.json$/,
	/(^|[\\/])\.tigeropen([\\/]|$)/,
	/tiger_openapi_config/,
	/private_key/,
];

const READ_DENY_REASON =
	"⛔ [trade-guards] 拦截：该文件是券商凭证（完整账户号 / RSA 私钥 / API 配置）。" +
	"实盘与模拟共用同一套凭证，完整账户号与私钥不得进入 AI 对话上下文（会随上下文进入" +
	"模型请求）。需要账户信息时用打码口径（如 67****91）；需要切实盘确认时请用户自己核对" +
	"账户号。如确需检查凭证内容，请用户在终端自行查看。";

interface PiToolCallLike {
	toolName: string;
	input?: Record<string, unknown>;
}

/** 取「命令类」工具的命令文本：bash 与 background_run（pi 宿主的后台段工具）。 */
function commandOf(event: PiToolCallLike): string {
	if (event.toolName !== "bash" && event.toolName !== "background_run") return "";
	const cmd = event.input?.["command"];
	return typeof cmd === "string" ? cmd : "";
}

export default function tradeGuards(pi: ExtensionAPI) {
	// —— ① PreToolUse 桥 ——
	pi.on("tool_call", async (event, ctx) => {
		try {
			const toolName = (event as PiToolCallLike).toolName;

			// read 工具的凭证文件硬拦（pi 没有 CC 的 permissions.deny 层）
			if (toolName === "read") {
				const pathArg = (event as PiToolCallLike).input?.["path"];
				if (typeof pathArg === "string" && READ_DENY.some((re) => re.test(pathArg))) {
					return { block: true, reason: READ_DENY_REASON };
				}
				return;
			}

			const command = commandOf(event as PiToolCallLike);
			if (!command) return;

			const sessionId = ctx.sessionManager.getSessionId();
			const payload = {
				hook_event_name: "PreToolUse",
				session_id: sessionId,
				tool_name: "Bash", // pi 的 bash / background_run 对守卫统一按 CC 的 Bash 语义处理
				tool_input: { command },
			};

			const jobs: Array<{ label: string; result: Promise<{ code: number; stdout: string; stderr: string }> }> = [];
			if (SECRET_TRIGGER.test(command)) {
				jobs.push({ label: "secret_guard", result: runHook("secret_guard.py", ["pretool"], payload) });
			}
			if (MONITOR_TRIGGER.test(command)) {
				jobs.push({ label: "monitor_guard", result: runHook("monitor_guard.py", ["pretool"], payload) });
			}
			if (jobs.length === 0) return;

			const results = await Promise.all(jobs.map((j) => j.result));
			for (let i = 0; i < results.length; i++) {
				const r = results[i];
				if (r.code === 2) {
					const reason =
						r.stderr.trim() ||
						r.stdout.trim() ||
						`[trade-guards] ${jobs[i].label} 拦截（未附原因）`;
					return { block: true, reason };
				}
			}
		} catch {
			// 守卫自身异常不阻塞会话（与全局 git-commit-guard / twitter-guard 的 fail-open 口径一致）
		}
	});

	// —— ② PostToolUse 桥：edit / write 后的 CHANGELOG 与 TODO 卫生提醒 ——
	pi.on("tool_result", async (event, ctx) => {
		try {
			if (event.toolName !== "edit" && event.toolName !== "write") return;
			const input = (event.input ?? {}) as Record<string, unknown>;
			const filePath = input["path"] ?? input["file_path"] ?? input["filePath"];
			if (typeof filePath !== "string" || !filePath) return;

			const payload = {
				hook_event_name: "PostToolUse",
				session_id: ctx.sessionManager.getSessionId(),
				tool_name: event.toolName === "edit" ? "Edit" : "Write",
				tool_input: { file_path: filePath },
				tool_response: { ok: !event.isError },
			};
			const r = await runHook("changelog_guard.py", [], payload);
			const note = additionalContext(r.stdout);
			if (note) {
				// 追加到工具结果内容：提醒随下一次模型请求进入上下文，不额外触发回合
				return { content: [...event.content, { type: "text", text: note }] };
			}
		} catch {
			// 忽略
		}
	});

	// —— ③ TaskStop 桥：background_stop 后的密采样停盯提醒（对应 CC 的 PreToolUse/TaskStop）——
	// 本项目规则「密采样停盯前确认边界」的原实现挂在 background-tasks 扩展内部；
	// 2026-10-11 该扩展上移全局（通用能力）后，项目联动迁回本项目守卫桥这里，
	// 语义不变：不阻断、只把 monitor_guard 的提醒带回给 AI（随下一次模型请求进入上下文）。
	pi.on("tool_result", async (event, ctx) => {
		try {
			if (event.toolName !== "background_stop") return;
			const payload = {
				hook_event_name: "PreToolUse",
				session_id: ctx.sessionManager.getSessionId(),
				tool_name: "TaskStop", // 对齐 CC 的 TaskStop 语义（monitor_guard taskstop 按此分支）
				tool_input: {},
			};
			const r = await runHook("monitor_guard.py", ["taskstop"], payload);
			const note = additionalContext(r.stdout) || r.stderr.trim();
			if (note) {
				return { content: [...event.content, { type: "text", text: note }] };
			}
		} catch {
			// 忽略
		}
	});

	// —— ④ Stop 桥：回合静止时检查盯盘密采样是否断链（对应 CC 的 Stop hook）——
	pi.on("agent_settled", async (_event, ctx) => {
		try {
			const payload = {
				hook_event_name: "Stop",
				session_id: ctx.sessionManager.getSessionId(),
			};
			const r = await runHook("monitor_guard.py", ["stop"], payload);
			const note = additionalContext(r.stdout);
			if (note) {
				// 与 CC Stop hook 的 additionalContext 效果对齐：注入提醒并让会话继续一轮。
				// 守卫内部已按「盯盘会话」门槛过滤（非盯盘会话静默），不会造成每回合反复唤醒。
				pi.sendMessage(
					{
						customType: "trade-guard",
						content: note,
						display: true,
					},
					{ deliverAs: "followUp", triggerTurn: true },
				);
			}
		} catch {
			// 忽略
		}
	});
}
