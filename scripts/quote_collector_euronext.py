#!/usr/bin/env python3
"""Run the generic collector with the page-first Euronext provider."""
from __future__ import annotations

import os

import quote_collector
from market_data.euronext_page import EuronextPageProvider


def main() -> int:
    os.environ["MARKET_DATA_PROVIDER"] = "euronext"
    quote_collector.EuronextProvider = EuronextPageProvider
    return quote_collector.main()


if __name__ == "__main__":
    raise SystemExit(main())
