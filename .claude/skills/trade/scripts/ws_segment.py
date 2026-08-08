#!/usr/bin/env python3
"""老虎 WebSocket 每秒采样盯盘段（2026-08-07 立，用户定：密采样固定每秒一个价格）。

为什么：monitor_segment.py 用富途快照、10 秒间隔，会漏掉瞬时高低点
（2026-08-07 教训：00100 止损 330 触发前 10 秒采样只见 330.6，实际低点 329.4，
用户指出要取每秒价格）。老虎 PushClient WebSocket 推送毫秒级、每秒都有价格
（2026-08-07 盘中实测：subscribe_quote 每秒多条推送），作为每秒采样源。

用法：
  python3 scripts/ws_segment.py <duration> <targets> <interval_sep=:>
    时长固定 40 秒（防夹回逻辑同 monitor_segment）
    targets = SYM:up:dn 逗号分隔（SYM 带 HK. 前缀，如 HK.00100:347:330）
  例：python3 scripts/ws_segment.py 40 HK.00100:347:330,HK.07709:29.4:27.8

行为：
  ① 订阅标的 quote，每秒记录 latestPrice 到 tmp/monitor_log_{SYM}_{date}_{mode}.csv
     （命名对齐 monitor_segment，供 monitor_guard 守卫识别 + 复盘读取）
  ② 段结束输出：每标的 全段点列（时间+价）、段高/低、破关键位告警
  ③ log 行格式：HH:MM:SS,CODE,last,bid,ask,(买卖比空),(量比空),high,low,(额空),(止损空)
     ——买卖比/量比/额/止损 段末由 AI 调 snapshot 补齐（本脚本只负责每秒价格流）

连接配置：TigerOpenClientConfig(props_path=os.path.expanduser('~/.tigeropen/'))
⚠️ props_path 必须 expanduser（不展开波浪号 = 配置全空、tiger_id 空 = access forbidden，
2026-08-07 排查教训）。私钥用 cfg.private_key（pk8，库内 load_der 可正常加载；
直接传 properties 的 pk1 也能走 PEM 分支，但 cfg.private_key 更稳）。
"""
import os
import sys
import time
import signal
from datetime import datetime, date

from tigeropen.tiger_open_config import TigerOpenClientConfig
from tigeropen.push.push_client import PushClient

_PROPS = os.path.expanduser('~/.tigeropen/')
MODE = os.environ.get('MODE', 'signal')
TODAY = date.today().strftime('%Y%m%d')
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


def main():
    if len(sys.argv) < 3:
        print('用法: ws_segment.py <duration> HK.00100:347:330,HK.07709:29.4:27.8')
        sys.exit(1)
    try:
        duration = int(sys.argv[1])
    except ValueError:
        duration = 40
    duration = max(1, min(duration, 40))  # 不放大段长（同 monitor_segment 防夹回）

    global targets
    targets = parse_targets(sys.argv[2:])
    symbols = [t[0] for t in targets]
    tiger_syms = [s.split('.')[-1] for s in symbols]
    # 每标的 log 文件
    for sym in symbols:
        code = sym.split('.')[-1]
        fname = f'monitor_log_{code}_{TODAY}_{MODE}.csv'
        logs[sym] = os.path.join(TMP, fname)
    os.makedirs(TMP, exist_ok=True)

    print(f'=== WebSocket 每秒采样 {symbols} duration={duration}s 开始 {time.strftime("%H:%M:%S")} ===')
    for t in targets:
        print(f'    {t[0]}: 阻力up={t[1]} 支撑dn={t[2]} | log={logs[t[0]]}')

    # 每标的：本段记录 + 段高/段低
    seg.update({sym: {'points': [], 'high': None, 'low': None, 'last': None} for sym in symbols})
    global broke
    broke = []

    def on_quote(frame):
        sym = 'HK.' + frame.symbol
        if sym not in seg:
            return
        last = getattr(frame, 'latestPrice', None)
        bid = getattr(frame, 'bidPrice', None)
        ask = getattr(frame, 'askPrice', None)
        high = getattr(frame, 'highPrice', None)
        low = getattr(frame, 'lowPrice', None)
        ts = time.strftime('%H:%M:%S')
        d = seg[sym]
        d['points'].append((ts, last, bid, ask))
        d['last'] = last
        if last is not None:
            d['high'] = last if d['high'] is None else max(d['high'], last)
            d['low'] = last if d['low'] is None else min(d['low'], last)
        # 写 log（append 行）
        with open(logs[sym], 'a') as f:
            f.write(f'{ts},{sym},{last},{bid},{ask},,,{high},{low},,\n')

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
        # 破位检测
        up = dn = None
        for t in targets:
            if t[0] == sym:
                up, dn = t[1], t[2]
        if up is not None and d['high'] is not None and d['high'] >= up:
            broke.append((sym, f'↑破阻力{up} 段高{d["high"]}'))
        if dn is not None and d['low'] is not None and d['low'] <= dn:
            broke.append((sym, f'↓破支撑{dn} 段低{d["low"]}'))
    if broke:
        print(f'!!! 破关键位: {broke} !!!')


if __name__ == '__main__':
    main()
