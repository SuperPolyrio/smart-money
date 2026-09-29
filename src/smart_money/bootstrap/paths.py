"""Explicit runtime locations and read-only packaged inputs."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv


def load_environment() -> None:
    """Load an explicitly selected local env file without replacing process values."""
    configured = os.environ.get("SMART_MONEY_ENV_FILE")
    if not configured:
        return
    path = Path(configured).expanduser()
    if not path.is_file():
        raise FileNotFoundError(path)
    load_dotenv(path, override=False)
