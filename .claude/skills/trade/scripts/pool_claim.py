#!/usr/bin/env python3
"""标的池自动认领（多会话并行盯盘·方案 A 后续增强，2026-08-19 立，TODO 落地）。

目的：替代用户手动给每个会话划分标的池。候选标的池（如 hot_list 选出的活跃标的）由
系统自动切分成各会话各自负责、彼此互斥的子池——同一标的不落两个会话、无重复盯。

机制 = **认领制（claim）而非一次性均分**：
  会话数不定（今日开几个会话不预先知道）、启动有先后。每个会话盯盘启动时从共享候选池
  里「认领」一段未占用的标的；后启动的会话认领剩下的；死会话（崩溃 / 忘注销）的标的
  自动释放回候选池。天然支持会话数动态变化。

数据文件：tmp/pool_claims.json（当天有效；跨日自动重置——每条认领带日期，读时只认
  当日条目，昨日残留不占坑）。结构：
  {"date": "YYYY-MM-DD", "claims": [{"session": "<sid>", "symbols": ["HK.00700", ...],
    "ts": "HH:MM:SS"}, ...]}

互斥保证 = flock 文件锁（同 trade_mutex 思路）：整个「检查占用 → 认领登记」临界区在
锁内原子完成，两会话同时启动也不出双占。锁内同时做死会话清理（认领者顺手当清道夫）。

死会话判定（同 monitor_watcher.py 口径）：会话 jsonl 停更 > 30 分钟 = 已结束，其认领
  自动释放（jsonl 路径 ~/.claude/projects/<slug>/<sid>.jsonl，slug 生成规则同 watcher）。

用法（盯盘启动序列内，方向研判定候选池之后、启动采样之前）：
  python3 pool_claim.py claim HK.00700,HK.00981,HK.01810    # 认领（返回分到的池 + 全局状态）
  python3 pool_claim.py status                               # 看当前划分（不动任何东西）
  python3 pool_claim.py release HK.00700                     # 释放单个标的（池内僵局主动换池时）
  python3 pool_claim.py release --all                       # 释放本会话全部（停盯时）

认领前税闸预检（2026-08-24 立；阈值沿革 1.67%→2.22%（随开仓门槛 2.4→1.8 联动）→1.2%
（2026-08-25 用户下调，48 笔样本实证旧值错杀大于保护）→0.4%（2026-08-27 用户下调，
判据改组合口径「不影响 P(g>0) 前提下尽可能放松」；config.risk.stamp_tax_gate_pct 权威）：
claim 港股个股时现查富途 snapshot，实算该股当日
「预期止损距上限」（多空两侧结构距离较大者 × 0.4 折扣，同 hot_list.py 口径）——低于
门槛的 70% 即 ⚠️ 警告「过闸概率低、不值得认领」（2026-08-24 实录：认领 MINIMAX 后两次
形态成立均被税闸拦、空盯一上午，预检本可在认领时点就拦下）。预检是警告不是硬拦：
低概率 ≠ 零概率（新结构会出现），但警告在场 = AI 认领时必须直面这个数字、给出认领
理由（如「该板块无 ETF 替代 + 该股历史波幅大」），不能无意识认领。ETF/REIT 免印花税
直接放行；富途查询失败不阻断认领（降级提示「预检不可用」，认领链路不能因行情故障停摆）。

与现约定的关系：monitoring.md「标的池分配约定」的手动约定被本机制替代；「池内僵局换标的
只从本会话池关联板块换、不跨池抢」不变——换入新标的前先 claim（未占才可用）、换出旧标的
release，互斥语义持续成立。
"""

import fcntl
import json
import os
import sys
import time
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, "..", "..", "..", ".."))
TMP_DIR = os.path.join(PROJECT_ROOT, "tmp")
CLAIMS_FILE = os.path.join(TMP_DIR, "pool_claims.json")
LOCK_FILE = os.path.join(TMP_DIR, "pool_claim.lock")

# 死会话阈值（同 monitor_watcher.DEAD_SESSION_SECONDS）：jsonl 停更 > 30 分钟 = 已结束。
DEAD_SESSION_SECONDS = 30 * 60

# ---- 认领前税闸预检（2026-08-24 立，口径同 hot_list.py）----
# 2026-08-25 起 config.json risk.stamp_tax_gate_pct 为唯一权威源（open_position 下单硬校验
# 同读此值）,此处默认仅作 config 缺失时的回退——调整阈值改 config,并同步 hot_list.py 回退常量。
# 2026-08-25 用户下调 2.22%→1.2%；2026-08-27 再下调 1.2%→0.4%（判据改组合口径：
# P(g>0)=99.6% 贴顶约束不 binding、增量单税后 EV 转负底线 ≈0.26% 止损距、闸 0.4% 组合
# g_hat 仅降 6.4%——推导见 risk-management.md「港股个股印花税闸」）。
STAMP_STOP_PCT = 0.004       # 印花税闸门槛（止损距 ≥ 0.4%×股价；2026-08-27 组合口径下调）
STOP_DISCOUNT = 0.4          # 结构距离 → 实际止损距折扣（入场与止损各贴结构位吃缓冲）
GATE_WARN_RATIO = 0.7        # 预期止损距 < 70% 门槛 → ⚠️ 过闸概率低警告

def _stamp_gate_pct():
    """税闸门槛（config 优先,缺失回退本文件常量）。"""
    import json
    try:
        cfg = json.load(open(os.path.join(SCRIPT_DIR, "..", "config.json")))
        return float(cfg.get("risk", {}).get("stamp_tax_gate_pct", STAMP_STOP_PCT))
    except Exception:
        return STAMP_STOP_PCT


def _stamp_gate_enabled():
    """税闸总开关（2026-08-28 立）：false = 税闸停用,认领预检整体跳过。"""
    import json
    try:
        cfg = json.load(open(os.path.join(SCRIPT_DIR, "..", "config.json")))
        return bool(cfg.get("risk", {}).get("stamp_tax_gate_enabled", True))
    except Exception:
        return True


def _stamp_gate_check(symbols):
    """对认领候选跑税闸预检。返回 {sym: msg}，msg None = 通过（ETF/美股/预检过），否则为警告文案。

    只对港股个股查（HK. 前缀）；现查富途 snapshot 实算预期止损距上限——多空两侧结构距离
    （现价↔当日低点 / 现价↔min(VWAP,当日高点)）较大者 × 折扣。ETF/REIT 免印花税、美股无
    此约束，直接通过。富途 OpenD 不可用 → 全部返回「预检不可用」提示（不阻断认领）。
    类型判定复用 classify_hk_security（同目录上级）；判定失败按个股处理（宁可多警告）。
    ⚠️ 2026-08-28 税闸停用（config.risk.stamp_tax_gate_enabled=false）时本函数直接返回空
    dict（不预检、不警告）。
    """
    if not _stamp_gate_enabled():
        return {}
    hk = [s for s in symbols if s.startswith("HK.")]
    if not hk:
        return {}
    script_parent = os.path.dirname(SCRIPT_DIR)
    if script_parent not in sys.path:
        sys.path.insert(0, script_parent)
    sec_type = {}
    try:
        from classify_hk_security import classify as _classify
        for c in hk:
            try:
                sec_type[c] = _classify(c.split(".")[-1]).get("type", "?")
            except Exception:
                sec_type[c] = "?"
    except ImportError:
        sec_type = {c: "?" for c in hk}
    result = {}
    todo = [c for c in hk if sec_type.get(c, "?") in ("stock", "?")]   # ETF/REIT 跳过、未知类型照检
    if not todo:
        return result
    snaps = {}
    try:
        from futu import OpenQuoteContext
        ctx = OpenQuoteContext('127.0.0.1', 11111)
        try:
            ret, snap = ctx.get_market_snapshot(todo)
            if ret == 0 and snap is not None:
                snaps = {r['code']: r for _, r in snap.iterrows()}
        finally:
            ctx.close()
    except Exception:
        for c in todo:
            result[c] = "⚠️ 税闸预检不可用（富途查询失败）——认领后开盘前自查形态止损距能否过税闸门槛（config.risk.stamp_tax_gate_pct×股价）"
        return result
    for c in todo:
        s = snaps.get(c)
        if s is None:
            result[c] = "⚠️ 税闸预检不可用（无快照数据）——认领后开盘前自查形态止损距能否过税闸门槛（config.risk.stamp_tax_gate_pct×股价）"
            continue
        px, lo, hi, vwap = s.get('last_price'), s.get('low_price'), s.get('high_price'), s.get('avg_price')
        if not (px and px == px and lo and lo == lo):
            result[c] = "⚠️ 税闸预检不可用（快照缺价字段）"
            continue
        long_cap = (px - lo) / px if px > lo else 0.0
        resist_cands = [v for v in (hi, vwap) if v and v == v]
        short_cap = (min(resist_cands) - px) / px if resist_cands and min(resist_cands) > px else 0.0
        exp_pct = max(long_cap, short_cap) * STOP_DISCOUNT * 100
        gate_pct = _stamp_gate_pct() * 100
        if exp_pct < gate_pct * GATE_WARN_RATIO:
            result[c] = (f"⚠️ 税闸预检：过闸概率低（预期止损距上限 {exp_pct:.2f}% < 70%×门槛 "
                         f"{gate_pct:.2f}%）——该股当日结构大概率过不了印花税闸、不值得认领，"
                         f"优先换同板块 ETF 或预检过的个股")
        elif exp_pct < gate_pct:
            result[c] = (f"🟡 税闸预检：边缘（预期止损距上限 {exp_pct:.2f}%，门槛 {gate_pct:.2f}%）"
                         f"——谨慎认领，形态成立时止损距须拉宽才过闸")
        # ≥ 门槛：不输出（通过）
    return result

# ~/.claude/projects/<slug>/ 的 slug 生成规则（同 monitor_watcher）：路径去斜杠、段间 - 连接、前加 -
_SLUG = "-" + PROJECT_ROOT.strip(os.sep).replace(os.sep, "-")
TRANSCRIPT_DIR = os.path.expanduser(os.path.join("~/.claude/projects", _SLUG))


def _jsonl_age(sid):
    """会话 jsonl 停更秒数；文件不存在返回 None（同 monitor_watcher.jsonl_age 口径）。"""
    p = os.path.join(TRANSCRIPT_DIR, sid + ".jsonl")
    if not os.path.isfile(p):
        return None
    try:
        return time.time() - os.path.getmtime(p)
    except OSError:
        return None


def _session_alive(sid):
    """会话是否还活着：jsonl 停更 ≤ 30 分钟（None = 文件不存在 = 已结束）。"""
    age = _jsonl_age(sid)
    return age is not None and age <= DEAD_SESSION_SECONDS


def _load_claims(today):
    """读当日认领表；文件不存在 / 非当日 / 坏 JSON → 空表（昨日残留不占坑）。"""
    if not os.path.exists(CLAIMS_FILE):
        return []
    try:
        with open(CLAIMS_FILE) as f:
            data = json.load(f)
        if data.get("date") != today:
            return []   # 跨日：昨日认领全部失效
        return data.get("claims", [])
    except Exception:
        return []


def _save_claims(today, claims):
    os.makedirs(TMP_DIR, exist_ok=True)
    tmp = CLAIMS_FILE + ".tmp"
    with open(tmp, "w") as f:
        f.write(json.dumps({"date": today, "claims": claims}, ensure_ascii=False, indent=1))
    os.replace(tmp, CLAIMS_FILE)


def _prune_dead(claims):
    """清死会话认领（返回 (清理后 claims, 被释放的 [(sid, symbols), ...])）。"""
    kept, released = [], []
    for c in claims:
        if _session_alive(c["session"]):
            kept.append(c)
        else:
            released.append((c["session"], c["symbols"]))
    return kept, released


def _norm_sym(s):
    """标的代码规范化：去空白、统一大写（HK.00700 / us.mu 同义）。"""
    s = s.strip().upper()
    # 补齐港股 5 位前导 0（HK.700 → HK.00700，认领与采样命令格式可能不一致）
    if s.startswith("HK.") and s[3:].isdigit() and len(s[3:]) < 5:
        s = "HK." + s[3:].zfill(5)
    return s


def _fmt_claims(claims):
    """认领表 → 人读状态行。"""
    if not claims:
        return "（无认领——候选池全部空闲）"
    lines = []
    for i, c in enumerate(claims, 1):
        sid_short = c["session"][:8] if c["session"] else "?"
        syms = ", ".join(c["symbols"]) if c["symbols"] else "（空认领——仅放弃清单）"
        lines.append(f"  会话{i} [{sid_short}…]: {syms}（认领于 {c['ts']}）")
    return "\n".join(lines)


def _sec_types(symbols):
    """批量判港股标的类型（复用 classify_hk_security；美股返回 'us'）。

    ETF 优先认领闸（2026-08-28 立）用：判 etf / 非 etf。classify 不可用 / 判定失败
    返回 '?'（不参与 ETF 优先拦截——判不了类型不能瞎拦，认领链路不因分类器故障停摆）。
    """
    out = {}
    hk = [s for s in symbols if s.startswith("HK.")]
    for s in symbols:
        if not s.startswith("HK."):
            out[s] = "us"
    if not hk:
        return out
    script_parent = os.path.dirname(SCRIPT_DIR)
    if script_parent not in sys.path:
        sys.path.insert(0, script_parent)
    try:
        from classify_hk_security import classify as _classify
        for c in hk:
            try:
                out[c] = _classify(c.split(".")[-1]).get("type", "?")
            except Exception:
                out[c] = "?"
    except ImportError:
        for c in hk:
            out[c] = "?"
    return out


def _hot_etf_candidates():
    """热榜 ETF 候选（2026-08-28 用户裁定「先选没人认领的 ETF」实现）：
    取富途港股热榜（同 hot_list.py 口径），筛出类型为 ETF 的标的作为「可认领 ETF 全集」。
    2026-08-28 午重构：只返回热榜里的 ETF（供按方向匹配），不再作为「全量灌入池子」的来源。

    只返回榜单里类型可判为 etf 的代码。富途不可用 / 榜单空 / 分类器故障 → 返回 []（调用方
    降级为「只按本次候选判定」，不因热榜故障阻断认领链路——热榜是增强、不是认领前置）。
    """
    etfs = []
    try:
        from futu import OpenQuoteContext, Market
        ctx = OpenQuoteContext('127.0.0.1', 11111)
        try:
            ret, payload = ctx.get_hot_list(market=Market.HK, count=50)
            if ret != 0:
                return []
            df = payload[1] if isinstance(payload, tuple) else payload
            if df is None or len(df) == 0:
                return []
            sec_col = 'security' if 'security' in df.columns else df.columns[0]
            codes = [c for c in df[sec_col].tolist() if str(c).startswith('HK.')]
        finally:
            ctx.close()
        if not codes:
            return []
        types = _sec_types(codes)
        etfs = [c for c in codes if types.get(c) == "etf"]
    except Exception:
        return []
    return etfs


def _direction_similar(cand, ref):
    """方向相似性（2026-08-28 立，ETF 优先认领闸「按盯盘方向」判定）：
    简单同数字前缀判定——港股代码前缀相同段（如 03067 与 03110 同属恒科 03 段）视为
    同方向候选。够用、无外部依赖；精确的板块映射留待需要时再加。
    """
    a, b = cand.split(".")[-1], ref.split(".")[-1]
    # 同 2 位前缀（030xx / 028xx）判同方向；不同前缀不判
    return a[:2] == b[:2] and len(a) >= 2 and len(b) >= 2


def cmd_claim(args):
    """认领：候选标的逐个检查，未占用则登记给本会话；返回分到的池。"""
    if not args:
        print("用法：python3 pool_claim.py claim HK.00700,HK.00981,...", file=sys.stderr)
        return 1
    want = []
    for a in args:
        for s in a.split(","):
            if s.strip():
                want.append(_norm_sym(s))
    want = list(dict.fromkeys(want))   # 去重保序
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    if not sid:
        print("⚠️ 未拿到 CLAUDE_CODE_SESSION_ID（非会话内跑？）——认领会记到 unknown，"
              "死会话判定失效；建议在盯盘会话内跑本命令", file=sys.stderr)

    today = datetime.now().strftime("%Y-%m-%d")
    now_t = datetime.now().strftime("%H:%M:%S")
    # ETF 优先认领闸要的类型判定（锁外先算好，锁内只做判定不做 IO）
    got_types = {}
    with open(LOCK_FILE, "a") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        claims, released = _prune_dead(_load_claims(today))
        if released:
            for r_sid, r_syms in released:
                print(f"🧹 已释放死会话认领：[{r_sid[:8]}…] 的 {', '.join(r_syms)}")
        mine = next((c for c in claims if c["session"] == sid), None)
        taken = {}   # symbol -> 持有会话
        for c in claims:
            for s in c["symbols"]:
                taken[s] = c["session"]
        got, conflict = [], []
        for s in want:
            holder = taken.get(s)
            if holder is None or holder == sid:
                got.append(s)
            else:
                conflict.append((s, holder))
        # 🚫 ETF 优先认领闸（2026-08-28 用户立，硬拦；2026-08-28 午重构为「按盯盘方向」）：
        # 认领标的时先选没人认领的 ETF，个股只有在不存在「可认领 ETF」时才能认领。
        # 范围（2026-08-28 用户裁定）：按盯盘方向——候选里 + 热榜里与候选**同方向**的
        # ETF 必须优先认，不强制把全市场热榜 ETF 全灌进池子（那样一认个股就要 release
        # 一堆强加 ETF、池子巨大难盯）。
        # 实现：① 候选里类型 etf 的（本次 got 中）就是「候选内 ETF」；② 热榜里与候选
        #    同方向的未认领 ETF 自动并入本次认领；③ 个股（stock/reit）只在「候选内无
        #    ETF 且热榜无同方向可认领 ETF 且本会话池无 ETF」时才放行，否则拒绝。
        # 为什么（当日实录）：00700 第二笔止损 −1R 里 0.42R 是印花税，同笔若 ETF 只亏
        #    −1.19R；「港股优先 ETF」旧规只是偏好、税闸过了就跳过 ETF 核查——本闸把
        #    它变成机械强制。
        # 口径：① 「可认领 ETF」= 未被其它会话认领（互斥语义优先、不逼 AI 跨池抢）；
        #    ② 本会话已持有的 ETF 也算在场约束；③ 类型判定失败（?）不拦（判不了不瞎
        #    拦，认领链路不因分类器故障停摆）；④ 美股候选不参与本闸（美股个股无印花税
        #    劣势）；⑤ 拒绝是「本次不登记」而非全局拉黑——release 掉 ETF 后可再 claim
        #    个股；⑥ release 掉的 ETF 不自动认回（released 清单）；⑦ 热榜取不到 → 退化
        #    为「只按候选内 ETF 判定」（仍拦候选内 ETF 存在时的个股）。
        got_types = _sec_types(got) if got else {}
        held_syms = mine["symbols"] if mine else []
        held_types = _sec_types([s for s in held_syms if s not in got_types]) if held_syms else {}
        all_types = dict(held_types)
        all_types.update(got_types)
        released = mine.get("released", []) if mine else []
        # 热榜同方向未认领 ETF：候选内已有方向参照（候选里的个股/ETF）时，才并入同方向
        # 热榜 ETF；候选为空 → 无方向参照，不强灌热榜。
        cand_dirs = [c for c in (held_syms + got) if c.startswith("HK.")]
        hot_etfs = []
        if cand_dirs:
            hot_etfs = [c for c in _hot_etf_candidates()
                        if taken.get(c) in (None, sid) and c not in got and c not in released
                        and any(_direction_similar(c, r) for r in cand_dirs)]
        for c in hot_etfs:
            if c not in got:
                got.append(c)
        all_types.update(_sec_types([c for c in hot_etfs if c not in got_types]))
        # 可认领 ETF = 候选内 ETF + 热榜同方向并入 ETF + 本会话已持有 ETF
        held_etfs = sorted({s for s in (held_syms + hot_etfs + got)
                            if s.startswith("HK.") and all_types.get(s) == "etf"})
        if held_etfs:
            blocked = [s for s in got if s.startswith("HK.") and all_types.get(s) in ("stock", "reit")]
            if blocked:
                got = [s for s in got if s not in blocked]
        else:
            blocked = []
        # 登记段（原逻辑，2026-08-28 ETF 闸编辑时误删、同日补回）：把本次拿到的标的并入本会话池
        if got:
            if mine:
                new_syms = list(dict.fromkeys(mine["symbols"] + [s for s in got if s not in mine["symbols"]]))
                mine["symbols"] = new_syms
                mine["ts"] = now_t
            else:
                claims.append({"session": sid or "unknown", "symbols": got, "ts": now_t})
        _save_claims(today, claims)
        fcntl.flock(lf, fcntl.LOCK_UN)

    if conflict:
        for s, holder in conflict:
            print(f"❌ {s} 已被会话 [{holder[:8]}…] 认领——不跨池抢标的，从候选里去掉")
    if blocked:
        print(f"🚫 ETF 优先认领闸（2026-08-28 立）：存在可认领 ETF（{', '.join(sorted(set(held_etfs)))}），"
              f"个股不得认领——已拒 {', '.join(blocked)}。股票只有在不存在可认领 ETF 时才能认领；"
              f"要认个股先 release ETF 或确认全池无 ETF 可认")
    if got:
        # 认领前税闸预检（2026-08-24 立）：对认领到的港股个股跑过闸概率预检——
        # 预期止损距上限（结构距离 × 0.4）< 70%×税闸门槛即 ⚠️ 警告「不值得认领」（阈值读 config）。
        # 预检不撤销已完成的登记（认领互斥语义不变），但警告在场 = AI 必须直面数字：
        # 要么立刻 release 换标的、要么带着明确理由继续盯，不允许无意识认领。
        gate_msgs = _stamp_gate_check(got)
        for s in got:
            if s in gate_msgs:
                print(f"{gate_msgs[s]}（{s}）")
        print(f"✅ 本会话认领到：{', '.join(got)}")
    else:
        print("⚠️ 候选全部被其它会话占用——换 hot_list 刷新候选再试")
    print(f"\n📌 当前全局划分（{today}）：\n{_fmt_claims(claims)}")
    return 0


def cmd_status(_args):
    today = datetime.now().strftime("%Y-%m-%d")
    with open(LOCK_FILE, "a") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        claims, released = _prune_dead(_load_claims(today))
        if released:
            _save_claims(today, claims)
            fcntl.flock(lf, fcntl.LOCK_UN)
        else:
            fcntl.flock(lf, fcntl.LOCK_UN)
    if released:
        for r_sid, r_syms in released:
            print(f"🧹 已释放死会话认领：[{r_sid[:8]}…] 的 {', '.join(r_syms)}")
    print(f"📌 当前全局划分（{today}）：\n{_fmt_claims(claims)}")
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    if sid:
        mine = [c for c in claims if c["session"] == sid]
        if mine:
            print(f"👉 本会话的池：{', '.join(mine[0]['symbols'])}")
        else:
            print("👉 本会话当前无认领")
    return 0


def cmd_release(args):
    """释放：release --all 释放本会话全部；release SYM[,SYM…] 释放指定标的（仅限本会话持有的）。"""
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    today = datetime.now().strftime("%Y-%m-%d")
    release_all = "--all" in args
    syms = [_norm_sym(s) for a in args if a != "--all" for s in a.split(",") if s.strip()]
    with open(LOCK_FILE, "a") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        claims, _ = _prune_dead(_load_claims(today))
        removed = []
        for c in claims:
            if c["session"] != sid:
                continue
            if release_all:
                removed.extend(c["symbols"])
                c["symbols"] = []
                # release --all = 停盯 / 放弃全部：放弃清单一并清空，新会话重新认领
                c["released"] = []
            else:
                for s in syms:
                    if s in c["symbols"]:
                        c["symbols"].remove(s)
                        removed.append(s)
        # 部分释放的标的记入本会话「放弃清单」（ETF 优先认领闸用——热榜自动并入时排除，
        # 保证 AI 明确放弃的 ETF 不自动认回）。release --all 已清空（见上），不重复记。
        if removed and not release_all:
            for c in claims:
                if c["session"] == sid:
                    c.setdefault("released", [])
                    c["released"] = list(dict.fromkeys(c["released"] + removed))
        # 空认领条目保留（released 清单要留着；不占池——taken 遍历只认 symbols 非空）
        claims = [c for c in claims if c["symbols"] or c.get("released")]
        _save_claims(today, claims)
        fcntl.flock(lf, fcntl.LOCK_UN)
    if removed:
        print(f"✅ 已释放：{', '.join(dict.fromkeys(removed))}（其它会话可认领）")
    else:
        print("（本会话没有可释放的认领）")
    return 0


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    args = sys.argv[2:]
    if cmd == "claim":
        return cmd_claim(args)
    if cmd == "status":
        return cmd_status(args)
    if cmd == "release":
        return cmd_release(args)
    print("用法：python3 pool_claim.py claim <SYM,SYM,...> | status | release <SYM,...|--all>",
          file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
