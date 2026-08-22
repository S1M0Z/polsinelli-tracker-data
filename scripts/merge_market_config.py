#!/usr/bin/env python3
"""Merge versioned provider configuration into persistent resolved mappings."""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def write(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--runtime", type=Path, required=True)
    args = parser.parse_args()
    baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
    runtime = json.loads(args.runtime.read_text(encoding="utf-8")) if args.runtime.exists() else {}
    merged = {**runtime, **baseline}
    for provider in ("euronext", "saxo"):
        runtime_provider = runtime.get(provider, {})
        baseline_provider = baseline.get(provider, {})
        provider_value = {**runtime_provider, **baseline_provider}
        provider_value["instrumentMappings"] = {
            **runtime_provider.get("instrumentMappings", {}),
            **baseline_provider.get("instrumentMappings", {}),
        }
        if provider == "euronext":
            for mapping in provider_value["instrumentMappings"].values():
                if isinstance(mapping, dict) and mapping.get("symbol"):
                    mapping.setdefault("underlyingSource", "unresolved")
        merged[provider] = provider_value
    write(args.runtime, merged)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
