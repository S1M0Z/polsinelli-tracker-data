from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from data_quality import data_quality, refresh_data_quality


class DataQualityTests(unittest.TestCase):
    def test_incomplete_position_gets_explicit_low_coverage(self):
        position = {
            "status": "open", "mnemo": "MU13V", "direction": "CALL",
            "productType": "Turbo", "url": "https://example.test/article",
        }
        confidence = data_quality(position)
        self.assertEqual(confidence["kind"], "data_quality")
        self.assertLess(confidence["score"], 60)
        self.assertEqual(confidence["label"], "Données insuffisantes")

    def test_complete_position_is_marked_exploitable(self):
        position = {
            "status": "open", "mnemo": "MU13V", "direction": "CALL",
            "productType": "Turbo", "url": "https://example.test/article",
            "isin": "DE000TEST001", "entryPrice": 1.2, "target": 2800,
            "stop": 2600, "currentPrice": 1.3, "bid": 1.29, "ask": 1.31,
            "quoteAt": "2026-08-15T10:00:00+00:00",
            "publishedAt": "2026-08-15T09:00:00+00:00",
            "detectedAt": "2026-08-15T09:01:00+00:00",
        }
        self.assertEqual(data_quality(position)["score"], 100)
        self.assertEqual(refresh_data_quality([position]), 1)
        self.assertEqual(position["confidence"]["label"], "Données exploitables")


if __name__ == "__main__":
    unittest.main()
