# 当前状态 & 待办

## 已实现功能

| 模块 | 状态 | 备注 |
|------|------|------|
| **backtest** 事件驱动主循环 | ✅ | weekly / biweekly / monthly cadence；event-replay 资金账户 |
| **predict** 实时模式 | ✅ | per-market sub-agent，与 backtest 共用流水线 |
| **Deep-research pipeline** | ✅ | planner → research × N (并行) → critic |
| Sub-agent harness | ✅ | `subagent.py` 通用原语，可派生任意层级 |
| Evidence ledger 三件套 | ✅ | sources / evidence / claims JSONL，per-market |
| Cluster-independent 计数 | ✅ | URL 标准化 + 域聚类，自动 cap research strength |
| Claim-evidence 校验 | ✅ | analyzer 引用不存在 evidence_id 触发 retry-once 拒绝 |
| Critic 盲审 | ✅ | 看不到 evidence，能 keep / lower / flip_to_skip |
| Kelly 自动定 size | ✅ | agent 只报 (model_prob, confidence)，方向 + 金额由代码算 |
| 多 provider | ✅ | OpenAI / Anthropic + ofox 网关（一份 key 两 endpoint） |
| Backtest baselines | ✅ | always-skip / market-prob / anti-favorite / random |
| 跨决策日交易日记 | ✅ | 上次结算回顾注入 system prompt（`--no-journal` 关）|
| 提前平仓 / 止盈 | ✅ | 持仓价 ≥ 0.85 自动 early_close，二次扣 spread+fee |
| Portfolio cap | ✅ | 单决策日总仓位 ≤ 50% starting equity |
| Out-of-window 月报 | ✅ | settle 在 end_month 之后的 bet 单独标注 |
| 跨次预测对照 | ✅ | predict 跑前扫历史 runs/predict-*，输出 prior_predictions_review.md |
| 多维度 P&L 分析 | ✅ | confidence/category 分桶 + calibration 表 + 资金时间序列 |
| 时间约束 | ✅ | backtest cutoff = 决策日；search 相对日期按 cutoff 解析 |
| 搜索后端 | ✅ | backtest=SerpAPI（带日期过滤），predict=Tavily（实时） |
| 语言策略 | ✅ | search query 英文；assessment/reasoning/claims 中文 |
| 多模型支持 | ✅ | deepseek / kimi / claude opus 4.7 / gpt-5.5 |
| 资金会计 | ✅ | starting_capital / available_cash / total_equity 三量 |
| 手续费模拟 | ✅ | spread 动态化（rails 处 +2.5%）+ taker 0.01% + gas |

## 已知问题

### 🔴 P0 - SerpAPI 限流
backtest 模式下 deep-research 每市场会发 ~10-15 次搜索，5 个市场 ~75 次/决策日，免费层很快 429。
临时缓解：用 `--min-volume` 收紧候选数量。
长期方案：
- 升级 SerpAPI 套餐
- 加 retry + jitter
- 给 SearchFn 加 brave / google-cse 备用后端

### 🟡 P1 - LLM 过度自信
calibration 数据显示 model_prob 高 bucket 系统性偏 +20pp 以上。
后续优化：跑足够样本后做 isotonic regression 校准 model_prob。

### 🟡 P1 - 回测 look-ahead
模型预训练截止可能晚于测试月份，等于在已知答案上做"预测"。
缓解：用更早的历史窗口；或用预训练截止之后的月份。

### 🟢 P2 - 决策日 horizon 默认 30 天
之前 90 天导致 "out-of-window" 的 bet 拖到回测期外。
已默认收紧到 30 天，可通过 `config.yaml` 的 `backtest.horizon_days` 调整。

## 待办

### 高优先级

- [ ] Calibration 校准层：用历史数据做 model_prob 后处理（isotonic regression）
- [ ] SerpAPI retry / 多 search 后端 fallback
- [ ] Live trading 模式（真钱小金额验证）
- [ ] 加更多 baseline：volume-weighted random / momentum

### 中优先级

- [ ] 把 `tools.py` 旧单 agent 路径完全拆掉（AgentContext / _place_bet 已不再走，但还在）
- [ ] 配置统一到 pydantic schema
- [ ] 跨市场 entity 抽取（"Trump 相关" 多市场分组算 risk-correlated cap）
- [ ] Web UI 查看 trace（per-market trace 树状浏览）
- [ ] 更长窗口实证（6-12 个月、多模型对照）

### 低优先级

- [ ] 单元测试基建
- [ ] Docker 部署
- [ ] 回测结果可视化（calibration plot、cumulative PnL）
- [ ] Sharpe 计算改成 weekly 归一化（目前是 monthly × √12）

## 不会做

- 复制 199-biotechnologies/deep-research 的 8-phase 瀑布流和 McKinsey HTML 报告 — 那是给"长篇研究报告"设计的，不适合"批量评估市场"的场景
- 把所有 tool result 全文塞回 main agent — 走 sub-agent + evidence_id 引用模式更干净
