#!/usr/bin/env python3
"""富途热度榜找标的 (找标的第一步铁律)。用法: python3 hot_list.py [HK|US] [count]
叠 snapshot 查流动性,输出 code/名称/类型/热度/现价/成交额/振幅/换手。
港股加类型列（ETF/个股,来自 classify_hk_security 白名单判定）+ 印花税最小止损距提示——
「港股优先 ETF（免印花税）」从开仓末端的软勾选前移到找标的第一步在场（2026-08-18 立）。
2026-08-24 加「预期止损距%」预检列（个股才显示）：认领前预估该股当日能形成的最大止损距
= 多空两侧结构距离（现价↔当日低点 / 现价↔min(VWAP,当日高点)）较大者 × 折扣 0.4
（入场与止损各贴结构位吃缓冲,2026-08-24 MINIMAX 两形态实测折后 0.3~0.5 取中）——
低于税闸门槛（2026-08-24 立 2.22% → 2026-08-25 用户降 1.2%,48 笔样本实证旧值错杀大于
保护）= 该股当日结构大概率过不了闸、不值得认领（2026-08-24 用户立,
当日实录:认领 MINIMAX 后两次形态成立均被税闸拦,预检本可在认领时点就判「不过」；
同日开仓净赔率门槛 2.4→1.8、2026-08-25 再降 1.2,阈值读 config 统一调参）。
已处理 get_hot_list 嵌套返回 (ret,(total,df)) 的坑。"""
import os
import sys
from futu import OpenQuoteContext, Market

market_str = (sys.argv[1] if len(sys.argv) > 1 else "HK").upper()
count = int(sys.argv[2]) if len(sys.argv) > 2 else 15
# 2026-08-17 修：market 参数拼错显式报错退出——原来 `else Market.US` 把任何非 HK 的值
# （如 HK.、hkx、hkg）静默当美股查，查错市场还不报错、浪费一次盯盘启动时间。
if market_str not in ("HK", "US"):
    print(f"❌ 未知 market 参数 '{sys.argv[1]}'（应为 HK / US）", file=sys.stderr)
    sys.exit(1)
market = Market.HK if market_str == "HK" else Market.US

ctx = OpenQuoteContext('127.0.0.1', 11111)

def extract_df(p):
    """get_hot_list 返回嵌套 (ret,(total,df)),递归找出 DataFrame。"""
    if hasattr(p, 'columns'):
        return p
    if isinstance(p, tuple):
        for e in p:
            d = extract_df(e)
            if d is not None:
                return d
    return None

ret, payload = ctx.get_hot_list(market=market, count=count)
if ret != 0:
    print(f"热度榜失败 ret={ret}: {payload}"); ctx.close(); sys.exit(1)
df = extract_df(payload)
if df is None or len(df) == 0:
    print("热度榜为空"); ctx.close(); sys.exit(1)

# 叠 snapshot 查成交额/振幅/换手 (筛流动性,排除盘口薄的小盘题材)
sec_col = 'security' if 'security' in df.columns else df.columns[0]
codes = df[sec_col].tolist()[:count]
ret2, snap = ctx.get_market_snapshot(codes)
ctx.close()
snap_map = {r['code']: r for _, r in snap.iterrows()} if (ret2 == 0 and snap is not None) else {}

# 港股加类型判定（2026-08-18 立：ETF 偏好前移到找标的第一步）。
# 复用 classify_hk_security.classify（HKEX ETF 官方白名单,同目录 import）；
# 判定失败不阻断榜单输出（类型列显示 ?,找标的链路不能因分类器故障停摆）。
sec_type = {}
if market_str == "HK":
    try:
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from classify_hk_security import classify as _classify
        for c in codes:
            try:
                # 榜单代码带 HK. 前缀，classify 的 normalize_symbol 只认 .HK 后缀——
                # 先剥前缀再传（实测 'HK.07709' 原样传入会 miss 白名单、误判 stock）
                sec_type[c] = _classify(c.split('.')[-1]).get('type', '?')
            except Exception:
                sec_type[c] = '?'
    except ImportError:
        sec_type = {c: '?' for c in codes}

# 印花税 R 损耗判据（2026-08-18 立;2026-08-24 随开仓门槛 2.4→1.8 联动重算为 2.22%;
# 2026-08-25 用户下调至 1.2%——48 笔样本实证旧值按 25% 兑现率标定而真实兑现率 0.62R,
# 旧闸会拦掉 34/40 笔个股、其中已扣税仍 +24.3R,错杀大于保护;推导记录在 SKILL.md「港股个股印花税闸」）：
# 个股止损距 < 门槛×股价时,双边印花税 R 损耗超上限,个股不经济、应换 ETF 替代或放弃
# ——在榜单直接标注个股的最小合规止损距。
# 2026-08-25 起 config.json risk.stamp_tax_gate_pct 为唯一权威源（open_position 下单硬校验
# 同读此值）,此处默认仅作 config 缺失时的回退——调整阈值改 config,并同步本回退与
# pool_claim.py 的回退常量,防三处分叉。
STAMP_STOP_PCT = 0.004

def _stamp_gate_pct():
    """税闸门槛（config 优先,缺失回退本文件常量）。"""
    import json, os
    try:
        cfg = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                          "..", "config.json")))
        return float(cfg.get("risk", {}).get("stamp_tax_gate_pct", STAMP_STOP_PCT))
    except Exception:
        return STAMP_STOP_PCT

def _stamp_gate_enabled():
    """税闸总开关（2026-08-28 立）：false = 税闸停用,榜单不显示税闸列与预检列。"""
    import json, os
    try:
        cfg = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                          "..", "config.json")))
        return bool(cfg.get("risk", {}).get("stamp_tax_gate_enabled", True))
    except Exception:
        return True

# 预期止损距折扣（2026-08-24 立）：结构距离 → 实际可用止损距的折减系数。
# 入场点与止损位都要贴结构位、各吃一段缓冲,实际止损距只有结构距离的三至五成
# （2026-08-24 MINIMAX 两形态实测:结构距离 3.4 → 实际 1.1 / 1.8 → 1.1,折后 0.3~0.5）,取 0.4。
STOP_DISCOUNT = 0.4

def _exp_stop_pct(s):
    """个股当日预期止损距上限（%,取多空两侧较大者）。
    做多侧 = (现价−当日低点)/现价（止损只能放结构低点下方）;
    做空侧 = (min(VWAP,当日高点)−现价)/现价（阻力取 VWAP 与当日高点的较近者——
    高价开盘跳空的上沿不会有合理止损结构,跳空缺口上沿不算阻力）。
    数据缺字段返回 None（显示 -）,不阻断榜单。"""
    px, lo = s.get('last_price'), s.get('low_price')
    hi, vwap = s.get('high_price'), s.get('avg_price')
    if not (px and px == px) or not (lo and lo == lo):
        return None
    long_cap = (px - lo) / px if px > lo else 0.0
    short_cap = 0.0
    resist = min(v for v in (hi, vwap) if v and v == v) if any(v and v == v for v in (hi, vwap)) else None
    if resist and resist > px:
        short_cap = (resist - px) / px
    return max(long_cap, short_cap) * STOP_DISCOUNT * 100

def _type_tag(sec):
    t = sec_type.get(sec, '?')
    return {'etf': 'ETF', 'stock': '股', 'reit': 'REIT', 'derivative': '衍生!'}.get(t, '?')

_gate_on = _stamp_gate_enabled() and market_str == "HK"
_gate_cols_hdr = f" {'税闸止损≥':>9} {'预期止损距%':>12} {'预检':>6}" if _gate_on else ""
print(f"{'代码':12} {'名称':14} {'类型':>5} {'热度':>9} {'现价':>8} {'额(亿)':>7} {'振幅%':>6} {'换手%':>6}{_gate_cols_hdr}")
for _, r in df.iterrows():
    sec = r[sec_col]
    name = str(r.get('name', ''))[:12]
    heat = r.get('average_heat', r.get('trade_heat', 0))
    s = snap_map.get(sec, {})
    def fmt(v, f=".2f"):
        return f"{v:{f}}" if v is not None and v == v else "-"
    tag = _type_tag(sec)
    gate_cols = ""
    if _gate_on:
        # 仅个股提示最小止损距与预检；ETF/REIT 免印花税无此约束
        _gate_pct = _stamp_gate_pct()
        price = s.get('last_price')
        gate = f"{price*_gate_pct:.2f}" if (tag == '股' and price and price == price) else "-"
        exp_stop = _exp_stop_pct(s) if tag == '股' else None
        exp_str = f"{exp_stop:.2f}" if exp_stop is not None else ("免" if tag in ('ETF', 'REIT') else "-")
        if tag != '股':
            verdict = "免"
        elif exp_stop is None:
            verdict = "?"
        else:
            verdict = "过" if exp_stop >= _gate_pct * 100 else ("近" if exp_stop >= _gate_pct * 100 * 0.7 else "低")
        gate_cols = f" {gate:>9} {exp_str:>12} {verdict:>6}"
    print(f"{sec:12} {name:14} {tag:>5} {heat:>9.0f} {fmt(price) if False else fmt(s.get('last_price')):>8} "
          f"{fmt(s.get('turnover',0) and s.get('turnover')/1e8):>7} {fmt(s.get('amplitude')):>6} {fmt(s.get('turnover_rate')):>6}{gate_cols}")
if market_str == "HK":
    n_stock = sum(1 for v in sec_type.values() if v == 'stock')
    n_etf = sum(1 for v in sec_type.values() if v == 'etf')
    if _gate_on:
        print(f"\n📌 港股偏好 ETF（免印花税 0.1%/边）：榜上 ETF {n_etf} 只 / 个股 {n_stock} 只；"
              f"「税闸止损≥」= 该股作为个股开仓所需的最小止损距（{_stamp_gate_pct()*100:.2f}%×股价,低于它印花税损耗超上限、应换 ETF 或放弃）")
        print(f"📌 「预期止损距%/预检」= 认领前税闸预检（2026-08-24 立）：个股当日能形成的最大止损距估计"
              f"（多空两侧结构距离较大者 × {STOP_DISCOUNT} 折扣,剔跳空——现价↔当日低点 / 现价↔min(VWAP,当日高点)）。"
              f"预检「低」（<70% 门槛）= 该股当日结构大概率过不了税闸、不值得认领,优先换 ETF 或其它个股；"
              f"「近」（70%~100% 门槛）= 边缘,谨慎认领；「过」/「免」= 可认领。")
    else:
        print(f"\n📌 港股偏好 ETF（免印花税 0.1%/边）：榜上 ETF {n_etf} 只 / 个股 {n_stock} 只。"
              f"（印花税闸已停用 2026-08-28——个股止损距不再受限,印花税 0.1%/边仍计入净赔率；恢复改 config.risk.stamp_tax_gate_enabled=true）")
