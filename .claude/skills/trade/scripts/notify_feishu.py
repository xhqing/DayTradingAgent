#!/usr/bin/env python3
"""停盯总结（业务版）→ 飞书推送（机器人私聊接收人）。

用途：每次停止盯盘后，把「停盯总结（业务版）」+「人为待办」推送到飞书（2026-10-13 用户立）。
内容纪律（用户原话要点）：飞书读者是普通用户、只在乎业务内容——只发业务（下了哪些单、
什么标的、赔率计算汇总、需要人为处理的事项）；后台进程、脚本、日志、采样、hook、缓存、
git 等技术运行细节一律不发。本脚本内置黑名单硬拦（命中即拒绝发送），规定本体见
SKILL.md「停盯总结 · 飞书汇报（业务版）」节。

配置：同目录上一级 feishu.local.json（本机文件、已被 .gitignore 忽略；样板 feishu.example.json）：
    {"user_id": "<FEISHU_USER_OPEN_ID>", "identity": "bot", "enabled": true}
  - user_id = 机器人私聊该用户（接收人本人）；也支持 "chat_id": "oc_xxx"（发到群，二选一）
  - identity 默认 bot；enabled=false 时本机跳过发送（仍退出 0，不报错）
  - 环境变量 FEISHU_CONFIG 可指定别的配置文件（测试用）

用法：
    python3 scripts/notify_feishu.py --file tmp/feishu_stop_2026-10-12.md --market us
    echo "……" | python3 scripts/notify_feishu.py --market hk
    python3 scripts/notify_feishu.py --file x.md --dry-run   # 只校验与预览，不发送

发送成功：写标记 tmp/feishu_sent_<market>_<交易日>.txt（preflight 凭此检查漏发），
并在 tmp/feishu_sent/ 留发送内容存档。退出码：0 成功 / 2 内容被过滤拦下 /
3 未配置或已停用 / 1 发送失败。

**消息最前面自动加 session_id（2026-10-21 用户立：跳代码追踪）**：默认在消息首行插入
`session_id: <完整会话 id>`（从 CLAUDE_CODE_SESSION_ID / PI_SESSION_ID 环境变量读），
方便用户看出这条投报是哪一条会话发的。加在内容过滤**之后**——否则「会话」二字
会被黑名单当成技术词拦下。拿不到 id 时自动跳过；确不需要时加 `--no-session-id`。

本机飞书域名必须直连（shell 挂全局代理时 NO_PROXY 不排除会连不上）——本脚本已内置
NO_PROXY（与 backup skill 的飞书端点同口径）。
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TRADE_DIR = os.path.dirname(SCRIPT_DIR)
PROJECT_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, "..", "..", "..", ".."))
CONFIG_PATH = os.environ.get("FEISHU_CONFIG") or os.path.join(TRADE_DIR, "feishu.local.json")
LARK_CLI = shutil.which("lark-cli") or os.path.expanduser("~/.local/bin/lark-cli")

# —— 内容过滤黑名单（用户 2026-10-13 立）——
# 中文词用子串匹配、ASCII 词用词边界匹配（避免 catalog 命中 log 这类包含误伤）。
# 覆盖「后台进程 / 运行机制 / 工程细节」类内容；业务词（订单、标的、赔率、盈亏、
# 待办、白名单、IP、API 等）不在此列——待办本身可能需要提技术动作，那是给人看的，
# 不属于「运行细节」。
CN_TOKENS = [
    "后台进程", "后台任务", "进程", "防睡眠", "心跳", "认领", "采样", "监控段",
    "钩子", "缓存", "仓库", "分支", "脚本", "日志", "代码", "报错", "异常栈",
    "调试", "代理", "断网", "断链", "会话", "密钥", "环境变量", "配置文件",
    "进程组", "本机",
]
EN_TOKENS = [
    "nohup", "tmux", "caffeinate", "claim", "watcher", "hook", "cache", "git",
    "commit", "repo", "log", "pid", "proxy", "debug", "session", "token", "csv",
    "json", "http", "ssh", "launchd", "cron", "python", "bash", "stderr",
    "stdout", "sid",
]
EN_PATTERNS = [(t, re.compile(r"\b" + re.escape(t) + r"\b", re.IGNORECASE)) for t in EN_TOKENS]


def filter_message(text):
    """返回命中列表 [(行号, 词, 行内容截断), ...]；空列表 = 通过。"""
    hits = []
    for i, line in enumerate(text.splitlines(), start=1):
        for tok in CN_TOKENS:
            if tok in line:
                hits.append((i, tok, line.strip()[:80]))
        for tok, pat in EN_PATTERNS:
            if pat.search(line):
                hits.append((i, tok, line.strip()[:80]))
    return hits


def current_session_id():
    """完整会话 id（2026-10-21 用户立：飞书投报首行带 session_id，方便看是哪条会话发的）。"""
    return (os.environ.get("CLAUDE_CODE_SESSION_ID") or os.environ.get("PI_SESSION_ID") or "").strip()


def with_session_id(text, enabled=True):
    """把 `session_id: <完整 id>` 插到消息最前面（不得在过滤前插——“会话”会被黑名单拦）。

    幂等：已经带 session_id 首行就不重复加。拿不到 id 时原样返回。
    """
    if not enabled:
        return text
    sid = current_session_id()
    if not sid:
        return text
    body = text.lstrip("\n")
    if body.startswith("session_id:") or body.startswith("session_id："):
        return body
    return f"session_id: {sid}\n\n{body}"


def load_config():
    if not os.path.isfile(CONFIG_PATH):
        return None
    try:
        with open(CONFIG_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception as exc:
        print(f"⛔ 飞书配置读取失败：{CONFIG_PATH}（{exc}）", file=sys.stderr)
        return None


def default_market():
    """未传 --market 时的推断：北京 17:00–次日 05:00 视为美股时段，其余港股（拿不准就显式传参）。"""
    hour = datetime.now().hour
    return "us" if (hour >= 17 or hour < 5) else "hk"


def trading_date(market):
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("America/New_York" if market == "us" else "Asia/Shanghai")
        return datetime.now().astimezone(tz).strftime("%Y-%m-%d")
    except Exception:
        return datetime.now().strftime("%Y-%m-%d")


def send_via_lark(cfg, message):
    """调 lark-cli 发送；返回 message_id；失败抛 RuntimeError。"""
    if not os.path.isfile(LARK_CLI):
        raise RuntimeError(f"找不到 lark-cli（{LARK_CLI}）——见 backup skill 的飞书端点说明")
    cmd = [LARK_CLI, "im", "+messages-send", "--as", str(cfg.get("identity") or "bot"), "--format", "json"]
    if cfg.get("chat_id"):
        cmd += ["--chat-id", cfg["chat_id"]]
    elif cfg.get("user_id"):
        cmd += ["--user-id", cfg["user_id"]]
    else:
        raise RuntimeError("配置里既没有 user_id 也没有 chat_id")
    cmd += ["--markdown", message]
    env = dict(os.environ)
    direct = "feishu.cn,.feishu.cn,larksuite.com,.larksuite.com,127.0.0.1,localhost"
    env["NO_PROXY"] = (env.get("NO_PROXY", "") + "," + direct).strip(",")
    env["no_proxy"] = (env.get("no_proxy", "") + "," + direct).strip(",")
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=60, env=env)
    except subprocess.TimeoutExpired:
        raise RuntimeError("lark-cli 超时（60s）")
    if res.returncode != 0:
        raise RuntimeError(f"lark-cli 退出码 {res.returncode}：{(res.stderr or res.stdout or '').strip()[:300]}")
    try:
        payload = json.loads(res.stdout)
    except Exception:
        raise RuntimeError(f"lark-cli 输出无法解析：{res.stdout[:300]}")
    if not payload.get("ok"):
        raise RuntimeError(f"飞书返回失败：{json.dumps(payload.get('error') or payload, ensure_ascii=False)[:300]}")
    return (payload.get("data") or {}).get("message_id") or ""


def main():
    ap = argparse.ArgumentParser(description="停盯总结（业务版）→ 飞书推送")
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--file", help="业务版总结文件（markdown）")
    src.add_argument("--text", help="直接给消息文本（短消息用）")
    ap.add_argument("--market", choices=["us", "hk"], help="市场（us 美股 / hk 港股；默认按当前时刻推断）")
    ap.add_argument("--date", help="交易日 YYYY-MM-DD（默认取该市场时区的今天；只用于发送标记）")
    ap.add_argument("--dry-run", action="store_true", help="只做过滤校验并打印预览，不发送")
    ap.add_argument("--no-session-id", action="store_true",
                    help="不在消息最前面加 session_id（默认会加，2026-10-21 用户立）")
    args = ap.parse_args()

    market = args.market or default_market()
    date_str = args.date or trading_date(market)

    if args.file:
        try:
            with open(args.file, encoding="utf-8") as fh:
                message = fh.read().strip()
        except Exception as exc:
            print(f"⛔ 读取文件失败：{args.file}（{exc}）", file=sys.stderr)
            return 1
    elif args.text:
        message = args.text.strip()
    else:
        message = sys.stdin.read().strip()

    if not message:
        print("⛔ 消息为空——没有任何内容可发", file=sys.stderr)
        return 1
    if len(message) > 20000:
        print(f"⛔ 消息过长（{len(message)} 字符 > 上限 20000）——请精简", file=sys.stderr)
        return 1

    # —— 内容过滤：飞书只发业务内容（命中即拒绝）——
    hits = filter_message(message)
    if hits:
        print(f"⛔ 内容被飞书过滤拦下：命中 {len(hits)} 处技术内容（飞书只发业务内容——订单 / "
              f"标的 / 赔率 / 盈亏 / 人为待办；规定见 SKILL.md「停盯总结 · 飞书汇报（业务版）」节）")
        for line_no, tok, ctx in hits[:10]:
            print(f"   第 {line_no} 行 命中「{tok}」：{ctx}")
        if len(hits) > 10:
            print(f"   ……其余 {len(hits) - 10} 处省略")
        print("   请改写为业务表述后重试。")
        return 2

    # —— 注入 session_id：[注意] 必须在内容过滤之后（否则“会话/session”会被黑名单拦下）——
    message = with_session_id(message, enabled=not args.no_session_id)
    if len(message) > 20000:
        print(f"⛔ 消息过长（{len(message)} 字符 > 上限 20000）——请精简", file=sys.stderr)
        return 1

    if args.dry_run:
        print(f"✅ 过滤校验通过（market={market}, date={date_str}, {len(message)} 字符）——以下为将发送内容：\n")
        print(message)
        print("\n（--dry-run：未发送）")
        return 0

    cfg = load_config()
    if cfg is None:
        print(f"⛔ 未配置飞书推送：{CONFIG_PATH} 不存在（样板见 feishu.example.json）"
              f"——按 README 配置后重试；确不使用可置 enabled=false 跳过", file=sys.stderr)
        return 3
    if cfg.get("enabled") is False:
        print("⏭️ 飞书推送已在本机停用（feishu.local.json enabled=false）——跳过")
        return 0

    try:
        message_id = send_via_lark(cfg, message)
    except Exception as exc:
        print(f"⛔ 飞书发送失败：{exc}", file=sys.stderr)
        return 1

    # 发送成功：落标记（preflight 凭此检查漏发）+ 内容存档
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    out_dir = os.path.join(PROJECT_ROOT, "tmp", "feishu_sent")
    os.makedirs(out_dir, exist_ok=True)
    marker = os.path.join(PROJECT_ROOT, "tmp", f"feishu_sent_{market}_{date_str}.txt")
    try:
        with open(marker, "w", encoding="utf-8") as fh:
            fh.write(f"{now}\t{market}\t{date_str}\t{message_id}\n")
        with open(os.path.join(out_dir, f"{market}_{date_str}.md"), "w", encoding="utf-8") as fh:
            fh.write(message)
    except Exception as exc:
        print(f"⚠️ 已发送成功，但标记 / 存档写入失败（{exc}）")

    print(f"✅ 飞书已发送（market={market}, date={date_str}, message_id={message_id or 'n/a'}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
