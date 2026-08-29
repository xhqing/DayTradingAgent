#!/usr/bin/env python3
"""持仓状态工具强制模块（2026-08-18 立，用户两起事故后）。

事故背景（2026-08-18 实盘）：
  ① AI 开仓 00700 后用户在 App 把止损从 440.0 移到 442.6，AI 未知悉、仍按 440 记忆盯盘；
  ② 止损单 14:04:19 触发成交、持仓归零，AI 空盯约 29 分钟未察觉。
  根因同一：AI 只信「开仓脚本输出 + 行情采样」，从不向账户核对。SKILL.md / auto-mode.md
  虽明文规定「每轮采样查账户当日订单获取最新止损价」「最新止损价不能凭记忆」，但散文
  规定靠 AI 记忆执行、上下文压缩后必衰减（用户 2026-08-18 立「文档规定必须尽可能配工具强制」）。

本模块 = 工具强制落地点：把「持仓期间高频核对账户状态」从 AI 记忆变成段输出自带。

规则（2026-08-18 用户立，本模块即其工具强制实现）：
  - 空仓时不必查持仓状态（别人开的仓不用管）——actions 无未平仓记录 → 直接返回 None、零 API 调用；
  - 自己开仓后（actions 有开仓且无对应平仓）→ 每次调用都查账户：
      持仓实况（量/成本） + 该标的活动止损单最新触发价（用户可能 App 手动改单，不凭记忆）
      + 状态比对告警（账户已无持仓但 AI 未记录平仓 / 量不一致 / 账户有持仓而 actions 无记录）。

段输出用法（采样脚本 finish() 里调用，持仓时打印「📌 持仓状态」行，空仓静默）：
  from account_status import position_status
  s = position_status()
  if s:
      print(s, flush=True)
"""
import os
import re
import sys
from datetime import date
from pathlib import Path

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', '..', '..'))
_ACTIONS_DIR = os.path.join(_PROJECT_ROOT, 'actions')
_TMP_DIR = os.path.join(_PROJECT_ROOT, 'tmp')

# 账户切换：actions 记录里「| 账户 | 实盘 ...」→ 实盘（load_config(account='live')），
# 其余（模拟 / 无账户字段的旧记录）→ 默认账户。凭据由 trade_utils_tiger 内部读取，本模块不打码号。
_LIVE_MARK = re.compile(r'\|\s*账户\s*\|\s*实盘')


def _today_action_files():
    """当日全部交易动作文件（HKT / ET 都可能）。返回存在的文件列表。"""
    today = date.today().strftime('%Y-%m-%d')
    out = []
    for name in ('HKT', 'ET'):
        p = os.path.join(_ACTIONS_DIR, f'{today}-{name}-actions.md')
        if os.path.exists(p):
            out.append(p)
    return out


def _parse_actions(files):
    """解析当日动作文件 → [(时间, 类型, 标的, 方向, 量)]。类型 open/close/move。

    sym 为空（标题无代码的补充记录等）的事件保留在返回列表里（供会话归属推断），
    但不参与持仓推导（见 _holding_from_actions 的空 symbol 过滤，2026-08-19 修——
    原实现把 sym='' 的开仓也塞进 holding，产生「无记录持仓 ['']」假告警）。
    stop（2026-08-23 立，止损单铁律配套）：开仓记录「止损」行 / 移损记录「新止损」行
    解析出的止损价——「持仓在但无活动止损单」告警时用它给恢复指引（失效前止损价）。"""
    events = []
    for fp in files:
        try:
            text = Path(fp).read_text(encoding='utf-8')
        except Exception:
            continue
        # 按动作标题行分节（2026-08-23 修：兼容「## 🟢🟢🟢 开仓」与无 ## 前缀的
        # 「🟢🟢🟢 开仓」两种历史格式——原只按 '\n## ' 切、无前缀的节被并进上一节漏解析）；
        # 每节内找 ⏰ 动作时间与止损价行
        sections = re.split(
            r'\n(?=(?:#+\s*)?(?:🟢🟢🟢|🔴🔴🔴|🟡🟡🟡|🔵🔵🔵)\s*(?:开仓|平仓|移动止损|移动止盈))',
            '\n' + text)
        for sec in sections:
            title = sec.split('\n', 1)[0]
            if '开仓' in title:
                etype = 'open'
            elif '平仓' in title:
                etype = 'close'
            elif '移损' in title or '移动止损' in title:
                etype = 'move'
            else:
                continue
            m = re.search(r'([A-Z]{2}\.\d{5}|\d{5})', title)
            sym = m.group(1) if m else ''
            mtime = re.search(r'⏰\s*动作时间：(\S+)', sec)
            ts = mtime.group(1) if mtime else ''
            mdir = re.search(r'\|\s*方向\s*\|\s*(\S+)', sec)
            direction = mdir.group(1) if mdir else ''
            mqty = re.search(r'\|\s*量\s*\|\s*(\d+)', sec)
            qty = int(mqty.group(1)) if mqty else 0
            # 止损价：开仓「| 止损 | **330.0**…」/ 移损「| 新止损 | 443.0…」（表格行首个数字）
            stop = None
            mstop = (re.search(r'\|\s*止损\s*\|\s*\*{0,2}(\d+(?:\.\d+)?)', sec)
                     if etype == 'open'
                     else re.search(r'\|\s*新止损\s*\|\s*\*{0,2}(\d+(?:\.\d+)?)', sec))
            if mstop:
                stop = float(mstop.group(1))
            events.append({'ts': ts, 'type': etype, 'sym': sym, 'dir': direction,
                           'qty': qty, 'stop': stop})
    # 按时间排序（无时间戳的按记录顺序兜底）
    events.sort(key=lambda e: e['ts'] if e['ts'] else '')
    return events


def _last_recorded_stop(sym, only_session=None):
    """解析该标的当日记录的最新止损价（2026-08-23 立，止损单铁律配套）。

    来源：当日 actions 的开仓「止损」行与移损「新止损」行，按时间序取最新一条
    （移损价天然覆盖开仓价）。找不到返回 None（告警文案会提示按趋势反转位定）。
    sym 格式与 actions 标题一致（HK.07709 / 07709 均可匹配）。"""
    events = _parse_actions(_today_action_files())

    def _latest_stop(evts):
        target_forms = {sym, sym.split('.')[-1]}
        best = None
        for e in evts:
            if e['sym'] in target_forms and e.get('stop') is not None:
                best = e['stop']   # events 已按时间序，后见覆盖先见
        return best

    # 先按会话视角取（并行盯盘不串台）；取不到（历史记录无 session 归属、intent 日志
    # 缺失等）回退全量——恢复止损价的优先级高于会话隔离（铁律场景要的就是这个数）。
    if only_session:
        sym_owner = _session_of_symbol()
        session_events = [e for e in events
                          if not (e['type'] in ('open', 'move') and e['sym']
                                  and _owner_of(sym_owner, e['sym']) not in (only_session, None))]
        best = _latest_stop(session_events)
        if best is not None:
            return best
    return _latest_stop(events)


def _session_id():
    """本进程所属的 Claude Code 会话 id（环境变量注入；无则 ''）。

    段输出脚本（futu_ws_segment / monitor_segment / ws_segment）由 AI 在盯盘会话内
    调起、继承会话环境变量，CLAUDE_CODE_SESSION_ID 即本会话标识（与 trade_mutex 的
    intent 日志、monitor_sessions.txt 注册表同源）。"""
    return os.environ.get('CLAUDE_CODE_SESSION_ID', '')


def _owner_of(owner_map, sym):
    """双格式查归属映射：裸代码（actions 解析口径）与 HK. 前缀（intent 日志口径）都认。

    背景（2026-08-28 修）：_session_of_symbol() 的映射 key 是 HK.01810 富途格式，
    _parse_actions() 解析出的 sym 是 01810 裸代码——原实现直接 owner_map.get(sym)
    永远查不到，导致本会话自己开的仓被误判「别人的」而过滤掉、position_status()
    返回 None、段输出「📌 持仓状态」行静默消失、止损单铁律监控失效（当日实盘
    09988 开仓后实录）。
    """
    if not sym:
        return None
    if sym in owner_map:
        return owner_map[sym]
    code = sym.split('.')[-1]
    return owner_map.get(f'HK.{code}')


def _session_of_symbol():
    """当日「标的 → 开仓会话 id」映射（2026-08-19 立，跨会话告警串台修复）。

    数据源 = tmp/trade_intent.log 的当日 filled intent（开仓脚本在 TradeMutex 内写、
    带本会话 CLAUDE_CODE_SESSION_ID 与标的）。多会话并行盯盘时（当日实录：实盘会话盯
    09888/01347、模拟会话开 01810），A 会话的段输出只核对**自己会话**开的仓——别的
    会话的仓由别的会话核对，告警不再串台（假告警稀释真实告警的「狼来了」效应消除）。
    返回 {sym: sid}（sym 为 intent 的富途格式 HK.01810）。
    """
    intent_log = os.path.join(_TMP_DIR, 'trade_intent.log')
    mapping = {}
    today = date.today().strftime('%Y-%m-%d')
    if not os.path.isfile(intent_log):
        return mapping
    import json
    try:
        with open(intent_log) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if not str(d.get('ts', '')).startswith(today):
                    continue
                if d.get('status') != 'filled':
                    continue
                sym, sid = d.get('symbol', ''), d.get('session_id', '')
                if sym and sid:
                    mapping[sym] = sid   # 同标的多次开仓：后写的覆盖（最新持仓归属）
    except OSError:
        pass
    return mapping


def _holding_from_actions(only_session=None):
    """从当日 actions 推导 AI 认为的当前持仓。返回 {sym: {'qty','dir'}} 或空 dict。

    only_session（2026-08-19 立）：给会话 id 时只推导**该会话**开的仓——开仓事件按
    _session_of_symbol() 的标的归属过滤，别的会话开/平的仓不进本会话视角。None =
    不过滤（旧行为，单会话场景等价）。sym 为空的事件不进持仓（2026-08-19 修——
    原实现 sym='' 的开仓塞进 holding 产生「无记录持仓 ['']」假告警）。"""
    events = _parse_actions(_today_action_files())
    if only_session:
        sym_owner = _session_of_symbol()
        # 只保留「本会话开的仓」的开仓事件；平仓事件按标的归属决定是否影响本会话视角
        events = [e for e in events
                  if not (e['type'] == 'open' and e['sym']
                          and _owner_of(sym_owner, e['sym']) not in (only_session, None))]
        # 平仓事件：标的开仓属于别的会话 → 该平仓与本会话视角无关、剔除
        events = [e for e in events
                  if not (e['type'] == 'close' and e['sym']
                          and _owner_of(sym_owner, e['sym']) is not None
                          and _owner_of(sym_owner, e['sym']) != only_session)]
    holding = {}
    for e in events:
        if not e['sym']:
            continue   # 标题无代码的记录（补充记录等）：不进持仓推导（2026-08-19 修）
        if e['type'] == 'open':
            holding[e['sym']] = {'qty': e['qty'], 'dir': e['dir']}
        elif e['type'] == 'close':
            holding.pop(e['sym'], None)
    return holding


# ---------------------------------------------------------------------------
# 加速赶极端检测（2026-08-19 立，工具强制——「动能将竭主动平仓」的在场打印）
#
# 背景：2026-08-19 小米两笔实盘，10:31 加速赶顶（20 秒 +0.58% 破前高远离 VWAP）、
# 11:12-11:13 急速赶顶，均为 SKILL.md「动能将竭 → 立即主动平仓」条文的命中形态，
# AI 却只在盯「回踩观察」「企稳条件」、未执行主动平仓——散文规定靠记忆必衰减
# （同日对照：工具打印的 VWAP 检查 / 持仓状态全守住，靠记忆的全失守）。本检测挂在
# 段结束输出里（与持仓状态同位）：持仓时每段自动检测，命中即高亮打印、逼 AI 当段决策。
#
# 判据（对应 trading-strategy.md「加速赶极端 = 动能将竭信号」节，全市场同构）：
#   ① 段内涨速 ≥ 0.4% / 40 秒（约每 20 秒 +0.2%，10:31 实测 20 秒 +0.58% 的保守阈值）；
#   ② 现价为本段新高且创「近 N 秒窗口」新高（破前高，N=段窗口）；
#   ③ 现价距 VWAP ≥ +0.8%（远离 VWAP 超买；阈值取当日多次赶顶实测 +0.5~+0.66% 的上浮）。
#   做空对称（涨速 ≤ −0.4% / 创新低 / 距 VWAP ≤ −0.8%）。
#   三条同时命中 → 打印 ⚡ 赶顶/赶底警报（提示 AI 走「主动平仓 vs 让利润奔跑」决断，
#   并非机械平仓指令——趋势健康续涨时该让利润奔跑，由 AI 按「突破后动能是否延续」判）。
# ---------------------------------------------------------------------------
_BLAST_PCT_PER_SEG = 0.4     # ① 段内涨速阈值（%，40 秒段）
_BLAST_VWAP_DIST = 0.8       # ③ 距 VWAP 阈值（%）


def blast_check(vwap_by_sym, prices_by_sym=None):
    """加速赶极端检测（段结束调用）。持仓时检测、空仓跳过（与「空仓不查」同规则）。

    会话视角（2026-08-19 立）：带 CLAUDE_CODE_SESSION_ID 时只检测本会话的仓（同
    position_status——别的会话的仓由别的会话检测，警报不串台）。

    参数 vwap_by_sym: {futu_sym: vwap}（VWAP 检查段已查过的快照数据复用，不重复查询）。
    参数 prices_by_sym: {futu_sym: [价格序列]}——本段价格序列（时间升序）。
        futu_ws_segment 传本段 seg 的秒点序列；monitor_segment（无 seg 结构）传 log
        本段采样序列；不传时回落读调用方模块级 seg（futu_ws_segment 内联调用场景）。
    返回告警行列表（空列表 = 无警报）——由调用方打印。
    """
    holding = _holding_from_actions(only_session=_session_id() or None)
    if not holding:
        return []
    alerts = []
    # 价格序列来源：① 显式 prices_by_sym（sym→价格 list）优先；② futu_ws_segment 风格的
    # seg dict（sym→{'sec': {ts: price}}）——显式传入若是 dict-of-dict 也能走这条。
    def _extract(sym):
        if prices_by_sym is not None and sym in prices_by_sym:
            v = prices_by_sym[sym]
            if isinstance(v, dict):           # seg 风格 {'sec': {...}}
                sec = v.get('sec')
                return [p for _, p in sorted(sec.items())] if isinstance(sec, dict) else None
            if isinstance(v, (list, tuple)):  # 纯价格序列
                return list(v)
        return None
    for sym, info in holding.items():
        prices = _extract(sym)
        if prices is None:
            # 回落：调用方（futu_ws_segment）模块级 seg
            caller_seg = sys._getframe(1).f_globals.get('seg', {})
            d = caller_seg.get(sym)
            sec = d.get('sec') if isinstance(d, dict) else None
            prices = [p for _, p in sorted(sec.items())] if isinstance(sec, dict) else None
        if not prices or len(prices) < 10:
            continue  # 秒点太少（刚订阅 / 推送稀疏）不判
        first, last = prices[0], prices[-1]
        if first in (None, 0):
            continue
        pct = (last - first) / first * 100
        vwap = vwap_by_sym.get(sym)
        vwap_dist = ((last - vwap) / vwap * 100) if (vwap not in (None, 0)) else None
        is_long = '多' in info['dir'] or 'long' in info['dir'].lower()
        if is_long:
            hit_speed = pct >= _BLAST_PCT_PER_SEG
            hit_extreme = last >= max(prices) - 1e-9   # 现价即段内最高（破前高收尾）
            hit_far = vwap_dist is not None and vwap_dist >= _BLAST_VWAP_DIST
            if hit_speed and hit_extreme and hit_far:
                alerts.append(
                    f'⚡⚡⚡ {sym} 持仓（做多）加速赶顶形态命中：段内 {pct:+.2f}% / 现价 {last} = 段新高 / 距 VWAP +{vwap_dist:.2f}% 超买\n'
                    f'    → 按 trading-strategy.md「动能将竭」节当场决断：加速赶顶 = 动能将竭信号，\n'
                    f'      主动平仓 🔴 锁利（不等止损被动触发）；仅当突破后动能明确延续（续创新高 + 买盘维持）才让利润奔跑。\n'
                    f'      本警报必须在本段分析里显式回应（平 / 不平 + 理由），不得静默跳过。'
                )
        else:
            hit_speed = pct <= -_BLAST_PCT_PER_SEG
            hit_extreme = last <= min(prices) + 1e-9
            hit_far = vwap_dist is not None and vwap_dist <= -_BLAST_VWAP_DIST
            if hit_speed and hit_extreme and hit_far:
                alerts.append(
                    f'⚡⚡⚡ {sym} 持仓（做空）加速赶底形态命中：段内 {pct:+.2f}% / 现价 {last} = 段新低 / 距 VWAP {vwap_dist:.2f}% 超卖\n'
                    f'    → 对称规则：动能将竭 → 主动平仓 🔴 锁利，本段分析必须显式回应。'
                )
    return alerts


def _pos_symbol(p):
    """持仓对象的证券代码。SDK Position 无顶层 symbol 属性（2026-08-19 实测），
    代码挂在 p.contract.symbol——此前误用 getattr(p, 'symbol', '') 恒取默认值 ''，
    导致持仓期间每段必误报「账户已无持仓」+「存在无记录持仓 ['']」。"""
    contract = getattr(p, 'contract', None)
    return getattr(contract, 'symbol', None) if contract else None


def _query_account(holding, account=None):
    """有持仓时查老虎账户：持仓实况 + 活动止损单触发价。返回 (持仓列表, 止损价 dict, 账户标签, 查询错误)。

    2026-08-19 修「实持 0 股」误报（当日实录：开仓 01810 后连续两段误报、AI 每次手动
    核实才发现账户明明有仓）：原实现 get_positions 抛异常（代理节点漂移出老虎 IP 白名单
    等）被 except 静默吞成 []——查询失败与「真的空仓」两种情形在段输出里**长得一样**，
    都打「🚨 账户已无持仓」假告警（狼来了效应：真实平仓告警被淹没）。修法：查询异常
    不再吞——positions 记为 None 并把错误文本带回段输出（「账户查询失败」与「账户已无
    持仓」显式区分，前者不触发平仓处置动作、只提示修复）。

    account（2026-08-19 立会话视角后新增）：显式指定账户（'live' / None=按当日 actions
    实盘标记判定）。"""
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from trade_utils_tiger import load_config, get_today_orders_tiger
        from tigeropen.trade.trade_client import TradeClient
    except Exception as e:
        return None, {}, '', f'模块导入失败: {e}'
    # 账户选择：显式指定优先；否则当日 actions 里出现过「实盘」→ 实盘账户，否则默认（模拟）
    account_tag = ''
    query_err = ''
    try:
        if account is None:
            files = _today_action_files()
            is_live = any(_LIVE_MARK.search(Path(f).read_text(encoding='utf-8')) for f in files)
            account = 'live' if is_live else None
        config = load_config(account=account) if account else load_config()
        account_tag = '实盘' if account == 'live' else '默认（模拟）'
    except Exception as e:
        return None, {}, '', f'配置加载失败: {e}'
    try:
        tc = TradeClient(config)
        positions = tc.get_positions() or []
    except Exception as e:
        # ⚠️ 查询失败 ≠ 空仓（原实现吞异常成 [] = 假告警根源，见 docstring）
        positions = None
        query_err = f'持仓查询失败: {str(e)[:120]}'
    # 止损价：活动 STP 单最新触发价（同 monitor_segment.query_stop_prices 口径）
    stops = {}
    try:
        orders = get_today_orders_tiger(config)
        for order in orders:
            contract = getattr(order, 'contract', None)
            sym = getattr(contract, 'symbol', None) if contract else None
            if not sym:
                continue
            ot = getattr(order, 'order_type', None)
            if ot is None or 'STP' not in str(ot):
                continue
            st_obj = getattr(order, 'status', None)
            st = st_obj.value if hasattr(st_obj, 'value') else str(st_obj)
            if any(k in st for k in ('Filled', 'Cancelled', 'Expired', 'Inactive', 'Invalid')):
                continue
            aux = getattr(order, 'aux_price', None)
            if not aux or float(aux) <= 0:
                continue
            oid = int(getattr(order, 'id', 0) or 0)
            if sym not in stops or oid > stops[sym][1]:
                stops[sym] = (float(aux), oid)
    except Exception as e:
        # 止损价查失败只降级该部分（「无活动止损单」提示附错误），不影响持仓核对主体
        query_err = (query_err + '；' if query_err else '') + f'止损单查询失败: {str(e)[:80]}'
    return positions, {k: v[0] for k, v in stops.items()}, account_tag, query_err


def position_status():
    """持仓状态段输出（工具强制）。无持仓返回 None（不查账户，遵循「空仓不查」）；有持仓返回多行字符串。

    会话视角（2026-08-19 立，跨会话串台修复）：进程带 CLAUDE_CODE_SESSION_ID 时只核对
    **本会话**开的仓（intent 日志归属）——并行盯盘时 A 会话不再把 B 会话的仓当自己的核对、
    告警不串台；无会话 id（手动跑脚本）退回旧行为（全核对）。"""
    sid = _session_id()
    holding = _holding_from_actions(only_session=sid or None)
    if not holding:
        return None
    positions, stops, account_tag, query_err = _query_account(holding)
    lines = []
    if positions is None:
        # 查询失败 ≠ 空仓（2026-08-19 修：不再吞成 [] 触发「已无持仓」假告警）——
        # 明示失败原因，AI 处置 = 修复查询（查代理/白名单），不做平仓处置
        lines.append(f'🚨 持仓状态查询失败（账户 {account_tag}）: {query_err}')
        lines.append('    → 这是查询链路故障、**不是**「账户已无持仓」——不要按空仓处置；'
                     '先排查（常见根因：代理节点漂移出老虎 IP 白名单，见 proxy_guard 白名单守护），'
                     '修复前本段以 actions 记录为持仓依据。')
        for sym, info in holding.items():
            stop = stops.get(sym.split('.')[-1])
            stop_str = f'{stop:.2f}' if stop is not None else '无活动止损单'
            lines.append(f'📌（actions 口径）{sym} {info["qty"]}股({info["dir"]}) | 活动止损单触发价 {stop_str}')
        return '\n'.join(lines)
    for sym, info in holding.items():
        code = sym.split('.')[-1]
        # 账户持仓匹配（老虎持仓 symbol 为裸代码，取 p.contract.symbol——见 _pos_symbol）
        acct_pos = [p for p in positions if _pos_symbol(p) == code]
        acct_qty = sum(int(getattr(p, 'quantity', 0) or 0) for p in acct_pos)
        stop = stops.get(code)
        stop_str = f'{stop:.2f}' if stop is not None else '无活动止损单'
        base = (f'📌 持仓状态: {sym} {info["qty"]}股({info["dir"]}) | 账户实持 {acct_qty}股'
                f'（{account_tag}） | 活动止损单触发价 {stop_str}')
        # 🚨 止损单铁律（2026-08-23 用户立）：持仓在 → 止损单必须存在。账户仍有持仓但
        # 无活动止损单 = 铁律被破坏（最常见：瞬时插针触发止损但平仓失败、价格反弹回来——
        # 止损单已终结、仓位还在、处于裸奔状态）。按当前盘面二选一处置（AI 当段执行）：
        #   a) 仍想平仓 → 跑 close_position 脚本（循环第④步会补设止损单、触发价=现价、
        #      立刻市价触发平仓）；
        #   b) 不想平仓了（价格已反弹回来）→ **立刻恢复失效前的止损单**（move_stop 按记录
        #      里的最新止损价重设），恢复后继续盯盘。
        # 失效前止损价从当日 actions 解析（开仓「止损」/ 移损「新止损」行，见 _parse_actions）。
        if stop is None and acct_qty > 0:
            last_stop = _last_recorded_stop(sym, only_session=sid or None)
            ls_str = f'{last_stop:.2f}' if last_stop is not None else '（当日记录未解析到止损价，按最新趋势反转位定）'
            lines.append(f'🚨 {base} —— 止损单铁律被破坏：**持仓在、止损单必须存在**，当前处于无止损裸奔状态！')
            lines.append(f'    → 处置（当段执行、二选一）：a) 仍想平仓 → close_position（补设止损单触发价=现价、市价触发离场）；')
            _dir_en = 'long' if '多' in info['dir'] else 'short'
            lines.append(f'       b) 不想平仓（价格已反弹）→ 立刻恢复止损单：move_stop {sym} {_dir_en} {ls_str} {acct_qty}'
                         f'（失效前止损价 {ls_str}），恢复后继续盯盘')
            continue
        if acct_qty == 0:
            lines.append(f'🚨 {base} —— 账户已无持仓但 actions 无平仓记录：止损可能已触发/已被平，AI 立即核对账户！')
        elif acct_qty != info['qty']:
            lines.append(f'⚠️ {base} —— 数量与 actions 记录不一致（部分成交/降档/手动改单），以账户为准')
        else:
            # 👤 用户改单检测（2026-08-26 立，当日实录：用户 App 手动改止损触发价 39.5→40.46，
            # AI 段输出只显数字、误判为「下单参数被改」的生产 bug、留未知问题排查）。
            # 逻辑：账户活动止损触发价 ≠ 当日 actions 最新记录止损价 → 排除法唯一解释 =
            # 用户在 App 改的（AI 每次移损都写记录，脚本下单的止损价不会自己变）。
            # 明示「判定为用户改单」+ 方向风险判断，AI 不再当未知 bug 排查；用户改反方向
            # （做空上移 = 放宽风险、做多下移 = 放宽风险）时当场提醒。
            note = ''
            if stop is not None:
                rec_stop = _last_recorded_stop(sym, only_session=sid or None)
                if rec_stop is not None and abs(stop - rec_stop) >= 0.005:
                    is_short = '空' in info['dir'] or 'short' in info['dir'].lower()
                    loosened = (stop > rec_stop) if is_short else (stop < rec_stop)
                    risk_word = ('放宽（离场更远、风险变大）' if loosened
                                 else '收紧（离场更近、风险变小）')
                    warn = (' ⚠️ 注意：方向与持仓相反的移动——若是按做多习惯改的请复核'
                            if loosened else '')
                    note = (f' | 👤 用户改单：账户止损 {stop:.2f} ≠ 记录 {rec_stop:.2f}'
                            f'（判定为您在 App 手动修改，非脚本异常）——较记录{risk_word}{warn}')
            lines.append(base + ' | 账户一致 ✅' + note)
    # 账户有持仓而 actions 无记录 → 提示（用户手动开仓等漏网路径）。
    # 空 symbol（合约缺代码的脏数据）不计入（2026-08-19 修「无记录持仓 ['']」假告警）。
    acct_syms = {_pos_symbol(p) for p in positions} - {None, ''}
    known = {s.split('.')[-1] for s in holding}
    extra = acct_syms - known
    if extra:
        lines.append(f'⚠️ 账户存在 actions 无记录的持仓 {sorted(extra)}——用户手动开仓？AI 处置：按单持仓护栏判断是否需要处理')
    if query_err:
        lines.append(f'⚠️ 附注（不影响持仓核对主体）: {query_err}')
    return '\n'.join(lines)


if __name__ == '__main__':
    # 自测：python3 account_status.py [--force-show]
    s = position_status()
    if s:
        print(s)
    else:
        print('空仓（或不判定持仓）：不查账户（遵循「空仓不查」规则）')
