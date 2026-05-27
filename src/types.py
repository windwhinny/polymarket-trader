from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional
import json


@dataclass
class Market:
    """A single Polymarket binary market."""
    id: str
    condition_id: str
    question: str
    slug: str
    outcomes: list[str]  # ["Yes", "No"]
    token_ids: list[str]  # [yes_token_id, no_token_id]
    volume: float
    start_date: str  # ISO date
    end_date: str  # ISO date (resolution date)
    closed: bool
    resolution: Optional[str] = None  # "Yes" / "No" / None (unresolved)
    category: str = ""
    outcome_prices: list[float] = field(default_factory=list)


@dataclass
class PriceSnapshot:
    """Price of a market at a specific point in time."""
    token_id: str
    timestamp: str  # ISO datetime
    price: float  # 0-1


@dataclass
class SearchContext:
    """Tavily search results for a market at a point in time."""
    query: str
    end_date: str
    results: list[dict]  # raw Tavily results
    summary: str  # concatenated text of top results


@dataclass
class ModelPrediction:
    """Model's prediction for a market."""
    market_id: str
    model_prob: float  # 0-1, model's estimated probability of YES
    reasoning: str
    search_context_summary: str


@dataclass
class Bet:
    """A simulated bet."""
    market_id: str
    month: str  # "2025-01"
    direction: str  # "YES" or "NO"
    model_prob: float
    market_prob: float
    edge: float
    kelly_fraction: float
    amount: float
    entry_price: float
    resolution: Optional[str] = None  # filled after resolution check
    pnl: Optional[float] = None  # filled after resolution check


@dataclass
class MonthlyReport:
    """P&L report for one month."""
    month: str
    total_bets: int
    won: int
    lost: int
    unresolved: int
    win_rate: float
    total_bet_amount: float
    total_pnl: float
    starting_capital: float
    ending_capital: float
    roi: float
    bets: list[Bet] = field(default_factory=list)


@dataclass
class BacktestResult:
    """Full backtest results."""
    months: list[MonthlyReport]
    total_pnl: float
    total_roi: float
    sharpe_ratio: float
    max_drawdown: float
    total_bets: int
    overall_win_rate: float
    config: dict = field(default_factory=dict)


class DataclassEncoder(json.JSONEncoder):
    def default(self, obj):
        if hasattr(obj, '__dataclass_fields__'):
            return asdict(obj)
        if isinstance(obj, datetime):
            return obj.isoformat()
        return super().default(obj)
