#!/usr/bin/env python3
"""JMS 订阅拉取与解析（本项目自己的 IP 池数据源，2026-10-29 立）。

## 这个文件干什么

本项目**独立**从 JMS 订阅链接拉取节点/IP 池，不读 xpilot 的任何文件、不调任何 xpilot 命令。
订阅链接只从本机 `config.json`（被 .gitignore 忽略）的 `jms.subscription_url` 读取。

## 职责边界（用户 2026-10-29 定）

- 本项目 = **监护者**：比对「JMS 最新 IP 池」与「老虎 IP 白名单池」，不一致就提醒用户去
  老虎开发者页加白；用户确认后把本项目的可用 IP 池同步成与老虎白名单一致。
- xpilot = 完全无关的独立项目，唯一共同点是都消费同一份 JMS IP 池。本项目不读它的配置、
  不调它的命令、不关心它选哪个节点。

## 安全约定（重要）

订阅链接含 token、解析结果含 IP 清单——两者都属敏感类，**不得打印、不得写进被 git 跟踪
的文件**。本模块的所有对外输出都只给「数量 / 布尔 / 差异摘要」，绝不回显链接或 IP 原文。
节点凭证同理：只进内存 + `tmp/` 下的运行时配置（0600、已 gitignore）。

## 支持的订阅格式（自动探测，按顺序试）

1. **Base64 链接列表**（机场最常见）：整段 base64 → 逐行 `vless://` / `vmess://` /
   `trojan://` / `ss://` 链接。
2. **明文链接列表**：不经 base64 直接是逐行链接。
3. **Clash / Mihomo YAML**：`proxies:` 数组。
4. **Xray / V2Ray JSON**：`outbounds` 数组。

拿到节点后统一转成内部结构（与 `ip_pool._node_outbound()` 消费的字段一致），
这样代理配置生成逻辑不用改。
"""

import base64
import json
import time
import os
import re
import sys
from urllib.parse import urlparse, parse_qs, unquote

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, '..', '..', '..', '..'))
CONFIG_PATH = os.path.join(HERE, '..', 'config.json')
TMP_DIR = os.path.join(ROOT, 'tmp')
CACHE_PATH = os.path.join(TMP_DIR, 'jms_subscription.cache')

# 拉取超时与失败退避（2026-10-30 立，修 T177）。
# 背景：2026-10-29 美股盯盘时订阅源拉不到，fetch_subscription 走「代理 25s + 直连 25s」
# 两条通道，每次卡 60-90 秒；而 refresh() 被 ip_pool.ensure() / build_multi_config() /
# monitor_segment 每轮采样调用——结果整条采样链与下单链一起被拖死（当日至此无法下单）。
# 现在：① 默认拉取超时降到 10s；② 缓存已过期时用 5s 快拉；③ 失败后 300s 内不再重试
# （直接用旧缓存），保证最坏情况下 5 分钟内最多卡一次 10s、而不是每调一次卡 60s。
FETCH_TIMEOUT = 10           # 无缓存时的拉取超时（秒，单通道）
FAST_FETCH_TIMEOUT = 5       # 缓存过期时快拉超时（秒，单通道）
FETCH_FAIL_BACKOFF = 900     # 拉取失败后的退避窗口（秒）——取 15 分钟：订阅源会回
                             # HTTP 429（限流），重重重试反而一直拿不到；旧缓存完全够用，
                             # 节点变动不差这 15 分钟。

_last_fetch_fail_at = 0.0    # 进程级：上次拉取失败的时刻（time.time()）
_in_fetch = False            # 进程级：正在拉取中（防递归）——见下条注释


# ──────────────────────────────────────────────────────────────────────────────
# 配置读取
# ──────────────────────────────────────────────────────────────────────────────

def load_subscription_url():
    """读订阅链接（只从本机 config.json）。没配就返回 None，不报错、不打印内容。"""
    try:
        with open(CONFIG_PATH) as f:
            return (json.load(f).get('jms') or {}).get('subscription_url') or None
    except Exception:
        return None


# ──────────────────────────────────────────────────────────────────────────────
# 拉取
# ──────────────────────────────────────────────────────────────────────────────

def fetch_subscription(url=None, timeout=None, use_local_pool=True):
    """拉取订阅原文。返回 (ok, text_or_None, reason)。

    走本项目自己的代理池端口（避免直连被墙 / 被 Cloudflare 拦），池不可用时退回直连。
    **不打印链接**，只在失败时给一句不含敏感值的 reason。

    `timeout` = 单通道超时（秒）；不传用 FETCH_TIMEOUT（10s）。缓存过期时的快拉传
    FAST_FETCH_TIMEOUT（5s）——见模块顶部 T177 的说明，绝不允许再把调用方卡 60 秒以上。
    """
    url = url or load_subscription_url()
    if not url:
        return False, None, 'no_subscription_url'
    timeout = timeout or FETCH_TIMEOUT

    # 防递归（2026-10-30，T177 修复的一部分）：fetch_subscription → ip_pool.fast_switch()
    # → start_multi() → build_multi_config() → refresh() → fetch_subscription …
    # 缓存过期时这条链会自己套自己，每层再花「代理 5s + 直连 5s」，实测能转上好几分钟
    # （2026-10-29 盘中就是这么卡住的）。发现已在拉取中就直接回退——让内层 refresh 走缓存/
    # 快速失败，不再发任何网络请求。
    global _in_fetch
    if _in_fetch:
        return False, None, 'reentrant_skip（拉取进行中，避免递归）'
    _in_fetch = True
    try:
        return _fetch_subscription_inner(url, timeout, use_local_pool)
    finally:
        _in_fetch = False


def _fetch_subscription_inner(url, timeout, use_local_pool=True):
    """真正干活的拉取（不含防递归外壳）。"""

    proxies = []
    if use_local_pool:
        try:
            import ip_pool as P
            # 拿任一可用端口当出口；拿不到就直连（不强行依赖池）
            port, _eg, _nid = P.fast_switch()
            if port:
                proxies.append(f'http://127.0.0.1:{port}')
        except Exception:
            pass
    proxies.append(None)          # 兜底直连

    last = 'unknown_error'
    for px in proxies:
        cmd = ['curl', '-sS', '--max-time', str(timeout), '-L',
               '-H', 'User-Agent: clash-verge/2.0',   # 多数机场按 UA 决定返回哪种格式
               '-w', '\n__HTTP_CODE__%{http_code}']
        if px:
            cmd += ['--proxy', px]
        cmd.append(url)
        try:
            import subprocess
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 5)
            out = r.stdout or ''
            m = re.search(r'__HTTP_CODE__(\d+)$', out.strip())
            code = int(m.group(1)) if m else 0
            body = out[:m.start()].strip() if m else out.strip()
            if code == 200 and body:
                return True, body, 'ok'
            last = f'http_{code or "unknown"}'
        except Exception as e:
            last = type(e).__name__
    return False, None, last


# ──────────────────────────────────────────────────────────────────────────────
# 解析：统一成内部节点结构
# ──────────────────────────────────────────────────────────────────────────────

def _b64fix(s):
    """补全 base64 的 padding 并解码（容错：机场给的 padding 常不规范）。"""
    s = s.strip().replace('-', '+').replace('_', '/')
    s += '=' * (-len(s) % 4)
    return base64.b64decode(s, validate=False).decode('utf-8', 'ignore')


def _node_id(address, port, name=''):
    """节点内部 id：用 名称@入口:端口（与 xpilot nodes.json 的命名风格保持一致，
    但完全由本项目自己生成，不依赖任何外部文件）。"""
    host = name or address
    return f'{host}@{address}:{port}'.lower().replace(' ', '_')


def _parse_vless(link):
    """vless://uuid@host:port?params#name"""
    u = urlparse(link)
    q = parse_qs(u.query)
    addr, port = u.hostname, int(u.port or 443)
    return {
        'protocol': 'vless',
        'address': addr,
        'port': port,
        'uuid': u.username,
        'flow': (q.get('flow') or [''])[0],
        'security': (q.get('security') or ['none'])[0],
        'network': (q.get('type') or ['tcp'])[0],
        'servername': (q.get('sni') or q.get('host') or [''])[0],
        'reality_public_key': (q.get('pbk') or [''])[0],
        'reality_short_id': (q.get('sid') or [''])[0],
        'fingerprint': (q.get('fp') or ['chrome'])[0],
        'tls': (q.get('security') or ['none'])[0] in ('tls', 'reality'),
        'name': unquote(u.fragment) if u.fragment else '',
    }


def _parse_trojan(link):
    """trojan://password@host:port?params#name"""
    u = urlparse(link)
    q = parse_qs(u.query)
    return {
        'protocol': 'trojan',
        'address': u.hostname,
        'port': int(u.port or 443),
        'password': u.username,
        'security': 'tls',
        'network': (q.get('type') or ['tcp'])[0],
        'servername': (q.get('sni') or q.get('peer') or [''])[0],
        'fingerprint': (q.get('fp') or ['chrome'])[0],
        'tls': True,
        'name': unquote(u.fragment) if u.fragment else '',
    }


def _parse_ss(link):
    """ss://base64(method:password)@host:port 或 ss://base64(method:password@host:port)#name"""
    body = link[len('ss://'):]
    name = ''
    if '#' in body:
        body, name = body.split('#', 1)
        name = unquote(name)
    body = body.split('?')[0]
    if '@' in body:
        userinfo, hostport = body.rsplit('@', 1)
        try:
            cred = _b64fix(userinfo)
        except Exception:
            cred = unquote(userinfo)
    else:
        decoded = _b64fix(body)
        cred, _, hostport = decoded.rpartition('@')
    method, _, password = cred.partition(':')
    host, _, port = hostport.rpartition(':')
    return {
        'protocol': 'ss',
        'address': host.strip('[]'),
        'port': int(port or 443),
        'method': method,
        'password': password,
        'security': 'none',
        'network': 'tcp',
        'tls': False,
        'name': name,
    }


def _parse_vmess(link):
    """vmess://base64(json)"""
    data = json.loads(_b64fix(link[len('vmess://'):]))
    return {
        'protocol': 'vmess',
        'address': data.get('add'),
        'port': int(data.get('port') or 443),
        'uuid': data.get('id'),
        'alterId': data.get('aid', 0),
        'security': data.get('tls') or 'none',
        'network': data.get('net') or 'tcp',
        'servername': data.get('sni') or data.get('host') or '',
        'fingerprint': data.get('fp') or 'chrome',
        'tls': (data.get('tls') or '') == 'tls',
        'name': data.get('ps') or '',
    }


_LINK_PARSERS = {
    'vless://': _parse_vless,
    'trojan://': _parse_trojan,
    'ss://': _parse_ss,
    'vmess://': _parse_vmess,
}


def _parse_link_lines(text):
    """把逐行链接解析成节点列表（跳过不支持的协议，不抛错）。"""
    nodes = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(('#', '//')):
            continue
        for prefix, fn in _LINK_PARSERS.items():
            if line.startswith(prefix):
                try:
                    n = fn(line)
                    if n.get('address') and n.get('port'):
                        n['id'] = _node_id(n['address'], n['port'], n.get('name'))
                        nodes[n['id']] = n
                except Exception:
                    pass
                break
    return nodes


def _parse_clash_yaml(text):
    """极简 Clash proxies 解析（不引入 PyYAML 依赖：只认机场订阅里最常见的键值形态）。"""
    nodes = {}
    try:
        m = re.search(r'^proxies:\s*$(.*?)(?=^\S|\Z)', text, re.M | re.S)
        if not m:
            return nodes
        block = m.group(1)
        for item in re.split(r'^\s*-\s+', block, flags=re.M):
            if not item.strip() or ':' not in item:
                continue
            kv = {}
            for line in item.splitlines():
                mm = re.match(r'\s*([A-Za-z0-9_-]+):\s*(.+?)\s*$', line)
                if mm:
                    kv[mm.group(1)] = mm.group(2).strip('\'"')
            addr, port = kv.get('server'), kv.get('port')
            if not addr or not port:
                continue
            proto = (kv.get('type') or '').lower()
            sn = {
                'protocol': proto,
                'address': addr,
                'port': int(port),
                'uuid': kv.get('uuid'),
                'flow': kv.get('flow', ''),
                'security': kv.get('security') or ('tls' if kv.get('tls') else 'none'),
                'network': kv.get('network', 'tcp'),
                'servername': kv.get('servername') or kv.get('sni') or '',
                'reality_public_key': kv.get('public-key') or kv.get('public_key') or '',
                'reality_short_id': kv.get('short-id') or kv.get('short_id') or '',
                'fingerprint': kv.get('client-fingerprint') or 'chrome',
                'tls': bool(kv.get('tls')),
                'name': kv.get('name', ''),
            }
            sn['id'] = _node_id(addr, int(port), sn.get('name'))
            nodes[sn['id']] = sn
    except Exception:
        pass
    return nodes


def _parse_xray_json(text):
    """Xray / V2Ray 配置的 outbounds。"""
    nodes = {}
    try:
        data = json.loads(text)
    except Exception:
        return nodes
    for ob in (data.get('outbounds') or []):
        try:
            proto = ob.get('protocol', '')
            st = ob.get('streamSettings') or {}
            if proto == 'vless':
                v = (ob.get('settings', {}).get('vnext') or [{}])[0]
                usr = (v.get('users') or [{}])[0]
                addr, port = v.get('address'), int(v.get('port') or 443)
                real = st.get('realitySettings') or {}
                n = {
                    'protocol': 'vless', 'address': addr, 'port': port,
                    'uuid': usr.get('id'), 'flow': usr.get('flow', ''),
                    'security': st.get('security', 'none'),
                    'network': st.get('network', 'tcp'),
                    'servername': real.get('serverName') or (st.get('tlsSettings') or {}).get('serverName', ''),
                    'reality_public_key': real.get('publicKey', ''),
                    'reality_short_id': real.get('shortId', ''),
                    'fingerprint': real.get('fingerprint') or 'chrome',
                    'tls': st.get('security') in ('tls', 'reality'),
                    'name': ob.get('tag', ''),
                }
            elif proto == 'vmess':
                v = (ob.get('settings', {}).get('vnext') or [{}])[0]
                usr = (v.get('users') or [{}])[0]
                addr, port = v.get('address'), int(v.get('port') or 443)
                n = {
                    'protocol': 'vmess', 'address': addr, 'port': port,
                    'uuid': usr.get('id'), 'alterId': usr.get('alterId', 0),
                    'security': st.get('security', 'none'),
                    'network': st.get('network', 'tcp'), 'servername': '',
                    'fingerprint': 'chrome', 'tls': st.get('security') == 'tls',
                    'name': ob.get('tag', ''),
                }
            else:
                continue
            if n.get('address'):
                n['id'] = _node_id(n['address'], n['port'], n.get('name'))
                nodes[n['id']] = n
        except Exception:
            continue
    return nodes


def parse_subscription(text):
    """自动探测格式并解析。返回 {node_id: node_dict}。

    探测顺序：Xray/Clash 结构化 → base64 链接列表 → 明文链接列表。
    """
    if not text:
        return {}
    t = text.strip()
    if t.startswith('{'):
        nodes = _parse_xray_json(t)
        if nodes:
            return nodes
    if re.search(r'^proxies:\s*$', t, re.M):
        nodes = _parse_clash_yaml(t)
        if nodes:
            return nodes
    # base64 整段（机场最常见的形态）：解码后应是逐行链接
    try:
        decoded = _b64fix(t)
        if '://' in decoded:
            nodes = _parse_link_lines(decoded)
            if nodes:
                return nodes
    except Exception:
        pass
    return _parse_link_lines(t)


# ──────────────────────────────────────────────────────────────────────────────
# IP 池比对（本项目核心职责）
# ──────────────────────────────────────────────────────────────────────────────

def ip_pool_from_nodes(nodes):
    """从节点集合里取「入口 IP 清单」（去重、排序）。不打印内容，调用方自行决定怎么用。"""
    return sorted({n['address'] for n in nodes.values() if n.get('address')})


def compare(jms_ips=None, whitelist=None):
    """比对 JMS 池与老虎白名单池。返回差异摘要（只给数量和分类，可在日志里安全打印）。

    - missing_in_whitelist：JMS 有、白名单没有 → 这些节点用不了，用户需要去老虎页加白
    - stale_in_whitelist：白名单有、JMS 没有 → 已经拉不到的旧 IP，建议从老虎页清掉
    """
    if jms_ips is None or whitelist is None:
        raise ValueError('compare() 需要显式传入 jms_ips 与 whitelist（避免误读全局状态）')
    a, b = set(jms_ips), set(whitelist)
    return {
        'in_sync': a == b,
        'jms_count': len(a),
        'whitelist_count': len(b),
        'missing_in_whitelist': sorted(a - b),
        'stale_in_whitelist': sorted(b - a),
    }


def refresh(cache=True, ttl=600, force=False, network=True):
    """一次完整刷新：拉订阅 → 解析 → 返回 {ok, nodes, ips, reason}。

    结果缓存到 `tmp/jms_subscription.cache`（含节点凭证，0600、已 gitignore）。

    **两级缓存 + network 开关（2026-10-30 再修，接 T177）**：
      ① **TTL 内直接用缓存、不发请求**——`ip_pool.build_multi_config()` 会在每次切端口时
         调本函数，若每次都拉订阅，切换延迟会从 1ms 涨到秒级。
      ② 拉取失败时用**过期缓存兜底**（标记 stale=True），避免订阅源临时不可用就让整条
         交易链路瘫痪。
      ③ **`network=False` = 热路径专用：只读本地缓存、绝不发网络请求**（不管缓存多旧）。
         为什么必须这么改：盯盘 / 下单每轮采样都会调本函数 3 次左右（build_multi_config /
         fast_switch / ensure），10 分钟 TTL 会让每个会话每 10 分钟**在交易主链路上**做一次
         真实拉取——这正是 2026-10-29 卡死（甚至下不了单）的放大器。本地那 6 个 IP 与节点
         凭证都已经在缓存文件里，交易根本不需要重新拉订阅；拉订阅只用于「发现订阅商换节点」
         这类**慢信号**，应该交给低频的守护路径（proxy_guard / preflight / 手工 CLI）。
         （无缓存时例外：允许联网引导一次，否则首次跑不起来。）

    `force=True` 强制真拉取（运维排查 / 用户手工刷新时用）。
    """
    global _last_fetch_fail_at
    if not network:
        cached = _read_cache()
        if cached:
            return {'ok': True, 'nodes': cached, 'ips': ip_pool_from_nodes(cached),
                    'reason': 'cache_only（热路径不联网）', 'stale': True}
        network = True   # 本地无缓存（首次运行）→ 仍允许联网引导一次（有界）
    if cache and not force and ttl:
        cached = _read_cache()
        if cached:
            try:
                age = time.time() - os.path.getmtime(CACHE_PATH)
            except Exception:
                age = ttl + 1
            if age < ttl:
                return {'ok': True, 'nodes': cached, 'ips': ip_pool_from_nodes(cached),
                        'reason': f'cache({age:.0f}s)', 'stale': False}
            # ── 缓存已过期（T177，2026-10-30）──────────────────────────────
            # 旧行为：过期就同步真拉，且每条通道 25s，拉不到再回退缓存 → 每次调用卡 60s 以上。
            # 新行为：① 刚失败过（退避窗口内）→ 直接返回旧缓存、不做任何网络请求；
            #         ② 否则用 5s 快拉，拉不到也立刻返回旧缓存。
            if _last_fetch_fail_at and (time.time() - _last_fetch_fail_at) < FETCH_FAIL_BACKOFF:
                return {'ok': True, 'nodes': cached, 'ips': ip_pool_from_nodes(cached),
                        'reason': f'cache({age:.0f}s，拉取失败退避中，'
                                  f'{int(FETCH_FAIL_BACKOFF - (time.time() - _last_fetch_fail_at))}s 后重试)',
                        'stale': True}
            ok, text, reason = fetch_subscription(timeout=FAST_FETCH_TIMEOUT)
            nodes = parse_subscription(text) if ok else {}
            if nodes:
                if cache:
                    _write_cache(nodes)
                return {'ok': True, 'nodes': nodes, 'ips': ip_pool_from_nodes(nodes),
                        'reason': 'ok', 'stale': False}
            _last_fetch_fail_at = time.time()
            return {'ok': True, 'nodes': cached, 'ips': ip_pool_from_nodes(cached),
                    'reason': f'{reason}（缓存已过期，已用旧缓存兜底，'
                              f'{FETCH_FAIL_BACKOFF}s 内不再重试）', 'stale': True}
    ok, text, reason = fetch_subscription()
    nodes = parse_subscription(text) if ok else {}
    if nodes:
        if cache:
            _write_cache(nodes)
        return {'ok': True, 'nodes': nodes, 'ips': ip_pool_from_nodes(nodes),
                'reason': 'ok', 'stale': False}
    _last_fetch_fail_at = time.time()
    cached = _read_cache()
    if cached:
        return {'ok': True, 'nodes': cached, 'ips': ip_pool_from_nodes(cached),
                'reason': f'{reason}（已用本地缓存兜底）', 'stale': True}
    return {'ok': False, 'nodes': {}, 'ips': [], 'reason': reason, 'stale': False}


def _write_cache(nodes):
    try:
        os.makedirs(TMP_DIR, exist_ok=True)
        with open(CACHE_PATH, 'w') as f:
            json.dump(nodes, f, ensure_ascii=False)
        os.chmod(CACHE_PATH, 0o600)
    except Exception:
        pass


def _read_cache():
    try:
        with open(CACHE_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def main(argv=None):
    """CLI：只输出数量与是否同步，**绝不打印链接与 IP 原文**。

    用法：
        python3 jms_pool.py            # 拉取 + 解析 + 与本地白名单镜像比对，打印摘要
        python3 jms_pool.py --verbose  # 额外打印每个 IP 的归属（本机终端看，别贴出去）
    """
    argv = argv if argv is not None else sys.argv[1:]
    url = load_subscription_url()
    if not url:
        print('❌ 未配置 JMS 订阅链接。请在本机 config.json 的 jms.subscription_url 填入'
              '（该文件已被 .gitignore 忽略，不会进仓库）。')
        return 3

    r = refresh()
    if not r['ok']:
        print(f'❌ 拉取订阅失败：{r["reason"]}（未打印链接与内容）')
        return 2

    print(f'✅ 拉取成功：{len(r["nodes"])} 个节点，{len(r["ips"])} 个入口 IP'
          + ('（用的是本地缓存兜底）' if r['stale'] else ''))

    try:
        import proxy_guard as G
        wl = G.load_whitelist() or []
    except Exception:
        wl = []
    if not wl:
        print('⚠️ 本地老虎白名单镜像为空，无法比对')
        return 0

    d = compare(r['ips'], wl)
    if d['in_sync']:
        print(f'✅ JMS 池与老虎白名单一致（{d["jms_count"]} 个 IP）')
    else:
        print(f'⚠️ 不一致：JMS {d["jms_count"]} 个 / 白名单 {d["whitelist_count"]} 个')
        if d['missing_in_whitelist']:
            print(f'   → JMS 有 {len(d["missing_in_whitelist"])} 个 IP 不在老虎白名单内，'
                  f'这些节点当前用不了，需去老虎开发者页加白')
        if d['stale_in_whitelist']:
            print(f'   → 白名单有 {len(d["stale_in_whitelist"])} 个 IP 已不在 JMS 池内，'
                  f'建议从老虎页清掉')
        if '--verbose' in argv:
            for ip in d['missing_in_whitelist']:
                print(f'      [待加白] {ip}')
            for ip in d['stale_in_whitelist']:
                print(f'      [可清除] {ip}')
    return 0 if d['in_sync'] else 1


if __name__ == '__main__':
    sys.exit(main())
