# 使用指南

## CLI 命令

```bash
# 回测模式
python trader.py backtest --model deepseek-chat --start 2026-01 --end 2026-04

# 完整参数
python trader.py backtest \
  --provider openai \           # openai 或 anthropic
  --model deepseek-chat \       # 模型名
  --api-key sk-xxx \            # 覆盖 .env 中的 API key
  --base-url https://xxx/v1 \   # 覆盖 base URL
  --start 2026-01 \             # 起始月份
  --end 2026-04 \               # 结束月份
  --capital 5000 \              # 初始资金
  --min-volume 50000 \          # 最低市场成交量
  --parallel 4 \                # 并行线程数
  --run-id my-experiment \      # 自定义 run ID 前缀
  --output runs/custom/         # 自定义输出目录

# 旧版入口（兼容）
python run_agent.py --model deepseek-chat --start 2026-01 --end 2026-04
```

## 配置

### .env（密钥，不提交）

```bash
DEEPSEEK_API_KEY=sk-xxx
DEEPSEEK_BASE_URL=https://api.deepseek.com
SERPAPI_API_KEY=xxx              # https://serper.dev 免费注册
TAVILY_API_KEY=xxx               # 可选备选搜索引擎
```

### config.yaml（可提交的参数）

```yaml
backtest:
  start_month: "2026-01"
  end_month: "2026-04"
  initial_capital: 1000
  min_monthly_volume: 10000
api:
  page_limit: 100
  max_pages: 8
```

## 运行输出

每次运行在 `runs/{model}-{timestamp}/` 下：

```
runs/deepseek-chat-2026-05-27-230308/
├── trace.jsonl      # Agent 完整交互
├── bets.jsonl        # 每笔下注
├── config.yaml       # 运行配置
├── result.json       # 结果摘要
├── manifest.json     # 运行元信息
└── backtest_result.json  # 完整 P&L 报告
```

### trace.jsonl 格式

每行一个 JSON 事件：

```json
{"type":"system_prompt","content":"你是一个自主预测市场交易员..."}
{"type":"turn_start","turn":1}
{"type":"model_call","messages_count":1,"provider":"openai","model":"deepseek-chat"}
{"type":"model_response","content":"","tool_calls":[{"name":"search_news","args":{...}}]}
{"type":"tool_call","name":"search_news","args":{"query":"Bitcoin 2026 forecast"}}
{"type":"tool_result","name":"search_news","result":"{\"status\":\"ok\",...}"}
{"type":"bet","direction":"NO","amount":150,"slug":"...","pnl":125.01,"resolution":"NO"}
{"type":"finish","month":"2026-01","final_capital":1140.87,...}
```

## 多模型对比

```bash
# 分别跑，结果在不同目录
python run_agent.py --model deepseek-chat --run-id deepseek --start 2026-01 --end 2026-04 &
python run_agent.py --model gpt-4o --api-key sk-xxx --run-id gpt4o --start 2026-01 --end 2026-04 &

# 等完成后对比 runs/ 下的 result.json
```

## 代理配置

如果全局没有代理，可以设置环境变量：

```bash
export HTTPS_PROXY=http://127.0.0.1:7890
python run_agent.py ...
```
