#!/usr/bin/env python3
"""Shared market-data types and helpers."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class ProviderError(RuntimeError):
    """Raised when a market-data provider cannot return a valid quote."""


class ProviderConfigurationError(ProviderError):
    """Raised when required credentials or mappings are missing."""


@dataclass(frozen=True)
class InstrumentRef:
    uic: int | None
    asset_type: str
    symbol: str | None = None
    description: str | None = None


@dataclass(frozen=True)
class QuoteSnapshot:
    position_id: str
    provider: str
    session: str
    price: float
    bid: float
    ask: float
    quote_at: str | None
    retrieved_at: str
    source_url: str
    source_confidence: str
    delayed_by_minutes: int | None = None
    timestamp_source: str = "unknown"
    market_status: str = "unknown"
    is_indicative: bool = True
    bid_size: float | None = None
    ask_size: float | None = None
    underlying_price: float | None = None
    underlying_quote_at: str | None = None
    quote_currency: str | None = None
    underlying_currency: str | None = None
    instrument_uic: int | None = None
    instrument_asset_type: str | None = None
    instrument_symbol: str | None = None

    def as_document(self) -> dict[str, Any]:
        return {key: value for key, value in asdict(self).items() if value is not None}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.year < 1970:
        return None
    return parsed


def require_positive_number(value: Any, field: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
        raise ProviderError(f"Invalid or missing {field}")
    return float(value)


def request_json(
    url: str,
    *,
    token: str | None = None,
    timeout: float = 20.0,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    request_headers = {
        "Accept": "application/json",
        "User-Agent": "polsinelli-tracker/1.0",
    }
    if headers:
        request_headers.update(headers)
    if token:
        request_headers["Authorization"] = f"Bearer {token}"
    request = Request(url, headers=request_headers)
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = response.read().decode("utf-8")
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise ProviderError(f"HTTP {exc.code} from market-data API: {body[:300]}") from exc
    except URLError as exc:
        raise ProviderError(f"Market-data API connection failed: {exc.reason}") from exc

    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ProviderError("Market-data API returned invalid JSON") from exc
    if not isinstance(parsed, dict):
        raise ProviderError("Market-data API returned a non-object JSON response")
    return parsed
