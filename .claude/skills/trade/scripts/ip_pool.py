#!/usr/bin/env python3
"""可用 IP 池保证（2026-10-29 用户立）。

## 为什么要有这个东西（用户原话要点）

「盯盘下单需要自己维护一个可用 IP 池，这个 IP 池里面的 IP 就是白名单里面的所有 IP，
如果池里面的 IP 突然失效应该在池里面切换，不能用 xpilot 的 IP，因为 xpilot 的当前使用
IP 可能被 JMS 更新成了不在老虎白名单里面的 IP，这个时候来不急手动添加，手动添加没那么
及时。」

翻译成设计口径：

- **真相是「白名单 IP 池」，不是「xpilot 当前选了哪个节点」**。xpilot 判断节点的唯一
  依据是「这个节点连不连得通」，它完全不知道老虎白名单的存在；订阅商（JMS）随时可能把
  某个节点的入口 IP 换成新的、能上网、但老虎不认的 IP。跟着 xpilot 走 = 随时可能在盘中
  失去下单能力，而人工去老虎开发者页加白根本来不及（盘中几分钟就够亏一笔）。
- **失效就换**：池里当前那个 IP 一旦失效（老虎网关拒 / 连不通），立刻在池内换一个能用的，
  不需要用户介入、不需要等加白。
- **加白是「扩池」，不是「救急」**：用户去老虎页加白的行为等价于往池里加新 IP；加白之前，
  守护要能在旧池内自我恢复。

## 判定真相的三层（按可靠性排序）

1. **老虎网关探针**（最终真相）：向老虎网关发一个**无签名的无害请求**——网络层通过时它
   回 `{"code":1000,"message":"common param error(request url is invalid)"}`；被白名单拒
   时才回 `{"code":4,"message":"access forbidden: request ip ... is not in ip whitelist"}`。
   不需要凭证、不需要 SDK、约 200ms，用它判定「老虎此刻认不认我这个出口」最准。
   **好处**：本地白名单镜像（config.tiger_whitelist）靠「用户点已添加」同步，可能滞后于
   老虎页的真实配置——只按镜像判定会在镜像滞后时**误切节点**（白白断网几秒），而按网关
   探针判定永远跟着老虎的真实态度走。
2. **实测出口 IP**（诊断 + 记录）：经 xpilot HTTP 入站打外部 IP 查询服务，拿到当前真实
   出口。必须实测，不能读 nodes.json 的 address——address 是订阅写的入口地址，实际出口
   可能因服务商侧 NAT / 多入口 / 负载均衡而不同，而老虎按「实际出口 IP」判白名单。
3. **本地白名单镜像**（只用来「挑候选」）：池内候选 = `config.tiger_whitelist` 里有对应
   节点的那些 IP。它决定「可以切到哪几个节点」，不决定「现在是否可用」。

## pool（池）的定义

池 = `config.json` 的 `proxy.tiger_whitelist`（老虎页白名单的本地镜像，6 个 IP 量级）。
池内每个 IP 需要能对应到一个 xpilot 节点（`xpilot node list` 实查的 address），才能
「切过去」。没有节点对应的 IP 是死 IP（老虎认、但本机没有节点落在那），不能作为切换目标。

## 对外接口

    ensure(account='paper', quiet=False, allow_switch=True, budget=45) -> dict
        保证老虎链路可用。正常情况约 0.3~0.5s（一次网关探针 + 一次出口实测）；
        需要切换时 5~15s（xpilot switch 会重启 xray、断网 1~3s）。
        返回 {ok, action, egress, in_pool, ...}；ok=False 时 reason 说明原因。

    CLI: python3 ip_pool.py [--quiet] [--json] [--no-switch] [--account paper|live]

退出码：0 = 可用（原生可用或已切换恢复）；2 = 池内没有可用节点（需人工加白 / 排查 xpilot）；
3 = 配置缺失（白名单或节点清单取不到）。
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import proxy_guard as G   # 复用：白名单读取、节点清单、switch、日志、打印

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
SWITCH_TIMEOUT = 25       # 单次 xpilot switch 超时（秒；ensure 场景比默认 60 收紧，下单不能等太久）
SWITCH_SETTLE = 1.5       # 切换后等 xray 就绪（秒）——立刻探测会打到还没起来的端口
ENSURE_BUDGET = 45        # ensure 总预算（秒）：下单前置可接受的最长等待；超预算即停下报失败
POOL_LOG = os.path.join(TMP_DIR, 'ip_pool.log')
# 结果缓存（2026-10-29 加）：下单前置不能每次都花 3~5s 实检（限价单挂盘口对价，延迟可能错过
# 成交）。采样段 / 空闲时后台跑实检刷新缓存，下单时用 --quick 读缓存（TTL 内直接用）。
# TTL 取 30s：出口 IP 漂移不是秒级事件（实测单节点漂移以分钟计），30s 足够新。
STATE_PATH = os.path.join(TMP_DIR, 'ip_pool_state.json')
QUICK_TTL = 30


def _log(line):
    """追加写 tmp/ip_pool.log（秒级时间戳 + 原因）。写失败静默——日志不是主路径。"""
    try:
        os.makedirs(TMP_DIR, exist_ok=True)
        with open(POOL_LOG, 'a') as f:
            f.write(f'{time.strftime("%Y-%m-%d %H:%M:%S")}\t{line}\n')
    except Exception:
        pass


def proxy_url():
    """读 config.json 的 proxy.http_proxy（默认本机 xpilot HTTP 入站口）。"""
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


def probe_tiger(account='paper', quiet=True):
    """老虎网关探针（判定「老虎此刻认不认当前出口」，无需凭证）。

    返回 (state, detail)：
      'ok'        = 网络层通过（网关回了业务/参数类错误码，说明 IP 已被放行）
      'forbidden' = 被 IP 白名单拒（access forbidden / not in ip whitelist）
      'unknown'   = 连不上 / 超时（代理挂了、节点挂了、断网——与白名单无关的另一类故障）
    """
    url = TIGER_PROBE.get(account, TIGER_PROBE['paper'])
    px = proxy_url()
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


def pool_candidates(whitelist, nodes, exclude=()):
    """池内候选节点：address ∈ 白名单（且不在 exclude 里）。vless 优先（ss 节点有过不通先例）。"""
    wl = set(whitelist or [])
    cands = [nid for nid, v in (nodes or {}).items()
             if v.get('address') in wl and nid not in exclude]
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
      whitelist_gap = 与 JMS 订阅池比对的结果（in_sync / missing_in_whitelist 等），
                  这是本项目的核心监护职责：不一致就提醒用户去老虎页加白
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

    whitelist = G.load_whitelist()
    if not whitelist:
        out['reason'] = 'no_whitelist（config.json 缺 proxy.tiger_whitelist）'
        out['elapsed'] = round(time.time() - t0, 2)
        return out
    wl_set = set(whitelist)

    # ① 确保本项目自己的端口池在跑（节点源 = JMS 订阅），并做一次「JMS 池 ↔ 白名单」比对
    try:
        import jms_pool as _J
        _jms = _J.refresh()
        if _jms.get('ok') and _jms.get('ips'):
            out['whitelist_gap'] = _J.compare(_jms['ips'], whitelist)
    except Exception:
        pass

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
    out['in_pool'] = (egress in wl_set) if egress else None
    cur_port = None
    try:
        cur_port = int(proxy_url().split(':')[-1])
    except Exception:
        pass
    out['proxy_port'] = cur_port

    if not quiet:
        G._print(f'🌐 IP 池检查：出口 {egress or "未测出"}'
                 f'（{"在" if out["in_pool"] else "不在"}白名单）| 老虎网关 {state}')

    # ── 情况 A：老虎放行 → 可用（哪怕本地镜像滞后）──────────────────────────
    if state == 'ok':
        out['ok'] = True
        out['action'] = 'native'
        if out['in_pool'] is False:
            out['mirror_stale'] = True
            _log(f'老虎放行但本地镜像无此 IP（出口 {egress}）—— 镜像滞后，不换端口；'
                 f'建议补 tiger_whitelist 镜像')
            if not quiet:
                G._print('  ℹ️ 老虎实际放行（本地白名单镜像滞后），保持当前端口不切换')
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
        st2, d2 = probe_tiger(account)
        out['egress'] = e2
        out['in_pool'] = (e2 in wl_set) if e2 else None
        if st2 == 'ok':
            out['ok'] = True
            out['action'] = 'switched'
            if out['in_pool'] is False:
                out['mirror_stale'] = True
            _log(f'ensure 已恢复：换到端口 {port}（节点 {nid}），实测出口 {e2}，网关 ok')
            if not quiet:
                G._print(f'    ✅ 已换到端口 {port} 并验证：出口 {e2}，老虎网关放行')
            out['elapsed'] = round(time.time() - t0, 2)
            _save_state(out)
            return out
        if not quiet:
            G._print(f'    ⚠️ 该端口仍不可用：出口 {e2}，网关 {st2}（{d2[:80]}）')
        cur_port = port

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
            G._print('   → 需人工处理：老虎开发者页加白当前出口 IP，或 xpilot update 同步订阅'
                     '后重跑本脚本')
    return 0 if res['ok'] else (3 if 'no_whitelist' in (res['reason'] or '') or
                                'no_nodes' in (res['reason'] or '') else 2)




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


def switch_next(quiet=True, exclude=None):
    """切到池内下一个候选节点（轮换），只等端口就绪、不做出口/网关验证。

    用于「请求被拒 → 立刻换 IP 重发」的快速路径：切换后由调用方直接重发真实请求，
    请求成功即恢复。返回 (ok, node_id, elapsed_seconds)。
    """
    t0 = time.time()
    whitelist = G.load_whitelist() or []
    default_id, nodes = G.fetch_nodes_live()
    if not nodes:
        default_id, nodes = G.load_nodes()
    if not nodes or not whitelist:
        return False, None, round(time.time() - t0, 2)

    cands = pool_candidates(whitelist, nodes, exclude=set(exclude or ()))
    ordered = [c for c in cands if c != default_id]
    if not ordered:
        return False, None, round(time.time() - t0, 2)

    # 轮换：从「上次切过的节点」的下一个开始，避免连续失败反复试同一个
    last = _load_rotate()
    if last in ordered:
        i = ordered.index(last)
        ordered = ordered[i + 1:] + ordered[:i + 1]

    for cand in ordered:
        try:
            r = G.subprocess.run(['xpilot', 'switch', cand], capture_output=True,
                                 text=True, timeout=SWITCH_TIMEOUT)
            if r.returncode != 0:
                if not quiet:
                    G._print(f'  ❌ switch {cand} 失败: {(r.stderr or "")[:100]}')
                continue
        except Exception as e:
            if not quiet:
                G._print(f'  ❌ switch {cand} 异常: {e}')
            continue
        waited = _wait_proxy_port()
        if waited is None:
            _log(f'switch_next：节点 {cand} 端口未就绪（>{PORT_WAIT_TIMEOUT}s）')
            continue
        time.sleep(PORT_SETTLE)
        _save_rotate(cand)
        elapsed = round(time.time() - t0, 2)
        _log(f'switch_next：切到 {cand}（端口就绪 {waited:.2f}s，总计 {elapsed}s），由真实请求验证')
        if not quiet:
            G._print(f'  🔄 已切到 {cand}（{elapsed}s），重发请求验证')
        return True, cand, elapsed

    _log('switch_next：池内候选全部切换失败')
    return False, None, round(time.time() - t0, 2)


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
# switch_next()（慢路径、重启 xpilot），功能不丢、只是慢。
# ══════════════════════════════════════════════════════════════════════════════

MULTI_BASE_PORT = 10871          # 多端口池 http 起始端口（10871..10871+N-1）
SOCKS_PORT_OFFSET = 10           # socks 端口 = http 端口 + 10（10881..10885），同节点同出口
MULTI_CONFIG = os.path.join(TMP_DIR, 'ip_pool_xray.json')   # 生成的 xray 配置（含节点凭证 → 必须留在 tmp/）
MULTI_PID = os.path.join(TMP_DIR, 'ip_pool_xray.pid')
MULTI_LOG = os.path.join(TMP_DIR, 'ip_pool_xray.log')
MULTI_PORTS = os.path.join(TMP_DIR, 'ip_pool_ports.json')
NODES_PATH_RAW = os.path.expanduser('~/.config/xpilot/nodes.json')   # 含凭证的原始节点数据（只读、只在内存用）
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
    """把一个 xpilot 节点配置转成 xray outbound（vless + reality / tls）。"""
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
    if proto != 'vless':
        # 只编 vless 节点（2026-10-29 实测）：ss 节点在 nodes.json 里 security 字段存的是
        # 加密方式（aes-256-gcm），与 vless 的 security（reality/tls）语义冲突，直接编进去
        # 会让 xray 报 Unknown security 起不来；而 c56s1 那个 ss 节点只是备选、白名单内
        # 还有 5 个 vless 节点，跳过它即可（池容量 5 个足够）。
        return None
    settings = {'vnext': [{'address': node.get('address'), 'port': int(node.get('port', 443)),
                           'users': [user]}]}
    return {'protocol': proto, 'settings': settings, 'streamSettings': stream}



def load_nodes_raw():
    """读 nodes.json 的**原始**节点数据（含 uuid / reality 凭证等全部字段）。

    为什么不复用 G.load_nodes()：那个只取 address / name / protocol 三个字段（够白名单比对
    用），而生成 xray outbound 必须要有 uuid、reality_public_key、servername 等凭证字段。
    注意：这些字段属敏感类，只在内存里用、只写 tmp/ 下 0600 的运行时配置，不进仓库。
    """
    try:
        with open(NODES_PATH_RAW) as f:
            data = json.load(f)
        return data.get('default_node'), (data.get('nodes') or {})
    except Exception:
        return None, {}


def build_multi_config(nodes=None):
    """生成多端口 xray 配置（每个池内节点一个 inbound 端口 + 一一对应的 routing 规则）。

    返回 [(port, node_id), ...]；失败返回 []。
    """
    whitelist = G.load_whitelist() or []
    if not whitelist:
        return [], False
    # 节点源 = 本项目自己拉取的 JMS 订阅（2026-10-29 用户定：与 xpilot 彻底解耦）。
    # 订阅里的字段（protocol/address/port/uuid/flow/security/servername/reality_*）
    # 与 _node_outbound() 期望的完全一致，所以代理配置生成逻辑不用改。
    try:
        import jms_pool as _J
        _r = _J.refresh()
        nodes = _r['nodes'] if _r.get('ok') else {}
        default_id = None
    except Exception:
        nodes, default_id = {}, None
    if not nodes:
        _log('多端口池：JMS 订阅取不到节点（详见 jms_pool.py 输出）')
        return [], False

    # 优先用「address 在白名单内」的节点（实测该服务商 address == 出口 IP）；剩下的也编进去，
    # 反正后面探活会按「实测出口 ∈ 白名单」筛选，编多不亏。
    wl = set(whitelist)
    ordered = sorted(nodes.items(),
                     key=lambda kv: (kv[1].get('address') not in wl,
                                     kv[1].get('protocol') != 'vless', kv[0]))
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
    wl = set(G.load_whitelist() or [])

    def _probe_once(port, timeout):
        try:
            r = G.subprocess.run(['curl', '-s', '--max-time', str(timeout),
                                  '--proxy', f'http://127.0.0.1:{port}',
                                  EGRESS_SOURCES[0]],
                                 capture_output=True, text=True, timeout=timeout + 2)
            body = (r.stdout or '').strip()
            return body if (r.returncode == 0 and body and len(body) <= 64 and ' ' not in body) else None
        except Exception:
            return None

    def _one(item):
        port, nid = item
        eg = _probe_once(port, timeout)
        if not eg:
            # 首次失败再试一次（xray 刚重启完、与节点握手尚未就绪时容易空返）；
            # 只重试一次、间隔 0.4s，避免把探活拖成秒级。
            time.sleep(0.4)
            eg = _probe_once(port, timeout)
        return port, {'node': nid, 'egress': eg, 'ok': bool(eg and eg in wl), 'at': time.time()}

    out = {}
    with ThreadPoolExecutor(max_workers=min(8, len(mapping))) as ex:
        for port, info in ex.map(_one, mapping):
            out[str(port)] = info
    try:
        with open(MULTI_PORTS, 'w') as f:
            json.dump(out, f, indent=1)
    except Exception:
        pass
    good = sum(1 for v in out.values() if v.get('ok'))
    _log(f'多端口池探活：{good}/{len(out)} 个端口出口在白名单内')
    return out


def _port_node_map():
    """从生成的配置反推 [(port, node_id)]（进程重启后仍能对齐）。"""
    try:
        with open(MULTI_CONFIG) as f:
            cfg = json.load(f)
        try:
            import jms_pool as _J
            _r = _J.refresh()
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
        try:
            wl = set(G.load_whitelist() or [])
            _, raw_nodes = load_nodes_raw()
            for port, nid in mapping:
                if exclude_port is not None and int(port) == int(exclude_port):
                    continue
                addr = (raw_nodes.get(nid) or {}).get('address')
                if addr and addr in wl:
                    return port, addr, nid
        except Exception:
            pass
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
