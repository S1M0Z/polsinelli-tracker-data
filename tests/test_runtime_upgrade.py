from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class RuntimeUpgradeTests(unittest.TestCase):
    def test_config_merge_preserves_runtime_resolution_and_applies_baseline(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            baseline = root / "baseline.json"
            runtime = root / "runtime.json"
            baseline.write_text(json.dumps({
                "provider": "euronext",
                "euronext": {"locale": "fr", "instrumentMappings": {"known": {"underlyingSource": "euronext"}}},
                "saxo": {"instrumentMappings": {}},
            }))
            runtime.write_text(json.dumps({
                "provider": "euronext",
                "euronext": {"locale": "en", "instrumentMappings": {"resolved": {"symbol": "DE000VY3GDC5-XMLI"}}},
                "saxo": {"instrumentMappings": {}},
            }))
            subprocess.run([
                "python3", str(ROOT / "scripts/merge_market_config.py"),
                "--baseline", str(baseline), "--runtime", str(runtime),
            ], check=True)
            merged = json.loads(runtime.read_text())
            self.assertEqual(merged["euronext"]["locale"], "fr")
            self.assertIn("known", merged["euronext"]["instrumentMappings"])
            self.assertIn("resolved", merged["euronext"]["instrumentMappings"])
            self.assertEqual(merged["euronext"]["instrumentMappings"]["resolved"]["underlyingSource"], "unresolved")

    def test_migration_adds_required_legacy_quote_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "positions.json").write_text(json.dumps({"meta": {}, "positions": []}))
            (root / "risk-policy.json").write_text(json.dumps({"meta": {}, "policy": {}}))
            (root / "quote-history.json").write_text(json.dumps({"meta": {}, "quotes": [{"quoteAt": "2026-01-01T00:00:00Z"}]}))
            subprocess.run(["python3", str(ROOT / "scripts/migrate_schema.py"), "--root", str(root)], check=True)
            quote = json.loads((root / "quote-history.json").read_text())["quotes"][0]
            self.assertEqual(quote["timestampSource"], "legacy_unverified")
            self.assertEqual(quote["marketStatus"], "unknown")
            self.assertTrue(quote["isIndicative"])


if __name__ == "__main__":
    unittest.main()
