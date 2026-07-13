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

# /tmp/lb.sh：长桥 CLI 包装器，屏蔽 LONGPORT_* 环境变量强制走 OAuth。
# /tmp 重启即清，这里在调用前自检，不在则自动重建，避免脚本静默报 'No such file'。
LB_WRAPPER = "/tmp/lb.sh"
LB_WRAPPER_CONTENT = (
    "#!/bin/bash\n"
    'exec env -u LONGPORT_APP_KEY -u LONGPORT_APP_SECRET '
    '-u LONGPORT_ACCESS_TOKEN ~/.local/bin/longbridge "$@"\n'
)


def ensure_lb_wrapper():
    """确保 /tmp/lb.sh 存在且内容正确；被清则重建。幂等。"""
    need_rebuild = True
    try:
        with open(LB_WRAPPER) as f:
            if "env -u LONGPORT_APP_KEY" in f.read():
                need_rebuild = False
    except OSError:
        pass
    if not need_rebuild:
        return
    try:
        with open(LB_WRAPPER, "w") as f:
            f.write(LB_WRAPPER_CONTENT)
        os.chmod(LB_WRAPPER, 0o755)
        print(f"🔧 已重建 {LB_WRAPPER}（/tmp 被清过，长桥命令现在可用）")
    except OSError as e:
        print(f"⚠️ 重建 {LB_WRAPPER} 失败：{e}——长桥命令将报 'No such file'")


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

# 长桥 token（先确保 /tmp/lb.sh 存在，/tmp 被清会自动重建）
ensure_lb_wrapper()
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
else:
    # 解析 markdown 表格判断有无数据行——不能靠 'symbol' 字样：表头永远含 Symbol 会误判"有持仓"（2026-07-13 实测修复）
    data_lines = [l for l in pos.splitlines()
                  if l.strip().startswith('|')
                  and 'symbol' not in l.lower()
                  and 'name' not in l.lower()
                  and '---' not in l]
    if data_lines:
        pos_stat = f"⚠️有持仓({len(data_lines)}行，注意一次一标的硬规矩)"
    else:
        pos_stat = "✅空仓(可开仓)"
print(f"📊 positions: {pos_stat}")
