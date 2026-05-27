# Polymarket Trader

基于 AI multi sub-agent 的 Polymarket 预测市场分析与回测系统。

**核心理念**：每个候选市场跑独立的 deep-research pipeline——
planner 派发 2-4 个有立场的研究员 (for_yes / for_no / base_rate) 并行调研，
盲审 critic 质疑论证，所有 evidence 进 append-only ledger 做 cluster-independent
计数和 claim-evidence 校验。

## 模式

| 模式 | 命令 | 用途 |
|------|------|------|
| backtest | `python trader.py backtest ...` | 历史事件驱动回测，含 baseline 对照 |
| predict | `python trader.py predict ...` | 实时分析当前活跃市场，输出建议 |
| trade | `python trader.py trade ...` | 规划中：真钱小金额 |

## 快速开始

```bash
pip install -r requirements.txt
cp .env.example .env   # 填写 API key（DEEPSEEK / OFOX / SERPAPI / TAVILY）

# Backtest 2026-04 月，每两周一个决策日，跑全部 4 个 baseline 对照
python trader.py backtest --start 2026-04 --end 2026-04 --cadence biweekly \
  --capital 1000 --min-volume 500000 --baseline all

# Predict 实时模式
python trader.py predict --capital 1000 --min-volume 500000 --parallel 3

# 用 Claude opus 4.7（通过 ofox 网关）
python trader.py backtest --provider anthropic --start 2026-04 --end 2026-04
```

## 决策流水线（per market）

```
screener → analyzer (planner)
              ├── plan_research → research-1 (for_yes)  ┐
              │                   research-2 (for_no)   │ 并行
              │                   research-3 (base_rate)┘
              │                       (每个独立 search loop, evidence 入 ledger)
              ↓
         submit_analysis (claims 必须 cite 真实 evidence_id)
              ↓
         critic (盲审：只看 reasoning, 不看 evidence)
              ↓
       Kelly 把 (model_prob, confidence) 转成 (direction, amount)
              ↓
       portfolio cap → bet placed
```

## 输出结构

每次跑出一个目录，包含完整可审计的 trace：

```
runs/backtest-{model}-{ts}/
├── analysis.md / analysis.json          # 多维度 P&L（含 calibration）
├── baseline_comparison.md               # baseline 对照
├── decisions/{date}/recommendations.md  # 当日建议（人类阅读）
└── decisions/{date}/traces/{slug}/
    ├── analyzer.json + research-{N}-{stance}.json + critic.json
    ├── verdict.json                     # 最终结构化判断
    └── sources.jsonl + evidence.jsonl + claims.jsonl
```

## 项目结构

```
src/core/                       # 核心模块
├── subagent.py                 # 通用 sub-agent harness
├── market_analyzer.py          # Per-market planner agent
├── research_agent.py           # 单方向研究员 sub-agent
├── critic_agent.py             # 盲审 sub-agent
├── screener.py                 # 候选市场快速筛选
├── evidence_store.py           # sources/evidence/claims ledger
├── search_backend.py           # SerpAPI / Tavily 工厂
├── baselines.py                # 4 个对照基线
├── analysis.py                 # 多维度 P&L 分析
├── report_writer.py            # recommendations.md 渲染
├── simulator.py                # 费用 / 结算 / 提前平仓
├── market_data.py              # Polymarket Gamma API
├── price_data.py               # Polymarket CLOB API
├── search.py                   # SerpAPI 搜索 + 日期过滤
├── llm.py / config.py / ...
└── types.py
src/backtest/runner.py          # 事件驱动主循环 + baseline + journal
src/predict/runner.py           # 实时模式
src/predict/review.py           # 跨次预测对照
```

## 文档

| 文件 | 内容 |
|------|------|
| [docs/0-architecture.md](docs/0-architecture.md) | 系统架构 |
| [docs/1-requirements.md](docs/1-requirements.md) | 功能需求 & 设计决策（ADR-1 ~ 15）|
| [docs/2-modules.md](docs/2-modules.md) | 模块说明 |
| [docs/3-usage.md](docs/3-usage.md) | 使用指南 |
| [docs/4-status.md](docs/4-status.md) | 当前状态 & 待办 |
| [docs/5-feasibility.md](docs/5-feasibility.md) | **可行性评估（先读这个）**|

## ⚠️ 现实预期

回测里出现的 +50% / +149% ROI 数字**不可信**——样本量小、look-ahead 风险、
SerpAPI 限流后退化为 base knowledge 回忆。详见 `docs/5-feasibility.md`。

把这套当**研究工具和 multi sub-agent 框架原型**，不要直接当生产策略用。
