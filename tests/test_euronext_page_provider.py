from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from market_data.base import InstrumentRef
from market_data.euronext_page import EuronextPageProvider, parse_product_summary


PAGE_FIXTURE = """
<html><body>
<h1>APPLE 290P 0327V</h1>
<div>DE000VY3GDC5 - Structured product</div>
<div class="quote"><span>Best Bid</span><strong>1,29</strong></div>
<div class="quote"><span>Best Ask</span><strong>1,31</strong></div>
<div>31/07/2026 - 14:25 CET</div>
</body></html>
"""

TABLE_FIXTURE = """
<html><body>
<div>DE000VY3GDC5 - Structured product</div>
<table>
<tr><th>Orders</th><th>Shares</th><th>Bid price</th><th>Ask price</th><th>Shares</th><th>Orders</th></tr>
<tr><td>1</td><td>20000</td><td>1.29</td><td>1.31</td><td>18000</td><td>1</td></tr>
</table>
<div>31/07/2026 - 14:25 CET</div>
</body></html>
"""


class ProductSummaryTests(unittest.TestCase):
    def test_parses_visible_best_bid_and_ask(self):
        self.assertEqual(parse_product_summary(PAGE_FIXTURE), (1.29, 1.31))

    def test_page_provider_uses_summary_without_ajax(self):
        calls: list[str] = []

        def requester(url: str, *, referer: str | None = None) -> str:
            calls.append(url)
            return PAGE_FIXTURE

        provider = EuronextPageProvider(requester=requester)
        quote = provider.fetch_instrument_quote(
            "apple",
            InstrumentRef(None, "StructuredProduct", symbol="DE000VY3GDC5-XMLI"),
            session="manual",
        )
        self.assertEqual(quote.bid, 1.29)
        self.assertEqual(quote.ask, 1.31)
        self.assertEqual(len(calls), 1)

    def test_page_provider_parses_embedded_order_book(self):
        provider = EuronextPageProvider(requester=lambda *_args, **_kwargs: TABLE_FIXTURE)
        quote = provider.fetch_instrument_quote(
            "apple",
            InstrumentRef(None, "StructuredProduct", symbol="DE000VY3GDC5-XMLI"),
            session="manual",
        )
        self.assertEqual(quote.bid, 1.29)
        self.assertEqual(quote.ask, 1.31)
        self.assertEqual(quote.bid_size, 20000.0)
        self.assertEqual(quote.ask_size, 18000.0)


if __name__ == "__main__":
    unittest.main()
