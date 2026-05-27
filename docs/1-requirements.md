# 功能需求 & 设计决策

## 功能需求

| ID | 需求 | 状态 |
|----|------|------|
| FR-1 | Agent 自主调用搜索 API 收集信息 | 完成 |
| FR-2 | 严格时间约束：只能获取某日期前的信息 | 完成 |
| FR-3 | Agent 自主决策下注策略（model_prob + confidence） | 完成 |
| FR-4 | 模拟真实交易（含动态 spread / taker / gas） | 完成 |
| FR-5 | 回测任意时间段（事件驱动决策日序列） | 完成 |
| FR-6 | 月报 / 多维度 P&L 分析 | 完成 |
| FR-7 | 多模型支持（OpenAI / Anthropic / ofox 网关） | 完成 |
| FR-8 | 每次运行可溯源（per-market trace + evidence ledger） | 完成 |
| FR-9 | CLI 可配置：模型/日期/cadence/baseline | 完成 |
| FR-10 | 年化 10-20% 收益目标 | 未达成（见 docs/5-feasibility.md）|
| FR-11 | 多月数据并行获取 | 完成 |
| FR-12 | 多模型对比跑 | 完成（独立 run-id 跑后人工对照） |
| FR-13 | 搜索日期过滤（文章级而非仅 API 级） | 完成 |
| FR-14 | Deep-research 多 sub-agent 流水线 | 完成 |
| FR-15 | Evidence ledger + claim-level 校验 | 完成 |
| FR-16 | Cluster-independent source 计数 | 完成 |
| FR-17 | 盲审 critic | 完成 |
| FR-18 | Backtest baseline 对照 | 完成 |
| FR-19 | 跨次预测对照（predict） | 完成 |
| FR-20 | 提前平仓 / 止盈 | 完成 |
| FR-21 | Portfolio cap | 完成 |
| FR-22 | 跨决策日交易日记 | 完成 |

## 设计决策（ADR）

### ADR-1: Agent 模式 vs 管道模式
**决策**: Agent 自主调用工具，而非预先收集数据喂给模型。
**理由**: 更接近真实交易场景；每个 sub-agent 可独立 trace。

### ADR-2: 市场日期过滤策略
**决策**: backtest 用 `endDate ∈ (decision_dt, decision_dt + horizon_days]`，
默认 horizon_days=30。
**理由**: 月份对齐有存活者偏差（专门挑"月底未结算"的市场）；
30 天 horizon 与 prompt 推荐的 T-30d 偏好一致。

### ADR-3: 搜索 API 选择
**决策**: backtest=SerpAPI（带 tbs 日期过滤），predict=Tavily（实时无日期）。
**理由**: SerpAPI 的日期过滤是回测正确性的硬要求；Tavily 在实时模式下时效性更好。

### ADR-4: 手续费模型
**决策**: 动态 spread + 0.01% taker + $0.005 gas。
spread 在 0.5 时 1%，rails 处 3.25%（线性插值）。
**理由**: 常数 1% 在低概率市场低估真实成本。

### ADR-5: 单笔上限锚定 starting_capital
**决策**: 15% 上限基于 starting_capital 而非 available_cash。
**理由**: 否则 5 笔下注后阈值已被前 4 笔的扣减打到很小，agent 无法均匀分散。

### ADR-6: 事件驱动 vs 月份切片
**决策**: 决策日序列 + event-replay 资金账户。
**理由**: 月份切片混淆了下注月和结算月；真实交易里资金随结算时间演进，不按日历切。

### ADR-7: Per-market sub-agent vs 统一主 agent
**决策**: 每市场一个独立 analyzer（planner），跑独立的 evidence ledger。
**理由**: 主 agent 模式的 context 容易被多个市场的 evidence 互相污染；
独立 agent 让每笔决策的 trace 自包含、可单独审计。

### ADR-8: Deep-research = planner + research × N + critic
**决策**: planner 不直接搜索，派发 2-4 个有立场的 research sub-agent 并行调研，
synthesize 后 critic 盲审。
**理由**: 强迫 pro/con 各自独立调研，避免单 agent 单方面叙事；
critic 看不到 evidence 是有意为之，承担逻辑一致性检查（不与 analyzer 锚定到同一证据）。

### ADR-9: Evidence ledger 三件套
**决策**: per-market 维护 sources/evidence/claims JSONL（参考
199-biotechnologies/deep-research-skill 但裁剪掉报告级的部分）。
**理由**: 让 claim-evidence 链接机械可验证；让 cluster-independent 计数取代
naive evidence count；保留 audit trail。

### ADR-10: Cluster-independent 计数
**决策**: research strength 由独立 cluster 数 cap（cluster=0 → weak, cluster=1 → ≤medium）。
**理由**: 5 篇 cnn.com 文章不是 5 个独立来源；同 wire 报道大量重复，
不做去重的 strength 是膨胀的。

### ADR-11: Kelly 自动定 size
**决策**: agent 只报 (model_prob, confidence)，方向 + 金额由 Kelly + confidence multiplier 算。
**理由**: agent 自报 amount 给了它独立"虚报置信度"的渠道；统一从 prob 推导避免这个问题，
也让 model_prob 变成可校准的（calibration plot）。

### ADR-12: Critic 不看 evidence
**决策**: critic 只看 (question, market_prob, model_prob, reasoning)。
**理由**: 看 evidence 容易和 analyzer 收敛到同一叙事；纯逻辑审查独立性更高。

### ADR-13: 提前平仓
**决策**: 持仓价 ≥ 0.85 自动 early_close，二次 taker fee + 半 spread。
**理由**: 持有到结算锁住后期收益；提前兑现把胜率换 ROI。

### ADR-14: 跨决策日交易日记
**决策**: 默认开启（过去 8 笔 settled bet 摘要注入 system prompt）；
`--no-journal` 关掉以保实验独立性。
**理由**: 真实交易员会从过往学习；但破坏了"决策日独立"的实验假设，给开关。

### ADR-15: 语言策略
**决策**: search query 强制英文；assessment / reasoning / claims 用中文。
**理由**: 英文来源覆盖广 + 时效好 + 独立 cluster 多；中文输出方便人类用户阅读和审计。
