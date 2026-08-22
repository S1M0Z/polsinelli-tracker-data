#!/usr/bin/env python3
from __future__ import annotations

import argparse
import collections
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from investment_engine import ENGINE_VERSION, Policy
import jsonschema

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


def valid_isin(value: str) -> bool:
    if not re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9}\d", value):
        return False
    digits = "".join(str(ord(char) - 55) if char.isalpha() else char for char in value)
    total = 0
    for index, char in enumerate(reversed(digits)):
        number = int(char) * (2 if index % 2 else 1)
        total += number // 10 + number % 10
    return total % 10 == 0


def validate_positions(positions: list[dict[str, Any]]) -> None:
    allowed_recommendations = {"open", "closed", "PENDING_RECONCILIATION", "UNKNOWN", None}
    allowed_portfolio = {"not_held", "held", "sold", None}
    for position in positions:
        position_id = position.get("id")
        if position.get("status") not in {"open", "closed"}:
            raise ValueError(f"{position_id}: invalid status")
        if position.get("direction") not in {"CALL", "PUT"}:
            raise ValueError(f"{position_id}: invalid direction")
        if position.get("recommendationStatus") not in allowed_recommendations:
            raise ValueError(f"{position_id}: invalid recommendationStatus")
        if position.get("portfolioStatus") not in allowed_portfolio:
            raise ValueError(f"{position_id}: invalid portfolioStatus")
        currency = position.get("currency")
        if not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency):
            raise ValueError(f"{position_id}: invalid currency")
        isin = position.get("isin")
        if isin is not None and (not isinstance(isin, str) or not valid_isin(isin)):
            raise ValueError(f"{position_id}: invalid ISIN checksum")
        if position.get("portfolioStatus") == "held":
            for field in ("executedAt", "executedPrice", "quantity", "account"):
                if position.get(field) in (None, ""):
                    raise ValueError(f"{position_id}: held position needs {field}")
        if position.get("entryDate") and position.get("exitDate") and position["exitDate"] < position["entryDate"]:
            raise ValueError(f"{position_id}: exitDate predates entryDate")


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


def validate_market_data_config(config: dict[str, Any], position_ids: set[str]) -> None:
    provider = config.get("provider")
    if provider not in {"saxo", "euronext"}:
        raise ValueError("market-data-config.json provider must be saxo or euronext")
    provider_config = config.get(provider, {})
    if not isinstance(provider_config, dict):
        raise ValueError(f"market-data-config.json {provider} must be an object")
    mappings = provider_config.get("instrumentMappings", {})
    if not isinstance(mappings, dict):
        raise ValueError("market-data-config.json instrumentMappings must be an object")

    for position_id, mapping in mappings.items():
        if position_id not in position_ids:
            raise ValueError(f"market-data mapping references unknown position {position_id}")
        if not isinstance(mapping, dict):
            raise ValueError(f"market-data mapping for {position_id} must be an object")
        asset_type = mapping.get("assetType")
        if asset_type is not None and not isinstance(asset_type, str):
            raise ValueError(f"market-data mapping for {position_id} has invalid assetType")

        if provider == "saxo":
            uic = mapping.get("uic")
            if uic is not None and (not isinstance(uic, int) or uic <= 0):
                raise ValueError(f"market-data mapping for {position_id} has invalid uic")
            if uic is not None and not isinstance(asset_type, str):
                raise ValueError(f"market-data mapping for {position_id} needs assetType")
        else:
            mic = mapping.get("mic")
            symbol = mapping.get("symbol")
            underlying_symbol = mapping.get("underlyingSymbol")
            underlying_mic = mapping.get("underlyingMic")
            underlying_currency = mapping.get("underlyingCurrency")
            underlying_source = mapping.get("underlyingSource")
            if mic is not None and (
                not isinstance(mic, str) or not re.fullmatch(r"[A-Z0-9]{4}", mic)
            ):
                raise ValueError(f"market-data mapping for {position_id} has invalid mic")
            if symbol is not None and (
                not isinstance(symbol, str)
                or not re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9}\d-[A-Z0-9]{4}", symbol)
            ):
                raise ValueError(f"market-data mapping for {position_id} has invalid Euronext symbol")
            if symbol is not None and underlying_source not in {"euronext", "unresolved"}:
                raise ValueError(f"market-data mapping for {position_id} needs underlyingSource")
            if underlying_source == "euronext" and (
                not isinstance(underlying_symbol, str)
                or not re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9}\d-[A-Z0-9]{4}", underlying_symbol)
                or not isinstance(underlying_mic, str) or not re.fullmatch(r"[A-Z0-9]{4}", underlying_mic)
                or not isinstance(underlying_currency, str) or not re.fullmatch(r"[A-Z]{3}", underlying_currency)
            ):
                raise ValueError(f"market-data mapping for {position_id} has incomplete Euronext underlying mapping")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="Directory containing the JSON documents to validate.",
    )
    parser.add_argument(
        "--max-system-age-hours",
        type=float,
        default=24.0,
        help="Maximum age of generated data before validation fails.",
    )
    parser.add_argument(
        "--allow-unhealthy",
        action="store_true",
        help="Validate structure while allowing a stale/unavailable pipeline.",
    )
    parser.add_argument(
        "--mode", choices=("structural", "operational"), default="operational",
        help="Structural mode checks integrity without requiring currently fresh sources.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = args.root.resolve()
    documents = {name: load(root / name) for name in REQUIRED_JSON_FILES}
    schema_root = Path(__file__).resolve().parent.parent / "schemas"
    jsonschema.validate(documents["positions.json"], load(schema_root / "positions.schema.json"))
    jsonschema.validate(documents["risk-policy.json"], load(schema_root / "risk-policy.schema.json"))
    Policy.from_document(documents["risk-policy.json"])
    positions = documents["positions.json"].get("positions", [])
    position_ids = {position["id"] for position in positions}
    if len(position_ids) != len(positions):
        raise ValueError("positions.json contains duplicate ids")
    validate_positions(positions)

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
        quote_at_raw = quote.get("quoteAt")
        if quote.get("timestampSource") not in {"provider_last_updated", "euronext_page", "legacy_unverified", "unknown"}:
            raise ValueError(f"quote #{index} has invalid timestampSource")
        if quote.get("marketStatus") not in {"open", "closed", "unknown"}:
            raise ValueError(f"quote #{index} has invalid marketStatus")
        if not isinstance(quote.get("isIndicative"), bool):
            raise ValueError(f"quote #{index} needs boolean isIndicative")
        if quote_at_raw is None:
            if quote.get("timestampSource") != "unknown" or quote.get("isIndicative") is not True:
                raise ValueError(
                    f"quote #{index} without quoteAt must be explicitly unconfirmed and indicative"
                )
        else:
            parse_timestamp(quote_at_raw, f"quote #{index} quoteAt")
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

    validate_market_data_config(documents["market-data-config.json"], position_ids)

    open_ids = {position["id"] for position in positions if position.get("status") == "open"}
    view_open_ids = {
        item["positionId"]
        for item in documents["investment-view.json"].get("openPositions", [])
    }
    if open_ids != view_open_ids:
        raise ValueError("investment-view.json is not aligned with open positions")

    view = documents["investment-view.json"]
    present_snapshot_ids = [document.get("meta", {}).get("snapshotId") for document in documents.values()]
    if any(present_snapshot_ids) and (
        any(not value for value in present_snapshot_ids) or len(set(present_snapshot_ids)) != 1
    ):
        raise ValueError("every document must share one non-empty snapshotId")
    health_path = root / "health.json"
    if health_path.exists():
        health = load(health_path)
        jsonschema.validate(health, load(schema_root / "health.schema.json"))
        if not present_snapshot_ids or health.get("meta", {}).get("snapshotId") != present_snapshot_ids[0]:
            raise ValueError("health.json snapshotId does not match the snapshot")
    view_generated_at = parse_timestamp(
        view.get("meta", {}).get("generatedAt"), "investment-view.json generatedAt"
    )
    latest_quote_at = max(
        (parse_timestamp(quote.get("quoteAt"), "quote quoteAt") for quote in quotes if quote.get("quoteAt")),
        default=None,
    )
    if latest_quote_at is not None and view_generated_at < latest_quote_at:
        raise ValueError("investment-view.json predates the latest market quote")
    age_hours = (datetime.now(timezone.utc) - view_generated_at).total_seconds() / 3600
    if age_hours > args.max_system_age_hours and args.mode == "operational":
        raise ValueError(
            f"investment-view.json is stale ({age_hours:.1f} hours old)"
        )
    if view.get("meta", {}).get("engineVersion") != ENGINE_VERSION:
        raise ValueError(f"investment-view.json was not built with engine {ENGINE_VERSION}")

    open_rows = view.get("openPositions", [])
    if not isinstance(open_rows, list):
        raise ValueError("investment-view.json openPositions must be an array")
    decisions = collections.Counter(item.get("decision") for item in open_rows)
    if None in decisions:
        raise ValueError("investment-view.json contains an open position without a decision")
    summary = view.get("summary", {})
    expected_counts = {
        "positionCount": len(positions),
        "openCount": len(open_ids),
        "closedCount": len(positions) - len(open_ids),
        "actionableCount": decisions.get("ELIGIBLE", 0),
    }
    for field, expected in expected_counts.items():
        if summary.get(field) != expected:
            raise ValueError(
                f"investment-view.json summary {field} is {summary.get(field)!r}, expected {expected}"
            )
    if summary.get("decisionCounts") != dict(decisions):
        raise ValueError("investment-view.json decisionCounts does not match open positions")
    policy = documents["risk-policy.json"]["policy"]
    freshness = view.get("meta", {}).get("freshness", {})
    watch_age = freshness.get("watch", {}).get("ageMinutes")
    positions_age = freshness.get("positions", {}).get("ageMinutes")
    source_unavailable = documents["positions.json"].get("meta", {}).get("sourceHealth", {}).get("status") == "source_unavailable"
    if watch_age is None or positions_age is None or source_unavailable:
        expected_verdict = "DATA_PIPELINE_UNAVAILABLE"
    elif watch_age > policy["maxWatchAgeMinutes"] or positions_age > policy["maxPositionsAgeMinutes"]:
        expected_verdict = "SYSTEM_STALE"
    elif open_rows and all(item.get("decision") in {"STALE_QUOTE", "DATA_INCOMPLETE"} for item in open_rows):
        expected_verdict = "SYSTEM_STALE" if any(item.get("market", {}).get("quoteAt") for item in open_rows) else "DATA_PIPELINE_UNAVAILABLE"
    elif decisions.get("ELIGIBLE", 0):
        expected_verdict = "ACTIONABLE_POSITIONS_AVAILABLE"
    else:
        expected_verdict = "NO_ACTIONABLE_POSITION"
    if summary.get("systemVerdict") != expected_verdict:
        raise ValueError("investment-view.json systemVerdict is inconsistent")
    if (
        summary.get("systemVerdict") in {"SYSTEM_STALE", "DATA_PIPELINE_UNAVAILABLE"}
        and not args.allow_unhealthy and args.mode == "operational"
    ):
        raise ValueError(
            f"investment data is operationally unusable: {summary.get('systemVerdict')}"
        )

    print(
        f"Validated {len(REQUIRED_JSON_FILES)} JSON files, "
        f"{len(positions)} positions and {len(quotes)} quote snapshots"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
