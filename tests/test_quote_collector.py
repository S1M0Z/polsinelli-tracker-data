from __future__ import annotations

import sys
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from market_data.base import InstrumentRef, QuoteSnapshot
from market_data.saxo import SaxoProvider
from quote_collector import collect_documents


class FakeSaxoRequester:
    def __call__(self, url: str, *, token: str | None = None):
        self.last_token = token
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        if parsed.path.endswith("/ref/v1/instruments"):
            return {
                "Data": [
                    {
                        "Identifier": 12345,
                        "AssetType": "Warrant",
                        "Symbol": "OZ97V",
                        "Description": "Apple PUT warrant",
                        "SummaryType": "Instrument",
                    }
                ]
            }
        if parsed.path.endswith("/trade/v1/infoprices"):
            if query.get("Uic") == ["999"]:
                return {
                    "LastUpdated": "2026-07-30T18:34:58Z",
                    "Quote": {"Bid": 332.1, "Ask": 332.2, "Mid": 332.15, "DelayedByMinutes": 0},
                }
            return {
                "LastUpdated": "2026-07-30T18:35:00Z",
                "Quote": {
                    "Bid": 1.50,
                    "Ask": 1.51,
                    "Mid": 1.505,
                    "DelayedByMinutes": 0,
                    "ErrorCode": "None",
                },
                "PriceInfoDetails": {"BidSize": 39000, "AskSize": 39000},
            }
        raise AssertionError(url)


class FakeProvider:
    def resolve_instrument(self, position, mapping):
        return InstrumentRef(42, "Warrant", symbol=position["mnemo"])

    def fetch_underlying_price(self, mapping):
        return 100.0

    def fetch_instrument_quote(self, position_id, instrument, *, session, underlying_price):
        return QuoteSnapshot(
            position_id=position_id,
            provider="fake",
            session=session,
            price=1.005,
            bid=1.00,
            ask=1.01,
            quote_at="2026-07-30T18:35:00+00:00",
            retrieved_at="2026-07-30T18:35:01+00:00",
            source_url="https://example.test/quote",
            source_confidence="high",
            underlying_price=underlying_price,
            instrument_uic=instrument.uic,
            instrument_asset_type=instrument.asset_type,
            instrument_symbol=instrument.symbol,
        )


class SaxoProviderTests(unittest.TestCase):
    def test_resolves_mnemo_and_parses_bid_ask(self):
        requester = FakeSaxoRequester()
        provider = SaxoProvider(token="secret", environment="sim", requester=requester)
        position = {
            "id": "apple",
            "asset": "Apple",
            "productType": "Warrant",
            "mnemo": "OZ97V",
            "isin": "DE000VY3GDC5",
        }
        instrument = provider.resolve_instrument(position, {})
        self.assertEqual(instrument.uic, 12345)
        quote = provider.fetch_instrument_quote(
            "apple", instrument, session="close", underlying_price=332.15
        )
        self.assertEqual(quote.bid, 1.50)
        self.assertEqual(quote.ask, 1.51)
        self.assertEqual(quote.price, 1.505)
        self.assertEqual(quote.quote_at, "2026-07-30T18:35:00+00:00")
        self.assertEqual(quote.underlying_price, 332.15)
        self.assertEqual(requester.last_token, "secret")


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.positions = {
            "meta": {},
            "positions": [
                {
                    "id": "test-position",
                    "asset": "Test",
                    "productType": "Warrant",
                    "direction": "CALL",
                    "mnemo": "TEST",
                    "isin": "TEST00000000",
                    "status": "open",
                    "entryPrice": 1.0,
                    "currentPrice": 0.9,
                    "quoteAt": "2026-07-30T17:00:00+00:00",
                },
                {
                    "id": "closed-position",
                    "asset": "Closed",
                    "productType": "Warrant",
                    "direction": "PUT",
                    "mnemo": "CLOSE",
                    "isin": "TEST00000001",
                    "status": "closed",
                    "entryPrice": 1.0,
                    "exitPrice": 1.2,
                    "entryDate": "2026-07-01",
                    "exitDate": "2026-07-02",
                },
            ],
        }
        self.quotes = {"meta": {"maxQuotesPerPosition": 500}, "quotes": []}
        self.config = {"provider": "saxo", "saxo": {"instrumentMappings": {}}}

    def test_updates_open_position_and_appends_snapshot(self):
        result = collect_documents(
            self.positions, self.quotes, self.config, FakeProvider(), session="close"
        )
        self.assertTrue(result.changed)
        self.assertEqual(result.quotes_added, 1)
        self.assertEqual(result.positions_updated, 1)
        self.assertEqual(len(self.quotes["quotes"]), 1)
        position = self.positions["positions"][0]
        self.assertEqual(position["currentPrice"], 1.005)
        self.assertEqual(position["bid"], 1.0)
        self.assertEqual(position["ask"], 1.01)
        self.assertEqual(position["underlyingPrice"], 100.0)
        self.assertEqual(
            self.config["saxo"]["instrumentMappings"]["test-position"]["uic"], 42
        )

    def test_exact_duplicate_is_not_appended(self):
        collect_documents(
            self.positions, self.quotes, self.config, FakeProvider(), session="close"
        )
        result = collect_documents(
            self.positions, self.quotes, self.config, FakeProvider(), session="close"
        )
        self.assertEqual(result.quotes_added, 0)
        self.assertEqual(len(self.quotes["quotes"]), 1)

    def test_closed_position_is_never_modified(self):
        before = dict(self.positions["positions"][1])
        collect_documents(
            self.positions, self.quotes, self.config, FakeProvider(), session="close"
        )
        self.assertEqual(self.positions["positions"][1], before)


if __name__ == "__main__":
    unittest.main()
