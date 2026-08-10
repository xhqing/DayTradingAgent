#!/usr/bin/env python3
"""富途 OpenD 逐笔推送每秒采样盯盘段（2026-08-07 立）。

为什么：老虎 PushClient 单账户单连接、新连接踢旧连接（code 4001「kick out by a new
connection」）——2026-08-07 盘中实测：auto 会话占着老虎连接跑 ws_segment 时，
本会话（signal）的 ws_segment 段反复被踢、采样中断（40 秒段只采到 1-2 个点）。
老虎没有「多客户端共用连接」的机制。富途 OpenD 是本地网关（127.0.0.1:11111）、
多客户端可同时连接同一网关互不踢断，逐笔 TICKER 推送毫秒级、每秒多条，
完全满足「每秒一个价格」的密采样要求——作为 ws_segment 的替代采样源，
彻底绕开「两会话抢老虎连接」的冲突。

用法：
  python3 scripts/futu_ws_segment.py <duration> <targets>
    时长固定 40 秒（防夹回逻辑同 ws_segment/monitor_segment）
    targets = SYM:up:dn 逗号分隔（SYM 带 HK. 前缀，如 HK.00100:347:330）
  例：python3 scripts/futu_ws_segment.py 40 HK.00100:347:330,HK.07709:29.4:27.8

行为：
  ① 富途 subscribe TICKER（逐笔推送），按秒聚合每秒一个价格，
     记到 tmp/monitor_log_{SYM}_{date}_{mode}.csv（命名对齐 ws_segment，
     供 monitor_guard 守卫识别 + 复盘读取）
  ② 段结束输出：每标的 按秒点列（时间+价）、段高/低、破关键位告警
  ③ log 行格式：HH:MM:SS,CODE,last,bid,ask,(买卖比空),(量比空),high,low,(额空),(止损空)
     ——bid/ask/买卖比/量比/额/止损 段末由 AI 调 snapshot 补齐
     （TICKER 逐笔只含成交价；high/low 列写段内累计逐笔高低）

连接：OpenQuoteContext('127.0.0.1', 11111)。富途 OpenD 是本地网关，
多客户端可并存连接（与老虎单连接互踢的本质区别）。订阅权限：港股 TICKER
属 Level1 免费行情，OpenD 默认可用；订阅失败（ret != 0）即退出报错。
"""
import os
import sys
import time
import signal
from datetime import date

from futu import OpenQuoteContext, SubType, TickerHandlerBase, RET_OK

MODE = os.environ.get('MODE', 'signal')
TODAY = date.today().strftime('%Y%m%d')
# scripts/ → trade → skills → .claude → 项目根（上四级）
TMP = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', '..', '..', 'tmp')
TMP = os.path.abspath(TMP)

# 模块级状态（finish 由硬超时回调调用，需访问）
seg = {}
targets = []
logs = {}
ctx = None


def parse_targets(argv):
    """解析 HK.00100:347:330,HK.07709:29.6:27.8 格式 → [(symbol, up, dn)]。
    targets 可能作为一个逗号分隔参数传入，先按逗号拆开。"""
    out = []
    for arg in argv:
        for t in arg.split(','):
            parts = t.split(':')
            sym = parts[0]
            up = float(parts[1]) if len(parts) > 1 and parts[1] else None
            dn = float(parts[2]) if len(parts) > 2 and parts[2] else None
            out.append((sym, up, dn))
    return out


class TickerSink(TickerHandlerBase):
    """逐笔推送回调（独立子线程）：按秒聚合、维护段内高低。"""

    def on_recv_rsp(self, rsp_pb):
        ret_code, content = super().on_recv_rsp(rsp_pb)
        if ret_code != RET_OK:
            print(f'>>> Ticker 错误: {content}', flush=True)
            return ret_code, content
        for _, row in content.iterrows():
            code = row['code']
            if code not in seg:
                continue
            price = row['price']
            if price is None or price == 0:
                continue
            d = seg[code]
            d['last'] = price
            if d['high'] is None or price > d['high']:
                d['high'] = price
            if d['low'] is None or price < d['low']:
                d['low'] = price
            # 按秒聚合（每秒保留最后一条价），time 字段转 HH:MM:SS
            # （futu Ticker 的 time 是字符串 'YYYY-MM-DD HH:MM:SS.mmm'，直接切片）
            t = row['time']
            if isinstance(t, str):
                t = t[11:19] if len(t) >= 19 else t
            else:
                try:
                    t = t.strftime('%H:%M:%S')
                except AttributeError:
                    t = str(t)
            d['sec'][t] = price
        return ret_code, content


def main():
    if len(sys.argv) < 3:
        print('用法: futu_ws_segment.py <duration> HK.00100:347:330,HK.07709:29.4:27.8')
        sys.exit(1)
    try:
        duration = int(sys.argv[1])
    except ValueError:
        duration = 40
    duration = max(1, min(duration, 40))  # 不放大段长（同 ws_segment 防夹回）

    global targets, ctx
    targets = parse_targets(sys.argv[2:])
    symbols = [t[0] for t in targets]
    # 每标的 log 文件
    for sym in symbols:
        code = sym.replace('.', '_')  # HK.00100 -> HK_00100（对齐 monitor_segment 命名，monitor_summary/monitor_guard 才能识别）
        fname = f'monitor_log_{code}_{TODAY}_{MODE}.csv'
        logs[sym] = os.path.join(TMP, fname)
    os.makedirs(TMP, exist_ok=True)

    print(f'=== 富途逐笔每秒采样 {symbols} duration={duration}s 开始 {time.strftime("%H:%M:%S")} ===')
    for t in targets:
        print(f'    {t[0]}: 阻力up={t[1]} 支撑dn={t[2]} | log={logs[t[0]]}')

    seg.update({sym: {'sec': {}, 'high': None, 'low': None, 'last': None} for sym in symbols})
    global broke
    broke = []

    def hard_timeout(signum, frame):
        finish()
        if ctx is not None:
            try:
                ctx.close()
            except Exception:
                pass
        sys.exit(0)

    signal.signal(signal.SIGALRM, hard_timeout)
    signal.alarm(duration + 15)

    ctx = OpenQuoteContext('127.0.0.1', 11111)
    ctx.set_handler(TickerSink())
    ret, err = ctx.subscribe(symbols, [SubType.TICKER] * len(symbols))
    if ret != RET_OK:
        print(f'>>> 订阅失败: {err}（检查 OpenD 行情权限）')
        ctx.close()
        sys.exit(2)
    print(f'>>> 已订阅 TICKER {symbols}，每秒采样 {duration}s…', flush=True)

    # 主循环：每秒写各标的 log（取该秒最后一条逐笔价）
    t0 = time.time()
    last_sec = None
    while time.time() - t0 < duration:
        cur_sec = time.strftime('%H:%M:%S')
        if cur_sec != last_sec:
            last_sec = cur_sec
            for sym, d in seg.items():
                if d['last'] is None:
                    continue
                # ts,code,last,bid,ask,买卖比空,量比空,high,low,额空,止损空
                with open(logs[sym], 'a') as f:
                    f.write(f'{cur_sec},{sym},{d["last"]},,,,{d["high"]},{d["low"]},,\n')
        time.sleep(0.2)

    try:
        ctx.unsubscribe(symbols, [SubType.TICKER] * len(symbols))
    except Exception:
        pass
    ctx.close()
    finish()


def finish():
    """段结束统计输出（由 main 的 timeout 或正常流程调用）。"""
    print('\n📊 富途逐笔每秒采样段结束统计（按秒点列 + 段高低 + 破位）:')
    for sym, d in seg.items():
        secs = sorted(d['sec'].items())
        if not secs:
            print(f'  {sym}: 无推送')
            continue
        print(f'  {sym}: 秒点={len(secs)} 段高={d["high"]} 段低={d["low"]} 末={d["last"]}')
        recent = secs[-12:]
        line = '  '.join(f'{t}:{v}' for t, v in recent)
        print(f'    最近{len(recent)}点: {line}')
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
