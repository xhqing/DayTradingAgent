# actions/

港股和美股的模拟盘（auto 模式）交易动作记录。与 `signals/` 分工：

- `signals/` — signal 模式的信号记录：AI 发信号、用户手动执行，信号文件记信号事实（含响铃时刻价、假设持仓），用于复盘 + 用户挂止损参考
- `actions/` — auto 模式的交易动作记录：AI 调脚本对老虎模拟账户自动下单，记录实际成交动作（含 order_id、真实成交价）

文件命名（港美分开记、文件名带 HKT/ET 时区标识，便于分别复盘）：

- 港股：`YYYY-MM-DD-HKT-actions.md`
- 美股：`YYYY-MM-DD-ET-actions.md`

复盘数据源 = `signals/` + `actions/` 两个目录的交易记录（signal 模式交易在 `signals/`、auto 模式交易在 `actions/`）；默认复盘范围 = 港股（只遍历 `*-HKT-*` 文件，用户明确要求复盘美股时才遍历 `*-ET-*`）。详见项目 `CLAUDE.md`「actions/ 目录归属」节与 trade skill「复盘分析」节。
