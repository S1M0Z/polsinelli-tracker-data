#!/usr/bin/env python3
"""Public Euronext Live quote provider for listed structured products."""
from __future__ import annotations

import json
import re
import time
import unicodedata
from datetime import date, datetime, time as clock_time, timedelta, timezone
from html import unescape
from html.parser import HTMLParser
from http.cookiejar import CookieJar
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import HTTPCookieProcessor, Request, build_opener
from zoneinfo import ZoneInfo

from .base import InstrumentRef, ProviderConfigurationError, ProviderError, QuoteSnapshot, utc_now_iso

BASE_URL = "https://live.euronext.com"
DEFAULT_MIC_CANDIDATES = ("XMLI", "XPAR", "SEDX")
PARIS = ZoneInfo("Europe/Paris")


def _easter_sunday(year: int) -> date:
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f, g = (b + 8) // 25, (b - (b + 8) // 25 + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = (h + l - 7 * m + 114) % 31 + 1
    return date(year, month, day)


def euronext_market_status(at: datetime | None) -> str:
    if at is None:
        return "unknown"
    local = at.astimezone(PARIS)
    easter = _easter_sunday(local.year)
    holidays = {
        date(local.year, 1, 1), easter - timedelta(days=2), easter + timedelta(days=1),
        date(local.year, 5, 1), date(local.year, 12, 25), date(local.year, 12, 26),
    }
    if local.weekday() >= 5 or local.date() in holidays:
        return "closed"
    return "open" if clock_time(9, 0) <= local.time().replace(tzinfo=None) <= clock_time(17, 30) else "closed"


def _normalize_label(value: str) -> str:
    value = unicodedata.normalize("NFKD", value)
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def _parse_number(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) if value > 0 else None
    if not isinstance(value, str):
        return None
    text = unescape(value).replace("\u00a0", " ").strip()
    if not text or text in {"-", "–", "—", "n/a", "N/A"}:
        return None
    text = re.sub(r"[^0-9,\.\-]", "", text.replace(" ", ""))
    if not text or text in {"-", ".", ","}:
        return None
    if "," in text and "." in text:
        decimal = "," if text.rfind(",") > text.rfind(".") else "."
        thousands = "." if decimal == "," else ","
        text = text.replace(thousands, "").replace(decimal, ".")
    elif "," in text:
        text = text.replace(".", "").replace(",", ".")
    try:
        parsed = float(text)
    except ValueError:
        return None
    return parsed if parsed > 0 else None


class _TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[str]]] = []
        self._table_depth = 0
        self._rows: list[list[str]] | None = None
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        if tag == "table":
            self._table_depth += 1
            if self._table_depth == 1:
                self._rows = []
        elif self._table_depth == 1 and tag == "tr":
            self._row = []
        elif self._table_depth == 1 and tag in {"th", "td"}:
            self._cell = []
        elif self._cell is not None and tag == "br":
            self._cell.append(" ")

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self._table_depth == 1 and tag in {"th", "td"} and self._cell is not None:
            if self._row is not None:
                self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif self._table_depth == 1 and tag == "tr":
            if self._rows is not None and self._row and any(cell for cell in self._row):
                self._rows.append(self._row)
            self._row = None
        elif tag == "table" and self._table_depth:
            if self._table_depth == 1 and self._rows is not None:
                self.tables.append(self._rows)
                self._rows = None
            self._table_depth -= 1


def _strip_html(raw: str) -> str:
    text = re.sub(r"<script\b[^>]*>.*?</script>", " ", raw, flags=re.I | re.S)
    text = re.sub(r"<style\b[^>]*>.*?</style>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    return " ".join(unescape(text).split())


def _json_html_strings(value: Any) -> list[str]:
    found: list[str] = []
    if isinstance(value, str) and "<" in value and ">" in value:
        found.append(value)
    elif isinstance(value, dict):
        for item in value.values():
            found.extend(_json_html_strings(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(_json_html_strings(item))
    return found


def _unwrap_payload(raw: str) -> tuple[str, Any | None]:
    stripped = raw.strip()
    try:
        payload = json.loads(stripped)
    except (json.JSONDecodeError, TypeError):
        return raw, None
    html_strings = _json_html_strings(payload)
    return "\n".join(html_strings) if html_strings else raw, payload


def _find_json_value(payload: Any, names: set[str]) -> Any | None:
    if isinstance(payload, dict):
        for key, value in payload.items():
            normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
            if normalized in names and not isinstance(value, (dict, list)):
                return value
        for value in payload.values():
            found = _find_json_value(value, names)
            if found is not None:
                return found
    elif isinstance(payload, list):
        for value in payload:
            found = _find_json_value(value, names)
            if found is not None:
                return found
    return None


def _extract_element_text(raw: str, element_id: str) -> str | None:
    pattern = re.compile(
        rf"<(?P<tag>[a-zA-Z0-9:_-]+)\b[^>]*\bid\s*=\s*['\"]{re.escape(element_id)}['\"][^>]*>(?P<body>.*?)</(?P=tag)\s*>",
        re.I | re.S,
    )
    match = pattern.search(raw)
    return _strip_html(match.group("body")) if match else None


def _extract_timestamp(*texts: str) -> datetime | None:
    candidates: list[datetime] = []
    paris = ZoneInfo("Europe/Paris")
    patterns = (
        re.compile(r"(20\d{2})[-/](\d{1,2})[-/](\d{1,2})[T ](\d{1,2}):(\d{2})(?::(\d{1,2}))?"),
        re.compile(r"(\d{1,2})[./-](\d{1,2})[./-](20\d{2})\D{0,30}(\d{1,2}):(\d{2})(?::(\d{1,2}))?"),
    )
    for text in texts:
        for index, pattern in enumerate(patterns):
            for match in pattern.finditer(text or ""):
                values = [int(part or 0) for part in match.groups()]
                try:
                    if index == 0:
                        year, month, day, hour, minute, second = values
                    else:
                        day, month, year, hour, minute, second = values
                    candidates.append(datetime(year, month, day, hour, minute, second, tzinfo=paris))
                except ValueError:
                    continue
    return max(candidates) if candidates else None


BID_LABELS = {"bid", "bid price", "buy", "buy price", "achat", "cours achat", "geld", "geldkurs"}
ASK_LABELS = {"ask", "ask price", "sell", "sell price", "vente", "cours vente", "brief", "briefkurs"}


def _matches_label(label: str, choices: set[str]) -> bool:
    normalized = _normalize_label(label)
    return normalized in choices or any(normalized.endswith(f" {choice}") for choice in choices)


def _parse_order_book(raw: str, payload: Any | None = None) -> tuple[float | None, float | None, float | None, float | None]:
    bid = _parse_number(_find_json_value(payload, {"bid", "bestbid", "bidprice", "buy", "buyprice"}))
    ask = _parse_number(_find_json_value(payload, {"ask", "bestask", "askprice", "sell", "sellprice"}))
    bid_size = _parse_number(_find_json_value(payload, {"bidsize", "bidquantity", "bidvolume", "buyvolume"}))
    ask_size = _parse_number(_find_json_value(payload, {"asksize", "askquantity", "askvolume", "sellvolume"}))
    if bid and ask:
        return bid, ask, bid_size, ask_size

    parser = _TableParser()
    parser.feed(raw)
    for table in parser.tables:
        for header_index, header in enumerate(table):
            bid_indices = [i for i, cell in enumerate(header) if _matches_label(cell, BID_LABELS)]
            ask_indices = [i for i, cell in enumerate(header) if _matches_label(cell, ASK_LABELS)]
            if not bid_indices or not ask_indices:
                continue
            bid_index, ask_index = bid_indices[0], ask_indices[0]
            best_bid: tuple[float, float | None] | None = None
            best_ask: tuple[float, float | None] | None = None
            for row in table[header_index + 1 :]:
                if len(row) <= max(bid_index, ask_index):
                    continue
                row_bid = _parse_number(row[bid_index])
                row_ask = _parse_number(row[ask_index])
                row_bid_size = _parse_number(row[bid_index - 1]) if bid_index > 0 else None
                row_ask_size = _parse_number(row[ask_index + 1]) if ask_index + 1 < len(row) else None
                if row_bid and (best_bid is None or row_bid > best_bid[0]):
                    best_bid = (row_bid, row_bid_size)
                if row_ask and (best_ask is None or row_ask < best_ask[0]):
                    best_ask = (row_ask, row_ask_size)
            if best_bid and best_ask:
                return best_bid[0], best_ask[0], best_bid[1], best_ask[1]
    return bid, ask, bid_size, ask_size


class EuronextHttpSession:
    def __init__(self, *, timeout: float = 25.0, attempts: int = 3) -> None:
        self.timeout = timeout
        self.attempts = attempts
        self.opener = build_opener(HTTPCookieProcessor(CookieJar()))

    def get(self, url: str, *, referer: str | None = None) -> str:
        headers = {
            "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-GB,en;q=0.9,fr;q=0.8",
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/126 Safari/537.36 polsinelli-tracker/2.0",
        }
        if referer:
            headers["Referer"] = referer
            headers["X-Requested-With"] = "XMLHttpRequest"
        last_error: Exception | None = None
        for attempt in range(self.attempts):
            try:
                with self.opener.open(Request(url, headers=headers), timeout=self.timeout) as response:
                    return response.read().decode("utf-8", errors="replace")
            except HTTPError as exc:
                last_error = exc
                if exc.code not in {403, 429, 500, 502, 503, 504}:
                    break
            except URLError as exc:
                last_error = exc
            if attempt + 1 < self.attempts:
                time.sleep(0.5 * (2**attempt))
        if isinstance(last_error, HTTPError):
            raise ProviderError(f"Euronext HTTP {last_error.code} for {url}") from last_error
        if isinstance(last_error, URLError):
            raise ProviderError(f"Euronext connection failed: {last_error.reason}") from last_error
        raise ProviderError(f"Euronext request failed for {url}")


class EuronextProvider:
    name = "euronext"

    def __init__(
        self,
        *,
        default_mic: str = "XMLI",
        mic_candidates: tuple[str, ...] | list[str] | None = None,
        locale: str = "en",
        requester: Callable[..., str] | None = None,
    ) -> None:
        self.default_mic = str(default_mic or "XMLI").upper()
        candidates = tuple(str(item).upper() for item in (mic_candidates or DEFAULT_MIC_CANDIDATES))
        self.mic_candidates = tuple(dict.fromkeys((self.default_mic, *candidates)))
        self.locale = locale if locale in {"en", "fr", "de", "it", "pt", "nl"} else "en"
        self._session = EuronextHttpSession()
        self.requester = requester or self._session.get

    def resolve_instrument(self, position: dict[str, Any], mapping: dict[str, Any] | None = None) -> InstrumentRef:
        mapping = mapping or {}
        isin = str(position.get("isin", "")).strip().upper()
        if not re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9}\d", isin):
            raise ProviderConfigurationError(f"{position.get('id')}: valid ISIN required for Euronext")
        mic = str(mapping.get("mic") or self.default_mic).upper()
        symbol = str(mapping.get("symbol") or f"{isin}-{mic}").upper()
        return InstrumentRef(
            uic=None,
            asset_type="StructuredProduct",
            symbol=symbol,
            description=f"{position.get('asset', isin)} {position.get('productType', '')}".strip(),
        )

    def fetch_underlying_price(self, mapping: dict[str, Any]) -> float | None:
        return None

    def fetch_underlying_quote(self, mapping: dict[str, Any]) -> dict[str, Any] | None:
        """Fetch a separately timestamped Euronext underlying quote when mapped."""
        if mapping.get("underlyingSource") != "euronext":
            return None
        symbol = str(mapping.get("underlyingSymbol") or "").upper()
        if not re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9}\d-[A-Z0-9]{4}", symbol):
            raise ProviderConfigurationError("complete Euronext underlyingSymbol required")
        product_url = f"{BASE_URL}/{self.locale}/product/equities/{symbol}"
        detail_url = f"{BASE_URL}/{self.locale}/ajax/getDetailedQuote/{symbol}"
        product_html = self._request(product_url)
        detail_raw = self._request(detail_url, referer=product_url)
        detail_html, payload = _unwrap_payload(detail_raw)
        price = _parse_number(_extract_element_text(detail_html, "header-instrument-price"))
        if price is None:
            price = _parse_number(_find_json_value(payload, {"last", "lastprice", "price", "instrumentprice"}))
        quote_time = _extract_timestamp(_strip_html(detail_html), _strip_html(product_html))
        if price is None or quote_time is None:
            raise ProviderError(f"Euronext returned no confirmed underlying quote for {symbol}")
        quote_at = quote_time.astimezone(timezone.utc).isoformat(timespec="seconds")
        return {
            "price": price,
            "quoteAt": quote_at,
            "currency": mapping.get("underlyingCurrency"),
            "source": "euronext",
            "symbol": symbol,
        }

    def _request(self, url: str, *, referer: str | None = None) -> str:
        try:
            return self.requester(url, referer=referer)
        except TypeError:
            return self.requester(url)

    def _fetch_symbol_quote(
        self,
        position_id: str,
        symbol: str,
        instrument: InstrumentRef,
        *,
        session: str,
        underlying_price: float | None,
    ) -> QuoteSnapshot:
        product_url = f"{BASE_URL}/{self.locale}/product/structured-products/{symbol}"
        detail_url = f"{BASE_URL}/{self.locale}/ajax/getDetailedQuote/{symbol}"
        orderbook_url = f"{BASE_URL}/{self.locale}/popout-page/getOrderBook/{symbol}"

        try:
            product_html = self._request(product_url)
        except ProviderError:
            product_html = ""
        detail_raw = self._request(detail_url, referer=product_url)
        order_raw = self._request(orderbook_url, referer=product_url)
        detail_html, detail_payload = _unwrap_payload(detail_raw)
        order_html, order_payload = _unwrap_payload(order_raw)

        bid, ask, bid_size, ask_size = _parse_order_book(order_html, order_payload)
        if not bid or not ask:
            bid, ask, bid_size, ask_size = _parse_order_book(detail_html, detail_payload)
        if not bid or not ask:
            raise ProviderError(f"Euronext returned no usable bid/ask for {symbol}")
        if ask < bid:
            raise ProviderError(f"Euronext ask is lower than bid for {symbol}")

        last = _parse_number(_extract_element_text(detail_html, "header-instrument-price"))
        if last is None:
            last = _parse_number(
                _find_json_value(detail_payload, {"last", "lastprice", "price", "instrumentprice"})
            )
        price = last if last is not None and bid <= last <= ask else (bid + ask) / 2
        if underlying_price is None:
            underlying_price = _parse_number(
                _find_json_value(detail_payload, {"underlyingprice", "underlyinglastprice"})
            )

        retrieved_at = utc_now_iso()
        detail_text = _strip_html(detail_html)
        order_text = _strip_html(order_html)
        quote_time = _extract_timestamp(detail_text, order_text, product_html)
        if quote_time is None:
            quote_at = None
            delayed_minutes = None
            confidence = "low"
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
            timestamp_source="euronext_page" if quote_time is not None else "unknown",
            market_status=euronext_market_status(quote_utc if quote_time is not None else None),
            is_indicative=quote_time is None,
            bid_size=bid_size,
            ask_size=ask_size,
            underlying_price=underlying_price,
            underlying_quote_at=quote_at if underlying_price is not None else None,
            quote_currency="EUR",
            underlying_currency="EUR" if underlying_price is not None else None,
            instrument_uic=None,
            instrument_asset_type=instrument.asset_type,
            instrument_symbol=symbol,
        )

    def fetch_instrument_quote(
        self,
        position_id: str,
        instrument: InstrumentRef,
        *,
        session: str,
        underlying_price: float | None = None,
    ) -> QuoteSnapshot:
        if not instrument.symbol:
            raise ProviderConfigurationError(f"{position_id}: Euronext symbol is missing")
        preferred = instrument.symbol.upper()
        isin = preferred.split("-", 1)[0]
        symbols = tuple(
            dict.fromkeys((preferred, *(f"{isin}-{mic}" for mic in self.mic_candidates)))
        )
        errors: list[str] = []
        for symbol in symbols:
            try:
                return self._fetch_symbol_quote(
                    position_id,
                    symbol,
                    instrument,
                    session=session,
                    underlying_price=underlying_price,
                )
            except ProviderError as exc:
                errors.append(f"{symbol}: {exc}")
        details = "; ".join(errors[-3:])
        raise ProviderError(f"{position_id}: no Euronext quote across {', '.join(symbols)} ({details})")
