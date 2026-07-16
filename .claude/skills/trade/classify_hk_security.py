#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
港股证券类型判定(个股/ETF/REIT/衍生品/未知)。

背景:长桥 `static`/`quote`/`security-list` 实测均无 type 字段
(2026-07-05 用 19 标的实测确认:name/lot_size/eps/dividend/currency/exchange,
无 security_type)。本脚本用四层启发式判定,最终 ETF 身份以 HKEX 官方白名单
为准(本地缓存一份,见 HKEX_ETF_WHITELIST)。

用法:
  python3 classify_hk_security.py 02800.HK 00700.HK 00823.HK
  python3 classify_hk_security.py 02800 2800  (自动补 .HK,前导零容忍)

判定层(按优先级):
  1. 衍生品代码段/后缀 → warrant/CBBC/deduct 立即返回(衍生品,禁交易)
  2. HKEX 官方 ETF 白名单(本地缓存) → ETF
  3. name 关键词(ETF/FUND/TRACKER/INDEX 信号) + eps=0 → ETF
  4. 代码段启发式 028xx/030xx/031xx → 疑似 ETF(需白名单核对)
  5. 其他 → 个股(默认)/未知

注意:盈富 02800 name=TRACKER FUND 且 eps=-2.058 是已知异常,
靠白名单命中(已在白名单),不靠 name/eps 启发式。
"""
import sys
import json
import subprocess
import os

# ============ HKEX 官方港股 ETF 白名单(本地缓存) ============
# 来源:HKEX 交易所买卖产品名录 + 长桥 static 实测(2026-07-05)
# 这只是主力 ETF,非完整名单完整名单需从 HKEX 官网下载更新
# 更新方法:见本目录 hk-level2-sources.md 的"优先行动序列"
HKEX_ETF_WHITELIST = {
    # 恒生指数系列
    '02800',  # 盈富基金 Tracker Fund of Hong Kong
    '03140',  # CAM HKUS AI ETF
    # 恒生中国企业指数(H股/国企指数)
    '02828',  # 恒生 H 股 ETF(盈富基金系列)
    '02838',  # 恒生 FCI50 ETF
    # 恒生科技指数
    '03032',  # 恒生科技 ETF(华夏)
    # 跨市场/MSCI/主题
    '03040',  # GX MSCI China
    '02837',  # GX HS TECH
    '03000',  # iShares 安硕 A50
    '02833',  # DB 星展 A50
    '03035',  # iShares 安硕 亚洲 50
    '03036',  # iShares 安硕 MSCI Asia ex Japan
    # 商品/单资产
    '03112',  # A Pando Blockchain
    '03016',  # 依范斯达克达
    '03100',  # 恒生 FCI50
    '03130',  # 易方达(香港)中证 100
    '03148',  # 华夏沪深 300
    '03155',  # 华夏香港 MSCI
    '03168',  # 彭博
    '03169',  # Bloomberg
    '03171',  # 华夏
    '03172',  # 华夏香港
    '03017',  # 依范斯达克达亚洲
    '03019',  # 依范斯达克达日本
    '03020',  # 依范斯达克达美国
    # 反向/杠杆 ETF(注意:这些虽免印花税,但属衍生策略,谨慎)
    '07300',  # FI 恒生指数每日反向
    '07500',  # FI 恒生指数 2x 杠杆
    '07311',  # FI 恒生国企指数每日反向
    '07511',  # FI 恒生国企指数 2x 杠杆
    '07709',  # 南方两倍做多海力士(杠杆ETF,韩国存储,2026-07-06 港股热度第1)
    '07747',  # 南方两倍做多三星电子(杠杆ETF,韩国存储,2026-07-06 港股热度第5)
}

# ============ 港股代码段启发式 ============
# HKEX 历史代码段大致分工(不绝对,但守住门)
DERIVATIVE_CODE_RANGE = [
    (10000, 19999),  # 窝轮 warrants
    (20000, 23999),  # 窝轮/CBBC
    (27000, 27999),  # CBBC 牛熊证(实测 security-list 见 274xx)
    (68000, 69999),  # 部分衍生品
]

ETF_CODE_RANGES = [
    (2800, 2899),    # 028xx 主力 ETF 段
    (3000, 3199),    # 030xx/031xx ETF 段
    (7000, 7999),    # 07xxx 反向/杠杆 ETF(注意含衍生策略)
]

# ============ name 关键词 ============
ETF_NAME_KEYWORDS = ['ETF', 'FUND', 'TRACKER', 'INDEX', 'ISHARES', 'GX ', 'PANDO']
REIT_NAME_KEYWORDS = ['REIT', 'TRUST']
# 衍生品 name 信号(窝轮/CBBC 常见)
DERIVATIVE_NAME_KEYWORDS = ['CALL', 'PUT', 'BULL', 'BEAR', '牛熊', '窝轮']


def normalize_symbol(sym: str) -> str:
    """统一为 5 位数字代码(无 .HK 后缀)。02800 / 2800 / 02800.HK 都归一化。"""
    s = sym.strip().upper()
    if s.endswith('.HK'):
        s = s[:-3]
    # 去掉前导零后转 int 再格式化为 5 位
    try:
        n = int(s)
        return f'{n:05d}'
    except ValueError:
        return s


def code_in_ranges(code_str: str, ranges):
    """代码字符串是否落在 [(lo, hi)] 任一区间。"""
    try:
        n = int(code_str)
    except ValueError:
        return False
    return any(lo <= n <= hi for lo, hi in ranges)


def get_static_info(symbols: list) -> dict:
    """用富途 OpenD get_market_snapshot 拉静态信息,返回 {归一化code: static_dict}。
    富途符号 市场.代码 前缀（HK.02800）。长桥 CLI 2026-07-15 撤销后改用富途。"""
    from futu import OpenQuoteContext
    # 统一转富途 HK. 前缀格式
    codes = []
    for s in symbols:
        c = str(s).replace('.HK', '').replace('.hk', '')
        if c.startswith('HK.'):
            c = c[3:]
        codes.append(f'HK.{c}')
    try:
        ctx = OpenQuoteContext('127.0.0.1', 11111)
        ret, df = ctx.get_market_snapshot(codes)
        ctx.close()
        if ret != 0:
            print(f'[warn] futu snapshot 失败 ret={ret}', file=sys.stderr)
            return {}
        result = {}
        for _, row in df.iterrows():
            code = normalize_symbol(str(row.get('code', '')).replace('HK.', ''))
            result[code] = {
                'symbol': f'{code}.HK',
                'name': row.get('name'),
                'lot_size': row.get('lot_size'),
                'eps': row.get('earning_per_share'),  # 富途 eps 字段名 = earning_per_share
            }
        return result
    except Exception as e:
        print(f'[warn] static 拉取失败: {e}', file=sys.stderr)
        return {}


def classify(symbol: str, static: dict = None) -> dict:
    """
    判定单个标的类型。
    返回 {symbol, code, name, lot_size, eps, type, confidence, reasons}
    type ∈ {'etf','stock','reit','derivative','adr','unknown'}
    confidence ∈ {'high','medium','low'}
    """
    code = normalize_symbol(symbol)
    result = {
        'symbol': f'{code}.HK', 'code': code,
        'name': None, 'lot_size': None, 'eps': None,
        'type': 'unknown', 'confidence': 'low', 'reasons': []
    }
    if static and code in static:
        info = static[code]
        result['name'] = info.get('name')
        result['lot_size'] = info.get('lot_size')
        result['eps'] = info.get('eps')
        name_upper = (info.get('name') or '').upper()
        try:
            eps = float(info.get('eps') or 0)
        except (ValueError, TypeError):
            eps = None

        # 1. 衍生品 name 关键词（先区分杠杆 ETF vs 牛熊证/窝轮，2026-07-06 修）
        # 1a. 窝轮/CBBC 硬关键词（CALL/PUT/牛熊/窝轮）— ETF 绝不会有，直接判衍生品
        if any(k in name_upper for k in ('CALL', 'PUT', '牛熊', '窝轮')):
            result['type'] = 'derivative'
            result['confidence'] = 'high'
            result['reasons'].append('name 含窝轮/CBBC 关键词(CALL/PUT/牛熊/窝轮)')
            return result
        # 1b. BULL/BEAR — 可能是杠杆/反向 ETF（允许）或牛熊证（禁），按代码段区分
        if 'BULL' in name_upper or 'BEAR' in name_upper:
            if code_in_ranges(code, ETF_CODE_RANGES) or code in HKEX_ETF_WHITELIST:
                # 代码在 ETF 段/白名单 → 杠杆/反向 ETF（2026-07-06 用户允许），不判衍生品
                result['reasons'].append('name 含 BULL/BEAR + 代码在 ETF 段 → 疑似杠杆/反向 ETF(允许)，继续往下判 ETF')
                # 不 return，落到层 3(白名单)/层 5(代码段)判 ETF
            else:
                # 代码不在 ETF 段 → 牛熊证/CBBC
                result['type'] = 'derivative'
                result['confidence'] = 'high'
                result['reasons'].append('name 含 BULL/BEAR + 代码不在 ETF 段 → 牛熊证/CBBC')
                return result

    # 2. 衍生品代码段(优先级高,直接判衍生品)
    if code_in_ranges(code, DERIVATIVE_CODE_RANGE):
        result['type'] = 'derivative'
        result['confidence'] = 'high'
        result['reasons'].append(f'代码 {code} 在衍生品段(1xxxx/2xxxx/27xxx)')
        return result

    # 3. HKEX 官方 ETF 白名单(最高优先级,权威)
    if code in HKEX_ETF_WHITELIST:
        result['type'] = 'etf'
        result['confidence'] = 'high'
        result['reasons'].append(f'命中 HKEX ETF 官方白名单')
        # 反向/杠杆 ETF 标注(衍生策略,虽免印花税但谨慎)
        if 7000 <= int(code) <= 7999:
            result['reasons'].append('⚠️ 反向/杠杆 ETF,属衍生策略,谨慎')
        return result

    # 4. name 关键词 + eps 判 ETF
    if static and code in static:
        name_upper = (result['name'] or '').upper()
        has_etf_kw = any(k in name_upper for k in ETF_NAME_KEYWORDS)
        eps_is_zero = (eps == 0)
        if has_etf_kw and eps_is_zero:
            result['type'] = 'etf'
            result['confidence'] = 'medium'
            result['reasons'].append(f'name 含 ETF 关键词({has_etf_kw}) + eps=0')
            return result
        # REIT
        if any(k in name_upper for k in REIT_NAME_KEYWORDS):
            result['type'] = 'reit'
            result['confidence'] = 'high'
            result['reasons'].append(f'name 含 REIT/TRUST')
            return result
        # name 含 ETF 但 eps 非0(如盈富异常)→ 疑似 ETF
        if has_etf_kw and not eps_is_zero:
            result['type'] = 'etf'
            result['confidence'] = 'medium'
            result['reasons'].append(f'name 含 ETF 关键词但 eps={eps}(异常,需白名单核对)')
            return result

    # 5. ETF 代码段启发式(疑似)
    if code_in_ranges(code, ETF_CODE_RANGES):
        result['type'] = 'etf'
        result['confidence'] = 'low'
        result['reasons'].append(f'代码 {code} 在 ETF 段(028xx/030xx/031xx/07xxx),但未命中白名单,需核对')
        return result

    # 6. 默认个股
    result['type'] = 'stock'
    result['confidence'] = 'medium'
    result['reasons'].append(f'默认个股(代码不在 ETF/衍生品段,未命中白名单)')
    # 港股第二上市/外资股后缀 -W/-S/-SW 等
    if result['name'] and ('-W' in result['name'] or '-S' in result['name']):
        result['reasons'].append('name 含 -W(同股不同权)/-S(第二上市)后缀,仍为个股')
    return result


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    symbols = sys.argv[1:]
    static = get_static_info(symbols)
    print(f"{'symbol':<10} | {'type':<12} | {'confidence':<8} | {'name':<24} | reasons")
    print('-' * 90)
    for s in symbols:
        r = classify(s, static)
        name = (r['name'] or '-')[:24]
        print(f"{r['symbol']:<10} | {r['type']:<12} | {r['confidence']:<8} | {name:<24} | {'; '.join(r['reasons'])}")


if __name__ == '__main__':
    main()
