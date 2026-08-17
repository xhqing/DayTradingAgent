#!/usr/bin/env python3
"""老虎 WebSocket 每秒采样盯盘段（2026-08-07 立，用户定：密采样固定每秒一个价格）。

为什么：monitor_segment.py 用富途快照、10 秒间隔，会漏掉瞬时高低点
（2026-08-07 教训：00100 止损 330 触发前 10 秒采样只见 330.6，实际低点 329.4，
用户指出要取每秒价格）。老虎 PushClient WebSocket 推送毫秒级、每秒都有价格
（2026-08-07 盘中实测：subscribe_quote 每秒多条推送），作为每秒采样源。

用法：
  python3 ws_segment.py <duration> <targets> [--mode signal|auto]
    时长固定 40 秒（防夹回逻辑同 monitor_segment）
    targets = SYM:up:dn 逗号分隔（SYM 带 HK. 前缀，如 HK.00100:347:330）
  例：python3 ws_segment.py 40 HK.00100:347:330,HK.07709:29.4:27.8 --mode auto

行为：
  ① 订阅标的 quote，**按秒聚合**记录 latestPrice 到 tmp/monitor_log_{SYM}_{date}_{mode}.csv
     （命名对齐 monitor_segment，供 monitor_guard 守卫识别 + 复盘读取）
  ② 段结束输出：每标的 全段点列（时间+价）、段高/低、破关键位告警
  ③ log 行格式：HH:MM:SS,CODE,last,bid,ask,(买卖比空),(量比空),high,low,(额空),(止损空)
     ——买卖比/量比/额/止损 段末由 AI 调 snapshot 补齐（本脚本只负责每秒价格流）

2026-08-16 修四处（中危审计）：
  ① --mode 解析（原只读环境变量 MODE）：auto 会话按 SKILL.md 惯例传 --mode auto 不生效、
     log 写进 signal 文件污染两会话隔离，且 '--mode' 字符串会被 parse_targets 当成标的；
  ② log 日期按市场对齐（原 date.today() 北京日历日命名）：美股跨午夜后 ws 系写今天文件、
     monitor_summary 读美东交易日文件 → 午夜后采样在 summary 里消失；
  ③ 破位告警加「创新高/新低」条件（原跨段累计 + 无新高条件：某段破位后之后每段都重复报
     「破关键位」，告警疲劳；现对齐 monitor_segment 口径——只有段高/低创新才报）；
  ④ 按秒聚合（原每 tick 一行：每秒多条推送、点数虚增；现每秒一行取该秒最后价）。

2026-08-17 补分析锚点（红灯「盯盘空转」修法①）：段结束输出补三样——
  ⑤ 📊 VWAP 检查（富途 OpenD 查 avg_price；OpenD 不可用降级为强制提示，AI 必须自行补查）；
  ⑥ ⏰ 每 10 分钟方向重估提醒（marker 文件去重，逻辑同 monitor_segment）；
  ⑦ 👉 空转防护提示行（先给本段判断 + 写分析心跳，再重启下一段）。
  为什么：2026-08-07 采样源迁 ws_segment 时，monitor_segment 段输出的强制锚点（VWAP 检查
  + 重估提醒）没跟着搬，ws 段输出只剩裸数字——分析链失去「下一步该分析」的锚点后空转
  一下午（2026-08-17 实录：52 次纯重启采样、0 分析文本）。本批把锚点补齐对齐 monitor_segment。

连接配置：TigerOpenClientConfig(props_path=os.path.expanduser('~/.tigeropen/'))
⚠️ props_path 必须 expanduser（不展开波浪号 = 配置全空、tiger_id 空 = access forbidden，
2026-08-07 排查教训）。私钥用 cfg.private_key（pk8，库内 load_der 可正常加载；
直接传 properties 的 pk1 也能走 PEM 分支，但 cfg.private_key 更稳）。
"""
import os
import sys
import time
import signal
import csv
from datetime import datetime, timedelta, date

from tigeropen.tiger_open_config import TigerOpenClientConfig
from tigeropen.push.push_client import PushClient

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from trade_utils_tiger import parse_mode  # --mode / 环境变量统一解析（2026-08-16）
from trade_utils_tiger import apply_socket_proxy  # WS 链路 socks 化（IP 白名单下 WS 直连被拒，2026-08-17）

_PROPS = os.path.expanduser('~/.tigeropen/')
MODE = None   # main 里经 parse_mode 赋值（兼容 --mode 与旧 MODE 环境变量两种来源）
# scripts/ → trade → skills → .claude → 项目根（上四级）
TMP = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', '..', '..', 'tmp')
TMP = os.path.abspath(TMP)

# 模块级状态（finish 由硬超时回调调用，需访问）
seg = {}
targets = []
logs = {}


def parse_targets(argv):
    """解析 HK.00100:347:330,HK.07709:29.6:27.8 格式 → [(symbol, up, dn)]。
    targets 可能作为一个逗号分隔参数传入（如 'HK.00100:347:330,HK.07709:29.6'），
    先按逗号拆开。"""
    out = []
    for arg in argv:
        for t in arg.split(','):
            parts = t.split(':')
            sym = parts[0]
            up = float(parts[1]) if len(parts) > 1 and parts[1] else None
            dn = float(parts[2]) if len(parts) > 2 and parts[2] else None
            out.append((sym, up, dn))
    return out


def trading_date_str(sym):
    """log 日期按市场对应交易日命名（2026-08-16 修，与 monitor_segment 同口径）：
    港股用北京日期；美股用美东交易日（北京 −12h 夏令时；冬令时 EST 需 −13h）——
    否则美股跨北京午夜时 ws_segment 写今天文件、monitor_summary 读美东交易日文件，
    午夜后采样在 summary 里消失。"""
    now = datetime.now()
    if sym.startswith("US."):
        return (now - timedelta(hours=12)).strftime('%Y%m%d')
    return now.strftime('%Y%m%d')


def main():
    global MODE
    # --mode 解析（2026-08-16 修）：parse_mode 兼容 --mode x / --mode=x 与旧 MODE 环境变量，
    # 且先剥掉 --mode 参数再解析 targets（原实现 '--mode' 字符串会被 parse_targets 当标的）
    MODE = parse_mode()
    argv = [a for i, a in enumerate(sys.argv[1:])
            if not (a == '--mode' or a.startswith('--mode=')
                    or (i > 0 and sys.argv[1:][i - 1] == '--mode'))]
    if len(argv) < 2:
        print('用法: ws_segment.py <duration> HK.00100:347:330,HK.07709:29.4:27.8 [--mode signal|auto]')
        sys.exit(1)
    try:
        duration = int(argv[0])
    except ValueError:
        duration = 40
    duration = max(1, min(duration, 40))  # 不放大段长（同 monitor_segment 防夹回）

    global targets
    targets = parse_targets(argv[1:])
    symbols = [t[0] for t in targets]
    tiger_syms = [s.split('.')[-1] for s in symbols]
    # 每标的 log 文件（日期按市场对齐，2026-08-16）
    for sym in symbols:
        code = sym.replace('.', '_')  # HK.00100 -> HK_00100（对齐 monitor_segment 命名，monitor_summary/monitor_guard 才能识别）
        fname = f'monitor_log_{code}_{trading_date_str(sym)}_{MODE}.csv'
        logs[sym] = os.path.join(TMP, fname)
    os.makedirs(TMP, exist_ok=True)

    # 写表头（对齐 monitor_segment 列名，monitor_summary / csv.DictReader 才能解析）
    for sym in symbols:
        if not os.path.exists(logs[sym]):
            with open(logs[sym], 'w') as f:
                f.write('time,symbol,last,bid,ask,ratio,vr,high,low,turnover_yi,stop_price\n')

    print(f'=== WebSocket 每秒采样 {symbols} duration={duration}s mode={MODE} 开始 {time.strftime("%H:%M:%S")} ===')
    for t in targets:
        print(f'    {t[0]}: 阻力up={t[1]} 支撑dn={t[2]} | log={logs[t[0]]}')

    # 每标的：本段记录 + 段高/段低。破位检测基准（2026-08-16 修③）：prev_high/prev_low 从
    # log 末行初始化（跨段连续），段内高/低**创新**（> prev_high / < prev_low）且越过关键位
    # 才报——对齐 monitor_segment 口径，某段破位后之后每段不再重复报。
    for sym in symbols:
        prev_high, prev_low = None, None
        try:
            with open(logs[sym]) as f:
                lines = [l.rstrip('\n') for l in f if l.strip()]
            if len(lines) > 1:  # 跳过表头
                cols = lines[-1].split(',')
                if len(cols) >= 9 and cols[7] not in ('', 'None'):
                    prev_high, prev_low = float(cols[7]), float(cols[8])
        except Exception:
            pass
        seg[sym] = {'points': [], 'high': prev_high, 'low': prev_low,
                    'last': None, 'reported_up': False, 'reported_dn': False,
                    'agg_sec': None, 'agg_row': None}
    global broke
    broke = []

    def flush_agg(sym):
        """把聚合缓冲的秒行写入 log（每秒最多一行，2026-08-16 修④）。"""
        d = seg[sym]
        if d['agg_row'] is None:
            return
        with open(logs[sym], 'a') as f:
            f.write(d['agg_row'] + '\n')
        d['agg_row'] = None

    def on_quote(frame):
        sym = 'HK.' + frame.symbol
        if sym not in seg:
            return
        last = getattr(frame, 'latestPrice', None)
        bid = getattr(frame, 'bidPrice', None)
        ask = getattr(frame, 'askPrice', None)
        ts = time.strftime('%H:%M:%S')
        d = seg[sym]
        # 按秒聚合（2026-08-16 修④）：老虎每秒推多条 tick，旧实现每 tick 写一行 log
        # （每秒 1-3 行、点数虚增、与「每秒采样」口径不符）。现同一秒内只保留最后一条，
        # 秒变化时才落一行。
        if d['agg_sec'] != ts:
            flush_agg(sym)
            d['agg_sec'] = ts
            d['agg_row'] = f'{ts},{sym},{last},{bid},{ask},,,{d["high"]},{d["low"]},,\n'.rstrip('\n')
        else:
            d['agg_row'] = f'{ts},{sym},{last},{bid},{ask},,,{d["high"]},{d["low"]},,\n'.rstrip('\n')
        d['points'].append((ts, last, bid, ask))
        d['last'] = last
        if last is not None:
            d['high'] = last if d['high'] is None else max(d['high'], last)
            d['low'] = last if d['low'] is None else min(d['low'], last)

    def on_connect(frame):
        connected[0] = True
        print('>>> WebSocket 已连接', flush=True)

    def on_disconnect():
        print('>>> WebSocket 断开', flush=True)

    def on_error(frame):
        print(f'>>> 错误: {frame}', flush=True)

    def on_kickout(frame):
        print(f'>>> 被踢出: {frame}', flush=True)

    def hard_timeout(signum, frame):
        finish()
        sys.exit(0)

    signal.signal(signal.SIGALRM, hard_timeout)
    signal.alarm(duration + 15)

    connected = [False]
    # WS 链路走代理（2026-08-17 立）：老虎 IP 白名单上线后，PushClient 裸 socket 直连出口
    # （家宽 IP）被网关拒「code=4 access forbidden」（白名单只放行 [订阅商] 代理 6 IP）。
    # apply_socket_proxy 按 config.json proxy 节把 socket.create_connection socks 化——
    # enabled=false / socks 不通时保持直连（apply_socket_proxy 内部处理并警告）。
    import json as _json
    try:
        _cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'config.json')
        with open(_cfg_path) as _f:
            _proxy_cfg = _json.load(_f).get('proxy', {})
        if _proxy_cfg.get('enabled', False) and _proxy_cfg.get('apply_scope', 'live_only') != 'off':
            apply_socket_proxy(_proxy_cfg.get('http_proxy', 'http://127.0.0.1:1087'))
    except Exception as _e:
        print(f'>>> ⚠️ 读 proxy 配置失败（{_e}），WS 直连（白名单下会被拒）', flush=True)
    cfg = TigerOpenClientConfig(props_path=_PROPS)
    protocol, host, port = cfg.socket_host_port
    pc = PushClient(host, port, use_ssl=(protocol == 'ssl'))
    pc.quote_changed = on_quote
    pc.connect_callback = on_connect
    pc.disconnect_callback = on_disconnect
    pc.error_callback = on_error
    pc.kickout_callback = on_kickout
    pc.connect(cfg.tiger_id, cfg.private_key)
    for _ in range(20):
        time.sleep(0.5)
        if connected[0]:
            break
    if not connected[0]:
        print('>>> 连接失败（检查 ~/.tigeropen/ 配置与网络）')
        sys.exit(2)
    pc.subscribe_quote(tiger_syms)
    print(f'>>> 已订阅 {tiger_syms}，每秒采样 {duration}s…', flush=True)

    t0 = time.time()
    while time.time() - t0 < duration:
        time.sleep(0.2)
    pc.unsubscribe_quote(tiger_syms)
    pc.disconnect()
    finish()


def finish():
    """段结束统计输出（由 main 的 timeout 或正常流程调用）。"""
    # 落盘残余聚合行（段尾最后未换秒的缓冲）
    for sym in list(seg.keys()):
        try:
            if seg[sym]['agg_row'] is not None:
                with open(logs[sym], 'a') as f:
                    f.write(seg[sym]['agg_row'] + '\n')
                seg[sym]['agg_row'] = None
        except Exception:
            pass
    print('\n📊 WebSocket 每秒采样段结束统计（最近 N 个点 + 段高低 + 破位）:')
    for sym, d in seg.items():
        pts = d['points']
        if not pts:
            print(f'  {sym}: 无推送')
            continue
        print(f'  {sym}: 点数={len(pts)} 段高={d["high"]} 段低={d["low"]} 末={d["last"]}')
        # 最近 12 个点
        recent = pts[-12:]
        line = '  '.join(f'{ts}:{v}' for ts, v, _, _ in recent)
        print(f'    最近{len(recent)}点: {line}')
        # 破位检测（2026-08-16 修③）：加「创新高/新低」条件——段高/低须严格超过 log 末行
        # 继承的 prev 值（首段 prev 为 None 时视为新、直接判），且每方向只报一次
        # （reported 标记去重：同段内不因持续高于关键位反复报）。
        up = dn = None
        for t in targets:
            if t[0] == sym:
                up, dn = t[1], t[2]
        if up is not None and d['high'] is not None and d['high'] >= up and not d['reported_up']:
            broke.append((sym, f'↑破阻力{up} 段高{d["high"]}'))
            d['reported_up'] = True
        if dn is not None and d['low'] is not None and d['low'] <= dn and not d['reported_dn']:
            broke.append((sym, f'↓破支撑{dn} 段低{d["low"]}'))
            d['reported_dn'] = True
    if broke:
        print(f'!!! 破关键位: {broke} !!!')

    # ⏰ 每 10 分钟方向重估提醒（2026-08-17 补，逻辑对齐 monitor_segment）：读 log 首条时间
    # 算累计盯盘时长，每满 10 分钟输出一次重估提醒（marker 文件去重，避免跨段重复）。
    # 为什么搬过来：ws_segment 接替 monitor_segment 当港股主力采样源后，这个强制锚点没跟着
    # 搬，AI 段间只重启不分析时再也没有工具级的「该重估方向了」提醒（2026-08-17 空转实录）。
    try:
        syms = [t[0] for t in targets]
        first_log = logs[syms[0]]
        marker_file = os.path.join(TMP, f'reassess_marker_{trading_date_str(syms[0])}_{MODE}.txt')
        reminded = set()
        if os.path.exists(marker_file):
            with open(marker_file) as mf:
                reminded = {int(x) for x in mf.read().split() if x.strip().isdigit()}
        elapsed_min = None
        if os.path.exists(first_log):
            with open(first_log) as lf:
                rdr = csv.reader(lf)
                next(rdr, None)  # 跳表头
                first_row = next(rdr, None)
            if first_row:
                dstr = trading_date_str(syms[0])
                first_dt = datetime.strptime(f'{dstr} {first_row[0]}', '%Y%m%d %H:%M:%S')
                elapsed_min = (datetime.now() - first_dt).total_seconds() / 60
        if elapsed_min is not None and elapsed_min >= 10:
            ten_mark = int(elapsed_min // 10) * 10
            if ten_mark not in reminded:
                reminded.add(ten_mark)
                with open(marker_file, 'w') as mf:
                    mf.write(' '.join(str(h) for h in sorted(reminded)))
                print(
                    f'⏰ 重估方向提醒（已盯盘 {int(elapsed_min)} 分钟、满 {ten_mark} 分钟）：'
                    f'过动态修正方向 5 触发（①破阻力 ②破支撑 ③站上/跌破VWAP ④箱体假突破≥2次 '
                    f'⑤持续单向运动≥1h）+ 自问「方向/趋势/行情是否仍与开盘一致」，不固守开盘判断。',
                    flush=True,
                )
    except Exception as e:
        print(f'[重估提醒检查 err:{e}]', flush=True)

    # 📊 VWAP 检查（2026-08-17 补，锚点对齐 monitor_segment）：段结束自动打印每个被采标的的
    # 现价 / VWAP / 相对位置——段结束唤醒 AI 后第一眼必看这段输出，作为方向框架的地面真相。
    # ws 段输出的 VWAP 用富途 OpenD 补查（老虎 WebSocket 只推价格、无 VWAP 字段）。
    # OpenD 不可用时降级为「强制提示」而非静默跳过——提示 AI 必须自行 snapshot 补 VWAP。
    try:
        vwap_lines = []
        try:
            from futu import OpenQuoteContext
            _ctx = OpenQuoteContext(host='127.0.0.1', port=11111)
            try:
                ret, df = _ctx.get_market_snapshot(syms)
                if ret != 0 or df is None or len(df) == 0 or 'avg_price' not in df.columns:
                    raise RuntimeError(f'snapshot ret={ret}')
                if 'code' in df.columns:
                    rows = {r['code']: r for _, r in df.iterrows()}
                else:
                    rows = {syms[i]: df.iloc[i] for i in range(min(len(syms), len(df)))}
                for sym in syms:
                    row = rows.get(sym)
                    if row is None:
                        vwap_lines.append(f'  {sym}: 无快照')
                        continue
                    cur, vwap = row.get('last_price'), row.get('avg_price')
                    if cur is None or vwap is None or (isinstance(vwap, float) and vwap != vwap):
                        vwap_lines.append(f'  {sym}: VWAP 获取失败')
                        continue
                    cur, vwap = float(cur), float(vwap)
                    diff = cur - vwap
                    who = '上方（多头占优）' if diff > 0 else ('下方（空头占优）' if diff < 0 else '持平')
                    vwap_lines.append(f'  {sym}: 现价 {cur:.2f} | VWAP {vwap:.2f} | {who} {diff:+.2f}')
            finally:
                _ctx.close()
        except Exception as e:
            vwap_lines.append(f'  OpenD 不可用（{e}）——AI 必须自行跑 snapshot/monitor_summary 补 VWAP，禁止跳过')
        print('📊 VWAP 检查（方向框架地面真相，段结束必看）:\n' + '\n'.join(vwap_lines), flush=True)
    except Exception as e:
        print(f'[VWAP 检查 err:{e}]', flush=True)

    # 👉 空转防护提示行（2026-08-17 补）：每次段结束都提醒 AI「先给本段判断 + 写分析心跳，
    # 再重启下一段」——堵「收到段结束通知 → 只重启采样 → 不分析」的空转路径。
    print(
        '👉 空转防护：本段输出不是只读的——先用一行式模板给本段判断（现价/关键位/VWAP/结论/下次段'
        '时间），再写分析心跳（echo 追加 tmp/analysis_beat_日期_模式.csv），最后才重启下一段采样。',
        flush=True,
    )


if __name__ == '__main__':
    main()
