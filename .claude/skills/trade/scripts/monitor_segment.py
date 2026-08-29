#!/usr/bin/env python3
# 多标的批量分段采样（2026-07-22 改造：一个进程同时采多只 + 关键位参数化）。
#
# 为什么一个进程采多只：富途 get_market_snapshot 接受标的列表、一次调用批量返回多只快照，
# 一个进程一轮循环里采完所有标的，比每只票各开一个 OpenQuoteContext 连接 / 各起一个进程
# 更省资源、更快（连接与进程开销远大于多几次毫秒级 API 调用）。多标的并行盯盘不该靠多进程，
# 而是靠单进程批量采样——这是本脚本的设计出发点。
#
# 为什么关键位参数化：原版 UP_BREAK/DN_BREAK 写死成澜起 06809 的 330/306，换标的就失配、
# 更没法一只进程同时给多只票各设各的关键位。现在每个标的的关键位（阻力 up / 支撑 dn）
# 由 AI 盯盘启动做方向研判时定好、随命令行传入——谁的 high 创本段新高且 >= 它自己的 up 阻力位，
# 或 low 创新低且 <= 它自己的 dn 支撑位，立即打标记 + 整段提前退出通知 AI
# （任一只破位都叫 AI 来看，突破响应延迟从段时长压到约一个采样间隔）。
#
# 每只票各写各的连续 log（tmp/monitor_log_{symbol}_{date}_{mode}.csv，按标的 + 模式分文件、累积不丢），
# monitor_summary.py 按标的 + 模式读——多标的不混在同一个 CSV 里、signal/auto 两会话也不混（2026-08-04 立）。
#
# 用法：
#   python3 monitor_segment.py <targets> <duration_sec> [interval_sec]
#     targets      标的列表，逗号分隔；每项格式 SYM[:up[:dn]]，冒号后是关键位（阻力 up / 支撑 dn）
#                  HK.00981:330:306,US.MU:950:890     两只、各带关键位
#                  HK.00981:330:306,HK.06809          第二只不检测突破（只采样）
#                  HK.00981:330                       只检测向上破阻力
#                  HK.00981                           单只、不带关键位（只采样，向后兼容旧用法）
#     duration_sec 本段采样时长（秒），到点退出触发通知；建议 40
#     interval_sec 采样间隔（秒），默认 10
#
# log 文件：每只票 tmp/monitor_log_{SYM}_{YYYYMMDD}_{mode}.csv（mode = signal/auto，两会话并行盯盘各写各的、不污染），CSV 列：
#   time,symbol,last,bid,ask,ratio,vr,high,low,turnover_yi
# AI 分析时读各标的 log 最近 N 行（如 tail -60 tmp/monitor_log_<SYM>_*.csv）。

import csv
import json
import os
import sys
import time
from datetime import datetime, timedelta

from futu import OpenQuoteContext

from trade_utils_tiger import parse_mode  # 模式标识：运行时 log 按 mode 分文件，signal/auto 两会话并行盯盘不互相污染

# 老虎止损单查询（auto 模式：每次采样获取最新止损单止损价）
_TIGER_AVAILABLE = False
try:
    from trade_utils_tiger import load_config, get_today_orders_tiger
    _TIGER_AVAILABLE = True
except ImportError:
    pass

# 多会话单持仓互斥·第四层兜底（2026-08-17 立，方案 A）：与止损价查询同一次 get_orders
# 顺带检测白名单外持仓数——脚本可控路径外的漏网（用户手机 App 手动下单等）从一个
# 采样段内（≤40 秒 + 段间）被发现并告警，而不是静默双持仓、敞口翻倍直到收盘。
try:
    from trade_mutex import today_open_exposure, resident_positions
    _MUTEX_AVAILABLE = True
except ImportError:
    _MUTEX_AVAILABLE = False

_today_orders_cache = None   # query_stop_prices 每轮写入，第四层检测复用（同一次查单）


def check_position_discipline(orders):
    """第四层兜底检测：白名单外在场敞口 ≥2 → 告警（返回告警字符串或 None）。

    口径与开仓闸门同源（today_open_exposure：当日订单流「开仓成交 − 平仓成交」>0 的
    标的集合），常驻历史持仓（config resident_positions）不经当日订单流、天然不在
    集合里。集合 ≥2 = 双持仓违规（漏网路径：用户 App 手动开仓、闸门崩溃窗口残留等）；
    =1 正常（本系统当日一笔在场）。检测到时 AI 处置：立即查两笔持仓、平掉较新的那笔
    （后开的仓位是漏网者；平仓脚本走 symbol 级隔离不会误伤先开仓位）。
    """
    if not _MUTEX_AVAILABLE or orders is None:
        return None
    try:
        exposure = today_open_exposure(orders)
    except Exception:
        return None
    rp = resident_positions()
    violators = sorted(s for s in exposure if s not in rp)
    if len(violators) >= 2:
        return violators
    return None


def parse_targets(raw):
    """解析 'SYM[:up[:dn]][,SYM...]' 成 [{sym, up, dn}, ...]。

    up / dn 缺省为 None = 不检测该方向突破（只采样）。这样不带冒号的裸标的、
    只写 up 不写 dn 等情况都能正常解析，向后兼容旧的单标的用法。
    """
    targets = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        parts = item.split(":")
        sym = parts[0].strip()
        if not sym:
            continue
        up = float(parts[1]) if len(parts) > 1 and parts[1].strip() else None
        dn = float(parts[2]) if len(parts) > 2 and parts[2].strip() else None
        targets.append({"sym": sym, "up": up, "dn": dn})
    return targets


def query_stop_prices(symbols):
    """查询老虎当日订单，提取各标的最新的**活动**止损条件单触发价（STP 单 aux_price）。

    返回 {symbol: stop_price} 字典。查不到或出错返回空字典。
    用户可能在券商 App 里手动添加止损单，所以每次采样都要查、不凭记忆。

    为什么放在采样脚本里：止损价是盯盘决策的关键输入（判断持仓止损触发、
    移损后新的止损位），必须与行情数据同步获取，不能事后单独查。

    ⚠️ 2026-08-11 修复：此前不过滤订单状态、匹配到第一个订单即 break，会取到
    已作废批次（开仓前 Invalid 的测试单 STP @846 EXPIRED）的触发价、全天显示旧止损
    （LITE 实测 846 恒不更新、与实际移损 840→836 脱节）。现在只认活动状态
    （排除 Filled/Cancelled/Expired/Inactive/Invalid 等已结束订单）的 STP 单；
    同标的多个活动止损单时取 order_id 最大的（提交最晚、最新）那个。
    """
    if not _TIGER_AVAILABLE:
        return {}
    try:
        config = load_config()
        orders = get_today_orders_tiger(config)
        global _today_orders_cache   # 第四层持仓检测复用同一次查单（不再多打一次 API）
        _today_orders_cache = orders
        result = {}
        for order in orders:
            # 老虎订单对象：contract.symbol（老虎格式，港股 5 位裸数字 / 美股裸代码）、
            # order_type、aux_price（STP 止损触发价）、status
            contract = getattr(order, "contract", None)
            sym = getattr(contract, "symbol", None) if contract else None
            if sym is None:
                continue
            for s in symbols:
                # 匹配：订单返回 "02800" / "MU"，对比 symbols 列表中的 "HK.02800" / "US.MU"
                if sym != s and sym != s.split(".")[-1]:
                    continue
                # 只认止损条件单（STP；开仓附加止损腿在订单列表里同为独立 STP 单）
                ot = getattr(order, "order_type", None)
                if ot is None or "STP" not in str(ot):
                    break
                # 只认活动状态：排除已成交 / 已撤 / 已过期 / 无效 / 停用等已结束订单
                st_obj = getattr(order, "status", None)
                st = st_obj.value if hasattr(st_obj, "value") else str(st_obj)
                if any(k in st for k in ("Filled", "Cancelled", "Expired", "Inactive", "Invalid")):
                    break
                aux = getattr(order, "aux_price", None)
                if not aux or float(aux) <= 0:
                    break
                # 同标的多个活动止损单：取 order_id 最大的（提交最晚 = 最新）
                oid = int(getattr(order, "id", 0) or 0)
                cur = result.get(s)
                if cur is None or oid > cur[1]:
                    result[s] = (float(aux), oid)
                break
        return {s: v[0] for s, v in result.items()}
    except Exception:
        return {}


def main():
    if len(sys.argv) < 2:
        print(
            "用法：python3 monitor_segment.py <targets> <duration_sec> [interval_sec]\n"
            "  targets 格式 SYM[:up[:dn]][,SYM[:up[:dn]]...]\n"
            "  例：HK.00981:330:306,US.MU:950:890  （两只、各带关键位）\n"
            "      HK.00981:330:306,HK.06809        （第二只只采样不检测突破）\n"
            "      HK.00981                         （单只、向后兼容）",
            flush=True,
        )
        sys.exit(1)

    targets = parse_targets(sys.argv[1])
    if not targets:
        print(f"未解析出任何标的：{sys.argv[1]}", flush=True)
        sys.exit(1)

    DURATION = int(sys.argv[2]) if len(sys.argv) > 2 else 40
    # 强制 40 以内密采样（2026-08-03 用户立）：盯盘期间曾擅自降频 120s/300s 违反「不因市况降频」。
    # 工具固化——传入 >40 自动夹到 40 + 警告，AI 即便误传降频也被挡回 40。
    # 市场无机会应换标的（hot_list 找活跃）或继续密盯，不降频（与段结束自带完整统计同理：工具强制不依赖记忆）。
    if DURATION > 40:
        print(
            f"⚠️ DURATION={DURATION} 超过 40，违反「40 以内密采样 + 不因市况降频」规定，强制夹到 40。"
            f"市场无机会应换标的（hot_list）或继续密盯，不降频。",
            flush=True,
        )
        DURATION = 40
    INTERVAL = int(sys.argv[3]) if len(sys.argv) > 3 else 10
    syms = [t["sym"] for t in targets]
    mode = parse_mode()  # signal（默认）/ auto —— 运行时 log 按 mode 分文件，两会话并行盯盘不互相污染

    # 每只票的连续 log 路径 + 突破检测状态。项目根 = 脚本目录(scripts)上四级。
    _SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
    _PROJECT_ROOT = os.path.abspath(os.path.join(_SCRIPT_DIR, "..", "..", "..", ".."))
    LOG_DIR = os.path.join(_PROJECT_ROOT, "tmp")
    # log 文件按市场对应交易日命名（2026-08-04 修 bug）：原统一用「北京 -12h」算美东交易日（为美股跨北京午夜），
    # 但对港股早盘失效——港股盘中北京 09:30-12:00，减 12h = 昨天 21:30 → log 落昨天日期，今天采样污染昨天 log、
    # 完整采样统计混入昨天数据（如开=123.60 是昨天的价）+ 哨兵读昨天末行误报断层（1069 分钟）。改为按市场区分：
    # 港股 HK 用北京日期（盘中不跨午夜）；美股 US 用美东交易日。
    # 2026-08-17 修：美股日期改 zoneinfo 直接转美东时区取（与 preflight 同法）——原「北京 -12h」
    # 是夏令时硬编码，11 月切冬令时（EST=UTC-5 需 -13h）会取错日、log 写错文件。
    def trading_date_str(sym: str) -> str:
        now = datetime.now()
        if sym.startswith("US."):
            try:
                from zoneinfo import ZoneInfo
                return now.astimezone(ZoneInfo("America/New_York")).strftime("%Y%m%d")  # 美东交易日（自动适配 DST）
            except Exception:
                return (now - timedelta(hours=12)).strftime("%Y%m%d")  # zoneinfo 不可用：夏令时估兜底
        return now.strftime("%Y%m%d")  # 港股及其它：北京日期
    # 重估提醒检查（下方 marker 文件命名 + 首点时间拼算）需 date_str 与主标的 log 文件日期一致，
    # 故取主标的市场对应的交易日（港股=北京日期、美股=美东交易日）。
    date_str = trading_date_str(syms[0]) if syms else datetime.now().strftime("%Y%m%d")
    LOG_FIELDS = ["time", "symbol", "last", "bid", "ask", "ratio", "vr", "high", "low", "turnover_yi", "stop_price"]

    # state[sym] = 该标的的 log 路径 + 上一轮 high/low（用于判创新高/新低）+ 它自己的关键位
    state = {}
    for t in targets:
        sym = t["sym"]
        log_file = os.path.join(LOG_DIR, f"monitor_log_{sym.replace('.', '_')}_{trading_date_str(sym)}_{mode}.csv")
        state[sym] = {"log_file": log_file, "last_high": None, "last_low": None, "up": t["up"], "dn": t["dn"]}

    def append_log(log_file, row):
        os.makedirs(LOG_DIR, exist_ok=True)
        write_header = not os.path.exists(log_file)
        with open(log_file, "a", newline="") as f:
            w = csv.writer(f)
            if write_header:
                w.writerow(LOG_FIELDS)
            w.writerow(row)

    # 时间断层哨兵（2026-07-28 立）：启动时读各标的 log 最后采样时间，距现在 ≥ 5 分钟
    # （正常段间循环 < 1 分钟）= 断网/暂停/故障致断层——警告 AI 先跑恢复协议再继续，别用过时
    # 数据发信号。2026-07-28 中芯事故根因：断层期未察觉、用过时参考价发信号（13:45 数据 →
    # 15:34 才响铃）。哨兵让 AI 即便没主动意识到断层，脚本也强制提醒。
    # 港股午休感知（2026-08-16 立）：港股 12:00-13:00 休市、正常不采样，每个交易日 13:00
    # 重启首段时 gap ≈ 61 分钟必报「疑似断层」——狼来了效应削弱真警告。午休窗口内的 gap
    # 不按断层报、降级为提示。
    try:
        for sym in syms:
            log_file = state[sym]["log_file"]
            if not os.path.exists(log_file):
                continue
            with open(log_file) as lf:
                rows = list(csv.reader(lf))
            if len(rows) <= 1:
                continue
            last_t = rows[-1][0]  # "HH:MM:SS"（log 只记时分秒，日期靠推断）
            now = datetime.now()
            last_time = datetime.strptime(last_t, "%H:%M:%S").time()
            # 推断 last 的日期（跨午夜安全 2026-08-04 立）：默认今天，若 last 时分 > now 时分
            # （如 last 23:59 now 00:01，跨午夜）则 last 是昨天。避免用 date_str 拼接导致跨午夜 gap 误算
            # （美东盘中跨午夜后 date_str 是昨天美东交易日、但 last_t 是今天时分，拼接错算 24h gap 误报）。
            last_date = now.date() - timedelta(days=1) if last_time > now.time() else now.date()
            last_dt = datetime.combine(last_date, last_time)
            gap_min = (now - last_dt).total_seconds() / 60
            if gap_min >= 5:
                # 港股午休感知：last 在 11:55-12:00、now 在 12:55-13:05 附近 = 正常午休 gap，
                # 降级提示不按断层报警（每天午后首段必现，误报削弱真警告的狼来了效应）
                t_noon_end = now.replace(hour=13, minute=5, second=0, microsecond=0)
                t_noon_start = now.replace(hour=11, minute=50, second=0, microsecond=0)
                if (not sym.startswith("US.")) and now.weekday() < 5 and \
                   t_noon_start <= last_dt <= now.replace(hour=12, minute=0, second=0) and \
                   now.time() <= t_noon_end.time():
                    print(
                        f"ℹ️ 午休间隔：{sym} 上次采样 {last_t} → 现在 {now:%H:%M}（港股午休 12:00-13:00 "
                        f"正常不采样），非断层。",
                        flush=True,
                    )
                    continue
                print(
                    f"⚠️ 时间断层哨兵：{sym} 距上次采样 {gap_min:.0f} 分钟（上次 {last_t} → 现在），"
                    f"疑似断网/暂停/故障致断层！先跑 `python3 scripts/resume.py` 重建上下文、"
                    f"刷新现价，禁止直接用断层前旧数据发信号（发信号前必过「发信号硬前置」："
                    f"距上次 date/snapshot >2min 必须刷新）。",
                    flush=True,
                )
    except Exception as e:
        print(f"[时间断层哨兵 err:{e}]", flush=True)

    ctx = OpenQuoteContext(host="127.0.0.1", port=11111)
    start = time.time()
    print(
        f"=== 多标的分段采样 {syms} duration={DURATION}s interval={INTERVAL}s "
        f"开始 {datetime.now():%H:%M:%S} ===",
        flush=True,
    )
    for t in targets:
        sym = t["sym"]
        if t["up"] is None and t["dn"] is None:
            level_desc = "无关键位（只采样）"
        else:
            level_desc = f"阻力up={t['up']} 支撑dn={t['dn']}"
        print(f"    {sym}: {level_desc} | log={state[sym]['log_file']}", flush=True)

    broke = []  # 本段若提前退出，记录哪些标的破了什么位，供结尾汇总
    stop_prices = {}
    while time.time() - start < DURATION:
        try:
            # 每轮采样都查一次最新止损单（用户可能在 App 中途手动加止损，频率随采样间隔走）。
            # 2026-08-16 修复缓存只增不清：止损单撤销/成交后 fresh 不再含该标的，旧实现
            # stop_prices.update(fresh) 保留旧价 → 后续每轮持续显示已不存在的止损（误导
            # 「持仓有止损保护」判断，与「每轮现查、不凭记忆」设计意图相反）。现以 fresh
            # 为准整体替换——fresh 非空即 replace、fresh 为空（查询失败）才保留上一轮值。
            fresh = query_stop_prices(syms)
            if fresh:
                stop_prices = dict(fresh)

            # 第四层兜底（2026-08-17 立，方案 A）：同一次查单顺带检测白名单外双持仓。
            # 检测到立即告警（每轮都输出直到违规消除——不是只报一次，AI / 用户反复可见）。
            if _TIGER_AVAILABLE:
                try:
                    violators = check_position_discipline(_today_orders_cache)
                    if violators:
                        print(
                            f"🚨 单持仓违规：白名单外在场敞口 {violators}（当日开仓成交且无对应平仓 ≥2）——"
                            f"多会话互斥的脚本闸门拦不住的漏网路径（用户 App 手动下单等）。"
                            f"AI 处置：立即查两笔持仓、平掉较新的那笔（平仓脚本 symbol 级隔离不误伤先开仓位），"
                            f"恢复单持仓后本告警自动消除。",
                            flush=True,
                        )
                except Exception:
                    pass

            ret, df = ctx.get_market_snapshot(syms)
            if ret != 0 or df is None or len(df) == 0:
                print(f"[{datetime.now():%H:%M:%S}] snapshot 失败 ret={ret} {df}", flush=True)
                time.sleep(INTERVAL)
                continue
            # 富途一次返回多行、每行一只标的。优先按 code 列匹配；无 code 列则按请求顺序兜底
            # （富途返回行序通常与请求一致，兜底仅为防御列名差异）。
            if "code" in df.columns:
                rows = {r["code"]: r for _, r in df.iterrows()}
            else:
                rows = {syms[i]: df.iloc[i] for i in range(min(len(syms), len(df)))}

            ts = datetime.now().strftime("%H:%M:%S")
            for t in targets:
                sym = t["sym"]
                row = rows.get(sym)
                if row is None:
                    print(f"[{ts}] [{sym}] 无快照（snapshot 未返回该标的）", flush=True)
                    continue
                # 美股盘前（04:00-09:30 ET）快照的 last_price 停在昨收不动、盘前真实价在
                # pre_price / pre_high_price / pre_low_price（2026-08-19 实测：盘前 MU
                # last_price 恒 940.76 昨收、pre_price 947.87 实时跳）；盘中 pre_* 字段为
                # nan。取值逻辑：pre_price 非空且与昨收不同 → 用盘前字段，否则用常规字段。
                pre = row.get("pre_price")
                pre_ok = pre is not None and str(pre) != "nan" and not (
                    str(pre) == str(row.get("last_price")) and row.get("pre_volume") in (None, "nan", 0)
                )
                if pre_ok:
                    last = pre
                    high = row.get("pre_high_price")
                    low = row.get("pre_low_price")
                else:
                    last = row["last_price"]
                    high = row["high_price"]
                    low = row["low_price"]
                bid = row["bid_price"]
                ask = row["ask_price"]
                ratio = row.get("bid_ask_ratio") or 0
                vr = row.get("volume_ratio") or 0
                turnover = row.get("turnover") or 0

                # 突破检测：每只票用自己的关键位 + 自己的上一轮 high/low。
                # high 创本段新高且 >= 阻力 = 向上突破；low 创新低且 <= 支撑 = 向下突破。
                st = state[sym]
                tags = []
                if st["last_high"] is not None:
                    if st["up"] is not None and high > st["last_high"] + 1e-9 and high >= st["up"]:
                        tags.append(f"[↑破阻力{high}≥{st['up']}]")
                    if st["dn"] is not None and low < st["last_low"] - 1e-9 and low <= st["dn"]:
                        tags.append(f"[↓破支撑{low}≤{st['dn']}]")
                st["last_high"], st["last_low"] = high, low

                # 止损价（当日止损条件单，查不到标 "-")
                sp = stop_prices.get(sym)
                sp_str = f"{sp:.2f}" if sp is not None else "-"

                # ① append 该标的自己的连续 log（累积，AI 分析时读最近 N 行）
                append_log(
                    state[sym]["log_file"],
                    [ts, sym, last, bid, ask, f"{ratio:.0f}", f"{vr:.1f}", high, low,
                     f"{turnover / 1e8:.2f}", sp_str],
                )
                # ② stdout 仍 print（段通知 + 突破标记），每只票一行带 [sym] 前缀
                stop_info = f" 止损={sp_str}" if sp_str != "-" else ""
                print(
                    f"[{ts}] [{sym}] last={last} bid={bid} ask={ask} "
                    f"买卖比={ratio:.0f} 量比={vr:.1f} high={high} low={low} "
                    f"额={turnover / 1e8:.1f}亿{stop_info} {' '.join(tags)}",
                    flush=True,
                )
                if tags:
                    broke.append((sym, " ".join(tags)))
            # 任一只破关键位即整段提前结束、触发通知（突破响应从≤duration压到~interval）
            if broke:
                print(f"!!! 有标的破关键位，提前结束本段以即时通知 AI：{broke} !!!", flush=True)
                break
        except Exception as e:
            print(f"[{datetime.now():%H:%M:%S}] err:{e}", flush=True)
        time.sleep(INTERVAL)

    # 每 10 分钟重估方向提醒（2026-07-28 立，原 60 分钟、同日用户要求加密）：每段结束时
    # 读 log 首条时间算累计盯盘时长，每满 10 分钟输出一次重估提醒（标记文件去重、避免跨段重复）。
    # 为什么：盘中方向/趋势/行情变化快，1 小时重估太慢、错过转向；10 分钟强制重估一次。
    # 盯盘容易固守开盘方向、忘记 skill 的「动态修正方向」规定（2026-07-27 MU 午盘守偏空 4 小时
    # 没重估、错过 ~9700 HKD）。靠工具强制提醒，不靠记忆。
    try:
        marker_file = os.path.join(LOG_DIR, f"reassess_marker_{date_str}_{mode}.txt")
        reminded = set()
        if os.path.exists(marker_file):
            with open(marker_file) as mf:
                reminded = {int(x) for x in mf.read().split() if x.strip().isdigit()}
        first_log = state[syms[0]]["log_file"]
        elapsed_min = None
        if os.path.exists(first_log):
            with open(first_log) as lf:
                rdr = csv.reader(lf)
                next(rdr, None)  # 跳表头
                first_row = next(rdr, None)
                if first_row:
                    first_dt = datetime.strptime(f"{date_str} {first_row[0]}", "%Y%m%d %H:%M:%S")
                    elapsed_min = (datetime.now() - first_dt).total_seconds() / 60
        if elapsed_min is not None and elapsed_min >= 10:
            ten_mark = int(elapsed_min // 10) * 10  # 对齐到 10 倍数（10/20/30…）
            if ten_mark not in reminded:
                reminded.add(ten_mark)
                with open(marker_file, "w") as mf:
                    mf.write(" ".join(str(h) for h in sorted(reminded)))
                print(
                    f"⏰ 重估方向提醒（已盯盘 {int(elapsed_min)} 分钟、满 {ten_mark} 分钟）："
                    f"过动态修正方向 5 触发（①破阻力 ②破支撑 ③站上/跌破VWAP ④箱体假突破≥2次 "
                    f"⑤持续单向运动≥1h）+ 自问「方向/趋势/行情是否仍与开盘一致」，不固守开盘判断。另：重读 SKILL.md「硬性护栏」全 8 条刷新记忆（上下文压缩后规则细节会衰减，每 10 分钟强制重读一次，2026-08-18 用户立）。",
                    flush=True,
                )
    except Exception as e:
        print(f"[重估提醒检查 err:{e}]", flush=True)

    # 临近停盯边界的开仓资格提醒（2026-08-18 立，工具强制防「自设截止线」）：
    # 距该市场停盯边界 ≤60 分钟窗口内每段打印剩余分钟 + 规则原文要点——距停盯 >5 分钟
    # 开仓资格就在，按压缩止盈实算净赔率 ≥1.8 照常评估；AI 无权自设「临近收盘/午休
    # 不开仓」截止线（2026-08-17、2026-08-18 两次同类违规后用户立工具强制）。
    # ≤5 分钟段改为打印「绝对不开仓窗口、只盯到停盯」。
    try:
        from trade_utils_tiger import minutes_to_session_end, OPEN_WINDOW_MIN
        for _mkt in ("HK", "US"):
            if any(s.startswith(f"{_mkt}.") for s in syms):
                _mins = minutes_to_session_end(_mkt)
                if 0 < _mins <= 60:
                    if _mins > OPEN_WINDOW_MIN:
                        print(
                            f"⏰ 距{_mkt}停盯边界 {_mins:.0f} 分钟（>5）：开仓资格仍在——按压缩止盈"
                            f"实算净赔率 ≥1.8 照常评估，禁止自设「临近收盘/午休不开仓」截止线"
                            f"（2026-08-18 用户立；下单脚本另有 ≤5 分钟时间闸硬拦）。",
                            flush=True,
                        )
                    else:
                        print(
                            f"⏰ 距{_mkt}停盯边界 {_mins:.0f} 分钟（≤5）：绝对不开仓窗口，"
                            f"持仓按停盯流程处理、空仓只盯到停盯。",
                            flush=True,
                        )
                break
    except Exception as e:
        print(f"[停盯边界提醒 err:{e}]", flush=True)

    # 段结束 VWAP 检查（强制锚点 2026-08-03：VWAP 位置随盘面动态变化，段结束输出自带 VWAP，
    # AI 段结束唤醒即见全貌方向，不靠记忆去跑 monitor_summary）
    _vwap_map_mseg = {}  # {sym: vwap}——供下方赶顶检测复用（快照只查一次）
    try:
        ret, df = ctx.get_market_snapshot(syms)
        if ret == 0 and df is not None and len(df) > 0 and "avg_price" in df.columns:
            if "code" in df.columns:
                rows = {r["code"]: r for _, r in df.iterrows()}
            else:
                rows = {syms[i]: df.iloc[i] for i in range(min(len(syms), len(df)))}
            lines = []
            for sym in syms:
                row = rows.get(sym)
                if row is None:
                    lines.append(f"  {sym}: 无快照")
                    continue
                # 美股盘前：last_price 停昨收、现价在 pre_price（同上采样逻辑，2026-08-19）；
                # avg_price（VWAP）在盘前为昨日盘中口径，对盘前价格发现代表性弱——盘前
                # 同时展示 pre_price 与 VWAP 差值，AI 按「盘前 VWAP 代表性弱于盘中」解读。
                pre = row.get("pre_price")
                cur = pre if (pre is not None and str(pre) != "nan") else row.get("last_price")
                vwap = row.get("avg_price")
                if cur is None or vwap is None or (isinstance(vwap, float) and vwap != vwap):
                    lines.append(f"  {sym}: VWAP 获取失败")
                    continue
                cur, vwap = float(cur), float(vwap)
                _vwap_map_mseg[sym] = vwap
                diff = cur - vwap
                who = "上方（多头占优）" if diff > 0 else ("下方（空头占优）" if diff < 0 else "持平")
                lines.append(f"  {sym}: 现价 {cur:.2f} | VWAP {vwap:.2f} | {who} {diff:+.2f}")
            print("📊 VWAP 检查（方向框架地面真相，段结束必看）:\n" + "\n".join(lines), flush=True)
        else:
            print(f"📊 VWAP 检查: snapshot 失败 ret={ret}", flush=True)
    except Exception as e:
        print(f"[VWAP 检查 err:{e}]", flush=True)

    # ⚡ 加速赶极端检测（2026-08-19 立，工具强制——「动能将竭主动平仓」的在场打印）：
    # 持仓标的段内急速赶顶/赶底 → 高亮警报逼 AI 当段显式决断（详见 account_status.blast_check）。
    # monitor_segment 是 10 秒快照采样（段内仅 4-5 点），价格序列从当日 log 全量取
    # （blast_check 内 len<10 门槛要求足够点数；10 秒间隔下 40 秒段太稀，用全日序列近似段窗口）。
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from account_status import blast_check
        _prices = {}
        for sym in syms:
            log_file = state[sym]["log_file"]
            if os.path.exists(log_file):
                with open(log_file) as lf:
                    _rows = [r for r in csv.DictReader(lf) if r.get("last", "").replace(".", "").isdigit()]
                if _rows:
                    _prices[sym] = [float(r["last"]) for r in _rows]
        _blasts = blast_check(_vwap_map_mseg, prices_by_sym=_prices)
        if _blasts:
            print("\n".join(_blasts), flush=True)
    except Exception as _e:
        print(f"[赶顶检测 err:{_e}]", flush=True)

    # 段结束完整采样统计（2026-08-03 立）：段结束自带「从开盘到当前的完整统计」，
    # AI 看段结束即见整体（点数/开/末/high/low/买卖比均/量比均/近5点），不只看本段 4-5 点。
    # 为什么：曾只看段结束当前段、漏整体趋势（2026-08-03 用户三次纠正：降频/没拉全貌/没看完整log）。
    # 靠工具固化段结束自带完整统计，不依赖记忆——与上方 VWAP 检查、重估提醒同理（工具强制，不靠记忆）。
    try:
        print("📊 完整采样统计（从开盘到当前所有点，看整体不只看本段）:", flush=True)
        for sym in syms:
            log_file = state[sym]["log_file"]
            if not os.path.exists(log_file):
                continue
            with open(log_file) as lf:
                rows = list(csv.DictReader(lf))
            if not rows:
                continue
            lasts = [float(r["last"]) for r in rows if r.get("last", "").replace(".", "").isdigit()]
            ratios = [float(r["ratio"]) for r in rows if r.get("ratio", "").lstrip("-").replace(".", "").isdigit()]
            vrs = [float(r["vr"]) for r in rows if r.get("vr", "").lstrip("-").replace(".", "").isdigit()]
            if not lasts:
                continue
            n = len(lasts)
            recent = " ".join(f"{r['last']}({r['ratio']})" for r in rows[-5:])
            # 空列表跳过该指标（2026-08-16 修）：纯 ws 采样写出的 log ratio/vr 列全空（已实测
            # 19099 行全空 ratio），原实现 sum/len 直接 ZeroDivisionError、被外层 except 吞掉后
            # 该块对剩余标的统计一并丢失。
            ratio_avg = f"{sum(ratios)/len(ratios):.0f}" if ratios else "N/A（ws log 无此列）"
            vr_avg = f"{sum(vrs)/len(vrs):.1f}" if vrs else "N/A（ws log 无此列）"
            # 连续性指标（2026-08-04 立 C7）：距首次采样分钟数，与点数并列让 AI 和用户一眼看出降频
            # （密采样正常 = 点数多且距首采样合理；降频 = 点数少-距首采样久，明显不匹配）。
            mins_str = ""
            first_t = rows[0].get("time", "") if rows else ""
            if first_t:
                try:
                    ft = datetime.strptime(first_t, "%H:%M:%S").time()
                    fdate = (datetime.now().date() - timedelta(days=1)) if ft > datetime.now().time() else datetime.now().date()
                    mins_str = f" 距首采样{(datetime.now() - datetime.combine(fdate, ft)).total_seconds()/60:.0f}分钟"
                except Exception:
                    pass
            print(
                f"  {sym}: 点数={n}{mins_str} 开={lasts[0]:.2f} 末={lasts[-1]:.2f} "
                f"high={max(lasts):.2f} low={min(lasts):.2f} "
                f"买卖比均={ratio_avg} 量比均={vr_avg} "
                f"| 近5点(价 买卖比): {recent}",
                flush=True,
            )
    except Exception as e:
        print(f"[完整统计 err:{e}]", flush=True)

    print(
        f"=== 分段结束 {datetime.now():%H:%M:%S}"
        f"（AI 读各标的 log 最近 N 行分析：{' / '.join(state[s]['log_file'] for s in syms)}）===",
        flush=True,
    )
    ctx.close()


if __name__ == "__main__":
    main()
