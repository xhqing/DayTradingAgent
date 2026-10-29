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


def probe_egress(quiet=True):
    """实测当前代理出口 IP。返回 (ip_or_None, detail)。

    为什么必须实测：老虎按「实际出口 IP」判白名单；nodes.json 的 address 只是订阅里写的
    入口地址，实际出口可能因服务商 NAT / 多入口 / 负载均衡而不同。实测值才是判定依据。
    """
    px = proxy_url()
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
           quick=False):
    """保证老虎链路可用：出口 IP 落在白名单池内、且老虎网关放行；不可用则池内切换。

    返回 dict：
      ok        = True/False（True = 现在可以正常调老虎 API）
      action    = 'native'（本来就可用）/ 'switched'（已切到池内另一节点）/ 'noop' / 'failed'
      egress    = 实测出口 IP（可能 None）
      in_pool   = 出口是否在本地白名单镜像内（None = 没测出来）
      node      = 当前节点 id（切换后为新节点）
      reason    = 失败原因（ok=False 时）
      tried     = 已尝试的候选节点
      elapsed   = 耗时（秒）
      mirror_stale = True 表示「老虎认、但本地镜像里没有这个 IP」→ 该去补镜像（不是故障）
    """
    t0 = time.time()

    # 快速路径：TTL 内缓存且 ok → 直接放行（下单前置走这条，省掉 3~5s 实检）
    if quick:
        st = _load_state(QUICK_TTL)
        if st and st.get('ok'):
            st['action'] = 'cached'
            st['elapsed'] = round(time.time() - t0, 3)
            return st

    out = {'ok': False, 'action': 'failed', 'egress': None, 'in_pool': None,
           'node': None, 'reason': None, 'tried': [], 'elapsed': 0.0, 'mirror_stale': False}

    whitelist = G.load_whitelist()
    if not whitelist:
        out['reason'] = 'no_whitelist（config.json 缺 proxy.tiger_whitelist）'
        out['elapsed'] = round(time.time() - t0, 2)
        return out
    wl_set = set(whitelist)

    default_id, nodes = G.fetch_nodes_live()
    if not nodes:
        default_id, nodes = G.load_nodes()
    if not nodes:
        out['reason'] = 'no_nodes（xpilot node list 与 nodes.json 都取不到）'
        out['elapsed'] = round(time.time() - t0, 2)
        return out
    out['node'] = default_id

    egress, eg_detail = probe_egress()
    state, detail = probe_tiger(account)
    out['egress'] = egress
    out['in_pool'] = (egress in wl_set) if egress else None

    if not quiet:
        G._print(f'🌐 IP 池检查：当前出口 {egress or "未测出"}'
                 f'（{"在" if out["in_pool"] else "不在"}池内）| 老虎网关 {state}')

    # ── 情况 A：老虎放行 → 可用，不需要动（哪怕本地镜像里没有这个 IP）──────────────
    # 本地白名单镜像是「用户点已添加」同步来的，可能滞后于老虎页真实配置；此时切节点纯属
    # 自伤（白白断网几秒）。所以以网关探针为最终真相，镜像滞后只做提示。
    if state == 'ok':
        out['ok'] = True
        out['action'] = 'native'
        if out['in_pool'] is False:
            out['mirror_stale'] = True
            _log(f'老虎放行但本地镜像无此 IP（出口 {egress}）—— 镜像滞后，不切节点；'
                 f'建议补 tiger_whitelist 镜像')
            if not quiet:
                G._print('  ℹ️ 老虎实际放行（本地白名单镜像滞后），保持当前节点不切换')
        out['elapsed'] = round(time.time() - t0, 2)
        _save_state(out)
        return out

    # ── 情况 B：被白名单拒 / 连不通 → 池内切换 ───────────────────────────────
    if not allow_switch:
        out['reason'] = f'tiger={state}（{detail}）；allow_switch=False 不切换'
        out['elapsed'] = round(time.time() - t0, 2)
        return out

    cands = pool_candidates(whitelist, nodes, exclude={default_id} if default_id else set())
    if not cands:
        out['reason'] = (f'tiger={state}；池内无可切节点（白名单 {len(wl_set)} IP 里'
                         f'没有能对应节点的）')
        _log(f'ensure 失败：{out["reason"]}')
        out['elapsed'] = round(time.time() - t0, 2)
        return out

    deadline = t0 + budget
    for cand in cands:
        if time.time() > deadline:
            out['reason'] = f'预算用尽（{budget}s）—— 已试 {len(out["tried"])}/{len(cands)} 个候选'
            break
        addr = (nodes.get(cand) or {}).get('address')
        out['tried'].append(cand)
        if not quiet:
            G._print(f'  🔄 池内切换：{default_id} → {cand}（{addr}）')
        ok_switch = False
        try:
            r = G.subprocess.run(['xpilot', 'switch', cand], capture_output=True,
                                 text=True, timeout=SWITCH_TIMEOUT)
            ok_switch = (r.returncode == 0)
            if not ok_switch and not quiet:
                G._print(f'    ❌ switch 返回 {r.returncode}: {(r.stderr or "")[:120]}')
        except Exception as e:
            if not quiet:
                G._print(f'    ❌ switch 异常: {e}')
        if not ok_switch:
            continue

        time.sleep(SWITCH_SETTLE)
        e2, _ = probe_egress()
        st2, d2 = probe_tiger(account)
        out['egress'] = e2
        out['in_pool'] = (e2 in wl_set) if e2 else None
        if st2 == 'ok':
            out['ok'] = True
            out['action'] = 'switched'
            out['node'] = cand
            if out['in_pool'] is False:
                out['mirror_stale'] = True
            _log(f'ensure 已恢复：切到 {cand}（{addr}），实测出口 {e2}，网关 ok')
            if not quiet:
                G._print(f'    ✅ 已切换并验证：出口 {e2}，老虎网关放行')
            out['elapsed'] = round(time.time() - t0, 2)
            _save_state(out)
            return out
        if not quiet:
            G._print(f'    ⚠️ 切换后仍不可用：出口 {e2}，网关 {st2}（{d2[:80]}）')

    out['ok'] = False
    out['action'] = 'failed'
    out['reason'] = out['reason'] or (f'池内候选全部失败（试了 {len(out["tried"])} 个）')
    _log(f'ensure 失败：{out["reason"]}；最后出口 {out["egress"]}')
    out['elapsed'] = round(time.time() - t0, 2)
    return out


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    quiet = '--quiet' in argv
    as_json = '--json' in argv
    no_switch = '--no-switch' in argv
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


if __name__ == '__main__':
    sys.exit(main())
