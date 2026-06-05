"""
Python Automation Toolkit - Core Utilities
Common helpers used across all scripts.
"""

import os
import json
import yaml
import logging
from pathlib import Path
from datetime import datetime
from typing import Any, Dict, Optional

from rich.console import Console
from rich.logging import RichHandler

console = Console()


def setup_logging(name: str, level: str = "INFO") -> logging.Logger:
    """Setup rich logging for any script."""
    logging.basicConfig(
        level=getattr(logging, level.upper()),
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(console=console, rich_tracebacks=True)],
    )
    return logging.getLogger(name)


def load_config(path: str = "config.yaml") -> Dict[str, Any]:
    """Load YAML config file."""
    p = Path(path)
    if not p.exists():
        return {}
    with open(p) as f:
        return yaml.safe_load(f) or {}


def save_json(data: Any, path: str, indent: int = 2) -> None:
    """Save data as JSON."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=indent, default=str, ensure_ascii=False)


def load_json(path: str) -> Any:
    """Load JSON file."""
    with open(path) as f:
        return json.load(f)


def timestamp_filename(prefix: str, ext: str = "json") -> str:
    """Generate timestamped filename."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{prefix}_{ts}.{ext}"


def ensure_dir(path: str) -> Path:
    """Ensure directory exists."""
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p


def chunk_list(lst: list, size: int) -> list:
    """Split list into chunks."""
    return [lst[i:i + size] for i in range(0, len(lst), size)]


def retry(func, retries: int = 3, delay: float = 1.0):
    """Simple retry decorator usage."""
    import time
    for attempt in range(retries):
        try:
            return func()
        except Exception as e:
            if attempt == retries - 1:
                raise
            time.sleep(delay * (attempt + 1))
