# 模块说明

## Agent 层 (`src/core/`)

### `core/agent.py` — Agent 循环
工具调用循环 + 系统 prompt 模板。定义 Agent 角色、时间约束、市场列表、交易规则、风险管理要求。

### `core/tools.py` — 工具定义
Agent 可用的 5 个工具：

| 工具 | 用途 | 时间约束 |
|------|------|---------|
| `search_news(query)` | 搜索新闻/信息 | SerpAPI tbs + 文章日期过滤 |
| `get_market_detail(slug)` | 获取市场详情（价格/规则） | 返回月底价格快照 |
| `place_bet(slug, direction, amount, reasoning)` | 下注 | 结算后立即返回结果 |
| `get_portfolio()` | 查看资金/持仓 | 实时 |
| `finish_trading(summary, decisions)` | 结束本月 | — |

### `core/llm.py` — 多模型客户端
支持 OpenAI / Anthropic 格式，自动转换消息和 tool definitions。

## 数据层 (`src/core/`)

### `core/search.py` — 搜索引擎
- SerpAPI (Google): `tbs` 参数 + 文章级 `_filter_by_date()` 双重日期过滤
- `_parse_article_date()`: 解析 "Jan 15, 2026" / "2 days ago" 等格式

### `core/market_data.py` — 市场数据
Polymarket Gamma API:
- `fetch_markets()`: 拉取已结算市场，按 `endDate > month_end` 过滤 + 成交量过滤

### `core/price_data.py` — 历史价格
Polymarket CLOB API:
- `fetch_prices_at_month_end()`: 拉取多个 token 的历史价格，按时间戳匹配

## 交易层 (`src/core/`)

### `core/simulator.py` — 交易引擎
- Taker fee: 0.01% | Spread cost: 1% | Gas: $0.005
- 赢: P&L = shares × 1.0 - amount - fees
- 输: P&L = -(amount + fees)

### `core/kelly.py` — Kelly 公式
Agent 模式下 Agent 自行决策仓位，此模块为参考实现。

## 基础设施 (`src/core/`)

### `core/tracer.py` — 运行追踪
每次运行保存: `trace.jsonl`, `bets.jsonl`, `config.yaml`, `result.json`, `manifest.json`

### `core/config.py` — 配置管理
YAML 加载 + `.env` 读取 + `${VAR}` 展开 + 文件缓存

## 回测层 (`src/backtest/`)

### `backtest/runner.py` — 回测编排器
- Phase 1: 并行拉取所有月份的市场数据
- Phase 2: 串行执行 Agent（资金复利），集成 tracer 记录全流程

### `core/reporter.py` — 报告生成
月报/年报 P&L、Sharpe 比率、最大回撤。
