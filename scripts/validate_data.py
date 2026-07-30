#!/usr/bin/env python3
from __future__ import annotations

import json
from datetime import datetime
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
    "market-data-config.json",
)


def load(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain an object")
    return value


def parse_timestamp(value: Any, label: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{label} needs an ISO-8601 timestamp")
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"{label} contains an invalid timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{label} timestamp needs a timezone")
    return parsed


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

    quote_document = documents["quote-history.json"]
    quotes = quote_document.get("quotes", [])
    if not isinstance(quotes, list):
        raise ValueError("quote-history.json quotes must be an array")
    max_quotes = quote_document.get("meta", {}).get("maxQuotesPerPosition", 500)
    quote_counts: dict[str, int] = {}
    seen_quote_keys: set[tuple[Any, ...]] = set()
    for index, quote in enumerate(quotes):
        if not isinstance(quote, dict):
            raise ValueError(f"quote-history.json quote #{index} must be an object")
        position_id = quote.get("positionId")
        if position_id not in position_ids:
            raise ValueError(f"quote #{index} references unknown position {position_id}")
        quote_counts[position_id] = quote_counts.get(position_id, 0) + 1
        parse_timestamp(quote.get("quoteAt"), f"quote #{index} quoteAt")
        parse_timestamp(quote.get("retrievedAt"), f"quote #{index} retrievedAt")
        for field in ("price", "bid", "ask"):
            value = quote.get(field)
            if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"quote #{index} has invalid {field}")
        if quote["ask"] < quote["bid"]:
            raise ValueError(f"quote #{index} has ask below bid")
        if not quote.get("provider") or not quote.get("sourceUrl"):
            raise ValueError(f"quote #{index} needs provider and sourceUrl")
        key = (
            position_id,
            quote.get("quoteAt"),
            quote.get("bid"),
            quote.get("ask"),
            quote.get("price"),
            quote.get("provider"),
        )
        if key in seen_quote_keys:
            raise ValueError(f"quote-history.json contains an exact duplicate for {position_id}")
        seen_quote_keys.add(key)
    if any(count > max_quotes for count in quote_counts.values()):
        raise ValueError("quote-history.json exceeds maxQuotesPerPosition")

    config = documents["market-data-config.json"]
    provider = config.get("provider")
    if provider not in {"saxo"}:
        raise ValueError("market-data-config.json provider must be saxo")
    mappings = config.get("saxo", {}).get("instrumentMappings", {})
    if not isinstance(mappings, dict):
        raise ValueError("market-data-config.json instrumentMappings must be an object")
    for position_id, mapping in mappings.items():
        if position_id not in position_ids:
            raise ValueError(f"market-data mapping references unknown position {position_id}")
        if not isinstance(mapping, dict):
            raise ValueError(f"market-data mapping for {position_id} must be an object")
        uic = mapping.get("uic")
        asset_type = mapping.get("assetType")
        if uic is not None and (not isinstance(uic, int) or uic <= 0):
            raise ValueError(f"market-data mapping for {position_id} has invalid uic")
        if uic is not None and not isinstance(asset_type, str):
            raise ValueError(f"market-data mapping for {position_id} needs assetType")

    open_ids = {position["id"] for position in positions if position.get("status") == "open"}
    view_open_ids = {
        item["positionId"]
        for item in documents["investment-view.json"].get("openPositions", [])
    }
    if open_ids != view_open_ids:
        raise ValueError("investment-view.json is not aligned with open positions")

    print(
        f"Validated {len(REQUIRED_JSON_FILES)} JSON files, "
        f"{len(positions)} positions and {len(quotes)} quote snapshots"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
