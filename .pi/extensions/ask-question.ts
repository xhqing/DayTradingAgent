/**
 * pi 宿主的 AskUserQuestion 工具（2026-09-29 立）。
 *
 * 为什么需要它：trade skill 里多处硬性要求「结构化提问」——切实盘授权、熔断重启确认
 * 等场景必须由用户显式点选选项（不许纯文本提问，防 AI 把含糊回复当默认同意）。
 * Claude Code / ZCode / CodeBuddy 自带 `AskUserQuestion` 工具；pi 没有内建问询工具
 * （README 明说 permission popups / built-in MCP 等一律不做，扩展自理），本扩展用
 * `ctx.ui.select / confirm` 提供同名工具，行为与 CC 对齐。
 *
 * 关键安全链：用户点选确认后，把「真实点击」合成成 CC 的 PostToolUse AskUserQuestion
 * payload（tool_response.answers 由此工具在进程内生成、AI 伪造不了），spawn
 * `live_auth_witness.py` 见证——实盘授权凭证（HMAC、密钥在 accounts.json 内 AI 读不到）
 * 照旧产出。即 pi 宿主同样要求「用户真的点了确认授权」，AI 无法凭对话文本绕过。
 */
import { Type } from "typebox";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { additionalContext, runHook } from "./lib/hooks.ts";

interface QuestionOption {
	label: string;
	description?: string;
}

interface QuestionSpec {
	question: string;
	header?: string;
	options: QuestionOption[];
	multiSelect?: boolean;
}

export default function askQuestion(pi: ExtensionAPI) {
	pi.registerTool({
		name: "AskUserQuestion",
		label: "结构化提问",
		description:
			"向用户发起结构化提问：每问给若干选项，用户必须显式点选（Esc 可取消）。" +
			"用于需要无歧义确认的场景（如实盘交易授权、熔断重启确认等安全动作）——这些场景" +
			"禁止用纯文本提问替代。AI 不能自答，用户取消 = 无答案。",
		parameters: Type.Object({
			questions: Type.Array(
				Type.Object({
					question: Type.String({ description: "问题全文（实盘授权类问题必须含打码账户号或 --account live、含「实盘」字样、含授权范围）" }),
					header: Type.Optional(Type.String({ description: "短标题（显示在对话框上方）" })),
					options: Type.Array(
						Type.Object({
							label: Type.String({ description: "选项文本（用户点选的就是它）" }),
							description: Type.Optional(Type.String({ description: "选项补充说明" })),
						}),
						{ description: "可选项，用户必须点选其一", minItems: 1 },
					),
					multiSelect: Type.Optional(Type.Boolean({ description: "是否多选（默认单选）" })),
				}),
				{ description: "要问的一组问题（按顺序逐个弹给用户）" },
			),
		}),
		executionMode: "sequential",
		promptGuidelines: [
			"需要用户做无歧义确认（如实盘授权、熔断重启）时用 AskUserQuestion 而不是纯文本提问。",
		],
		async execute(_toolCallId, params, _signal, _onUpdate, ctx) {
			const questions = params.questions as QuestionSpec[];
			if (!questions || questions.length === 0) {
				return { content: [{ type: "text", text: "❌ 未提供问题" }], isError: true, details: {} };
			}
			if (ctx.mode !== "tui") {
				return {
					content: [
						{
							type: "text",
							text:
								"❌ 当前不是交互模式，无法向用户提问、也拿不到用户点击。" +
								"不要在非交互模式执行需要用户授权的动作（如实盘下单）。",
						},
					],
					isError: true,
					details: {},
				};
			}

			const answers: Record<string, string> = {};
			let cancelled = false;

			for (const q of questions) {
				// 显示文本 → 原始 label 的映射（description 只用于显示，答案里回传纯 label）
				const displayToLabel = new Map<string, string>();
				const display = q.options.map((o) => {
					const d = o.description ? `${o.label} — ${o.description}` : o.label;
					displayToLabel.set(d, o.label);
					return d;
				});
				const title = q.header ? `${q.header}：${q.question}` : q.question;

				if (q.multiSelect) {
					const chosen: string[] = [];
					let remaining = [...display];
					while (remaining.length > 0) {
						const sel = await ctx.ui.select(`${title}（多选：逐项点选）`, remaining);
						if (sel === undefined) {
							cancelled = true;
							break;
						}
						chosen.push(displayToLabel.get(sel) ?? sel);
						remaining = remaining.filter((d) => d !== sel);
						if (remaining.length === 0) break;
						const more = await ctx.ui.confirm("继续选择？", "本题支持多选：继续选下一项，或结束本题");
						if (!more) break;
					}
					if (cancelled) {
						answers[q.question] = chosen.length ? `${chosen.join("、")}（未选完，用户中断）` : "（用户取消 / 未回答）";
						break;
					}
					answers[q.question] = chosen.length ? chosen.join("、") : "（用户取消 / 未回答）";
				} else {
					const sel = await ctx.ui.select(title, display);
					if (sel === undefined) {
						cancelled = true;
						answers[q.question] = "（用户取消 / 未回答）";
						break;
					}
					answers[q.question] = displayToLabel.get(sel) ?? sel;
				}
			}

			// 见证（live_auth_witness.py）：只在问题命中实盘授权特征且答案为确认词时写凭证；
			// 其余情况脚本自身静默放行。失败提示带回给 AI（不阻断继续对话）。
			let witnessNote = "";
			try {
				const r = await runHook("live_auth_witness.py", [], {
					hook_event_name: "PostToolUse",
					session_id: ctx.sessionManager.getSessionId(),
					tool_name: "AskUserQuestion",
					tool_input: { questions: questions.map((q) => ({ question: q.question, header: q.header, options: q.options })) },
					tool_response: { answers },
				});
				witnessNote = (additionalContext(r.stdout) || r.stderr.trim()).trim();
			} catch {
				// 见证异常不阻断问答
			}

			const lines: string[] = [];
			if (cancelled) lines.push("⚠️ 用户取消了提问（Esc / 未答完），未回答的问题按下述标记处理。");
			lines.push("用户回答：");
			for (const [q, a] of Object.entries(answers)) {
				lines.push(`Q: ${q}`);
				lines.push(`A: ${a}`);
			}
			if (witnessNote) lines.push("", witnessNote);

			return {
				content: [{ type: "text", text: lines.join("\n") }],
				details: { answers, cancelled },
			};
		},
	});
}
