#!/usr/bin/env python3
"""老虎 IP 白名单守护（2026-08-19 立）：代理节点 IP 漂移检测 + 自动切换 + 响铃弹窗 +
「已添加」点击自动同步本地白名单。

背景（为什么需要，2026-08-19 实录）：老虎开发者级 IP 白名单只认 IP 不认域名，代理订阅
刷新后节点入口 IP 会变——当天多个节点 IP 全变，当前节点出口漂出白名单，
get_filled_orders 被「access forbidden: request ip is not in ip whitelist」拒，实盘 / 模拟
（apply_scope=all 金丝雀）全部断 API。白名单本身无 API 可改（只能在老虎开发者信息页
手改），但「漂移后自动恢复服务 + 提醒补白 + 本地自动同步」可以全自动。

四件事（2026-08-19 用户立，按钮确认制；2026-09-01 T133 修订为全量清单制）：
  1. 当前节点 IP 不在白名单 → 自动 `xpilot switch` 切到白名单内可用节点（vless 优先）
     + 连通验证（xpilot test --current）；
  2. 检测到 代理节点 IP 变化（漂移）→ 响铃 + 可最小化提醒面板（2026-09-01 T128 换载体，
     见 alert_panel.swift）；
  3. 面板给出【当前全部可用 IP 清单】（2026-09-01 T133：`xpilot node list` 实查的全部
     节点出口 IP——旧口径只给漂移后新 IP，用户只能逐个补、旧 IP 无法按额度上限清理；
     新口径支持「先清空老虎页白名单 → 一次性粘贴全部可用 IP」的全量重配）；
  4. 用户在老虎页完成「清空 → 全量粘贴」后回面板点「已添加」→ 等待子进程收到点击信号，
     把 config.json 的 proxy.tiger_whitelist **全量替换**为面板所给清单（旧口径是合并，
     与「先清空再加白」的老虎页操作不对应——本地会残留老虎页已清掉的死 IP）。

数据源（全部本机实读、不硬编码节点）：
  - 节点全景：`xpilot node list` 实查（2026-09-01 T133 起；失败回退 ~/.config/xpilot/
    nodes.json——xpilot 订阅同步后写入，含每节点真实入口 IP）；
  - 当前节点：实查输出的 `*` 行（回退 nodes.json 的 default_node）；
  - 白名单：config.json 的 proxy.tiger_whitelist（本地副本 = 比对基准；老虎页真实白名单
    无 API 可读，靠「用户点已添加」把老虎页的变更同步到这份副本）。

行为分支：
  - 当前节点 IP 在白名单、无节点漂移 → 打印一行 ✅、退出 0（正常态，preflight / 巡检
    每次跑都过）。
  - 当前节点 IP 不在白名单 → 响铃 + 面板（告知漂移 + 给全量可用 IP 清单），然后自动
    `xpilot switch` 切到「IP 仍在白名单内」的节点并验证连通；切换成功打印恢复摘要、
    退出 0（服务已自动恢复，面板只等用户加白后点「已添加」）；无可用节点时面板升级
    告知「全部漂出白名单」、退出 2。
  - 当前节点 IP 在白名单但其它节点 IP 漂移 → 低频提醒（面板给全量清单；响铃一次），
    不切节点、退出 0。

面板与点击链路（2026-09-01 T128 换载体；2026-08-19 原按钮确认制语义不变）：remind()
把 {ips, message, title} 写 tmp/proxy_guard_pending.json，派生 `--await-confirm` 子进程
（start_new_session 脱离守护进程——守护退出 / 下轮巡检不影响等待）；子进程起 alert_panel
（AppKit 面板，scripts/alert_panel.swift 编译产物 tmp/alert_panel——**可最小化到 Dock、
点 Dock 图标唤回**，替代 osascript display dialog 模态窗的「置顶不可最小化」；产物缺失
时子进程自动 swiftc 现场编译）等点击：
  - 点「已添加」→ replace_whitelist 把 pending 里的全量清单**整体写入** config.json +
    log_event 留痕（对应老虎页「先清空 → 全量粘贴」操作）；
  - 点「取消」/ 关窗 / 超时 → 不替换、删 pending 退出（条件仍成立时 5 分钟冷却后再提醒）。

为什么切节点前先提醒不阻塞流程：自动切换是恢复主路径，面板是「提醒用户去老虎页补白」
的异步信息——若等用户点「已添加」才切，断 API 窗口被人为拉长。响铃用 Basso
（macOS 警示音，watcher 通知同款）。

⚠️ 误点边界（如实说明）：点「已添加」即视为「老虎页已按清单全量重配」，代码不二次验证
（老虎无 API 可查白名单）——若实际没改就点，本地白名单会与老虎页不一致（本地可能超前
或滞后），guard 的漂移判定会失真、切换后可能被老虎拒（连通验证测的是节点出网
generate_204、测不出老虎层拒绝）。面板文案明确「先在老虎页完成清空 + 全量粘贴、再点
已添加」。

通知冷却：同事件（漂移提醒 / 全漂出警报）5 分钟内不重复弹（tmp/proxy_guard_<key>.stamp
mtime 判定，与 monitor_watcher 的 NOTIFY_COOLDOWN_SECONDS 同思路）——本脚本被
launchd 巡检高频触发时不会弹窗风暴。

用法：
  python3 proxy_guard.py               # 检查 + 按需自动切换 + 提醒（preflight / 巡检挂接）
  python3 proxy_guard.py --dry-run     # 只检查和打印会做什么、不实际切换不弹窗（测试用）
  python3 proxy_guard.py --test-alert  # 弹一次测试确认面板（ips 为空、点已添加不改 config）
  python3 proxy_guard.py --await-confirm  # 【内部】等面板点击 + 替换（remind 派生，不手跑）

退出码：0 = 正常或已自动恢复；2 = 全部节点漂出白名单（需人工补白）/ 切换全部失败；
3 = 配置缺失（白名单未配置 / 节点清单取不到）。
"""
import json
import os
import subprocess
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, "..", "..", "..", ".."))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "..", "config.json")
NODES_PATH = os.path.expanduser("~/.config/xpilot/nodes.json")
TMP_DIR = os.path.join(PROJECT_ROOT, "tmp")
# 弹窗待确认内容（ips + 文案）：remind 写入、--await-confirm 子进程读取
PENDING_PATH = os.path.join(TMP_DIR, "proxy_guard_pending.json")

# 同一事件的提醒冷却（秒）：launchd 巡检高频触发时防弹窗风暴（对齐 monitor_watcher 的
# NOTIFY_COOLDOWN_SECONDS = 300）。
NOTIFY_COOLDOWN_SECONDS = 300

# 节点连通性验证超时（秒）：xpilot test --current 真实流量实测约 15s 内，给 45s 余量。
TEST_TIMEOUT = 45

# 提醒面板（2026-09-01 T128）：AppKit 可最小化面板，源码 scripts/alert_panel.swift、
# 编译产物 tmp/alert_panel（运行时数据目录、gitignore 不入库；缺失时现场 swiftc 编译）。
PANEL_SRC = os.path.join(SCRIPT_DIR, "alert_panel.swift")
PANEL_BIN = os.path.join(TMP_DIR, "alert_panel")
PANEL_RESULT = os.path.join(TMP_DIR, "proxy_guard_panel_result.json")   # 面板点击结果（await 轮询）

# xpilot node list 实查超时（2026-09-01 T133：节点全景以实查为准，失败回退 nodes.json）。
NODE_LIST_TIMEOUT = 30

# 自动切换重试总预算（秒）：preflight 挂接后单条 Bash 须 < harness 120s——多候选重试
# 不拖爆预检。预算检查在每轮候选尝试前做：首轮必放行（预算未耗尽），一轮最坏
# switch 60s + test 45s = 105s < 120s；首轮失败后预算已耗尽、停止再试。
SWITCH_BUDGET_SECONDS = 90


def _print(s):
    print(s, flush=True)


def load_whitelist():
    """读 config.json proxy.tiger_whitelist（列表或 ';'-分隔字符串都兼容）。"""
    try:
        with open(CONFIG_PATH) as f:
            cfg = json.load(f)
        wl = cfg.get("proxy", {}).get("tiger_whitelist")
    except Exception:
        return None
    if wl is None:
        return None
    if isinstance(wl, str):
        wl = [ip.strip() for ip in wl.split(";") if ip.strip()]
    return [str(ip).strip() for ip in wl if str(ip).strip()]


def replace_whitelist(new_ips):
    """把 config.json 的 proxy.tiger_whitelist **全量替换**为 new_ips（去重、保序；缩进 2
    与原格式一致）。返回替换后的完整列表。

    2026-09-01 T133 改合并为替换：用户在老虎页的操作是「先清空白名单 → 一次性粘贴全部
    可用 IP」——点「已添加」时本地副本应与老虎页等价（旧 merge 会保留老虎页已清掉的死
    IP，本地与老虎页脱节、漂移判定失真）。写失败抛异常（白名单是比对基准，写失败要让
    上层知道），由调用方兜底。"""
    with open(CONFIG_PATH) as f:
        cfg = json.load(f)
    proxy = cfg.setdefault("proxy", {})
    wl = []
    for ip in new_ips:
        ip = str(ip).strip()
        if ip and ip not in wl:
            wl.append(ip)
    proxy["tiger_whitelist"] = wl
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
        f.write("\n")
    return wl


def ensure_panel():
    """确保提醒面板二进制存在（缺失则 swiftc 现场编译，约 10s、一次性）。返回是否可用。"""
    if os.path.isfile(PANEL_BIN) and os.access(PANEL_BIN, os.X_OK):
        return True
    if not os.path.isfile(PANEL_SRC):
        _print(f"⚠️ 面板源码缺失（{PANEL_SRC}）——回退 osascript 弹窗")
        return False
    try:
        os.makedirs(TMP_DIR, exist_ok=True)
        r = subprocess.run(["swiftc", "-O", "-o", PANEL_BIN, PANEL_SRC],
                           capture_output=True, text=True, timeout=120)
        return r.returncode == 0
    except Exception:
        return False


def fetch_nodes_live():
    """`xpilot node list` 实查（2026-09-01 T133）→ (当前节点 id or None, {node_id: info})。

    输出行格式：`[*) ]<id>: <name>@<host>:<port> [<protocol>] <ip>:<port> - <延迟>`；
    `*` 前缀 = 当前节点。实查保证 IP 清单反映订阅最新状态（nodes.json 可能滞后）。
    返回 {node_id: {"address", "protocol", "name"}}（与 load_nodes 同构，main 直接换用）。
    命令失败 / 解析为空 → (None, {})，调用方回退 nodes.json。"""
    try:
        r = subprocess.run(["xpilot", "node", "list"], capture_output=True,
                           text=True, timeout=NODE_LIST_TIMEOUT)
        if r.returncode != 0:
            return None, {}
        import re
        nodes, cur = {}, None
        for line in (r.stdout or "").splitlines():
            m = re.match(
                r"^(\*?)\s*(\S+?):\s*(\S+?)@\S+\s+\[(\w+)\]\s+(\d{1,3}(?:\.\d{1,3}){3}):\d+",
                line.strip())
            if m:
                if m.group(1) == "*":
                    cur = m.group(2)
                nodes[m.group(2)] = {"address": m.group(5), "protocol": m.group(4),
                                     "name": m.group(3)}
        return cur, nodes
    except Exception:
        return None, {}


def load_nodes():
    """读 nodes.json → (default_node_id, {node_id: {address, name, protocol}})。"""
    try:
        with open(NODES_PATH) as f:
            data = json.load(f)
    except Exception:
        return None, None
    nodes = data.get("nodes", {})
    default = data.get("default_node")
    return default, {nid: {"address": v.get("address"), "name": v.get("name"),
                           "protocol": v.get("protocol")} for nid, v in nodes.items()}


def in_cooldown(key):
    stamp = os.path.join(TMP_DIR, f"proxy_guard_{key}.stamp")
    try:
        if os.path.isfile(stamp) and (time.time() - os.path.getmtime(stamp)) < NOTIFY_COOLDOWN_SECONDS:
            return True
    except OSError:
        pass
    return False


def mark_notified(key):
    try:
        os.makedirs(TMP_DIR, exist_ok=True)
        stamp = os.path.join(TMP_DIR, f"proxy_guard_{key}.stamp")
        with open(stamp, "w") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S"))
    except OSError:
        pass


def log_event(line):
    """追加写 tmp/proxy_guard.log（时间戳 + 事件行）——漂移 / 切换 / 已添加合并历史
    可追溯（对齐 monitor_watcher 的 tmp/watcher_notify.log 思路）。失败静默。"""
    try:
        os.makedirs(TMP_DIR, exist_ok=True)
        with open(os.path.join(TMP_DIR, "proxy_guard.log"), "a") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S") + "\t" + line + "\n")
    except OSError:
        pass


def ring(sound="Basso"):
    """响铃（异步、不阻塞主流程）。"""
    subprocess.Popen(["afplay", f"/System/Library/Sounds/{sound}.aiff"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def dialog_pending():
    """是否已有未确认的 DayTradingAgent 提醒窗在屏。

    用途（2026-08-19 用户立「必须点击才消失」后配的防叠加闸）：旧窗未被点击又弹新的会
    多窗叠着，已有在屏窗时跳过新弹（冷却机制 5 分钟后条件仍成立会再弹，信息不丢）。
    2026-09-01 T128 换载体后匹配 alert_panel 二进制（编译产物进程名）；保留旧
    display dialog 模式兼容（monitor_watcher 等仍在用的告知窗）——任意时刻至多一个
    模态/面板提醒窗。"""
    try:
        for pattern in ("alert_panel", "display dialog.*DayTradingAgent"):
            r = subprocess.run(["pgrep", "-f", pattern], capture_output=True)
            if r.returncode == 0:
                return True
        return False
    except Exception:
        return False


def _spawn_listener():
    """派生 --await-confirm 等待子进程（脱离守护进程会话：守护退出 / 下轮巡检不影响
    弹窗等待与点击合并）。"""
    subprocess.Popen(
        [sys.executable, os.path.abspath(__file__), "--await-confirm"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True)


def remind(add_ips, message, title="DayTradingAgent · 老虎白名单提醒", dry_run=False):
    """弹「取消 / 已添加」确认窗：pending 内容（ips + 文案）落盘后派生等待子进程，
    用户点「已添加」时由子进程把 IP 合并进 config.json（见 await_confirm）。响铃由
    调用方负责。防叠加：已有未确认弹窗在屏（dialog_pending）则跳过本次。"""
    if dry_run:
        _print(f"  [dry-run] 将弹「已添加/取消」确认窗：{title}\n  {message}")
        return
    if dialog_pending():
        _print("  （已有未确认弹窗在屏，跳过本次弹窗防叠加；条件仍成立时 5 分钟冷却后会再弹）")
        return
    try:
        os.makedirs(TMP_DIR, exist_ok=True)
        with open(PENDING_PATH, "w") as f:
            json.dump({"ips": list(add_ips), "message": message, "title": title},
                      f, ensure_ascii=False)
    except OSError as e:
        _print(f"  ⚠️ pending 文件写入失败（{e}）——点「已添加」将拿不到待合并 IP，仍弹窗提醒")
    _spawn_listener()


def await_confirm():
    """【内部模式 --await-confirm】弹可最小化提醒面板并等待点击（2026-09-01 T128 换载体）。

    只由 remind() 派生的独立子进程执行（守护主进程不等待）；--test-alert 也复用本函数
    （ips 为空 → 点「已添加」不改 config，纯测信号链路）。

    链路：起 alert_panel（缺失现场编译；面板常驻可最小化、超时 1h 自动关）→ 轮询结果
    文件（面板点击后写 {"clicked": ok|cancel|timeout}）→ 按结果收尾：
      ok      → replace_whitelist 把 pending 里的【全量清单】整体写入 config.json
                proxy.tiger_whitelist（对应老虎页「先清空 → 全量粘贴」，T133）+ log_event；
      cancel  / timeout → 不替换、删 pending 退出（5 分钟冷却后条件仍成立会再提醒）。
    面板编译失败（无 Xcode CLT 等）→ 回退 osascript display dialog（旧载体、语义一致，
    只是不可最小化）。"""
    try:
        with open(PENDING_PATH) as f:
            pend = json.load(f)
    except Exception:
        pend = {}
    ips = [str(ip).strip() for ip in (pend.get("ips") or []) if str(ip).strip()]
    message = pend.get("message") or "（提醒内容丢失——pending 文件缺失，本次点击不替换白名单）"
    title = pend.get("title") or "DayTradingAgent · 老虎白名单提醒"

    if ensure_panel():
        try:
            os.remove(PANEL_RESULT)
        except OSError:
            pass
        proc = subprocess.Popen([PANEL_BIN, PENDING_PATH, PANEL_RESULT])
        # 轮询结果文件：面板点按钮 / 超时后写入并退出；上限 1h 面板超时 + 60s 缓冲
        deadline = time.time() + 3660
        clicked = None
        while time.time() < deadline:
            if proc.poll() is not None or os.path.isfile(PANEL_RESULT):
                time.sleep(0.3)   # 面板写结果与退出有小竞争，稍等落盘
                try:
                    with open(PANEL_RESULT) as f:
                        clicked = (json.load(f).get("clicked") or "").strip()
                except Exception:
                    clicked = None
                if clicked:
                    break
            time.sleep(1)
        try:
            proc.terminate()
        except Exception:
            pass
        try:
            os.remove(PANEL_RESULT)
        except OSError:
            pass
        if clicked != "ok":
            try:
                os.remove(PENDING_PATH)
            except OSError:
                pass
            return 0   # 取消 / 超时 / 异常：不替换
    else:
        # 回退旧载体（面板不可用时保底，语义一致）
        footer = ("\n\n—— 在老虎页完成「清空白名单 → 全量粘贴」后回此窗口点「已添加」：本地 "
                  "config.json 白名单自动全量同步；点「取消」（或 Esc）= 暂不同步、稍后会再提醒。")
        esc = (message + footer).replace("\\", "\\\\").replace('"', '\\"')
        esc_title = title.replace('"', '\\"')
        try:
            r = subprocess.run(
                ["osascript", "-e",
                 f'display dialog "{esc}" with title "{esc_title}" '
                 f'buttons {{"取消", "已添加"}} default button "已添加" '
                 f'cancel button "取消" with icon caution'],
                capture_output=True, text=True)
        except Exception:
            return 1
        clicked = "ok" if (r.returncode == 0 and "已添加" in (r.stdout or "")) else "cancel"
        if clicked != "ok":
            try:
                os.remove(PENDING_PATH)
            except OSError:
                pass
            return 0

    if not ips:
        _print("✅ 收到「已添加」确认（测试：无 IP 待替换，config 未改动）")
        try:
            os.remove(PENDING_PATH)
        except OSError:
            pass
        return 0
    add_str = ";".join(ips)
    old_wl = load_whitelist() or []
    try:
        wl = replace_whitelist(ips)
    except Exception as e:
        _print(f"❌ 收到「已添加」确认，但 config.json 全量写入失败（{e}）——"
               f"请手动把 {add_str} 写入 proxy.tiger_whitelist")
        log_event(f"已添加确认但全量写入失败（{e}）：{add_str}")
        return 1
    _print(f"✅ 收到「已添加」确认：config.json tiger_whitelist 已全量替换为面板清单"
           f"（旧 {len(old_wl)} IP → 现 {len(wl)} IP，与老虎页保持一致）")
    log_event(f"已添加确认：全量替换 {len(old_wl)} → {len(wl)} IP：{add_str}")
    try:
        os.remove(PENDING_PATH)
    except OSError:
        pass
    return 0


def test_node_connectivity():
    """当前节点真实流量连通性检测（xpilot test --current）。返回 True/False/None
    （None = 命令执行异常，视作 False 处理但保留区分）。"""
    try:
        r = subprocess.run(["xpilot", "test", "--current"], capture_output=True,
                           text=True, timeout=TEST_TIMEOUT)
        out = r.stdout + r.stderr
        return ("OK" in out) and ("失败" not in out and "FAIL" not in out)
    except Exception:
        return None


def switch_node(node_id, dry_run=False):
    """xpilot switch 到指定节点，返回是否成功（switch 后 nodes.json 的 default_node
    已更新、xray 配置已重载——xpilot 自身保证）。"""
    if dry_run:
        _print(f"  [dry-run] 将执行: xpilot switch {node_id}")
        return True
    try:
        r = subprocess.run(["xpilot", "switch", node_id], capture_output=True,
                           text=True, timeout=60)
        return r.returncode == 0
    except Exception:
        return False


def main():
    args = sys.argv[1:]
    dry_run = "--dry-run" in args

    # 内部模式：等弹窗点击 + 合并（remind 派生的子进程入口）
    if "--await-confirm" in args:
        return await_confirm()

    if "--test-alert" in args:
        ring()
        try:
            os.makedirs(TMP_DIR, exist_ok=True)
            with open(PENDING_PATH, "w") as f:
                json.dump({"ips": [],
                           "message": "测试提醒（--test-alert）：确认窗样式与「已添加 → "
                                      "自动同步」链路测试。本次 IP 为空，点「已添加」"
                                      "不会改 config.json。",
                           "title": "DayTradingAgent · 白名单提醒（测试）"},
                          f, ensure_ascii=False)
        except OSError:
            pass
        _spawn_listener()
        _print("✅ 测试提醒面板已发出（detached 等待点击；点「已添加」= 收到信号、不改 config）")
        return 0

    # 配置读取（节点全景以 xpilot node list 实查为准，2026-09-01 T133；实查失败回退
    # nodes.json——订阅同步后的落地数据，可能滞后于订阅最新状态）
    whitelist = load_whitelist()
    default_id, nodes = fetch_nodes_live()
    nodes_source = "xpilot node list 实查"
    if not nodes:
        default_id, nodes = load_nodes()
        nodes_source = "nodes.json（实查失败回退）"
    if not whitelist:
        _print("⚠️ config.json 未配置 proxy.tiger_whitelist —— 无法做白名单比对。"
               "请在 config.json 的 proxy 节加 tiger_whitelist（当前老虎页配置的 IP 清单）；"
               "之后老虎页加白时弹窗点「已添加」会自动同步这里。")
        return 3
    if not nodes or not default_id:
        _print("⚠️ 节点清单取不到（xpilot node list 与 nodes.json 都失败）—— 先跑 xpilot "
               "node list / xpilot update 确认订阅已同步。")
        return 3

    wl_set = set(whitelist)
    cur = nodes.get(default_id)
    cur_ip = (cur or {}).get("address")
    _print(f"🌐 当前节点：{cur.get('name') if cur else default_id}（出口 {cur_ip}）"
          f" | 白名单 {len(wl_set)} IP | 节点数据：{nodes_source}")

    # 节点 IP 全景与白名单的关系
    in_wl = {nid: v for nid, v in nodes.items() if v.get("address") in wl_set}
    drifted = {nid: v for nid, v in nodes.items() if v.get("address") not in wl_set}
    # 全量可用 IP 清单（2026-09-01 T133）：当前全部节点的出口 IP——「既有白名单中仍可用
    # （= 有节点对应的白名单 IP）+ 漂移后新节点」的并集即节点全集；白名单里无节点对应
    # 的死 IP 不给（先清空重配的意图就是清掉它们，老虎页白名单有数量上限）。
    all_ips = sorted({v["address"] for v in nodes.values() if v.get("address")})
    all_str = ";".join(all_ips)
    add_str = all_str   # 旧变量名沿用（log / 提醒串），语义已从「漂移新 IP」改为「全量清单」

    # ── 分支 1：当前节点漂出白名单 → 面板提醒（全量清单）+ 自动切换恢复 ──────────
    if cur_ip not in wl_set:
        if not in_cooldown("drift"):
            ring()
            _op_hint = (
                f"【加白步骤】老虎开发者信息页 → 额外配置 → IP 白名单：先【清空】已有全部"
                f"白名单 IP，再一次性粘贴以下【全部可用 IP】（{len(all_ips)} 个、\";\" 分隔）："
                f"\n{all_str}\n\n（旧 IP 不清会逐步占满白名单数量上限——先清空再全量加入"
                f"才不会撞上限。）")
            if in_wl:
                remind(all_ips,
                       f"⚠️ 代理节点 IP 已变化：当前节点出口 {cur_ip} 不在老虎 IP 白名单，"
                       f"老虎 API 已被拒（access forbidden）。\n\n"
                       f"正在自动切换到白名单内节点恢复服务（结果见终端 / proxy_guard.log）。\n\n"
                       f"{_op_hint}",
                       dry_run=dry_run)
            else:
                remind(all_ips,
                       f"🚨 全部 代理节点 IP 都不在老虎 IP 白名单——自动切换无可用目标，"
                       f"老虎 API 保持中断。\n\n必须人工处理。\n\n{_op_hint}\n\n"
                       f"加白后本守护下轮检测自动恢复。",
                       title="DayTradingAgent · 白名单全漂出警报", dry_run=dry_run)
            mark_notified("drift")
            log_event(f"漂移提醒：当前 {default_id} 出口 {cur_ip} 不在白名单；"
                      f"面板给全量可用清单 {len(all_ips)} IP")
        if not in_wl:
            _print(f"🚨 无白名单内节点可切（{len(drifted)}/{len(nodes)} 节点漂出）——"
                  f"需人工去老虎页清空重配（全量清单 {len(all_ips)} IP 见面板提醒）")
            return 2
        # 自动切换：优先选 vless 节点（ss 节点 2026-08-17 实测有不通的先例），再验证连通。
        # 总预算 90s（SWITCH_BUDGET_SECONDS）：preflight 挂接后单条 Bash < harness 120s。
        candidates = sorted(in_wl.keys(),
                            key=lambda n: (nodes[n].get("protocol") != "vless", n))
        budget_end = time.time() + SWITCH_BUDGET_SECONDS
        for cand in candidates:
            if not dry_run and time.time() > budget_end:
                _print("⏱️ 自动切换重试预算用尽（90s）——停止尝试，需人工检查")
                log_event("自动切换预算用尽（90s），候选未试完")
                return 2
            _print(f"🔄 自动切换：{default_id}（{cur_ip} 漂出白名单）→ {cand}"
                  f"（{nodes[cand]['address']}）")
            if not switch_node(cand, dry_run=dry_run):
                _print(f"  ❌ 切换失败，试下一个候选")
                continue
            if dry_run:
                _print(f"  [dry-run] 跳过连通性验证")
                return 0
            ok = test_node_connectivity()
            if ok:
                _print(f"  ✅ 已切换并验证连通（generate_204 经代理 OK）——老虎 API 出口恢复白名单内")
                log_event(f"自动切换：{default_id}({cur_ip}) → {cand}({nodes[cand]['address']}），连通 OK")
                return 0
            _print(f"  ⚠️ 切换后连通性验证未过（{ok}），试下一个候选")
        _print("❌ 白名单内候选节点全部切换失败——需人工检查（xpilot test -a 全节点实测）")
        log_event(f"自动切换失败：候选 {len(candidates)} 个全部未过连通验证")
        return 2

    # ── 分支 2：当前节点正常，但存在漂移节点 → 低频提醒全量重配（不切不动）───────
    if drifted:
        if not in_cooldown("suggest"):
            ring()
            remind(all_ips,
                   f"ℹ️ 检测到 代理节点 IP 变化（当前节点正常、服务无影响）："
                   f"{len(drifted)}/{len(nodes)} 个节点 IP 不在老虎白名单。\n\n"
                   f"建议全量重配（老虎开发者信息页 → 额外配置 → IP 白名单）：先【清空】"
                   f"已有白名单，再一次性粘贴以下【全部可用 IP】（{len(all_ips)} 个、"
                   f"\";\" 分隔）：\n{all_str}\n\n"
                   f"不处理风险：当前节点日后漂移时，新 IP 节点无法作为切换备选；旧 IP "
                   f"不清会逐步占满白名单数量上限。",
                   title="DayTradingAgent · 节点 IP 变化提醒", dry_run=dry_run)
            mark_notified("suggest")
            log_event(f"建议全量重配：{len(drifted)}/{len(nodes)} 节点漂移，"
                      f"面板给全量可用清单 {len(all_ips)} IP")
        _print(f"ℹ️ 当前节点出口在白名单内 ✅；另有 {len(drifted)}/{len(nodes)} 节点 IP 未加白"
              f"（已提醒，冷却 5 分钟；全量清单 {len(all_ips)} IP 见面板）")
    else:
        _print(f"✅ 节点 IP 全景与白名单一致（{len(nodes)} 节点全部在白名单）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
