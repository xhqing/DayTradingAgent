/**
 * pi 宿主后台任务工具（2026-09-29 立）。
 *
 * 为什么需要它：pi 官方设计「不内建后台 bash」（README 明说 use tmux），而本项目的
 * 盯盘机制靠「后台跑一段密采样（40 秒）→ 段结束通知唤醒 AI → 读数据 + 分析 + 重启
 * 下一段」的循环驱动（Claude Code 里是 `Bash(run_in_background=true)` + task-notification）。
 * 没有后台任务能力，trade skill 在 pi 里根本转不起来。本扩展用 Node 子进程 + 完成时
 * `pi.sendMessage(..., { triggerTurn: true })` 补上这一能力，语义与 CC 对齐：
 *
 *   - `background_run`   ≈ CC 的 `Bash(run_in_background=true)`：非阻塞启动、输出落文件，
 *                          进程结束时以通知消息唤醒 AI（消息里带输出尾部 + 完整日志路径）。
 *   - `background_stop`  ≈ CC 的 `TaskStop`：杀整个进程组（含 python 子进程）；
 *                          若正在停的是密采样，`monitor_guard.py taskstop` 会给出违规提醒。
 *   - `background_list`  ≈ 查看后台任务清单与日志路径。
 *
 * 守卫联动：`background_run` 的命令同样受 trade-guards 扩展的 PreToolUse 桥拦截
 * （nohup / 行尾 & / 拼 sleep 三种跑法一律阻断，合法跑法只有本工具）。
 */
import { spawn } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { Type } from "typebox";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { additionalContext, PROJECT_ROOT, runHook } from "./lib/hooks.ts";

interface BgTask {
	id: string;
	command: string;
	label: string;
	pid: number;
	logPath: string;
	startedAt: number;
	endedAt?: number;
	status: "running" | "exited" | "killed" | "failed";
	exitCode: number | null;
	signal?: string | null;
}

/** 输出尾部（通知里只带尾部，完整输出在日志文件）。 */
function tailOf(logPath: string, maxLines = 60, maxChars = 6000): string {
	try {
		const text = fs.readFileSync(logPath, "utf8");
		let lines = text.split("\n");
		if (lines.length > maxLines) lines = lines.slice(-maxLines);
		let out = lines.join("\n").trimEnd();
		if (out.length > maxChars) out = out.slice(-maxChars);
		return out;
	} catch {
		return "";
	}
}

function fmtDuration(ms: number): string {
	const s = Math.max(0, Math.round(ms / 1000));
	if (s < 60) return `${s} 秒`;
	const m = Math.floor(s / 60);
	return `${m} 分 ${s % 60} 秒`;
}

export default function backgroundTasks(pi: ExtensionAPI) {
	const tasks = new Map<string, BgTask>();
	let counter = 0;
	let shuttingDown = false;

	const logDir = path.join(PROJECT_ROOT, "tmp", "pi-bg");

	function killGroup(pid: number, signal: NodeJS.Signals): void {
		try {
			process.kill(-pid, signal); // 负号 = 杀整个进程组（含 python 子进程）
		} catch {
			try {
				process.kill(pid, signal);
			} catch {
				// 进程已退出
			}
		}
	}

	function notifyCompletion(task: BgTask, signal: string | null): void {
		if (shuttingDown) return;
		const duration = fmtDuration((task.endedAt ?? Date.now()) - task.startedAt);
		const outcome =
			task.status === "exited"
				? `退出码 ${task.exitCode}`
				: signal
					? `被信号 ${signal} 终止`
					: "已结束";
		const tail = tailOf(task.logPath);
		const rel = path.relative(PROJECT_ROOT, task.logPath);
		const body = tail ? `\n──── 输出尾部 ────\n${tail}` : "\n（无输出）";
		const text =
			`[后台任务完成] ${task.id}${task.label ? `「${task.label}」` : ""} ${outcome}（运行 ${duration}）\n` +
			`命令：${task.command}\n` +
			`完整日志：${rel}\n` +
			`请读取日志/输出完成分析；盯盘密采样段请按 trade skill 规定：读完数据、分析、再启动下一段。` +
			body;
		try {
			pi.sendMessage(
				{ customType: "bg-task", content: text, display: true },
				{ deliverAs: "followUp", triggerTurn: true },
			);
		} catch {
			// pi 正在退出等场景：忽略
		}
	}

	pi.registerTool({
		name: "background_run",
		label: "后台运行",
		description:
			"在后台运行一条 bash 命令（立即返回、不阻塞对话）；命令结束时 pi 会以通知消息唤醒你并附上输出尾部。" +
			"完整输出落在 tmp/pi-bg/ 下的任务日志（通知里给出具体路径）。" +
			"盯盘密采样段（monitor_segment.py / ws_segment.py / futu_ws_segment.py）必须用它启动" +
			"（等价 Claude Code 的 Bash run_in_background）；禁止 nohup / 行尾 & / sleep 轮询等绕过通知的跑法。",
		parameters: Type.Object({
			command: Type.String({ description: "要后台运行的 bash 命令（不要自己加 nohup / & / sleep）" }),
			label: Type.Optional(Type.String({ description: "任务标签，便于识别（如「00100 密采样段」）" })),
		}),
		executionMode: "sequential",
		async execute(_toolCallId, params, _signal, _onUpdate, ctx) {
			fs.mkdirSync(logDir, { recursive: true });
			counter += 1;
			const id = `bg-${counter}`;
			// 日志文件按会话分名（<sid8>-<id>.log）：任务编号是进程内计数、多会话并行时
			// 会重复（都从 bg-1 起），不加会话前缀会互相覆盖 / 混入别的会话的段输出。
			// 会话 id 若取不到（极端情况）退到进程启动时间戳。
			const sid = String(ctx?.sessionManager?.getSessionId?.() ?? "");
			const sidSafe = sid.replace(/[^A-Za-z0-9-]/g, "").slice(0, 8) || `${Date.now().toString(36)}`;
			const logName = `${sidSafe}-${id}.log`;
			const logPath = path.join(logDir, logName);
			const relLog = `tmp/pi-bg/${logName}`;
			let fd: number | undefined;
			try {
				fd = fs.openSync(logPath, "a");
				// bash -c（非登录 shell）：pi 进程已继承用户环境（PATH / 代理等），
				// 不必再 source 一遍 ~/.bash_profile——登录 shell 会把 profile 报错行
				// 混进每段输出尾部，对密采样分析是纯噪声。
				const child = spawn("bash", ["-c", params.command as string], {
					cwd: PROJECT_ROOT,
					detached: true, // 独立进程组，停任务时整组杀
					stdio: ["ignore", fd, fd],
					env: { ...process.env },
				});
				if (child.pid === undefined) {
					throw new Error("spawn 未返回 pid");
				}
				fs.closeSync(fd);
				fd = undefined;
				const task: BgTask = {
					id,
					command: params.command as string,
					label: params.label ?? "",
					pid: child.pid,
					logPath,
					startedAt: Date.now(),
					status: "running",
					exitCode: null,
				};
				tasks.set(id, task);
				child.on("error", (err) => {
					task.status = "failed";
					task.endedAt = Date.now();
					const note = `[后台任务失败] ${id} 启动失败：${String(err)}`;
					try {
						pi.sendMessage(
							{ customType: "bg-task", content: note, display: true },
							{ deliverAs: "followUp", triggerTurn: true },
						);
					} catch {
						// 忽略
					}
				});
				child.on("exit", (code, signal) => {
					task.endedAt = Date.now();
					task.exitCode = code;
					task.signal = signal;
					if (task.status === "running") task.status = "exited";
					if (!shuttingDown) notifyCompletion(task, signal);
				});
				return {
					content: [
						{
							type: "text",
							text:
								`后台任务已启动：${id}${task.label ? `「${task.label}」` : ""}\n` +
								`日志：${relLog}\n` +
								`任务结束时你会收到一条通知消息（附输出尾部）；期间可继续其他工作，不要 sleep 等待。`,
						},
					],
					details: { id, pid: child.pid, log: relLog },
				};
			} catch (e) {
				if (fd !== undefined) {
					try {
						fs.closeSync(fd);
					} catch {
						// 忽略
					}
				}
				return {
					content: [{ type: "text", text: `❌ 后台任务启动失败：${String(e)}` }],
					isError: true,
					details: { id },
				};
			}
		},
	});

	pi.registerTool({
		name: "background_list",
		label: "后台任务清单",
		description: "列出后台任务（运行中 / 已完成）及其日志路径。",
		parameters: Type.Object({}),
		async execute() {
			if (tasks.size === 0) {
				return { content: [{ type: "text", text: "（无后台任务记录）" }], details: {} };
			}
			const lines: string[] = [];
			for (const t of tasks.values()) {
				const state =
					t.status === "running"
						? `运行中（已 ${fmtDuration(Date.now() - t.startedAt)}）`
						: t.status === "killed"
							? "已停止"
							: t.status === "failed"
								? "启动失败"
								: `已结束（退出码 ${t.exitCode ?? "?"}）`;
				lines.push(
					`- ${t.id}${t.label ? `「${t.label}」` : ""} [${state}] ${t.command.slice(0, 100)} | 日志 ${path.relative(PROJECT_ROOT, t.logPath)}`,
				);
			}
			return { content: [{ type: "text", text: lines.join("\n") }], details: {} };
		},
	});

	pi.registerTool({
		name: "background_stop",
		label: "停止后台任务",
		description:
			"停止一个后台任务（等价 Claude Code 的 TaskStop）。停密采样任务前先确认已到停盯边界" +
			"（港股 12:00 / 16:00 或用户喊停），盘中擅自停密采样 = 违规。",
		parameters: Type.Object({
			id: Type.String({ description: "后台任务 id（如 bg-3；用 background_list 可查）" }),
		}),
		executionMode: "sequential",
		async execute(_toolCallId, params, _signal, _onUpdate, ctx) {
			const task = tasks.get(params.id);
			if (!task) {
				const known = tasks.size ? [...tasks.keys()].join(", ") : "（无）";
				return {
					content: [{ type: "text", text: `❌ 未找到任务 ${params.id}。当前任务：${known}` }],
					isError: true,
					details: {},
				};
			}
			// 密采样守卫提醒（对应 CC 的 TaskStop hook）：不阻断，只把提醒带回给 AI
			let reminder = "";
			try {
				const r = await runHook("monitor_guard.py", ["taskstop"], {
					hook_event_name: "PreToolUse",
					session_id: ctx.sessionManager.getSessionId(),
					tool_name: "TaskStop",
					tool_input: {},
				});
				reminder = additionalContext(r.stdout) || r.stderr.trim();
			} catch {
				// 守卫异常不影响停止动作
			}
			if (task.status === "running") {
				task.status = "killed";
				killGroup(task.pid, "SIGTERM");
				await new Promise((resolve) => setTimeout(resolve, 800));
				killGroup(task.pid, "SIGKILL"); // 兜底：SIGTERM 未及时退出的（如 python 卡 IO）
			}
			const text =
				`已停止 ${task.id}${task.label ? `「${task.label}」` : ""}（原状态：${task.status}）。` +
				(reminder ? `\n\n${reminder}` : "");
			return { content: [{ type: "text", text }], details: { id: task.id } };
		},
	});

	// 会话退出（Ctrl+C / /new / /resume 等）：停掉本会话遗留的后台任务，避免孤儿采样进程。
	pi.on("session_shutdown", async () => {
		shuttingDown = true;
		for (const t of tasks.values()) {
			if (t.status === "running") {
				t.status = "killed";
				killGroup(t.pid, "SIGTERM");
			}
		}
	});

	// 新会话（含 /new、/resume、/fork 换会话）：重置状态——退出标记不重置的话，
	// 换会话后新起任务的完成通知会被 forever 抑制；任务表清空（旧会话的任务已随 shutdown 停掉）。
	pi.on("session_start", async () => {
		shuttingDown = false;
		tasks.clear();
		counter = 0;
	});
}
