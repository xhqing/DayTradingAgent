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
# 美股时段（2026-07-16 修订：撤销"美股 24h 盯盘"，四类信号只在盘中且限美东 12:00 之前——
#   信号窗口=美东 09:30-12:00=北京时间夏令时 21:30-24:00（冬令时 22:30-次日 01:00），美东 12:00 之后停止盯盘；夏令时估）
if wd >= 5:
    us = "周末休市"
elif 1290 <= hhmm < 1440:
    us = "美股盘中(夏令时估)·可发信号(美东09:30-12:00)"
elif 1200 <= hhmm < 1290:
    us = "美股盘前(夏令时估)·只预热不发信号"
else:
    us = "美股盘外·不发信号(美东12:00后停盯)"
print(f"📈 港股:{hk} | 美股:{us}")

# 富途 OpenD 端口（盯盘行情主力源，须登录成功才监听 11111）
def port_open(p):
    s = socket.socket(); s.settimeout(1)
    try:
        s.connect(('127.0.0.1', p)); s.close(); return True
    except Exception:
        return False
print(f"📊 富途OpenD:11111 {'✅' if port_open(11111) else '❌(未登录/未启动)'}")

# positions 检查已移除（2026-07-15 演练模式：假设执行、不查 positions，见 SKILL「自主演练模式升级」第 1 条）
# 长桥 CLI token 检查已移除（2026-07-15 长桥 CLI 授权撤销、token 删除，盯盘数据走富途 + 老虎）
