# 系统架构

## 分层架构

```
┌─────────────────────────────────────────────┐
│              trader.py (CLI)                 │  ← 命令行入口
├─────────────────────────────────────────────┤
│            backtest/runner.py (编排器)        │  ← 回测编排 + trace
├──────────────┬──────────────┬────────────────┤
│ core/agent   │ core/tools    │ core/llm.py   │  ← Agent 层
│ (工具调用循环)│ (工具定义)     │ (多模型)       │
├──────────────┼──────────────┼────────────────┤
│ core/search  │ core/market   │ core/price    │  ← 数据层
│ (搜索)        │ _data (市场)   │ _data (价格)   │
├──────────────┴──────────────┴────────────────┤
│ core/simulator.py (交易) | core/tracer.py    │  ← 横切层
├─────────────────────────────────────────────┤
│ core/config.py / core/logger.py / core/types │  ← 基础设施
└─────────────────────────────────────────────┘
```

## 核心流程

### 单月 Agent 回测

```
1. 市场发现: Gamma API → 筛选当月活跃、未结算市场
2. 价格采集: CLOB API → 获取月底价格快照
3. Agent 循环 (每市场):
   ├── 系统 prompt: 资金/市场列表/时间约束/目标
   ├── Agent 调用 search_news → SerpAPI (日期过滤)
   ├── Agent 调用 get_market_detail → 本地数据
   ├── Agent 调用 place_bet → 模拟下单(含手续费)
   └── Agent 调用 finish_trading → 结束本月
4. 月报生成: 胜率/ROI/P&L
```

### 时间约束机制

- SerpAPI `tbs` 参数: 限定搜索结果的发布日期范围
- 文章日期过滤: 解析 `date` 字段, 过滤 cutoff 之后的文章
- 市场过滤: 只选取结算日在当月结束之后的市场

### Trace 可溯源

每次运行在 `runs/{model}-{timestamp}/` 下保存:
- `trace.jsonl`: 每步 Agent 交互 (system prompt, tool calls, responses)
- `config.yaml`: 完整运行配置
- `result.json`: 最终 P&L 结果
- `bets.jsonl`: 每笔下注记录
