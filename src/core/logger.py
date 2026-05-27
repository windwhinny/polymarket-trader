import logging
import sys
from pathlib import Path


def setup_logger(name: str = "pm-backtest", level: int = logging.DEBUG) -> logging.Logger:
    logger = logging.getLogger(name)
    logger.setLevel(level)

    if logger.handlers:
        return logger

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )

    # Console
    ch = logging.StreamHandler(sys.stderr)
    ch.setLevel(level)
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    # File — anchored at the project root's data/, not src/data/
    log_dir = Path(__file__).parent.parent.parent / "data"
    log_dir.mkdir(parents=True, exist_ok=True)
    fh = logging.FileHandler(log_dir / "backtest.log", mode="a")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    return logger
