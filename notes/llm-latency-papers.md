# LLM 决策延迟与推理权衡：两篇论文的调研笔记

> 本文档收录两篇与「LLM 盯盘交易的延迟问题」直接相关的研究论文，包括出处、实验设计、关键结论，以及对本项目（Victor / DTAgent）的适用边界与落地启发。
>
> 缘起：2026-09-12 调研 GitHub 项目 8treenet/deeptrade（LLM 交易 ETH 期货的 Go 机器人）时引出的「双模型分工是否被证明有效」问题，检索到这两篇论文。背景是本项目长期存在的**秒级跳变问题**——盘面快速变化时，信号基于的快照到用户执行时已经失配。

---

## 论文一：Agents Are Not Algorithms

### 出处

- 标题：**Agents Are Not Algorithms: The Tradeoffs of Decision-Time Reasoning in AI Trading**
- 作者：Ing-Haw Cheng、Maurice Granger、Justin Shi、Vasily Strela（多伦多大学 Rotman 商学院）
- 发表：2026 年 5 月，SSRN working paper
- 链接：<https://papers.ssrn.com/sol3/papers.cfm?abstract_id=6713620>
- 一级来源核实：作者主页 inghawcheng.github.io、Rotman FinHub 实验室页面的摘要与 SSRN 一致

### 实验设计

在 RIT（Rotman Interactive Trader）**实时市场模拟器**上，让 LLM agent 做 tender（报价单）的挑选与执行。交叉两个变量：

| 变量 | 档位 |
|---|---|
| 推理强度 | 关 / 低 / 中 |
| 市场速度 | 慢 / 正常 / 快 |

### 关键结论

1. **推理有「分红」（reasoning dividend）**：tender 挑选、条件执行这类判断题，多推理确实提高决策质量。
2. **推理也有「税」（deliberation tax）**：深思耗时、市场在走——快速市场里**中等推理的 agent 反而亏钱**，亏损来自操作错误（试图接受已过期的 tender、库存卡住）；低推理 agent 在正常和快市场里全面占优。
3. **架构结论（最有 actionable 的一条）**：不要让 LLM 处理低延迟执行环。当 agent 只负责 tender 判断、配上预计算（pre-computed calculations）和编译好的确定性平仓执行（compiled unwind execution）时，agentic 系统才能匹敌全确定性算法的基准。推荐混合部署：**LLM 管稀疏的高判断决策，传统算法管密集的时间敏感动作**。
4. **没有普适的推理预算**：小模型受益于额外推理（补弱先验），强模型（Anthropic 系）零推理时最好。「开仓一律上重推理」不是无条件成立。

---

## 论文二：Win Fast or Lose Slow

### 出处

- 标题：**Win Fast or Lose Slow: Balancing Speed and Accuracy in Latency-Sensitive Decisions of LLMs**
- 作者：Hao Kang、Qingru Zhang、Han Cai、Weiyuan Xu、Tushar Krishna、Yilun Du、Tsachy Weissman（MIT / Princeton 团队）
- 发表：arXiv:2505.19481
- 链接：<https://arxiv.org/abs/2505.19481>

### 实验设计

首个系统研究 LLM 实时决策「延迟-质量」权衡的工作。提出两个基准：

- **HFTBench**：高频交易模拟；
- **StreetFighter**：格斗游戏（实时对抗）。

并提出 FPX 框架——按实时需求动态选模型大小与量化级别。

### 关键结论

1. 最优延迟-质量平衡点**随任务而异**，没有全局最优。
2. 在延迟敏感任务里，**为低延迟牺牲模型质量可以显著提升下游净收益**——交易日收益最高提升 26.52%（此数字出自本文，注意是 HFT 模拟场景）。

---

## 对 DTAgent（Victor）的映射

### 秒级跳变问题 = 延迟税

Victor 的信号链路（信号模式、人工执行）：

```
盘面跳变 →（AI 察觉）→（AI 分析生成信号）→（送达用户）→（用户理解并手动执行）
   毫秒         秒            秒~几十秒          秒             分钟
```

秒级跳变的本质就是论文一实测的「深思耗时、市场在走」：**信号基于的盘面快照，到用户执行时已经变了**。链路最后一段（人工执行）的耗时是刚性约束、压不掉，所以只能优化前面几段。

### 论文一架构结论 ↔ 本项目对应物

| 论文一的架构结论 | 本项目的对应物 |
|---|---|
| 时间敏感执行环不交给 LLM，用编译好的确定性算法 | 跳变检测、止损止盈调整下沉为纯代码规则（阈值、ATR 倍数），毫秒级触发，不经 LLM |
| LLM 只管稀疏的高判断决策 | Victor 只在「要不要进场、形势判断是否改变」这类节点做 LLM 分析 |
| 预计算支撑决策 | Markowitz 侧的回测查表（类别 → 胜率 / avg_R / 样本数）就是现成的预计算 |

## 适用边界（不过度外推）

- **论文一**：场景是做市 / tender 执行，有「报价几秒内必须接、库存必须即时处理」的硬秒级约束。本项目的分钟 K 交易里多数时刻不需要秒级响应，**只有跳变时刻需要**——适用的是架构原则，不是具体参数；场景并不同构。
- **论文二**：HFT 模拟，粒度比本项目细得多。「为低延迟牺牲质量净收益更高」的方向可借鉴（跳变时刻用轻量快速通道，别跑重推理），但 26.52% 这类数字对分钟级场景没有参考价值。
- **两篇都没有证明**「双模型分工能赚钱」——双模型分工最稳的收益是省 API 费与降延迟（定价事实），交易层面的效果是未检验假设，检验手段仍是消融对比 + 回测（Markowitz 侧职责）。

## 落地启发（四条）

1. **触发与判断分离**：跳变检测用纯代码阈值规则（毫秒级），LLM 只在触发后介入做形势判断——消除「等 LLM 察觉」这段延迟税。
2. **止损止盈规则化**：持仓跟踪中的止损调整（移动止损、ATR 倍数）写成确定性规则，不依赖每次 LLM 重推理——对应论文一 compiled unwind execution。
3. **信号自带时效（TTL）**：信号标注失效条件（如「基于价格 X 生成，成交价偏离 X 超过 Y% 即作废」）——人工执行慢是刚性约束，信号必须承认自己会过期。
4. **用回测量化延迟成本**：用历史数据统计「信号发出后第 N 秒 / N 分钟才执行」相对「立即执行」的收益衰减曲线，把「延迟值多少」从定性判断变成有数字的量；第 3 条的 Y 值可直接从该曲线取。此条属 Markowitz（QuantStrategistAgent）侧职责，跨项目协作项。

---

## 检索记录

- 2026-09-12：经 Exa 搜索（查询 reasoning LLM trading performance / fast slow model routing）发现；论文一先由 mlquants.substack.com 解读文章引入，后向 SSRN / 作者主页 / Rotman FinHub 一级来源核实；论文二直接来自 arXiv。
