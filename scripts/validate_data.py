#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

REQUIRED_JSON_FILES = (
    "positions.json",
    "updates.json",
    "article-state.json",
    "scan-log.json",
    "risk-policy.json",
    "quote-history.json",
    "investment-view.json",
)


def load(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain an object")
    return value


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    documents = {name: load(root / name) for name in REQUIRED_JSON_FILES}
    positions = documents["positions.json"].get("positions", [])
    position_ids = {position["id"] for position in positions}
    if len(position_ids) != len(positions):
        raise ValueError("positions.json contains duplicate ids")
    for update in documents["updates.json"].get("updates", []):
        position_id = update.get("positionId")
        if position_id is not None and position_id not in position_ids:
            raise ValueError(f"updates.json references unknown position {position_id}")
    for slug, article in documents["article-state.json"].get("articles", {}).items():
        position_id = article.get("positionId")
        if position_id is not None and position_id not in position_ids:
            raise ValueError(f"article {slug} references unknown position {position_id}")
    scans = documents["scan-log.json"].get("scans", [])
    max_entries = documents["scan-log.json"].get("meta", {}).get("maxEntries", 200)
    if len(scans) > max_entries:
        raise ValueError("scan-log.json exceeds maxEntries")
    open_ids = {position["id"] for position in positions if position.get("status") == "open"}
    view_open_ids = {item["positionId"] for item in documents["investment-view.json"].get("openPositions", [])}
    if open_ids != view_open_ids:
        raise ValueError("investment-view.json is not aligned with open positions")
    print(f"Validated {len(REQUIRED_JSON_FILES)} JSON files and {len(positions)} positions")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
