#!/usr/bin/env python3
"""Euronext provider that prefers the server-rendered product page.

Euronext currently exposes Best Bid / Best Ask in the structured-product page
itself. Older AJAX routes remain as fallbacks because their response shape and
availability can vary by market and locale.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from .base import ProviderError, QuoteSnapshot, utc_now_iso
from .euronext import (
    BASE_URL,
    EuronextProvider,
    _extract_element_text,
    _extract_timestamp,
    _find_json_value,
    _parse_number,
    _parse_order_book,
    _strip_html,
    _unwrap_payload,
)

_LABELS = {
    "bid": (
        r"best\s+bid",
        r"bid\s+price",
        r"meilleure?\s+(?:offre|demande|achat)",
        r"geldkurs",
        r"miglior\s+denaro",
        r"melhor\s+compra",
        r"beste\s+bied",
    ),
    "ask": (
        r"best\s+ask",
        r"ask\s+price",
        r"meilleure?\s+(?:vente|offre)",
        r"briefkurs",
        r"miglior\s+lettera",
        r"melhor\s+venda",
        r"beste\s+laat",
    ),
}


def _number_after_label(text: str, labels: tuple[str, ...]) -> float | None:
    number = r"(?:[€$£]\s*)?[0-9][0-9\s.,'’]*"
    for label in labels:
        match = re.search(rf"(?:{label})\s*[:\-]?\s*(?P<value>{number})", text, re.I)
        if match:
            value = _parse_number(match.group("value"))
            if value is not None:
                return value
    return None


def parse_product_summary(raw: str) -> tuple[float | None, float | None]:
    """Extract the visible Best Bid / Best Ask pair from a product page."""
    text = _strip_html(raw)
    return (
        _number_after_label(text, _LABELS["bid"]),
        _number_after_label(text, _LABELS["ask"]),
    )


def _merge_quote(
    current: tuple[float | None, float | None, float | None, float | None],
    candidate: tuple[float | None, float | None, float | None, float | None],
) -> tuple[float | None, float | None, float | None, float | None]:
    return tuple(
        existing if existing is not None else incoming
        for existing, incoming in zip(current, candidate, strict=True)
    )  # type: ignore[return-value]


class EuronextPageProvider(EuronextProvider):
    """Read the page first, then use legacy Euronext subroutes as fallbacks."""

    def _fetch_symbol_quote(
        self,
        position_id: str,
        symbol: str,
        instrument,
        *,
        session: str,
        underlying_price: float | None,
    ) -> QuoteSnapshot:
        product_url = f"{BASE_URL}/{self.locale}/product/structured-products/{symbol}"
        detail_url = f"{BASE_URL}/{self.locale}/ajax/getDetailedQuote/{symbol}"
        orderbook_url = f"{BASE_URL}/{self.locale}/popout-page/getOrderBook/{symbol}"

        product_html = self._request(product_url)
        quote = _parse_order_book(product_html)
        summary_bid, summary_ask = parse_product_summary(product_html)
        quote = _merge_quote(quote, (summary_bid, summary_ask, None, None))

        detail_html = ""
        detail_payload: Any | None = None
        order_html = ""
        order_payload: Any | None = None

        if not quote[0] or not quote[1]:
            try:
                detail_raw = self._request(detail_url, referer=product_url)
                detail_html, detail_payload = _unwrap_payload(detail_raw)
                quote = _merge_quote(quote, _parse_order_book(detail_html, detail_payload))
            except ProviderError:
                pass

        if not quote[0] or not quote[1]:
            try:
                order_raw = self._request(orderbook_url, referer=product_url)
                order_html, order_payload = _unwrap_payload(order_raw)
                quote = _merge_quote(quote, _parse_order_book(order_html, order_payload))
            except ProviderError:
                pass

        bid, ask, bid_size, ask_size = quote
        if not bid or not ask:
            page_text = _strip_html(product_html)
            page_has_product = symbol.split("-", 1)[0] in page_text.upper()
            page_has_labels = bool(re.search(r"best\s+(?:bid|ask)", page_text, re.I))
            raise ProviderError(
                f"Euronext product page has no usable bid/ask for {symbol} "
                f"(product={page_has_product}, labels={page_has_labels})"
            )
        if ask < bid:
            raise ProviderError(f"Euronext ask is lower than bid for {symbol}")

        last = _parse_number(_extract_element_text(product_html, "header-instrument-price"))
        if last is None and detail_html:
            last = _parse_number(_extract_element_text(detail_html, "header-instrument-price"))
        if last is None:
            last = _parse_number(
                _find_json_value(detail_payload, {"last", "lastprice", "price", "instrumentprice"})
            )
        price = last if last is not None and bid <= last <= ask else (bid + ask) / 2

        retrieved_at = utc_now_iso()
        quote_time = _extract_timestamp(
            _strip_html(product_html),
            _strip_html(detail_html),
            _strip_html(order_html),
        )
        if quote_time is None:
            quote_at = retrieved_at
            delayed_minutes = 0
            confidence = "medium"
        else:
            quote_utc = quote_time.astimezone(timezone.utc)
            quote_at = quote_utc.isoformat(timespec="seconds")
            age_seconds = max(0.0, datetime.now(timezone.utc).timestamp() - quote_utc.timestamp())
            delayed_minutes = int(age_seconds // 60)
            confidence = "high" if delayed_minutes <= 2 else "medium"

        return QuoteSnapshot(
            position_id=position_id,
            provider=self.name,
            session=session,
            price=price,
            bid=bid,
            ask=ask,
            quote_at=quote_at,
            retrieved_at=retrieved_at,
            source_url=product_url,
            source_confidence=confidence,
            delayed_by_minutes=delayed_minutes,
            bid_size=bid_size,
            ask_size=ask_size,
            underlying_price=underlying_price,
            instrument_uic=None,
            instrument_asset_type=instrument.asset_type,
            instrument_symbol=symbol,
        )
