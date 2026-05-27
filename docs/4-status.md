# 当前状态 & 待办

## 完成状态

| 模块 | 状态 | 备注 |
|------|------|------|
| Agent 工具调用循环 | ✅ | ReAct 模式，max 30 turns |
| 5 个 Agent 工具 | ✅ | search_news, get_market_detail, place_bet, get_portfolio, finish_trading |
| 多模型支持 | ✅ | OpenAI/Anthropic 格式统一封装 |
| Trace 可溯源 | ✅ | JSONL trace + config + result 完整记录 |
| CLI 参数配置 | ✅ | model/provider/date/capital 可命令行覆盖 |
| 市场日期过滤 | ✅ | endDate > month_end，逐月递减 |
| 搜索日期过滤 | ✅ | SerpAPI tbs + 文章级日期过滤 |
| 手续费模拟 | ✅ | Taker 0.01% + Spread 1% + Gas $0.005 |
| 资金风控 | ✅ | 单笔 ≤15% 资本硬限制 |
| 并行数据拉取 | ✅ | 4 线程拉取各月市场数据 |

## 已知问题

### 🔴 P0 - DeepSeek API 频繁超时
Agent 在第 1-2 轮就超时退出，导致 0 笔下注。需要：
- 增加 API timeout
- 添加重试逻辑
- 或者切换模型

### 🟡 P1 - Agent 有些月份不下注
2 月/3 月市场只剩 50/50 随机游走型（BTC 5min 涨跌），Agent 正确判断无 edge 并跳过。这是合理行为，但导致部分月份无数据。

### 🟡 P1 - Agent prompt 有时被忽略
Agent 偶尔会：
- 反复搜索同一主题
- 下注超过建议的 10%
- 直到 max_turns 才调用 finish_trading
需要调整 prompt 策略或添加更严格的限制。

### 🟢 P2 - SerpAPI 偶发 Connection reset
国内访问需要代理。已有 proxy 支持但未测试。

### 🟢 P2 - 同一市场跨月重复出现
对于结算日在很久以后的市场（如 2026 年的市场在 2025 年一直可用），Agent 可能在不同月份重复下注。这是真实交易中也会发生的情况，但可能影响回测结果的统计意义。

## 待办

### 高优先级
- [ ] 修复 DeepSeek API timeout 问题（加重试 + timeout）
- [ ] 添加 API 调用重试机制（exponential backoff）
- [ ] 实现真正的多模型对比跑（一次 CLI 启动多个模型并行）
- [ ] Agent 搜索结果展示优化（不要让 Agent 读太多无关内容）

### 中优先级
- [ ] 添加更多 2025 年市场数据（降低成交量阈值 / 拉更多页）
- [ ] Agent 跨月记忆（记住上月已下注的市场，避免重复）
- [ ] 支持分批下注（Agent 可以先下小注试探，再加仓）
- [ ] 回测结果可视化（P&L 曲线图）
- [ ] Web UI 查看 trace 回放

### 低优先级
- [ ] 支持自定义 Agent prompt 模板
- [ ] 单位测试
- [ ] Docker 化部署
