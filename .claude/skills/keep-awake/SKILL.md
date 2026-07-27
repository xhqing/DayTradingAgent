# Keep-Awake（已并入 trade，不再独立触发）

⚠️ **2026-07-27 修订：本 skill 已取消独立触发、防睡眠功能并入 trade 盯盘流程**——盯盘预热（`../trade/scripts/preflight.py`）无条件自动启用 `caffeinate -s`（无论开盖合盖、不询问、不弹电池提醒），停止盯盘时 trade 停盯流程自动调本目录 `scripts/off.sh` 解除。用户不再需要说「启用合盖盯盘」、AI 也不再询问开盖/合盖（默认盯盘即自动防睡眠）。

本目录 `scripts/on.sh`（手动启用防睡眠，备用）、`scripts/off.sh`（停盯解除防睡眠，trade 停盯流程调用）保留。

## 启用

```bash
bash .claude/skills/keep-awake/scripts/on.sh
```

- **AC 供电**：启动 `caffeinate -s`，创建 `PreventSystemSleep` assertion，防合盖(Clamshell)与维护(Maintenance)两类系统级睡眠。
- **电池供电**：合盖是硬件强制睡眠、软件防不住，但仍启动（防空闲维护睡眠部分有效）。强烈建议接电源。

启用后 AI 自主记住「已启用防睡眠」。

**停止盯盘时 AI 自动收尾（重要纪律）**：用户**不会**主动说「停用合盖盯盘」——AI 必须在停止盯盘的时刻自己跑 `off.sh` 收尾，避免长期持 `PreventSystemSleep` assertion。停止盯盘的时机（与 trade skill「时点平仓与停止盯盘约束」一致）：
- 用户喊停（发出停止盯盘指令）；
- 或 AI 自主停止盯盘——**只在快休市 / 收盘前**（港股 12:00 午休前、港股 16:00 收盘前、美股用户喊停或 16:00 ET 收盘前）才允许自主停；其余时段盯盘持续、不自主停、也就不收尾。

收尾动作：`bash .claude/skills/keep-awake/scripts/off.sh`，在停盯总结前执行。

## 停用

```bash
bash .claude/skills/keep-awake/scripts/off.sh
```

`pkill -f "caffeinate -s"`、解除防睡眠。

## 根因背景（2026-07-24 复盘）

盯盘期间系统睡眠（合盖 / 维护）暂停所有进程，富途 OpenD 的 `get_market_snapshot` 无 timeout，卡到 TCP 超时 ~15 分钟才返回、整段采样空窗；claude-proxy、xpilot 同断。`caffeinate -s` 的 `PreventSystemSleep` assertion 能防住系统级睡眠，但 **「只在 AC 电源有效」**（`man caffeinate` 明确）——电池模式合盖是硬件强制、软件防不住。
