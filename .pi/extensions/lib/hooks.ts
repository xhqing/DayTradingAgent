/**
 * pi 宿主守卫桥公共库（2026-09-29 立）。
 *
 * 本文件不在 `.pi/extensions/` 直下一级，也不是 `某目录/index.ts`，故不会被 pi
 * 当作扩展自动加载——它只是给同目录上层的三个扩展（trade-guards / background-tasks /
 * ask-question）复用的内部工具库。
 *
 * 核心思路（为什么这么设计）：本项目所有护栏逻辑都写在 `.claude/hooks/*.py`
 * （唯一源，CC / ZCode / CodeBuddy 三宿主共用）。pi 端**不重写一套 TS 判定**，
 * 而是把 pi 的事件合成成 Claude Code 钩子协议的 JSON（tool_name / tool_input /
 * tool_response / session_id 字段同名），spawn 同一个 Python 脚本、读它的
 * exit code（2 = 阻断）与 stdout（hookSpecificOutput.additionalContext = 提醒文本）。
 * 这样规则永远只有一份，改 Python 脚本三宿主同步生效，不存在两端漂移。
 * 唯一代价 = 每次事件多一次 Python 进程启动（约 50ms），可接受。
 */
import { spawn } from "node:child_process";
import fs from "node:fs";
import path from "node:path";

declare const __dirname: string;

/** 从本文件位置 / 当前工作目录向上找项目根：判定标志 = 存在 `.claude/hooks/monitor_guard.py`。 */
function findProjectRoot(): string {
	const candidates: string[] = [];
	// jiti 以 CJS 形态加载时 __dirname 可用（.pi/extensions/lib → 上三级 = 项目根）；
	// 万一运行在不提供 __dirname 的环境（typeof 判定不会抛错），退到从 cwd 上找。
	if (typeof __dirname !== "undefined") {
		candidates.push(path.resolve(__dirname, "..", "..", ".."));
	}
	let cur = process.cwd();
	for (let i = 0; i < 8; i++) {
		candidates.push(cur);
		const parent = path.dirname(cur);
		if (parent === cur) break;
		cur = parent;
	}
	for (const c of candidates) {
		try {
			if (fs.existsSync(path.join(c, ".claude", "hooks", "monitor_guard.py"))) return c;
		} catch {
			// 忽略探测异常，继续试下一个候选
		}
	}
	return process.cwd();
}

export const PROJECT_ROOT = findProjectRoot();

export interface HookResult {
	code: number;
	stdout: string;
	stderr: string;
}

/**
 * 跑一个 `.claude/hooks/` 下的 Python 钩子：stdin 喂合成 payload、读 exit code 与输出。
 * exit 2 = 阻断（stderr 为给 AI 的拦截原因）；其它 code 交给调用方判断。
 * 脚本崩溃 / 超时按 code=-1 返回、**不阻断**（与 CC 端 hook fail-open 口径一致：
 * 守卫自身故障不该卡死交易流程）。
 */
export function runHook(
	script: string,
	args: string[],
	payload: unknown,
	timeoutMs = 15000,
): Promise<HookResult> {
	return new Promise((resolve) => {
		const scriptPath = path.join(PROJECT_ROOT, ".claude", "hooks", script);
		let child;
		try {
			child = spawn("python3", [scriptPath, ...args], {
				cwd: PROJECT_ROOT,
				stdio: ["pipe", "pipe", "pipe"],
			});
		} catch (e) {
			resolve({ code: -1, stdout: "", stderr: String(e) });
			return;
		}
		let stdout = "";
		let stderr = "";
		let settled = false;
		const finish = (code: number) => {
			if (settled) return;
			settled = true;
			resolve({ code, stdout, stderr });
		};
		const timer = setTimeout(() => {
			try {
				child.kill("SIGKILL");
			} catch {
				// 进程可能已退出
			}
			finish(-1);
		}, timeoutMs);
		child.stdout?.on("data", (d) => {
			stdout += d.toString();
		});
		child.stderr?.on("data", (d) => {
			stderr += d.toString();
		});
		child.on("error", () => {
			clearTimeout(timer);
			finish(-1);
		});
		child.on("close", (code) => {
			clearTimeout(timer);
			finish(code ?? -1);
		});
		child.stdin?.on("error", () => {
			// 脚本可能不读 stdin 就退出；忽略 EPIPE
		});
		try {
			child.stdin?.write(JSON.stringify(payload));
			child.stdin?.end();
		} catch {
			// 写失败按脚本没收到 payload 处理（脚本自身会走解析失败放行分支）
		}
	});
}

/** 从钩子 stdout 提取 hookSpecificOutput.additionalContext（提醒文本）；无则返回空串。 */
export function additionalContext(stdout: string): string {
	const trimmed = (stdout || "").trim();
	if (!trimmed) return "";
	for (const line of trimmed.split("\n").reverse()) {
		const t = line.trim();
		if (!t.startsWith("{")) continue;
		try {
			const data = JSON.parse(t);
			const note = data?.hookSpecificOutput?.additionalContext;
			if (typeof note === "string" && note.trim()) return note;
		} catch {
			// 该行不是合法 JSON，继续往前找
		}
	}
	return "";
}
