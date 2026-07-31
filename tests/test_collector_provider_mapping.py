from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from market_data.base import InstrumentRef, QuoteSnapshot
from quote_collector import collect_documents


class FakeProvider:
    name = "euronext"

    def resolve_instrument(self, position, mapping):
        return InstrumentRef(
            None,
            "StructuredProduct",
            symbol=f"{position['isin']}-XMLI",
            description="Test product",
        )

    def fetch_underlying_price(self, mapping):
        return None

    def fetch_instrument_quote(self, position_id, instrument, *, session, underlying_price):
        return QuoteSnapshot(
            position_id=position_id,
            provider=self.name,
            session=session,
            price=1.005,
            bid=1.00,
            ask=1.01,
            quote_at="2026-07-31T12:00:00+00:00",
            retrieved_at="2026-07-31T12:00:01+00:00",
            source_url="https://example.test/product",
            source_confidence="high",
            instrument_asset_type=instrument.asset_type,
            instrument_symbol=instrument.symbol,
        )


class CollectorProviderMappingTests(unittest.TestCase):
    def test_mappings_are_stored_under_active_provider_without_fake_uic(self):
        positions = {
            "meta": {},
            "positions": [
                {
                    "id": "test-position",
                    "asset": "Test",
                    "productType": "Warrant",
                    "direction": "CALL",
                    "mnemo": "TEST",
                    "isin": "DE000VY3GDC5",
                    "status": "open",
                    "currentPrice": 0.9,
                    "quoteAt": "2026-07-30T17:00:00+00:00",
                }
            ],
        }
        quotes = {"meta": {"maxQuotesPerPosition": 500}, "quotes": []}
        config = {"provider": "euronext", "euronext": {"instrumentMappings": {}}}

        result = collect_documents(
            positions, quotes, config, FakeProvider(), session="manual"
        )
        self.assertEqual(result.quotes_added, 1)
        mapping = config["euronext"]["instrumentMappings"]["test-position"]
        self.assertEqual(mapping["mic"], "XMLI")
        self.assertNotIn("uic", mapping)


if __name__ == "__main__":
    unittest.main()
