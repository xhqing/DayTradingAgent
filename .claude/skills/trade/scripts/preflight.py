#!/usr/bin/env python3
"""盯盘开盘前检查：当前时间 / 港股美股时段 / 富途 OpenD。
用法: python3 preflight.py
一行汇总就绪状态,避免开盘才发现数据源掉线或时间误判。"""
import datetime, socket

now = datetime.datetime.now()
hhmm = now.hour * 60 + now.minute
wd = now.weekday()  # 0=Mon..6=Sun
print(f"⏰ {now.strftime('%Y-%m-%d %H:%M:%S %A')}")

# 港股时段 (UTC+8, 与北京同时区)
if wd >= 5:
    hk = "周末休市"
elif 570 <= hhmm < 720:
    hk = "港股早市 09:30-12:00"
elif 780 <= hhmm < 960:
    hk = "港股午市 13:00-16:00"
else:
    hk = "盘外"
# 美股时段（2026-07-19 修订：撤销"美东 12:00 固定停盯"，美股盯盘覆盖整个盘中——
#   信号窗口=美东 09:30-16:00（盯到用户喊停或收盘）；换算北京时间随美东夏/冬令时切换：
#     夏令时 EDT(UTC-4)：美东 09:30-16:00 = 北京 21:30-次日 04:00（跨午夜）
#     冬令时 EST(UTC-5)：美东 09:30-16:00 = 北京 22:30-次日 05:00（跨午夜）
#   直接用 zoneinfo 把当前时间转到美东时区判「周末 / 时段 / DST」，不再用北京本地反推——
#   冬令时盘中跨午夜段（北京周六凌晨 00:00-05:00 实为美东周五盘中）不会被本地周末判断误伤。
#   zoneinfo 不可用时回退北京夏令时估并标注。）
def _us_status():
    try:
        from zoneinfo import ZoneInfo
        us_now = now.astimezone(ZoneInfo("America/New_York"))
    except Exception:
        # zoneinfo 不可用：回退北京本地估夏令时（仅兜底，冬令时段可能不准）
        if wd >= 5:
            return "周末休市"
        # 夏令时估：美东 09:30-16:00 = 北京 21:30-次日 04:00（跨午夜）
        if 1290 <= hhmm < 1440 or hhmm < 240:
            return "美股盘中(夏令时估·zoneinfo不可用)·可发信号"
        if 1200 <= hhmm < 1290:
            return "美股盘前(夏令时估·zoneinfo不可用)·只预热不发信号"
        return "美股盘外(夏令时估·zoneinfo不可用)·不发信号"
    us_wd = us_now.weekday()
    us_hhmm = us_now.hour * 60 + us_now.minute
    tz_tag = "EDT夏令时" if bool(us_now.dst()) else "EST冬令时"
    if us_wd >= 5:
        return "周末休市"
    if 570 <= us_hhmm < 960:  # 美东 09:30-16:00 盘中
        return f"美股盘中({tz_tag})·可发信号(美东09:30-16:00，盯到用户喊停或收盘)"
    if 240 <= us_hhmm < 570:  # 美东 04:00-09:30 盘前
        return f"美股盘前({tz_tag})·只预热不发信号"
    return f"美股盘外({tz_tag})·不发信号"

us = _us_status()
print(f"📈 港股:{hk} | 美股:{us}")

# 富途 OpenD 端口（盯盘行情主力源，须登录成功才监听 11111）
def port_open(p):
    s = socket.socket(); s.settimeout(1)
    try:
        s.connect(('127.0.0.1', p)); s.close(); return True
    except Exception:
        return False
print(f"📊 富途OpenD:11111 {'✅' if port_open(11111) else '❌(未登录/未启动)'}")

# positions 检查已移除（2026-07-15 信号模式：假设执行、不查 positions，见 SKILL「信号模式总则」第 1 条）
# 长桥 CLI token 检查已移除（2026-07-15 长桥 CLI 授权撤销、token 删除，盯盘数据走富途 + 老虎）
