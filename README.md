# Polymarket Trader

基于 AI Agent 的 Polymarket 预测市场交易系统。

**核心理念**：LLM 作为自主交易 Agent，调用搜索引擎收集信息、分析市场定价偏差、自主决策下注。

## 模式

| 模式 | 命令 | 状态 |
|------|------|------|
| backtest | `python trader.py backtest ...` | 可用 |
| trade | `python trader.py trade ...` | 规划中 |

## 快速开始

```bash
pip install -r requirements.txt
cp .env.example .env  # 填写 API key

# 回测 2026 Q1
python trader.py backtest --model deepseek-chat --start 2026-01 --end 2026-04

# 指定模型
python trader.py backtest --provider anthropic --model claude-sonnet-4-20250514 --start 2026-01 --end 2026-04
```

## 项目结构

```
polymarket-trader/
├── trader.py               # CLI 入口 (backtest / trade)
├── run_agent.py            # 旧版入口 (兼容)
├── config.yaml             # 默认配置
├── src/
│   ├── core/               # 可复用核心模块
│   │   ├── agent.py        # Agent 循环 + system prompt
│   │   ├── tools.py        # 工具定义 (search, bet, portfolio)
│   │   ├── llm.py          # 多模型客户端 (OpenAI/Anthropic)
│   │   ├── simulator.py    # 交易模拟 (含手续费)
│   │   ├── market_data.py  # 市场数据 (Gamma API)
│   │   ├── price_data.py   # 历史价格 (CLOB API)
│   │   ├── search.py       # 搜索 (SerpAPI + 日期过滤)
│   │   ├── config.py       # 配置 + .env 加载 + 缓存
│   │   ├── tracer.py       # 运行追踪 (JSONL)
│   │   ├── reporter.py     # 结果报告
│   │   ├── logger.py       # 日志
│   │   ├── types.py        # 数据类型
│   │   └── kelly.py        # Kelly 公式
│   └── backtest/           # 回测模块
│       └── runner.py       # 月度回测编排器
├── runs/                   # 各次运行输出
├── docs/                   # 设计文档
├── data/                   # 缓存
└── .env.example            # 环境变量模板
```

## 文档

| 文件 | 内容 |
|------|------|
| [docs/0-architecture.md](docs/0-architecture.md) | 系统架构 |
| [docs/1-requirements.md](docs/1-requirements.md) | 功能需求 & 设计决策 |
| [docs/2-modules.md](docs/2-modules.md) | 模块说明 |
| [docs/3-usage.md](docs/3-usage.md) | 使用指南 |
| [docs/4-status.md](docs/4-status.md) | 当前状态 & 待办 |
