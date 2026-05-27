# 系统架构

## 整体流程

```
┌──────────────────────────────────────────────────────────────────┐
│                        trader.py (CLI)                            │
├──────────────────────────────────────────────────────────────────┤
│ backtest.runner            │            predict.runner            │
│  事件驱动决策日序列         │            实时单次扫描              │
│  + baselines 对照          │            + 跨次预测对照            │
├──────────────────────────────────────────────────────────────────┤
│                  per-market deep-research pipeline                │
│  screener  →  analyzer (planner)  →  research × N  →  critic     │
│              evidence ledger 三件套（sources/evidence/claims）    │
├──────────────────────────────────────────────────────────────────┤
│ market_data │ price_data │ search_backend │ simulator │ analysis │
│  (Gamma)    │  (CLOB)    │ (SerpAPI/Tavily)│ (fees+Kelly)│ (slices)│
├──────────────────────────────────────────────────────────────────┤
│ subagent (LLM tool-loop harness) │ evidence_store │ tracer        │
│ llm (multi-provider)             │ config / logger / types       │
└──────────────────────────────────────────────────────────────────┘
```

## Per-market 决策流水线

每个候选市场跑一次独立的 5-agent pipeline。任何决策都可以审计回到一个市场目录下的全部 trace 文件。

```
analyzer (planner) ─┬─→ research-1 (for_yes)  ┐
                    ├─→ research-2 (for_no)   ┤  并行执行
                    └─→ research-3 (base_rate)┘
                       ↓
                synthesize → claims + (model_prob, confidence, reasoning)
                       ↓ mechanical claim-evidence verification
                    critic (盲审：只看 reasoning，不看 evidence)
                       ↓ keep / lower_confidence / flip_to_skip
                  verdict.json
```

**关键不变量**：

- 每个 sub-agent 一个独立 trace 文件，可单独审计
- 所有 search 结果走 `EvidenceStore`，每条 evidence 有稳定 id（E1, E2, ...）
- analyzer 的每条 claim 必须 cite 真实 evidence_id，机械校验（不存在则拒绝重提）
- research strength 由 cluster-independent 计数 cap（5 篇 cnn.com 文章 = 1 个 cluster）
- critic 看不到 evidence，只能从论证质量判断，避免与 analyzer 锚定到同一叙事

## 回测的事件驱动模型

回测**不**按日历月切片，而是按决策日序列推进：

```
1. _decision_dates(start, end, cadence)  → ["2026-04-01", "2026-04-15", ...]
2. 对每个决策日 dt:
   a. _replay_until(pending_bets, cursor=dt)
      把已到期的 bet 真实结算掉，回收资金
   b. _early_exit_check(pending_bets, cursor=dt, threshold=0.85)
      持仓价跑到 0.85+ 就平仓兑现
   c. fetch markets active at dt (horizon 30/60/90 天可调)
   d. screen → analyzer × N (per-market deep research)
   e. 把 analyzer 的 (model_prob, confidence) 通过 Kelly 转 (direction, amount)
   f. portfolio cap：单决策日总仓位 ≤ 50% starting equity
   g. 新 bet 入队 pending
3. 序列结束后 final replay 把剩余 pending 全部结算
4. 按 settled_at 分桶月报；超出 end_month 的归 "out-of-window"
5. 多维度 P&L 分析（confidence / category / calibration / 时间序列）
6. 可选：跑 1-4 个 baseline 对照（always-skip / market-prob / anti-favorite / random）
```

## 时间约束机制

- **Backtest**: cutoff_date = 决策日。SerpAPI `tbs` 参数 + 文章级日期过滤双重限制。
  相对日期"2 days ago"按 cutoff 解析（不是 wall-clock），避免泄露未来。
- **Predict**: cutoff = None（实时模式）。Tavily 不支持日期过滤但实时数据本身就够用。

## 资金会计

| 字段 | 含义 |
|---|---|
| `starting_capital` | 决策日开盘 equity（cash + 已锁仓位）|
| `available_cash` | 可花现金 |
| `total_equity` | cash + open stakes + realized PnL |
| `placed_at` | bet 实际下注时间 |
| `settle_due_at` | 市场原 endDate |
| `settled_at` | 实际结算时间（可能是 endDate，也可能是 early_exit 的决策日）|

15% 单笔上限锚定 `starting_capital` 而非 `available_cash`，避免逐笔阈值递减。

## Trace 可溯源

每次运行：

```
runs/backtest-{model}-{ts}/
├── trace.jsonl              # 主 trace（决策日级事件）
├── analysis.md              # 多维度 P&L 报告（人类阅读）
├── analysis.json            # 同上，机器读
├── result.json              # P&L 摘要
├── backtest_result.json     # 详细月报
├── baseline_comparison.md   # baseline 对照表（如开启）
├── baselines/{name}/
│   └── analysis.md          # 每个 baseline 独立分析
└── decisions/{date}/
    ├── recommendations.md   # 当日下注建议
    ├── predictions.json
    └── traces/{slug}/
        ├── analyzer.json    # planner 主 trace
        ├── research-{N}-{stance}.json
        ├── critic.json
        ├── verdict.json     # 结构化最终判断
        ├── sources.jsonl    # de-duplicated source registry
        ├── evidence.jsonl   # append-only evidence
        ├── claims.jsonl     # 已校验 claim-evidence 链接
        └── ledger_summary.json
```

predict 模式的目录结构相同，少 `decisions/{date}/` 一层（直接 `traces/{slug}/`），
多一个 `prior_predictions_review.md`（跨次预测对照）。
