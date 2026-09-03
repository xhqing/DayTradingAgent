#!/usr/bin/env python3
"""实盘授权见证 hook（2026-08-21 立，PostToolUse matcher AskUserQuestion）。

职责（实盘误开防护「会话级解锁 + 网络层物理拒单」方案的授权链第 ② 步）：
AskUserQuestion 真实点击发生且问题命中「实盘授权」特征、答案为确认授权时，
写短时凭证 live-auth.<session_id>.local（按会话分文件，2026-08-24 立——多会话
并行授权互不偷凭证；时间戳 + HMAC，密钥 = accounts.json 的
tiger.live_unlock_salt，AI 读不到原料）——随后 AI 跑
`python3 scripts/live_unlock.py verify-auth` 验凭证写解锁文件（凭证用后即焚，
一次点击只换一次解锁）。

为什么需要 hook 见证（设计核心）：AI 自己写文件时没有任何环节能验证「AskUserQuestion
真发生过且用户真点了授权」——对话文本里写「用户已授权」不算数（2026-08-18 教训：
靠 AI 记忆 / 自述的规矩全失守）。AskUserQuestion 的 tool_response.answers 由 harness
生成、AI 伪造不了；本 hook 只在 harness 真实投递该事件时运行，把「用户真点了授权」
这个事实变成机器可验证的 HMAC 凭证。

触发条件（三要素全中才写凭证，含糊提问不产生凭证）：
  1. 问题文本含「实盘」字样；
  2. 问题文本含实盘打码号特征（「67****91」或 mask_account 口径 `**`）或含「--account live」；
  3. 答案文本命中确认词（确认 / 授权 / 同意 / 开 / yes / ok / 准）。

凭证内容：{"ts": <秒级时间戳>, "hmac": HMAC-SHA256(salt, "ts|live_account"),
"session_id": <来源会话>}，写入 live-auth.<session_id>.local（按会话分文件）。
10 分钟过期、verify-auth 验完即删（live_unlock.AUTH_TTL_SECONDS / _burn_auth）。

安全属性：
  - salt 在 accounts.json（permissions.deny 挡 Read + secret_guard 挡 Bash 读），AI 上下文
    拿不到 → AI 无法自行伪造 HMAC；
  - hook 由 harness 投递事件触发，AI 无法命令本 hook 运行（只能真发 AskUserQuestion）；
  - 凭证只换一次解锁（用后即焚），重放无效；
  - hook 自身失败静默 exit 0（不该因自身故障卡死正常问答流程）。

实测依据（2026-08-21）：PostToolUse 对 AskUserQuestion 触发，payload 含
tool_response.answers（用户点选）与 session_id——已用临时 hook 写日志验证。
2026-09-02 双宿主适配：ZCode 的 AskUserQuestion tool_response 同构（questions 数组 +
answers「问题文本→答案文本」字典，源码实证 normalizeAskUserQuestionAnswers），零字段改动兼容。

用法（hooks 注册，2026-09-02 起双宿主：CC settings.json + ZCode .zcode/config.json 同挂）：
  PostToolUse matcher AskUserQuestion → python3 .claude/hooks/live_auth_witness.py
"""
import sys
import os
import re
import json
import time
import hmac
import hashlib

_HOOKS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.abspath(os.path.join(_HOOKS_DIR, "..", ".."))
_ACCOUNTS_PATH = os.path.join(_PROJECT_ROOT, ".claude", "skills", "trade", "accounts.json")
_TRADE_DIR = os.path.join(_PROJECT_ROOT, ".claude", "skills", "trade")


def _auth_path(session_id):
    """授权凭证按会话分文件（2026-08-24 立，多会话并行解锁互不偷凭证）：
    live-auth.<session_id>.local；无 sid（人工场景兜底）为 live-auth.manual.local。
    sid 只留 UUID 安全字符（字母数字与连字符），防异常值注入路径。"""
    safe = "".join(ch for ch in str(session_id or "").strip() if ch.isalnum() or ch == "-")
    return os.path.join(_TRADE_DIR, f"live-auth.{safe or 'manual'}.local")

# 答案确认词（用户点「确认授权」类选项；非确认答案不产生凭证）
_CONFIRM_WORDS = ("确认", "授权", "同意", "开", "yes", "ok", "准")


def _xml_question_answer_texts(text):
    """从 CodeBuddy ask_followup_question 的字符串形态 tool_response 提取问题与答案。

    CodeBuddy 的 tool_response 是 harness 生成的 XML 片段（question_answer 格式）：
      <question_answer><question_item id="x"><question>Q…</question><answers>A…</answers>
      </question_item>…</question_answer>
    与 CC / ZCode 的 dict（questions 数组 + answers 字典）结构不同，单独解析；解析失败
    返回空列表（调用方按「无答案」处理 → 不写凭证，保守方向安全）。
    """
    out = []
    for tag in ("question", "answers"):
        for m in re.finditer(r"<%s>(.*?)</%s>" % (tag, tag), text or "", re.S):
            frag = m.group(1)
            if "<" in frag:      # 内嵌子标签（富文本选项）→ 剥掉标签留文本
                frag = re.sub(r"<[^>]+>", " ", frag)
            frag = frag.strip()
            if frag:
                out.append(frag)
    return out


def _question_texts(payload):
    """从 tool_input / tool_response 提取全部问题与答案文本（两处都带，取并集更稳）。

    2026-09-03 T138：CodeBuddy 的 tool_response 是 XML 字符串（非 dict），字符串形态
    交给 _xml_question_answer_texts 单独解析；dict 形态走原逻辑。questions 本身若是
    JSON 字符串（CodeBuddy 允许传 JSON 字符串而非数组）先尝试还原成结构。"""
    texts = []
    for src in (payload.get("tool_input"), payload.get("tool_response")):
        if isinstance(src, str):
            texts.extend(_xml_question_answer_texts(src))
            continue
        if not isinstance(src, dict):
            continue
        qs = src.get("questions")
        if isinstance(qs, str):
            try:
                qs = json.loads(qs)
            except Exception:
                qs = None
        for q in qs or []:
            if isinstance(q, dict):
                texts.append(str(q.get("question", "")))
        ans = src.get("answers")
        if isinstance(ans, dict):
            texts.extend(str(v) for v in ans.values())
        elif isinstance(ans, str):
            texts.append(ans)
    # tool_response 的 answers 可能直接平铺（非 questions 内嵌），兜底再收一层
    tr = payload.get("tool_response")
    if isinstance(tr, dict) and isinstance(tr.get("answers"), dict):
        texts.extend(str(v) for v in tr["answers"].values())
    return texts


def _is_live_auth_question(texts):
    """问题是否命中实盘授权特征（含糊提问不产生凭证）。"""
    joined = " ".join(texts)
    if "实盘" not in joined:
        return False
    has_masked_acct = "**" in joined or "67****" in joined
    has_live_flag = "--account live" in joined or "account live" in joined
    return has_masked_acct or has_live_flag


def _has_confirm_answer(payload):
    """用户点选的答案是否为确认授权（读 tool_response.answers——harness 生成的真实点击）。

    2026-09-03 T138：CodeBuddy 的 tool_response 是 XML 字符串，字符串形态先解析出
    <answers> 文本再判确认词；解析不出 → 无答案 → 不写凭证（保守方向安全）。"""
    answers = []
    for src in (payload.get("tool_response"), payload.get("tool_input")):
        if isinstance(src, str):
            answers.extend(_xml_question_answer_texts(src))
            continue
        if isinstance(src, dict):
            ans = src.get("answers")
            if isinstance(ans, dict):
                answers.extend(str(v) for v in ans.values())
            elif isinstance(ans, str):
                answers.append(ans)
    joined = " ".join(answers).lower()
    return any(w in joined for w in _CONFIRM_WORDS)


def _write_witness(session_id):
    """写短时凭证（原料内部拼装，不输出）。返回 (ok, note)。"""
    try:
        with open(_ACCOUNTS_PATH) as f:
            tiger = (json.load(f) or {}).get("tiger", {})
    except Exception as e:
        return False, f"accounts.json 读取失败: {e}"
    salt = tiger.get("live_unlock_salt")
    live_account = tiger.get("account_live")
    if not salt or not live_account:
        return False, "缺 live_unlock_salt / account_live（先跑 scripts/live_unlock.py --init-salt）"
    ts = int(time.time())
    mac = hmac.new(str(salt).encode(), f"{ts}|{live_account}".encode(),
                   hashlib.sha256).hexdigest()
    payload = {"ts": ts, "hmac": mac, "session_id": session_id or ""}
    auth_path = _auth_path(session_id)
    with open(auth_path, "w") as f:
        json.dump(payload, f)
    os.chmod(auth_path, 0o600)
    return True, "witness_written"


def _emit_note(msg):
    """失败提示的双宿主输出（2026-09-02 ZCode hook 迁移立）：stderr 照旧（CC 原通道），
    另加 stdout JSON hookSpecificOutput——两宿主官方支持的注入通道（CC / ZCode 的
    PostToolUse schema 均含 hookSpecificOutput.additionalContext，CC 顶层 additionalContext
    非法键会被同步校验降级、不注入，故用标准形态；细节见 monitor_guard._emit_reminder）。"""
    sys.stderr.write(msg + "\n")
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": msg,
        }
    }, ensure_ascii=False))


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        sys.exit(0)   # 解析失败静默放行（hook 不卡正常流程）
    if payload.get("tool_name") not in ("AskUserQuestion", "ask_followup_question"):
        # 2026-09-03 T138：CodeBuddy IDE 宿主的同类工具名是 ask_followup_question
        # （CLI / CC / ZCode 是 AskUserQuestion），两个名字都认、其余单源兼容。
        sys.exit(0)
    try:
        texts = _question_texts(payload)
        if not _is_live_auth_question(texts):
            sys.exit(0)
        if not _has_confirm_answer(payload):
            sys.exit(0)   # 问题对但用户没点确认（拒绝 / Other 输入）→ 不写凭证
        ok, note = _write_witness(payload.get("session_id"))
        if not ok:
            _emit_note(f"⚠️ 实盘授权见证失败（不写凭证）: {note}")
        sys.exit(0)
    except Exception as e:
        _emit_note(f"⚠️ live_auth_witness hook 异常（忽略）: {e}")
        sys.exit(0)


if __name__ == "__main__":
    main()
