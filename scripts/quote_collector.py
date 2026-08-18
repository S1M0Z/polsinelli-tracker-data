#!/usr/bin/env python3
"""Collect structured market quotes for open positions.

The collector is intentionally separate from investment_engine.py:
- this module talks to a market-data provider;
- investment_engine.py only evaluates already-normalized data.
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from market_data.base import (
    ProviderConfigurationError,
    ProviderError,
    QuoteSnapshot,
    parse_iso_datetime,
    utc_now_iso,
)
from market_data.euronext import EuronextProvider
from market_data.saxo import SaxoProvider
from data_quality import refresh_data_quality


@dataclass
class CollectionResult:
    changed: bool
    quotes_added: int
    positions_updated: int
    mappings_updated: int
    errors: list[str]
    skipped: list[str]
    status: str


def read_json(path: Path) -> dict[str, Any]:
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"Missing required file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return parsed


def write_json(path: Path, document: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def build_provider(config: dict[str, Any]):
    provider_name = str(
        os.getenv("MARKET_DATA_PROVIDER") or config.get("provider", "euronext")
    ).lower()

    if provider_name == "euronext":
        provider_config = config.get("euronext", {})
        if not isinstance(provider_config, dict):
            provider_config = {}
        candidates = provider_config.get("micCandidates")
        if not isinstance(candidates, list):
            candidates = None
        return EuronextProvider(
            default_mic=(
                os.getenv("EURONEXT_DEFAULT_MIC")
                or provider_config.get("defaultMic")
                or "XMLI"
            ),
            mic_candidates=candidates,
            locale=(
                os.getenv("EURONEXT_LOCALE")
                or provider_config.get("locale")
                or "en"
            ),
        )

    if provider_name == "saxo":
        provider_config = config.get("saxo", {})
        if not isinstance(provider_config, dict):
            provider_config = {}
        return SaxoProvider(
            environment=os.getenv("SAXO_ENV") or provider_config.get("environment"),
            account_key=os.getenv("SAXO_ACCOUNT_KEY") or provider_config.get("accountKey"),
        )

    raise ProviderConfigurationError(
        f"Unsupported MARKET_DATA_PROVIDER={provider_name!r}; supported: euronext, saxo"
    )


def _snapshot_key(item: dict[str, Any]) -> tuple[Any, ...]:
    return (
        item.get("position_id") or item.get("positionId"),
        item.get("quote_at") or item.get("quoteAt"),
        item.get("bid"),
        item.get("ask"),
        item.get("price"),
        item.get("provider"),
    )


def _normalize_snapshot(snapshot: QuoteSnapshot) -> dict[str, Any]:
    raw = snapshot.as_document()
    rename = {
        "position_id": "positionId",
        "quote_at": "quoteAt",
        "retrieved_at": "retrievedAt",
        "source_url": "sourceUrl",
        "source_confidence": "sourceConfidence",
        "delayed_by_minutes": "delayedByMinutes",
        "bid_size": "bidSize",
        "ask_size": "askSize",
        "underlying_price": "underlyingPrice",
        "instrument_uic": "instrumentUic",
        "instrument_asset_type": "instrumentAssetType",
        "instrument_symbol": "instrumentSymbol",
    }
    return {rename.get(key, key): value for key, value in raw.items()}


def _is_newer(candidate: str | None, current: str | None) -> bool:
    candidate_time = parse_iso_datetime(candidate)
    current_time = parse_iso_datetime(current)
    if candidate_time is None:
        return False
    return current_time is None or candidate_time > current_time


def collect_documents(
    positions_document: dict[str, Any],
    quotes_document: dict[str, Any],
    config_document: dict[str, Any],
    provider,
    *,
    session: str,
) -> CollectionResult:
    positions = positions_document.get("positions")
    quotes = quotes_document.get("quotes")
    if not isinstance(positions, list):
        raise ValueError("positions.json must contain a positions array")
    if not isinstance(quotes, list):
        raise ValueError("quote-history.json must contain a quotes array")

    provider_name = str(
        getattr(provider, "name", None)
        or config_document.get("provider")
        or "unknown"
    )
    config_document["provider"] = provider_name
    provider_config = config_document.setdefault(provider_name, {})
    if not isinstance(provider_config, dict):
        raise ValueError(f"market-data-config.json {provider_name} must be an object")
    mappings = provider_config.setdefault("instrumentMappings", {})
    if not isinstance(mappings, dict):
        raise ValueError("instrumentMappings must be an object")

    existing_keys = {_snapshot_key(item) for item in quotes if isinstance(item, dict)}
    added = 0
    updated = 0
    mappings_updated = 0
    errors: list[str] = []
    skipped: list[str] = []
    now = utc_now_iso()

    for position in positions:
        if not isinstance(position, dict) or position.get("status") != "open":
            continue
        position_id = position.get("id")
        if not isinstance(position_id, str):
            errors.append("Open position without a valid id")
            continue
        mapping = mappings.get(position_id, {})
        if not isinstance(mapping, dict):
            mapping = {}

        try:
            instrument = provider.resolve_instrument(position, mapping)
            resolved_mapping = {
                **mapping,
                "assetType": instrument.asset_type,
            }
            if instrument.uic is not None:
                resolved_mapping["uic"] = instrument.uic
            if instrument.symbol:
                resolved_mapping["symbol"] = instrument.symbol
                if provider_name == "euronext" and "-" in instrument.symbol:
                    resolved_mapping["mic"] = instrument.symbol.rsplit("-", 1)[1]
            if instrument.description:
                resolved_mapping["description"] = instrument.description
            if resolved_mapping != mapping:
                resolved_mapping["resolvedAt"] = now
                mappings[position_id] = resolved_mapping
                mapping = resolved_mapping
                mappings_updated += 1

            underlying_price = provider.fetch_underlying_price(mapping)
            snapshot = provider.fetch_instrument_quote(
                position_id,
                instrument,
                session=session,
                underlying_price=underlying_price,
            )
            normalized = _normalize_snapshot(snapshot)
        except ProviderConfigurationError as exc:
            # Une recommandation nouvellement détectée peut légitimement ne pas
            # encore avoir d'ISIN. Elle ne doit pas faire échouer la collecte des
            # positions déjà résolues ni empêcher la publication du tracker.
            skipped.append(str(exc))
            continue
        except ProviderError as exc:
            errors.append(str(exc))
            continue

        key = _snapshot_key(normalized)
        if key not in existing_keys:
            quotes.append(normalized)
            existing_keys.add(key)
            added += 1

        if _is_newer(normalized.get("quoteAt"), position.get("quoteAt")):
            position["currentPrice"] = normalized["price"]
            position["quoteAt"] = normalized["quoteAt"]
            position["bid"] = normalized["bid"]
            position["ask"] = normalized["ask"]
            if normalized.get("underlyingPrice") is not None:
                position["underlyingPrice"] = normalized["underlyingPrice"]
            position["marketDataProvider"] = normalized["provider"]
            position["marketDataSourceUrl"] = normalized["sourceUrl"]
            position["marketDataRetrievedAt"] = normalized["retrievedAt"]
            position["marketDataDelayMinutes"] = normalized.get("delayedByMinutes", 0)
            updated += 1

    max_per_position = quotes_document.get("meta", {}).get("maxQuotesPerPosition", 500)
    if not isinstance(max_per_position, int) or max_per_position <= 0:
        max_per_position = 500
    grouped: dict[str, list[dict[str, Any]]] = {}
    ungrouped: list[dict[str, Any]] = []
    for item in quotes:
        if not isinstance(item, dict):
            continue
        position_id = item.get("positionId")
        if isinstance(position_id, str):
            grouped.setdefault(position_id, []).append(item)
        else:
            ungrouped.append(item)
    trimmed = list(ungrouped)
    for items in grouped.values():
        items.sort(
            key=lambda item: parse_iso_datetime(item.get("quoteAt"))
            or parse_iso_datetime("1970-01-01T00:00:00+00:00"),
            reverse=True,
        )
        trimmed.extend(items[:max_per_position])
    trimmed.sort(
        key=lambda item: parse_iso_datetime(item.get("quoteAt"))
        or parse_iso_datetime("1970-01-01T00:00:00+00:00"),
        reverse=True,
    )
    quotes_document["quotes"] = trimmed

    quality_updated = refresh_data_quality(positions)
    health = {
        "status": (
            "error" if errors and not added
            else "partial" if errors or skipped
            else "ok"
        ),
        "attemptedAt": now,
        "quotesAdded": added,
        "positionsUpdated": updated,
        "skippedCount": len(skipped),
        "errorCount": len(errors),
        "skipped": skipped[:25],
        "errors": errors[:25],
    }
    positions_meta = positions_document.setdefault("meta", {})
    health_updated = positions_meta.get("marketDataHealth") != health
    if health_updated:
        positions_meta["marketDataHealth"] = health
    changed = bool(
        added or updated or mappings_updated or quality_updated or health_updated
    )
    if changed:
        quotes_document.setdefault("meta", {})["updatedAt"] = now
        positions_meta["marketDataUpdatedAt"] = now
        config_document.setdefault("meta", {})["updatedAt"] = now

    if added:
        status = "updated"
    elif mappings_updated:
        status = "mapped"
    elif errors:
        status = "error"
    elif skipped:
        status = "partial"
    else:
        status = "no_change"
    return CollectionResult(
        changed=changed,
        quotes_added=added,
        positions_updated=updated,
        mappings_updated=mappings_updated,
        errors=errors,
        skipped=skipped,
        status=status,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--session", default="intraday")
    parser.add_argument("--allow-unconfigured", action="store_true")
    parser.add_argument("--changed-flag", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = args.root.resolve()
    positions_path = root / "positions.json"
    quotes_path = root / "quote-history.json"
    config_path = root / "market-data-config.json"

    positions = read_json(positions_path)
    quotes = read_json(quotes_path)
    config = read_json(config_path)

    try:
        provider = build_provider(config)
    except ProviderConfigurationError as exc:
        summary = {"status": "unconfigured", "changed": False, "message": str(exc)}
        print(json.dumps(summary, ensure_ascii=False))
        return 0 if args.allow_unconfigured else 2

    result = collect_documents(
        positions,
        quotes,
        config,
        provider,
        session=args.session,
    )
    if result.changed:
        write_json(positions_path, positions)
        write_json(quotes_path, quotes)
        write_json(config_path, config)
        if args.changed_flag:
            args.changed_flag.parent.mkdir(parents=True, exist_ok=True)
            args.changed_flag.write_text("changed\n", encoding="utf-8")

    summary = {
        "status": result.status,
        "changed": result.changed,
        "quotesAdded": result.quotes_added,
        "positionsUpdated": result.positions_updated,
        "mappingsUpdated": result.mappings_updated,
        "errors": result.errors,
        "skipped": result.skipped,
    }
    print(json.dumps(summary, ensure_ascii=False))
    if result.errors and result.quotes_added == 0 and result.mappings_updated == 0:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
