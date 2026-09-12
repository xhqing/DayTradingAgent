#!/usr/bin/env python3
"""实盘熔断机制（2026-09-12 用户立项：复盘后给出暂停 / 重启实盘交易的建议）。

为什么需要熔断：两级判据（review-and-evaluation.md「实盘熔断机制」节）的第二级
「暂停修整线」在 2026-08-17 立定时只有复盘报告的文本建议——无工具强制、无状态
持久化，AI 给完建议下次盯盘照样能切实盘开仓（散文规定会衰减，2026-08-18 早盘
三起漏执行已实证）。本脚本把第二级判据落成机制：熔断状态持久化到
tmp/circuit_breaker.json，open_position 脚本对 --account live 前置检查熔断状态、
tripped 即拒单（blocked_by: circuit_breaker）——与 live_unlock（授权闸）、
losing_streak（一级降频闸）并列的第三道开仓前置闸。

方向不对称（与 live_unlock「上锁自动、解锁人工」同哲学）：
  - trip（置熔断）= 保护动作、fail-safe 方向 → 复盘触发第二级判据时 AI 直接写，
    无需用户确认（连败闸 T131 同理：保护动作自动化）；
  - reset（解除熔断）= 放开风险的动作 → 钥匙在用户手里：复盘给出重启建议 +
    AskUserQuestion 用户点「确认重启实盘」后，AI 才能带 --confirmed-by-user 跑
    reset；脚本对不带该参数的 reset 直接拒绝执行（AI 忘了问用户时参数这道仪式
    性闸会拦住）。

作用域（关键边界）：
  - 只拦**实盘开仓**（--account live 的 open_position_*）。平仓 / 移损 / 移盈是
    风控动作，任何时候都必须可用（持仓在、熔断中也要能平掉——只拦进攻、不拦撤退）；
  - 模拟盘 / 信号模式**不受熔断限制**——熔断期恰是模拟盘验证场：重启判据的数据
    来源就是熔断期间的模拟盘 / 信号新样本（模拟盘跑修整、达标后重启实盘）。

判据与参数：触发 = 第二级暂停修整线（P(g>0)@f 跌破 80% 且最近连续 5 笔无盈利，
2026-08-17 蒙特卡洛标定）；重启判据见 review-and-evaluation.md「实盘熔断机制」节。
参数（enabled / pg_pos_floor / recent_winless_required / resume_min_trades）从
config.json risk.circuit_breaker 读，调参改配置不改代码。

用法：
  python3 circuit_breaker.py status                        # 查看当前状态 + 历史
  python3 circuit_breaker.py trip --reason "P(g>0)=76%<80% 且近5笔无盈利（复盘 2026-09-12）"
                                                          # 置熔断（复盘触发时 AI 跑）
  python3 circuit_breaker.py reset --confirmed-by-user "用户已 AskUserQuestion 确认重启实盘（复盘 2026-09-12 建议重启）"
                                                          # 解除熔断（须用户确认后）

状态文件 tmp/circuit_breaker.json（本机运行时数据，tmp/ 已 gitignore）：
  {"status": "ok"|"tripped", "updated_at": "...", "tripped_at": "...",
   "reason": "...", "evidence": {...}, "history": [{"at","event","note"}, ...]}
"""

import json
import sys
from datetime import datetime
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))

STATE_FILE = "circuit_breaker.json"
HISTORY_KEEP = 20   # 历史最多保留条数（防止无限增长）


def state_path():
    """状态文件路径：scripts/ 的上四级 = 项目根（scripts → trade → skills → .claude → 项目根）。"""
    return SCRIPTS_DIR.parent.parent.parent.parent / "tmp" / STATE_FILE


def load_config_section():
    """读 config.json risk.circuit_breaker 参数节（缺省回退到内置默认值）。

    返回 dict；config 缺失 / 无该节 / 解析异常时返回默认值并附 _warning（不抛出——
    闸的可用性优先于参数精确性，默认参数就是判据文档的口径）。
    """
    import copy
    default = {
        "enabled": True,
        "pg_pos_floor": 0.80,          # 第二级第一条件：P(g>0) 跌破此线
        "recent_winless_required": 5,  # 第二级第二条件：最近连续 N 笔无盈利
        "resume_min_trades": 10,       # 重启判据：修整后模拟盘/信号新样本最少笔数
    }
    out = copy.deepcopy(default)
    try:
        cfg = json.load(open(SCRIPTS_DIR.parent / "config.json"))
        sec = (cfg.get("risk") or {}).get("circuit_breaker") or {}
        for k in default:
            if k in sec:
                out[k] = sec[k]
        out["_warning"] = None
    except Exception as e:
        out["_warning"] = f"config.json 读取失败（{e}），用默认参数"
    return out


def read_state():
    """读熔断状态文件。文件不存在 / 损坏 → 视为从未熔断（status=ok）。

    熔断器哲学：默认闭合（可交易），触发才断开——文件缺失 = 从未熔断 = 放行。
    损坏时同样按 ok 处理并附警告（熔断状态是复盘写入的静态判定、非实时计算，
    损坏属极端情形；复盘核对段会重建提示）。
    """
    try:
        st = json.load(open(state_path()))
        if st.get("status") in ("ok", "tripped"):
            return st
        return {"status": "ok", "_warning": f"状态字段非法: {st.get('status')!r}，按 ok 处理"}
    except FileNotFoundError:
        return {"status": "ok"}
    except Exception as e:
        return {"status": "ok", "_warning": f"状态文件损坏（{e}），按 ok 处理——建议复盘核对后重写"}


def _write_state(new_state):
    state_path().parent.mkdir(parents=True, exist_ok=True)
    json.dump(new_state, open(state_path(), "w"), ensure_ascii=False, indent=1)


def trip(reason, evidence=None):
    """置熔断。保护方向：任何调用方（AI 复盘 / 用户手动）均可执行。"""
    st = read_state()
    if st.get("status") == "tripped":
        print(json.dumps({"ok": True, "already": True, "status": "tripped",
                          "tripped_at": st.get("tripped_at"), "reason": st.get("reason")},
                         ensure_ascii=False))
        return
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    hist = list(st.get("history") or [])
    hist.append({"at": now, "event": "trip", "note": reason[:200]})
    new_state = {
        "status": "tripped",
        "updated_at": now,
        "tripped_at": now,
        "reason": reason,
        "evidence": evidence or {},
        "history": hist[-HISTORY_KEEP:],
    }
    _write_state(new_state)
    print(json.dumps({"ok": True, "status": "tripped", "tripped_at": now,
                      "action": "实盘开仓将被拒（blocked_by: circuit_breaker）；"
                                "模拟盘/信号不受限（重启验证场）。解除走复盘重启流程"
                                "（reset --confirmed-by-user）"}, ensure_ascii=False))


def reset(confirm_text):
    """解除熔断。须带 --confirmed-by-user（用户 AskUserQuestion 确认后的留痕文本）。"""
    st = read_state()
    if st.get("status") == "ok":
        print(json.dumps({"ok": True, "already": True, "status": "ok"}, ensure_ascii=False))
        return
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    hist = list(st.get("history") or [])
    hist.append({"at": now, "event": "reset", "note": confirm_text[:200]})
    _write_state({"status": "ok", "updated_at": now,
                  "last_tripped": {"tripped_at": st.get("tripped_at"),
                                   "reason": st.get("reason"),
                                   "resumed_at": now},
                  "history": hist[-HISTORY_KEEP:]})
    print(json.dumps({"ok": True, "status": "ok", "resumed_at": now,
                      "action": "实盘开仓恢复放行（实盘下单仍需 live 授权解锁等既有闸）"},
                     ensure_ascii=False))


def check_gate(account, script_name):
    """开仓脚本前置闸（第三道：熔断闸，2026-09-12 立）。

    与 live_gate_for_order_scripts（授权闸）、check_losing_streak_gate（一级降频闸）
    并列，在 open_position_tiger(_us).py 解析完 --account 后调用：
    account == 'live' 且熔断状态 tripped → 打印 {ok:false, blocked_by:"circuit_breaker"}
    结构化拒单并 sys.exit(1)。account 非 live 直接返回（模拟盘 = 熔断期验证场）。

    无 --force 旁路（熔断是第二级判据、高于第一级连败闸的当日降频）：解除的唯一通道
    是 reset 流程（复盘重启判据 + 用户确认）。用户若坚持盘中强开，须先跑 reset。
    enabled=false（config 开关）时整闸跳过（供用户显式关闭机制用）。
    """
    if account != "live":
        return
    cfg = load_config_section()
    if not cfg.get("enabled", True):
        return
    st = read_state()
    if st.get("status") != "tripped":
        return
    ev = st.get("evidence") or {}
    print(json.dumps({
        "ok": False,
        "blocked_by": "circuit_breaker",
        "action": script_name,
        "tripped_at": st.get("tripped_at"),
        "reason": st.get("reason"),
        "evidence": {k: ev.get(k) for k in ("pg_pos", "f", "n_trades", "review") if ev.get(k) is not None},
        "error": (
            f"⛔ 实盘熔断中（{st.get('tripped_at')} 触发：{st.get('reason')}）——"
            f"第二级暂停修整线已触发，实盘开仓一律拒绝。"
            f"处置：① 平仓/移损/移盈不受限（风控动作照常，只拦进攻）；"
            f"② 模拟盘/信号照常（熔断期 = 模拟盘修整验证场，重启判据数据源）；"
            f"③ 解除唯一通道 = 复盘重启流程：重启判据达标（修整结论已出 + 模拟盘新样本"
            f" ≥{cfg.get('resume_min_trades', 10)} 笔且段内 EV>0、无 3 连败）→ AskUserQuestion"
            f" 用户确认 → python3 scripts/circuit_breaker.py reset --confirmed-by-user \"...\"。"
            f"本闸无 --force 旁路。"),
    }, ensure_ascii=False))
    sys.exit(1)


def print_status():
    st = read_state()
    cfg = load_config_section()
    print(json.dumps({"state": st, "params": {k: cfg[k] for k in
                                              ("enabled", "pg_pos_floor",
                                               "recent_winless_required", "resume_min_trades")}},
                     ensure_ascii=False, indent=1))
    if st.get("_warning"):
        print(f"⚠️ {st['_warning']}")
    if cfg.get("_warning"):
        print(f"⚠️ {cfg['_warning']}")
    if st.get("status") == "tripped":
        print("🛑 实盘熔断中——实盘开仓被拒；解除走复盘重启流程（reset --confirmed-by-user）")
    else:
        print("🟢 实盘熔断：未触发（实盘开仓正常，仍需 live 授权解锁 + 一级降频闸等既有闸）")


def main():
    args = sys.argv[1:]
    if not args or args[0] in ("status",):
        print_status()
        return
    cmd = args[0]
    if cmd == "trip":
        reason = None
        if "--reason" in args:
            i = args.index("--reason")
            if i + 1 < len(args):
                reason = args[i + 1]
        if not reason:
            print("用法: circuit_breaker.py trip --reason \"触发判据 + 证据（复盘日期、P(g>0)、近5笔）\"", file=sys.stderr)
            sys.exit(1)
        ev = {}
        if "--evidence-json" in args:
            i = args.index("--evidence-json")
            if i + 1 < len(args):
                try:
                    ev = json.loads(args[i + 1])
                except Exception as e:
                    print(f"--evidence-json 解析失败（{e}），证据未记录", file=sys.stderr)
        trip(reason, ev)
    elif cmd == "reset":
        confirm = None
        if "--confirmed-by-user" in args:
            i = args.index("--confirmed-by-user")
            if i + 1 < len(args):
                confirm = args[i + 1]
        if not confirm:
            print(json.dumps({
                "ok": False, "blocked_by": "reset_requires_user_confirmation",
                "error": ("⛔ reset 必须带 --confirmed-by-user \"留痕文本\"——解除熔断是放开风险的动作，"
                          "钥匙在用户手里：复盘给出重启建议 → AI 发 AskUserQuestion（选项含"
                          "「确认重启实盘 / 维持熔断」）→ 用户点确认后才可带此参数执行。"
                          "AI 未问用户就跑 reset 属违规。"),
            }, ensure_ascii=False))
            sys.exit(1)
        reset(confirm)
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
