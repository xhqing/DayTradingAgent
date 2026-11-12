#!/usr/bin/env python3
"""初始止损基准（T191HKUS）。

R 的分母「修正 max_loss」在开仓时点算死，移动止损不改基准（见 trading-strategy.md
「赔率三阶段」）。开仓成功后把初始止损写进 state/，平仓时读它算落地 R、过程指标
与连败结算；**找不到就跳过结算并显式告警**，绝不再拿「移损后的活动止损单触发价」
凑数——后者的止损距更小，会把 |R| 系统性放大，还会经连败闸错拦实盘开仓。

为什么单独存一份：平仓脚本运行时只能看到「当前活动止损单」，而移损早已把它改写；
actions 里的开仓条目虽然也有初始止损，但那是给人和复盘看的文本，不能作为脚本的
确定性输入。state/ 是跨会话、跨 worktree 共享的持久状态（与 losing_streak.json 同目录）。

手工补记（历史持仓 / 会话中断后）：
    python3 stop_basis.py set --market US --symbol MU --entry 824.08 --stop 821.5 \
        --qty 18 --direction long
    python3 stop_basis.py get --market US --symbol MU --entry 824.08
    python3 stop_basis.py clear --market US --symbol MU
"""
import argparse
import json
import os
import subprocess
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]        # scripts → trade → skills → .pi → 项目根
ENV_OVERRIDE = "STOP_BASIS_STATE"                 # 离线用例可指向临时文件


def _state_file():
    override = os.environ.get(ENV_OVERRIDE)
    if override:
        return Path(override)
    # worktree 的 state/ 可能是独立目录，持久状态必须落到主工作区那份
    try:
        out = subprocess.check_output(
            ["git", "-C", str(ROOT), "rev-parse", "--git-common-dir"],
            stderr=subprocess.DEVNULL, timeout=2, text=True).strip()
        common = Path(out)
        if not common.is_absolute():
            common = (ROOT / common).resolve()
        d = common.parent / "state"
        if d.is_dir():
            return d / "stop_basis.json"
    except Exception:
        pass
    return ROOT / "state" / "stop_basis.json"


def _load():
    try:
        return json.loads(_state_file().read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(data):
    p = _state_file()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)


def key(market, symbol):
    # 开仓 / 平仓脚本传进来的 symbol 可能是裸代码（MU）也可能带市场前缀（US.MU），
    # 统一去皮再拼键，否则两边会写成两份、平仓读不到。
    s = str(symbol).upper().strip()
    for prefix in (f"{market.upper()}.", "US.", "HK."):
        if s.startswith(prefix):
            s = s[len(prefix):]
            break
    return f"{market.upper()}:{s}"


def record(market, symbol, entry, stop_initial, quantity, direction, order_id=None):
    """开仓成交后调用：把初始止损钉在开仓时点。"""
    try:
        entry, stop_initial = float(entry), float(stop_initial)
        quantity = float(quantity)
    except (TypeError, ValueError):
        return False
    if entry <= 0 or stop_initial <= 0 or quantity <= 0:
        return False
    data = _load()
    data[key(market, symbol)] = {
        "entry": entry, "stop_initial": stop_initial, "quantity": quantity,
        "direction": direction, "order_id": str(order_id) if order_id else None,
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    try:
        _save(data)
        return True
    except Exception:
        return False


def resolve(market, symbol, entry=None, tol=0.006):
    """平仓时调用：返回 (初始止损, 来源)，找不到返回 (None, 原因)。

    带上开仓成交价做一致性校验，避免同标的连续两笔交易串到上一笔的基准。
    """
    rec = _load().get(key(market, symbol))
    if not rec:
        return None, "state 里没有该标的的初始止损记录"
    stop = rec.get("stop_initial")
    if not isinstance(stop, (int, float)) or stop <= 0:
        return None, "state 记录无效（缺 stop_initial）"
    if entry and rec.get("entry"):
        if abs(float(rec["entry"]) - float(entry)) > max(0.02, float(entry) * tol):
            return None, "state 记录的开仓价与本次平仓不匹配（疑似串笔）"
    return float(stop), f"state/stop_basis.json（{rec.get('ts', '')} 开仓时点）"


def clear(market, symbol):
    """平仓结算后调用，避免下一笔误用旧基准。"""
    data = _load()
    k = key(market, symbol)
    if k in data:
        del data[k]
        try:
            _save(data)
        except Exception:
            return False
    return True


def main():
    ap = argparse.ArgumentParser(description="初始止损基准（R 分母，移损不改）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("set", "get", "clear"):
        p = sub.add_parser(name)
        p.add_argument("--market", required=True, choices=("US", "HK"))
        p.add_argument("--symbol", required=True)
        if name == "set":
            p.add_argument("--entry", type=float, required=True)
            p.add_argument("--stop", type=float, required=True)
            p.add_argument("--qty", type=float, required=True)
            p.add_argument("--direction", required=True, choices=("long", "short"))
            p.add_argument("--order-id", default=None)
        elif name == "get":
            p.add_argument("--entry", type=float, default=None)
    args = ap.parse_args()
    if args.cmd == "set":
        ok = record(args.market, args.symbol, args.entry, args.stop,
                    args.qty, args.direction, args.order_id)
        print(json.dumps({"ok": ok, "path": str(_state_file())}, ensure_ascii=False))
    elif args.cmd == "get":
        stop, src = resolve(args.market, args.symbol, args.entry)
        print(json.dumps({"stop_initial": stop, "source": src}, ensure_ascii=False))
    else:
        print(json.dumps({"ok": clear(args.market, args.symbol)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
