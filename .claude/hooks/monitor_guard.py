#!/usr/bin/env python3
"""盯盘密采样守卫 hook（2026-08-04 立，A3 + B4 多层防护）。

为什么：盯盘期间 AI 曾用 CronCreate 设低频扫描 + cron 触发时直接调 snapshot.py/hot_list.py
（不经 monitor_segment.py）绕过 40 秒密采样强制（2026-08-04 违规教训）。脚本护栏
（monitor_segment.py 的 DURATION>40 夹回 40）只在调用该脚本时生效，AI 不调脚本就绕过。
本 hook 在两个关卡补查 monitor_segment 是否在跑，堵绕过路径：

- PreToolUse（matcher Bash）：盘中调 snapshot.py/hot_list.py 但 monitor_segment 未在跑 →
  exit 2 阻断 + stderr 提醒「密采样走 monitor_segment，禁 snapshot/hot_list 替代」。
  （monitor_segment 在跑时的 snapshot 是开仓前正常刷新，不阻。）
- PreToolUse（matcher Bash，A4）：盘中单条 Bash 命令含 ≥2 次 monitor_segment.py 调用
  （&& 连跑多段）→ exit 2 阻断 + stderr 提醒「连跑 = 段间不分析 = 等效降频」。
- PreToolUse（matcher Bash，空转硬门 2026-08-17 立）：盘中重启密采样段（恰好 1 个
  exec_seg）但分析心跳停更 > 180 秒 / 当天采样已跑 ≥180 秒仍零心跳 → exit 2 阻断，
  逼 AI 先补「一行式判断 + 写分析心跳」再重启（堵 2026-08-17 空转实录：52 次纯重启
  采样、0 分析文本——采样链防护全部放行、分析链死亡无人拦）。
- PreToolUse（matcher Bash，跑法拦截 2026-08-18 立）：盘中用 nohup 后台跑密采样段 /
  sleep 拼接在采样段命令上 → exit 2 阻断 + 提示「段启动必须 run_in_background +
  task-notification 通知驱动」。为什么：2026-08-18 实录——切富途逐笔源时把启动方式
  误改成 nohup+sleep 轮询自驱，段结束不唤醒、AI sleep 后才顺手读旧输出，数据滞后
  40-60 秒、错过入场判定窗口；run_in_background 的段结束 task-notification 才是
  skill 规定的唤醒机制（monitoring.md「后台采样不间断规矩」）。
- PreToolUse（matcher Bash，标的池互斥 2026-08-19 立；2026-08-20 修管道绕过 + 扩三层）：
  启动采样段时三层检查——targets 含其它会话已认领标的（跨池冲突）/ 本会话未认领就采样 /
  目标含无人认领标的 → exit 2 阻断 + 提示先跑 pool_claim.py。2026-08-20 港股实录：真实
  采样命令几乎都带 `2>&1 | grep -vE …` 输出管道，旧「提及未执行」排除词把管道里的 grep
  误当命令本体、整条采样命令被剔出 exec_seg → 本拦截（连同 A4 连跑 / 跑法 / 空转门）对
  管道形态命令全部失明，signal 会话与 auto 会话重复盯 00100/01810/09988 一小时 0 拦截。
  修正 = 排除词只看管道首段（见 pretool 分支注释）。
- Stop：盘中且回合结束 monitor_segment 未在跑 → stderr 提醒「盯盘期间必须保持
  monitor_segment 循环，立即重启」。

局限（诚实）：AI 仍能 kill 进程或换别的方式绕过——本 hook 只提高绕过成本 + 暴露，
不是 100% 银弹（详见 monitoring.md「2026-08-04 教训」多层防护说明）。

用法（hooks 注册，2026-09-02 起双宿主：CC settings.json + ZCode .zcode/config.json 同挂）：
  PreToolUse matcher Bash → python3 .claude/hooks/monitor_guard.py pretool
  PreToolUse matcher TaskStop → python3 .claude/hooks/monitor_guard.py taskstop
  Stop                    → python3 .claude/hooks/monitor_guard.py stop
hook 接收 stdin JSON（tool_name/tool_input 等，两宿主字段同名），exit 0 放行 / exit 2 阻断（PreToolUse）；
提醒类输出走 _emit_reminder 双通道（CC stderr / ZCode stdout JSON，见其 docstring）。
"""
import sys
import os
import json
import time
import subprocess
from datetime import datetime

# 项目根 = .claude/hooks 的上两级（.claude/hooks -> .claude -> 项目根）
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.abspath(os.path.join(_SCRIPT_DIR, "..", ".."))
TMP_DIR = os.path.join(_PROJECT_ROOT, "tmp")
# 2026-08-16 分层（原 STALE_SECONDS=300 单层与自述「段间循环 <90 秒」不匹配——进程死后
# log 5 分钟内仍判「在跑」、最长 5 分钟盲窗）：
#   进程在跑（pgrep）或 log <120 秒有更新 → 在跑（确定信号）；
#   log 120-300 秒有更新 → 疑似断了（提示重启但不阻断）；
#   log >300 秒无更新 → 断了（原口径，阻断/提醒照旧）。
RUNNING_SECONDS = 120   # log 近 2 分钟有更新 = 视为在跑
STALE_SECONDS = 300     # 超过 5 分钟无更新 = 确定断

# 空转防护（2026-08-17 立红灯「盯盘空转」修法④）：AI 要重启密采样段时，若分析心跳
# （tmp/analysis_beat_{date}_{mode}.csv，AI 每段分析时追加）停更超过此阈值 → PreToolUse
# 阻断，逼 AI 先补「一行式判断 + 写心跳」再重启采样。为什么做成 hook 硬门：2026-08-17
# 实录空转一下午（52 次纯重启采样、0 分析文本），用户纠正后 10 分钟复发——reference
# 软约束会衰减，工具级阻断才拦得住「只重启不分析」的路径。
# 阈值同 watcher 的 ANALYSIS_BEAT_STALE_SECONDS（180 秒 ≈ 3 个段周期）。
# 边界（防误伤）：① 当天无心跳文件且采样 log 也不足 3 分钟 = 盯盘刚启动，放行（第一段
# 之前本就没有分析）；② 非采样命令不拦（本检查只挂在「重启采样」动作上）。
ANALYSIS_BEAT_STALE_SECONDS = 180


def analysis_beat_status():
    """分析心跳状态（2026-08-17 立）。返回 (state, age_seconds)：
    - ("fresh", 秒)：心跳新鲜（< 阈值）。
    - ("stale", 秒)：心跳停更超阈值（空转形态①：分析过、后来死了）。
    - ("none", None)：当天无心跳文件。调用方结合采样 log 时长判断——log 也不足阈值
      = 刚启动放行；log 已跑 ≥ 阈值仍零心跳 = 从未分析过（空转形态②）。
    signal/auto 任一 mode 的心跳新鲜即 fresh（guard 拿不到本会话 mode，任一路分析
    在跑 = 不是全空转，与 watcher 同口径）。
    """
    import glob
    today = datetime.now().strftime("%Y%m%d")
    beats = glob.glob(os.path.join(TMP_DIR, f"analysis_beat_{today}_*.csv"))
    if not beats:
        return ("none", None)
    newest = max(os.path.getmtime(p) for p in beats)
    age = datetime.now().timestamp() - newest
    return ("fresh", age) if age < ANALYSIS_BEAT_STALE_SECONDS else ("stale", age)


def sampling_log_run_seconds():
    """当天采样已运行多久（秒）：读当天（按文件名日期）monitor_log 文件**首行采样时刻**，
    取最早的距今时长。无当天文件 / 读不出首行返回 None。与 monitor_watcher 的
    _sampling_log_age_seconds 同口径（读文件内容首行而非 ctime——ctime 会因 rename 等
    元数据操作被重置，语义不稳，2026-08-17 实测教训）。"""
    import glob
    today = datetime.now().strftime("%Y%m%d")
    logs = glob.glob(os.path.join(TMP_DIR, f"monitor_log_*_{today}_*.csv"))
    if not logs:
        return None
    from datetime import timedelta
    now = datetime.now()
    oldest_start = None
    for p in logs:
        try:
            with open(p) as lf:
                for line in lf:
                    line = line.strip()
                    if not line or line.startswith("time,"):
                        continue  # 跳表头 / 空行
                    first_t = line.split(",", 1)[0]
                    ft = datetime.strptime(first_t, "%H:%M:%S").time()
                    fdate = (now.date() - timedelta(days=1)) if ft > now.time() else now.date()
                    start = datetime.combine(fdate, ft)
                    if oldest_start is None or start < oldest_start:
                        oldest_start = start
                    break  # 只看首行
        except Exception:
            continue
    if oldest_start is None:
        return None
    return (now - oldest_start).total_seconds()


def _parse_hm(s):
    return datetime.strptime(s, "%H:%M").time()


_HK_AM_START = _parse_hm("09:30")
_HK_AM_END = _parse_hm("12:00")
_HK_PM_START = _parse_hm("13:00")
_HK_PM_END = _parse_hm("16:00")


def in_hk_session(now):
    """港股盘中（HKT 09:30-12:00 / 13:00-16:00，周一至周五）。"""
    if now.weekday() >= 5:  # 周末
        return False
    t = now.time()
    return (_HK_AM_START <= t < _HK_AM_END) or (_HK_PM_START <= t < _HK_PM_END)


def in_us_session(now):
    """美股可交易时段（美东 04:00-16:00 = 盘前 + 盘中，zoneinfo 自动处理夏令时/冬令时与跨午夜）。

    2026-08-18 立规：美股可交易窗口扩为盘前 + 盘中（美东 04:00 起），guard 守卫窗口同步扩——
    盘前盯盘（采样 / 分析 / 开仓前刷新）与盘中同权受守卫保护。16:00 收盘边界不变。
    2026-08-16 修（原实现「夏令时 HKT 21:30-次日 04:00 硬编码 + weekday()>=5 排周末」三处错）：
    ① 北京周六 00:00-04:00（美东周五盘中）guard 完全失效 4 小时；
    ② 周一凌晨（美东周日休市）误激活；
    ③ 11 月切冬令时后整体错位 1 小时。
    现按美东本地时间判定（preflight.py 同口径），zoneinfo 不可用时回退旧硬编码。"""
    try:
        from zoneinfo import ZoneInfo
        us = now.astimezone(ZoneInfo("America/New_York"))
        if us.weekday() >= 5:
            return False
        t = us.time()
        return _parse_hm("04:00") <= t < _parse_hm("16:00")
    except Exception:
        # zoneinfo 不可用（极端环境）→ 回退夏令时硬编码（美东 04:00-16:00 = 北京 16:00-次日 04:00）
        if now.weekday() >= 5:
            return False
        t = now.time()
        return t >= _parse_hm("16:00") or t < _parse_hm("04:00")


def in_trading_session(now=None):
    now = now or datetime.now()
    return in_hk_session(now) or in_us_session(now)


def monitor_segment_running(now=None):
    """monitor_segment（或 ws_segment / futu_ws_segment，2026-08-16 扩检测面）是否在跑：
    进程在 OR monitor_log 近 RUNNING_SECONDS 有更新。

    分层（2026-08-16 立，原单层 300 秒与自述「段间循环 <90 秒」不匹配）：
    进程在 / log <120s → (True, ...)；log 120-300s → (False, "疑似断了…")——
    调用方 pretool 阻断、stop 提醒时按 False 处理（宁可多提醒一次），文案带疑似标记。

    返回 (running: bool, why: str)。
    """
    now = now or datetime.now()
    # ① 进程检查（三个密采样入口都认——2026-08-07 起港股主力采样是 ws_segment，
    #    只认 monitor_segment 会把正常 ws 采样误判为「未在跑」而阻断正常操作）
    for script in ("monitor_segment.py", "ws_segment.py", "futu_ws_segment.py"):
        try:
            out = subprocess.run(
                ["pgrep", "-f", script],
                capture_output=True, text=True, timeout=3,
            )
            if out.stdout.strip():
                return True, f"进程在跑（{script}）"
        except Exception:
            pass
    # ② monitor_log 新鲜度分层（段间循环 <90 秒；ws 系每秒写一行更密）
    try:
        if os.path.isdir(TMP_DIR):
            newest = 0.0
            for f in os.listdir(TMP_DIR):
                if f.startswith("monitor_log_") and f.endswith(".csv"):
                    mtime = os.path.getmtime(os.path.join(TMP_DIR, f))
                    if mtime > newest:
                        newest = mtime
            if newest > 0:
                age = now.timestamp() - newest
                if age < RUNNING_SECONDS:
                    return True, f"log {int(age)} 秒前更新"
                if age < STALE_SECONDS:
                    return False, f"疑似断了（log {int(age)} 秒未更新，段间循环应 <90 秒）"
            return False, "log 长时间无更新"
    except Exception as e:
        return False, f"检查异常 {e}"
    return False, "无 monitor_log"


def _pool_conflict(exec_seg_cmds, sid=""):
    """标的池互斥检测（2026-08-19 立；2026-08-20 修管道绕过 + 扩三层语义）。

    启动采样段的三层检查（命中任一层 → 阻断消息；全过 / 无认领表 / 无会话 id → None）：
      层1 跨池冲突：targets 含**其它会话**已认领标的（原 2026-08-19 语义，不变）；
      层2 未认领即采样：本会话**没跑过 pool_claim.py claim 就启动采样段**（2026-08-20 新增）
          ——2026-08-20 港股实录：两个 signal 会话跳过认领直接采样，把 auto 会话已认领的
          00100/01810/09988 重复盯了一小时，层1 因这些标的没进认领表而拦不住（未认领 ≠
          未占用），层2 补的就是这个洞：**不认领就不给采样**，认领是采样的前置；
      层3 孤儿标的：targets 含**任何人（含自己）都未认领**的标的（2026-08-20 新增）
          ——已认领的会话临时往 targets 里塞新标的（僵局换标的没先 claim）也拦，
          堵「先斩后奏」路径。层2 / 层3 只在认领表**非空**（存在活认领）时生效——
          全场无人认领（单会话 / 认领制未启用 / 认领者全死）时不拦，
          保持旧口径「无划分不误伤」。

    从命令里提取 targets 标的，与 tmp/pool_claims.json（scripts/pool_claim.py 维护）比对。

    targets 格式（monitor_segment / ws_segment / futu_ws_segment 同构）：
    `SYM[:up[:dn]][,SYM[:up[:dn]]...]`，SYM 带 HK./US. 前缀。可能作为多个独立参数传入
    （futu_ws_segment 的 <duration> <targets> 两参数式），从命令里逐 token 抓含市场前缀的。
    """
    if not sid:
        return None   # 非会话内（手动跑 / 测试）：无池概念，不拦
    claims_path = os.path.join(TMP_DIR, "pool_claims.json")
    if not os.path.isfile(claims_path):
        return None   # 无认领表文件：不拦（认领制未启用）
    try:
        import datetime as _dt
        with open(claims_path) as f:
            data = json.load(f)
        if data.get("date") != _dt.datetime.now().strftime("%Y-%m-%d"):
            return None   # 非当日认领（跨日残留）：不拦
        claims_list = data.get("claims", [])
    except Exception:
        return None   # 认领表读坏：宁可放行（互斥闸另有兜底），不当普通采样拦死

    taken = {}          # symbol -> 持有会话 sid
    alive_claims = []
    for c in claims_list:
        holder = c.get("session", "")
        # 死会话认领不占坑（同 pool_claim._prune_dead 口径，jsonl 停更 >30 分钟 = 已结束）：
        # 不写回认领表（hook 只读——写回需拿 pool_claim 的 flock，hook 高频跑不宜持锁），
        # 只在本轮判定里当作已释放。认领者下次跑 pool_claim 时会真正落盘清理。
        if holder and holder != sid and not _claim_holder_alive(holder):
            continue
        alive_claims.append(c)
        for s in c.get("symbols", []):
            taken[str(s).strip().upper()] = holder

    import re as _re2
    syms_in_cmd = set()
    for sc in exec_seg_cmds:
        for tok in _re2.findall(r"(?:HK|US)\.[A-Za-z0-9]+", sc):
            s = tok.upper()
            if s.startswith("HK.") and s[3:].isdigit() and len(s[3:]) < 5:
                s = "HK." + s[3:].zfill(5)   # 与 pool_claim._norm_sym 同口径补前导 0
            syms_in_cmd.add(s)
    if not syms_in_cmd:
        return None   # 命令里没有带市场前缀的标的 token：不是采样段，不拦

    # 层1 跨池冲突
    cross = sorted((s, h) for s, h in taken.items() if s in syms_in_cmd and h and h != sid)
    # 层2 本会话未认领（认领表里没有任何本会话条目 = 从没跑过 claim）。
    # 仅当存在活认领（taken 非空）时生效——认领制停用后残留的空认领表不拦单会话。
    no_claim = bool(taken) and sid not in {c.get("session", "") for c in alive_claims}
    # 层3 孤儿标的：认领表非空 + 有标的无人认领
    orphan = sorted(s for s in syms_in_cmd if s not in taken) if taken else []

    if not cross and not no_claim and not orphan:
        return None

    tip = (
        f"⚠️ 密采样守卫阻断（标的池互斥，2026-08-19 立；2026-08-20 修管道绕过 + 扩三层）："
    )
    if cross:
        detail = "；".join(f"{s}（会话 [{h[:8]}…]）" for s, h in cross)
        tip += (
            f"\n  ① 跨池冲突：{detail} 已被其它会话认领——多会话并行盯盘各盯各池，"
            f"不跨池抢标的（重复盯 = 重复信号 / 浪费会话容量）。"
        )
    if no_claim:
        tip += (
            f"\n  ② 本会话未认领：还没跑 pool_claim.py claim 就启动采样段——认领是采样的"
            f"前置（2026-08-20 港股实录：signal 会话跳过认领直接采样，与 auto 会话重复盯"
            f"一小时无人拦）。启动序列：方向研判定候选 → claim → 采样。"
        )
    if orphan:
        tip += (
            f"\n  ③ 未认领标的：{', '.join(orphan)} 没有被任何会话认领（含本会话）——"
            f"换标的 / 加标的前先 claim（未占才可用），不先斩后奏。"
        )
    tip += (
        f"\n处理：先跑 `python3 .claude/skills/trade/scripts/pool_claim.py claim <候选标的逗号分隔>`"
        f" 认领本会话标的池（已被占的会返回 ❌ 冲突、从候选去掉），再启动采样段；"
        f"该标的确需换主盯时先让持有会话跑 `pool_claim.py release <标的>` 释放、本会话再 claim；"
        f"无划分需求时删除 tmp/pool_claims.json 关闭本检查。"
    )
    return tip


def _claim_holder_alive(sid):
    """认领持有会话是否活着（同 pool_claim._session_alive 口径，hook 侧只读版）。

    会话 transcript 路径按宿主区分（2026-09-02 双宿主适配，hooks 同时挂 CC 与 ZCode）：
    - CC 会话（sid 为裸 UUID）→ ~/.claude/projects/<slug>/<sid>.jsonl（slug 生成同 pool_claim）；
    - ZCode 会话（sid 带 sess_ 前缀）→ ~/.zcode/cli/rollout/model-io-<sid>.jsonl（ZCode 的
      会话持久化 jsonl，活跃会话持续追加，与 CC jsonl 同构）。
    两套会话可并行认领互认活死；停更 ≤30 分钟 = 活着；文件不存在 / 停更超阈 = 已结束
    （其认领视为已释放，不占坑）。
    """
    if not sid:
        return False
    if sid.startswith("sess_"):
        candidates = [os.path.expanduser(os.path.join(
            "~/.zcode/cli/rollout", f"model-io-{sid}.jsonl"))]
    else:
        slug = "-" + _PROJECT_ROOT.strip(os.sep).replace(os.sep, "-")
        candidates = [os.path.expanduser(os.path.join(
            "~/.claude/projects", slug, sid + ".jsonl"))]
    try:
        return any(os.path.isfile(p) and (time.time() - os.path.getmtime(p)) <= 30 * 60
                   for p in candidates)
    except OSError:
        return False


def _is_monitoring_session(sid):
    """本会话是否盯盘会话（2026-09-02 立，stop 提醒门槛——非盯盘会话不提醒）。

    背景：守卫 stop 分支原来只判「盘中 + 无采样」，不看提醒对象是谁——美股盘中窗口
    （美东 04:00-16:00 = 北京 16:00-次日 04:00）里任何非盯盘会话（/commit、写代码、
    复盘）每个回合结束都触发提醒，2026-09-02 实录纯 commit 会话被无限唤醒 160+ 轮。

    判定（三判据任一命中 = 盯盘会话；全部只读、无副作用）：
    ① 会话在盯盘注册表 tmp/monitor_sessions.txt（preflight 港股盯盘注册、
       monitor_unregister.sh 停盯注销）；
    ② 当日认领表 tmp/pool_claims.json 的 claims 里有本 sid（认领 = 在盯标的）；
    ③ 当日开过仓：tmp/trade_intent.log 当日行含本 sid（开仓会话必然在盯盘）。
    另一前提：**今天全项目完全没有采样日志**（tmp/monitor_log_*_今日日期_*.csv 一个都
    没有）= 今天没有任何会话开过盯盘 → 直接 False（没盯过就不存在「断了」）。

    局限（诚实）：③ 只认「开过仓」，纯信号盯盘没开仓、且既没注册（美股会话按
    2026-08-12 规矩不注册）也没认领（claim 是港股池概念）的美股盯盘会话会被漏拦——
    该场景由 pretool 分支的密采样阻断与用户监督兜底；stop 误扰的代价（死循环）远
    大于这个窄缝的漏提醒，权衡后收窄。
    """
    try:
        import glob as _glob
        today = datetime.now().strftime("%Y%m%d")
        # 前提：今天没有任何采样日志 → 没有会话开过盯盘
        if not _glob.glob(os.path.join(TMP_DIR, f"monitor_log_*_{today}_*.csv")):
            return False
        if not sid:
            return False
        # ① 盯盘注册表
        reg = os.path.join(TMP_DIR, "monitor_sessions.txt")
        if os.path.isfile(reg):
            try:
                with open(reg) as f:
                    if sid in f.read().splitlines():
                        return True
            except OSError:
                pass
        # ② 当日认领表
        claims = os.path.join(TMP_DIR, "pool_claims.json")
        if os.path.isfile(claims):
            try:
                with open(claims) as f:
                    data = json.load(f)
                if data.get("date") == datetime.now().strftime("%Y-%m-%d"):
                    if any(c.get("session") == sid for c in data.get("claims", [])):
                        return True
            except Exception:
                pass
        # ③ 当日开仓 intent
        intent = os.path.join(TMP_DIR, "trade_intent.log")
        if os.path.isfile(intent):
            try:
                today_dash = datetime.now().strftime("%Y-%m-%d")
                with open(intent) as f:
                    for line in f:
                        line = line.strip()
                        if not line or today_dash not in line:
                            continue
                        try:
                            rec = json.loads(line)
                        except Exception:
                            continue
                        if rec.get("session_id") == sid:
                            return True
            except OSError:
                pass
        return False
    except Exception:
        # 判定链任何异常 → 保守按盯盘会话处理（宁可多提醒一次，不漏拦真断采样）
        return True


def _emit_reminder(msg, hook_event_name):
    """提醒类输出（不阻断）的双宿主适配（2026-09-02 ZCode hook 迁移立）。

    三通道并发，两宿主各取所需：
    - stderr 文本：CC 与 ZCode 对 exit 0 的 stderr 处理不同（CC 可见、ZCode 忽略），
      保留给 CC 的原提醒通道；
    - stdout JSON hookSpecificOutput：**两宿主官方支持的注入通道**——CC 源码实证每个事件
      的 hookSpecificOutput 都有 additionalContext 字段（Stop 事件描述明说「non-error
      feedback delivered to the model; the conversation continues」），ZCode 的 schema
      同构支持（discriminatedUnion 各 case 均含 additionalContext）。
    注意 hookEventName 必须与真实事件一致（两宿主都强校验：CC/ZCode 不匹配即拒绝），
    故由调用方按子命令传入（stop → Stop、taskstop → PreToolUse）。
    """
    print(msg, file=sys.stderr)
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": hook_event_name,
            "additionalContext": msg,
        }
    }, ensure_ascii=False))


def main():
    hook_type = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        payload = json.load(sys.stdin) or {}
    except Exception:
        payload = {}

    # 盘外不干预（周末 / 夜间 / 午休）
    if not in_trading_session():
        sys.exit(0)

    running, why = monitor_segment_running()

    if hook_type == "pretool":
        tool_input = payload.get("tool_input") or {}
        command = tool_input.get("command", "") if isinstance(tool_input, dict) else ""
        # 会话标识优先取 payload（hook stdin JSON 的 session_id，实测与环境变量一致且更稳），
        # 拿不到再退环境变量——避免个别环境变量未注入时互斥检查整个静默失效（2026-08-20）。
        sid = payload.get("session_id") or os.environ.get("CLAUDE_CODE_SESSION_ID", "")
        # A4（2026-08-05 立；2026-08-16 扩检测面 + 修反向误伤）：单条 Bash 命令连跑多个
        # 密采样段（&& / ; / || 串联）= 等效降频——段间 AI 不醒来分析、段结束通知只在全部段
        # 跑完后触发一次。原实现只数 monitor_segment.py 子串次数：① 2026-08-07 起港股主力
        # 采样已是 ws_segment.py、&& 连跑不被拦；② 反向误伤——同一命令里两次提及文件名但
        # 非连跑（如 `py_compile monitor_segment.py ws_segment.py`）也被拦。现按 shell 串联
        # 操作符（&& ; || 换行）切分子命令、只数「真正独立执行了采样段」的子命令数 ≥2。
        # 密采样唯一合法循环 = 单段 40 秒 → 段结束通知唤醒 AI 分析 → 重启下一段。
        import re as _re
        subcmds = _re.split(r"&&|\|\||;|\n", command)
        seg_subcmds = [sc for sc in subcmds
                       if any(s in sc for s in ("monitor_segment.py", "ws_segment.py",
                                                "futu_ws_segment.py"))]
        # 子命令级再排除「提及但不执行」：子命令须像一次执行（python3 … 脚本名），
        # py_compile / cat / diff / grep 等工具引用脚本文件不算执行采样。
        # ⚠️ 排除词只看**管道首段**（2026-08-20 修）：真实采样命令几乎都带输出管道
        # `…segment.py 40 HK.… 2>&1 | grep -vE '…'`——旧实现整条子命令匹配排除词，
        # 管道右侧的 grep 把**正在执行的采样段**误当「提及未执行」剔出 exec_seg，
        # 导致 A4 连跑 / 跑法 / 空转 / 标的池互斥四项检查对管道形态命令全部失明
        # （2026-08-20 港股实录：signal 与 auto 会话重复盯一小时、0 拦截）。
        # 判定语义：命令的**执行体**在管道首段（python3 … segment.py …），
        # 尾部的 grep / head 等只处理输出、不是执行体本身——排除词查首段即可；
        # 首段干净（python3 执行）+ 任意输出管道 = 正在执行采样段，必须算数。
        def _is_exec_seg(sc):
            head = sc.split("|", 1)[0]   # 管道首段 = 执行体
            if not _re.search(r"(^|\s|/)python3?\s", head) or "py_compile" in sc:
                return False
            return not _re.search(r"\b(cat|head|tail|diff|grep|ls|rm|mv|cp|less|more)\b", head)

        exec_seg = [sc for sc in seg_subcmds if _is_exec_seg(sc)]
        # 跑法拦截（2026-08-18 立）：采样段用 nohup 后台 / 尾接 sleep = 通知驱动机制被绕过。
        # 合法跑法只有一种——run_in_background 参数（hook 侧表现为普通前台命令交给 harness
        # 后台化，命令文本里不该出现 nohup/&/sleep 拼接）。误伤排查：`&` 单字符检测会误伤
        # 无关注释，故只查三种模式：nohup + 段脚本、`段脚本 … &`（行尾 &）、`段脚本 … ; sleep`/`&& sleep`。
        bad_patterns = []
        for sc in exec_seg:
            if "nohup" in sc:
                bad_patterns.append(f"nohup 后台跑段：{sc.strip()[:80]}")
            if _re.search(r"futu_ws_segment\.py.*\d\s*&\s*$|monitor_segment\.py.*\d\s*&\s*$|ws_segment\.py.*\d\s*&\s*$", sc.strip()):
                bad_patterns.append(f"行尾 & 后台挂段：{sc.strip()[:80]}")
            if _re.search(r"sleep\s+\d+", sc) and any(s in sc for s in ("futu_ws_segment", "monitor_segment", "ws_segment")):
                bad_patterns.append(f"段命令内拼 sleep：{sc.strip()[:80]}")
        if bad_patterns:
            msg = (
                f"⚠️ 密采样守卫阻断（跑法拦截，2026-08-18 立）：检测到绕过通知驱动的采样跑法——"
                f"{'；'.join(bad_patterns)}。段启动必须用 run_in_background（工具参数）+ 段结束"
                f"task-notification 唤醒 AI 即刻分析，禁止 nohup/&/sleep 轮询自驱"
                f"（2026-08-18 实录：nohup+sleep 致数据滞后 40-60 秒、错过入场判定；"
                f"run_in_background 滞后实测 ≤11 秒）。切数据源时只换脚本名，不换启动方式。"
            )
            print(msg, file=sys.stderr)
            sys.exit(2)
        if len(exec_seg) >= 2:
            msg = (
                f"⚠️ 密采样守卫阻断：单条 Bash 命令串联执行 {len(exec_seg)} 个密采样段"
                f"（&&/;/|| 连跑多段）。连跑 = 段间 AI 不醒来分析 = 等效降频，违反密采样规定（2026-08-05 教训："
                f"AI 误把段结束进程归 0 当断链、用 && 连跑 4 段减少断链点，被用户纠正——段结束进程归 0 本就正常，"
                f"连跑才是故障）。请改为单段调用，靠段结束通知驱动循环。"
            )
            print(msg, file=sys.stderr)
            sys.exit(2)  # PreToolUse exit 2 = 阻断 + stderr 反馈给 AI
        # 空转硬门（2026-08-17 立红灯「盯盘空转」修法④）：命令要重启密采样段（恰好 1 个
        # exec_seg）时检查分析心跳——心跳停更 > 3 分钟 = 分析链死了还只顾重启采样，阻断，
        # 逼 AI 先给「一行式判断 + 写心跳」再重启。刚启动（无心跳且采样 log < 3 分钟）放行。
        if len(exec_seg) == 1:
            bstate, bage = analysis_beat_status()
            if bstate == "stale":
                print(
                    f"⚠️ 密采样守卫阻断（空转防护）：检测到重启密采样，但分析心跳已停更 "
                    f"{int(bage)} 秒（> {ANALYSIS_BEAT_STALE_SECONDS}s）——采样链活着、分析链死了"
                    f"（2026-08-17 空转实录形态）。先补做本段分析：① 用一行式模板给判断"
                    f"（现价/关键位/VWAP/结论/下次段时间）；② 追加分析心跳 "
                    f"echo \"$(date '+%H:%M:%S'),<标的>,<判断>\" >> tmp/analysis_beat_{datetime.now().strftime('%Y%m%d')}_<mode>.csv；"
                    f"③ 再重启下一段。模板见 references/monitoring.md「每段最小输出模板 + 分析心跳」。",
                    file=sys.stderr,
                )
                sys.exit(2)
            if bstate == "none":
                run_sec = sampling_log_run_seconds()
                if run_sec is not None and run_sec >= ANALYSIS_BEAT_STALE_SECONDS:
                    print(
                        f"⚠️ 密采样守卫阻断（空转防护）：当天采样已运行约 {int(run_sec)} 秒，"
                        f"但分析心跳为零（analysis_beat 文件不存在）——只采样、从未分析"
                        f"（2026-08-17 空转实录形态）。先给本段一行式判断并写分析心跳"
                        f"（tmp/analysis_beat_YYYYMMDD_<mode>.csv），再重启下一段。"
                        f"模板见 references/monitoring.md「每段最小输出模板 + 分析心跳」。",
                        file=sys.stderr,
                    )
                    sys.exit(2)
        target = ""
        if "snapshot.py" in command:
            target = "snapshot"
        elif "hot_list.py" in command:
            target = "hot_list"
        if target and not running:
            msg = (
                f"⚠️ 密采样守卫阻断：盘中调 {target}，但密采样未在跑（{why}）。"
                f"盯盘密采样的唯一入口是 monitor_segment / ws_segment / futu_ws_segment 40 秒循环，"
                f"禁用 snapshot/hot_list 替代（2026-08-04 违规教训：曾用 cron+snapshot 绕过降频）。"
                f"请先重启密采样循环，再在循环内做开仓前刷新。"
            )
            print(msg, file=sys.stderr)
            sys.exit(2)  # PreToolUse exit 2 = 阻断 + stderr 反馈给 AI
        # 标的池互斥拦截（2026-08-19 立；2026-08-20 扩三层）：启动密采样段时查
        # 跨池冲突 / 本会话未认领 / 未认领标的（三层语义见 _pool_conflict docstring）。
        # 认领表见 scripts/pool_claim.py（tmp/pool_claims.json）。多会话并行盯盘互斥划分的
        # 硬执行点——散文约定（不跨池抢标的）靠 AI 记忆会衰减，hook 阻断才拦得住
        # （同「文档规定必须尽可能配工具强制」）。无认领表文件 / 无本会话 sid 时跳过
        # （单会话场景无池概念，不误伤）。
        if exec_seg:
            _pool_block = _pool_conflict(exec_seg, sid)
            if _pool_block:
                print(_pool_block, file=sys.stderr)
                sys.exit(2)
        sys.exit(0)

    if hook_type == "stop":
        # 只提醒盯盘会话（2026-09-02 立，修 160+ 轮死循环实录）：守卫原来只判「盘中 +
        # 无采样」，不判「本会话是不是盯盘会话」——美股盘中窗口（美东 04:00-16:00 = 北京
        # 16:00-次日 04:00）里任何非盯盘会话（如纯 /commit、写代码、复盘会话）每个回合
        # 结束都会触发一次提醒 → 提醒注入上下文唤醒模型 → 模型回复 → 回合又结束 → 再提醒，
        # 无限循环。修复 = 加「盯盘会话」门槛，三个判据任一命中才提醒（都只读、无副作用）：
        # ① 本会话在盯盘注册表（tmp/monitor_sessions.txt，preflight 港股盯盘时写入、
        #    monitor_unregister.sh 停盯注销）——注册过 = 盯盘会话；
        # ② 本会话当日认领过标的（tmp/pool_claims.json 当日 claims 里有本 sid）——
        #    认领 = 在盯标的 = 盯盘会话；
        # ③ 采样日志今天有本会话会话标记（monitor_log_*_{sid8}*.csv）——写过采样日志
        #    = 跑过密采样 = 盯盘会话。
        # 都不命中 = 非盯盘会话，静默退出（AI 不自主启动盯盘，盘中开盯的会话必然先走
        # preflight 注册 / claim 认领 / 写采样日志之一，不会漏拦真盯盘会话的断采样）。
        sid = payload.get("session_id") or os.environ.get("CLAUDE_CODE_SESSION_ID", "")
        if not running and _is_monitoring_session(sid):
            msg = (
                f"⚠️ 密采样守卫提醒：回合结束，盘中但密采样未在跑（{why}）。"
                f"盯盘期间必须保持 monitor_segment / ws_segment 40 秒密采样循环（不得擅自停/降频，2026-08-04 教训）。"
                f"请立即重启密采样恢复密盯，或确认已到停盯边界（港股 12:00/16:00、用户喊停）。"
            )
            _emit_reminder(msg, "Stop")  # Stop 提醒（stderr 原通道 + hookSpecificOutput 双宿主注入）
        sys.exit(0)

    if hook_type == "taskstop":
        # A2（2026-08-04）：TaskStop 提醒（不阻断，避免误伤停出错进程）——
        # 盘中 + monitor_segment 在跑时，提醒 AI 确认停的不是密采样（hook 拿不到 task 命令，用「在跑」代理判断）。
        if running:
            msg = (
                f"⚠️ 密采样守卫提醒：盘中 TaskStop 后台任务，且 monitor_segment 正在跑（{why}）。"
                f"若要停的是 monitor_segment 密采样 = 违规（2026-08-04 教训：盯盘期间不得擅自停密采样），"
                f"除非已到停盯边界（港股 12:00/16:00、用户喊停）。停盯走 trade skill 停盯流程，勿直接 TaskStop 密采样。"
            )
            _emit_reminder(msg, "PreToolUse")  # PreToolUse/TaskStop 提醒（同上，事件名按真实事件传）
        sys.exit(0)

    sys.exit(0)


if __name__ == "__main__":
    main()
