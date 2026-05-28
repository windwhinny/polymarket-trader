# LLM 驱动 Polymarket 交易的可行性评估

记录于 2026-05-28，基于本仓库 deep-research 架构跑过的两次小规模回测，以及一次
DeepSeek v4 pro 实时 predict 试跑。

## TL;DR

**作为生产策略不建议直接用真钱。** 作为研究工具和多 sub-agent 框架原型有价值。

理由：当前回测数字虚高（样本小、look-ahead 风险、历史搜索召回不稳定）；
LLM 在 Polymarket 上面临的真实瓶颈是结构性的——信息边际有限、calibration 系统性过度自信、费用吃掉小 edge。

## 已有回测数据

| 跑次 | 周期 | bets | 胜率 | ROI | Sharpe | random baseline |
|---|---|---|---|---|---|---|
| 2026-01 monthly | 1 月 | 8 | 88% | +149% | 2.06 | -31% |
| 2026-04 biweekly | 3 月 | 16 | 75% | +50.9% | 1.52 | -42% |

数字看起来很好，但有三个不要轻信的理由：

1. **样本量极小**（8 / 16 笔）。75% 胜率在 16 个样本下 95% CI ≈ [50%, 90%]，与 50% 没有统计显著差异。
2. **市场选样有强 look-ahead**：模型预训练截止已在测试月之后，等于让它"预测"它已知道结果的事件，是回忆而非预测。
3. **历史搜索受搜索引擎约束很大**：Google/SerpAPI 的 `tbs=cdr` 可能在指定日期范围返回 0 条，
   但无日期过滤又能搜到相关页面；严格 fallback 又必须丢弃无绝对日期、相对日期或晚于 cutoff 的结果。
   所以回测 evidence recall 不稳定，不能把"搜索为空"当作"当时没有信息"。

## 2026-05-28 DeepSeek v4 pro 实时预测补充

跑法：

```bash
python3 trader.py predict --gateway deepseek --model deepseek-v4-pro \
  --capital 1000 --min-volume 500000 --parallel 3 \
  --run-id predict-ds-v4-pro-live
```

结果目录：`runs/predict-ds-v4-pro-live-2026-05-28-140526`

结果摘要：

- 46 个 active markets，8 个 deep analyzed。
- 0 笔下注，46 个 skip，总下注金额 $0。
- 主要 skip 原因不是"没有观点"，而是模型概率与市场价差距多在 1-3pp 内，扣除 spread/fee 和
  min-edge 后没有交易价值。
- 深度分析覆盖 Spain / France / Argentina / England 世界杯、Spurs NBA Finals、Iran regime fall、
  Strait of Hormuz 等市场。

对推理内容的判断：

- **方向上合理**：模型整体偏保守，没有为了交易而交易；对热门球队、宏观政治、短期地缘事件的判断
  大多落在市场附近。
- **不能直接自动下注**：最终报告缺少 top evidence 链接，Tavily 来源质量混杂，且出现过一个
  skip 市场 reasoning 为空的问题。
- **更适合作为研究 triage**：它可以帮助快速筛掉 edge 不足的市场，但真正下单前仍应审计
  `sources.jsonl` / `evidence.jsonl` / `claims.jsonl` / `critic.json`。

这次 live predict 的正面信号是"会克制"；负面信号是"证据展示和来源质量还不够交易级"。
因此下一步比继续刷回测 ROI 更重要的是：报告证据引用、source quality 控制、calibration 校准。

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

2. **证据质量控制**：最终报告展示 top evidence；对 Tavily/SerpAPI 结果做 allowlist、blacklist、
   credibility weighting；空 reasoning / 无 claims 的 verdict 自动重试或标 invalid。

3. **历史搜索多后端**：先尝试 Google News `tbm=nws + tbs`，再走普通 Google `tbs`，
   最后才用严格 no-tbs fallback；同时评估 Brave / Google CSE / 新闻档案源。

4. **专攻 longshot 市场**：明确策略——只对 YES ≤ 0.10 或 ≥ 0.90 的市场启动 deep research。
   anti-favorite baseline 跑 -28% 表明一刀切反向不行，需 case-by-case。

5. **小金额验证 1-3 个月**：$200-500 真钱，每周 2-3 笔，记录每笔的 placed_at + reasoning + 结算。跑满 50 笔再判断 alpha。

6. **不用 v4-flash 上真钱**：base knowledge 不够新、不够深。
   至少用 v4-pro / claude opus 4.7 / gpt-5.5。

7. **预期 ROI 现实区间**：-5% ~ +15% 年化。**不要相信回测里的 +149% 数字。**

8. **算时间成本**：每个市场分析 ~$0.10-0.50 LLM 费用 + 1-3 分钟 walltime。
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
- **搜索召回审计**：记录 `search_mode`、空结果查询、fallback 命中率，区分"无新闻"和"搜索引擎没返"
- **关掉 journal**（`--no-journal`）以保实验独立性
- **收集 calibration 数据**：看 LLM 在不同 category / confidence 下的偏差模式
- **对照不同模型**：v4-pro / opus 4.7 / gpt-5.5 同窗口跑一遍

真要试真钱：挑 Polymarket 上 LLM 显著有优势的细分（longshot + 复合条件 + 冷门地区政治），用 nominal 资金（$100-500）。
