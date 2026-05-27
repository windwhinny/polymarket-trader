# 模块说明

## Agent 层 (`src/core/`)

### `subagent.py` — 通用 sub-agent harness
`run_subagent(role, system_prompt, tools, tool_handlers, finalize_tool, ...)`：

- 跑一轮 tool-calling LLM 循环直到 `finalize_tool` 被调用或 max_turns 到
- 维护 message thread + 持久化完整 trace JSON 文件
- 工具 handler 通过 `SubAgentCtx` 拿到 `subagent_dir`，可派生更深层 sub-agent
- 不持任何全局状态——所有依赖（LLM client、output dir）显式注入

所有领域 agent（analyzer / research / critic）都基于这个原语。

### `market_analyzer.py` — Planner agent
每个候选市场一个实例。tools:

- `plan_research(tasks)`：派发 2-4 个研究方向，每个交给独立 research sub-agent 并行执行
- `submit_analysis(model_prob, confidence, reasoning, claims)`：提交结论
  - 每条 claim 必须 cite 真实 evidence_id；编造 id 触发 retry-once 拒绝

### `research_agent.py` — Research sub-agent
单方向调研（for_yes / for_no / base_rate / neutral）。tools:

- `search_news(query)`：英文搜索（prompt 强制），结果带 evidence_id 入 EvidenceStore
- `finish_research(evidence_ids, assessment, strength, caveats)`：提交
  - strength 自动按 cluster-independent 计数 cap（cluster=0 → weak, cluster=1 → ≤medium）

### `critic_agent.py` — 盲审 sub-agent
看不到 evidence，只看 (question, market_prob, model_prob, reasoning)。tools:

- `finish_critic(approves, concerns, suggested_action, rationale)`
- suggested_action ∈ {keep, lower_confidence, flip_to_skip, flip_direction}

`apply_critic_action()` 把 action 应用回 verdict（自动降一档 confidence、改 SKIP 等）。

### `screener.py` — Quick screening agent
全量市场 → 5-10 个候选。每个市场返回 `{selected, reason}`，跳过的也给原因。
LLM 失败时回退到 top-N by volume。

### `evidence_store.py` — append-only ledger
三件套（sources / evidence / claims JSONL），每个市场一份。

- **Source de-dup**：URL 标准化（去 query/fragment）+ 域聚类（cnn.com / amp.cnn.com → cnn.com）
- **Cluster-independent 计数**：5 篇 cnn 文章 = 1 cluster ≠ 5 独立来源
- **Claim-evidence 校验**：`add_claim` 检查所有 evidence_id 是否存在，不存在的列入 `missing` 让 caller 决定是拒绝还是接受
- **Thread-safe**：多 research sub-agent 并发写

### `agent.py` — 旧的 SYSTEM_PROMPT 模板
保留作 reference，新 pipeline 已不直接用（每个 sub-agent 自己定义 prompt）。

## 数据层 (`src/core/`)

### `market_data.py` — Polymarket Gamma API
- `fetch_markets_active_at(decision_dt, horizon_days=30)` — 决策日活跃市场
  - `endDate ∈ (decision_dt, decision_dt + horizon_days]`
  - `volume ≥ min_volume`
  - 必须有已知 resolution（用 `_extract_resolution`：UMA → resolvedOutcome → 价格回退）
- `fetch_markets(year, month, ...)` — 旧的月份接口，保留兼容

### `price_data.py` — Polymarket CLOB API
- `fetch_price_at(token_ids, decision_dt)` — 任意时间点
- `fetch_prices_at_month_end(...)` — 旧月底接口，保留兼容

### `search.py` — SerpAPI 后端
- `search_context(query, end_date, ...)`：Google 搜索 + tbs 日期参数 + 文章级日期过滤
- `_parse_article_date(date_str, now=cutoff)`：相对日期"2 days ago"按 cutoff 解析

### `search_backend.py` — Search 后端工厂
- `make_serpapi_backend(key, cache)` — 用于 backtest（支持日期过滤）
- `make_tavily_backend(key)` — 用于 predict（实时，不限流）
- 都返回 `SearchFn = (query, cutoff_iso) -> list[{title,snippet,date,source}]`

## 交易层 (`src/core/`)

### `simulator.py` — 费用 + 结算
- `effective_spread(price)`：随价格距离 0.5 的远近线性放大（rails 处 +2.5%）
- `simulate_bet()`：建仓时算 entry_price + 扣手续费
- `settle_bet()`：到期结算（赢 = shares × $1）
- `early_close_bet(bet, current_yes_price, exit_at)`：提前平仓（卖出价 = current × (1 - spread)，二次 taker fee）

### `tools.py` — 共用常量 + 旧 AgentContext
- `CONFIDENCE_KELLY_FRACTION`：high=1.0 / medium=0.5 / low=0.25
- `MIN_EDGE_TO_BET = 0.03`、`MAX_BET_PCT_OF_EQUITY = 0.15`
- `AgentContext`、`_place_bet` 等是旧单 agent 路径残留，新 pipeline 不再走

### `kelly.py` — 参考实现
新 pipeline 在 `tools.py` 常量上自己算，这个模块作 reference。

### `baselines.py` — 4 个对照基线
- `always-skip`：从不下注
- `market-prob`：model_prob = market YES，等价 always-skip
- `anti-favorite`：YES ≤ 0.10 时下 NO（longshot premium 假设）
- `random`：50/50 picking 强制 +5pp edge

`run_baseline_decision_day()` 与 LLM 路径同 schema，可插拔。

## 报告层 (`src/core/`)

### `report_writer.py` — 共用 markdown/JSON 决策日报告
`write_decision_report(decisions, capital, out_dir, ...)` 输出 recommendations.md + predictions.json。
backtest 和 predict 共用此格式。

### `analysis.py` — 多维度 P&L 分析
回测结束后跑：
- 总览（initial / final / ROI / Sharpe）
- 按 confidence 分桶（high/medium/low/unknown）
- 按 category 分桶
- Calibration 表（model_prob 区间 → 实际胜率，按 pnl > 0 而非 resolution 字符串比较）
- 资金时间序列（按 settled_at 排）

### `tracer.py` — 主 trace JSONL 写入
旧 backtest runner 用的；新 pipeline 用 sub-agent 自带 trace 替代主 trace 大部分内容。

### `reporter.py` — 月报聚合
`generate_final_report(monthly_reports, initial_capital)` → BacktestResult。
`save_report()` 终端打印 + 写 backtest_result.json。

## 入口

### `backtest/runner.py` — 事件驱动主循环
- `_decision_dates(start, end, cadence)` — weekly / biweekly / monthly
- `_decision_data(dt)` — 拉市场 + 拉决策日价格
- `_run_decision_day(dt, ...)` — screen → per-market analyzer → Kelly → portfolio cap
- `_replay_until(cursor)` — 按 settle_due_at 时序结算
- `_early_exit_check(cursor)` — 持仓价 ≥ 阈值就提前平仓
- `_aggregate_by_settle_month(end_month)` — 月报，超 end_month 归 "out-of-window"
- `_run_baseline_track(name)` — 跑 baseline，复用同 decision_dates / market data
- `_build_journal(settled_bets)` — 跨决策日交易日记（可 `--no-journal` 关）

### `predict/runner.py` — 实时预测
单决策点版本：`_fetch_active_markets` → `screen_markets` → 并行 `analyze_market` → `write_decision_report`。
入口前先调 `predict/review.py` 输出 `prior_predictions_review.md`（跨次对照）。

### `predict/review.py` — 跨次预测对照
扫历史 predict run 的 predictions.json，对每个 prior bet 拉当前价，输出"上次说 65%, 现在 72%, T-14d, 已结算 NO ❌"。

## 基础设施 (`src/core/`)

### `config.py` — YAML + .env + 缓存
- `load_config()`：YAML 加载 + ${VAR} 展开 + env 覆盖（DEEPSEEK / OFOX / SERPAPI / TAVILY 等）
- `Cache`：md5 key 文件缓存，TTL based

### `logger.py` — 日志
- 控制台 + 文件 (`<repo>/data/backtest.log`)
- 不在 `src/data/` 下（修过一次）

### `llm.py` — 多 provider client
- OpenAI / Anthropic 兼容接口
- 支持 ofox 网关（一份 key、两个 endpoint：`/anthropic` 走 Claude，`/v1` 走 GPT）
- v4-pro thinking 模式 reasoning_content 透传

### `types.py` — Dataclasses
`Market` / `Bet` / `MonthlyReport` / `BacktestResult` 等。
`Bet` 有 placed_at / settle_due_at / settled_at / resolution / pnl 时间字段。
