#!/usr/bin/env python3
"""Build a complete, immutable public snapshot in a staging directory."""
from __future__ import annotations

import argparse
import json
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


SOURCE_FILES = (
    "positions.json", "investment-view.json", "quote-history.json", "updates.json",
    "article-state.json", "scan-log.json", "risk-policy.json", "market-data-config.json",
)


def read(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain an object")
    return value


def write(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    source, output = args.source.resolve(), args.output.resolve()
    snapshot_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid4().hex[:8]
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{output.name}-", dir=output.parent))
    try:
        documents = {}
        for name in SOURCE_FILES:
            documents[name] = read(source / name)
            documents[name].setdefault("meta", {})["snapshotId"] = snapshot_id
            write(staging / name, documents[name])
        positions_meta = documents["positions.json"].get("meta", {})
        quote_meta = documents["quote-history.json"].get("meta", {})
        view = documents["investment-view.json"]
        source_health = positions_meta.get("sourceHealth") or {}
        market_health = positions_meta.get("marketDataHealth") or {}
        verdict_unhealthy = view.get("summary", {}).get("systemVerdict") in {"SYSTEM_STALE", "DATA_PIPELINE_UNAVAILABLE"}
        degraded = source_health.get("status") == "degraded" or market_health.get("status") == "degraded"
        health = {
            "meta": {"schemaVersion": 1, "snapshotId": snapshot_id, "generatedAt": datetime.now(timezone.utc).isoformat()},
            "status": "unhealthy" if verdict_unhealthy else "degraded" if degraded else "ok",
            "systemVerdict": view.get("summary", {}).get("systemVerdict"),
            "freshness": {
                "recommendationsAt": positions_meta.get("lastPublicationCheckedAt"),
                "positionsAt": positions_meta.get("generatedAt"),
                "quotesAt": quote_meta.get("updatedAt"),
                "calculationAt": view.get("meta", {}).get("generatedAt"),
            },
            "sourceHealth": source_health,
            "marketDataHealth": market_health,
        }
        write(staging / "health.json", health)
        shutil.copy2(source / "public-manifest.json", staging / "public-manifest.json")
        if output.exists():
            raise FileExistsError(f"snapshot output already exists: {output}")
        staging.replace(output)
        print(snapshot_id)
        return 0
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
