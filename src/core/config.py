import os
import re
import yaml
import json
import hashlib
import time
from pathlib import Path
from typing import Optional


def _load_dotenv() -> None:
    """Load .env file into os.environ. Does NOT override existing env vars."""
    env_path = Path(__file__).parent.parent / ".env"
    if not env_path.exists():
        return
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key not in os.environ:
                os.environ[key] = val


def _expand_env_vars(value: str) -> str:
    """Expand ${VAR} and ${VAR:-default} placeholders in a string."""
    def _replace(m):
        expr = m.group(1)
        if ":-" in expr:
            var, default = expr.split(":-", 1)
            return os.environ.get(var.strip(), default.strip())
        return os.environ.get(expr.strip(), "")
    return re.sub(r"\$\{([^}]+)\}", _replace, value)


def _expand_dict(obj):
    """Recursively expand env vars in a config dict."""
    if isinstance(obj, dict):
        return {k: _expand_dict(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_expand_dict(v) for v in obj]
    elif isinstance(obj, str):
        return _expand_env_vars(obj)
    return obj


def load_config(path: str = "config.yaml") -> dict:
    """Load config from YAML, expand .env vars, merge with env overrides."""
    _load_dotenv()

    config_path = Path(path)
    if not config_path.exists():
        config_path = Path(__file__).parent.parent / path

    with open(config_path) as f:
        config = yaml.safe_load(f)

    config = _expand_dict(config)

    # Direct env var overrides (highest priority)
    if os.environ.get("DEEPSEEK_API_KEY"):
        config.setdefault("api_keys", {}).setdefault("deepseek", {})["key"] = os.environ["DEEPSEEK_API_KEY"]
    if os.environ.get("SERPAPI_API_KEY"):
        config.setdefault("api_keys", {}).setdefault("serpapi", {})["key"] = os.environ["SERPAPI_API_KEY"]
    if os.environ.get("TAVILY_API_KEY"):
        config.setdefault("api_keys", {}).setdefault("tavily", {})["key"] = os.environ["TAVILY_API_KEY"]

    return config


class Cache:
    """Simple file-based cache for API responses."""

    def __init__(self, cache_dir: str, ttl_hours: int = 72):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.ttl_seconds = ttl_hours * 3600

    def _key(self, *args) -> str:
        raw = "|".join(str(a) for a in args)
        return hashlib.md5(raw.encode()).hexdigest()

    def get(self, *args) -> Optional[dict]:
        key = self._key(*args)
        path = self.cache_dir / f"{key}.json"
        if not path.exists():
            return None
        if time.time() - path.stat().st_mtime > self.ttl_seconds:
            return None
        with open(path) as f:
            return json.load(f)

    def set(self, data: dict, *args):
        key = self._key(*args)
        path = self.cache_dir / f"{key}.json"
        with open(path, "w") as f:
            json.dump(data, f)
