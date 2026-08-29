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

2026-08-17 补分析锚点（红灯「盯盘空转」修法①，对齐 ws_segment / monitor_segment）：
段结束输出补三样——📊 VWAP 检查（复用本脚本的 OpenQuoteContext 查 avg_price）、
⏰ 每 10 分钟方向重估提醒（marker 文件去重）、👉 空转防护提示行。为什么：段输出
只剩裸数字时，分析链失去「下一步该分析」的锚点、会退化为纯重启采样（2026-08-17
空转实录）。

2026-08-18 修订空转防护提示行的动作顺序（用户立）：拿到段结束数据立刻分析（期间
不做任何别的事、不先启下一段）→ 有机会当场执行（下单不排队）→ 没机会才写分析
心跳 + 重启下一段。堵「先启下一段再回来分析」把最新数据放旧的路径。
"""
import os
import sys
import time
import signal
import csv
from datetime import date, datetime

from futu import OpenQuoteContext, SubType, TickerHandlerBase, RET_OK

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from trade_utils_tiger import parse_mode  # --mode / 环境变量统一解析（对齐 ws_segment / monitor_segment，2026-08-21）

# mode 解析（2026-08-21 修）：原只读环境变量 MODE——2026-08-16 三采样脚本统一 parse_mode 时
# 本脚本被漏掉，导致 ① 不认 --mode auto 参数（ws_segment / monitor_segment 都认）；② 两个 auto
# 会话（2026-08-20 港股实录）启动段时没带 MODE= 前缀，log 全部落成 _signal 后缀、reassess
# marker 同样错档（resume 按 mode 查 log 断层也读不到）。现统一 parse_mode：--mode 优先、
# 环境变量 MODE 兜底（parse_mode 内已补环境变量分支）、默认 signal，与另两个脚本同口径。
MODE = parse_mode()
TODAY = date.today().strftime('%Y%m%d')
# scripts/ → trade → skills → .claude → 项目根（上四级）
TMP = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', '..', '..', 'tmp')
TMP = os.path.abspath(TMP)

# 订阅时刻（epoch 秒）：TickerSink 只统计订阅时刻之后发生的逐笔（2026-08-19 立）。
# 背景：富途 subscribe TICKER 成功瞬间会推一批**历史缓冲逐笔**（含开盘以来成交）——当日
# 实录：小米段低被污染成 26.0（日低实为 26.38）、段 9 段低 27.06（last 序列最低 27.30）、
# 腾讯段 11 段低 435.4（秒闪收回），「↓破支撑」告警被缓冲噪声误触发 3 次、AI 每次需人工
# 核 last 序列排除。修法：逐笔行自带的 time 字段（交易所逐笔时间）早于订阅时刻 → 丢弃
# （不进 high/low/last/sec 统计）；订阅后正常逐笔的交易所时间与墙钟几乎同步（毫秒级
# 推送），不受影响。取「subscribe 返回成功后」的时间戳（缓冲批在 subscribe 调用返回前
# 已推完，此刻墙钟 = 订阅边界）。
SUB_EPOCH = None   # main() 里 subscribe 成功后设置


def _row_epoch(t_val):
    """TICKER 行 time 字段（'YYYY-MM-DD HH:MM:SS.mmm' 或 datetime）→ epoch 秒。失败返回 None。

    时区口径：富逐 TICKER 的 time 是**本地墙钟**字符串（当日实录 log 采样时刻与
    time.strftime('%H:%M:%S') 墙钟一致）——用 time.mktime 按本地时区解析、与
    time.time()（SUB_EPOCH）同系可比。勿用 calendar.timegm（按 UTC 解析、差 8 小时）。
    """
    try:
        if isinstance(t_val, str):
            dt = datetime.strptime(t_val[:19], '%Y-%m-%d %H:%M:%S')
        else:
            dt = t_val.replace(microsecond=0)
        if dt.tzinfo is not None:
            return dt.timestamp()
        return time.mktime(dt.timetuple())
    except Exception:
        return None


# 模块级状态（finish 由硬超时回调调用，需访问）
seg = {}
targets = []
logs = {}
ctx = None


STRIP_FLAGS = ('--mode', '--account')

def parse_targets(argv):
    """解析 HK.00100:347:330,HK.07709:29.6:27.8 格式 → [(symbol, up, dn)]。
    targets 可能作为一个逗号分隔参数传入，先按逗号拆开。
    --mode / --account 参数先剥掉再解析（--mode 2026-08-21 对齐 ws_segment；--account
    2026-08-25 补——实盘盯盘传 '--account live' 时 'live' 被当标的、订阅报
    'format of code live is wrong' 且 log 落成 monitor_log_live_*.csv）。
    带值标志的值 token（跟在标志后的下一个 argv）一并剥除。"""
    argv = [a for i, a in enumerate(argv)
            if not (any(a == f or a.startswith(f + '=') for f in STRIP_FLAGS)
                    or (i > 0 and argv[i - 1] in STRIP_FLAGS))]
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
            # 订阅缓冲过滤（2026-08-19 立）：丢弃订阅时刻之前发生的逐笔（历史缓冲批，
            # 见 SUB_EPOCH 注释）——避免 high/low 被开盘以来极值污染、破位告警误触发。
            # time 解析失败（None）按「无法判定」保守丢弃：段统计宁可少一个点、不可收
            # 一个来历不明的极值。
            row_t = row['time']
            row_epoch = _row_epoch(row_t)
            if row_epoch is None or (SUB_EPOCH is not None and row_epoch < SUB_EPOCH):
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

    # 写表头（对齐 monitor_segment 列名，monitor_summary 的 csv.DictReader 才能解析；
    # 2026-08-19 修：此前不写表头，DictReader 把首条数据行当列名，monitor_summary 报 KeyError 'last'）
    for sym, path in logs.items():
        if not os.path.exists(path):
            with open(path, 'a') as f:
                f.write('time,code,last,bid,ask,ratio,vr,high,low,turnover_yi,stop_price\n')

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

    global SUB_EPOCH
    ctx = OpenQuoteContext('127.0.0.1', 11111)
    ctx.set_handler(TickerSink())
    # SUB_EPOCH 在 subscribe **前**取：订阅成功瞬间回调线程可能已开始收缓冲批，
    # 若在 subscribe 返回后才取、回调线程可能已用旧 SUB_EPOCH（None）放行缓冲——
    # 取「发起订阅前」的墙钟作边界，首 1-2 秒真实逐笔误杀（量级毫秒级推送、可忽略）
    # 远好于缓冲极值污染段统计。
    SUB_EPOCH = time.time()
    ret, err = ctx.subscribe(symbols, [SubType.TICKER] * len(symbols))
    if ret != RET_OK:
        print(f'>>> 订阅失败: {err}（检查 OpenD 行情权限）')
        ctx.close()
        sys.exit(2)
    print(f'>>> 已订阅 TICKER {symbols}，每秒采样 {duration}s…（订阅前逐笔缓冲已过滤）', flush=True)

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
                # ts,code,last,bid,ask,买卖比空,量比空,high,low,额空,止损空（共 11 列）
                # 2026-08-16 修复：last 后少写一个逗号（4 个空位 bid/ask/ratio/vr 只写出 3 个），
                # 10 字段入 11 列表头致 high 落进 vr 列、low 落进 high 列、low 与 stop_price 整列丢失，
                # 下游 monitor_summary 的日高/日低/量比数据全错（08-14 当天复盘数据已被污染）。
                with open(logs[sym], 'a') as f:
                    f.write(f'{cur_sec},{sym},{d["last"]},,,,,{d["high"]},{d["low"]},,\n')
        time.sleep(0.2)

    try:
        ctx.unsubscribe(symbols, [SubType.TICKER] * len(symbols))
    except Exception:
        pass
    ctx.close()
    finish()
    return


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

    # 📌 持仓状态工具强制（2026-08-18 立）：持仓期间段输出自动带出账户持仓 + 活动止损单
    # 最新触发价 + 状态比对告警——AI 开仓后「查账户」从记忆责任变成段输出自带（不凭记忆、
    # 用户 App 手动移损也能当场看到）；空仓时返回 None 静默（遵循「空仓不查」规则，零 API）。
    try:
        from account_status import position_status
        _ps = position_status()
        if _ps:
            print(_ps, flush=True)
    except Exception as _e:
        print(f'[持仓状态检查 err:{_e}]', flush=True)

    # ⏰ 每 10 分钟方向重估提醒（2026-08-17 补，逻辑对齐 monitor_segment / ws_segment）。
    try:
        syms = [t[0] for t in targets]
        marker_file = os.path.join(TMP, f'reassess_marker_{TODAY}_{MODE}.txt')
        reminded = set()
        if os.path.exists(marker_file):
            with open(marker_file) as mf:
                reminded = {int(x) for x in mf.read().split() if x.strip().isdigit()}
        elapsed_min = None
        first_log = logs[syms[0]]
        if os.path.exists(first_log):
            with open(first_log) as lf:
                rdr = csv.reader(lf)
                next(rdr, None)  # 跳表头
                first_row = next(rdr, None)
            if first_row:
                first_dt = datetime.strptime(f'{TODAY} {first_row[0]}', '%Y%m%d %H:%M:%S')
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
                    f'⑤持续单向运动≥1h）+ 自问「方向/趋势/行情是否仍与开盘一致」，不固守开盘判断。另：重读 SKILL.md「硬性护栏」全 8 条刷新记忆（上下文压缩后规则细节会衰减，每 10 分钟强制重读一次，2026-08-18 用户立）。',
                    flush=True,
                )
    except Exception as e:
        print(f'[重估提醒检查 err:{e}]', flush=True)

    # ⏰ 临近停盯边界的开仓资格提醒（2026-08-18 立，对齐 monitor_segment）：距停盯 ≤60 分钟
    # 每段打印剩余分钟 + 「>5 分钟开仓资格仍在、禁止自设截止线」；≤5 分钟打印绝对不开仓窗口。
    try:
        from trade_utils_tiger import minutes_to_session_end, OPEN_WINDOW_MIN
        _syms = [t[0] for t in targets]
        for _mkt in ('HK', 'US'):
            if any(s.startswith(f'{_mkt}.') for s in _syms):
                _mins = minutes_to_session_end(_mkt)
                if 0 < _mins <= 60:
                    if _mins > OPEN_WINDOW_MIN:
                        print(
                            f'⏰ 距{_mkt}停盯边界 {_mins:.0f} 分钟（>5）：开仓资格仍在——按压缩止盈'
                            f'实算净赔率 ≥1.8 照常评估，禁止自设「临近收盘/午休不开仓」截止线'
                            f'（2026-08-18 用户立；下单脚本另有 ≤5 分钟时间闸硬拦）。'
                            f'⛔ 同时：空仓 / 无信号 ≠ 停盯理由——盯到用户喊停或收盘（取先到），'
                            f'停盯收尾脚本已受 stop_gate 时间闸硬拦（2026-08-24 立，T118）。',
                            flush=True,
                        )
                    else:
                        print(
                            f'⏰ 距{_mkt}停盯边界 {_mins:.0f} 分钟（≤5）：绝对不开仓窗口，'
                            f'持仓按停盯流程处理、空仓只盯到停盯。',
                            flush=True,
                        )
                break
    except Exception as e:
        print(f'[停盯边界提醒 err:{e}]', flush=True)

    # 📊 VWAP 检查（2026-08-17 补，锚点对齐 monitor_segment / ws_segment）：逐笔推送只有
    # 成交价、无 VWAP，段结束用富途 OpenD 快照补查 avg_price（本脚本本就走 OpenD 网关）。
    vwap_map = {}  # {sym: vwap}——供下方赶顶检测复用（快照只查一次）
    try:
        vwap_lines = []
        try:
            _vctx = OpenQuoteContext('127.0.0.1', 11111)
            try:
                ret, df = _vctx.get_market_snapshot([t[0] for t in targets])
                if ret != 0 or df is None or len(df) == 0 or 'avg_price' not in df.columns:
                    raise RuntimeError(f'snapshot ret={ret}')
                syms = [t[0] for t in targets]
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
                    vwap_map[sym] = vwap
                    diff = cur - vwap
                    who = '上方（多头占优）' if diff > 0 else ('下方（空头占优）' if diff < 0 else '持平')
                    vwap_lines.append(f'  {sym}: 现价 {cur:.2f} | VWAP {vwap:.2f} | {who} {diff:+.2f}')
            finally:
                _vctx.close()
        except Exception as e:
            vwap_lines.append(f'  OpenD 不可用（{e}）——AI 必须自行跑 snapshot/monitor_summary 补 VWAP，禁止跳过')
        print('📊 VWAP 检查（方向框架地面真相，段结束必看）:\n' + '\n'.join(vwap_lines), flush=True)
    except Exception as e:
        print(f'[VWAP 检查 err:{e}]', flush=True)

    # ⚡ 加速赶极端检测（2026-08-19 立，工具强制——「动能将竭主动平仓」的在场打印）：
    # 持仓标的段内急速赶顶/赶底（涨速 + 破前高 + 远离 VWAP 三条件）→ 高亮警报逼 AI
    # 当段显式决断（平 / 不平 + 理由），杜绝 2026-08-19 小米 10:31/11:12 两次赶顶不作为。
    try:
        from account_status import blast_check
        # 显式传本段秒点序列（seg 风格 dict，blast_check 内识别 {'sec': {...}}），
        # 不依赖回落读全局——调用链清晰、不受调用栈影响。
        _blasts = blast_check(vwap_map, prices_by_sym=dict(seg))
        if _blasts:
            print('\n'.join(_blasts), flush=True)
    except Exception as _e:
        print(f'[赶顶检测 err:{_e}]', flush=True)

    # 👉 空转防护提示行（2026-08-17 补，对齐 ws_segment；2026-08-18 修订顺序）：
    # 立刻分析 → 有机会当场下单 → 没机会才写心跳 + 重启下一段。
    print(
        '👉 空转防护：拿到本段数据立刻分析（期间不做任何别的事，不先启下一段）；分析后有机会当场执行'
        '（下单不排队），没机会才写分析心跳（echo 追加 tmp/analysis_beat_日期_模式.csv）+ 重启下一段采样。',
        flush=True,
    )


if __name__ == '__main__':
    main()
