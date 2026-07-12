#!/usr/bin/env python3
"""盯盘开盘前检查：当前时间 / 港股美股时段 / 长桥 token / 富途 OpenD / signals 文件。
用法: python3 preflight.py
一行汇总就绪状态,避免开盘才发现 token 失效或时间误判。"""
import subprocess, datetime, os, socket

def bash(cmd, timeout=15):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return (r.stdout or "").strip()
    except Exception as e:
        return f"ERR:{e}"

now = datetime.datetime.now()
today = now.strftime("%Y-%m-%d")
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
# 美股时段 (夏令时 21:30-04:00 次日 / 冬令时 22:30-05:00 次日, 简化需人工确认夏冬令)
if wd >= 5:
    us = "周末休市"
elif 1290 <= hhmm < 1440 or 0 <= hhmm < 240:
    us = "美股盘中(夏令时估,确认夏冬令)"
else:
    us = "盘外"
print(f"📈 港股:{hk} | 美股:{us}")

# 长桥 token
auth = bash("bash /tmp/lb.sh auth status 2>&1 | grep -iE 'Status|Account' | head -2")
ok = "valid" in auth.lower()
print(f"🔑 长桥token: {'✅' if ok else '❌'} {auth.replace(chr(10),' | ')[:80]}")

# 富途 OpenD 端口
def port_open(p):
    s = socket.socket(); s.settimeout(1)
    try:
        s.connect(('127.0.0.1', p)); s.close(); return True
    except Exception:
        return False
print(f"📊 富途OpenD:11111 {'✅' if port_open(11111) else '❌(未登录/未启动)'}")

# positions 持仓检查（2026-07-10 前置：开仓前确认无持仓，一次一标的硬规矩）
pos = bash("bash /tmp/lb.sh positions 2>&1", timeout=15)
pos_low = pos.lower()
if 'error' in pos_low or 'connect' in pos_low or not pos:
    pos_stat = "❌查询失败(长桥API，盘后再查)"
elif 'symbol' in pos_low or 'qty' in pos_low or 'code' in pos_low:
    pos_stat = "⚠️有持仓(注意一次一标的硬规矩)"
else:
    pos_stat = "✅空仓(可开仓)"
print(f"📊 positions: {pos_stat}")
