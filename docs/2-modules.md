# 模块说明

## Agent 层

### `agent_loop.py`
Agent 工具调用循环 + 系统 prompt 模板。定义了 Agent 的角色、时间约束、市场列表、交易规则、风险管理要求。

### `agent_tools.py`
Agent 可用的 5 个工具：

| 工具 | 用途 | 时间约束 |
|------|------|---------|
| `search_news(query)` | 搜索新闻/信息 | SerpAPI tbs + 文章日期过滤 |
| `get_market_detail(slug)` | 获取市场详情（价格/规则） | 返回月底价格快照 |
| `place_bet(slug, direction, amount, reasoning)` | 下注 | 结算后立即返回结果 |
| `get_portfolio()` | 查看资金/持仓 | 实时 |
| `finish_trading(summary, decisions)` | 结束本月 | — |

### `llm.py`
多模型统一客户端，支持：
- **openai** 格式: DeepSeek, GPT-4o, 任何 /v1 兼容 API
- **anthropic** 格式: Claude 系列

自动转换消息格式和 tool definitions。

## 数据层

### `info_gatherer.py`
搜索模块，当前实现：
- SerpAPI (Google): 支持 `tbs` 日期过滤 + 文章级日期后过滤
- `_parse_article_date()`: 解析 "Jan 15, 2026" / "2 days ago" 等格式
- `_filter_by_date()`: 过滤 cutoff 之后的文章

### `market_fetcher.py`
Polymarket Gamma API:
- 拉取已结算市场 (`closed=true`)
- 按成交量排序，分页获取
- 日期过滤: `endDate > month_end`
- 成交量过滤: `volume >= min_volume`

### `price_fetcher.py`
Polymarket CLOB API:
- `/prices-history`: 获取 token 历史价格
- 查找最接近目标时间戳的价格数据点

## 交易层

### `simulator.py`
模拟交易引擎：
- Taker fee: 0.01%
- Spread cost: 1%
- Gas: $0.005
- DEMAND 公式: `entry_price = market_prob * (1 + spread)` for YES, `(1-market_prob) * (1 + spread)` for NO
- 赢: P&L = shares × 1.0 - amount - fees
- 输: P&L = -(amount + fees)

### `kelly.py`
Kelly 公式（Agent 模式下未直接使用，Agent 自行决策仓位）:
- YES: f = (p - m) / (1 - m)
- NO: f = (m - p) / m

## 基础设施

### `tracer.py`
运行追踪系统：
- `trace.jsonl`: 每步 Agent 交互记录
- `bets.jsonl`: 下注记录
- `config.yaml`: 运行配置快照
- `result.json`: 最终结果
- `manifest.json`: 运行元信息

### `runner.py`
回测编排器：
- Phase 1: 并行拉取所有月份的市场数据
- Phase 2: 串行执行 Agent（资金复利）
- 集成 tracer 记录全流程

### `reporter.py`
结果报告：月报/年报 P&L、Sharpe 比率、最大回撤。
