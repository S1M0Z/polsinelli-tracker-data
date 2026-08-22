from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class SnapshotTests(unittest.TestCase):
    def build(self, directory: str) -> Path:
        output = Path(directory) / "snapshot"
        subprocess.run(
            ["python3", str(ROOT / "scripts/build_snapshot.py"), "--source", str(ROOT), "--output", str(output)],
            check=True, capture_output=True, text=True,
        )
        return output

    def test_snapshot_has_one_id_and_health_document(self):
        with tempfile.TemporaryDirectory() as directory:
            output = self.build(directory)
            ids = {
                json.loads((output / name).read_text())["meta"]["snapshotId"]
                for name in ("positions.json", "quote-history.json", "investment-view.json", "health.json")
            }
            self.assertEqual(len(ids), 1)
            self.assertEqual(json.loads((output / "health.json").read_text())["status"], "unhealthy")

    def test_validator_rejects_falsified_system_verdict(self):
        with tempfile.TemporaryDirectory() as directory:
            output = self.build(directory)
            view_path = output / "investment-view.json"
            view = json.loads(view_path.read_text())
            view["summary"]["systemVerdict"] = "ACTIONABLE_POSITIONS_AVAILABLE"
            view_path.write_text(json.dumps(view))
            result = subprocess.run(
                ["python3", str(ROOT / "scripts/validate_data.py"), "--root", str(output), "--mode", "structural"],
                capture_output=True, text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("systemVerdict is inconsistent", result.stderr)

    def test_validator_requires_snapshot_id_on_every_document(self):
        with tempfile.TemporaryDirectory() as directory:
            output = self.build(directory)
            path = output / "updates.json"
            value = json.loads(path.read_text())
            value["meta"].pop("snapshotId")
            path.write_text(json.dumps(value))
            result = subprocess.run(
                ["python3", str(ROOT / "scripts/validate_data.py"), "--root", str(output), "--mode", "structural"],
                capture_output=True, text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("every document must share", result.stderr)
