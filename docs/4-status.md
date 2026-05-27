# 当前状态 & 待办

## 已实现功能

| 模块 | 状态 | 备注 |
|------|------|------|
| **backtest** 模式 | ✅ | 历史回测，Agent 自主搜索+下注，月度复利 |
| **predict** 模式 | ✅ | 实时预测，Agent 筛查市场→并行分析→输出建议文档 |
| Agent 工具调用循环 | ✅ | ReAct 模式，backtest: 30 turns, predict: 5 turns |
| 5 个 Agent 工具 | ✅ | search_news, get_market_detail, place_bet, get_portfolio, finish_trading |
| predict 专用工具 | ✅ | search_news (Tavily), place_prediction |
| 多模型支持 | ✅ | OpenAI/Anthropic 格式，v4-pro 为默认 |
| v4-pro thinking mode | ✅ | reasoning_content 自动回传 |
| Trace 可溯源 | ✅ | JSONL trace + config + result |
| CLI 参数 | ✅ | model/provider/date/capital 全覆盖 |
| 市场日期过滤 | ✅ | backtest: endDate > month_end; predict: 3个月内 |
| 搜索日期过滤 | ✅ | backtest: SerpAPI tbs + 文章级过滤; predict: Tavily |
| 手续费模拟 | ✅ | Taker 0.01% + Spread 1% + Gas $0.005 |
| 资金风控 | ✅ | 单笔 ≤15% |
| 并行执行 | ✅ | 市场数据拉取、predict 市场分析均多线程 |
| 两阶段筛选 | ✅ | predict: Agent 先筛查全量市场，再并行深度分析 |
| .env 密钥管理 | ✅ | .env 加载 + .env.example 模板 |

## 当前模式对比

| | backtest | predict |
|------|----------|---------|
| 用途 | 历史回测验证策略 | 实时预测，月末验证 |
| 数据源 | 已结算市场 (closed=true) | 活跃市场 (active=true) |
| 时间约束 | 按日历月，信息截止月底 | 实时，只看3个月内结算 |
| 搜索 | SerpAPI + 日期过滤 | Tavily |
| 结算 | 月末统一结算 (无 bias) | 不结算（真实结果月末对比） |
| 资金 | 月度复利 | 固定 $1000 |
| 输出 | P&L 报告 + trace | recommendations.md + .json |

## 已知问题

### 🔴 P0 - predict: DeepSeek v4-pro thinking mode 过慢
Agent 在 5 turns 内经常超时未输出结果。已增加 timeout=120s，但部分市场仍会分析超时显示"分析超时"。

### 🟡 P1 - predict: 搜索引擎短时不可用
SerpAPI 被限流，已切换到 Tavily。但 Tavily 偶发 401。

### 🟡 P1 - backtest: 2025 年数据太少
Top 200 按成交量全是 2026 年的市场，2025 年可分析市场不足。

### 🟢 P2 - predict: 部分 Agent 直到 max_turns 才输出
v4-pro thinking 模式单轮 30-60s，需要强制限制搜索次数。

## 待办

### 高优先级
- [ ] 解决 v4-pro 超时问题（降级到 v3 或减少 thinking）
- [ ] backtest 恢复可用（需修 API timeout）
- [ ] 多模型并行对比
- [ ] predict 模式下注建议实时推送到负一屏（today-task skill）

### 中优先级
- [ ] predict 增加重试：Agent 超时的市场重新分析
- [ ] 回测结果可视化
- [ ] live trading 模式
- [ ] Web UI 查看 trace

### 低优先级
- [ ] 单元测试
- [ ] Docker 部署
- [ ] 自定义 prompt 模板
