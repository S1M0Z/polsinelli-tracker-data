"""Transparent data-completeness indicator for tracker positions."""
from __future__ import annotations

from typing import Any


COMPONENTS = (
    ("recommendation", 20, ("mnemo", "direction", "productType", "url")),
    ("productIdentity", 15, ("isin",)),
    ("tradePlan", 30, ("entryPrice", "target", "stop")),
    ("marketQuote", 25, ("currentPrice", "bid", "ask", "quoteAt")),
    ("timing", 10, ("publishedAt", "detectedAt")),
)


def _present(value: Any) -> bool:
    return value is not None and value != ""


def data_quality(position: dict[str, Any]) -> dict[str, Any]:
    components = []
    total = 0.0
    for key, weight, fields in COMPONENTS:
        present = sum(_present(position.get(field)) for field in fields)
        score = 100.0 * present / len(fields)
        total += weight * score / 100.0
        sources = []
        if position.get("url"):
            sources.append(position["url"])
        if key == "marketQuote" and position.get("marketDataSourceUrl"):
            sources = [position["marketDataSourceUrl"]]
        components.append(
            {
                "key": key,
                "weight": weight,
                "score": round(score),
                "available": present == len(fields),
                "sources": sources,
            }
        )
    score = round(total)
    if score >= 90:
        label = "Données exploitables"
    elif score >= 60:
        label = "Données partielles"
    else:
        label = "Données insuffisantes"
    return {
        "kind": "data_quality",
        "score": score,
        "coveragePct": score,
        "label": label,
        "components": components,
    }


def refresh_data_quality(positions: list[dict[str, Any]]) -> int:
    changed = 0
    for position in positions:
        if not isinstance(position, dict) or position.get("status") != "open":
            continue
        confidence = data_quality(position)
        if position.get("confidence") != confidence:
            position["confidence"] = confidence
            changed += 1
    return changed
