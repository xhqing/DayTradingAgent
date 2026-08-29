#!/usr/bin/env python3
"""实盘开仓解锁工具（2026-08-21 立，实盘误开防护「会话级解锁 + 网络层物理拒单」方案的执行端）。

方案背景（完整设计见 TODO-archive 2026-08-21 条目 / SKILL.md「auto 账户选择」节）：
auto 模式下防 AI「不小心」开仓到实盘（误传 --account live、上下文残留、混淆型失守）。
核心思路（用户提出）：老虎按「指令发出网络环境」判定，受限地域出口下实盘开仓/加仓被
监管类错误码拒；且 IP 白名单（账号级、只认代理节点入口 IP）下本地直连在**连接层**
就被拒（access forbidden），比 1200 更强、不受老虎政策变动影响。改造后**默认态实盘
请求不挂代理 → 本地直连 → 被白名单物理拒**；只有本工具写出合法解锁文件后，
trade_utils_tiger.apply_proxy 才给实盘挂代理（恢复可达）。

两道检查点（同校验一个解锁文件）：
  1. 6 个下单脚本（open/close/move_stop × 港美）解析到 --account live 时先查解锁文件，
     无效 → 输出 {ok:false, blocked_by:"live_locked"} 拒单；
  2. trade_utils_tiger.apply_proxy 挂代理前查——实盘账户 + 解锁无效 → 跳过挂代理保持直连。

解锁文件设计（会话级、按会话 ID 分文件——多会话并行各持各的解锁，2026-08-24 改）：
  - 路径 .claude/skills/trade/live-unlock.<session_id>.local（gitignore 通配；人工终端无
    session_id 时为 live-unlock.manual.local）；
  - 内容 JSON：{token, expires_at, session_id, created_at}；
  - token = sha256(salt + 实盘账户号 + 到期时间戳 + session_id)——salt 与实盘账户号都存
    accounts.json 内部（permissions.deny 挡 Read + secret_guard 挡 Bash 读取，AI 上下文
    只有打码号 67****91，不知道原料造不出合法 token——「AI 写了也无效」，不靠「AI 不能写」）；
  - 绑定三要素：实盘账户号（防它号借用）+ session_id（会话级，并行会话 A 的解锁 B 借不走
    ——token 绑 sid + 文件名含 sid 双重隔离）+ 到期时间戳（24h 自动过期，不用记得删）；
  - 会话 resume 换 sid 后旧 token 自动作废（fail-closed，实盘日断线恢复需重新授权一次）；
  - 删文件即时收回（任何人删无害；revoke 子命令删本会话的文件）。
  - 为什么要按会话分文件（2026-08-24 实盘盯盘事故改）：原单文件 live-unlock.local 下，
    会话 A 解锁后再授权会话 B 会**覆盖** A 的解锁文件（A 的实盘下单能力即失效）；授权
    凭证同样单文件被先到的 verify-auth 消费（一次点击的凭证被另一会话烧掉）。设计意图是
    多会话并行盯盘、各自授权各持解锁互不冲突——按 sid 分文件后每个会话读写自己的
    live-unlock.<sid>.local / live-auth.<sid>.local，天然无竞态。

授权链（AskUserQuestion 点击 → AI 生成解锁文件，用户不用开终端）：
  ① AI 发 AskUserQuestion（问题须含打码号 +「实盘」字样 + 授权范围）；
  ② PostToolUse hook（live_auth_witness.py，matcher AskUserQuestion）见证真实点击——
     问题命中实盘授权特征且答案为确认授权时，写短时凭证 live-auth.<session_id>.local
     （按会话分文件，时间戳 + HMAC，密钥 = accounts.json 的 tiger.live_unlock_salt，
     ≤10 分钟过期，用后即焚——一次点击只换一次解锁）；
  ③ 本工具 verify-auth 子命令验凭证（存在 + 未过期 + HMAC 合法）→ 从 accounts.json 内部
     取原料算 token 写解锁文件 → 删凭证。无凭证 / 过期 / 伪造 → 拒绝
     （blocked_by: no_fresh_authorization）。

  为什么依赖 hook 见证：AI 自己写文件时没有任何环节能验证「AskUserQuestion 真发生过且
  用户真点了授权」（文本里写「用户已授权」不算数，2026-08-18 教训）。AskUserQuestion
  点击是 harness 级真实事件、AI 伪造不了（tool_response.answers 由 harness 生成），
  hook 把点击变成机器可验证凭证。实测（2026-08-21）：PostToolUse 对 AskUserQuestion
  触发，payload 含 tool_response.answers 与 session_id。

人工场景（用户自己在终端手动跑 live 脚本）：环境无 CLAUDE_CODE_SESSION_ID——读写
live-unlock.manual.local（人工终端之间共享该文件，用户自己操作自己电脑，无 AI 越权面）；
「有 session_id（AI 调用）」按会话分文件严格隔离。

用法：
  python3 live_unlock.py --init-salt          # 初始化 salt 写 accounts.json（一次性）
  python3 live_unlock.py status               # 查解锁状态（打码口径，不吐 token 原料）
  python3 live_unlock.py verify-auth          # 验 live-auth.<sid>.local 凭证 → 写解锁文件 → 焚凭证
  python3 live_unlock.py revoke               # 删本会话解锁文件（收回本会话授权）
  python3 live_unlock.py revoke --all         # 删全部会话解锁文件（整体收回实盘授权）

trade_utils_tiger / 下单脚本 import 本模块的 live_unlock_valid()（同目录 import live_unlock）。
"""
import os
import sys
import json
import time
import hmac
import hashlib
import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
ACCOUNTS_PATH = os.path.join(_HERE, "..", "accounts.json")

UNLOCK_TTL_SECONDS = 24 * 3600     # 解锁 24h 自动过期
AUTH_TTL_SECONDS = 10 * 60         # 授权凭证 10 分钟过期、用后即焚


def _sid_tag(session_id=None):
    """会话文件名片段：sid 的 UUID 直接当文件名段安全（hex+连字符）；无 sid（人工终端）
    用 'manual'。按会话分文件（2026-08-24 立）——多会话并行各自解锁互不覆盖。"""
    sid = session_id if session_id is not None else _session_id()
    s = (sid or "").strip()
    # 只留 UUID 安全字符（字母数字与连字符），防 sid 异常值注入路径
    safe = "".join(ch for ch in s if ch.isalnum() or ch == "-")
    return safe or "manual"


def unlock_path(session_id=None):
    """本会话（或指定会话）的解锁文件路径 live-unlock.<sid>.local。"""
    return os.path.join(_HERE, "..", f"live-unlock.{_sid_tag(session_id)}.local")


def auth_path(session_id=None):
    """本会话（或指定会话）的授权凭证路径 live-auth.<sid>.local。"""
    return os.path.join(_HERE, "..", f"live-auth.{_sid_tag(session_id)}.local")


def _read_accounts():
    """读 accounts.json（本脚本属 trade 自家业务链路，原料不出现在输出里）。"""
    with open(ACCOUNTS_PATH) as f:
        return json.load(f)


def _materials():
    """取 token 原料 (salt, live_account)。缺任一返回 (None, None)。"""
    try:
        tiger = _read_accounts().get("tiger", {})
        return tiger.get("live_unlock_salt"), tiger.get("account_live")
    except Exception:
        return None, None


def _compute_token(salt, live_account, expires_at, session_id):
    msg = f"{salt}|{live_account}|{int(expires_at)}|{session_id or ''}"
    return hashlib.sha256(msg.encode()).hexdigest()


def _session_id():
    """当前会话 id（AI 调用时在场；人工终端为 None → 放宽校验，见 docstring）。"""
    return os.environ.get("CLAUDE_CODE_SESSION_ID")


def live_unlock_valid(verbose=False):
    """解锁文件当前是否有效（两个检查点共用的校验函数）。

    按会话分文件（2026-08-24 改）：校验**本会话**的 live-unlock.<sid>.local。返回
    (ok: bool, reason: str)。reason 在 ok=False 时给机器可读原因：
    - no_file          本会话无解锁文件（默认态；其它会话有自己的文件、与本会话无关）
    - expired          已过 24h 有效期
    - bad_token        token 与期望不匹配（伪造 / salt 变过 / resume 换 sid）
    - bad_json         文件损坏
    人工终端（无 CLAUDE_CODE_SESSION_ID）读 live-unlock.manual.local——人工终端与人工
    终端之间共享 manual 文件（用户自己操作自己电脑，无 AI 越权面）。
    """
    path = unlock_path()
    if not os.path.isfile(path):
        return False, "no_file"
    try:
        with open(path) as f:
            data = json.load(f)
        token = data["token"]
        expires_at = float(data["expires_at"])
    except Exception:
        return False, "bad_json"
    if time.time() >= expires_at:
        return False, "expired"
    salt, live_account = _materials()
    if not salt or not live_account:
        return False, "bad_token"   # 原料缺失（accounts.json 未配 salt/account_live）= 无法验证 = 拒
    # token 绑定校验：文件名已含本会话 sid，token 也用同一 sid 重算须一致（双保险——
    # 即便有人把别会话的文件拷来改名，token 对不上照样拒）
    expected = _compute_token(salt, live_account, expires_at, _session_id() or "")
    if not hmac.compare_digest(token, expected):
        return False, "bad_token"
    return True, "ok"


def write_auth_witness(session_id):
    """（hook 调用路径）不在此实现——见 .claude/hooks/live_auth_witness.py。
    本模块只负责验凭证与写解锁。"""
    raise NotImplementedError("见 live_auth_witness.py")


def verify_auth_and_unlock():
    """验本会话的 live-auth.<sid>.local 凭证 → 合法则写解锁文件 → 焚凭证。返回 (ok, msg)。"""
    salt, live_account = _materials()
    if not salt or not live_account:
        return False, "accounts.json 缺 tiger.live_unlock_salt 或 tiger.account_live（先跑 --init-salt）"
    a_path = auth_path()
    if not os.path.isfile(a_path):
        return False, "blocked_by: no_fresh_authorization（本会话无授权凭证——先经 AskUserQuestion 实盘授权，由 hook 写 live-auth.<sid>.local）"
    try:
        with open(a_path) as f:
            auth = json.load(f)
        ts = float(auth["ts"])
        mac = auth["hmac"]
    except Exception:
        _burn_auth()
        return False, "blocked_by: no_fresh_authorization（凭证损坏，已焚毁）"
    if time.time() - ts > AUTH_TTL_SECONDS:
        _burn_auth()
        return False, "blocked_by: no_fresh_authorization（凭证已过期（>10 分钟），已焚毁——须重新走 AskUserQuestion 授权）"
    expected_mac = hmac.new(salt.encode(), f"{int(ts)}|{live_account}".encode(),
                            hashlib.sha256).hexdigest()
    if not hmac.compare_digest(mac, expected_mac):
        _burn_auth()
        return False, "blocked_by: no_fresh_authorization（凭证 HMAC 不合法，已焚毁）"
    # 凭证合法 → 写解锁文件（token 原料只在本进程内拼装，不输出）
    session_id = _session_id() or ""
    expires_at = time.time() + UNLOCK_TTL_SECONDS
    token = _compute_token(salt, live_account, expires_at, session_id)
    payload = {
        "token": token,
        "expires_at": int(expires_at),
        "session_id": session_id,
        "created_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    u_path = unlock_path()
    with open(u_path, "w") as f:
        json.dump(payload, f, indent=2)
    os.chmod(u_path, 0o600)
    _burn_auth()
    until = datetime.datetime.fromtimestamp(expires_at).strftime("%Y-%m-%d %H:%M")
    return True, (f"✅ 实盘解锁已写入（账户 {mask(live_account)}，本会话绑定，"
                  f"有效期至 {until}，24h 自动过期；撤销跑 python3 live_unlock.py revoke）")


def _burn_auth():
    try:
        os.remove(auth_path())
    except OSError:
        pass


def revoke():
    """删除**本会话**的解锁文件（按会话分文件后 revoke 只收回自己的；要收回全部会话
    用 revoke --all——多会话并行盯盘时用户喊「撤销实盘」应跑 revoke --all）。"""
    try:
        os.remove(unlock_path())
        return True, "✅ 本会话解锁文件已删除，本会话实盘恢复物理不可达（默认态）"
    except FileNotFoundError:
        pass
    if not _any_unlock_files():
        return True, "（解锁文件本就不存在，已是默认态）"
    others = _other_unlock_files()
    note = "；另有其它会话的解锁文件仍在：" + ", ".join(others) + "（全收跑 revoke --all）"
    return True, "✅ 本会话解锁已删" + note


def _all_unlock_files():
    """trade 目录下全部 live-unlock.*.local 文件（按会话分文件后的全景）。"""
    import glob
    return sorted(glob.glob(os.path.join(_HERE, "..", "live-unlock.*.local")))


def _any_unlock_files():
    return bool(_all_unlock_files())


def _other_unlock_files():
    mine = os.path.abspath(unlock_path())
    return [os.path.basename(p) for p in _all_unlock_files()
            if os.path.abspath(p) != mine]


def revoke_all():
    """删除全部会话的解锁文件（用户喊「撤销实盘」的完整收回）。"""
    files = _all_unlock_files()
    for p in files:
        try:
            os.remove(p)
        except OSError:
            pass
    n = len(files)
    return True, (f"✅ 已删除全部 {n} 个解锁文件（含其它会话），实盘全面恢复物理不可达（默认态）"
                  if n else "（没有任何解锁文件，已是默认态）")


def mask(account):
    """打码口径（同 trade_utils_tiger.mask_account）。"""
    s = str(account)
    if not s:
        return ""
    if len(s) <= 4:
        return "****"
    return f"{s[:2]}****{s[-2:]}"


def status():
    ok, reason = live_unlock_valid()
    salt, live_account = _materials()
    rows = {
        "has_salt": salt is not None,
        "has_live_account": live_account is not None,
        "account_live_masked": mask(live_account) if live_account else None,
        "unlocked": ok,
        "reason": reason,
        "session_id": _session_id(),
        "auth_pending": os.path.isfile(auth_path()),
    }
    others = _other_unlock_files()
    if others:
        rows["other_sessions_unlocked"] = others
    print(json.dumps(rows, ensure_ascii=False, indent=2))
    if ok:
        print("（实盘当前可达：挂代理路径开启——注意监管类拦截不再生效，钥匙已交出）")
    else:
        print("（实盘默认态：物理不可达——下单脚本 live_locked 拒单 + 实盘请求直连被 IP 白名单拒）")


def init_salt():
    """生成随机 salt 写入 accounts.json（值不输出到 stdout——不经 AI 上下文）。"""
    import secrets
    data = _read_accounts()
    tiger = data.setdefault("tiger", {})
    if tiger.get("live_unlock_salt"):
        print("salt 已存在（不覆盖——轮换须手动改 accounts.json）")
        return
    tiger["live_unlock_salt"] = secrets.token_hex(32)
    with open(ACCOUNTS_PATH, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print("✅ salt 已生成写入 accounts.json 的 tiger.live_unlock_salt（值为随机 64 位 hex，未打印）")


def live_gate_for_order_scripts(account, script_name):
    """下单脚本前置闸（2026-08-21 立检查点①）：解析到 --account live 且解锁无效时，
    打印 {ok:false, blocked_by:"live_locked"} 结构化拒单并 sys.exit(1)。

    6 个下单脚本（open/close/move_stop × 港美）在解析完 --account 参数后第一时间调用：
        import live_unlock
        live_unlock.live_gate_for_order_scripts(account, "open_position_tiger")
    account 非 'live' 时直接返回（模拟盘不受限）。解锁有效（用户刚授权过）也直接返回。
    双保险定位：网络层闸（apply_proxy 不挂代理 → 白名单连接层拒）是物理拦截，本闸是
    主动拒单（少烧一次注定失败的网络请求 + 给 AI 机器可读的处理指引）。
    """
    if account != "live":
        return
    ok, reason = live_unlock_valid()
    if ok:
        return
    print(json.dumps({
        "ok": False,
        "blocked_by": "live_locked",
        "reason": reason,
        "error": (
            f"⛔ 实盘未解锁（{reason}）——默认态实盘开仓/交易物理不可达。"
            f"授权路径：① AI 发 AskUserQuestion 实盘授权问题（含打码号 67****91 与授权范围、"
            f"选项须含确认授权项）→ ② 用户点击确认（hook live_auth_witness 自动写短时凭证）→ "
            f"③ python3 scripts/live_unlock.py verify-auth 写解锁（24h 有效、本会话绑定）。"
            f"撤销：python3 scripts/live_unlock.py revoke。"
            f"若本单确是误传 --account live：去掉该参数即回模拟盘。"),
    }, ensure_ascii=False))
    sys.exit(1)


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(0)
    if args[0] == "--init-salt":
        init_salt()
    elif args[0] == "status":
        status()
    elif args[0] == "verify-auth":
        ok, msg = verify_auth_and_unlock()
        print(msg)
        sys.exit(0 if ok else 1)
    elif args[0] == "revoke":
        # ⛔ 停盯边界时间闸（2026-08-24 立，T118）：盘中撤解锁 = 放弃下单能力，属停盯
        # 收尾动作——与 unregister / caffeinate off 同受 stop_gate 约束（盯到收盘或用户
        # 喊停；空仓 / 无信号不是理由。11:48 违规提前停盯把解锁也 revoke 掉的教训）。
        # 用户明确喊「撤销实盘 / 停止盯盘」时加 --force 放行（用户指令优先）。
        if "--force" not in args:
            try:
                import stop_gate
                allowed, reason, mins, market = stop_gate.check(False)
                if not allowed:
                    print(f"⛔ 已拒绝 revoke：{reason}")
                    print("   确属用户喊停 / 用户喊撤销，加 --force 显式放行")
                    sys.exit(2)
            except ImportError:
                pass   # stop_gate 不在（极端部署）→ 不拦（revoke 是安全方向，宁放勿断）
        if "--all" in args:
            _, msg = revoke_all()
        else:
            _, msg = revoke()
        print(msg)
    else:
        print(f"未知子命令：{args[0]}（--init-salt / status / verify-auth / revoke [--all]）")
        sys.exit(1)


if __name__ == "__main__":
    main()
