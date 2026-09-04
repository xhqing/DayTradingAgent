#!/usr/bin/env python3
"""盯盘开盘前检查：当前时间 / 港股美股时段 / 富途 OpenD。
用法: python3 preflight.py
一行汇总就绪状态,避免开盘才发现数据源掉线或时间误判。"""
import datetime, socket

now = datetime.datetime.now()
hhmm = now.hour * 60 + now.minute
wd = now.weekday()  # 0=Mon..6=Sun
print(f"⏰ {now.strftime('%Y-%m-%d %H:%M:%S %A')}")

# 港股时段 (UTC+8, 与北京同时区)
if wd >= 5:
    hk = "周末休市"
elif 570 <= hhmm < 720:
    hk = "港股早市 09:30-12:00"
elif 780 <= hhmm < 960:
    hk = "港股午市 13:00-16:00"
else:
    hk = "盘外"
# 美股时段（2026-08-18 修订：美股可交易窗口扩为「盘前 + 盘中」= 美东 04:00-16:00——
#   盘前 04:00-09:30 与盘中 09:30-16:00 都可开仓 / 平仓 / 移损 / 发信号，16:00 收盘边界不变；
#   2026-07-19 旧规「仅盘中 09:30-16:00」废止。换算北京时间随美东夏/冬令时切换：
#     夏令时 EDT(UTC-4)：美东 04:00-16:00 = 北京 16:00-次日 04:00（跨午夜）
#     冬令时 EST(UTC-5)：美东 04:00-16:00 = 北京 17:00-次日 05:00（跨午夜）
#   直接用 zoneinfo 把当前时间转到美东时区判「周末 / 时段 / DST」，不再用北京本地反推——
#   冬令时盘中跨午夜段（北京周六凌晨 00:00-05:00 实为美东周五盘中）不会被本地周末判断误伤。
#   zoneinfo 不可用时回退北京夏令时估并标注。）
def _us_status():
    try:
        from zoneinfo import ZoneInfo
        us_now = now.astimezone(ZoneInfo("America/New_York"))
    except Exception:
        # zoneinfo 不可用：回退北京本地估夏令时（仅兜底，冬令时段可能不准）
        if wd >= 5:
            return "周末休市"
        # 夏令时估：美东 04:00-16:00 = 北京 16:00-次日 04:00（跨午夜）
        if 960 <= hhmm < 1440 or hhmm < 240:
            return "美股可交易(夏令时估·zoneinfo不可用)·盘前+盘中均可(美东04:00-16:00)"
        return "美股盘外(夏令时估·zoneinfo不可用)·不发信号"
    us_wd = us_now.weekday()
    us_hhmm = us_now.hour * 60 + us_now.minute
    tz_tag = "EDT夏令时" if bool(us_now.dst()) else "EST冬令时"
    if us_wd >= 5:
        return "周末休市"
    if 240 <= us_hhmm < 960:  # 美东 04:00-16:00（盘前 04:00-09:30 + 盘中 09:30-16:00）
        phase = "盘前" if us_hhmm < 570 else "盘中"
        return f"美股可交易({phase}·{tz_tag})·可发信号(美东04:00-16:00，盯到用户喊停或收盘)"
    return f"美股盘外({tz_tag})·不发信号"

us = _us_status()
print(f"📈 港股:{hk} | 美股:{us}")

# 富途 OpenD 端口（盯盘行情主力源，须登录成功才监听 11111）
def port_open(p):
    s = socket.socket(); s.settimeout(1)
    try:
        s.connect(('127.0.0.1', p)); s.close(); return True
    except Exception:
        return False
print(f"📊 富途OpenD:11111 {'✅' if port_open(11111) else '❌(未登录/未启动)'}")

# 风险比例 + 权益（按模式取 equity：auto 走账户 API、signal 走 equity-log；2026-08-01 双模式重构）
import json as _json, os as _os, sys as _sys
try:
    _sys.path.insert(0, _os.path.dirname(__file__))
    from trade_utils_tiger import load_equity as _le, parse_mode as _pm
    _mode = _pm()
    # 账户选择（2026-08-20 立，实盘盯盘配套；2026-09-03 补模拟盘对齐实盘）：--account live
    # 切实盘账户取净值——2026-08-20 事故：实盘盯盘时 preflight 固定走默认模拟账户（约 789 万 HKD）
    # 算 B、实盘净值远小于模拟净值（量级见本机实查）、偏差 68 倍。修复 = 取净值与下单同账户
    # （load_equity 透传）。2026-09-03：auto + 模拟盘账户（默认 / --account paper）时，算仓位的
    # equity 恒取实盘口径（当日实盘参考快照，load_equity 已改）；快照缺失/非当日 fail-closed
    # （返回 None，触发下方 🚨 警示 + 自动尝试刷新）。
    _acct = None
    for _i, _a in enumerate(_sys.argv[1:]):
        if _a == '--account' and _i + 1 < len(_sys.argv[1:]):
            _acct = _sys.argv[1:][_i + 1].lower()
            _acct = _acct if _acct in ('live', 'paper') else None
    _cfg_path = _os.path.join(_os.path.dirname(__file__), '..', 'config.json')
    with open(_cfg_path) as _f:
        _risk = _json.load(_f).get('risk', {})
    _frac = _risk.get('risk_fraction'); _fmax = _risk.get('f_max', _frac)
    _lev = _risk.get('max_leverage', 10)
    _eq_now, _cur, _eq_src = _le(_mode, account=_acct)
    # auto 模式实盘参考快照联动（2026-09-03 立，auto 模拟盘恒开对齐实盘）：
    #   - 实盘会话（--account live）：顺手把实盘总资产/购买力刷成当日快照，供同日 auto 模拟盘
    #     会话算仓位用（失败仅静默，不阻断实盘会话——快照刷新不依赖本次是否成功）。
    #   - 模拟盘会话（默认/--account paper）：显示 B 用的是实盘口径（load_equity 已改）；快照
    #     缺失/非当日时自动尝试刷新一次，仍失败则打印指引（开仓会被拒 fail-closed）。
    if _mode == "auto":
        try:
            from trade_utils_tiger import fetch_live_reference as _flr
            from trade_utils_tiger import read_live_reference as _rlr
            from trade_utils_tiger import is_live_reference_fresh as _ilf
            if _acct == "live":
                # 实盘会话：快照缺失 / 非当日才刷（当天已刷过就不再打实盘 API）。
                _rl = _rlr()
                if _rl is None or not _ilf(_rl):
                    _ok_r, _msg_r = _flr(verbose=False)
                    if _ok_r:
                        print(f"🪞 实盘参考快照已刷新（供 auto 模拟盘对齐实盘，取数 {_msg_r.get('fetched_at')}）")
            else:
                if _eq_now is None:
                    print(f"🚨 auto 模拟盘算仓位须用实盘口径（2026-09-03 恒开）：{_eq_src}")
                    print(f"   刷新前模拟盘开仓会被拒（blocked_by: live_reference_required）——"
                          f"在已实盘解锁的会话执行 python3 scripts/trade_utils_tiger.py "
                          f"--refresh-live-reference 即可。")
                    # 快照缺失/非当日时自动尝试刷新一次：本会话恰有实盘解锁则直接成功
                    # （省一次手动命令）；无解锁则失败（含解锁指引）、开仓仍会被拒。
                    _ok_r, _msg_r = _flr(verbose=False)
                    if _ok_r:
                        print(f"🪞 实盘参考快照已刷新（取数 {_msg_r.get('fetched_at')}）")
                        _eq_now, _cur, _eq_src = _le(_mode, account=_acct)
        except Exception as _er:
            print(f"⚠️ 实盘参考快照联动检查失败（{_er}）")
    if _frac is not None and _eq_now is not None:
        _M = _frac * _eq_now
        print(f"💰 模式 {_mode} | 风险比例 {_frac*100:.1f}% × 当前 equity {_eq_now:,.2f} {_cur} = 单笔预算 B {_M:,.2f}（f_max 硬上限 {_fmax*100:.1f}%，max_loss 不得突破）")
        print(f"   equity 来源：{_eq_src}")
        print(f"⚖️  开仓市值上限 = equity × {_lev} 倍杠杆 = {_eq_now * _lev:,.2f} {_cur}（权益 {_eq_now:,.0f} → 最高开仓 {_eq_now * _lev:,.0f} 市值；选仓位时 max_loss 与市值两约束同时满足）")
    elif _mode == "auto" and _acct != "live" and _eq_now is None:
        # auto 模拟盘且实盘快照取不到：B 行已在上面 🚨 警示里说明原因，这里不再重复打。
        pass
    else:
        print(f"💰 ⚠️ config 缺 risk_fraction，盘中算仓位前务必手动确认")
except Exception as _e:
    print(f"💰 ⚠️ 读取 config/equity 失败({_e})，盘中算仓位前务必手动确认 risk.risk_fraction")

# positions 检查已移除（2026-07-15 信号模式：假设执行、不查 positions，见 SKILL「信号模式总则」第 1 条）

# 防系统睡眠（2026-07-25 立 → 2026-07-27 修订：无条件自动启用，取代旧「检测电源 + 弹窗建议启用 keep-awake」）。
# 根因：盯盘期间系统睡眠会暂停所有进程——富途 OpenD 的 get_market_snapshot 无 timeout、卡到 TCP 超时
# ~15 分钟才返回、整段采样空窗；claude-proxy、xpilot 同断。故盯盘预热（preflight）无条件启用
# caffeinate -s（创建 PreventSystemSleep assertion、防合盖 Clamshell 与维护 Maintenance 两类系统级睡眠），
# 不再询问开盖/合盖、不再弹窗建议「启用合盖盯盘」（防睡眠已并入 trade，原 keep-awake skill
# 2026-09-01 撤销；开盖盯盘无所谓电池/电源、电池下合盖是硬件强制软件防不住但防空闲维护睡眠仍有效，
# 故统一启用、不提醒）。停止盯盘时由 trade 停盯流程调 scripts/keepawake_off.sh 解除。
def _ensure_awake():
    import subprocess as _sp, time as _t
    if _sp.run(["pgrep", "-f", "caffeinate -s"], stdout=_sp.DEVNULL).returncode == 0:
        print("☕ caffeinate -s 已在跑（盯盘防系统睡眠；停盯时自动解除）")
        return
    try:
        _sp.Popen(["caffeinate", "-s"], stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
        _t.sleep(0.5)
        if _sp.run(["pgrep", "-f", "caffeinate -s"], stdout=_sp.DEVNULL).returncode == 0:
            print("☕ caffeinate -s 已启动（盯盘防系统睡眠；停盯时自动解除）")
        else:
            print("⚠️ caffeinate 启动失败（盯盘期间注意别让系统睡眠）")
    except Exception as _e:
        print(f"⚠️ 启用防睡眠失败（{_e}）（盯盘期间注意别让系统睡眠）")

_ensure_awake()

# D12（2026-08-04）：盯盘启动密采样入口提醒——防 AI 用 cron / 直接 snapshot 绕过 monitor_segment 降频
# （2026-08-04 教训，多层防护见 monitoring.md「不因市况降频」节 + .claude/hooks/monitor_guard.py）。
print("🔒 密采样提醒：盯盘密采样唯一入口是 monitor_segment.py 40 秒循环，禁用 cron / 直接 snapshot 替代。")

# 老虎 IP 白名单守护（2026-08-19 立，工具强制）：当前节点出口漂出白名单时自动切回白名单内
# 节点（preflight 时点就把服务恢复好，不等盘中断 API）；节点 IP 有变化时响铃弹窗给加白串、
# 用户在老虎页加白后点「已添加」自动同步本地 config（按钮确认制，详见 scripts/proxy_guard.py
# 头注释）。挂在 preflight = 每次盯盘启动必过这道闸（monitor_watcher 另在盘中每 10 秒兜底）。
# 超时保护：guard 含连通验证（switch 60s + test 45s/候选、总预算 90s），timeout=110 兜住。
try:
    import subprocess as _sp2
    _r = _sp2.run(["python3", _os.path.join(_os.path.dirname(__file__), "proxy_guard.py")],
                  capture_output=True, text=True, timeout=110)
    _out = (_r.stdout or "").strip()
    if _out:
        print(_out)
    if _r.returncode == 2:
        print("🚨 白名单全漂出：盯盘前须先人工去老虎开发者页加白（弹窗已给 IP 串），否则老虎 API 全断")
    elif _r.returncode == 3:
        print("⚠️ proxy_guard 配置缺失（tiger_whitelist / nodes.json），白名单比对未生效")
except Exception as _e:
    print(f"⚠️ 白名单守护检查失败（{_e}）——手动跑 python3 scripts/proxy_guard.py 确认")

# 交易互斥状态（2026-08-17 立，多会话并行盯盘·方案 A）：启动时一眼看到「谁在场」——
# intent 日志有无 pending（有 = 后到开仓会被拒，先处理）、常驻白名单、在场注册会话数。
try:
    from trade_mutex import pending_intents, resident_positions as _rp
    _pend = pending_intents()
    if _pend:
        print(f"⚠️ 互斥状态：intent 有 {len(_pend)} 条 pending（后到开仓会被拒）——先查当日订单确认无在途单，"
              f"再 python3 scripts/trade_mutex.py --clear-intent <行号> 清掉")
    else:
        print("🔒 互斥状态：intent 链干净（开仓闸门放行）；单持仓由 trade_mutex 强制（多会话先到先得）")
    _rp_list = _rp()
    if _rp_list:
        print(f"📌 常驻持仓白名单（不进开仓链）：{_rp_list}")
except Exception as _e:
    print(f"🔒 互斥状态：读取失败（{_e}）——trade_mutex.py status 手动确认")
try:
    _reg = _os.path.abspath(_os.path.join(_os.path.dirname(__file__), "..", "..", "..", "..", "tmp", "monitor_sessions.txt"))
    if _os.path.isfile(_reg):
        _n = sum(1 for _l in open(_reg) if _l.strip())
        print(f"👀 在场盯盘会话数：{_n}（含本会话；多会话并行时 caffeinate 引用计数、最后停盯才解除防睡眠）")
except Exception:
    pass

# 标的池划分状态（2026-08-19 立，TODO「标的池自动划分」）：多会话并行时启动序列内
# 方向研判定出候选池后跑 `python3 scripts/pool_claim.py claim <候选>` 认领（互斥划分，
# 同一标的不落两会话）；此处打印当前全局划分让 AI / 用户启动即见「谁盯哪些」。
# 停盯收尾跑 release --all 释放（monitor_unregister.sh 已联动，但显式跑一次更稳）。
# 2026-08-20 起认领是无条件步骤（不再只「多会话并行时」）——signal / auto 同规，
# 认领表有活认领时不认领会被 monitor_guard 拦（当日港股实录：signal 会话跳过认领、
# 与 auto 会话重复盯一小时）。在场打印提醒见下（决策时刻工具在场层）。
try:
    import subprocess as _sp3
    _r3 = _sp3.run(["python3", _os.path.join(_os.path.dirname(__file__), "pool_claim.py"), "status"],
                   capture_output=True, text=True, timeout=10)
    _out3 = (_r3.stdout or "").strip()
    if _out3:
        print(_out3)
        if "无认领" not in _out3 and "本会话当前无认领" in _out3:
            print("⚠️ 表里有别人的认领、本会话还没认领——采样前先跑 "
                  "`python3 scripts/pool_claim.py claim <候选标的逗号分隔>`，"
                  "否则启动采样段会被 monitor_guard 拦（2026-08-20 立三层互斥）")
except Exception as _e3:
    print(f"🤝 标的池划分状态读取失败（{_e3}）——python3 scripts/pool_claim.py status 手动确认")

# 密采样守护 watcher 自动开启（2026-08-20 立；取代同日 09:58「默认不开启」决定——同日 11 时
# watcher 已删中断警报 + 掩蔽提示、收缩为「空转警报 + 老虎白名单漂移检测」两个低频警告后，
# 用户立：这两个低频警告不扰民，盯盘开始默认开启；2026-08-21 空转警报再删、仅剩白名单漂移
# 检测，默认开启不变）：确保 launchd 项 com.daytrading.monitor-watcher
# 处于 load 状态——已 load 跳过、未 load 自动 launchctl load（幂等，重复跑无副作用）。
# 盘外 watcher 自身会跳过（in_trading_session 判断），故这里无条件确保 load 也无妨。
# 停盯**不联动 unload**（watcher 只剩白名单漂移一个低频提醒、常驻无害；盘外自动静默），用户明确说
# 「关守卫 / unload watcher」时才手动卸载。
def _ensure_watcher():
    import subprocess as _sp
    _label = "com.daytrading.monitor-watcher"
    _ls = _sp.run(["launchctl", "list"], capture_output=True, text=True)
    if _label in (_ls.stdout or ""):
        print("🛡️ 密采样守护 watcher 已在跑（白名单漂移检测，低频不扰民）")
        return
    _plist = _os.path.expanduser(f"~/Library/LaunchAgents/{_label}.plist")
    if not _os.path.isfile(_plist):
        _src = _os.path.abspath(_os.path.join(_os.path.dirname(__file__), "..", "..", "..", "..",
                                             ".claude", "hooks", f"{_label}.plist"))
        if _os.path.isfile(_src):
            import shutil as _sh
            _sh.copyfile(_src, _plist)
            print(f"🛡️ 已部署 watcher plist（{_src} → {_plist}）")
        else:
            print(f"⚠️ watcher plist 不存在（{_src}），密采样守护未开启")
            return
    _r = _sp.run(["launchctl", "load", _plist], capture_output=True, text=True)
    if _r.returncode == 0:
        print("🛡️ 密采样守护 watcher 已开启（白名单漂移检测，低频不扰民；盘外自动静默）")
    else:
        print(f"⚠️ watcher load 失败（{_r.stderr.strip() or _r.stdout.strip()}）——手动跑 "
              f"launchctl load {_plist}")

_ensure_watcher()

# 盯盘会话注册（2026-08-11 立；2026-08-12 收窄为仅港股注册）：把本会话 CLAUDE_CODE_SESSION_ID
# 写入 tmp/monitor_sessions.txt，纳入密采样守护 watcher（launchd 每 10 秒）的守护范围。
# ⚠️ **仅港股盯盘注册、美股不注册**（2026-08-12 用户立）：watcher 的 in_trading_session()
# 只在港股盘中检查（美股时段用户休息不打扰），若美股会话也注册、第二天港股开盘 watcher 仍会
# 检查到它 → jsonl 停更 8 小时 > 阈值 → 一直报。故注册源头按市场收窄：只在港股盘中（含盘前
# 09:30 前 30 分钟预启动阶段，覆盖盘前预热场景）注册，美股 / 夜间 / 盘外一律跳过。
# 停盯时由 trade 停盯流程调 scripts/monitor_unregister.sh 注销（正常停盯不被误报）；
# 即便忘注销，watcher 的死会话自动剔除（jsonl 停更 > 30 分钟）也会兜底清理。
def _register_monitor_session():
    import os as _os
    import datetime as _dt
    _sid = _os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    if not _sid:
        return  # 非 Claude Code 会话内（如手动跑脚本）→ 跳过
    # 港股盘中判断（与 preflight 主流程的 hk 变量口径一致：周一至周五 09:30-12:00 / 13:00-16:00）。
    # 额外允许 09:00 起注册（盘前 30 分钟，覆盖盘前预热启动 monitor_segment 的场景）。
    _now = _dt.datetime.now()
    if _now.weekday() >= 5:
        print("👀 美股/盘外：watcher 不注册（watcher 仅港股盘中守护、美股时段不打扰用户）")
        return
    _t = _now.time()
    _hk_session = (_dt.time(9, 0) <= _t < _dt.time(12, 0)) or (_dt.time(13, 0) <= _t < _dt.time(16, 0))
    if not _hk_session:
        print("👀 美股/盘外：watcher 不注册（watcher 仅港股盘中守护、美股时段不打扰用户）")
        return
    _root = _os.path.abspath(_os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "..", "..", "..", ".."))
    _reg = _os.path.join(_root, "tmp", "monitor_sessions.txt")
    try:
        _os.makedirs(_os.path.dirname(_reg), exist_ok=True)
        _exists = _os.path.isfile(_reg) and _sid in open(_reg).read().splitlines()
        if not _exists:
            with open(_reg, "a") as _f:
                _f.write(_sid + "\n")
        print(f"👀 港股盯盘会话已注册（密采样 watcher 守护；停盯时自动注销）")
    except Exception as _e:
        print(f"⚠️ 盯盘会话注册失败（{_e}），watcher 不守护本会话")


_register_monitor_session()
