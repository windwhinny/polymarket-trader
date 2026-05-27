"""DeepSeek API — predict market outcome probability."""
import json
import logging
from typing import Optional

from .types import ModelPrediction, Market, SearchContext
from .config import Cache

log = logging.getLogger("pm-backtest.predict")


SYSTEM_PROMPT = """You are a prediction market analyst with knowledge cutoff in early 2025. 
Given a binary market question, current market-implied probability, and recent news context, 
estimate the probability that YES resolves as the correct outcome.

CRITICAL RULES:
1. Do NOT just echo the market probability. Form your own independent estimate.
2. Even with no news context, use your general knowledge about base rates, historical patterns, 
   and domain expertise to form an opinion. Markets are often wrong.
3. Output ONLY valid JSON: {"probability": <number>, "reasoning": "<brief>"}
4. probability must be 0.01-0.99 (never 0 or 1)
5. If you truly have no basis for an opinion, tilt slightly against the market consensus 
   (e.g., if market says 70%, consider 65% or 75%) — markets have known biases.
6. reasoning is 1-2 sentences"""


def predict(
    market: Market,
    market_prob: float,
    search_ctx: SearchContext,
    api_key: str,
    base_url: str,
    model: str = "deepseek-chat",
    cache: Optional[Cache] = None,
) -> ModelPrediction:
    """Call DeepSeek API to predict market outcome probability."""
    cache_key = ("predict-v2", market.id)

    if cache:
        cached = cache.get(*cache_key)
        if cached:
            log.debug("CACHE HIT | predict %s", market.slug)
            return ModelPrediction(**cached)

    has_context = search_ctx.results and len(search_ctx.results) > 0
    context_note = (
        f"Recent news (from {search_ctx.end_date}):\n{search_ctx.summary}"
        if has_context
        else "(No relevant news found. Use your general knowledge to estimate.)"
    )

    user_prompt = f"""Market: {market.question}
Category: {market.category or 'General'}
Market probability (YES): {market_prob:.1%}

{context_note}

Estimate the probability that YES resolves as correct. Explain your reasoning briefly."""

    log.info("PREDICT | %s | market=%.2f has_context=%s", market.slug, market_prob, has_context)
    log.debug("PREDICT PROMPT | %s", user_prompt[:300])

    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key, base_url=base_url)
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.3,
            max_tokens=300,
            response_format={"type": "json_object"},
        )
        content = response.choices[0].message.content
        log.debug("PREDICT RAW | %s", content[:200])

        parsed = json.loads(content)
        prob = float(parsed.get("probability", market_prob))
        prob = max(0.005, min(0.995, prob))
        reasoning = parsed.get("reasoning", "")

        # If model still echoed exactly, nudge slightly to avoid zero edge
        if abs(prob - market_prob) < 0.001:
            prob = market_prob + (0.02 if market_prob < 0.5 else -0.02)
            prob = max(0.005, min(0.995, prob))
            log.debug("PREDICT NUDGE | %.4f -> %.4f", market_prob, prob)

        log.info("PREDICT RESULT | %s | model=%.4f market=%.4f edge=%.4f",
                 market.slug, prob, market_prob, abs(prob - market_prob))

    except Exception as e:
        log.error("PREDICT ERROR | %s: %s", market.slug, e)
        # Fallback: slight contrarian tilt
        prob = market_prob + (0.05 if market_prob < 0.5 else -0.05)
        prob = max(0.005, min(0.995, prob))
        reasoning = f"API error: {e}"

    pred = ModelPrediction(
        market_id=market.id,
        model_prob=prob,
        reasoning=reasoning,
        search_context_summary=search_ctx.summary[:200],
    )

    if cache:
        cache.set({
            "market_id": pred.market_id,
            "model_prob": pred.model_prob,
            "reasoning": pred.reasoning,
            "search_context_summary": pred.search_context_summary,
        }, *cache_key)

    return pred
