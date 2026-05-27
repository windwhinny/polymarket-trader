# Polymarket Agent Backtest

基于 AI Agent 的 Polymarket 预测市场回测系统。

**核心思路**：让 LLM 作为自主交易 Agent，在严格的时间约束下（只能看到某月之前的公开信息），调用搜索引擎收集信息、分析市场定价偏差、自主下注。通过回测历史数据验证 AI 的交易能力。

## 快速开始

```bash
# 安装
pip install -r requirements.txt

# 运行（默认 DeepSeek，2026年 Q1，$2000 本金）
python run_agent.py --model deepseek-chat --start 2026-01 --end 2026-04 --capital 2000

# 指定其他模型
python run_agent.py --provider openai --model gpt-4o --start 2026-01 --end 2026-04
python run_agent.py --provider anthropic --model claude-sonnet-4-20250514 --start 2026-01 --end 2026-04
```

## 项目结构

```
polymarket-backtest/
├── run_agent.py          # CLI 入口
├── run.py                # 旧版入口（非 Agent 模式）
├── config.yaml           # 默认配置
├── src/
│   ├── runner.py         # 回测编排器（含 trace）
│   ├── agent_loop.py     # Agent 工具调用循环 + 系统 prompt
│   ├── agent_tools.py    # 工具定义（search_news, place_bet 等）
│   ├── llm.py            # 多模型支持（OpenAI / Anthropic）
│   ├── tracer.py         # 运行追踪（JSONL trace 文件）
│   ├── info_gatherer.py  # 搜索（SerpAPI / Google）
│   ├── market_fetcher.py # 市场数据（Gamma API + 日期过滤）
│   ├── price_fetcher.py  # 历史价格（CLOB API）
│   ├── simulator.py      # 模拟交易（含手续费）
│   ├── reporter.py       # 结果报告生成
│   ├── config.py         # 配置加载 + 缓存
│   ├── logger.py         # 日志配置
│   └── types.py          # 数据类型定义
├── runs/                 # 各次运行输出
│   └── {model}-{timestamp}/
│       ├── config.yaml   # 运行的完整配置
│       ├── trace.jsonl   # Agent 完整交互记录
│       ├── result.json   # 结果摘要
│       └── manifest.json # 运行元信息
├── data/                 # 缓存数据
├── results/              # 旧版结果输出
└── docs/                 # 设计文档
```

## 文档索引

| 文件 | 内容 |
|------|------|
| [docs/0-architecture.md](docs/0-architecture.md) | 系统架构设计 |
| [docs/1-requirements.md](docs/1-requirements.md) | 功能需求 & 设计决策 |
| [docs/2-modules.md](docs/2-modules.md) | 各模块详细说明 |
| [docs/3-usage.md](docs/3-usage.md) | 使用指南 & CLI 参数 |
| [docs/4-status.md](docs/4-status.md) | 当前状态 & 待办事项 |
