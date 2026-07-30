#!/usr/bin/env python3
"""Run the quote collector with a persistent Saxo OAuth token bundle."""

from __future__ import annotations

import os
from pathlib import Path

from market_data.saxo_oauth import (
    DEFAULT_REDIRECT_URI,
    DEFAULT_TOKEN_FILE,
    resolve_access_token,
)
from quote_collector import main as collector_main


def main() -> int:
    environment = os.getenv("SAXO_ENV", "sim")
    token_file = Path(os.getenv("SAXO_TOKEN_FILE", DEFAULT_TOKEN_FILE))
    if not token_file.is_absolute():
        token_file = Path(__file__).resolve().parent.parent / token_file
    access_token = resolve_access_token(
        environment=environment,
        token_file=token_file,
        app_key=os.getenv("SAXO_APP_KEY"),
        app_secret=os.getenv("SAXO_APP_SECRET"),
        redirect_uri=os.getenv("SAXO_REDIRECT_URI", DEFAULT_REDIRECT_URI),
    )
    os.environ["SAXO_ACCESS_TOKEN"] = access_token
    return collector_main()


if __name__ == "__main__":
    raise SystemExit(main())
