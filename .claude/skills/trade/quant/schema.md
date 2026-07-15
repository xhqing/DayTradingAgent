# 量化策略 输入输出契约 + 回测设计（2026-07-14 定）

## 三条铁律（用户立，最高优先级）

1. **时间必带市场时区**：港股用北京/港时（`+08:00`），美股用美东（`EDT -04:00` / `EST -05:00`）。ISO 8601 带偏移。禁止无时区时间戳。
2. **输出全量化**：每个字段要么是**数值**（价格/量/比率/百分比），要么是**有限枚举**（离散值，数值编码）。**禁止定性文字**（如"5 日均线上行""动能减弱"）。这是量化区别于主观交易的本质。
3. **schema 与回测协同设计**：只有输入（K 线 OHLCV）和输出（本 schema）都可量化，回测才能**精准复现**（同数据 + 同参数 → 同结果）。

> 判定依据不许写文字，必须转成一组数值 `features`（如均线值、斜率、量比、ATR）。定性结论（方向/状态）只作为这些数值的**离散枚举结果**输出。

---

## 一、战略层 schema（日 K，今日地图）

| 字段 | 类型 | 量化说明 |
|---|---|---|
| `symbol` | string | 标的标识，富途前缀 `HK.02800` / `US.SPY` |
| `bar.date` | date | 该日 K 的日期，如 `2026-07-14` |
| `bar.tz` | string | 市场时区：港股 `Asia/Hong_Kong (+08:00 北京/港时)`；美股 `America/New_York (EDT -04:00 / EST -05:00 美东)` |
| `direction` | int 枚举 | `+1` 多 / `-1` 空 / `0` 中性 |
| `regime` | int 枚举 | `+1` 趋势上升 / `-1` 趋势下降 / `0` 区间震荡 |
| `levels.support` | float | 支撑位（价格） |
| `levels.resistance` | float | 阻力位（价格） |
| `levels.breakout` | float | 突破触发位（价格） |
| `levels.stop_loss_ref` | float | 止损参考位（价格，技术位；最终止损 AI 综合） |
| `levels.take_profit_ref` | float | 止盈参考位（价格） |
| `entry_condition` | string 枚举 | 进场条件类型：`pullback_hold`(回踩企稳) / `breakout`(突破) / `range_low`(区间支撑买) / `range_high`(区间阻力卖) / `none` |
| `entry_params` | object(全数值) | 该条件的量化参数（如 `pullback_zone_low/high`、`confirm_bars`），由策略定义 |
| `features` | object(全 float) | **判定的量化依据**（替代文字 reason）：如 `ma5/ma20/ma5_slope/volume_ratio_5d/atr20/adx`，具体由策略定，**必须全是数字** |
| `category` | string | 类别签名，由量化特征拼接（如 `trend_up_5d+vol_breakout+pullback_holds`），可复现、用于查回测表 |
| `confidence` | object | `{win_rate: float, samples: int, avg_R: float}`，**回测赋予**；策略原始输出 `null` |

**JSON 例子（港股盈富，战略层）**：
```json
{
  "symbol": "HK.02800",
  "bar": {"date": "2026-07-14", "tz": "Asia/Hong_Kong (+08:00 北京/港时)"},
  "direction": 1,
  "regime": 1,
  "levels": {"support": 24.20, "resistance": 25.10, "breakout": 25.10, "stop_loss_ref": 23.90, "take_profit_ref": 25.80},
  "entry_condition": "pullback_hold",
  "entry_params": {"pullback_zone_low": 24.20, "pullback_zone_high": 24.40, "confirm_bars": 2},
  "features": {"ma5": 24.50, "ma20": 24.10, "ma5_slope": 0.012, "volume_ratio_5d": 1.8, "atr20": 0.35},
  "category": "trend_up_5d+vol_breakout+pullback_holds",
  "confidence": {"win_rate": 0.62, "samples": 40, "avg_R": 1.3}
}
```

---

## 二、战术层 schema（分钟 K，盘中扳机）

| 字段 | 类型 | 量化说明 |
|---|---|---|
| `symbol` | string | 同上 |
| `bar.time` | ISO 8601 | 带偏移，如 `2026-07-14T10:35:00+08:00` |
| `bar.tz` | string | 同战略层（港股 `+08:00` / 美股 EDT·EST） |
| `bar.ktype` | string | `1m` / `5m` / `15m` |
| `direction` | int 枚举 | `+1` / `-1` / `0` |
| `action` | int 枚举 | `+1` 进场 / `-1` 离场 / `0` 不动 |
| `entry_price` | float | 触发价 |
| `trigger` | string 枚举 | 触发类型：`1m_vol_breakout` / `pullback_ma_cross` / `range_touch` 等（有限枚举） |
| `trigger_params` | object(全数值) | 触发的量化参数（如 `breakout_level`、`vol_threshold`、`confirm_bars`） |
| `levels` | object(全 float) | 分钟级 `support/resistance/stop_loss_ref/take_profit_ref` |
| `features` | object(全 float) | 量化依据（如 `ma5m/vol_ratio_1m/spread`），必须全数字 |
| `category` | string | 类别签名，可复现 |
| `confidence` | object | `{win_rate, samples, avg_R}`，回测赋予，策略输出 `null` |

**JSON 例子（港股盈富，战术层）**：
```json
{
  "symbol": "HK.02800",
  "bar": {"time": "2026-07-14T10:35:00+08:00", "tz": "Asia/Hong_Kong (+08:00 北京/港时)", "ktype": "1m"},
  "direction": 1,
  "action": 1,
  "entry_price": 24.56,
  "trigger": "1m_vol_breakout",
  "trigger_params": {"breakout_level": 24.55, "vol_threshold": 1.5, "confirm_bars": 1},
  "levels": {"support": 24.42, "resistance": 24.70, "stop_loss_ref": 24.38, "take_profit_ref": 24.70},
  "features": {"ma5m": 24.48, "vol_ratio_1m": 2.1, "spread": 0.01},
  "category": "1m_vol_breakout+pullback_5m_ma",
  "confidence": {"win_rate": 0.55, "samples": 120, "avg_R": 1.1}
}
```

---

## 三、时间字段时区规则

| 市场 | 时区 | 标注 | 交易时段（本地） |
|---|---|---|---|
| 港股 | `Asia/Hong_Kong` = UTC+8 | `+08:00 北京/港时` | 09:30-12:00 / 13:00-16:00 |
| 美股 | `America/New_York` | `EDT -04:00`（夏令时，3 月第二周日–11 月第一周日）/ `EST -05:00`（冬令时） | 09:30-16:00 美东 |

- 所有 `bar.time` 用 **ISO 8601 带偏移**（`2026-07-14T10:35:00+08:00`），偏移量本身就编码了时区。
- 富途 K 线返回的 `time_key` 无时区后缀，按**市场本地时间**解读（港股=港时、美股=美东）；落盘时**补上偏移**。
- `bar.tz` 字段冗余写明人类可读时区名，双重保险防误读。

---

## 四、回测设计（与 schema 协同，保证精准复现）

### 4.0 优化目标与可用性门槛（先于一切，2026-07-14 用户立）

**主优化目标：`avg_R`（平均风险倍数盈亏 = 期望值 expectancy 的 R 标准化）。**
- 公式：`avg_R = mean(R_i)`，`R_i = 单笔盈亏 / 单笔风险`（单笔风险 = `entry_price − stop_loss_ref` 做多）。`avg_R > 0` 才有 edge，越大越好。
- **不是胜率**：高胜率低赔率陷阱——胜率 90% 但赢 0.3R/亏 2R 的策略（EV +0.07R），不如胜率 40% 赢 2.5R/亏 1R（EV +0.4R）。单独优化胜率会推向"赚小钱跑、亏大钱扛"的亏损策略。
- **不是总收益率**：被仓位规模与少数极端交易主导、忽略回撤；用 R 标准化剥离仓位规模，看策略纯质量。

**可用性门槛（策略"能用"的最低要求，参考值可按风险偏好调）**：

| 指标 | 门槛 | 说明 |
|---|---|---|
| `avg_R` | ≥ 0.2 | 每笔预期 ≥0.2 倍风险；>0.5 优秀 |
| Profit Factor（总盈利/总亏损） | ≥ 1.3 | >1.5 良好，>2 优秀 |
| 样本数 | ≥ 30 | 统计意义下限（分钟策略通常远超） |
| Max Drawdown | ≤ 20% | 风险上限 |
| 胜率 | ≥ 35% | avg_R 正但胜率过低→连亏风险大、难执行 |

**综合参考（不优化、只看）**：Sharpe（风险调整收益）、Calmar（收益/回撤）。

**一句话**：在"样本足、回撤可控、胜率可执行"的门槛约束下，**最大化 `avg_R`**。回测产出每笔 R 倍数记录 → 既算 avg_R（优化用），又按 category 分组填 `confidence.win_rate/avg_R`（实时用）。

### 4.1 确定性输入（数据缓存）
- 首次从富途（分钟 K）/长桥（日 K）拉取 → 存 **parquet 快照** 到 `quant/data_cache/`（含时区），回测只读缓存。
- 好处：数据源变动 / 限流不影响回测；**同快照 → 同结果**。
- `data_cache/` 大文件不入库（`.gitignore`），可从源重生。

### 4.2 策略是纯函数
```python
def evaluate(bars: pd.DataFrame, params: dict) -> dict | None:
    # bars = 截至当前根的历史 K 线（含时区）；params = 策略参数
    # 返回战略层/战术层 schema dict，或 None（无信号）
```
- **无随机、无全局状态、无副作用** → 同 `bars` + 同 `params` 永远同输出。

### 4.3 逐根遍历，严防未来函数
- 按时序 `for t in range(start, end)`：`signal = evaluate(bars[:t], params)`，**t 时刻决策只能用 `≤ t` 的数据**。
- `features` / `levels` 必须只用历史数据算（如 `ma5` 只用 t 及之前 5 根）。回测框架自动检测：若输出引用了 `bars[t+1:]` 报错。

### 4.4 成交价假设（统一、保守）
- 信号在 t 根收盘后产生 → **t+1 根开盘价成交**（战略层 = 次日开盘；战术层 = 下一分钟开盘）。
- 这是**保守近似**：实盘是盘中实时执行（可能更优），但回测统一用下一根开盘，避免美化、保证可比。滑点/手续费作为可配置扣减项。

### 4.5 出场条件全量化
持仓中逐根检查（任一触发即平）：
- `stop_loss_ref` 被触及（价格 ≤ 止损，做多）；
- `take_profit_ref` 被触及；
- `max_hold_bars`（最大持仓 K 线数，如日 K 5 根 / 分钟 120 根）超时。
出场价 = 触发条件对应根的开盘价（同 4.4 保守口径）。

### 4.6 每笔记录（全数值）
`{symbol, category, direction, entry_price, exit_price, R, hold_bars, entry_time(tz), exit_time(tz)}`
- `R` = 盈亏 / 单股风险（`entry_price - stop_loss_ref` 做多），标准化盈亏倍数。

### 4.7 置信度查表（confidence 来源）
- 按 `category` 分组聚合所有历史交易 → 统计 `win_rate` / `avg_R` / `samples` → 存 `quant/confidence_table.json`。
- 盘中实时：策略出信号 + `category` → 查表填 `confidence`。
- **样本量门槛**：`samples < 20` → 标 `low_sample: true`，AI 自动降权；查无的 category → `confidence=null`（弃权）。

### 4.8 复现保证
**同 `data_cache` 快照 + 同 `params` + 同策略代码 = 完全相同的回测结果。** 版本化快照与参数（写入结果目录），任何时点重跑可对齐。

---

## 五、可量化性自查清单（写每个策略时逐条对照）

- [ ] 每个输出字段是 **数值** 或 **有限枚举**？（无定性文字）
- [ ] 判定依据在 `features` 里全是数字？（不允许"趋势强"这种词）
- [ ] `levels` 全是价格数值？
- [ ] 时间字段带市场时区（ISO 8601 偏移 + `tz`）？
- [ ] `evaluate` 是纯函数、无随机、无未来函数？
- [ ] 进出场条件都有量化阈值（`entry_params`/`trigger_params`/止损止盈价/`max_hold_bars`）？
