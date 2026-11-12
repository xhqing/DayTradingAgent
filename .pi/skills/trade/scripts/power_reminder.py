#!/usr/bin/env python3
"""插电源提醒 · 定时发送（独立于任何盯盘会话）。

2026-11-12 用户立：原实现是 `preflight.py` 每次盯盘启动都发一条飞书「插电源」提醒，
多会话并行盯盘时同一条消息会重复发好几遍。现在改成**每天美东时间 09:30 由 launchd
定时发一条**，发送次数只跟日期有关、跟开了几个盯盘会话完全无关。

为什么定时器部署在 macmini，而不是用户正在用的 MacBook：
本机合盖且没接电源时会休眠，macOS 的 launchd 定时任务在休眠期间不会触发；而「忘了插电源」
恰恰是最需要收到这条提醒的场景。macmini 是常开的算力机，且飞书命令行工具与接收人配置
都在它上面，所以由它来发最稳。

时间口径（项目 2026-11-05 立规，一律以美东时间为权威）：
脚本内部用 `zoneinfo.ZoneInfo("America/New_York")` 取当前美东时间，自动处理夏令时与冬令时，
**不硬编码「北京减 12 / 减 13 小时」**。plist 用「每 60 秒巡检一次」而不是
`StartCalendarInterval`，也是因为后者按系统本地时区排程，冬夏令一切换就会错位。

触发与去重：
  - 正常窗口：美东 09:30:00–09:35:00。launchd 每 60 秒调一次本脚本，最迟 09:30:59 发出。
  - 补发窗口：美东 09:35:00–12:00:00。定时任务因故错过（含休眠期间没跑）时，
    当天中午前仍会补上一条；过了 12:00 就不再补，避免晚上开机收到一条过时的提醒。
  - 每个美东日期只发一次：发送成功后写按日期命名的标记文件，并用文件锁防同机并发。
    标记放在**本机与 macmini 同步的私有状态目录**（`~/.dev/private/trading-agent/state/`），
    这样 macmini 发完、本机 preflight 的 `--status` 也能看到同一份记录，不会误报「今天没发」；
    没有这个私有目录的机器（例如单独 clone 出来用）自动回退到项目根 `state/`。
    可用环境变量 `POWER_REMINDER_STATE_DIR` 指定别的位置（测试 / 特殊部署用）。
  - 周末静默。已知边界：**美国节假日不排除**——脚本不依赖富途 OpenD，无法查交易日历；
    一年约九个节假日会多发一条提醒，代价可以接受。港股场次（香港时间 09:30 开盘）
    不再有插电源提醒，这是本次按用户指定「只留美东 09:30 一条」带来的边界，见 CHANGELOG。

发送走同目录 `notify_feishu.py`（`--no-mark` 不污染停盯总结的漏发标记与存档，
`--no-session-id` 因为定时通知不属于任何盯盘会话），不另写一套飞书发送逻辑。
飞书的 open_id 是「应用维度」的，部署机上的飞书命令行工具可能绑定另一个应用，
所以发送前会自动按本机绑定的应用校正接收人（见 `feishu_config_override()`）。

用法：
    python3 power_reminder.py                           # 定时巡检（launchd 调用）
    python3 power_reminder.py --status                  # 打印当日状态，供 preflight 在场提示
    python3 power_reminder.py --dry-run                 # 只判断、只做飞书内容过滤校验，不发送
    python3 power_reminder.py --force                  # 忽略窗口 / 周末 / 标记，立即实发一条
    python3 power_reminder.py --now "2026-11-13 09:31:00" --dry-run   # 指定美东时间做测试
"""

import argparse
import fcntl
import json
import os
import re
import socket
import subprocess
import sys
from datetime import datetime, time
from zoneinfo import ZoneInfo

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
# scripts/ → .pi/skills/trade → .pi/skills → .pi → 项目根
PROJECT_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, "..", "..", "..", ".."))
NOTIFY = os.path.join(SCRIPT_DIR, "notify_feishu.py")

# 共享私有状态目录：本机与 macmini 之间同步（项目「私有内容目录」机制）。
PRIVATE_STATE_DIR = os.path.expanduser("~/.dev/private/trading-agent/state")
FALLBACK_STATE_DIR = os.path.join(PROJECT_ROOT, "state")

ET = ZoneInfo("America/New_York")
START = time(9, 30)        # 正常准点窗口起点
NORMAL_END = time(9, 35)   # 正常准点窗口终点
CATCHUP_END = time(12, 0)  # 当天补发窗口终点（之后不再补）

MESSAGE = (
    "🔌 **每日插电源提醒（美东 09:30）：电源线两头都要插牢**\n\n"
    "① 插座 / 排插那一端\n"
    "② MacBook 那一端\n\n"
    "上次就是因为 MacBook 这一端忘了插、靠电池供电，合盖后电脑进入休眠，"
    "盯盘中断近 4 小时，收尾差点漏掉。\n\n"
    "插好电源后合盖也不会休眠，盯盘才能连续跑完整个交易时段。"
)


def state_dir():
    """返回发送标记所在目录；先认环境变量，其次用同步的私有目录，最后回退项目 state/。"""
    override = os.environ.get("POWER_REMINDER_STATE_DIR")
    if override:
        return override
    private_root = os.path.dirname(PRIVATE_STATE_DIR)
    if os.path.isdir(private_root):
        return PRIVATE_STATE_DIR
    return FALLBACK_STATE_DIR


def state_path(et_date):
    return os.path.join(state_dir(), f"power_reminder_sent_{et_date}.json")


def lock_path():
    # 固定名字、不按日期，避免每天多留一个锁文件。
    return os.path.join(state_dir(), "power_reminder.lock")


def now_et(explicit=None):
    """取当前美东时间；`explicit` 给 'YYYY-MM-DD HH:MM:SS' 时按美东本地时间解释（测试用）。"""
    if explicit:
        naive = datetime.strptime(explicit, "%Y-%m-%d %H:%M:%S")
        return naive.replace(tzinfo=ET)
    return datetime.now(ET)


def read_state(et_date):
    try:
        with open(state_path(et_date), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def write_state(et_date, window, sent_at_et, output):
    os.makedirs(state_dir(), exist_ok=True)
    match = re.search(r"message_id=([^；)]+)", output or "")
    payload = {
        "et_date": et_date,
        "window": window,
        "sent_at_et": sent_at_et,
        "message_id": match.group(1).strip() if match else "",
    }
    # 先写临时文件再改名，避免两次触发同时写坏标记文件。
    tmp = state_path(et_date) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(tmp, state_path(et_date))


def status_text(now):
    """给 preflight 在场打印用：只陈述事实与提醒，不发任何消息。"""
    et_date = now.strftime("%Y-%m-%d")
    state = read_state(et_date)
    if state:
        return (f"🔌 电源提醒：今日（美东 {et_date}）已由定时任务发送"
                f"（{state.get('sent_at_et', '时间未记')}，{state.get('window', '定时')}）——"
                f"不由盯盘会话重复发送")
    if now.weekday() >= 5:
        return "🔌 电源提醒：今天是周末，定时任务静默（美东工作日 09:30 才发）"
    clock = now.time()
    if clock < START:
        return "🔌 电源提醒：由定时任务于美东 09:30 统一发送（不由盯盘会话发送，避免多会话重复）"
    if clock < CATCHUP_END:
        return ("🔌 电源提醒：今日尚未发送，定时任务会在美东 12:00 前补发——"
                "请先自行确认电源线两头都已插牢")
    return ("⚠️ 电源提醒：美东 09:30 已过、本机没看到今日发送记录——"
            "请确认 macmini 上的定时任务仍在运行，并自行确认电源线两头都已插牢")


def feishu_config_override():
    """按本机 lark-cli 绑定的飞书应用校正接收人；不需要校正时返回 None。

    为什么（2026-11-12 实发验证）：飞书的 open_id 是**应用维度**的，同一个人在不同应用下
    open_id 不同。项目里同步的 `feishu.local.json` 写的是 MacBook 上 lark-cli 绑定应用下的
    open_id；macmini 上的 lark-cli 绑定的是另一个应用，直接拿那个 open_id 发会被飞书拒
    （错误码 99992361，提示去看「如何获取 open_id」文档）。所以定时任务在部署机发送前，
    读 lark-cli 本机配置里当前应用已绑定用户的 open_id，与项目配置不一致时生成一份只含
    接收人的小配置，通过 FEISHU_CONFIG 环境变量传给 notify_feishu.py。

    边界：只读 `~/.lark-cli/config.json` 的 `apps[].users[].userOpenId`，不读、不打印、
    不落盘 appSecret 等凭证；配置不存在 / 格式变化 / 没有绑定用户 / 与项目配置一致时，
    一律返回 None、回退到项目默认的 feishu.local.json（不阻断发送）。这个字段格式是
    本机实测（非 lark-cli 的公开承诺），升级后若变了会自动回退，届时按日志复核即可。
    """
    cli_path = os.path.expanduser("~/.lark-cli/config.json")
    base_path = os.path.join(SCRIPT_DIR, "..", "feishu.local.json")
    try:
        with open(cli_path, encoding="utf-8") as fh:
            cli = json.load(fh)
        with open(base_path, encoding="utf-8") as fh:
            base = json.load(fh)
        local_uid = ""
        for app in cli.get("apps") or []:
            for user in (app.get("users") or []):
                if isinstance(user, dict) and user.get("userOpenId"):
                    local_uid = str(user["userOpenId"])
                    break
            if local_uid:
                break
        if not local_uid or local_uid == str(base.get("user_id") or ""):
            return None
        host = socket.gethostname().split(".")[0] or "local"
        out_dir = state_dir()
        os.makedirs(out_dir, exist_ok=True)
        out_path = os.path.join(out_dir, f"feishu_power_reminder_{host}.json")
        payload = {"user_id": local_uid,
                   "identity": base.get("identity") or "bot",
                   "enabled": True}
        tmp = out_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp, out_path)
        return out_path
    except Exception:
        return None


def send(et_date, window, sent_at_et, dry_run=False):
    """调 notify_feishu.py 发一条；返回 (是否成功, 输出文本)。"""
    cmd = [sys.executable, NOTIFY, "--text", MESSAGE, "--market", "us",
           "--date", et_date, "--no-mark", "--no-session-id"]
    if dry_run:
        cmd.append("--dry-run")
    if not os.path.isfile(NOTIFY):
        return False, f"⛔ 找不到飞书发送脚本：{NOTIFY}"
    env = dict(os.environ)
    override = feishu_config_override()
    if override:
        env["FEISHU_CONFIG"] = override
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=90, env=env)
    except Exception as exc:
        return False, f"⛔ 调用飞书发送脚本异常：{exc}"
    output = (res.stdout or "") + (res.stderr or "")
    if override:
        output = (f"[power-reminder] 已按本机飞书应用校正接收人"
                  f"（{os.path.basename(override)}）\n" + output)
    if res.returncode != 0:
        return False, f"⛔ 飞书发送脚本退出码 {res.returncode}：{output.strip()[:300]}"
    if not dry_run:
        # notify_feishu.py 在「飞书已在本机停用」时也返回 0，但并没有真的发出消息。
        # 这种情形不能写「当日已发」标记，否则当天就静默漏发了；也不重试（重试同样发不出去）。
        if "飞书已发送" not in output:
            return True, f"⚠️ 飞书脚本返回成功但未见发送确认（可能已在本机停用）：{output.strip()[:200]}"
        try:
            write_state(et_date, window, sent_at_et, output)
        except Exception as exc:
            # 消息已经发出去了，标记写失败不能再重发，否则用户会收到两条。
            return True, f"✅ 飞书已发送，但标记写入失败（{exc}）；输出：{output.strip()[:200]}"
    return True, output


def main():
    ap = argparse.ArgumentParser(description="插电源提醒 · 美东 09:30 定时发送")
    ap.add_argument("--now", help="指定美东本地时间 'YYYY-MM-DD HH:MM:SS'（测试用）")
    ap.add_argument("--status", action="store_true", help="只打印当日状态（供 preflight 调用）")
    ap.add_argument("--dry-run", action="store_true", help="只判断并做内容过滤校验，不发送、不写标记")
    ap.add_argument("--force", action="store_true", help="忽略窗口 / 周末 / 当日标记，立即实发一条")
    args = ap.parse_args()

    try:
        now = now_et(args.now)
    except ValueError:
        print("⛔ --now 格式应为 'YYYY-MM-DD HH:MM:SS'（美东本地时间）", file=sys.stderr)
        return 2
    et_date = now.strftime("%Y-%m-%d")
    sent_at_et = now.strftime("%Y-%m-%d %H:%M:%S ET")

    if args.status:
        print(status_text(now))
        return 0

    # 文件锁：同一台机器上若有多个实例同时被唤起，只允许一个进到发送判断里。
    os.makedirs(state_dir(), exist_ok=True)
    try:
        lock_file = open(lock_path(), "a+")
    except OSError as exc:
        print(f"⛔ 打不开去重锁文件：{exc}", file=sys.stderr)
        return 1
    with lock_file:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0  # 另一个实例正在处理，本次直接退出

        # 拿到锁之后重读标记：可能另一个实例刚刚发完并写好了记录。
        state = read_state(et_date)
        if args.force:
            window = "手工强制"
        else:
            if state:
                return 0  # 当天已发——这是「多会话并行不重复」的核心去重
            if now.weekday() >= 5:
                return 0
            clock = now.time()
            if not (START <= clock < CATCHUP_END):
                return 0
            window = "美东 09:30 准点" if clock < NORMAL_END else "美东 12:00 前补发"

        ok, output = send(et_date, window, sent_at_et, dry_run=args.dry_run)
        print(f"[power-reminder] {et_date} {window}：{output.strip()[:400]}")
        return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
