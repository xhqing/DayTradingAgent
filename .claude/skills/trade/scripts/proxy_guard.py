#!/usr/bin/env python3
"""老虎 IP 白名单守护（2026-08-19 立；2026-10-29 重写为「与 xpilot 彻底解耦」）。

# 定位（用户 2026-10-29）

本项目是**监护者**，不是执行者：xpilot 是另一个独立项目，唯一共同点是都消费同一份
JMS 提供的 IP 池。本项目只做三件事：

1. **比对**：JMS 最新 IP 池（本项目自己拉的订阅，见 `jms_pool.py`）↔ 老虎 IP 白名单
   （本地镜像 `config.tiger_whitelist`）。不一致 → 响铃 + 面板给「全量 IP 清单」，
   用户去老虎开发者页「先清空再全量粘贴」；用户点「已添加」后本脚本自动同步本地镜像。
2. **校验当前出口**：实测**本项目自己代理池**（`ip_pool.py` 起的 xray，端口 10871+）
   的出口 IP 是否在白名单内。
3. **池内自愈**：出口不在白名单 → 在自有端口池里换一个端口（毫秒级、不重启进程）。

# 明确不做

- 不调 `xpilot switch` / `xpilot test` / `xpilot node list`，不读 `~/.config/xpilot/*`。
- 不替用户决定老虎白名单内容（老虎无 API，必须人工去开发者页操作）。
- 不重启任何代理进程（换端口即可，进程不动）。

# 调用方

- `preflight.py`：盯盘启动时跑一次（无参数）。
- `.claude/hooks/monitor_watcher.py`：盯盘期间每 10 秒兜底跑一次（launchd 驱动）。
- 本文件同时被 `ip_pool.py` / `jms_pool.py` import，复用 `load_whitelist()` /
  `log_event()` / `_print()` / `PROJECT_ROOT` / `TMP_DIR` / `CONFIG_PATH`（这些是纯工具
  函数，与 xpilot 无关，改动时注意别破坏被别人 import 的接口）。

# 参数

- （无参数）        正常检测 + 必要时提醒/换端口
- `--dry-run`       只检测与打印，不响铃、不弹窗、不写冷却状态
- `--test-alert`    发一个测试面板（点「已添加」不会改 config）
- `--await-confirm` 内部模式：等弹窗点击后合并白名单（`remind()` 派生的子进程入口）

# 冷却

两类提醒各自 5 分钟冷却（`in_cooldown()` / `mark_notified()`），launchd 每 10 秒触发
不会弹窗风暴。
"""
import json
import os
import subprocess
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, "..", "..", "..", ".."))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "..", "config.json")
TMP_DIR = os.path.join(PROJECT_ROOT, "tmp")
# 弹窗待确认内容（ips + 文案）：remind 写入、--await-confirm 子进程读取
PENDING_PATH = os.path.join(TMP_DIR, "proxy_guard_pending.json")

# 同一事件的提醒冷却（秒）：launchd 巡检高频触发时防弹窗风暴（对齐 monitor_watcher 的
# NOTIFY_COOLDOWN_SECONDS = 300）。
NOTIFY_COOLDOWN_SECONDS = 300

# 保留（历史常量；当前已无「重启式切换 + 连通验证」流程，勿删以免外部引用报错）。
TEST_TIMEOUT = 45

# 提醒面板（2026-09-01 T128）：AppKit 可最小化面板，源码 scripts/alert_panel.swift、
# 编译产物 tmp/alert_panel（运行时数据目录、gitignore 不入库；缺失时现场 swiftc 编译）。
PANEL_SRC = os.path.join(SCRIPT_DIR, "alert_panel.swift")
PANEL_BIN = os.path.join(TMP_DIR, "alert_panel")
PANEL_RESULT = os.path.join(TMP_DIR, "proxy_guard_panel_result.json")   # 面板点击结果（await 轮询）

# 保留（历史常量；节点全景现在来自 jms_pool 订阅解析）。
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
    """确保提醒面板二进制可用（缺失或源码更新过则 swiftc 现场编译，约 10s、一次性）。

    2026-10-27 补「源码更新自动重编」：原来只看二进制是否存在，改了 alert_panel.swift
    后旧二进制会一直沿用（如新增的一键复制按钮不生效）——现改为源码 mtime 晚于二进制
    即重编，改源码后无需手动删二进制。
    """
    need_build = True
    if os.path.isfile(PANEL_BIN) and os.access(PANEL_BIN, os.X_OK):
        try:
            need_build = os.path.getmtime(PANEL_SRC) > os.path.getmtime(PANEL_BIN)
        except OSError:
            need_build = False   # 取不到 mtime 就当已就绪（不因读不到时间戳把面板搞坏）
    if not need_build:
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
        # copy_text：面板「复制全部 IP」按钮写入剪贴板的内容（2026-10-27 用户立）。
        # 与 message 里的清单同源（都是 ";" 拼接），不靠面板解析正文。
        with open(PENDING_PATH, "w") as f:
            json.dump({"ips": list(add_ips), "message": message, "title": title,
                       "copy_text": ";".join(str(ip).strip() for ip in add_ips)},
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




def main():
    """白名单守护（2026-10-29 重写：与 xpilot 彻底解耦）。

    ## 现在的职责（用户 2026-10-29 定的定位）

    本项目 = **监护者**，不是执行者。做三件事：

    1. **比对**：JMS 最新 IP 池（本项目自己拉的订阅）↔ 老虎 IP 白名单（本地镜像）。
       不一致 → 响铃 + 面板给「全量 IP 清单」，用户去老虎页清空重配；用户点「已添加」
       后由 `await_confirm()` 自动同步本地镜像。
    2. **校验当前出口**：实测本项目自己代理池的出口 IP 是否在白名单内。
    3. **池内自愈**：出口不在白名单 → 在**本项目自己的端口池**（10871–10875）里换一个
       端口，毫秒级，不重启任何进程。

    ## 明确不做的事

    - 不调 `xpilot switch` / `xpilot test` / `xpilot node list`，不读 `~/.config/xpilot/*`
      ——xpilot 是独立项目，唯一共同点是都消费同一份 JMS IP 池。
    - 不替用户决定老虎白名单内容（老虎无 API，必须人工去开发者页操作）。

    分支说明：
      A. JMS 池里有 IP 不在白名单（含当前出口不在）→ 响铃 + 面板（全量清单）+ 池内换端口
      B. 白名单里有 IP 已不在 JMS 池（拉不到的旧 IP）→ 低频提醒建议清掉
      C. 完全一致 → 什么都不做
    """
    args = sys.argv[1:]
    dry_run = "--dry-run" in args

    # 内部模式：等弹窗点击 + 合并（remind 派生的子进程入口）
    if "--await-confirm" in args:
        return await_confirm()

    if "--test-alert" in args:
        ring()
        # 测试面板要看得到「复制全部 IP」按钮：copy_text 用**当前 config 里的白名单**填
        # （真数据、复制了也无害），但 ips 故意留空 → 点「已添加」不会写 config。
        demo_copy = ";".join(load_whitelist() or [])
        try:
            os.makedirs(TMP_DIR, exist_ok=True)
            with open(PENDING_PATH, "w") as f:
                json.dump({"ips": [],
                           "message": "测试提醒（--test-alert）：确认窗样式与「已添加 → "
                                      "自动同步」链路测试。本次 IP 为空，点「已添加」"
                                      "不会改 config.json。\n\n"
                                      "【复制全部 IP】按钮复制的 = 当前 config 里的真实"
                                      "白名单（仅作按钮演示，可右粘贴验证）；正式提醒时"
                                      "复制的是待加白的全量 IP 清单。",
                           "copy_text": demo_copy,
                           "title": "DayTradingAgent · 白名单提醒（测试）"},
                          f, ensure_ascii=False)
        except OSError:
            pass
        _spawn_listener()
        _print("✅ 测试提醒面板已发出（detached 等待点击；点「已添加」= 收到信号、不改 config）")
        return 0

    whitelist = load_whitelist()
    if not whitelist:
        _print("⚠️ config.json 未配置 proxy.tiger_whitelist —— 无法做白名单比对。"
               "请在 config.json 的 proxy 节加 tiger_whitelist（当前老虎页配置的 IP 清单）；"
               "之后老虎页加白时弹窗点「已添加」会自动同步这里。")
        return 3

    # ── 数据源：本项目自己拉的 JMS 订阅（不读 xpilot 任何东西）────────────────
    try:
        import jms_pool as J
    except Exception as e:
        _print(f"❌ 无法加载 jms_pool.py（{e}）")
        return 3
    _jms = J.refresh()
    if not _jms.get("ok") or not _jms.get("nodes"):
        _print(f"❌ JMS 订阅取不到节点（{_jms.get('reason')}）—— 无法比对白名单。"
               f"手查：python3 .claude/skills/trade/scripts/jms_pool.py")
        return 3

    nodes = _jms["nodes"]
    jms_ips = _jms["ips"]
    wl_set = set(whitelist)
    # 全量可用 IP 清单（给用户去老虎页「先清空再全量粘贴」用）：JMS 池里的全部入口 IP。
    # 白名单里那些已拉不到的旧 IP 不放进复制串——清空重配的意图正是把它们清掉。
    all_ips = list(jms_ips)
    all_str = ";".join(all_ips)

    missing = [ip for ip in jms_ips if ip not in wl_set]        # JMS 有、白名单没有 → 待加白
    stale = [ip for ip in whitelist if ip not in set(jms_ips)]  # 白名单有、JMS 没有 → 可清
    # 「全部失效」= 白名单里**一个 IP 都不在** JMS 池里（零重叠）。这不是正常的换 IP 残留，
    # 而通常意味着 JMS 侧整套换了节点或订阅异常，要先排查 JMS、而不是急着按清单加白（2026-10-29 用户定）。
    all_stale = bool(whitelist) and len(stale) == len(whitelist)

    _print(f"🌐 JMS 池 {len(nodes)} 节点 / {len(jms_ips)} IP"
           f"（订阅缓存 {_jms.get('reason')}）| 白名单镜像 {len(wl_set)} IP"
           f" | 待加白 {len(missing)} | 可清除 {len(stale)}")

    # ── 当前出口：本项目自己的端口池（不是 xpilot 的当前节点）──────────────────
    try:
        import ip_pool as P
    except Exception as e:
        _print(f"❌ 无法加载 ip_pool.py（{e}）")
        return 3

    cur_port = None
    cur_ip = None
    pool_total = 0
    pool_good = 0
    try:
        mapping = P.start_multi()
        pool_total = len(mapping or [])
        if mapping:
            ports = P.probe_ports(mapping)
            pool_good = sum(1 for v in ports.values() if v.get("ok"))
            cur_port = int(P.proxy_url().split(":")[-1])
            cur_ip, _ = P.probe_egress(quiet=True, port=cur_port)
            if not cur_ip:
                cur_ip, _ = P.probe_egress(quiet=True)
    except Exception as e:
        _print(f"⚠️ 端口池状态读取异常：{e}")

    cur_in_wl = (cur_ip in wl_set) if cur_ip else None
    _print(f"🔌 本项目代理：端口 {cur_port or '未知'}｜出口 {cur_ip or '未测出'}"
           f"（{'在' if cur_in_wl else '不在' if cur_in_wl is False else '?'}白名单）"
           f"｜池内可用端口 {pool_good}/{pool_total}")

    _op_hint = (
        f"【加白步骤】老虎开发者信息页 → 额外配置 → IP 白名单：先【清空】已有全部"
        f"白名单 IP，再一次性粘贴以下【全部可用 IP】（{len(all_ips)} 个、\";\" 分隔）："
        f"\n{all_str}\n\n（旧 IP 不清会逐步占满白名单数量上限——先清空再全量加入"
        f"才不会撞上限。）")

    # ── 分支 A：JMS 池里有 IP 不在白名单（含当前出口不在）→ 提醒 + 池内换端口 ─────
    if missing:
        if not in_cooldown("drift"):
            if not dry_run:
                ring()   # dry-run 不响铃（排查时不该打扰用户）
            if cur_in_wl is False:
                msg = (f"🚨 当前代理出口 {cur_ip} 不在老虎 IP 白名单——老虎 API 已被拒"
                       f"（access forbidden）。\n\n"
                       f"JMS 池共 {len(jms_ips)} 个 IP，其中 {len(missing)} 个尚未加白。\n\n"
                       f"正在本项目端口池内换一个白名单内的端口恢复服务"
                       f"（不重启进程、不碰 xpilot，结果见终端 / proxy_guard.log）。\n\n{_op_hint}")
                title = "DayTradingAgent · 当前出口被拒"
            else:
                msg = (f"ℹ️ JMS 订阅池里有 {len(missing)}/{len(jms_ips)} 个 IP 不在老虎白名单"
                       f"（当前出口 {cur_ip or '未测出'} 仍可用、服务无影响）。\n\n"
                       f"建议按下面步骤全量重配，否则那些节点日后无法作为备选；"
                       f"且旧 IP 不清会逐步占满白名单数量上限。\n\n{_op_hint}\n\n"
                       f"👉 点下方【复制全部 IP】按钮即可一键复制上面这串（⌘⇧C 同效）。")
                title = "DayTradingAgent · 白名单待更新"
            if all_stale:
                msg += (f"\n\n⚠️ 另外注意：白名单里现有的 {len(whitelist)} 个 IP **全部**不在 "
                        f"JMS 池内（零重叠）。这通常不是正常的节点换 IP 残留，而更可能是 "
                        f"JMS 侧整套更换了节点、或订阅本身异常 —— 建议**先排查 JMS 订阅是否正常**"
                        f"（`python3 .claude/skills/trade/scripts/jms_pool.py`），确认 JMS 没问题后"
                        f"再按上面清单重配白名单。")
            elif stale:
                msg += (f"\n\n（白名单里另有 {len(stale)} 个 IP 已不在 JMS 池内，"
                        f"属正常残留、全量重配时会自然清掉，本次不单独提醒。）")
            remind(all_ips, msg, title=title, dry_run=dry_run)
            if not dry_run:
                mark_notified("drift")
            log_event(f"白名单待更新：JMS {len(jms_ips)} IP 中 {len(missing)} 个未加白；"
                      f"当前出口 {cur_ip} {'不在' if cur_in_wl is False else '在'}白名单")

        # 当前出口被拒 → 在自有端口池内换端口（毫秒级、不重启进程、不碰 xpilot）
        if cur_in_wl is False:
            if dry_run:
                _print("  [dry-run] 跳过池内换端口")
                return 0
            port, eg, _nid = P.fast_switch(exclude_port=cur_port, force_probe=True)
            tried = 0
            while port and tried < 5:
                tried += 1
                _print(f"  🔄 池内换端口：{cur_port} → {port}（预期出口 {eg}）")
                if eg in wl_set:
                    _print(f"  ✅ 已换到端口 {port}（出口 {eg} 在白名单内）——"
                           f"交易脚本下次启动即走该端口")
                    log_event(f"池内换端口恢复：{cur_port} → {port}（出口 {eg}）")
                    return 0
                cur_port = port
                port, eg, _nid = P.fast_switch(exclude_port=cur_port, force_probe=True)
            _print("🚨 本项目端口池内没有出口在白名单内的端口——"
                   "必须人工去老虎开发者页为 JMS 池的 IP 加白（见面板清单）")
            log_event("池内换端口失败：无白名单内可用端口")
            return 2
        return 0

    # ── 分支 B：JMS 池全部已加白，但白名单里残留部分失效旧 IP ──
    # 2026-10-29 用户定：**这种情况不提醒**——订阅商偶尔换掉个别节点、白名单里留下拉不到的
    # 旧 IP 属正常现象，为它弹窗纯属打扰；旧 IP 会占白名单数量上限，但等下次真需要加白时
    # （分支 A）反正要「先清空再全量粘贴」，届时自然清掉。
    # 「全部失效」是不一样的信号（白名单与 JMS 池零重叠 → 大概率 JMS 侧出问题），
    # 那种情况一定伴随分支 A，排查提示已合并到 A 的文案里，这里不需要重复。
    if stale:
        log_event(f"白名单含 {len(stale)} 个已失效旧 IP（JMS 池已无）——按规则不提醒，"
                  f"下次加白时随全量重配清掉")
        _print(f"ℹ️ JMS 池已全部加白 ✅；白名单另有 {len(stale)} 个残留失效 IP"
               f"（正常现象，不提醒；下次加白时全量重配会自然清掉）")
        return 0

    # ── 分支 C：完全一致 ────────────────────────────────────────────────────
    _print(f"✅ JMS 池与老虎白名单完全一致（{len(jms_ips)} IP），"
           f"当前出口 {cur_ip or '未测出'} 在白名单内")
    return 0


if __name__ == "__main__":
    sys.exit(main())
