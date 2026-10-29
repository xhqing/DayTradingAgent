#!/usr/bin/env python3
"""停盯收尾标记的「手工补写」工具（2026-10-29 立）。

## 为什么需要这个工具

`stop_summary.py` 正常跑通时会自己写收尾标记 `tmp/stop_done_<market>_<date>_<sid8>.txt`。
但它跑失败时（典型：代理出口 IP 漂出老虎白名单，券商 API 全断）只能改由 AI 依
`actions/` 记录手工汇总，手工写标记就容易漏掉文件名里的**会话标识后缀 `_<sid8>`**。

漏了会怎样：`monitor_guard._stop_done_reminder()` 在「本会话在该交易日有交易记录」时
只认带本会话 sid8 的标记（多会话隔离设计），于是永远失配、**每回合反复提醒「停盯收尾缺失」**。
2026-10-28 ET 那次实测就是这样：收尾其实早做完了（飞书已发、券商侧复核通过），但因为
标记名写成 `stop_done_us_2026-10-28.txt`（缺 `_01a182ba`），误报跨越了多天。

本工具把「取会话标识 + 拼文件名 + 写规定格式」机械化，杜绝再漏。

## 用法

    python3 .claude/skills/trade/scripts/mark_stop_done.py \\
        --market us --date 2026-10-28 --trades 6 --net -393.60 \\
        --note "stop_summary.py 因白名单拒失败，改手工汇总；飞书已发 message_id=om_xxx；账户空仓"

可选参数：

    --feishu-id <message_id>   飞书业务版已发送的消息 id（有则记入标记，证明推送已发生）
    --session <sid>            显式指定会话 id（默认自动取 PI_SESSION_ID / CLAUDE_CODE_SESSION_ID）
    --no-feishu-confirmed      明确标记「飞书未发送」（便于事后追溯）

拿不到会话标识时**不写标记**、直接报错退出——因为写出来的标记检查器认不了，
写了等于没写（不如当场暴露问题）。
"""

import argparse
import json
import os
import sys
from datetime import datetime

BJ_FMT = "%Y-%m-%d %H:%M:%S"


def project_root():
    """项目根 = .claude/skills/trade/scripts 上溯四层（scripts→trade→skills→.claude→根）。

    与 stop_summary.project_root() 同一口径；少写一层会落到 .claude/ 下，标记就写错地方。
    """
    return os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                        "..", "..", "..", ".."))


def resolve_sid8(explicit=None):
    """取会话标识前 8 位（与 stop_summary.py / monitor_guard.py 同一个口径）。"""
    sid = explicit or os.environ.get("CLAUDE_CODE_SESSION_ID") or os.environ.get("PI_SESSION_ID") or ""
    return sid[:8] if sid else ""


def main(argv=None):
    ap = argparse.ArgumentParser(description="手工补写停盯收尾标记（自动带会话标识，防 monitor_guard 误报）")
    ap.add_argument("--market", required=True, choices=["us", "hk"], help="市场")
    ap.add_argument("--date", required=True, help="交易日日期（美股用 ET 日期、港股用北京日期），YYYY-MM-DD")
    ap.add_argument("--trades", type=int, default=None, help="当日成交笔数")
    ap.add_argument("--net", type=float, default=None, help="当日净盈亏（券商口径优先）")
    ap.add_argument("--note", default="", help="降级原因 / 汇总口径 / 关键结论（写清楚，后人要能看懂）")
    ap.add_argument("--feishu-id", default=None, help="飞书业务版已发送的 message_id")
    ap.add_argument("--session", default=None, help="显式指定会话 id（默认自动读环境变量）")
    ap.add_argument("--no-feishu-confirmed", action="store_true", help="明确记录「飞书未发送」")
    a = ap.parse_args(argv)

    sid8 = resolve_sid8(a.session)
    if not sid8:
        print("❌ 拿不到会话标识（PI_SESSION_ID / CLAUDE_CODE_SESSION_ID 都为空）。", file=sys.stderr)
        print("   请用 --session <会话id> 显式指定，否则写出的标记 monitor_guard 认不了、等于没写。",
              file=sys.stderr)
        return 2

    root = project_root()
    tmp_dir = os.path.join(root, "tmp")
    os.makedirs(tmp_dir, exist_ok=True)
    marker = os.path.join(tmp_dir, f"stop_done_{a.market}_{a.date}_{sid8}.txt")

    payload = {
        "market": a.market,
        "et_date": a.date,
        "session": sid8,
        "trades": a.trades,
        "net_pnl": a.net,
        "feishu_message_id": a.feishu_id,
        "feishu_sent": bool(a.feishu_id) and not a.no_feishu_confirmed,
        "manual_marker": True,          # 与 stop_summary.py 自动写的标记区分
        "note": a.note,
        "written_at_bj": datetime.now().strftime(BJ_FMT),
    }
    with open(marker, "w", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False, indent=1))

    print(f"✅ 收尾标记已写 {marker}")
    if not payload["feishu_sent"]:
        print("⚠️ 标记里记录了「飞书未发送」——若实际已发，请补 --feishu-id 重写一次。")

    # 自检：立刻用 monitor_guard 的同一判据复核，确认不再提醒
    acts = os.path.join(root, "actions", f"{a.date}-ET-actions.md" if a.market == "us"
                        else f"{a.date}-HKT-actions.md")
    if os.path.exists(acts):
        try:
            txt = open(acts, encoding="utf-8").read()
            if f"🆔 会话：{sid8}" in txt:
                print(f"ℹ️ actions 文件里有本会话（{sid8}）记录，本标记会被 monitor_guard 认作已收尾")
            else:
                print(f"ℹ️ actions 文件里没有本会话（{sid8}）标识 —— monitor_guard 会按「任意同日标记」宽松匹配")
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
