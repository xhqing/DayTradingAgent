#!/usr/bin/env python3
"""盯盘 worktree 启动器（2026-11-03 用户立，trade skill 盯盘启动第一动作）。

## 规矩来源

用户 2026-11-03 指令原话：
「本项目盯盘任务必然会在工作区写入文件，为了减少盘中干扰，在用户发出盯盘指令的时候
直接先准备好 worktree 并切换到 worktree 开始盯盘。」

## 为什么需要它

2026-11-02 立的团队项目规则：任何项目里的任何写操作，第一步就是生成 worktree；local main
只剩「生成 worktree」与「被 remote main 快进同步」两个职责，且 `git-main-guard.ts` 已硬拦
main 上的 `git add` / `git commit`。

而盯盘会**持续写入**文件：`actions/<date>-<market>-actions.md`（交易动作）、
`signals/*.md`（信号）、`tmp/`（采样 log / 状态文件 / 收尾标记）等。在主工作区（main）里做
这些写入会污染 main、导致后续 add/commit 全部被拦。所以盯盘必须整体搬进 worktree。

## 它做什么

1. 判断当前是否已经在 worktree 里——是则直接复用（幂等，避免重复建）；
2. 在主仓库同级建 `<仓库名>-watch-<YYYYMMDD>-<market>` + 分支
   `chore/watch-<YYYYMMDD>-<market>`；同名的已存在则复用；
3. **把被 `.gitignore` 忽略的本机私有文件软链进 worktree**——这是关键一步：这些文件
   git 不跟踪、worktree 检出里没有，但盯盘离不开它们：
     .pi/skills/trade/accounts.json        老虎凭证 + 账户号（下单 / 查账户 / 算购买力）
     .pi/skills/trade/config.json          风控参数（risk_fraction / f_min / 费率开关等）
     .pi/skills/trade/feishu.local.json    飞书接收人配置（电源提醒 / 停盯总结推送）
     reviews/ip-node-log.local.csv             JMS 节点使用记录（追加写；软链让数据回流主仓库）
   软链而非复制：凭证只有一份源、不会分叉；worktree 删除也不影响主仓库。
4. **软链 `state/`** 回主仓库——`state/` 放「删了会让功能失效」的持久状态（熔断 / 标的池
   认领 / 盯盘注册表 / 互斥 WAL / 收尾与飞书标记 / 影子仓 / 连败计数 / IP 池缓存），
   必须跨 worktree 共享；`tmp/` 则每个 worktree **独立一份**（真临时：采样 log / 心跳 /
   后台任务日志 / 待发草稿，删了不影响功能，2026-11-03 用户立的口径划分）；
5. 打印 `WORKTREE=<绝对路径>`，AI 据此把后续所有盯盘命令拼上该前缀。

## 用法

    python3 worktree_start.py [--market us|hk] [--branch-name <自定义分支名>]

输出关键行（供 AI 解析）：
    WORKTREE=/Users/xhq/Developer/DayTradingAgent-watch-20261103-us
    BRANCH=chore/watch-20261103-us

## 停盯时怎么收尾

盯盘期间产生的 `actions/`、`signals/` 改动都在 worktree 分支上，停盯后按流程收尾：
在 worktree 里跑 `/add` → `/commit`（非 main 分支，自动触发允许）→ push → PR → 合并；
合并后 `git worktree remove <路径>` 清理。**全程不要回主工作区提交。**

目录口径（2026-11-03 用户立）：**`state/` 软链共享、`tmp/` 每场独立**。
`state/` 里是删了会失效的状态（最严重的是 `circuit_breaker.json`——实盘熔断丢了等于
实盘开仓闸失效）；`tmp/` 里是真临时文件（采样 log、心跳、后台任务日志、待发草稿），
删了不影响功能。首次实现曾把两者都放 tmp/ 并让 worktree 独立，结果 preflight 立刻
误报「停盯总结已写但未见飞书推送记录」——这正是「状态文件不该放 tmp」的实证。
"""

import argparse
import os
import subprocess
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

# 被 .gitignore 忽略、但盯盘必需的本机私有文件（相对主仓库根）
LOCAL_FILES = [
    ".pi/skills/trade/accounts.json",
    ".pi/skills/trade/config.json",
    ".pi/skills/trade/feishu.local.json",
    "reviews/ip-node-log.local.csv",
]


def run(cmd, cwd=None):
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    return r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip()


def main():
    ap = argparse.ArgumentParser(description="盯盘 worktree 启动器")
    ap.add_argument("--market", default=None, choices=["us", "hk"],
                    help="市场（us / hk）；省略则按当前时刻推断")
    ap.add_argument("--branch-name", default=None,
                    help="自定义分支名（默认 chore/watch-<YYYYMMDD>-<market>）")
    args = ap.parse_args()

    rc, root, err = run(["git", "rev-parse", "--show-toplevel"])
    if rc != 0:
        print(f"⛔ 当前目录不是 git 仓库：{err}", file=sys.stderr)
        return 1

    repo_name = os.path.basename(root)

    # ── 1. 已经在 worktree 里？直接复用（幂等）──────────────────────────
    rc_gd, git_dir, _ = run(["git", "rev-parse", "--git-dir"], cwd=root)
    rc_cd, common_dir, _ = run(["git", "rev-parse", "--git-common-dir"], cwd=root)
    in_worktree = False
    if rc_gd == 0 and rc_cd == 0:
        # 主工作区：--git-dir 与 --git-common-dir 指向同一处；worktree：前者在
        # <common>/worktrees/<name> 下。统一转绝对路径再比。
        gd = os.path.realpath(os.path.join(root, git_dir))
        cd = os.path.realpath(os.path.join(root, common_dir))
        in_worktree = gd != cd

    if in_worktree:
        rc_br, branch, _ = run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=root)
        print(f"✅ 已在 worktree 中，直接复用（不重复创建）")
        print(f"WORKTREE={root}")
        print(f"BRANCH={branch}")
        return 0

    # ── 2. 推断市场 → 生成 worktree 名 / 分支名 ────────────────────────
    market = args.market
    if market is None:
        # 港股按北京时间、美股按美东时间各自的可交易窗口粗判；判不出就默认 us
        now_utc = datetime.now(ZoneInfo("UTC"))
        hk_hour = now_utc.astimezone(ZoneInfo("Asia/Hong_Kong")).hour
        market = "hk" if 9 <= hk_hour <= 16 else "us"

    if market == "us":
        et_date = datetime.now(ZoneInfo("America/New_York")).strftime("%Y%m%d")
    else:
        et_date = datetime.now(ZoneInfo("Asia/Hong_Kong")).strftime("%Y%m%d")

    branch = args.branch_name or f"chore/watch-{et_date}-{market}"
    wt_name = f"{repo_name}-watch-{et_date}-{market}"
    wt_path = os.path.join(os.path.dirname(root), wt_name)

    # ── 3. 建 worktree（已存在则复用）────────────────────────────────
    if os.path.isdir(wt_path):
        rc_ls, existing, _ = run(["git", "worktree", "list", "--porcelain"])
        if wt_path in existing:
            print(f"✅ worktree 已存在，复用：{wt_path}")
        else:
            print(f"⛔ 目录已存在但不是本仓库的 worktree：{wt_path}", file=sys.stderr)
            print("   请先手工处理（改名 / 删除）后重试。", file=sys.stderr)
            return 1
    else:
        # 分支已存在则直接挂上去（避免 checkout 冲突）
        rc_br, _, _ = run(["git", "rev-parse", "--verify", branch])
        if rc_br == 0:
            rc_add, out, err_add = run(["git", "worktree", "add", wt_path, branch])
        else:
            rc_add, out, err_add = run(["git", "worktree", "add", wt_path, "-b", branch])
        if rc_add != 0:
            print(f"⛔ 建 worktree 失败：{err_add or out}", file=sys.stderr)
            return 1
        print(f"✅ 已建 worktree：{wt_path}（分支 {branch}）")

    # ── 4. 软链本机私有文件（worktree 检出里没有、盯盘必需）────────────
    linked, missing = [], []
    for rel in LOCAL_FILES:
        src = os.path.join(root, rel)
        dst = os.path.join(wt_path, rel)
        if not os.path.exists(src):
            missing.append(rel)
            continue
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if os.path.islink(dst) or os.path.exists(dst):
            linked.append(f"{rel}（已存在，跳过）")
            continue
        os.symlink(src, dst)
        linked.append(rel)

    # ── 5. 状态目录与临时目录（2026-11-03 用户立的口径划分）────────────
    #
    # state/ = 「删了会让功能失效」的持久状态，**必须软链回主仓库**（跨会话/跨中断共享）：
    #   circuit_breaker.json   **实盘熔断状态**（丢了 = 实盘开仓闸失效，最严重）
    #   pool_claims.json       标的池认领表（多会话并行盯盘的互斥依据）
    #   monitor_sessions.txt   caffeinate 引用计数
    #   trade_intent.log       单持仓互斥闸 WAL
    #   shadow_positions.json  影子仓 / losing_streak.json 连败计数
    #   feishu_sent_* / stop_done_*   收尾与飞书漏发检查标记
    #   ip_pool_*.json / jms_subscription.cache   代理池与订阅缓存（盘中禁止重拉订阅）
    #
    # tmp/ = 「删了不影响功能」的临时文件（采样 log / 心跳 / 后台任务日志 / 待发草稿）。
    # **每个 worktree 独立一份**即可——一场盯盘在同一 worktree 内完成，log 天然连续；
    # 次日新 worktree 重置也是期望行为。
    state_src = os.path.join(root, "state")
    state_dst = os.path.join(wt_path, "state")
    os.makedirs(state_src, exist_ok=True)
    state_note = ""
    if os.path.islink(state_dst):
        state_note = "state/ 已软链（共用主仓库持久状态）"
    elif os.path.isdir(state_dst):
        try:
            os.rmdir(state_dst)          # 仅空目录可删
            os.symlink(state_src, state_dst)
            state_note = "state/ 空目录已替换为软链（共用主仓库持久状态）"
        except OSError:
            state_note = "⚠️ state/ 不是软链且非空，未替换——请手工确认"
    else:
        os.symlink(state_src, state_dst)
        state_note = "state/ 已软链（共用主仓库持久状态）"

    # tmp/ 独立（真临时）
    os.makedirs(os.path.join(wt_path, "tmp"), exist_ok=True)
    tmp_note = "tmp/ 独立（真临时文件，删了不影响功能）"

    # ── 6. 输出 ────────────────────────────────────────────────────
    print(f"🔗 本机私有文件软链：{len(linked)} 个")
    for item in linked:
        print(f"   - {item}")
    print(f"🔗 {state_note}")
    print(f"🔗 {tmp_note}")
    if missing:
        print(f"⚠️ 主仓库里找不到以下文件（盯盘可能缺配置）：")
        for item in missing:
            print(f"   - {item}")

    print()
    print("📌 后续所有盯盘命令请用该 worktree 路径（不要回主工作区写）：")
    print(f"WORKTREE={wt_path}")
    print(f"BRANCH={branch}")
    print()
    print(f"   例：python3 {wt_path}/.pi/skills/trade/scripts/preflight.py --mode auto")
    return 0


if __name__ == "__main__":
    sys.exit(main())
