# 使用指南

## CLI 命令

```bash
# Backtest 模式（事件驱动）
python trader.py backtest --start 2026-04 --end 2026-04 --cadence biweekly \
  --capital 1000 --min-volume 500000 --baseline all

# Predict 模式（实时）
python trader.py predict --capital 1000 --min-volume 500000 --parallel 3

# 指定 DeepSeek v4 pro
python trader.py predict --gateway deepseek --model deepseek-v4-pro \
  --capital 1000 --min-volume 500000 --parallel 3

# 指定 Claude（通过 ofox 网关）
python trader.py backtest --provider anthropic --start 2026-04 --end 2026-04
# Provider=anthropic 默认走 ofox /anthropic endpoint，model=anthropic/claude-opus-4.7

# 指定 GPT 5.5（也通过 ofox）
python trader.py backtest --provider openai --gateway ofox --start 2026-04 --end 2026-04
# 默认 model=openai/gpt-5.5
```

## 完整参数

### Backtest

```bash
python trader.py backtest \
  --provider {openai,anthropic} \         # default openai
  --gateway {deepseek,ofox} \             # default depends on provider
  --model MODEL \                         # 覆盖 gateway 的默认 model
  --api-key KEY --base-url URL \          # 覆盖
  --start 2026-01 --end 2026-04 \         # 决策日窗口
  --cadence {weekly,biweekly,monthly} \   # 决策日频率
  --capital 1000 \                        # 起始资金
  --min-volume 500000 \                   # 候选市场最低成交量
  --parallel 3 \                          # 并行 sub-agent 数
  --baseline {none,all,always-skip,market-prob,anti-favorite,random} \
  --no-journal \                          # 关跨决策日交易日记
  --run-id LABEL \
  --output runs/custom/
```

### Predict

```bash
python trader.py predict \
  --provider openai --gateway ofox \
  --capital 1000 --min-volume 500000 --parallel 3
# 自动会扫描历史 runs/predict-* 输出 prior_predictions_review.md
```

## 配置文件

### `.env`（密钥，不提交）

```bash
DEEPSEEK_API_KEY=sk-xxx
DEEPSEEK_BASE_URL=https://api.deepseek.com

OFOX_API_KEY=sk-of-xxx
OFOX_ANTHROPIC_BASE_URL=https://api.ofox.ai/anthropic
OFOX_OPENAI_BASE_URL=https://api.ofox.ai/v1

SERPAPI_API_KEY=xxx                 # backtest 搜索用（带日期过滤）
TAVILY_API_KEY=tvly-xxx             # predict 搜索用（实时）
KIMI_API_KEY=sk-xxx                 # 可选
```

CLI 启动时会 fail fast：backtest 必须有 LLM key + `SERPAPI_API_KEY`，predict 必须有
LLM key + `TAVILY_API_KEY`。如果 `.env` 是空的，应先补齐再跑，避免生成不可用的实验目录。

### `config.yaml`（可提交）

```yaml
backtest:
  start_month: "2026-01"
  end_month: "2026-04"
  initial_capital: 1000
  min_monthly_volume: 10000
  horizon_days: 30           # 候选市场只看 N 天内结算的
  early_exit_threshold: 0.85 # 持仓价到这个阈值就平仓兑现
  enable_early_exit: true

api:
  page_limit: 100
  max_pages: 8
  price_max_age_seconds: 172800 # 历史价格最多允许滞后 2 天

cache:
  enabled: true
  dir: "data/cache"
  ttl_hours: 72
```

## 输出目录

### Backtest

```
runs/backtest-deepseek-v4-flash-2026-05-28-024417/
├── analysis.md / analysis.json   # 多维度 P&L 报告
├── result.json                   # 摘要
├── backtest_result.json          # 详细月报
├── trace.jsonl                   # 主事件流
├── config.yaml                   # 运行配置
├── manifest.json
├── baseline_comparison.md/.json  # baseline 对照（如 --baseline all）
├── baselines/{name}/analysis.md  # 每个 baseline 单独的多维度分析
└── decisions/2026-04-01/
    ├── recommendations.md        # 当日下注建议（人类阅读）
    ├── predictions.json
    └── traces/{slug}/            # 每市场一个目录
        ├── analyzer.json         # planner 主 trace
        ├── research-1-for_yes.json
        ├── research-2-for_no.json
        ├── research-3-base_rate.json
        ├── critic.json
        ├── verdict.json          # 最终结构化判断
        ├── sources.jsonl         # de-duplicated source registry
        ├── evidence.jsonl        # append-only evidence
        ├── claims.jsonl          # claim-evidence verified links
        └── ledger_summary.json   # cluster counts
```

### Predict

```
runs/predict-2026-05-28-024417/
├── recommendations.md / predictions.json
├── prior_predictions_review.md  # 上次预测的当前状态（首次跑没有）
└── traces/{slug}/               # 同上
```

## 阅读建议

debug 一笔下注：

1. 看 `recommendations.md` 找到该 bet 行
2. `decisions/{date}/traces/{slug}/verdict.json` — 看最终判断 + claims
3. `analyzer.json` — 看 planner 派发了哪些 research 方向
4. `research-{N}-{stance}.json` — 看每个研究员的搜索 + 引用
5. `critic.json` — 看 critic 是否质疑了
6. `evidence.jsonl` / `sources.jsonl` — 看证据来源是否独立

跨次回顾：

- `analysis.md` 看整体 calibration（model_prob 是不是过度自信）
- `baseline_comparison.md` 看 LLM 是否真的胜过 random / anti-favorite

## 多模型对比

```bash
# 同时跑 deepseek 和 claude，比较结果
python trader.py backtest --start 2026-04 --end 2026-04 \
    --gateway deepseek --run-id ds-v4 --baseline all &
python trader.py backtest --start 2026-04 --end 2026-04 \
    --provider anthropic --run-id claude-47 --baseline all &
wait

# 对比 baseline_comparison.md / analysis.md
```

## 代理

如果走 SerpAPI / Google / Tavily 需要代理，优先使用本机 mihomo 的 7890 端口：

```bash
export HTTP_PROXY=http://127.0.0.1:7890
export HTTPS_PROXY=http://127.0.0.1:7890
export http_proxy=http://127.0.0.1:7890
export https_proxy=http://127.0.0.1:7890

python trader.py backtest --gateway deepseek --model deepseek-v4-pro \
  --start 2026-04 --end 2026-04 --cadence biweekly \
  --capital 1000 --min-volume 500000 --parallel 3 \
  --baseline all --no-journal
```

本机排查记录（2026-05-28）：

- Homebrew mihomo 监听 `127.0.0.1:7890`（mixed proxy）和 `127.0.0.1:9090`（controller）。
- Clash Verge 当时未监听 `9097`，而是使用 `/tmp/verge/verge-mihomo.sock`；不要只凭配置文件里的
  `7890 + 9097` 判断实际流量走向。
- 如果 `requests` 报 `SSLEOFError` / `EOF occurred in violation of protocol`，先确认 mihomo
  `GLOBAL` 不是 `DIRECT`。可以用 controller 检查：

```bash
curl -s http://127.0.0.1:9090/proxies/GLOBAL
curl -s -X PUT http://127.0.0.1:9090/proxies/GLOBAL \
  -H 'Content-Type: application/json' \
  -d '{"name":"✈️Proxy"}'
curl -I -x http://127.0.0.1:7890 https://serpapi.com/search
```

如果最后一条返回 `401`，说明 TLS 和代理链路已通，只是缺 SerpAPI key；这比 SSL EOF 更接近正常状态。
