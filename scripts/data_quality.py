"""Transparent data-completeness indicator for tracker positions."""
from __future__ import annotations

from typing import Any
from datetime import datetime, timezone


COMPONENTS = (
    ("recommendation", 20, ("mnemo", "direction", "productType", "url")),
    ("productIdentity", 15, ("isin",)),
    ("tradePlan", 30, ("entryPrice", "target", "stop")),
    ("marketQuote", 20, ("currentPrice", "bid", "ask", "quoteAt", "timestampSource", "marketStatus", "isIndicative")),
    ("underlying", 5, ("underlyingPrice", "underlyingQuoteAt", "underlyingCurrency", "riskLevelsCurrency")),
    ("timing", 10, ("publishedAt", "detectedAt")),
)


def _present(value: Any) -> bool:
    return value is not None and value != ""


def data_quality(position: dict[str, Any], as_of: datetime | None = None) -> dict[str, Any]:
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
    reference = as_of or datetime.now(timezone.utc)
    try:
        quote_at = datetime.fromisoformat(str(position.get("quoteAt")).replace("Z", "+00:00"))
        quote_age = (reference - quote_at).total_seconds() / 60
    except (TypeError, ValueError):
        quote_age = None
    operational = (
        quote_age is not None and -1 <= quote_age <= 30
        and position.get("timestampSource") not in {None, "unknown", "legacy_unverified"}
        and position.get("marketStatus") == "open"
        and position.get("isIndicative") is False
        and _present(position.get("underlyingPrice"))
        and _present(position.get("underlyingQuoteAt"))
        and position.get("underlyingCurrency") == position.get("riskLevelsCurrency")
    )
    if score >= 90 and operational:
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
        "operationallyUsable": operational,
        "quoteAgeMinutes": round(quote_age, 2) if quote_age is not None else None,
        "components": components,
    }


def refresh_data_quality(positions: list[dict[str, Any]], as_of: datetime | None = None) -> int:
    changed = 0
    for position in positions:
        if not isinstance(position, dict) or position.get("status") != "open":
            continue
        confidence = data_quality(position, as_of)
        if position.get("confidence") != confidence:
            position["confidence"] = confidence
            changed += 1
    return changed
