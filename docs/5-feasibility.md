# LLM 驱动 Polymarket 交易的可行性评估

记录于 2026-05-28，基于本仓库 deep-research 架构跑过的两次小规模回测。

## TL;DR

**作为生产策略不建议直接用真钱。** 作为研究工具和多 sub-agent 框架原型有价值。

理由：当前回测数字虚高（样本小、look-ahead 风险、SerpAPI 限流后退化为 base knowledge 回忆）；
LLM 在 Polymarket 上面临的真实瓶颈是结构性的——信息边际有限、calibration 系统性过度自信、费用吃掉小 edge。

## 已有回测数据

| 跑次 | 周期 | bets | 胜率 | ROI | Sharpe | random baseline |
|---|---|---|---|---|---|---|
| 2026-01 monthly | 1 月 | 8 | 88% | +149% | 2.06 | -31% |
| 2026-04 biweekly | 3 月 | 16 | 75% | +50.9% | 1.52 | -42% |

数字看起来很好，但有三个不要轻信的理由：

1. **样本量极小**（8 / 16 笔）。75% 胜率在 16 个样本下 95% CI ≈ [50%, 90%]，与 50% 没有统计显著差异。
2. **市场选样有强 look-ahead**：模型预训练截止已在测试月之后，等于让它"预测"它已知道结果的事件，是回忆而非预测。
3. **SerpAPI 限流后大量 research 用 base knowledge** 替代外部证据，加深了第 2 条问题。

## 把 LLM 当 Polymarket 交易员的真实瓶颈

### 1. 信息边际几乎为零

Polymarket 上有真金白银的 informed traders（顶级 sportsbook 套利、政治内部消息、链上鲸鱼）。
LLM agent 用公开新闻做研究，时效性比职业玩家慢几小时到几天。
在 effective-market 假说下，公开信息能读出的 alpha 通常 ≤ spread + fees。

### 2. 费用吃掉小 edge

成本模型：
- Spread: 0.5 时 1%，0.05/0.95 时 3.25%
- Taker fee: 0.01%
- Gas: ~$0.005

5pp edge 的 bet（model 35% / market 30%）→ 扣 1% spread 后真实 edge 4pp →
Kelly 仓位 ~5.7%。需要 50 笔成功的 +5pp bet 才能让 $1000 翻倍。
**任意一次大亏（30%+ 仓位）就吃光半年努力。**

### 3. Calibration 系统性过度自信

我们的 2026-04 calibration（修过 EARLY_CLOSE 计入后）显示：

```
70-80% bucket: avg belief 70%, actual win rate ~0%   → +70pp 过自信
90-100% bucket: avg belief 97%, actual win rate 75%  → +22pp 过自信
```

这是 LLM 通病（OpenAI 2024 / Anthropic 2024 calibration paper），
不是本框架问题。但 Kelly 公式喂给它"虚高的 model_prob"，长期一定被市场惩罚。

### 4. LLM 适合 vs 不适合的市场细分

**LLM 有相对优势：**
- 常识 + 算术：长尾市场（如"某非洲小队赢世界杯" 0.1% 定价）
- 明显错误定价：球队已淘汰但市场惯性 ≥ 10%
- 多步推理 / 条件市场：人类懒得算的隐含独立性
- 冷门信息密集：地区政治、技术里程碑

**LLM 没有优势：**
- 二元宏观（FOMC、大选）— 流动性最高、信息最对称
- 体育即时盘口 — sportsbook 已 price in
- 加密价格预测 — market makers 持续套利

## 实操建议

如果要继续推进：

1. **Calibration 校准**：跑完一段后用历史 calibration 数据给 model_prob 应用 isotonic regression，把过度自信掰回。单点 ROI 最高的优化。

2. **专攻 longshot 市场**：明确策略——只对 YES ≤ 0.10 或 ≥ 0.90 的市场启动 deep research。
   anti-favorite baseline 跑 -28% 表明一刀切反向不行，需 case-by-case。

3. **小金额验证 1-3 个月**：$200-500 真钱，每周 2-3 笔，记录每笔的 placed_at + reasoning + 结算。跑满 50 笔再判断 alpha。

4. **不用 v4-flash 上真钱**：base knowledge 不够新、不够深。
   至少用 v4-pro / claude opus 4.7 / gpt-5.5。

5. **预期 ROI 现实区间**：-5% ~ +15% 年化。**不要相信回测里的 +149% 数字。**

6. **算时间成本**：每个市场分析 ~$0.10-0.50 LLM 费用 + 1-3 分钟 walltime。
   每周决策日跑 50 个市场 ≈ $25 LLM + 1-2 小时。年化 +5% 可能不划算。

## 框架本身的独立价值（与赚钱无关）

这套 multi sub-agent + evidence ledger 架构可迁移到任何"基于公开信息做评估"的场景：

- 尽职调查
- 专利检索
- 医学循证
- 合规审查
- 任何需要"派发并行研究 + 引用追溯 + 独立来源计数 + 盲审 critic"的工作流

并且它把"AI 自主交易/分析"从 black box 拆成了可审计的 5-agent pipeline：
planner → research × N → synthesis → critic → verdict，
每步都有独立 trace 文件、claim-evidence 链接、cluster-independent 计数、机械校验。

## 如果继续做研究

要做的扩展实验：

- **跑长窗口**：6-12 个月历史，每周决策日，~50 决策日 × ~5 笔 = ~250 笔
- **严格 cutoff**：用 cutoff_date = 决策日，确认不是事后回忆
- **关掉 journal**（`--no-journal`）以保实验独立性
- **收集 calibration 数据**：看 LLM 在不同 category / confidence 下的偏差模式
- **对照不同模型**：v4-pro / opus 4.7 / gpt-5.5 同窗口跑一遍

真要试真钱：挑 Polymarket 上 LLM 显著有优势的细分（longshot + 复合条件 + 冷门地区政治），用 nominal 资金（$100-500）。
