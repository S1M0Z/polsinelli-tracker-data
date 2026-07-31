#!/usr/bin/env python3
"""Run the generic collector with browser-rendered Euronext pages."""
from __future__ import annotations

import os

import quote_collector
from market_data.euronext_browser import EuronextBrowserProvider


def main() -> int:
    os.environ["MARKET_DATA_PROVIDER"] = "euronext"
    original_provider = quote_collector.EuronextProvider
    quote_collector.EuronextProvider = EuronextBrowserProvider
    try:
        return quote_collector.main()
    finally:
        quote_collector.EuronextProvider = original_provider


if __name__ == "__main__":
    raise SystemExit(main())
