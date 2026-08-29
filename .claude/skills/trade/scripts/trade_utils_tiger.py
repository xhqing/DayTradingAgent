#!/usr/bin/env python3
"""港股交易工具库（老虎证券开放平台，港股默认账户）。

港股默认账户即老虎（2026-08-05 起港美股均走老虎、不再有备选账户，见 CHANGELOG），本库自包含：
配置加载、港股 symbol / lot_size / tick、行情、下单（开仓 LMT+附加止损、平仓 MKT、独立止损
STP）、持仓 / 资产 / 订单查询、撤单、成交回查。

✅ 实测状态（2026-08-03 paper 三动作全链路开盘实测通过）：
- ✅ 已实测：配置加载、paper 判定、港股 symbol 格式、lot_size / tick、资产 / 持仓 / 订单只读、
  行情，以及下单链路——开仓 LMT+附加止损腿（OrderLeg('LOSS') 落成独立 STP 单、主单成交后
  HELD 监控）、平仓 MKT（Filled、avg_fill_price 真实成交价）、独立止损 STP（modify aux_price
  移损、旧单可独立撤销）。
- 🔧 实测发现并修复 2 个 bug（2026-08-03）：① _make_order 的 order_type 传枚举对象致 place_order
  序列化失败（TypeError: Object of type OrderType is not JSON serializable）——须传字符串
  'LMT'/'MKT'/'STP'；② check_order_filled_tiger 直接 str(OrderStatus 枚举) 得
  'OrderStatus.FILLED'、'Filled' in 它恒 False → 已成交误判未成交并撤已成交单——须取
  status.value（'Filled'）再判断。**券商行为只信直接实测**——本模块订单语义已按实测落地。

本模块要点：
- **配置加载**：`TigerOpenClientConfig(props_path=...)` 构造（私钥自动从 properties 的
  private_key_pk1/pk8 读取）。⚠️ 不要用 `get_client_config(props_path=...)`——该函数会先
  硬读 `private_key_path` 参数（None 直接 TypeError，2026-08-02 实测复现），必须显式传
  private_key_path 或走 TigerOpenClientConfig。
- **paper 判定**：account 为 17 位纯数字账户号即自动判为模拟账户（is_paper=True），网关域名
  自动走 license-PAPER（domain_conf 已含 TBNZ-PAPER / TBSG-PAPER，实测确认）。paper 账户号
  由用户提供后写入 properties 的 account 字段即可切换。
- **港股 symbol 格式**：老虎只认 5 位带前导 0 的裸数字代码（'02800' / '00700'，2026-08-02
  实测：HK.02800 / 2800.HK / 700.HK 均报「We don't support trading of this」）。富途格式
  HK.02800 → 取 '.' 后 5 位。
- **每手股数 lot_size**：因标的而异（盈富 500、腾讯/阿里 100），从 get_contract.lot_size 取。
- **价位 tick**：港交所价位表，从 get_contract.tick_sizes 取（实测返回完整区间表）。
- **币种**：港股 HKD，equity 取 get_assets().summary.net_liquidation（currency 同源）。

三个动作的订单类型：
- 开仓：主单 LMT + 附加止损腿 OrderLeg('LOSS', price)（老虎附加订单仅限价单支持）。
- 移动止损：modify 现有活动 STP 单的 aux_price（2026-08-05 实测单步、无撤单 race；仅
  fallback 才先下新再撤旧）、量严格=持仓量。
- 平仓：**先撤全部未触发止损单、再下 MKT 市价单**（2026-08-03 午后实测：挂着的止损单占用
  持仓可平额度，Buy 平空单被拒「exceeds holdings」；先撤止损再平立即成交。
  平仓脚本 close_position_tiger.py 已按此顺序实现）。
"""

import os
import sys
import time
from pathlib import Path


# ---------------------------------------------------------------------------
# 配置加载（自包含；props_path 默认 ~/.tigeropen/）
# ---------------------------------------------------------------------------

def load_config(props_path=None, account=None):
    """创建老虎 SDK 客户端配置。

    props_path：properties 目录或文件（默认 ~/.tigeropen/）。SDK 支持环境变量
    TIGEROPEN_TIGER_ID / TIGEROPEN_ACCOUNT / TIGEROPEN_PRIVATE_KEY / TIGEROPEN_PROPS_PATH
    等覆盖（优先级：参数 > 环境变量 > properties 文件）。

    account（2026-08-12 立，实盘备选账户支持）：选择账户——
    - None / 'paper'：用 properties 默认账户（模拟账户，tiger.account）。
    - 'live'：切老虎实盘账户（从 accounts.json 的 tiger.account_live 读实盘账户号、覆盖 config.account）。
      ⚠️ 实盘=真钱，调用方须已征得用户明确同意（SKILL「auto 模式的账户选择」：用户明确说切实盘 + AI 额外确认两道闸）。
    - 具体账户号字符串：直接用该账户号（覆盖 config.account）。
    config.account 是可写属性（实测），切换不碰 properties 文件。

    ⚠️ 不走 get_client_config(props_path=...)（2026-08-02 实测：它先硬读 private_key_path
    参数、None 即 TypeError，读不到 properties 内嵌私钥）；TigerOpenClientConfig 会从
    properties 的 private_key_pk1/pk8 自动读私钥。

    ⚠️ is_paper 网关路由 bug 修复（2026-08-13）：老虎 SDK 的 account.setter 是单向开关——
    `if is_paper_account(value): is_paper = True`，只在「新账户号是 paper」时设 True、
    切到**非 paper 账户号（如实盘）时不清零**。构造时 _load_props 读到 properties 的 17 位
    模拟号已把 is_paper 设成 True，之后赋值实盘号 is_paper 卡在 True，导致 server_url 仍指向
    sandbox 沙箱网关（openapi-sandbox.tigerfintech.com）而非生产网关（openapi.tigerfintech.com）——
    实盘请求走错网关、行为不可控。故本函数在确定 config.account 后，用 SDK 自己的
    AccountUtil.is_paper_account() 按**当前账户号真实属性**显式设定 is_paper（赋 True 或 False），
    保证 server_url 路由与账户号一致。三个分支（默认/live/具体号）统一走 _sync_is_paper。

    ⚠️ 代理支持（2026-08-14 立，apply_proxy；2026-08-23 注释中性化）：老虎 SDK 用
    urllib3.PoolManager 发请求、**不读 HTTP(S)_PROXY 环境变量**（curl / requests 认、
    urllib3 不认），环境变量方式对 SDK 无效、必须代码层把 web_utils.http_pool 换成
    urllib3.ProxyManager。背景：券商按「指令发出的网络环境」判定下单请求是否受理，
    不满足合规要求的地域出口下实盘开仓/加仓会被监管类错误码拒（仅允许平仓/减仓/转出）；
    合规出口实测下单正常受理（2026-08-14 验证，拒单原因从监管拦截变为普通资金不足）。
    配置走 skill config.json 的 proxy 节（enabled / http_proxy / apply_scope），默认
    live_only（仅实盘走代理、模拟盘直连）。apply_proxy 失败回退直连并警告。
    """
    from tigeropen.tiger_open_config import TigerOpenClientConfig
    if props_path is None:
        props_path = os.path.expanduser("~/.tigeropen/")
    config = TigerOpenClientConfig(props_path=props_path)
    # 账户选择（2026-08-12）
    if account in (None, 'paper'):
        pass   # 用 properties 默认账户（模拟）
    elif account == 'live':
        live_acct = _read_accounts_json_field('tiger', 'account_live')
        if not live_acct:
            raise ValueError("切实盘失败：accounts.json 未配置 tiger.account_live（实盘账户号）。"
                             "请在 accounts.json 的 tiger 段加 account_live 字段。")
        config.account = live_acct
    else:   # 具体账户号字符串
        config.account = account
    _sync_is_paper(config)   # 修复 SDK 单向开关 bug，确保 is_paper / 网关路由与账户号一致
    apply_proxy(config)      # SDK 不读环境变量代理，代码层切 ProxyManager（2026-08-14）
    return config


def apply_proxy(config):
    """按 skill config.json 的 proxy 节给老虎 SDK 挂代理（2026-08-14 立；2026-08-21 加实盘
    解锁闸——实盘误开防护「会话级解锁 + 网络层物理拒单」的网络层检查点）。

    老虎 SDK 的请求出口在 tigeropen.common.util.web_utils.http_pool（模块级 urllib3.PoolManager
    单例），不读 HTTP(S)_PROXY 环境变量——须把该单例替换为 urllib3.ProxyManager 才能走代理
    （对进程内全部后续 SDK 请求生效）。

    为什么需要（合规背景，2026-06-12 新规；2026-08-23 注释中性化）：老虎按「指令发出的
    网络环境」判定，不满足合规要求的地域出口下实盘开仓/加仓被监管类错误码拒（仅允许
    平仓/减仓/转出）。合规出口实测下单正常受理（2026-08-14 验证：同一实盘账户、同一凭证，
    受限出口拒单、合规出口受理，拒单原因从监管拦截变为普通资金不足）。

    **实盘解锁闸（2026-08-21 立）**：当前账户为实盘（== accounts.json 的 tiger.account_live）
    且解锁文件无效（live_unlock.live_unlock_valid()）时**跳过挂代理保持直连**——实盘请求
    本地直连 → 被老虎 IP 白名单在连接层物理拒（本地出口不在白名单，实测「access forbidden:
    request ip is not in ip whitelist」）。默认态（无解锁文件）实盘物理不可达；用户经
    AskUserQuestion 授权 + live_unlock.py verify-auth 写解锁后本函数才给实盘挂代理。
    详见 scripts/live_unlock.py 与 SKILL.md「auto 模式的账户选择」节。

    配置（config.json 的 proxy 节）：
    - enabled：总开关，false 直接返回（保持直连）。
    - http_proxy：代理地址（默认 http://127.0.0.1:1087 = 本机 xpilot Xray 的 HTTP 入站口；
      xpilot 路由白名单须含 domain:openapi.tigerfintech.com，注意必须带 domain: 前缀——
      裸域名会被 xpilot 生成配置时静默丢弃，2026-08-14 踩过）。
    - apply_scope：'live_only'（默认，仅实盘/非模拟账户走代理——模拟盘无监管限制、直连更快）、
      'all'（全部走代理）、'off'（等价 enabled=false）。
    - accounts_live_required：live_only 下非实盘账户是否仍强制走代理（默认 false——监管拦截
      只作用于实盘开仓，模拟盘无此限制、直连更快）。

    失败回退：ProxyManager 构造或代理连通性探测失败时保留直连并 stderr 警告——代理挂了
    不该炸掉模拟盘的只读查询路径。
    """
    import json as _json
    cfg_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'config.json')
    proxy_cfg = {}
    try:
        with open(cfg_path) as _f:
            proxy_cfg = _json.load(_f).get('proxy', {})
    except Exception:
        pass   # config.json 缺失/损坏 → 无代理配置 → 保持直连
    if not proxy_cfg.get('enabled', False) or proxy_cfg.get('apply_scope') == 'off':
        return
    scope = proxy_cfg.get('apply_scope', 'live_only')
    # 判定当前账户是否需要代理
    if scope == 'live_only':
        live_acct = _read_accounts_json_field('tiger', 'account_live')
        is_live = live_acct is not None and str(config.account) == str(live_acct)
        if not is_live and not proxy_cfg.get('accounts_live_required', False):
            return   # 非实盘且不强制 → 直连
        # is_live 或 accounts_live_required=true 都走代理（保守：账户号判断失误也不漏）
    # 实盘解锁闸（2026-08-21）：当前账户是实盘且解锁文件无效 → 跳过挂代理（保持直连 =
    # 被 IP 白名单连接层物理拒）。fail-closed：解锁校验抛异常也按未解锁处理。
    live_acct = _read_accounts_json_field('tiger', 'account_live')
    if live_acct is not None and str(config.account) == str(live_acct):
        try:
            import live_unlock as _LU
            _ok, _reason = _LU.live_unlock_valid()
        except Exception as _e:
            _ok, _reason = False, f"unlock_check_error: {_e}"
        if not _ok:
            print(f"🔒 实盘代理未解锁（{_reason}）：实盘请求将本地直连、被老虎 IP 白名单拒"
                  f"（access forbidden）——实盘开仓物理不可达是默认态。授权路径：AskUserQuestion "
                  f"实盘授权（hook 见证）→ python3 scripts/live_unlock.py verify-auth。",
                  file=sys.stderr)
            return
    # 挂代理
    proxy_url = proxy_cfg.get('http_proxy', 'http://127.0.0.1:1087')
    try:
        import urllib3
        from tigeropen.common.util import web_utils
        probe = urllib3.PoolManager()   # 探测代理端口在监听（直连探测，不发业务流量）
        import socket as _socket
        host_port = proxy_url.split('//')[-1]
        host, port = host_port.split(':')[0], int(host_port.split(':')[1])
        s = _socket.create_connection((host, port), timeout=2)
        s.close()
        web_utils.http_pool = urllib3.ProxyManager(proxy_url)
        print(f"✅ 老虎 SDK 代理已启用: {proxy_url}（scope={scope}，账户 {mask_account(config.account)}）",
              file=sys.stderr)
        # WebSocket 链路同步挂代理（2026-08-17 立）：PushClient 走裸 socket.create_connection
        # （SDK transport 层），不经过上面换掉的 web_utils.http_pool（那只管 REST）——IP 白名单
        # 上线后 WS 直连出口（本地出口）被拒「code=4 access forbidden」（2026-08-17 实录：
        # 白名单 13:05 上线、13:28 后 ws_segment 全部连不上）。此处把 socket.create_connection
        # socks 化，让本进程后续建的 WS 连接也走代理出口（白名单内 IP）。
        apply_socket_proxy(proxy_url)
    except Exception as e:
        print(f"⚠️ 老虎 SDK 代理启用失败（回退直连；本地直连实盘开仓会被监管类错误码拒）: {e}",
              file=sys.stderr)


def apply_socket_proxy(proxy_url='http://127.0.0.1:1087'):
    """把本进程的 socket.create_connection 换成「先经本地 socks5/http 代理建链」的版本——
    供老虎 PushClient（WebSocket）走代理（2026-08-17 立）。

    为什么单独一个函数：SDK 的 WS 建链在 tigeropen.push.network.transport，用的是
    socket.create_connection 裸 TCP + TLS wrap，不吃 web_utils.http_pool（REST 专用）；
    IP 白名单上线后 WS 直连被拒（access forbidden，直连出口非白名单 IP）。monkeypatch
    全局 socket.create_connection 是让 SDK WS 链路走代理的最小侵入方式（SDK 未暴露
    WS 代理配置项）。

    代理协议：proxy_url 是 http:// 时 socks 端口取「http 端口 −7」（xpilot 约定
    http 1087 / socks 1080）；本身是 socks5:// 则直接用。PySocks 不可用或 socks 端口
    不通时保留直连并 stderr 警告（WS 退回直连 = 白名单下连不上，报警提示、不炸进程）。

    已 patch 过则幂等跳过（重复调用不叠加包装）。
    """
    import socket as _socket
    if getattr(_socket.create_connection, '_tiger_proxy_patched', False):
        return
    import socks
    # 解析 socks 端口
    host_port = proxy_url.split('//')[-1]
    phost, pport = host_port.split(':')[0], int(host_port.split(':')[1])
    socks_port = pport - 7 if proxy_url.startswith('http://') else pport   # xpilot: 1087→1080
    # 探测 socks 端口在监听
    try:
        s = _socket.create_connection((phost, socks_port), timeout=2)
        s.close()
    except Exception as e:
        print(f"⚠️ 老虎 WS 代理启用失败（socks {phost}:{socks_port} 不通，WS 保持直连；"
              f"IP 白名单下直连会被拒 access forbidden）: {e}", file=sys.stderr)
        return
    _orig_create = _socket.create_connection

    def _proxied_create(address, timeout=None, source_address=None):
        s = socks.socksocket(_socket.AF_INET, _socket.SOCK_STREAM)
        s.set_proxy(socks.SOCKS5, phost, socks_port)
        if timeout:
            s.settimeout(timeout)
        s.connect(address)
        return s
    _proxied_create._tiger_proxy_patched = True
    _socket.create_connection = _proxied_create
    print(f"✅ 老虎 WS 链路代理已启用: socks5 {phost}:{socks_port}（socket.create_connection 已 socks 化）",
          file=sys.stderr)


def _sync_is_paper(config):
    """按 config.account 真实属性显式设定 is_paper（修复老虎 SDK account.setter 单向开关 bug，
    2026-08-13）。

    SDK 的 account.setter 只在「账户号是 paper」时把 is_paper 设 True、切到非 paper 账户时不清零，
    导致从模拟（properties 默认 17 位号、构造时已 is_paper=True）切实盘时 is_paper 卡 True、
    server_url 仍走 sandbox 沙箱网关。这里用 SDK 权威判定 AccountUtil.is_paper_account() 同步：
    纯数字且长度 ≥ PAPER_ACCOUNT_DIGIT_LEN(17) → True（模拟）、否则 False（实盘）。

    设完 is_paper 后必须重算 server_url：构造时 refresh_server_info 已按（构造期的 is_paper + license）
    定了 server_url，这里改 is_paper 后调 refresh_server_info() 让 SDK 按（新 is_paper + license）重选
    生产 / 沙箱网关（实测改 is_paper 不自动重算 server_url，须显式调）。
    """
    from tigeropen.common.util.account_util import AccountUtil
    config.is_paper = bool(AccountUtil.is_paper_account(config.account))
    try:
        config.refresh_server_info()
    except Exception as e:
        print(f"⚠️ refresh_server_info 失败（is_paper 已修正，但 server_url 可能未重算）: {e}",
              file=sys.stderr)


def _read_accounts_json_field(section, field):
    """从项目 accounts.json（已 gitignore）读指定字段值。读不到返回 None。

    accounts.json 路径优先级：环境变量 ACCOUNTS_JSON > 脚本同目录 ../accounts.json（skill 根）。
    纯读取、不写入；文件不存在或字段缺失返回 None（调用方按默认处理）。
    """
    import json
    candidates = []
    env_path = os.environ.get('ACCOUNTS_JSON')
    if env_path:
        candidates.append(env_path)
    candidates.append(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'accounts.json'))
    for path in candidates:
        if os.path.exists(path):
            try:
                with open(path) as f:
                    data = json.load(f)
                return data.get(section, {}).get(field)
            except Exception:
                return None
    return None


def mask_account(account):
    """账户号打码（2026-08-17 立，凭证防泄漏）：输出首 2 位 + **** + 尾 2 位。

    为什么：实盘确认流程要求 AI 向用户展示「将用哪个账户下单」，此前 AI 直接读
    accounts.json 拿完整实盘号、写进对话上下文——凭证号随上下文进入模型请求，属
    泄漏面。改为：脚本 / AI 只输出打码号（如 67****91），用户自己知道完整号、足够
    确认是对是错，完整号不再进入上下文。
    口径：长度 >4 才打码（首 2 尾 2）；≤4 位直接全打码 ****（过短时首尾两位就能猜出大半）。
    """
    s = str(account)
    if not s:
        return ''
    if len(s) <= 4:
        return '****'
    return f"{s[:2]}****{s[-2:]}"


def print_masked_live_account():
    """打印实盘账户打码号（供切实盘确认流程用，AI 上下文只见打码口径）。

    输出一行 JSON：{"account_live_masked": "67****91", "has_live": true}——
    has_live=false 表示 accounts.json 未配置实盘账户。退出码恒 0（打印类子命令，
    不该因未配置而炸调用方）。用法：python3 trade_utils_tiger.py --masked-live-account
    """
    import json
    live = _read_accounts_json_field('tiger', 'account_live')
    paper = _read_accounts_json_field('tiger', 'account')
    print(json.dumps({
        "account_live_masked": mask_account(live) if live else None,
        "account_paper_masked": mask_account(paper) if paper else None,
        "has_live": live is not None,
    }))


if __name__ == '__main__':
    import argparse
    _ap = argparse.ArgumentParser(description='账户信息打码查询（凭证不进上下文）')
    _ap.add_argument('--masked-live-account', action='store_true',
                     help='打印实盘 / 模拟账户打码号（67****91 形式）')
    _args = _ap.parse_args()
    if _args.masked_live_account:
        print_masked_live_account()
    else:
        _ap.print_help()


def new_trade_client(config=None):
    """创建老虎 TradeClient（下单 / 持仓 / 资产 / 订单查询）。"""
    from tigeropen.trade.trade_client import TradeClient
    return TradeClient(config if config is not None else load_config())


def new_quote_client(config=None):
    """创建老虎 QuoteClient（行情查询）。"""
    from tigeropen.quote.quote_client import QuoteClient
    return QuoteClient(config if config is not None else load_config())


def ring_after_fill(market, symbol, note="", ring_type="open"):
    """auto 模式成交后响铃提醒（2026-08-19 立，TODO「auto 模式开仓成功后响铃提醒」）。

    为什么：auto 模式 AI 直接调脚本开仓，用户若不盯屏幕感知不到已开仓——成交确认后
    响一声把用户注意力拉回来。挂在**开仓脚本成交确认之后**（脚本内置 = 工具强制，
    不靠 AI 记得调 ring.sh；见项目 CLAUDE.md「文档规定必须尽可能配工具强制」）。

    实现：调 ring.sh（复用现有响铃机制与音色映射），但 RING_LOG=ring-log-auto.csv
    隔离响铃记录——signal 的 ring-log.csv 末行被 log_action.sh（signal 模式读末行当
    拍板时刻）与 resume.py（signal 模式断层检测）消费，auto 写入会把 signal 会话的
    动作时间戳 / 断层判据污染成 auto 的开仓时刻（多会话并行时两会话互踩）。

    静默失败（响铃是提醒件、不是交易链路）：ring.sh 不存在 / 播放失败只打 warning，
    不抛异常、不影响开仓结果输出。子进程异步放后台，不阻塞 JSON 输出。

    参数：market "HK"/"US"（只用于 note 前缀）、symbol 富途格式（HK.00700/US.MU）、
    note 备注写响铃 log、ring_type open/close/ts（音色，见 ring.sh）。
    """
    import subprocess
    scripts_dir = Path(__file__).resolve().parent
    ring_sh = scripts_dir / "ring.sh"
    try:
        env = dict(os.environ)
        env["RING_LOG"] = "ring-log-auto.csv"
        subprocess.Popen(
            ["bash", str(ring_sh), ring_type, symbol, note],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env=env,
        )
    except Exception as e:
        print(f"⚠️ 开仓响铃失败（不影响交易）：{e}", file=sys.stderr)


# ---------------------------------------------------------------------------
# symbol 格式转换（富途 HK.02800 ↔ 老虎 02800）
# ---------------------------------------------------------------------------

def to_tiger_symbol(symbol):
    """富途 → 老虎：HK.02800 → 02800（老虎只认 5 位带前导 0 的裸数字代码，2026-08-02 实测：
    HK.02800 / 2800.HK / 700.HK 均不支持，'02800' / '00700' 可用）。美股不支持（老虎美股无权限）。
    """
    if not symbol or "." not in symbol:
        return symbol
    market, code = symbol.split(".", 1)
    if market != "HK":
        raise ValueError(f"老虎脚本只支持港股（HK.xxx），收到 {symbol}")
    return code  # 直接返回、不做 zfill 补零（2026-08-17 注释更正：此前注释误称「如传入无前导 0 则补足」，实际不补）
    # 注：本函数不补前导 0——传入 HK.700 这类无前导 0 的代码会原样返回 "700"、券商侧报不支持。
    # 调用方必须传带前导零的 5 位富途格式代码（如 HK.00700），保证 "." 后本就是 5 位数字。


def to_futu_symbol_tiger(tiger_symbol):
    """老虎 → 富途：02800 → HK.02800（补前导 0 到 5 位）。"""
    code = str(tiger_symbol)
    if "." in code:
        code = code.split(".")[0]
    return f"HK.{code.zfill(5)}"


# ---------------------------------------------------------------------------
# 合约查询：lot_size / tick / 名称（get_contract，2026-08-02 实测通过）
# ---------------------------------------------------------------------------

def get_contract_tiger(tc, symbol):
    """查港股合约（get_contract）。返回 Contract 对象（含 lot_size / tick_sizes / name /
    shortable / shortable_count）或 None。symbol 传富途格式 HK.02800。"""
    from tigeropen.common.consts import SecurityType
    return tc.get_contract(to_tiger_symbol(symbol), sec_type=SecurityType.STK)


def get_lot_size_tiger(tc, symbol):
    """港股每手股数（get_contract.lot_size，实测 02800=500、00700=100）。返回 int 或 None。"""
    try:
        c = get_contract_tiger(tc, symbol)
        if c is not None:
            ls = getattr(c, "lot_size", None)
            if ls:
                return int(ls)
    except Exception as e:
        print(f"⚠️ 查 lot_size 失败 {symbol}: {e}", file=sys.stderr)
    return None


def get_tick_sizes_tiger(tc, symbol):
    """港股价位表（get_contract.tick_sizes，港交所规则：随价格区间变化）。返回区间列表
    [{'begin','end','type','tick_size'}, ...] 或 None。"""
    try:
        c = get_contract_tiger(tc, symbol)
        if c is not None:
            return getattr(c, "tick_sizes", None)
    except Exception as e:
        print(f"⚠️ 查 tick 价位表失败 {symbol}: {e}", file=sys.stderr)
    return None


def _tick_from_table(price, tick_sizes):
    """从价位表按价格查最小报价单位 tick。区间匹配（begin, end]；2026-08-03 paper 实测：
    开仓 LMT 486.2、移损 trigger 484.0 均正确取整合 tick，边界语义验证通过。"""
    if not tick_sizes:
        return None
    p = float(price)
    for row in tick_sizes:
        try:
            begin = float(row.get("begin", 0))
            end = float(row.get("end", float("inf")))
            if p > begin and p <= end:
                return float(row.get("tick_size"))
        except (TypeError, ValueError):
            continue
    return None


def round_to_tick_tiger(price, tick_sizes=None):
    """把价格向下取整到港股 tick（限价单必须合 tick）。tick_sizes 缺失时 fallback 固定价位表
    （2025-08-04 调整版）。

    ⚠️ 恒向下取整（floor）、方向性后果要心里有数（历史一致、非 bug，不改行为）：
    - 卖方限价：取整后比原价低最多一个 tick → 低于当前 bid 也能成交（更激进、可能贱卖一点）；
    - 做多止损触发价：被压低最多一个 tick → 实际触发更晚、实际 max_loss 略大于计划值。"""
    import math
    tick = _tick_from_table(price, tick_sizes)
    if not tick:
        tick = get_tick_hk_fallback(price)
    return round(math.floor(price / tick) * tick, 6)


# 港交所最小报价单位（价位表），2025-08-04 调整版
# (价格上界 HKD, tick)；价格落在 (上一上界, 本上界] 用本 tick
_HK_TICK_TABLE = [
    (0.25, 0.001),
    (0.50, 0.005),
    (10.00, 0.010),
    (20.00, 0.010),   # 2025-08-04 从 0.020 下调为 0.010
    (100.00, 0.050),
    (200.00, 0.100),
    (500.00, 0.200),
    (1000.00, 0.500),
    (2000.00, 1.000),
    (5000.00, 2.000),
    (float("inf"), 5.000),
]


def get_tick_hk_fallback(price):
    """按价格查港股最小报价单位（tick_sizes 缺失时兜底）。"""
    if price is None or price <= 0:
        return 0.001
    for upper, tick in _HK_TICK_TABLE:
        if price <= upper:
            return tick
    return 5.000


# ---------------------------------------------------------------------------
# 行情（QuoteClient.get_stock_briefs，2026-08-02 实测通过；返回 DataFrame）
# ---------------------------------------------------------------------------

def get_quote_tiger(config, symbol, retries=3):
    """港股最新报价。返回 dict {symbol, last, bid, ask, high, low, volume, latest_time} 或 None。

    ⚠️ get_stock_briefs 返回 pandas DataFrame（不是对象列表），按列名取
    （df['latest_price']），用 getattr 会得 None（2026-07-07 实测踩坑）。latest_time 为
    毫秒 Unix 时间戳。
    """
    qc = new_quote_client(config)
    tig = to_tiger_symbol(symbol)
    for attempt in range(retries):
        try:
            df = qc.get_stock_briefs([tig])
            if df is None or len(df) == 0:
                return None
            row = df.iloc[0]

            def _f(v):
                try:
                    return float(v) if v is not None and str(v) not in ("nan", "None") else None
                except (TypeError, ValueError):
                    return None

            return {
                "symbol": symbol,
                "last": _f(row.get("latest_price")),
                "bid": _f(row.get("bid_price")),
                "ask": _f(row.get("ask_price")),
                "high": _f(row.get("high")),
                "low": _f(row.get("low")),
                "volume": int(row.get("volume") or 0),
                "latest_time": row.get("latest_time"),
            }
        except Exception as e:
            # 2026-08-21 实测：实盘账户（生产网关）港股行情报 code=4 permission denied
            #（模拟账户沙箱网关有港股行情权限、实盘无）——按「美股脚本行情源走富途」同款方案，
            # 港股老虎行情失败时回落富途 OpenD 快照（本地网关、港股行情免费可用）。
            try:
                return get_quote_hk_futu(symbol)
            except Exception:
                pass
            if attempt < retries - 1:
                time.sleep(1)
                continue
            raise RuntimeError(f"老虎港股行情失败 {symbol}: {e}") from e
    return None


def get_quote_hk_futu(symbol):
    """港股最新报价——富途 OpenD 快照（老虎实盘账户港股无行情权限时的回落源，
    2026-08-21 立；与 get_quote_us 同款实现）。返回 dict 或 None。"""
    from futu import OpenQuoteContext, RET_OK
    qc = OpenQuoteContext("127.0.0.1", 11111)
    try:
        ret, df = qc.get_market_snapshot([symbol])
        if ret != RET_OK or df is None or len(df) == 0:
            return None
        row = df.iloc[0]

        def _f(v):
            try:
                return float(v) if v is not None and str(v) not in ("nan", "None") else None
            except (TypeError, ValueError):
                return None

        return {
            "symbol": symbol,
            "last": _f(row.get("last_price")),
            "bid": _f(row.get("bid_price")),
            "ask": _f(row.get("ask_price")),
            "high": _f(row.get("high_price")),
            "low": _f(row.get("low_price")),
            "volume": int(row.get("volume") or 0),
            "latest_time": row.get("update_time"),
        }
    finally:
        qc.close()


# ---------------------------------------------------------------------------
# 权益（老虎账户净值。2026-08-05 实测确认：get_prime_assets(base_currency) 直接返回
# 对应币种的账户净值——港股 base_currency='HKD'、美股 base_currency='USD'，无需外部汇率；
# 见 CHANGELOG 2026-08-05「equity 口径修复」）
# ---------------------------------------------------------------------------

def note_equity_baseline(value, currency):
    """【已退役，2026-08-29 用户裁定】实盘净值特征串追加上基准黑名单（2026-08-20 立）。

    2026-08-29 用户裁定：实盘账户净值不属敏感财务信息（账户资金只是个人全部财产的
    一小部分、不能代表个人财务状况），照常可写——「实盘净值禁止写入被跟踪文件」
    旧规废止，equity_guard.py hook 已从 settings.json 摘除。本函数保留为空操作
    （调用点一并清理），防止旧调用点报 NameError；基准黑名单文件
    .claude/hooks/equity-baseline.local 不再写入。沿革见 CLAUDE.md「实盘净值不属
    敏感信息、照常可写」节与 CHANGELOG 2026-08-29 条。
    """
    return None


def load_equity_tiger(config=None, base_currency=None, account=None):
    """老虎账户净值。返回 (equity, currency)。

    - base_currency='HKD'（港股交易口径）：get_prime_assets(base_currency='HKD') 证券段
      net_liquidation——全账户权益按实时汇率折算 HKD（2026-08-05 实测为模拟账户 7,819,536.41 HKD =
      总净值 996,932.71 USD × 7.843595，汇率来自老虎自身 currency_assets[].forex_rate，
      不依赖外部数据源）。
    - base_currency='USD' / 不传（美股或兼容旧调用）：get_assets summary.net_liquidation
      （USD，实测 996,932.71，模拟账户）。

    - account（2026-08-20 立，实盘盯盘配套）：None/paper=默认模拟账户、'live'=老虎实盘账户
      （load_config(account='live') 切生产网关 + 代理）。⚠️ 实盘=真钱，调用方须已征得用户
      明确同意（SKILL「auto 模式的账户选择」双闸）。⚠️ 2026-08-20 事故背景：preflight /
      resume 曾固定走默认模拟账户取净值——实盘盯盘时 B 按模拟账户约 789 万 HKD 算、而实盘
      净值远小于模拟净值，偏差 68 倍，单笔风险上限完全错位。修复 = 取净值与下单同账户
      （load_config(account) 两处统一），杜绝「用模拟净值管实盘风险」。
      （2026-08-29 起 account='live' 查到净值不再记基准黑名单——实盘净值不属敏感信息、
      照常可写，note_equity_baseline 已退役为空操作。）

    ⚠️ 2026-08-02 实测：实盘账户未开通交易/资产权限时 get_assets 返回 summary 全 0 且
    timestamp=None（prime_assets 的 segments 为空）——净值取不到。此时返回 (None, currency)，
    调用方（open_position_tiger 自动算仓位）应拒绝下单，禁止用 0 净值算仓位 B。
    paper 账户接入后能取到真实净值。
    """
    if config is None:
        config = load_config(account=account)
    tc = new_trade_client(config)
    try:
        if base_currency is not None:
            # 按币种口径取净值（2026-08-05 修：港股 HKD / 美股 USD，与标的计价一致）
            try:
                pa = tc.get_prime_assets(base_currency=base_currency)
                if pa and getattr(pa, "segments", None):
                    for seg in pa.segments.values():
                        nl = getattr(seg, "net_liquidation", None)
                        if nl is not None:
                            cur = getattr(seg, "currency", None) or base_currency
                            return float(nl), str(cur)
            except Exception as e:
                print(f"⚠️ get_prime_assets(base_currency={base_currency}) 失败（{e}），回退 get_assets",
                      file=sys.stderr)
            return None, base_currency

        assets = tc.get_assets()
        if not assets:
            return None, "HKD"
        summary = assets[0].summary
        na = getattr(summary, "net_liquidation", None)
        ts = getattr(summary, "timestamp", None)
        currency = getattr(summary, "currency", None) or "HKD"
        if na is None or (float(na) <= 0 and ts is None):
            print("⚠️ 老虎资产查询异常（net_liquidation=0 且无时间戳）——账户未开通交易/资产权限？",
                  file=sys.stderr)
            return None, currency
        return float(na), currency
    finally:
        pass  # SDK 客户端无显式 close


# ---------------------------------------------------------------------------
# 下单
# ---------------------------------------------------------------------------

def _make_order(tc, config, symbol, action, order_type, quantity,
                limit_price=None, aux_price=None, order_legs=None):
    """创建订单对象（create_order）并提交（place_order）。返回全局订单 id。

    action: 'BUY' / 'SELL'（老虎订单动作枚举是 BUY/SELL 全大写）。
    order_type: 老虎 OrderType 枚举的**字符串值**（'LMT' / 'MKT' / 'STP'）——Order 构造函数
      原样存 order_type、place_order 序列化订单时 JSON 化该字段，传枚举对象会崩
      （TypeError: Object of type OrderType is not JSON serializable，2026-08-03 paper 实测发现）。
    """
    from tigeropen.common.consts import SecurityType
    contract = tc.get_contract(to_tiger_symbol(symbol), sec_type=SecurityType.STK)
    if contract is None:
        raise RuntimeError(f"查不到老虎合约 {symbol}（代码格式须 5 位数字，如 02800）")
    order = tc.create_order(
        account=config.account,
        contract=contract,
        action=action,
        order_type=order_type,
        quantity=int(quantity),
        limit_price=limit_price,
        aux_price=aux_price,
        order_legs=order_legs,
        time_in_force="DAY",
    )
    if order is None:
        raise RuntimeError(f"创建订单对象失败 {symbol} {action} qty={quantity}")
    return tc.place_order(order)


def _is_ambiguous_timeout_error(err):
    """判定提交异常是否为「超时模糊失败」（请求可能已达券商，2026-08-16 立）。

    盲重试的克制条件：timeout / read timeout / connection reset 等——请求发出去了、
    响应没回来，订单可能已在券商侧受理。此类异常重试有真实重复下单风险（MKT 主单即时
    成交，第二笔不会被 cross-trading 挡住），调用方须谨慎（防抖检查后再重试或不重试）。
    其余异常（参数错误、权限拒绝、网络未建立即失败）通常确定未到达，可安全重试。
    """
    msg = str(err).lower()
    return any(k in msg for k in ("timeout", "timed out", "connection reset",
                                  "read error", "connection aborted"))


def submit_order_with_stop_tiger(config, symbol, side, quantity, submitted_price,
                                 stop_loss_price, profit_price=None,
                                 order_type="LMT", retries=3):
    """开仓：主单（LMT 限价）+ 附加腿一次提交——止损腿 OrderLeg('LOSS', stop_loss_price) +
    止盈腿 OrderLeg('PROFIT', profit_price)（2026-08-23 用户立双腿；profit_price=None 时退回单止损腿）。

    side: 'Buy'（做多开仓）/ 'Sell'（做空开仓）——注意转老虎 'BUY'/'SELL'。
    order_type: 'LMT'（限价主单，limit_price=submitted_price）。⚠️ 附加订单仅限价主单支持
    （SDK demo 注释明示），市价主单已禁用（2026-08-23「下单必须用限价单」）。
    双腿语义（SDK 源码 model.py _parse_leg_param + 官方帮助中心，2026-08-23 查证）：
    LOSS+PROFIT 两腿齐发时 attach_type='BRACKETS'（括号订单）——主单成交后系统自动监控，
    止损/止盈任一边触发成交、另一边自动作废；主单撤单则两腿同步取消。
    腿的方向与触发语义由券商按主单方向自动定（做多：跌到止损价触发卖、涨到止盈价触发卖；
    做空反之）；腿 TIF 默认 DAY（日内策略当日有效；跨日场景待实测）。
    返回全局订单 id。

    ⚠️ 重试不再盲重试 3 次（2026-08-16 修复）：超时类模糊失败（请求已达券商、响应超时）
    重试是真实的重复下单路径——现在此类异常不再自动重试、直接抛出，错误信息注明
    「订单可能已提交成功、须先查当日订单确认」；确定未到达的异常照旧重试。外层调用方
    （开仓降档循环）捕获后同样不降档续下（见 open_position_tiger.py 的 ambiguous 处理）。
    """
    from tigeropen.trade.domain.order import OrderLeg
    last_err = None
    legs_desc = "止损+止盈双腿" if profit_price is not None else "止损单腿"
    for attempt in range(retries):
        try:
            tc = new_trade_client(config)
            action = "BUY" if side == "Buy" else "SELL"
            legs = [OrderLeg("LOSS", stop_loss_price)]
            if profit_price is not None:
                legs.append(OrderLeg("PROFIT", profit_price))
            if order_type == "MKT":
                return _make_order(tc, config, symbol, action, "MKT", quantity, order_legs=legs)
            return _make_order(tc, config, symbol, action, "LMT", quantity,
                               limit_price=submitted_price, order_legs=legs)
        except Exception as e:
            last_err = e
            if _is_ambiguous_timeout_error(e):
                raise RuntimeError(
                    f"老虎开仓（{order_type}+{legs_desc}）提交超时（模糊失败，订单可能已在券商侧受理）"
                    f" {symbol} {side} qty={quantity} price={submitted_price} stop={stop_loss_price}"
                    f"{' profit=' + str(profit_price) if profit_price is not None else ''}: {e}"
                    f"——禁止盲目重试，须先查当日订单确认是否已成交，未确认前不得再下单") from e
            if attempt < retries - 1:
                time.sleep(0.5 * (attempt + 1))
                continue
    raise RuntimeError(
        f"老虎开仓（{order_type}+{legs_desc}）提交失败 {symbol} {side} qty={quantity} "
        f"price={submitted_price} stop={stop_loss_price}: {last_err}"
    )


def submit_market_order_tiger(config, symbol, side, quantity, retries=3):
    """港股市价单 MKT。side: 'Buy' / 'Sell'。返回全局订单 id。

    ⚠️ 2026-08-23 用户立「下单必须用限价单」后本函数不再是平仓默认路径（平仓改走
    submit_limit_order_tiger 对价限价单）；函数保留供应急人工显式调用，AI 例行下单不得使用。

    超时类模糊失败不自动重试（2026-08-16，同 submit_order_with_stop_tiger：订单可能已达
    券商、重试=真实重复下单路径），错误信息注明须先查订单确认。"""
    raise RuntimeError(
        "下单必须用限价单（2026-08-23 用户立）：市价单已禁用——平仓用 submit_limit_order_tiger"
        "（对价限价、取整 tick），确需市价单须经用户明确同意后临时改回")


def submit_limit_order_tiger(config, symbol, side, quantity, limit_price, retries=3):
    """港股限价单 LMT（平仓用，2026-08-23 用户立「下单必须用限价单」落地）。side: 'Buy' /
    'Sell'，limit_price 由调用方取整 tick 后传入（平多挂 bid 主动卖、平空挂 ask 主动买）。
    返回全局订单 id。

    与 MKT 的行为差异：限价单可能不成交（价格快速离开时）——调用方须有超时撤单 + 如实
    上报的处理，不得假设必成交。超时类模糊失败不自动重试（2026-08-16 同款：重试=真实
    重复下单路径，错误信息注明须先查订单确认）。"""
    last_err = None
    for attempt in range(retries):
        try:
            tc = new_trade_client(config)
            action = "BUY" if side == "Buy" else "SELL"
            return _make_order(tc, config, symbol, action, "LMT", quantity,
                               limit_price=limit_price)
        except Exception as e:
            last_err = e
            if _is_ambiguous_timeout_error(e):
                raise RuntimeError(
                    f"老虎平仓 LMT 提交超时（模糊失败，订单可能已在券商侧受理）"
                    f" {symbol} {side} {quantity} @ {limit_price}: {e}"
                    f"——禁止盲目重试，须先查当日订单确认") from e
            if attempt < retries - 1:
                time.sleep(0.5 * (attempt + 1))
                continue
    raise RuntimeError(f"老虎平仓 LMT 提交失败 {symbol} {side} {quantity} @ {limit_price}: {last_err}")


def _submit_market_order_tiger_impl(config, symbol, side, quantity, retries=3):
    """市价单底层实现（禁用期保留，见 submit_market_order_tiger 的禁用说明）。"""
    raise RuntimeError(
        "下单必须用限价单（2026-08-23 用户立）：市价单已禁用——确需市价单须经用户明确同意后临时改回")


def submit_stop_order_tiger(config, symbol, side, quantity, trigger_price, retries=3):
    """独立止损单 STP（移损用）。aux_price=触发价。

    side 由调用方定（做多止损 Sell / 做空止损 Buy）；触发方向由券商按 trigger_price
    相对现价自动判定（2026-08-01 实测）。触发后市价成交。
    返回全局订单 id。

    超时类模糊失败不自动重试（2026-08-16，同 submit_order_with_stop_tiger——重试可能
    产生重复止损单；移损 fallback 调用方已有分步报告，此处直接抛出让调用方如实归因）。"""
    last_err = None
    for attempt in range(retries):
        try:
            tc = new_trade_client(config)
            action = "BUY" if side == "Buy" else "SELL"
            return _make_order(tc, config, symbol, action, "STP", quantity,
                               aux_price=trigger_price)
        except Exception as e:
            last_err = e
            if _is_ambiguous_timeout_error(e):
                raise RuntimeError(
                    f"老虎独立止损 STP 提交超时（模糊失败，订单可能已在券商侧受理）"
                    f" {symbol} {side} qty={quantity} trigger={trigger_price}: {e}"
                    f"——禁止盲目重试，须先查当日订单确认") from e
            if attempt < retries - 1:
                time.sleep(0.5 * (attempt + 1))
                continue
    raise RuntimeError(
        f"老虎独立止损 STP 提交失败 {symbol} {side} qty={quantity} trigger={trigger_price}: {last_err}"
    )


# ---------------------------------------------------------------------------
# 成交回查 / 持仓 / 资产 / 订单 / 撤单
# ---------------------------------------------------------------------------

def check_order_filled_tiger(config, order_id, timeout=8, poll_interval=2):
    """轮询订单成交状态（get_orders）。返回 (filled, fill_price, status_str, reason)。

    老虎状态值（2026-08-02 源码确认）：Filled / PartiallyFilled / Cancelled /
    Inactive（已失效）/ Invalid（非法）等。

    ⚠️ status 是 OrderStatus 枚举（OrderStatus.FILLED），必须取 .value（'Filled'）再判断——
    直接 str(枚举) 得 'OrderStatus.FILLED'，'Filled' in 它恒 False → 已成交误判未成交、
    随后撤单撤已成交的单（持仓实际已建立、附加止损还挂着）。2026-08-03 paper 实测暴露
    （开仓主单实际 FILLED @486.2，脚本却输出「主单未成交、已撤」）。

    ⚠️ 部分成交（PartiallyFilled）不再当全额成交（2026-08-16 修复）：原实现 `"Filled" in
    status` 对 'PartiallyFilled' 也为 True——大额市价单部分成交时开仓侧把下单量当全成量
    上报、平仓侧误信已平干净，持仓认知与账户脱节。现严格匹配：仅 status == 'Filled' 视为
    全额成交；'PartiallyFilled' 继续轮询到超时，超时返回 filled=False + status 原样
    （'PartiallyFilled'），调用方据此复查实际成交量（Order.filled 字段，见
    get_order_filled_qty_tiger）。

    ⚠️ 轮询异常不再带崩主流程（2026-08-16 修复）：原实现轮询内 get_orders 网络异常直接抛出、
    整个脚本 traceback——订单实际已提交在场，AI 拿不到任何输出可能误判未下单而重复下单。
    现捕获轮询异常：轻微异常（网络抖动）继续等下一轮；超时前持续失败则返回
    (False, None, 'poll_error:<err>', '')——明确告知「订单已在场但状态查询失败」，
    调用方不得据此重复下单、应人工查订单。

    reason：订单对象 Order.reason 字段（SDK 注释「下单失败时返回失败原因的描述」）。
    2026-08-06 MINIMAX 58,400 被拒时脚本只输出 status、没输出 reason，导致「原因未明」；
    2026-08-11 实测查历史订单确认 reason 可取到具体文案（如 120 股那笔的
    'cross-trading with your pending sell order'——账户已有同标的未成交委托单、新单与它
    交叉成交被拒，2026-08-11 用户纠正与持仓止损单无关）。注意部分被拒订单 reason 只有通用文案
    （'The order cannot be canceled...'），拿不到具体原因时按通用处理。
    """
    tc = new_trade_client(config)
    deadline = time.time() + timeout
    last_status = ""
    last_reason = ""
    last_err = None
    while time.time() < deadline:
        try:
            orders = tc.get_orders() or []
        except Exception as e:
            last_err = e
            time.sleep(poll_interval)   # 网络抖动等瞬态异常：等下一轮，不崩主流程
            continue
        for o in orders:
            if str(getattr(o, "id", "")) != str(order_id) and \
               str(getattr(o, "order_id", "")) != str(order_id):
                continue
            status_obj = getattr(o, "status", "")
            status = status_obj.value if hasattr(status_obj, "value") else str(status_obj)
            last_status = status
            reason = getattr(o, "reason", None) or ""
            last_reason = reason
            avg = getattr(o, "avg_fill_price", None)
            if status == "Filled":   # 严格匹配；'PartiallyFilled' 不算全额成交（见 docstring）
                return True, (float(avg) if avg else None), status, reason
            if any(s in status for s in ("Cancelled", "Inactive", "Invalid",
                                         "PendingCancel")):
                return False, None, status, reason
            break  # 已定位订单但未终结（含 PartiallyFilled），继续等
        time.sleep(poll_interval)
    if last_status:
        return False, None, last_status, last_reason   # 超时；status 可能是 PartiallyFilled（部分成交未全成）
    return False, None, f"poll_error:{last_err}" if last_err else "timeout", ""


def get_order_filled_qty_tiger(config, order_id):
    """查指定订单的已成交数量（Order.filled 字段，2026-08-16 立）。

    部分成交（PartiallyFilled）场景的复查入口：check_order_filled_tiger 超时返回
    status='PartiallyFilled' 时，调用方用本函数读实际成交量（filled / quantity），
    按实际成交量上报、不得把下单量当全成量。查不到订单返回 None。
    """
    tc = new_trade_client(config)
    for o in (tc.get_orders() or []):
        if str(getattr(o, "id", "")) != str(order_id) and \
           str(getattr(o, "order_id", "")) != str(order_id):
            continue
        try:
            return int(getattr(o, "filled", 0) or 0)
        except (TypeError, ValueError):
            return None
    return None


def has_active_open_order_tiger(config, symbol, side=None):
    """重复下单防抖检查（2026-08-16 立）：查该标的当日是否已有**开仓方向**的活动委托单。

    背景：提交异常盲重试 + 降档循环叠加时，超时模糊失败（请求已达券商、响应超时）会重复
    下单——MKT 主单即时成交，第二笔不会被 cross-trading 挡住，真实双倍持仓路径。开仓脚本
    在降档重试前调用本函数：同标的已有活动 BUY（开多）/ SELL（开空）方向委托单时拒绝继续。

    只查开仓方向（side 给定查该 side；None 则 Buy/Sell 都算）。止损单（STP 类型）不算——
    它们是持仓保护、不是开仓单。market=None 不过滤市场（港美共用，symbol 已带市场前缀语义，
    老虎 symbol 天然区分 HK 5 位数字 / US 裸代码）。返回 (has, order_ids)。
    """
    tc = new_trade_client(config)
    target = to_tiger_symbol(symbol)
    active_ids = []
    for o in (tc.get_orders() or []):
        contract = getattr(o, "contract", None)
        order_sym = getattr(contract, "symbol", None) if contract else None
        if order_sym is None or str(order_sym) != target:
            continue
        raw_status = getattr(o, "status", "")
        status = raw_status.value if hasattr(raw_status, "value") else str(raw_status)
        if any(s in status for s in ("Filled", "Cancelled", "Inactive", "Invalid",
                                     "Expired", "PendingCancel")):
            continue
        raw_otype = getattr(o, "order_type", "")
        otype = (raw_otype.value if hasattr(raw_otype, "value") else str(raw_otype) or "")
        legs = getattr(o, "order_legs", None) or []
        is_stop = str(otype).upper() in ("STP", "STOP", "TRAIL") or any(
            str(getattr(leg, "leg_type", "")).upper() == "LOSS" for leg in legs)
        if is_stop:
            continue   # 止损单不算开仓委托
        action = str(getattr(o, "action", "")).upper()
        if side is not None:
            want = "BUY" if side == "Buy" else "SELL"
            if action != want:
                continue
        active_ids.append(getattr(o, "id", None) or getattr(o, "order_id", None))
    return (len(active_ids) > 0), active_ids


def get_open_position_tiger(config, symbol=None):
    """查老虎港股持仓。返回 {'symbol','symbol_name','side','quantity','cost_price'} 或 None。

    side 判定：Position 无方向字段（2026-08-02 源码确认），quantity 正=多、负=空
    （港股融券做空）。本项目做空走反向 ETF、账户层均为多头，默认 long。

    只收港股持仓（2026-08-16 修复）：原实现不过滤市场，同账户的美股持仓也被收进
    collected——港股一键平仓（symbol=None）在恰有一个美股持仓、无港股持仓时会拿到
    US.xxx，下游 to_tiger_symbol 抛 ValueError（traceback 而非 JSON 错误）。老虎港股
    symbol 是 5 位数字、美股是裸代码，按 isdigit() 过滤（与美股版 get_open_position_us
    的排除逻辑对称——那里排除数字、这里只留数字）。
    """
    tc = new_trade_client(config)
    try:
        positions = tc.get_positions() or []
        collected = []
        for p in positions:
            qty_f = float(getattr(p, "quantity", 0) or 0)
            if qty_f == 0:
                continue
            sym_raw = str(getattr(getattr(p, "contract", None), "symbol", ""))
            if not sym_raw.isdigit():   # 港股 = 5 位数字；美股裸代码（MU）排除
                continue
            side = "short" if qty_f < 0 else "long"
            collected.append((side, p, abs(qty_f)))
        if not collected:
            return None
        if symbol is not None:
            target = to_tiger_symbol(symbol)
            matches = [(s, p, q) for s, p, q in collected
                       if str(getattr(p.contract, "symbol", "")) == target]
            if not matches:
                return None
            s, p, qty = matches[0]  # 2026-08-16 修复：取目标持仓自己的 side（原写法丢弃 s、
            # 返回值误用收集循环残留的 side——同账户多空混持时查后一个标的会拿前一个的
            # side，一键平仓据此把空仓再 Sell 加倍 / 多仓再 Buy 加倍）
        else:
            if len(collected) != 1:
                return None
            s, p, qty = collected[0]
        cost = getattr(p, "average_cost", None)
        return {
            "symbol": to_futu_symbol_tiger(getattr(p.contract, "symbol", None)),
            "symbol_name": getattr(p.contract, "name", None),
            "side": s,  # 目标持仓自己的 side（多空混持时不再张冠李戴）
            "quantity": int(qty),
            "cost_price": float(cost) if cost else None,
        }
    finally:
        pass


def cancel_order_tiger(config, order_id):
    """撤销订单（cancel_order(id=全局 id)）。"""
    tc = new_trade_client(config)
    try:
        tc.cancel_order(id=order_id)
    finally:
        pass


def cancel_all_stop_orders_tiger(config, symbol, exclude_order_id=None):
    """撤销指定港股标的的全部未触发止损单（平仓后防反向开仓；移损 fallback 撤旧用）。

    止损单口径（2026-08-16 对齐美股版 cancel_all_stop_orders_us）：STP / STOP / TRAIL /
    LOSS 附加腿四类都撤。TRAIL（跟踪止损）源自 2026-08-05 中芯残留事故教训（美股版当时
    补了 TRAIL、港股版漏了）——用户在券商 App 手动挂的港股 TRAIL 单也是止损保护，平仓
    须一并撤，否则残留单日后触发反向开仓。
    状态已 Filled / Cancelled / Inactive / Invalid / PendingCancel 的跳过。
    返回 (n, ids)。
    """
    tc = new_trade_client(config)
    try:
        target = to_tiger_symbol(symbol)
        orders = tc.get_orders() or []
        cancelled = []
        for order in orders:
            contract = getattr(order, "contract", None)
            order_sym = getattr(contract, "symbol", None) if contract else None
            if order_sym is None or str(order_sym) != target:
                continue
            oid = getattr(order, "id", None) or getattr(order, "order_id", None)
            if oid is None:
                continue
            if exclude_order_id is not None and str(oid) == str(exclude_order_id):
                continue  # 跳过刚下的新止损（移损 fallback「先下新再撤旧」保新止损）
            # 2026-08-16 修复：status / order_type 运行时是老虎 SDK 枚举（普通 Enum，
            # str() 得 'OrderStatus.FILLED' / 'OrderType.STP' 形态、裸字符串子串比较全部
            # 失效——独立 STP 单撤不掉、终结状态过滤失效）。统一 .value 优先、str 兜底，
            # 枚举 / 纯字符串两种形态都正确（与 close/move 脚本 _is_stop_order 口径一致）。
            raw_status = getattr(order, "status", "")
            status = raw_status.value if hasattr(raw_status, "value") else str(raw_status)
            if any(s in status for s in ("Filled", "Cancelled", "Inactive", "Invalid",
                                         "PendingCancel")):
                continue
            raw_otype = getattr(order, "order_type", "")
            otype = (raw_otype.value if hasattr(raw_otype, "value") else str(raw_otype) or "")
            legs = getattr(order, "order_legs", None) or []
            # 2026-08-16 对齐美股口径：含 TRAIL（跟踪止损）——2026-08-05 中芯残留事故
            # 修复只落在美股版（cancel_all_stop_orders_us），港股版漏 TRAIL：用户在 App 手动
            # 挂的港股 TRAIL 单撤不掉 → 平仓后残留、下次触发反向开仓。港美统一四类：
            # STP / STOP / TRAIL / LOSS 附加腿。
            is_stop = (str(otype).upper() in ("STP", "STOP", "TRAIL")
                       or any(str(getattr(leg, "leg_type", "")).upper() == "LOSS" for leg in legs))
            if not is_stop:
                continue
            try:
                tc.cancel_order(id=oid)
                cancelled.append(oid)
            except Exception:
                pass  # 单个撤销失败不影响其他
        return len(cancelled), cancelled
    finally:
        pass


def get_today_orders_tiger(config):
    """查老虎当日订单列表（get_orders），供 monitor_segment 每轮采样提取最新止损价。

    用户可能在券商 App 里手动新增止损单，最新止损价不能凭记忆，须每轮采样现查。
    返回订单对象列表（含 order_type=STP 的止损单，触发价在 aux_price）或 []。
    老虎订单对象字段（id / status / contract.symbol / order_type / aux_price /
    order_legs）见 cancel_all_stop_orders_tiger。
    """
    tc = new_trade_client(config)
    try:
        return tc.get_orders() or []
    except Exception as e:
        print(f"⚠️ 老虎当日订单查询失败: {e}", file=sys.stderr)
        return []


# ---------------------------------------------------------------------------
# 持仓期间极值（平仓过程指标素材，2026-08-05 立）
# ---------------------------------------------------------------------------

def calc_position_extremes_tiger(symbol, mode="signal", project_root=None):
    """从盯盘 log 取该标的当日采样极值（持仓期间 high/low 的近似），供平仓时原生记录
    mfe_R / mae_R（review-and-evaluation.md「⚠️ 数据约束」方案 b 落地：复盘直接读、
    不必每次回拉历史 K）。

    读 `tmp/monitor_log_{SYM}_{YYYYMMDD}_{mode}.csv`（log 由 monitor_segment 按市场交易日
    命名——港股北京日期、美股美东交易日，signal/auto 两会话分文件；SYM = 富途格式转下划线，
    如 HK.00981 → HK_00981）。多个日期文件只取最新日期那个（当日）。

    ⚠️ 近似：log 的 high/low 是行情快照的当日 high/low 列、且含开仓前时段（盘前采样点在
    开盘价附近；日内策略当天开当天平，当日 log 近似持仓期间，误差可控）。无 log（未盯盘 /
    停盯后平仓）返回 None，调用方按缺失处理、复盘跳过过程指标。
    返回 (raw_high, raw_low) 或 None。
    """
    import csv
    import glob
    if project_root is None:
        project_root = Path(__file__).resolve().parent.parent.parent.parent.parent
    log_dir = Path(project_root) / "tmp"
    sym_tag = str(symbol).replace(".", "_")  # HK.00981 → HK_00981（monitor_segment 的 log 命名）
    files = sorted(glob.glob(str(log_dir / f"monitor_log_{sym_tag}_*_{mode}.csv")))
    if not files:
        return None
    highs, lows = [], []
    with open(files[-1]) as fh:  # 只取最新日期文件（当日；跨日残留旧文件排除）
        for r in csv.DictReader(fh):
            if r.get("symbol") != symbol:
                continue
            try:
                if r.get("high") not in (None, ""):
                    highs.append(float(r["high"]))
                if r.get("low") not in (None, ""):
                    lows.append(float(r["low"]))
            except ValueError:
                continue
    if not highs or not lows:
        return None
    return max(highs), min(lows)


# ---------------------------------------------------------------------------
# 价格范围 / 仓位计算（纯函数）
# ---------------------------------------------------------------------------

# 单边费率（2026-08-12 改真实费率，复用 fee_schedule；2026-08-17 平台费改固定模式 + 美股改
# 按股结构）：港股按额计（佣金 max(15,×0.029%) + 印花税(个股0.1%/ETF免) + 征费 + 平台费 15/笔）、
# 美股按股计（佣金 0.0039/股 + 平台费 0.004/股 max(1, 0.5%×额) + 代收近似 0.00396/股）。
# 盯盘前瞻与复盘 review.py 同口径（2026-08-12 用户立：完全精算）。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fee_schedule as FS   # 共享真实费率模块

# 旧百分比费率（仅 _net_odds 缺 fee_ctx 时向后兼容用；正常路径都走真实费率 fee_ctx）
_LEGACY_FEE = {'HK': 0.0018, 'US': 0.0003}


def _market_of(symbol):
    s = (symbol or '').upper()
    if s.startswith('HK.'): return 'HK'
    if s.startswith('US.'): return 'US'
    return None   # 未知前缀


# 港股 ETF 白名单（免印花税；与复盘 CSV type 列、classify_hk_security.py 一致）。
# 港股代码无规律，ETF 靠白名单识别——白名单外默认 stock（保守收印花税）。
# 2026-08-16 修复：原白名单仅 {'HK.07709'} 一个成员、注释却声称「与 classify 一致」
# （classify 实际 30 个官方 ETF）——交易 02800 盈富等主流 ETF 时判 stock、赔率计算
# 多收 0.1% 印花税（保守方向、错杀交易）。现直接 import classify 的 HKEX_ETF_WHITELIST
# 作唯一权威源（单一来源，classify 更新此处自动跟随；HK. 前缀在此转换）。
try:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # skill 根（classify 所在）
    import classify_hk_security as _classify
    _HK_ETF_WHITELIST = {f"HK.{code}" for code in _classify.HKEX_ETF_WHITELIST}
except Exception:   # classify 导入失败（极端）回退最小集
    _HK_ETF_WHITELIST = {'HK.07709'}
# 美股杠杆 / 反向 ETF（无印花税；美股个股 ETF 费用结构同、但 type 仍标注供一致性）
_US_ETF_SET = {'US.SOXL', 'US.SOXS'}


def _sec_type_of(symbol):
    """标的类型（stock / etf），用于印花税判定（港股个股收、ETF 免）。
    港股 ETF 靠白名单识别（代码无规律）；白名单外默认 stock（保守收印花税）。
    开仓前护栏已跑 classify_hk_security.py 确认类型，此处白名单是脚本内快速判定。"""
    s = (symbol or '').upper()
    if s in _HK_ETF_WHITELIST or s in _US_ETF_SET:
        return 'etf'
    return 'stock'


def build_fee_ctx(symbol, shares, config, order_idx_open=None, order_idx_close=None):
    """构造 _net_odds 用的真实费率上下文（2026-08-12 立；2026-08-17 随固定平台费简化）。

    平台费改固定模式后不再查当月累计订单数——费项只由「市场 + 标的类型 + 成交额/股数」决定，
    本函数退化为纯属性打包（market / sec_type / shares），config 参数保留只为兼容调用方签名。
    返回 {shares, sec_type, market}。
    """
    return {'shares': shares, 'sec_type': _sec_type_of(symbol), 'market': _market_of(symbol)}


def attach_net_pnl_app(result, config, tiger_sym, direction, quantity, exit_price,
                       close_order_id=None, entry_price=None):
    """平仓成交后补记 App 口径净利 net_pnl_app（2026-08-19 用户立规的工具强制落地）。

    背景：当日小米平仓 AI 手算净利 506 vs App 实测 473.93——AI 拆算费率再细也会漏费项 /
    漏印花取整 / 口径差，用户裁定「action 记录盈亏直接从 App 取」。本函数把该口径做进
    平仓脚本输出：AI 转录动作记录时直接抄 net_pnl_app 字段、禁止手算（auto-mode.md
    「费用与成交价的唯一权威口径」节）。

    取数优先级（2026-08-19 实测校验：01810 第一笔 close 成交单 realized_pnl=473.93 与
    App 已实现盈亏完全一致；第二笔毛盈亏 900 − 双边 commission 425.13 = 474.87 与
    realized_pnl 474.87 一致——两条路径互验通过）：
      1. close 成交单 realized_pnl——老虎按自身成本价口径实算（与 App「已实现盈亏」同源，
         连 avg_fill_price 的小数精度都由券商侧自带，最可信）；
      2. 毛盈亏 − 双边 commission（开仓成交单 + 平仓成交单的 SDK commission 字段
         = 该笔实收总费用）；
      3. fee_schedule 复算兜底（当日成交单费用字段未结算 / 查询失败时），net_pnl_source
         标 estimate——转录时须注明「复算口径、待账单核对」。

    参数：
      tiger_sym：老虎格式代码（HK '01810' / US 'MU'，用于成交单过滤）。
      direction：'long' / 'short'（优先级 2/3 算毛盈亏用）。
      quantity：平仓股数（毛盈亏用）。
      exit_price：平仓成交价（脚本已确认的 fill_price）。
      close_order_id：平仓单全局 id；None = 按「该标的最新成交单」定位（fallback 路径
        止损单已实际触发但轮询误判时拿不到 id，用最新成交兜底）。
      entry_price：开仓成本（持仓 cost_price）；优先级 1 不需要它。

    任何查询失败不抛异常（不阻断平仓结果输出——net_pnl 是附加字段、平仓本身已成交），
    只在 result 里记 net_pnl_warning 提示转录时用 App 实测回填。
    """
    import datetime as _dt
    try:
        tc = new_trade_client(config)
        today = _dt.date.today()
        # ⚠️ get_filled_orders 的 start_time 必填（实测缺省报 code=1010 field 'start_date'
        # cannot be empty）；end_time 传次日、只查当日成交。
        fills = tc.get_filled_orders(
            start_time=today.strftime('%Y-%m-%d'),
            end_time=(today + _dt.timedelta(days=1)).strftime('%Y-%m-%d')) or []
        sym_fills = []
        for o in fills:
            contract = getattr(o, 'contract', None)
            if str(getattr(contract, 'symbol', '') if contract else '') != str(tiger_sym):
                continue
            sym_fills.append(o)

        def _f(v):
            try:
                return float(v) if v is not None and str(v) not in ('nan', 'None') else None
            except (TypeError, ValueError):
                return None

        def _ts(o):
            try:
                return float(getattr(o, 'trade_time', 0) or 0)
            except (TypeError, ValueError):
                return 0.0

        close_fill = None
        if close_order_id is not None:
            for o in sym_fills:
                if str(getattr(o, 'id', '')) == str(close_order_id):
                    close_fill = o
                    break
        else:
            cand = [o for o in sym_fills if _ts(o) > 0]
            if cand:
                close_fill = max(cand, key=_ts)
        realized = comm_open = comm_close = None
        if close_fill is not None:
            realized = _f(getattr(close_fill, 'realized_pnl', None))
            comm_close = _f(getattr(close_fill, 'commission', None))
            close_action = str(getattr(close_fill, 'action', '')).upper()
            # 开仓成交单 = 同标的、方向与平仓相反、时间不晚于平仓的最新一笔
            opens = [o for o in sym_fills
                     if o is not close_fill
                     and str(getattr(o, 'action', '')).upper() != close_action
                     and _ts(o) <= _ts(close_fill)]
            if opens:
                comm_open = _f(getattr(max(opens, key=_ts), 'commission', None))
        if realized is not None:
            result['net_pnl_app'] = round(realized, 2)
            result['net_pnl_source'] = 'tiger_realized_pnl（App 实测口径，AI 转录直接抄）'
        elif (comm_close is not None and comm_open is not None
              and entry_price and exit_price):
            gross = ((exit_price - entry_price) if direction == 'long'
                     else (entry_price - exit_price)) * quantity
            result['net_pnl_app'] = round(gross - comm_open - comm_close, 2)
            result['net_pnl_source'] = 'tiger_commission（毛盈亏 − 双边实收费）'
        elif entry_price and exit_price:
            # 复算兜底：当日成交单费用字段未结算（实测成交后短时间内 commission 可能为
            # None，需等结算）——用 fee_schedule 估、显式标 estimate 提醒转录时核对账单。
            is_hk = str(tiger_sym).isdigit()
            futu = f'HK.{str(tiger_sym).zfill(5)}' if is_hk else f'US.{tiger_sym}'
            market = 'HK' if is_hk else 'US'
            sec_type = _sec_type_of(futu)
            fee_open = FS.fee_per_side(market, sec_type, entry_price * quantity, shares=quantity)
            fee_close = FS.fee_per_side(market, sec_type, exit_price * quantity, shares=quantity)
            gross = ((exit_price - entry_price) if direction == 'long'
                     else (entry_price - exit_price)) * quantity
            result['net_pnl_app'] = round(gross - fee_open - fee_close, 2)
            result['net_pnl_source'] = ('estimate（fee_schedule 复算——commission 未结算/查询失败，'
                                        '转录须注明「复算口径、待账单核对」）')
        else:
            result['net_pnl_warning'] = ('net_pnl_app 未产出（成交单费用未结算且缺 entry_price，'
                                         '无法复算）——转录时用 App 实测回填')
        if comm_open is not None and comm_close is not None:
            result['fees_app'] = {'open': round(comm_open, 2), 'close': round(comm_close, 2),
                                  'total': round(comm_open + comm_close, 2)}
    except Exception as e:
        result['net_pnl_warning'] = f'net_pnl_app 查询失败（不阻断平仓，转录时用 App 实测）: {e}'


def get_buying_power_tiger(config, symbol, ref_price, tc=None, to_hkd=None):
    """按单标的保证金率算可买股数上限（2026-08-16 立，2026-08-06 00100 被拒根因闭环；
    2026-08-18 修港股币种偏差）。

    背景：2026-08-06 MINIMAX 58,400 股大单被拒，根因是购买力约束（buying_power ÷ 该标的
    保证金率），原开仓脚本不预算可买上限、只靠券商拒单后被动降档。本函数在**下单前**算出
    上限，让 open_position 主动降档（少踩拒单、少烧降档轮次）。

    口径（2026-08-12 实测 + 2026-08-16 复测确认）：
    - buying_power 取 get_assets().summary.buying_power（USD 计价）；
    - 保证金率取 get_contract(symbol).long_initial_margin（实测 00100 = 0.75）；
    - 可买市值上限 = buying_power × long_initial_margin；
      可买股数上限 = 市值上限 ÷ ref_price，按 lot 向下取整由调用方处理（本函数返回原始股数）。

    ⚠️ 币种（2026-08-18 修）：buying_power 为账户 USD 口径，港股标的价格为 HKD——直接相除
    有 ~7.8x 汇率偏差。原注释称「保守方向安全」，但实盘小账户（bp 5.9 万 USD ÷ 441.8 HKD 价
    → 40 股 < 1 手 100）会直接把降档结果压成 0、误拒本可开的仓——保守过头也是错。
    修法：to_hkd 汇率由调用方传入（取自 get_prime_assets 两币种净值之比，即老虎自身
    forex_rate），本函数把 bp 折成 HKD 再算；查不到汇率时按 7.80 保守常量兜底。

    返回 (max_shares, bp, margin_rate) 或 (None, None, None)（查询失败）。
    """
    try:
        if tc is None:
            tc = new_trade_client(config)
        assets = tc.get_assets()
        if not assets:
            return None, None, None
        s = assets[0].summary
        bp = getattr(s, "buying_power", None)
        if not bp or float(bp) <= 0:
            return None, None, None
        c = tc.get_contract(to_tiger_symbol(symbol))
        margin_rate = getattr(c, "long_initial_margin", None)
        if not margin_rate or float(margin_rate) <= 0:
            margin_rate = 1.0   # 查不到保证金率按 1.0 全额（最保守）
        if to_hkd is None:
            to_hkd = 7.80      # 无汇率来源时的保守兜底（低于实测 ~7.84，方向保守）
        bp_hkd = float(bp) * float(to_hkd)
        notional_cap = bp_hkd * float(margin_rate)
        if not ref_price or ref_price <= 0:
            return None, None, None
        return int(notional_cap / float(ref_price)), bp_hkd, float(margin_rate)
    except Exception as e:
        print(f"⚠️ 购买力查询失败 {symbol}: {e}", file=sys.stderr)
        return None, None, None


def get_month_order_count_tiger(config, market=None):
    """【已废弃，2026-08-17 平台费改固定模式】原用途：查当月已成交订单数给阶梯平台费定档。
    固定模式费率与订单数无关，本函数不再被任何调用方使用；保留仅为诊断查询（数当月成交笔数），
    不参与费率计算。
    """
    import datetime as _dt
    from tigeropen.common.consts import OrderStatus
    tc = new_trade_client(config)
    today = _dt.date.today()
    start = _dt.date(today.year, today.month, 1)
    end = _dt.date(today.year + (1 if today.month == 12 else 0),
                   1 if today.month == 12 else today.month + 1, 1)
    try:
        orders = tc.get_orders(start_time=start.strftime("%Y-%m-%d"),
                               end_time=end.strftime("%Y-%m-%d"),
                               states=[OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED],
                               limit=1000) or []
        if market:
            orders = [o for o in orders
                      if str(getattr(getattr(o, 'contract', None), 'market', '')).upper() == market.upper()]
        return len(orders)
    except Exception as e:
        print(f"⚠️ 老虎本月订单查询失败: {e}", file=sys.stderr)
        return None


def net_max_loss(market, sec_type, shares, entry, stop):
    """净 max_loss（2026-08-28 用户立：费计入分母，止损离场恰好 −1R）。

    = shares × |entry − stop| + 开仓边费(entry×shares) + 止损价平仓边费(stop×shares)。
    entry 取哪口径由调用方决定：开仓前 = 初始预期口径（参考价/现价），成交后 = 修正口径
    （实际成交价）。设计动机：止损价精确成交时净亏损 = 本值 → R 恰好 −1.000，最大亏损
    不再深于 −1R（仅剩滑点一层——止损 STP 市价触发单成交价劣于触发价时的真实滑点）。
    """
    fee_open = FS.fee_per_side(market, sec_type, entry * shares, shares=shares)
    fee_stop = FS.fee_per_side(market, sec_type, stop * shares, shares=shares)
    return shares * abs(entry - stop) + fee_open + fee_stop


def _net_odds(direction, entry, target, stop, fee_per_side=None, fee_ctx=None):
    """净前瞻赔率（与复盘 R = P_net / 净M 同口径，2026-08-28 分母改净口径）。

    分子 = 到止盈的净盈利（毛止盈距 × shares − 开仓边费(entry) − 止盈边费(target)）；
    分母 = 净 max_loss = 止损距 × shares + 开仓边费(entry) + 止损价平仓边费(stop)。
    前瞻假设到止盈 target 出场，故分子平仓费按 target 算；分母是最大亏损场景、平仓费按
    止损价算（两个费不同场景、各自按各自价格算——两边费分开算再加总，用户 2026-08-28 立）。
    旧口径「分母毛止损距、止损 R 略低于 −1」废止：新分母含费，止损价精确成交时 R 恰好 −1。

    费率两种口径（2026-08-12 改真实费率；2026-08-17 平台费改固定模式、美股改按股结构）：
    - fee_ctx 给定（正常路径）：真实费率。fee_ctx = {shares, sec_type, market}。
      开仓边费按 entry×shares、止盈/止损边费按 target/stop×shares，各自用 FS.fee_per_side
      ——港股按额计（佣金 max(15,×0.029%) + 印花税 + 征费 + 固定平台费 15/笔）、美股按股计
      （佣金 0.0039/股 + 平台费 0.004/股 max(1, 0.5%×额) + 代收近似 0.00396/股）。
    - fee_ctx 缺省（向后兼容）：shares 未知（quantity=0 自动算仓位前的价格范围检查），
      无法算绝对费额——退回旧百分比口径估分子费、分母保持毛止损距（费用占比小、偏差有限，
      算出仓位后由调用方按真实费率复核）。

    做多 stop_dist = entry − stop；做空 stop_dist = stop − entry；≤0 raise ValueError
    （方向错或贴止损——2026-08-16 修复：原返回 inf，价格范围闸门照常通过、赔率以 inf 这一
    最诱人形态呈现而非报错，带着开盘即触发的止损腿放行下单）。
    """
    if direction == 'long':
        gross_gain = target - entry
        stop_dist = entry - stop
    else:
        gross_gain = entry - target
        stop_dist = stop - entry
    if stop_dist <= 1e-12:
        raise ValueError(
            f"止损距 ≤0（direction={direction}, entry={entry}, stop={stop}）：做多要求 stop < entry、"
            f"做空要求 stop > entry——止损价在入场价错误一侧或与之重合，禁止计算赔率（原实现返回 inf 会误导放行）")
    if fee_ctx:   # 真实费率 + 净分母口径（2026-08-28 分母改净）
        shares = fee_ctx['shares']
        sec_type = fee_ctx['sec_type']
        market = fee_ctx['market']
        fee_open = FS.fee_per_side(market, sec_type, entry * shares, shares=shares)
        fee_target = FS.fee_per_side(market, sec_type, target * shares, shares=shares)
        fee_stop = FS.fee_per_side(market, sec_type, stop * shares, shares=shares)
        num = gross_gain * shares - fee_open - fee_target
        den = stop_dist * shares + fee_open + fee_stop
        return num / den
    else:         # 向后兼容：shares 未知（自动算仓位前）——旧百分比口径估分子、分母毛值
        fps = fee_per_side if fee_per_side is not None else 0.0
        fee_per_share = fps * (entry + target)
        return (gross_gain - fee_per_share) / stop_dist


def calc_entry_range(direction, entry_ref, stop_loss, target, symbol=None, fee_ctx=None):
    """开仓价格范围（经验参数、与毛/净赔率无关）：做多 [ref - R0*0.8, ref + ref*3/8]；做空 [ref - ref*3/8, ref + R0*0.8]。
    价格范围用毛 R0 算、不随净口径变；odds_at_ref 为净口径（扣双边费）。
    fee_ctx（真实费率）= {shares, sec_type, market}，
    给则用真实费率、不给则用旧百分比口径（_LEGACY_FEE 按 symbol 市场前缀）。"""
    R0 = abs(entry_ref - stop_loss)
    if R0 < 1e-9:
        raise ValueError("止损价与参考价相同，R0=0，无法计算价格范围")
    if direction == "long":
        range_low = entry_ref - R0 * 0.8
        range_high = entry_ref + entry_ref * 3.0 / 8.0
    else:
        range_low = entry_ref - entry_ref * 3.0 / 8.0
        range_high = entry_ref + R0 * 0.8
    if not fee_ctx:
        fee_ctx = None   # 退回旧百分比口径
    odds_at_ref = _net_odds(direction, entry_ref, target, stop_loss, fee_ctx=fee_ctx) \
        if fee_ctx else _net_odds(direction, entry_ref, target, stop_loss,
                                  fee_per_side=_LEGACY_FEE.get(_market_of(symbol), 0.0))
    return range_low, range_high, odds_at_ref


def check_price_in_range(direction, current_price, entry_ref, stop_loss, target, symbol=None, fee_ctx=None):
    """检查当前价是否在可接受开仓范围内。返回 (in_range, low, high, odds_ref, odds_current)。
    odds_ref / odds_current 均为净口径（扣双边费）。
    fee_ctx（真实费率）= {shares, sec_type, market}。
    2026-08-16：odds_at_current 以现价为 entry，现价漂过止损一侧时止损距 ≤0——此时
    _net_odds raise ValueError，捕获后 odds_at_current 记 -inf 且 in_range 强制 False
    （现价已在止损错误一侧 = 立即触发止损、绝不可开仓）。"""
    range_low, range_high, odds_at_ref = calc_entry_range(direction, entry_ref, stop_loss, target, symbol, fee_ctx)
    in_range = range_low <= current_price <= range_high
    try:
        if fee_ctx:
            odds_at_current = _net_odds(direction, current_price, target, stop_loss, fee_ctx=fee_ctx)
        else:
            odds_at_current = _net_odds(direction, current_price, target, stop_loss,
                                        fee_per_side=_LEGACY_FEE.get(_market_of(symbol), 0.0))
    except ValueError:
        odds_at_current = float('-inf')
        in_range = False
    return in_range, range_low, range_high, odds_at_ref, odds_at_current


def calc_position_size(equity, risk_fraction, f_max, stop_distance, lot_size,
                       entry_price=None, max_leverage=None, sec_type=None, market=None,
                       stop_price=None):
    """按 B = risk_fraction*equity、净 max_loss 上限 f_max*equity 选最接近 B 的 lot 离散仓位。

    2026-08-28 分母改净口径（用户立「max_loss 加止损价成交的手续费」）：max_loss = 股数×止损距
    + 开仓边费(entry_price×股数) + 止损价平仓边费(stop_price×股数)——选档目标与 f_max 上限都用
    净值。sec_type / market / stop_price / entry_price 齐备时按净口径；缺任一则退回毛口径
    （向后兼容旧调用，输出 net_max_loss=False 标记提醒调用方）。

    2026-08-08 新增市值杠杆上限约束：开仓市值（= 数量 × 开仓价）不得超过 equity × max_leverage
    （默认 10 倍，取 config.risk.max_leverage；权益 10 万 → 最高开 100 万市值）。与 f_max 是两套
    独立约束——f_max 限 max_loss（风险敞口）、max_leverage 限开仓市值（名义敞口），候选档须同时
    满足两者。

    max_leverage=None 时回退读 skill config.json 的 risk.max_leverage（默认 10）；entry_price 传
    参考价/开仓价（市值估算基准 + 净口径开仓边费基准）。

    双约束上界：max_loss 上界 = equity×f_max ÷ 止损距；市值上界 = equity×max_leverage ÷ 开仓价
    （有 entry_price 时）。取两者较小者向下取整到整手 = ub_lot；目标档 center = min(按 B 算的
    base 档, ub_lot)——cap 压下来则退到 ub_lot（市值/风险上限内的最大档），再在 center 附近
    ±2 档里选实际 max_loss 最接近 B 的档（剔除超 cap 的档）。

    返回 (quantity, max_loss, B, net_max_loss_flag)。"""
    import json
    from pathlib import Path
    if max_leverage is None:
        try:
            _cfg_path = Path(__file__).resolve().parent.parent / "config.json"
            with open(_cfg_path) as _f:
                max_leverage = float(json.load(_f).get("risk", {}).get("max_leverage", 10))
        except Exception:
            max_leverage = 10.0
    net_ok = all(v is not None for v in (sec_type, market, stop_price, entry_price))

    def _ml(s):
        """候选档 s 的 max_loss（净口径齐备则净、否则毛）。"""
        if net_ok:
            return net_max_loss(market, sec_type, s, entry_price, stop_price)
        return s * stop_distance

    B = equity * risk_fraction
    max_loss_cap = equity * f_max
    notional_cap = equity * max_leverage if entry_price else None
    raw = B / stop_distance if stop_distance > 0 else 0
    base = int(raw // lot_size) * lot_size
    # 双约束上界（整手）：净口径时按「净 max_loss 上界对应的股数」反解——每档净费随股数非线性
    # （最低收费 / 印花税取整），用毛近似定 ub 再向下多退一档余量，最终逐档精算判 cap。
    ub = max_loss_cap / stop_distance if stop_distance > 0 else float("inf")
    if notional_cap is not None:
        ub = min(ub, notional_cap / entry_price)
    ub_lot = int(ub // lot_size) * lot_size
    if net_ok and ub_lot > 0:
        # 净口径 cap 校正：ub 档若净超 cap，逐档下退到首个净 ≤ cap 的档（费随股数单调不减，
        # 向下退有限步内收敛）
        while ub_lot > 0 and _ml(ub_lot) > max_loss_cap:
            ub_lot -= lot_size
    if ub_lot <= 0:
        return 0, 0, B, net_ok
    center = min(base, ub_lot)
    candidates = []
    for mult in [-2, -1, 0, 1, 2]:
        s = center + mult * lot_size
        if s <= 0:
            continue
        ml = _ml(s)
        if ml > max_loss_cap:
            continue
        if notional_cap is not None and s * entry_price > notional_cap:
            continue
        candidates.append((s, ml))
    if not candidates:
        return 0, 0, B, net_ok
    best = min(candidates, key=lambda x: abs(x[1] - B))
    return best[0], best[1], B, net_ok


def minutes_to_session_end(market, now=None):
    """距该市场「停止盯盘时点」还有多少分钟（2026-08-18 立，下单时间闸用）。

    停盯边界（SKILL.md「时点平仓与停止盯盘约束」）：
    - HK：早市 12:00 午休 / 午市 16:00 收盘，取当前所在半场的结束时刻；半场之间
      （午休 12:00-13:00）或盘外返回 0（不在盘中，调用方按不可交易处理）。
    - US：美东 16:00 收盘（zoneinfo 动态处理夏令时/冬令时，不可用时回退夏令时硬编码）。
      美股「用户喊停」时点脚本感知不到，只按 16:00 兜底边界算。
      2026-08-18 起美股可交易窗口 = 盘前 + 盘中（美东 04:00-16:00，用户立规），起点
      从 09:30 扩到 04:00——盘前也允许开仓，16:00 收盘边界不变。

    返回 float 分钟。market 取 'HK' / 'US'；返回负值表示已过边界（同 0 处理）。
    用途：open_position 时间闸——距停盯 ≤5 分钟拒开仓（trading-strategy.md
    「距停盯 >5 分钟就仍可开仓」的唯一机械执行点，堵 AI 自设截止线或临场抢开）。
    """
    from datetime import datetime, timedelta
    now = now or datetime.now()
    if market == "HK":
        # 周末不算盘中
        if now.weekday() >= 5:
            return 0.0
        t = now.time()
        am_end = datetime.combine(now.date(), datetime.strptime("12:00", "%H:%M").time())
        pm_start = datetime.combine(now.date(), datetime.strptime("13:00", "%H:%M").time())
        pm_end = datetime.combine(now.date(), datetime.strptime("16:00", "%H:%M").time())
        am_start = datetime.combine(now.date(), datetime.strptime("09:30", "%H:%M").time())
        if am_start <= now < am_end:
            return (am_end - now).total_seconds() / 60
        if pm_start <= now < pm_end:
            return (pm_end - now).total_seconds() / 60
        return 0.0  # 盘外 / 午休
    # US（2026-08-18 起：盘前 04:00 起算可交易，16:00 收盘兜底不变）
    try:
        from zoneinfo import ZoneInfo
        us = now.astimezone(ZoneInfo("America/New_York"))
        if us.weekday() >= 5:
            return 0.0
        close = us.replace(hour=16, minute=0, second=0, microsecond=0)
        mins = (close - us).total_seconds() / 60
        session_start = us.replace(hour=4, minute=0, second=0, microsecond=0)
        if mins < 0 or us < session_start:
            return 0.0  # 已收盘 / 未到盘前起点（美东 04:00 前）
        return mins
    except Exception:
        # zoneinfo 不可用回退：夏令时北京 16:00-次日 04:00（美东 04:00-16:00，含盘前）
        t = now.time()
        if now.weekday() >= 5:
            return 0.0
        if t >= datetime.strptime("16:00", "%H:%M").time():
            # 到次日 04:00 收盘（北京夏令时口径）
            close = datetime.combine(now.date() + timedelta(days=1),
                                     datetime.strptime("04:00", "%H:%M").time())
            return (close - now).total_seconds() / 60
        return 0.0


OPEN_WINDOW_MIN = 5.0  # 距停盯 ≤5 分钟绝对不开仓（trading-strategy.md 2026-08-17 用户立）


def check_open_time_gate(market, now=None):
    """开仓时间闸（2026-08-18 立）：返回 (allowed: bool, msg: str)。

    距停盯 > 5 分钟 → (True, 剩余分钟数说明)；≤ 5 分钟或盘外 → (False, 原因)。
    这是「距停盯 >5 分钟就仍可开仓」规则的工具级执行点——AI 侧不许自设
    「临近收盘/午休不开仓」的截止线（2026-08-17、2026-08-18 两次同类违规后，
    用户立：把忘记规矩导致违反规矩的概率用工具强制降到最低）。
    """
    mins = minutes_to_session_end(market, now)
    if mins <= 0:
        return False, f"当前不在{market}可交易时段（距停盯边界 {mins:.1f} 分钟）——开仓只能在可交易时段执行（美股 = 盘前美东 04:00 起至 16:00 收盘）"
    if mins <= OPEN_WINDOW_MIN:
        return False, (
            f"距{market}停盯边界仅 {mins:.1f} 分钟（≤{OPEN_WINDOW_MIN:.0f} 分钟绝对不开仓，"
            f"trading-strategy.md「开仓的剩余时间边界」）——时间闸拒单"
        )
    return True, f"距{market}停盯 {mins:.1f} 分钟（>5 分钟，开仓资格在——按压缩止盈实算净赔率 ≥1.8 照常评估）"


def parse_mode(argv=None):
    """解析执行模式 --mode（auto / signal），默认 signal（与 trade skill 一致）。

    优先级：--mode 参数 > MODE 环境变量 > 默认 signal。环境变量分支 2026-08-21 补——
    ws_segment 2026-08-16 切到 parse_mode 时注释声称「兼容 --mode 与旧 MODE 环境变量两种
    来源」，但 parse_mode 本体只解析命令行、从未读环境变量，`MODE=auto python3 ws_segment.py
    …` 实测仍落 signal（当天实录靠会话恰好用 futu_ws_segment（只认环境变量）才没出事）。
    三采样脚本（monitor / ws / futu_ws）现统一走本函数，两种来源都认、行为一致。"""
    if argv is None:
        argv = sys.argv[1:]
    for i, a in enumerate(argv):
        if a == "--mode" and i + 1 < len(argv):
            m = argv[i + 1]
            return m if m in ("auto", "signal") else "signal"
        if a.startswith("--mode="):
            m = a.split("=", 1)[1]
            return m if m in ("auto", "signal") else "signal"
    env_mode = os.environ.get("MODE", "")
    return env_mode if env_mode in ("auto", "signal") else "signal"


def load_equity(mode='signal', project_root=None, base_currency='HKD', account=None):
    """按执行模式取当前 equity，返回 (equity, currency, source_str)。

    - mode='auto'：老虎账户净值（港股 base_currency='HKD'、美股 base_currency='USD'，与
      标的计价一致，见 load_equity_tiger）；查询失败 fallback signals/equity-log.csv
      （标记非真实、需修复）。
    - mode='signal'：读 signals/equity-log.csv 末行 equity_after（signal 模式不连账户、
      靠累加值；无记录返回 config.risk.initial_equity）。

    - account（2026-08-20 立，实盘盯盘配套）：None/paper=默认模拟账户、'live'=老虎实盘账户，
      透传给 load_equity_tiger → load_config(account)（2026-08-20 事故：preflight/resume 固定
      走默认账户取净值、实盘盯盘时 B 用错账户，见 load_equity_tiger docstring）。

    auto 模式 equity 必须是账户真实总资产（2026-07-31 用户立）；signal 模式因不碰账户、用
    equity-log 累加假设盈亏（2026-08-01 双模式重构立，见 signal-mode.md「signal 模式权益更新」）。
    2026-08-05 起港美股默认账户均为老虎，本函数随老虎脚本迁移至此（原在已删除的
    trade_utils.py）。
    """
    import csv
    import json
    if project_root is None:
        # trade_utils_tiger.py 在 .claude/skills/trade/scripts/，上五级 = 项目根（signals/equity-log.csv 在项目根 signals/）
        project_root = Path(__file__).resolve().parent.parent.parent.parent.parent
    # config.json 在 skill 根目录（scripts 上一级 = trade/）
    config_path = Path(__file__).resolve().parent.parent / "config.json"
    initial_equity = 100000.0
    currency = "HKD"
    try:
        with open(config_path) as f:
            risk = json.load(f).get("risk", {})
        initial_equity = float(risk.get("initial_equity", 100000))
        currency = risk.get("equity_currency", "HKD")
    except Exception:
        pass

    def _read_equity_log():
        log_path = Path(project_root) / "signals" / "equity-log.csv"
        if not log_path.exists():
            return None
        with open(log_path) as f:
            rows = [r for r in csv.DictReader(f) if not (r.get("date") or "").startswith("#")]
        if not rows:
            return None
        return float(rows[-1]["equity_after"])

    if mode == "signal":
        eq = _read_equity_log()
        if eq is None:
            return initial_equity, currency, f"config initial_equity={initial_equity:.0f}（signal 模式、equity-log 无记录）"
        return eq, currency, "signals/equity-log.csv 末行（signal 模式累加值）"

    # mode == 'auto'：老虎账户（港股 HKD / 美股 USD，与标的计价一致）
    eq, cur = load_equity_tiger(base_currency=base_currency, account=account)
    if eq is None:
        eq = _read_equity_log()
        if eq is not None:
            return eq, currency, f"equity-log.csv 末行（⚠️老虎账户查询失败（{base_currency} 口径），旧手动累加值、非真实，需修复）"
        return initial_equity, base_currency, f"config initial_equity={initial_equity:.0f}（⚠️老虎查询失败且 equity-log 无记录，占位非真实）"
    acct_tag = "实盘" if account == "live" else "默认（模拟）"
    return eq, cur, f"老虎账户 get_prime_assets(base_currency={cur}) 证券段净值（{acct_tag}）"
