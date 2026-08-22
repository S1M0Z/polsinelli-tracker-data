#!/usr/bin/env python3
"""Idempotent migration of legacy tracker data to schema version 2."""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def atomic_write(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args()
    path = args.root / "positions.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    config_path = args.root / "market-data-config.json"
    config = json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
    mappings = config.get(config.get("provider", "euronext"), {}).get("instrumentMappings", {})
    for position in document.get("positions", []):
        position.setdefault("recommendationStatus", "closed" if position.get("status") == "closed" else "open")
        position.setdefault("portfolioStatus", "not_held")
        position.setdefault("mutationHistory", [])
        if position.get("status") == "open":
            position.setdefault("missingOpenCycles", 0)
        mapping = mappings.get(position.get("id"), {})
        position.setdefault("riskLevelsCurrency", mapping.get("underlyingCurrency"))
    document.setdefault("meta", {})["schemaVersion"] = 2
    atomic_write(path, document)
    policy_path = args.root / "risk-policy.json"
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    policy.setdefault("meta", {})["schemaVersion"] = 3
    atomic_write(policy_path, policy)
    quotes_path = args.root / "quote-history.json"
    quotes = json.loads(quotes_path.read_text(encoding="utf-8"))
    for quote in quotes.get("quotes", []):
        quote.setdefault("timestampSource", "legacy_unverified")
        quote.setdefault("marketStatus", "unknown")
        quote.setdefault("isIndicative", True)
    quotes.setdefault("meta", {})["schemaVersion"] = 2
    atomic_write(quotes_path, quotes)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
