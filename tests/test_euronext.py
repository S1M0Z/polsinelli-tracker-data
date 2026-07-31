from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from market_data.euronext import (
    EuronextProvider,
    _parse_number,
    _parse_order_book,
    _unwrap_payload,
)

DETAIL = '''{"html":"<div><span id=\\"header-instrument-price\\">1,295</span><div>Updated 31/07/2026 14:04 CET</div></div>"}'''
ORDER = '''
<table>
  <tr><th>Orders</th><th>Quantity</th><th>Bid</th><th>Ask</th><th>Quantity</th><th>Orders</th></tr>
  <tr><td>1</td><td>39 000</td><td>1,29</td><td>1,30</td><td>40 000</td><td>1</td></tr>
  <tr><td>1</td><td>2 000</td><td>1,28</td><td>1,31</td><td>3 000</td><td>1</td></tr>
</table>
'''


class FakeRequester:
    def __call__(self, url: str, **_kwargs) -> str:
        if "/ajax/getDetailedQuote/" in url:
            return DETAIL
        if "/getOrderBook/" in url:
            return ORDER
        return "<html>product</html>"


class EuronextProviderTests(unittest.TestCase):
    def test_parses_european_numbers(self):
        self.assertEqual(_parse_number("1,295 EUR"), 1.295)
        self.assertEqual(_parse_number("39 000"), 39000)

    def test_unwraps_json_embedded_html(self):
        html, payload = _unwrap_payload(DETAIL)
        self.assertIn("header-instrument-price", html)
        self.assertIsInstance(payload, dict)

    def test_reads_best_bid_ask_and_sizes(self):
        self.assertEqual(_parse_order_book(ORDER), (1.29, 1.30, 39000, 40000))

    def test_builds_structured_product_snapshot(self):
        provider = EuronextProvider(requester=FakeRequester())
        instrument = provider.resolve_instrument(
            {
                "id": "apple",
                "asset": "Apple",
                "productType": "Warrant",
                "isin": "DE000VY3GDC5",
            },
            {},
        )
        quote = provider.fetch_instrument_quote(
            "apple", instrument, session="manual"
        )
        self.assertEqual(quote.bid, 1.29)
        self.assertEqual(quote.ask, 1.30)
        self.assertEqual(quote.price, 1.295)
        self.assertEqual(quote.instrument_symbol, "DE000VY3GDC5-XMLI")


if __name__ == "__main__":
    unittest.main()
