#!/usr/bin/env python3
"""Saxo OpenAPI market-data provider.

The provider reads indicative bid/ask quotes from the official Info Prices API.
Real-time versus delayed data depends on the market-data subscriptions attached
to the Saxo access token.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode

from .base import (
    InstrumentRef,
    ProviderConfigurationError,
    ProviderError,
    QuoteSnapshot,
    parse_iso_datetime,
    request_json,
    require_positive_number,
    utc_now_iso,
)

SUPPORTED_LEVERAGED_ASSET_TYPES = (
    "Warrant",
    "WarrantKnockOut",
    "WarrantOpenEndKnockOut",
    "WarrantOtherLeverageWithKnockOut",
    "WarrantOtherLeverageWithoutKnockOut",
    "WarrantDoubleKnockOut",
    "WarrantSpread",
    "InlineWarrant",
    "MiniFuture",
    "CertificateConstantLeverage",
    "CertificateOtherConstantLeverage",
    "CertificateTracker",
)


class SaxoProvider:
    name = "saxo"

    def __init__(
        self,
        *,
        token: str | None = None,
        environment: str | None = None,
        account_key: str | None = None,
        requester=request_json,
    ) -> None:
        self.token = token or os.getenv("SAXO_ACCESS_TOKEN")
        self.environment = (environment or os.getenv("SAXO_ENV", "sim")).lower()
        self.account_key = account_key or os.getenv("SAXO_ACCOUNT_KEY")
        self.requester = requester
        if not self.token:
            raise ProviderConfigurationError("SAXO_ACCESS_TOKEN is not configured")
        if self.environment not in {"sim", "live"}:
            raise ProviderConfigurationError("SAXO_ENV must be sim or live")
        self.base_url = (
            "https://gateway.saxobank.com/sim/openapi"
            if self.environment == "sim"
            else "https://gateway.saxobank.com/openapi"
        )

    def _get(self, path: str, params: dict[str, Any]) -> tuple[dict[str, Any], str]:
        clean = {key: value for key, value in params.items() if value is not None}
        url = f"{self.base_url}{path}?{urlencode(clean)}"
        return self.requester(url, token=self.token), url

    @staticmethod
    def _candidate_asset_types(position: dict[str, Any]) -> tuple[str, ...]:
        product_type = str(position.get("productType", "")).lower()
        if "mini" in product_type:
            return ("MiniFuture",)
        if "turbo" in product_type or "certificat" in product_type:
            return (
                "MiniFuture",
                "WarrantKnockOut",
                "WarrantOpenEndKnockOut",
                "WarrantOtherLeverageWithKnockOut",
                "CertificateConstantLeverage",
                "CertificateOtherConstantLeverage",
            )
        return SUPPORTED_LEVERAGED_ASSET_TYPES

    @staticmethod
    def _score_candidate(
        candidate: dict[str, Any],
        *,
        mnemo: str,
        isin: str,
        asset: str,
        allowed_asset_types: tuple[str, ...],
    ) -> int:
        symbol = str(candidate.get("Symbol", "")).upper().replace(" ", "")
        description = str(candidate.get("Description", "")).upper()
        asset_type = str(candidate.get("AssetType", ""))
        tradable_as = candidate.get("TradableAs") or []
        matched_keywords = set(candidate.get("_matchedKeywords") or [])
        score = 0
        if isin and isin in matched_keywords:
            score += 120
        if mnemo and symbol == mnemo:
            score += 110
        elif mnemo and mnemo in symbol:
            score += 90
        if mnemo and mnemo in matched_keywords:
            score += 25
        if asset and asset.upper() in description:
            score += 20
        if asset_type in allowed_asset_types:
            score += 10
        elif any(item in allowed_asset_types for item in tradable_as):
            score += 5
        if candidate.get("IsKeywordMatch"):
            score += 3
        if candidate.get("SummaryType") == "Instrument":
            score += 2
        return score

    def _search_instruments(
        self, keyword: str, asset_types: tuple[str, ...] | None
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "Keywords": keyword,
            "IncludeNonTradable": "true",
            "$top": 100,
            "AccountKey": self.account_key,
        }
        if asset_types:
            params["AssetTypes"] = ",".join(asset_types)
        payload, _ = self._get("/ref/v1/instruments", params)
        data = payload.get("Data", [])
        results: list[dict[str, Any]] = []
        if isinstance(data, list):
            for item in data:
                if not isinstance(item, dict):
                    continue
                copied = dict(item)
                copied["_matchedKeywords"] = [keyword]
                results.append(copied)
        return results

    def resolve_instrument(
        self,
        position: dict[str, Any],
        mapping: dict[str, Any] | None = None,
    ) -> InstrumentRef:
        mapping = mapping or {}
        if isinstance(mapping.get("uic"), int) and mapping.get("assetType"):
            return InstrumentRef(
                uic=int(mapping["uic"]),
                asset_type=str(mapping["assetType"]),
                symbol=mapping.get("symbol"),
                description=mapping.get("description"),
            )

        mnemo = str(position.get("mnemo", "")).strip().upper()
        isin = str(position.get("isin", "")).strip().upper()
        asset = str(position.get("asset", "")).strip()
        keywords = [value for value in (mnemo, isin) if value]
        if not keywords:
            raise ProviderError(f"{position.get('id')}: no mnémonique or ISIN to resolve")

        allowed = self._candidate_asset_types(position)
        candidates: list[dict[str, Any]] = []
        for keyword in keywords:
            candidates.extend(self._search_instruments(keyword, allowed))

        # Some structured products are classified differently across Saxo setups.
        # A second pass without AssetTypes is safe because the candidate still has
        # to match the mnemonic or the exact ISIN search before it is accepted.
        if not candidates:
            for keyword in keywords:
                candidates.extend(self._search_instruments(keyword, None))

        unique: dict[tuple[int, str], dict[str, Any]] = {}
        for candidate in candidates:
            identifier = candidate.get("Identifier")
            asset_type = candidate.get("AssetType")
            if not isinstance(identifier, int) or not isinstance(asset_type, str):
                continue
            key = (identifier, asset_type)
            if key in unique:
                matched = set(unique[key].get("_matchedKeywords") or [])
                matched.update(candidate.get("_matchedKeywords") or [])
                unique[key]["_matchedKeywords"] = sorted(matched)
            else:
                unique[key] = candidate

        ranked = sorted(
            unique.values(),
            key=lambda item: self._score_candidate(
                item,
                mnemo=mnemo,
                isin=isin,
                asset=asset,
                allowed_asset_types=allowed,
            ),
            reverse=True,
        )
        if not ranked:
            raise ProviderError(
                f"{position.get('id')}: instrument absent from Saxo "
                f"{self.environment.upper()} catalogue/access rights after searches "
                f"for {mnemo or 'no mnemo'} and {isin or 'no ISIN'}; OAuth is working"
            )

        best_score = self._score_candidate(
            ranked[0],
            mnemo=mnemo,
            isin=isin,
            asset=asset,
            allowed_asset_types=allowed,
        )
        second_score = (
            self._score_candidate(
                ranked[1],
                mnemo=mnemo,
                isin=isin,
                asset=asset,
                allowed_asset_types=allowed,
            )
            if len(ranked) > 1
            else -1
        )
        if best_score < 50 or best_score == second_score:
            sample = ", ".join(
                f"{item.get('Symbol', '?')} [{item.get('AssetType', '?')}]"
                for item in ranked[:5]
            )
            raise ProviderError(
                f"{position.get('id')}: ambiguous Saxo instrument mapping ({sample}); "
                "set uic and assetType in market-data-config.json"
            )

        best = ranked[0]
        return InstrumentRef(
            uic=int(best["Identifier"]),
            asset_type=str(best["AssetType"]),
            symbol=str(best.get("Symbol")) if best.get("Symbol") else None,
            description=str(best.get("Description")) if best.get("Description") else None,
        )

    def fetch_instrument_quote(
        self,
        position_id: str,
        instrument: InstrumentRef,
        *,
        session: str,
        underlying_price: float | None = None,
    ) -> QuoteSnapshot:
        params: dict[str, Any] = {
            "Uic": instrument.uic,
            "AssetType": instrument.asset_type,
            "FieldGroups": "Quote,PriceInfoDetails,InstrumentPriceDetails,DisplayAndFormat",
            "AccountKey": self.account_key,
        }
        payload, source_url = self._get("/trade/v1/infoprices", params)
        quote = payload.get("Quote")
        if not isinstance(quote, dict):
            raise ProviderError(f"{position_id}: Saxo response has no Quote object")
        if quote.get("ErrorCode") not in (None, "", "None"):
            raise ProviderError(f"{position_id}: Saxo quote error {quote.get('ErrorCode')}")

        bid = require_positive_number(quote.get("Bid"), "bid")
        ask = require_positive_number(quote.get("Ask"), "ask")
        if ask < bid:
            raise ProviderError(f"{position_id}: ask is lower than bid")
        mid = quote.get("Mid")
        price = float(mid) if isinstance(mid, (int, float)) and mid > 0 else (bid + ask) / 2

        retrieved_at = utc_now_iso()
        last_updated = payload.get("LastUpdated")
        quote_time = parse_iso_datetime(last_updated if isinstance(last_updated, str) else None)
        delayed = quote.get("DelayedByMinutes", 0)
        delayed_minutes = int(delayed) if isinstance(delayed, (int, float)) and delayed >= 0 else 0
        quote_at = quote_time.isoformat(timespec="seconds") if quote_time is not None else None

        details = payload.get("PriceInfoDetails")
        details = details if isinstance(details, dict) else {}
        raw_market_state = payload.get("MarketState") or details.get("MarketState")
        market_status = (
            "open" if str(raw_market_state).lower() in {"open", "continuous"}
            else "closed" if str(raw_market_state).lower() in {"closed", "postmarket", "premarket"}
            else "unknown"
        )
        confidence = "high" if delayed_minutes == 0 else "medium"
        return QuoteSnapshot(
            position_id=position_id,
            provider=self.name,
            session=session,
            price=price,
            bid=bid,
            ask=ask,
            quote_at=quote_at,
            retrieved_at=retrieved_at,
            source_url=source_url,
            source_confidence=confidence,
            delayed_by_minutes=delayed_minutes,
            timestamp_source="provider_last_updated" if quote_time is not None else "unknown",
            market_status=market_status,
            is_indicative=quote_time is None,
            bid_size=float(details["BidSize"])
            if isinstance(details.get("BidSize"), (int, float))
            else None,
            ask_size=float(details["AskSize"])
            if isinstance(details.get("AskSize"), (int, float))
            else None,
            underlying_price=underlying_price,
            instrument_uic=instrument.uic,
            instrument_asset_type=instrument.asset_type,
            instrument_symbol=instrument.symbol,
        )

    def fetch_underlying_price(self, mapping: dict[str, Any]) -> float | None:
        uic = mapping.get("underlyingUic")
        asset_type = mapping.get("underlyingAssetType")
        if not isinstance(uic, int) or not asset_type:
            return None
        payload, _ = self._get(
            "/trade/v1/infoprices",
            {
                "Uic": uic,
                "AssetType": asset_type,
                "FieldGroups": "Quote",
                "AccountKey": self.account_key,
            },
        )
        quote = payload.get("Quote")
        if not isinstance(quote, dict):
            return None
        mid = quote.get("Mid")
        if isinstance(mid, (int, float)) and mid > 0:
            return float(mid)
        bid, ask = quote.get("Bid"), quote.get("Ask")
        if all(isinstance(value, (int, float)) and value > 0 for value in (bid, ask)):
            return (float(bid) + float(ask)) / 2
        return None
