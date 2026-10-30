#!/usr/bin/env python3
"""本地可用 IP 池（2026-10-29 立，2026-10-30 按用户口径重构为「无白名单」版本）。

## 现在的口径（2026-10-30 用户立，取代此前的白名单体系）

1. **没有老虎白名单这回事了**：老虎开发者页的 IP 白名单已全部删除、不再维护、也不再提示
   加白。判定「能不能调老虎 API」只看一件事——**老虎网关放不放行**（网关探针）。
2. **池 = JMS 订阅里的全部节点**：本地 IP 池的 IP 全部来自 JMS 订阅（缓存
   `tmp/jms_subscription.cache`），不存在「本地白名单」这个第二数据源。
3. **盘中不拉订阅**：港/美可交易时段内禁止拉取 JMS 订阅（`jms_pool.fetch_subscription`
   直接返回 `blocked_intraday`，工具强制）。盘中只在本地池内做 fallback：某个 IP 的请求
   失败就换池内另一个端口重发。
4. **只在两个窗口刷新池**：
   - ① 收到盯盘指令时（`preflight.py` 启动，`allow_intraday=True`）；
   - ② 收盘后写总结之前（`stop_summary.py`，`allow_intraday=True`）。
5. **盘中池内全不可用 = JMS 代理服务稳定性问题**：归因到服务商，解法是换更稳定的代理服务；
   不在盘中反复重拉订阅（那只会更糟），也不存在「加白」这条出路。

## 判定手段

- **老虎网关探针**（最终真相）：无签名的无害请求，放行时回业务/参数类错误码，被判无权时回
  `access forbidden`。不需要凭证、约 200ms~2s。
- **实测出口 IP**：经本项目的代理端口打外部 IP 查询服务。必须实测——订阅里的 address 只是
  入口地址，实际出口可能因服务商 NAT / 多入口而不同。
- 端口可用性缓存（`tmp/ip_pool_ports.json`）：每个端口探一次「出口 + 网关放行」，供
  `fast_switch` 毫秒级挑端口。

## 切换计数（2026-10-30 用户立）

每次真实换端口都写一行到 `reviews/ip-switch-log.csv`（`log_switch()`），停盯总结据此报
「本场切换 IP N 次」，长期用来评估代理服务稳定性。

## 对外接口

    ensure(account='paper', quiet=False, allow_switch=True, budget=45) -> dict
        保证老虎链路可用。正常情况约 0.3~0.5s（一次网关探针 + 一次出口实测）；
        切换在本项目自己的端口池内换端口，毫秒级、不重启进程。

    CLI: python3 ip_pool.py [--quiet] [--json] [--no-switch] [--account paper|live]

退出码：0 = 可用；2 = 池内没有可用端口（归因 JMS 稳定性）；3 = 节点清单取不到。
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import proxy_guard as G   # 只复用公共设施（_print / subprocess / PROJECT_ROOT / TMP_DIR）；
                          # 该模块的白名单检测已停用（2026-10-30）

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = G.PROJECT_ROOT
TMP_DIR = G.TMP_DIR

# 老虎网关探针：无签名的无害请求。模拟盘走沙箱网关、实盘走生产网关（两者都按 IP 白名单
# 在连接层拦，实测 2026-10-29：放行时回 code=1000 url invalid，拒绝时回 code=4 access forbidden）。
TIGER_PROBE = {
    'paper': 'https://openapi-sandbox.tigerfintech.com/gateway/health',
    'live': 'https://openapi.tigerfintech.com/gateway/health',
}

# 出口 IP 实测源（按序尝试，任一成功即用）：多个源互为备份，避免单个服务不可达导致误判。
EGRESS_SOURCES = [
    'https://api.ipify.org',
    'https://ifconfig.me/ip',
    'https://ipinfo.io/ip',
]
EGRESS_TIMEOUT = 5        # 单个源超时（秒）
PROBE_TIMEOUT = 8         # 网关探针超时（秒）
SWITCH_TIMEOUT = 25       # 保留（历史常量，当前流程不再有「重启式切换」；勿删以免外部引用报错）
SWITCH_SETTLE = 1.5       # 切换后等 xray 就绪（秒）——立刻探测会打到还没起来的端口
ENSURE_BUDGET = 45        # ensure 总预算（秒）：下单前置可接受的最长等待；超预算即停下报失败
POOL_LOG = os.path.join(TMP_DIR, 'ip_pool.log')
# 结果缓存（2026-10-29 加）：下单前置不能每次都花 3~5s 实检（限价单挂盘口对价，延迟可能错过
# 成交）。采样段 / 空闲时后台跑实检刷新缓存，下单时用 --quick 读缓存（TTL 内直接用）。
# TTL 取 30s：出口 IP 漂移不是秒级事件（实测单节点漂移以分钟计），30s 足够新。
STATE_PATH = os.path.join(TMP_DIR, 'ip_pool_state.json')
QUICK_TTL = 30

# ──────────────────────────────────────────────────────────────────────────────
# IP 切换计数与持久化（2026-10-30 用户立）
#
# 目的：把「切换 IP」变成可统计的运行数据，用来评估**代理服务（JMS）的稳定性**。
# 每次真实换端口都追加一行到 `reviews/ip-switch-log.csv`（入库、append-only）：
#   · source=retry  → 请求级：某个 IP 请求失败，立刻换端口重发（_IPRetryClient，8ms 级）
#   · source=ensure_switch           → 体检级：ensure() 发现当前出口被拒/不通，池内换端口
#   · source=ensure_switch_after_resub → 池内全失败后重拉订阅、重建池再换（最后手段）
# 停盯总结按「会话 + 市场交易日」统计本场次数（stop_summary.py），可看出代理稳不稳。
# 写盘失败一律静默——计数是观察数据，绝不得影响交易链路。
# ──────────────────────────────────────────────────────────────────────────────
SWITCH_LOG = os.path.join(PROJECT_ROOT, 'reviews', 'ip-switch-log.csv')
SWITCH_LOG_HEADER = ('timestamp_utc,timestamp_hkt,timestamp_et,market,session_id,'
                     'source,from_port,to_port,from_node,to_node,reason')


def _session_id_short():
    """会话 id 前 8 位（与 actions 记录 / stop_summary 口径一致）。"""
    sid = (os.environ.get('CLAUDE_CODE_SESSION_ID')
           or os.environ.get('PI_SESSION_ID') or '')
    return sid[:8] if sid else ''


def _market_now():
    """当前时段对应的市场：hk / us / off（按 Beijing 时间粗判，只用于归类统计）。"""
    lt = time.localtime()
    hm = lt.tm_hour * 60 + lt.tm_min
    if lt.tm_wday >= 5:
        return 'off'
    if 9 * 60 + 30 <= hm < 12 * 60 or 13 * 60 <= hm < 16 * 60:
        return 'hk'
    # 美股：夏令时北京 16:00-次日 04:00 / 冬令时 17:00-次日 05:00（粗归，不影响交易）
    is_dst = time.localtime().tm_isdst if hasattr(time.localtime(), 'tm_isdst') else 0
    if hm >= (16 * 60 if is_dst else 17 * 60) or hm < (4 * 60 if is_dst else 5 * 60):
        return 'us'
    return 'off'


def log_switch(from_port, to_port, from_node, to_node, reason='', market=None, source=None):
    """追加一条 IP 切换记录（失败静默）。

    source 默认根据 reason 推：'retry' = 请求级换端口；其余归为体检级 ensure。
    """
    try:
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone.utc)
        src = source or ('retry' if reason in ('retry', 'request_retry') else 'ensure')
        row = [
            now.strftime('%Y-%m-%d %H:%M:%S'),
            now.astimezone(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S'),
            now.astimezone(timezone(timedelta(hours=-4))).strftime('%Y-%m-%d %H:%M:%S'),
            market or _market_now(),
            _session_id_short(),
            src,
            '' if from_port is None else int(from_port),
            '' if to_port is None else int(to_port),
            from_node or '',
            to_node or '',
            reason or '',
        ]
        os.makedirs(os.path.dirname(SWITCH_LOG), exist_ok=True)
        new = not os.path.exists(SWITCH_LOG)
        with open(SWITCH_LOG, 'a') as f:
            if new:
                f.write(SWITCH_LOG_HEADER + '\n')
            f.write(','.join(str(x) for x in row) + '\n')
    except Exception as e:
        _log(f'切换记录写盘失败（已忽略）: {e}')


def _log(line):
    """追加写 tmp/ip_pool.log（秒级时间戳 + 原因）。写失败静默——日志不是主路径。"""
    try:
        os.makedirs(TMP_DIR, exist_ok=True)
        with open(POOL_LOG, 'a') as f:
            f.write(f'{time.strftime("%Y-%m-%d %H:%M:%S")}\t{line}\n')
    except Exception:
        pass


def proxy_url():
    """读 config.json 的 proxy.http_proxy（本项目自己代理池的首个端口，默认 10871）。"""
    try:
        with open(G.CONFIG_PATH) as f:
            return json.load(f).get('proxy', {}).get('http_proxy', 'http://127.0.0.1:1087')
    except Exception:
        return 'http://127.0.0.1:1087'


def probe_egress(quiet=True, port=None):
    """实测当前代理出口 IP。返回 (ip_or_None, detail)。

    为什么必须实测：老虎按「实际出口 IP」判白名单；订阅里的 address 只是入口地址，实际
    出口可能因服务商 NAT / 多入口 / 负载均衡而不同。实测值才是判定依据。

    `port` 指定时按该本地端口实测（换端口后用它验证新端口），默认用 config 配置的端口。
    """
    px = f'http://127.0.0.1:{port}' if port else proxy_url()
    for src in EGRESS_SOURCES:
        try:
            r = G.subprocess.run(
                ['curl', '-s', '--max-time', str(EGRESS_TIMEOUT), '--proxy', px, src],
                capture_output=True, text=True, timeout=EGRESS_TIMEOUT + 3)
            body = (r.stdout or '').strip()
            if r.returncode == 0 and body and len(body) <= 64 and ' ' not in body:
                return body, f'source={src}'
        except Exception as e:
            if not quiet:
                G._print(f'  ⚠️ 出口探测源失败 {src}: {e}')
            continue
    return None, 'all_sources_failed'


def probe_tiger(account='paper', quiet=True, port=None):
    """老虎网关探针（判定「这个出口到底能不能调老虎 API」，无需凭证）。

    `port` 指定时按该本地端口探（探活池内每个端口用），不传则用当前代理端口。

    返回 (state, detail)：
      'ok'        = 网络层通过（网关回了业务/参数类错误码，说明请求被受理）
      'forbidden' = 被网关拒（access forbidden / not in ip whitelist）——
                    历史遗留：老虎侧白名单已于 2026-10-30 清空，现在基本不会再出现，
                    但保留识别以防服务方政策回调
      'unknown'   = 连不上 / 超时（代理挂了、节点挂了、断网）
    """
    url = TIGER_PROBE.get(account, TIGER_PROBE['paper'])
    px = f'http://127.0.0.1:{port}' if port else proxy_url()
    try:
        r = G.subprocess.run(
            ['curl', '-s', '--max-time', str(PROBE_TIMEOUT), '--proxy', px, url],
            capture_output=True, text=True, timeout=PROBE_TIMEOUT + 3)
        body = ((r.stdout or '') + (r.stderr or ''))
        low = body.lower()
        if 'access forbidden' in low or 'not in ip whitelist' in low:
            return 'forbidden', body.strip()[:200]
        if body.strip():
            # 任何有内容的网关响应（如 {"code":1000,"message":"common param error..."}）
            # 都说明请求已被网关受理 → 网络层通过。
            return 'ok', body.strip()[:160]
        return 'unknown', f'rc={r.returncode} empty_body'
    except Exception as e:
        return 'unknown', str(e)[:160]


def pool_candidates(nodes, exclude=()):
    """池内候选节点：全部 JMS 节点（不在 exclude 里就都算候选），vless 优先。

    2026-10-30 改：原先按「address ∈ 老虎白名单」筛候选，白名单功能已移除
    （老虎侧已清空白名单），候选就是 JMS 给的全部节点。
    """
    cands = [nid for nid in (nodes or {}) if nid not in exclude]
    return sorted(cands, key=lambda n: ((nodes[n].get('protocol') != 'vless'), n))


def _load_state(ttl):
    """读缓存状态；超过 ttl 秒或数据不全 → None。"""
    try:
        with open(STATE_PATH) as f:
            st = json.load(f)
        if time.time() - float(st.get('checked_at', 0)) > ttl:
            return None
        return st
    except Exception:
        return None


def _save_state(res):
    """写缓存状态（含 checked_at）。写失败静默——缓存不是主路径。"""
    try:
        os.makedirs(TMP_DIR, exist_ok=True)
        st = dict(res)
        st['checked_at'] = time.time()
        with open(STATE_PATH, 'w') as f:
            json.dump(st, f, ensure_ascii=False)
    except Exception:
        pass


def ensure(account='paper', quiet=False, allow_switch=True, budget=ENSURE_BUDGET,
           quick=False, apply_port=None):
    """保证老虎链路可用：出口 IP 落在老虎白名单池内、且老虎网关放行；不可用则换端口。

    **2026-10-29 重构（用户定位纠正）**：与 xpilot 彻底解耦——
      - IP 池来自本项目自己拉的 JMS 订阅（`jms_pool`），不读 xpilot 的 nodes.json；
      - 「切换」= 在**本项目自己起**的多端口池里换一个本地端口（10871+），毫秒级，
        不重启任何进程、不调 `xpilot switch`、不碰 xpilot 的任何配置；
      - 换完端口后由 `apply_port` 回调把老虎 SDK 的 ProxyManager 指过去（调用方注入，
        这样本模块不必知道 SDK 的存在）。

    返回 dict：
      ok        = True/False（True = 现在可以正常调老虎 API）
      action    = 'native' / 'switched'（已换到池内另一端口）/ 'cached' / 'failed'
      egress    = 实测出口 IP（可能 None）
      in_pool   = 出口是否在本地白名单镜像内（None = 没测出来）
      proxy_port= 建议使用的本地代理端口（调用方应把 SDK 指到这个端口）
      node      = 当前节点 id
      reason    = 失败原因
      tried     = 已尝试的端口
      elapsed   = 耗时（秒）
      mirror_stale = True 表示「老虎认、但本地镜像里没有这个 IP」→ 该去补镜像
    """
    t0 = time.time()

    # 快速路径：TTL 内缓存且 ok → 直接放行（下单前置走这条，省掉实检）
    if quick:
        st = _load_state(QUICK_TTL)
        if st and st.get('ok'):
            st['action'] = 'cached'
            st['elapsed'] = round(time.time() - t0, 3)
            return st

    out = {'ok': False, 'action': 'failed', 'egress': None, 'in_pool': None,
           'proxy_port': None, 'node': None, 'whitelist_gap': None, 'reason': None,
           'tried': [], 'elapsed': 0.0, 'mirror_stale': False}

    # ① 确保本地端口池在跑（节点源 = JMS 缓存；热路径绝不联网——2026-10-30 用户立）

    mapping = start_multi()
    if not mapping:
        out['reason'] = ('no_proxy_pool（xray 不可用或 JMS 订阅取不到节点）——'
                         '交易链路无法建立境外出口')
        _log('ensure 失败：' + out['reason'])
        out['elapsed'] = round(time.time() - t0, 2)
        return out

    # ② 实测当前出口 + 老虎网关
    egress, _eg_detail = probe_egress()
    state, detail = probe_tiger(account)
    out['egress'] = egress
    out['in_pool'] = None
    cur_port = None
    try:
        cur_port = int(proxy_url().split(':')[-1])
    except Exception:
        pass
    out['proxy_port'] = cur_port
    # 当前端口对应哪个 JMS 节点（端口→节点映射由本项目自己的池固定）
    for _p, _nid in mapping:
        if cur_port is not None and int(_p) == int(cur_port):
            out['node'] = _nid
            break

    if not quiet:
        G._print(f'🌐 IP 池检查：出口 {egress or "未测出"} | 老虎网关 {state}')

    # ── 情况 A：老虎网关放行 → 可用（2026-10-30 起不再有白名单概念）──────────
    if state == 'ok':
        out['ok'] = True
        out['action'] = 'native'
        out['elapsed'] = round(time.time() - t0, 2)
        _save_state(out)
        return out

    # ── 情况 B：被拒 / 连不通 → 在本项目端口池内换端口 ──────────────────────
    if not allow_switch:
        out['reason'] = f'tiger={state}（{detail}）；allow_switch=False 不切换'
        out['elapsed'] = round(time.time() - t0, 2)
        return out

    deadline = t0 + budget
    while time.time() < deadline:
        port, eg, nid = fast_switch(exclude_port=cur_port,
                                    force_probe=bool(out['tried']))
        if not port:
            break
        out['tried'].append(port)
        if not quiet:
            G._print(f'  🔄 池内换端口：{cur_port} → {port}（预期出口 {eg or "待测"}）')
        if apply_port:
            apply_port(port)
        out['proxy_port'] = port
        out['node'] = nid
        e2, _ = probe_egress(port=port)
        st2, d2 = probe_tiger(account, port=port)
        out['egress'] = e2
        out['in_pool'] = None
        if st2 == 'ok':
            out['ok'] = True
            out['action'] = 'switched'
            log_switch(cur_port, port, None, nid, 'ensure_switch', source='ensure')
            _log(f'ensure 已恢复：换到端口 {port}（节点 {nid}），实测出口 {e2}，网关 ok')
            if not quiet:
                G._print(f'    ✅ 已换到端口 {port} 并验证：出口 {e2}，老虎网关放行')
            out['elapsed'] = round(time.time() - t0, 2)
            _save_state(out)
            return out
        if not quiet:
            G._print(f'    ⚠️ 该端口仍不可用：出口 {e2}，网关 {st2}（{d2[:80]}）')
        cur_port = port

    # ── 池内全部失败 → 可能是订阅商整批换了节点（缓存里那 6 个全废）────────────
    # 2026-10-30 立：这是**唯一应该联网重拉订阅**的场景——本地缓存已经不能用，重拉才可能
    # 拿到新节点（要不要再去老虎页加白是用户的事，但至少能告诉 TA「池子已经换了」）。
    # 只试一次，失败照旧返回失败，不在这里循环等。
    try:
        import jms_pool as _J2
        _fr = _J2.refresh(force=True)
        if _fr.get('ok') and _fr.get('nodes'):
            if start_multi(force=True):
                port, eg, nid = fast_switch(force_probe=True)
                if port:
                    if apply_port:
                        apply_port(port)
                    e2, _ = probe_egress(port=port)
                    st2, d2 = probe_tiger(account, port=port)
                    out['proxy_port'], out['node'] = port, nid
                    out['egress'] = e2
                    out['in_pool'] = None
                    if st2 == 'ok':
                        out['ok'] = True
                        out['action'] = 'switched_after_resub'
                        log_switch(None, port, None, nid,
                                   'ensure_switch_after_resub', source='ensure')
                        _log(f'ensure 重拉订阅后恢复：端口 {port}（节点 {nid}）出口 {e2}')
                        if not quiet:
                            G._print(f'    ✅ 重拉订阅后已恢复：端口 {port}，出口 {e2}')
                        out['elapsed'] = round(time.time() - t0, 2)
                        _save_state(out)
                        return out
    except Exception:
        pass

    out['ok'] = False
    out['action'] = 'failed'
    out['reason'] = out['reason'] or (f'池内端口全部失败（试了 {len(out["tried"])} 个）——'
                                      f'很可能 JMS 池里有 IP 尚未在老虎白名单内')
    _log(f'ensure 失败：{out["reason"]}；最后出口 {out["egress"]}')
    out['elapsed'] = round(time.time() - t0, 2)
    return out


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    quiet = '--quiet' in argv
    as_json = '--json' in argv
    no_switch = '--no-switch' in argv

    # ── 多端口并行池的运维入口（2026-10-29）：排查 / 手动预热 ──────────────
    if '--multi-status' in argv or '--multi-start' in argv or '--multi-stop' in argv:
        if '--multi-stop' in argv:
            stop_multi()
            G._print('已停止多端口池进程')
            return 0
        mapping = start_multi(force='--multi-start' in argv)
        if not mapping:
            G._print('❌ 多端口池不可用（xray 缺失或配置生成失败），将回退单实例切换')
            return 2
        ports = probe_ports(mapping, force='--multi-start' in argv)
        rows = sorted(ports.items(), key=lambda kv: int(kv[0]))
        if as_json:
            print(json.dumps(ports, ensure_ascii=False))
        else:
            G._print(f'多端口池：进程 {"运行中" if multi_running() else "未运行"}，'
                     f'{sum(1 for v in ports.values() if v.get("ok"))}/{len(rows)} 个端口可用')
            for port, v in rows:
                mark = '✅' if v.get('ok') else '❌'
                G._print(f'  {mark} 端口 {port} 出口 {v.get("egress") or "不可用"}'
                         f'（节点 {v.get("node") or "?"}）')
        return 0 if any(v.get('ok') for v in ports.values()) else 2
    account = 'paper'
    for i, a in enumerate(argv):
        if a == '--account' and i + 1 < len(argv):
            account = argv[i + 1]

    quick = '--quick' in argv
    res = ensure(account=account, quiet=(quiet or as_json), allow_switch=not no_switch,
                 quick=quick)

    if as_json:
        print(json.dumps(res, ensure_ascii=False))
    else:
        if res['ok']:
            G._print(f'✅ IP 池可用（{res["action"]}）：出口 {res["egress"]}'
                     f'｜节点 {res["node"]}｜耗时 {res["elapsed"]}s')
        else:
            G._print(f'❌ IP 池不可用：{res["reason"]}｜出口 {res["egress"]}')
            G._print('   → 归因：JMS 代理服务的稳定性问题（不是老虎侧、也不需要加白）——'
                     '等下一窗口刷新订阅，或考虑更换更稳定的代理服务')
    return 0 if res['ok'] else (3 if 'no_nodes' in (res['reason'] or '') else 2)




# ══════════════════════════════════════════════════════════════════════════════
# 请求级快速切换（2026-10-29 用户立，要求把「被拒→恢复」从 9s 压下来）
#
# 用户原话：「9秒太久了，请求被拒直接切池子里的另一个 IP 重新请求。」
#
# 所以这里**刻意不做**出口实测 / 网关探针那两道验证（那是 ensure() 的「事前体检」口径，
# 适合巡检）；请求级恢复走最短链路：
#     真实请求被拒（access forbidden）→ switch 到池内下一个节点 → 等代理端口就绪 → 直接重发
# 重发成功即视为恢复（真实请求本身就是最强的验证），失败才继续切下一个。
# 省掉的是「切换后再跑两次 curl 探测」的 3~4s 固定开销。
# ══════════════════════════════════════════════════════════════════════════════

ROTATE_PATH = os.path.join(TMP_DIR, 'ip_pool_rotate.json')
PORT_WAIT_TIMEOUT = 6.0     # 等代理端口就绪上限（xray 重启后从 0 到能 accept 约 0.3~2s）
PORT_SETTLE = 0.25          # 端口就绪后再给 xray 一点握手时间，避免首个请求打在「刚 accept 还没通」的瞬间


def _load_rotate():
    """读「上次切过的节点」（轮换基准），让连续失败时不会反复试同一个。"""
    try:
        with open(ROTATE_PATH) as f:
            return json.load(f).get('last_node')
    except Exception:
        return None


def _save_rotate(node_id):
    try:
        os.makedirs(TMP_DIR, exist_ok=True)
        with open(ROTATE_PATH, 'w') as f:
            json.dump({'last_node': node_id, 'at': time.time()}, f)
    except Exception:
        pass


def _wait_proxy_port(timeout=PORT_WAIT_TIMEOUT):
    """等代理端口能 accept（轮询，不 sleep 固定时长）。返回就绪耗时或 None。

    为什么轮询而不是固定 sleep：xray 重启后端口就绪时间不固定（实测 0.3~2s），固定 sleep
    要么白等、要么抢跑。轮询 + 极短间隔能把这一步压到 ~0.3s。
    """
    import socket
    px = proxy_url()
    host_port = px.split('//')[-1]
    host, port = host_port.split(':')[0], int(host_port.split(':')[1])
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            s = socket.create_connection((host, port), timeout=0.4)
            s.close()
            return time.time() - t0
        except OSError:
            time.sleep(0.1)
    return None


# ══════════════════════════════════════════════════════════════════════════════
# 多端口并行池（2026-10-29 用户立，把「换 IP」从秒级压到毫秒级）
#
# 动机：`xpilot switch` 是 stop + start（重启 xray），实测 2~6s——这是**单实例的物理
# 下限**，用户明确嫌慢（「9秒太久了，请求被拒直接切池子里的另一个 IP 重新请求」）。
#
# 做法：自己起一个独立的 xray 进程（不干扰 xpilot 的 1080/1087），配置里放**多个
# inbound 端口**（每个端口固定走池内一个节点，由 routing 规则分流）。被白名单拒时，
# 只需把老虎 SDK 的 ProxyManager 指向**另一个端口**——不重启任何进程，毫秒级。
#
# 三层结构：
#   端口 10871..10876  ← 每个端口 = 一个池内节点
#   tmp/ip_pool_ports.json = {port: {node, egress, ok, at}}（探活缓存）
#   被拒 → fast_switch() 从「egress 在白名单内」的端口里挑一个（避开刚失败的）→ 换
#        ProxyManager → 重发。缓存 TTL 内不重新探活（探活要 6 个 curl，约 2s）。
#
# 降级：多端口池起不来（xray 缺失 / 配置生成失败 / 进程起不来）时，自动回退到
# 无可用端口时如实报错（不再有「重启代理进程」这类兜底——与 xpilot 解耦）。
# ══════════════════════════════════════════════════════════════════════════════

MULTI_BASE_PORT = 10871          # 多端口池 http 起始端口（10871..10871+N-1）
SOCKS_PORT_OFFSET = 10           # socks 端口 = http 端口 + 10（10881..10885），同节点同出口
MULTI_CONFIG = os.path.join(TMP_DIR, 'ip_pool_xray.json')   # 生成的 xray 配置（含节点凭证 → 必须留在 tmp/）
MULTI_PID = os.path.join(TMP_DIR, 'ip_pool_xray.pid')
MULTI_LOG = os.path.join(TMP_DIR, 'ip_pool_xray.log')
MULTI_PORTS = os.path.join(TMP_DIR, 'ip_pool_ports.json')
MULTI_PROBE_TTL = 300            # 端口出口探活缓存有效期（秒）
MULTI_START_TIMEOUT = 8.0        # 等全部端口 accept 的上限


def _xray_bin():
    """xray 可执行文件路径 —— **本项目自己的，与 xpilot 无关**（2026-10-29 用户定）。

    为什么用 Homebrew 的：一条命令安装、有校验、升级方便；装完 `brew pin xray` 锁住版本，
    避免哪天 `brew upgrade` 顺手升级导致配置不兼容、交易链路突然挂掉。它与 xpilot 自己
    那份 `~/.local/bin/xray` 是两个完全独立的二进制，互不影响。
    """
    import shutil
    for p in ('/opt/homebrew/bin/xray',      # Apple Silicon 的 brew prefix
              '/usr/local/bin/xray',          # Intel Mac 的 brew prefix
              '/usr/bin/xray'):
        if os.path.exists(p):
            return p
    return shutil.which('xray')              # 兜底：PATH 里的任意 xray


def _node_outbound(node):
    """把一个 JMS 订阅节点配置转成 xray outbound（vless + reality / tls）。"""
    stream = {'network': node.get('network', 'tcp'), 'security': node.get('security', 'none')}
    if node.get('security') == 'reality':
        stream['realitySettings'] = {
            'serverName': node.get('servername', ''),
            'publicKey': node.get('reality_public_key', ''),
            'shortId': node.get('reality_short_id', ''),
            'fingerprint': node.get('fingerprint', 'chrome'),
        }
    elif node.get('tls'):
        stream['tlsSettings'] = {'serverName': node.get('servername', ''),
                                 'fingerprint': node.get('fingerprint', 'chrome')}
    user = {'id': node.get('uuid') or node.get('id'), 'encryption': 'none'}
    if node.get('flow'):
        user['flow'] = node['flow']
    proto = node.get('protocol', 'vless')
    if proto in ('ss', 'shadowsocks'):
        # Shadowsocks 节点：JMS 订阅里 6 个节点中有 1 个是 ss（2026-10-29 实测）。
        # 注意三点（早期跳过它就是因为踩了这些坑）：
        #   ① xray 里协议名是 `shadowsocks`，不是 `ss`；
        #   ② `method`（如 aes-256-gcm）是**加密方式**，必须放 settings，绝不能塞进
        #      streamSettings.security —— 那会让 xray 报 "Unknown security" 直接起不来；
        #   ③ ss 不需要 streamSettings（默认 tcp 直连），整块省略最稳。
        return {'protocol': 'shadowsocks',
                'settings': {'servers': [{
                    'address': node.get('address'),
                    'port': int(node.get('port', 443)),
                    'method': node.get('method') or node.get('security') or '',
                    'password': node.get('password') or node.get('uuid') or '',
                }]}}
    if proto != 'vless':
        return None      # 其它未支持的协议仍跳过（当前订阅只有 vless 与 ss）
    settings = {'vnext': [{'address': node.get('address'), 'port': int(node.get('port', 443)),
                           'users': [user]}]}
    return {'protocol': proto, 'settings': settings, 'streamSettings': stream}



def build_multi_config(nodes=None):
    """生成多端口 xray 配置（每个池内节点一个 inbound 端口 + 一一对应的 routing 规则）。

    返回 [(port, node_id), ...]；失败返回 []。
    """
    # 2026-10-30：白名单功能已移除，池就是 JMS 给的全部节点（不再按白名单过滤/排序）
    # 节点源 = 本项目自己拉取的 JMS 订阅（2026-10-29 用户定：与 xpilot 彻底解耦）。
    # 订阅里的字段（protocol/address/port/uuid/flow/security/servername/reality_*）
    # 与 _node_outbound() 期望的完全一致，所以代理配置生成逻辑不用改。
    try:
        import jms_pool as _J
        _r = _J.refresh(network=False)
        nodes = _r['nodes'] if _r.get('ok') else {}
        default_id = None
    except Exception:
        nodes, default_id = {}, None
    if not nodes:
        _log('多端口池：JMS 订阅取不到节点（详见 jms_pool.py 输出）')
        return [], False

    # 排序：vless 优先（ss 节点有过不通先例），其余按 id。全部节点都编进池，
    # 实际用哪个由探活（网关探针）实时决定。
    ordered = sorted(nodes.items(),
                     key=lambda kv: (kv[1].get('protocol') != 'vless', kv[0]))
    inbounds, outbounds, rules, mapping = [], [], [], []
    for i, (nid, node) in enumerate(ordered):
        if not node.get('address'):
            continue
        port = MULTI_BASE_PORT + i
        socks_port = port + SOCKS_PORT_OFFSET
        in_tag, out_tag = f'ip-in-{i}', f'ip-out-{i}'
        # 每个节点开**两个**入站：http（REST 用，urllib3 ProxyManager）与 socks5
        # （WS 用，socket.create_connection 被 monkeypatch 成走 socks）。原先只有 http，
        # 导致 WS 链路按「http 端口−7」推导出不存在的 socks 端口而退回直连 → 白名单下被拒。
        inbounds.append({'tag': in_tag, 'listen': '127.0.0.1', 'port': port,
                         'protocol': 'http', 'settings': {}})
        inbounds.append({'tag': f'{in_tag}-socks', 'listen': '127.0.0.1', 'port': socks_port,
                         'protocol': 'socks', 'settings': {'auth': 'noauth', 'udp': False}})
        ob = _node_outbound(node)
        if ob is None:          # 非 vless 节点跳过（见 _node_outbound 注释）
            continue
        ob['tag'] = out_tag
        outbounds.append(ob)
        rules.append({'type': 'field', 'inboundTag': [in_tag, f'{in_tag}-socks'],
                      'outboundTag': out_tag})
        mapping.append((port, nid))

    if not mapping:
        return [], False
    cfg = {'log': {'loglevel': 'warning'},
           'inbounds': inbounds,
           'outbounds': outbounds + [{'tag': 'direct', 'protocol': 'freedom'},
                                    {'tag': 'block', 'protocol': 'blackhole'}],
           'routing': {'domainStrategy': 'AsIs', 'rules': rules}}
    txt = json.dumps(cfg, indent=1)
    # 关键（2026-10-29 修）：**内容没变就不要重写文件**。原先每次调用都写一遍，
    # mtime 随之刷新，于是 start_multi 的「配置文件比进程新 → 重启」判断永远为真，
    # 造成每次 fast_switch 都重启一次 xray、探活全打在重启窗口里返回 0/5，
    # 最后白白降级成秒级慢路径（实测 5.36s 而不是毫秒级）。
    changed = True
    try:
        with open(MULTI_CONFIG) as f:
            changed = (f.read() != txt)
    except Exception:
        changed = True
    if changed:
        try:
            os.makedirs(TMP_DIR, exist_ok=True)
            with open(MULTI_CONFIG, 'w') as f:
                f.write(txt)
            os.chmod(MULTI_CONFIG, 0o600)   # 含节点凭证，仅本用户可读
        except Exception as e:
            _log(f'多端口池：写配置失败 {e}')
            return [], False
    return mapping, changed


def _pid_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except Exception:
        return False


def multi_running():
    """多端口池 xray 进程是否在跑。"""
    try:
        with open(MULTI_PID) as f:
            pid = f.read().strip()
        return _pid_alive(pid)
    except Exception:
        return False


def stop_multi():
    """停掉多端口池进程（换配置 / 排查时用）。"""
    try:
        with open(MULTI_PID) as f:
            pid = int(f.read().strip())
        if _pid_alive(pid):
            os.kill(pid, 15)
    except Exception:
        pass
    try:
        os.remove(MULTI_PID)
    except Exception:
        pass


def start_multi(force=False):
    """确保多端口池进程在跑且配置最新。返回 [(port, node_id)] 或 []。

    懒加载设计：只在真正需要（fast_switch / 启动时）才拉起，不常驻 cron。
    配置按 nodes.json 的 mtime 判断是否过期（订阅更新 → 重新生成 + 重启）。
    """
    mapping, changed = build_multi_config()
    if not mapping:
        return []
    xray = _xray_bin()
    if not xray or not os.path.exists(xray):
        _log('多端口池：找不到 xray 可执行文件，回退单实例切换')
        return []

    # 只在「进程没在跑」或「配置内容真的变了」时才重启——判断依据是配置内容是否变化
    # （build_multi_config 的 changed 返回值），不是 mtime（见那里关于 2026-10-29 bug 的注释）。
    need_restart = force or changed or not multi_running()

    if need_restart:
        stop_multi()
        try:
            import subprocess as sp
            f = open(MULTI_LOG, 'a')
            proc = sp.Popen([xray, 'run', '-c', MULTI_CONFIG],
                            stdout=f, stderr=f, start_new_session=True)
            with open(MULTI_PID, 'w') as pf:
                pf.write(str(proc.pid))
            # 等所有端口 accept
            t0 = time.time()
            pending = {p for p, _ in mapping}
            while pending and time.time() - t0 < MULTI_START_TIMEOUT:
                for p in list(pending):
                    try:
                        s = socket_create((p))
                        s.close()
                        pending.discard(p)
                    except Exception:
                        pass
                if pending:
                    time.sleep(0.1)
            if pending:
                _log(f'多端口池：端口未就绪 {sorted(pending)}')
            _log(f'多端口池：进程 {proc.pid} 已启动，端口 {[p for p, _ in mapping]}')
        except Exception as e:
            _log(f'多端口池：启动失败 {e}')
            return []
    return mapping


def socket_create(port, host='127.0.0.1', timeout=0.3):
    """建一个 TCP 连接（探端口用）。"""
    import socket as _s
    return _s.create_connection((host, port), timeout=timeout)


def probe_ports(mapping=None, force=False, timeout=EGRESS_TIMEOUT):
    """并行探活每个端口的实测出口 IP，落缓存 tmp/ip_pool_ports.json。

    返回 {port: {node, egress, ok, at}}。缓存未过期且非 force 时直接返回缓存。
    """
    if not force:
        try:
            with open(MULTI_PORTS) as f:
                cache = json.load(f)
            if cache and time.time() - max(v.get('at', 0) for v in cache.values()) < MULTI_PROBE_TTL:
                return cache
        except Exception:
            pass

    if mapping is None:
        mapping = _port_node_map()
    if not mapping:
        return {}

    from concurrent.futures import ThreadPoolExecutor

    def _probe_once(port, src, timeout):
        try:
            r = G.subprocess.run(['curl', '-s', '--max-time', str(timeout),
                                  '--proxy', f'http://127.0.0.1:{port}', src],
                                 capture_output=True, text=True, timeout=timeout + 2)
            body = (r.stdout or '').strip()
            return body if (r.returncode == 0 and body and len(body) <= 64 and ' ' not in body) else None
        except Exception:
            return None

    def _one(item):
        idx, (port, nid) = item
        # 关键（2026-10-29 修）：**每个端口错开用不同的 IP 查询源**，且失败换源重试。
        # 原先 6 个端口并发都打 api.ipify.org，实测被限流 → 整批假失败报「0/5 可用」，
        # 进而让 fast_switch 以为池里没可用端口、误报「需加白」。这是会误导决策的假警报。
        srcs = EGRESS_SOURCES[idx % len(EGRESS_SOURCES):] + EGRESS_SOURCES[:idx % len(EGRESS_SOURCES)]
        eg = None
        for attempt, src in enumerate(srcs[:2]):     # 最多试 2 个不同的源
            eg = _probe_once(port, src, timeout)
            if eg:
                break
            time.sleep(0.25 * (attempt + 1))
        # ok 的判据（2026-10-30 改）：出口测得出来 **且** 经该端口打老虎网关被放行。
        # 旧口径是「出口 IP ∈ 本地白名单镜像」，白名单已废，改为直接问网关（最终真相）。
        ok = False
        if eg:
            try:
                st, _d = probe_tiger(port=port)
                ok = (st == 'ok')
            except Exception:
                ok = False
        return port, {'node': nid, 'egress': eg, 'ok': ok, 'at': time.time()}

    out = {}
    with ThreadPoolExecutor(max_workers=min(3, len(mapping))) as ex:
        for port, info in ex.map(_one, enumerate(mapping)):
            out[str(port)] = info
    try:
        with open(MULTI_PORTS, 'w') as f:
            json.dump(out, f, indent=1)
    except Exception:
        pass
    good = sum(1 for v in out.values() if v.get('ok'))
    _log(f'多端口池探活：{good}/{len(out)} 个端口可用（老虎网关放行）')
    return out


def _port_node_map():
    """从生成的配置反推 [(port, node_id)]（进程重启后仍能对齐）。"""
    try:
        with open(MULTI_CONFIG) as f:
            cfg = json.load(f)
        try:
            import jms_pool as _J
            _r = _J.refresh(network=False)
            nodes = _r['nodes'] if _r.get('ok') else {}
        except Exception:
            nodes = {}
        by_addr = {}
        for nid, n in (nodes or {}).items():
            by_addr[(n.get('address'), int(n.get('port', 443)))] = nid
        out = []
        for ib, ob in zip(cfg.get('inbounds', []), cfg.get('outbounds', [])):
            vnext = (ob.get('settings') or {}).get('vnext') or []
            if not vnext:
                continue
            key = (vnext[0].get('address'), int(vnext[0].get('port', 443)))
            nid = by_addr.get(key)
            if nid:
                out.append((ib['port'], nid))
        return out
    except Exception:
        return []


def fast_switch(exclude_port=None, force_probe=False):
    """毫秒级换端口：从「实测出口在白名单内」的端口里挑一个（避开 exclude_port）。

    返回 (port, egress, node_id) 或 (None, None, None)。纯内存挑选（探活走缓存），
    只有缓存缺失 / 过期 / 全部不可用时才真去探活（约 2s）。
    """
    mapping = start_multi()
    if not mapping:
        return None, None, None

    # ① 纯缓存优先（毫秒级）：只要之前探过、且不是刚失败的那个端口，直接返回。
    #    刻意**不在这里判断 TTL**——探活要 1.6s，而端口→节点映射是固定的，缓存里的
    #    「该端口出口在白名单内」大概率仍成立；真不成立时重发会被拒，下一轮自然会
    #    排除它并强制重探，代价是一次失败的请求，远小于每次都等 1.6s。
    ports = {}
    try:
        with open(MULTI_PORTS) as f:
            ports = json.load(f)
    except Exception:
        ports = {}

    cands = [(int(p), v) for p, v in ports.items()
             if v.get('ok') and v.get('egress') and (exclude_port is None or int(p) != int(exclude_port))]

    # ② 从没探过（缓存文件不存在）→ 按「端口对应节点的 address 在白名单内」推断，
    #    仍然毫秒级返回。依据：该服务商的节点入口地址与实测出口 IP 一致（本文件
    #    「判定三层」第 2 条已说明必须实测，这里的推断只是**首次**的临时兜底，
    #    一旦真出过问题，缓存就建立起来了，后续都走实测数据）。
    if not cands and force_probe is False and not os.path.exists(MULTI_PORTS):
        # 2026-10-30：无白名单可参照 → 首次直接拿池内第一个端口试（真不行会被网关拒，
        # 下一轮 probe_ports 就会把它标成 ok=False，代价只是一次失败请求）。
        for port, nid in mapping:
            if exclude_port is not None and int(port) == int(exclude_port):
                continue
            return port, None, nid
    if not cands:
        if not force_probe:
            # 强制重探一次（缓存过期 / 全部端口都失败过 / 刚从 assume 失败回来）。
            # 注意这里**不再进 start_multi**，避免在重启窗口里重复探活——2026-10-29 实测过这个坑。
            ports = probe_ports(mapping, force=True)
            cands = [(int(p), v) for p, v in ports.items()
                     if v.get('ok') and v.get('egress')
                     and (exclude_port is None or int(p) != int(exclude_port))]
            if cands:
                cands.sort(key=lambda kv: kv[0])
                port, info = cands[0]
                return port, info.get('egress'), info.get('node')
        return None, None, None
    cands.sort(key=lambda kv: kv[0])
    port, info = cands[0]
    return port, info.get('egress'), info.get('node')


if __name__ == '__main__':
    sys.exit(main())
