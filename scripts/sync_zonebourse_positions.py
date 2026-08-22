#!/usr/bin/env python3
"""Synchronise les positions ouvertes et les sorties explicites de Zonebourse."""
from __future__ import annotations

import json
import hashlib
import os
import re
import sys
import unicodedata
from html import unescape
from datetime import datetime, timedelta
from pathlib import Path

try:
    from .data_quality import refresh_data_quality
except ImportError:
    from data_quality import refresh_data_quality

try:
    from .fast_zonebourse_scan_v2 import (
        PARIS, Parser, canonical_article_url, fetch_text, parse_html_articles,
    )
except ImportError:  # Exécution directe: python scripts/sync_zonebourse_positions.py
    from fast_zonebourse_scan_v2 import (
        PARIS, Parser, canonical_article_url, fetch_text, parse_html_articles,
    )

OPEN_URLS = (
    "https://www.zonebourse.com/bourse/derives/recommandations/",
    "https://ch.zonebourse.com/bourse/derives/recommandations/",
    # This consolidated page contains both active recommendations and recent
    # events and has remained available when the active-only page returned an
    # empty shell to datacenter addresses.
    "https://www.zonebourse.com/bourse/derives/recommandations/historique/",
)
CLOSED_URLS = (
    "https://www.zonebourse.com/bourse/derives/recommandations/cloturees/",
    "https://ch.zonebourse.com/bourse/derives/recommandations/cloturees/",
    "https://www.zonebourse.com/bourse/derives/recommandations/historique/",
)
CLOSED_CARD_RE = re.compile(
    r"^(?P<asset>.+?)\s+(?P<product>TURBO|WARRANT)\s+-\s+"
    r"(?P<code>[A-Z0-9]+)\s+-\s+(?P<day>\d{2})/(?P<month>\d{2})\s+"
    r"(?P<title>.+?)\s+Prix d.exercice\s*Entrée\s*Sortie\s*Performance\s+"
    r"(?:[-\d\s,.]+\s*€|-)\s+(?P<entry>\d+[,.]\d+)\s+"
    r"(?P<exit>\d+[,.]\d+)\s+(?P<performance>[+-]\d+[,.]\d+)\s*%\s+SORTIE$",
    re.IGNORECASE,
)
ISIN_RE = re.compile(
    r"\b(?:ISIN\s*(?:\||:)?[\s-]*)?([A-Z]{2}[A-Z0-9]{9}\d)\b",
    re.IGNORECASE,
)


def article_text(raw: str) -> str:
    """Normalise aussi bien une page HTML qu'un rendu texte Jina."""
    without_scripts = re.sub(r"<(?:script|style)\b[^>]*>.*?</(?:script|style)>", " ", raw, flags=re.I | re.S)
    without_tags = re.sub(r"<[^>]+>", " ", without_scripts)
    return " ".join(unescape(without_tags).replace("**", " ").split())


def first_number(text: str, patterns: tuple[str, ...]) -> float | None:
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.I)
        if match:
            try:
                return number(match.group(1))
            except ValueError:
                continue
    return None


def unique_number(text: str, patterns: tuple[str, ...]) -> tuple[float | None, bool]:
    values: set[float] = set()
    for pattern in patterns:
        for match in re.finditer(pattern, text, flags=re.I):
            try:
                values.add(number(match.group(1)))
            except ValueError:
                pass
    return (next(iter(values)), False) if len(values) == 1 else (None, len(values) > 1)


def parse_article_details(raw: str) -> dict:
    """Extrait uniquement les valeurs explicitement publiées sur la fiche."""
    text = article_text(raw)
    isin_match = ISIN_RE.search(text)
    entry_patterns = (
        r"Cours d['’]entrée\s*(?:\||:)?[\s€]*([\d\s.,]+)",
        r"Prix d['’]entrée\s*(?:\||:)?[\s€]*([\d\s.,]+)",
        r"\bEntrée\s*(?:\||:)?[\s€]*([\d]+[,.]\d+)",
        r"(?:acheté|recommandé)\s+(?:à|au cours de)\s+([\d\s.,]+)\s*€",
    )
    target_patterns = (
        r"Objectif de cours\s*(?:\||:)?\s*([\d\s.,]+)",
        r"Objectif\s*(?:\||:)?\s*([\d\s.,]+)\s*(?:EUR|USD|€)",
    )
    stop_patterns = (
        r"seuil d['’]invalidation[^\d]{0,100}([\d\s.,]+)\s*(?:EUR|USD|€)",
        r"Opinion\s*(?:\||:)?\s*(?:Positive|Négative|Negative)\s+(?:au-dessus|au dessus|sous|en-dessous)\s+(?:(?:de|des?|les?)\s+)?([\d\s.,]+)\s*(?:EUR|USD|€)",
        r"Invalidation\s*:?\s*([\d\s.,]+)\s*(?:EUR|USD|€)",
        r"Stop(?: loss)?\s*:?\s*([\d\s.,]+)\s*(?:EUR|USD|€)",
    )
    def structured_values(keys: set[str]) -> set[float]:
        found: set[float] = set()
        for block in re.findall(r'<script[^>]+type=["\']application/(?:ld\+)?json["\'][^>]*>(.*?)</script>', raw, re.I | re.S):
            try:
                payload = json.loads(unescape(block))
            except (json.JSONDecodeError, TypeError):
                continue
            stack = [payload]
            while stack:
                item = stack.pop()
                if isinstance(item, dict):
                    for key, value in item.items():
                        if key.lower() in keys and isinstance(value, (int, float, str)):
                            try:
                                found.add(float(str(value).replace(" ", "").replace(",", ".")))
                            except ValueError:
                                pass
                        elif isinstance(value, (dict, list)):
                            stack.append(value)
                elif isinstance(item, list):
                    stack.extend(item)
        return found

    structured_entry = structured_values({"entryprice", "entry_price"})
    structured_target = structured_values({"target", "targetprice", "target_price"})
    structured_stop = structured_values({"stop", "stopprice", "invalidationlevel"})
    def preferred(structured: set[float], patterns: tuple[str, ...]) -> tuple[float | None, bool]:
        if structured:
            return (next(iter(structured)), False) if len(structured) == 1 else (None, True)
        return unique_number(text, patterns)
    entry, entry_ambiguous = preferred(structured_entry, entry_patterns)
    target, target_ambiguous = preferred(structured_target, target_patterns)
    stop, stop_ambiguous = preferred(structured_stop, stop_patterns)
    values = {
        "isin": isin_match.group(1).upper() if isin_match else None,
        "entryPrice": entry,
        "target": target,
        "stop": stop,
        "riskLevelsCurrency": (
            next(iter(set(re.findall(r"(?:Objectif|invalidation|Opinion)[^€$]{0,120}\b(EUR|USD)\b", text, re.I)))).upper()
            if len(set(value.upper() for value in re.findall(r"(?:Objectif|invalidation|Opinion)[^€$]{0,120}\b(EUR|USD)\b", text, re.I))) == 1
            else "EUR" if "€" in text else None
        ),
    }
    values["extraction"] = {
        "method": "structured_json" if any((structured_entry, structured_target, structured_stop)) else "normalized_text_regex",
        "selector": "script[type=application/json]" if any((structured_entry, structured_target, structured_stop)) else "document_text",
        "sourceExcerpt": text[:500],
        "ambiguous": entry_ambiguous or target_ambiguous or stop_ambiguous,
    }
    return values


def recommendation_identity(card: dict) -> str:
    """Stable composite identity; URLs and mnemonics alone are not identities."""
    parts = (
        str(card.get("isin") or "").upper(),
        str(card.get("productCode") or card.get("mnemo") or "").upper(),
        str(card.get("publishedAt") or card.get("entryDate") or "")[:10],
        str(card.get("author") or "zonebourse").lower(),
        str(card.get("eventType") or "recommendation").lower(),
    )
    return "|".join(parts)


def content_hash(card: dict) -> str:
    material = {key: card.get(key) for key in (
        "title", "isin", "productCode", "publishedAt", "direction",
        "entryPrice", "target", "stop", "exitPrice",
    )}
    encoded = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def fetch_article(url: str) -> str:
    try:
        return fetch_text(url, attempts=1)
    except Exception as direct_error:
        parsed = url.split("://", 1)[-1]
        try:
            return fetch_text(f"https://r.jina.ai/http://{parsed}", attempts=1)
        except Exception as fallback_error:
            raise RuntimeError(f"direct: {direct_error}; jina: {fallback_error}") from fallback_error


def enrich_current_cards(document: dict, current: list[dict]) -> list[str]:
    """Complète progressivement les cartes incomplètes depuis leurs articles."""
    positions_by_code = {
        item.get("mnemo"): item
        for item in document.get("positions", [])
        if item.get("status") == "open"
    }
    maximum = max(1, int(os.getenv("POLSINELLI_MAX_ARTICLE_FETCHES", "10")))
    errors: list[str] = []
    fetched = 0
    for card in current:
        existing = positions_by_code.get(card.get("productCode"), {})
        needed = any(not existing.get(field) for field in ("isin", "entryPrice", "target", "stop"))
        if not needed or fetched >= maximum or "/actualite-bourse/" not in card.get("url", ""):
            continue
        fetched += 1
        try:
            details = parse_article_details(fetch_article(card["url"]))
        except Exception as exc:
            errors.append(f"{card.get('productCode')}: {exc}")
            continue
        for field, value in details.items():
            if value is not None:
                card[field] = value
    return errors


def number(value: str) -> float:
    return float(value.replace(" ", "").replace("\u202f", "").replace(",", "."))


def slug(value: str) -> str:
    ascii_value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", ascii_value.lower()).strip("-")


def dated(day: int, month: int, now: datetime) -> datetime:
    value = datetime(now.year, month, day, tzinfo=PARIS)
    return value.replace(year=now.year - 1) if value > now + timedelta(days=31) else value


def fetch_first(urls: tuple[str, ...]) -> tuple[str, str]:
    errors: list[str] = []
    for url in urls:
        try:
            return fetch_text(url), url
        except Exception as exc:
            errors.append(f"{url}: {exc}")
    raise RuntimeError(" | ".join(errors))


def fetch_pages(base_urls: tuple[str, ...], max_pages: int = 10) -> list[tuple[str, str]]:
    """Charge toutes les pages disponibles depuis la première édition joignable."""
    errors: list[str] = []
    for base_url in base_urls:
        pages: list[tuple[str, str]] = []
        try:
            for page in range(1, max_pages + 1):
                url = base_url if page == 1 else f"{base_url}?p={page}"
                html = fetch_text(url)
                parser = Parser()
                parser.feed(html)
                if page > 1 and not parser.links:
                    break
                pages.append((html, url))
            if pages:
                return pages
        except Exception as exc:
            errors.append(f"{base_url}: {exc}")
    raise RuntimeError(" | ".join(errors))


def parse_closed_cards(html: str, base_url: str, now: datetime) -> list[dict]:
    parser = Parser()
    parser.feed(html)
    rows: list[dict] = []
    seen: set[str] = set()
    for href, raw in parser.links:
        text = " ".join(raw.split())
        match = CLOSED_CARD_RE.match(text)
        url = canonical_article_url(href, base_url)
        if not match or not url or url in seen:
            continue
        seen.add(url)
        closed_at = dated(int(match.group("day")), int(match.group("month")), now)
        rows.append(
            {
                "asset": match.group("asset").strip(),
                "productType": match.group("product").title(),
                "mnemo": match.group("code").upper(),
                "title": match.group("title").strip(),
                "entryPrice": number(match.group("entry")),
                "exitPrice": number(match.group("exit")),
                "realizedReturnPct": number(match.group("performance")),
                "exitDate": closed_at.date().isoformat(),
                "url": url,
            }
        )
    return rows


def new_position(card: dict, detected_at: str) -> dict:
    entry_date = card["publishedAt"][:10]
    return {
        "id": f"{slug(card['underlying'])}-{card['productCode'].lower()}-{entry_date.replace('-', '')}",
        "asset": card["underlying"],
        "productType": card["productType"].title(),
        "direction": card.get("direction")
        or ("PUT" if "PUT" in card["title"].upper() else "CALL"),
        "mnemo": card["productCode"],
        "isin": card.get("isin"),
        "status": "open",
        "recommendationStatus": "open",
        "portfolioStatus": "not_held",
        "entryPrice": card.get("entryPrice"),
        "currentPrice": None,
        "exitPrice": None,
        "entryDate": entry_date,
        "exitDate": None,
        "currency": "EUR",
        "url": card["url"],
        "publishedAt": card["publishedAt"],
        "publishedAtPrecision": card.get("publishedAtPrecision", "minute"),
        "detectedAt": detected_at,
        "lastSeenOpenAt": detected_at,
        "missingOpenCycles": 0,
        "recommendationIdentity": recommendation_identity(card),
        "contentHash": content_hash(card),
        "mutationHistory": [],
        "target": card.get("target"),
        "stop": card.get("stop"),
        "riskLevelsCurrency": card.get("riskLevelsCurrency"),
        "note": f"{card['title']}. Prix d'entrée et ISIN à confirmer depuis la fiche article.",
        "sourceConfidence": "medium",
    }


def synchronize(document: dict, current: list[dict], closed: list[dict], now: datetime) -> dict:
    positions = document.setdefault("positions", [])
    open_by_code = {
        item.get("mnemo"): item for item in positions if item.get("status") == "open"
    }
    open_by_identity = {
        item.get("recommendationIdentity"): item
        for item in positions
        if item.get("status") == "open" and item.get("recommendationIdentity")
    }
    detected_at = now.isoformat(timespec="seconds")
    added = 0
    closed_count = 0
    changed = False
    seen_ids: set[str] = set()

    for card in current:
        direction = card.get("direction")
        target, stop = card.get("target"), card.get("stop")
        incoherent = (
            isinstance(target, (int, float)) and isinstance(stop, (int, float))
            and ((direction == "CALL" and target <= stop) or (direction == "PUT" and target >= stop))
        )
        if card.get("extraction", {}).get("ambiguous") or incoherent:
            document.setdefault("meta", {}).setdefault("quarantine", []).append({
                "url": card.get("url"), "productCode": card.get("productCode"),
                "detectedAt": detected_at,
                "reason": "ambiguous_extraction" if card.get("extraction", {}).get("ambiguous") else "incoherent_risk_levels",
            })
            for field in ("entryPrice", "target", "stop"):
                card.pop(field, None)
        identity = recommendation_identity(card)
        existing = open_by_identity.get(identity)
        if existing is None:
            code_match = open_by_code.get(card["productCode"])
            same_date = str(code_match.get("publishedAt") or code_match.get("entryDate") or "")[:10] == str(card.get("publishedAt") or "")[:10] if code_match else False
            if code_match and (not code_match.get("recommendationIdentity") or same_date):
                existing = code_match
        if existing:
            seen_ids.add(existing.get("id") or existing.get("mnemo") or card["productCode"])
            previous_hash = existing.get("contentHash")
            new_hash = content_hash(card)
            if previous_hash and previous_hash != new_hash:
                existing.setdefault("mutationHistory", []).append({
                    "changedAt": detected_at,
                    "previousHash": previous_hash,
                    "eventType": "modification",
                })
                changed = True
            for key, value in (
                ("contentHash", new_hash), ("recommendationIdentity", identity),
                ("lastSeenOpenAt", detected_at), ("missingOpenCycles", 0),
                ("recommendationStatus", "open"),
            ):
                if existing.get(key) != value:
                    existing[key] = value
                    changed = True
            if existing.get("url") != card["url"]:
                existing["url"] = card["url"]
                changed = True
            if not existing.get("publishedAt"):
                existing["publishedAt"] = card["publishedAt"]
                changed = True
            precision = card.get("publishedAtPrecision", "minute")
            if existing.get("publishedAtPrecision") != precision:
                existing["publishedAtPrecision"] = precision
                changed = True
            if not existing.get("detectedAt"):
                existing["detectedAt"] = detected_at
                changed = True
            for destination, source in (
                ("asset", "underlying"),
                ("productType", "productType"),
                ("direction", "direction"),
            ):
                value = card.get(source)
                if value and not existing.get(destination):
                    existing[destination] = value.title() if destination == "productType" else value
                    changed = True
            for field in ("isin", "entryPrice", "target", "stop", "riskLevelsCurrency"):
                value = card.get(field)
                if value is not None and existing.get(field) != value:
                    existing[field] = value
                    changed = True
            if any(card.get(field) is not None for field in ("isin", "entryPrice", "target", "stop")):
                existing["sourceConfidence"] = "high"
            continue
        position = new_position(card, detected_at)
        positions.append(position)
        seen_ids.add(position["id"])
        open_by_code[position["mnemo"]] = position
        open_by_identity[position["recommendationIdentity"]] = position
        added += 1
        changed = True

    for row in closed:
        position = open_by_code.get(row["mnemo"])
        if not position:
            continue
        position.update(
            {
                "status": "closed",
                "recommendationStatus": "closed",
                "entryPrice": row["entryPrice"],
                "currentPrice": None,
                "exitPrice": row["exitPrice"],
                "exitDate": row["exitDate"],
                "quoteAt": None,
                "url": row["url"],
                "note": f"{row['title']} ({row['realizedReturnPct']:+.2f} % selon Zonebourse).",
                "realizedReturnPct": row["realizedReturnPct"],
                "sourceConfidence": "high",
            }
        )
        for key in (
            "bid", "ask", "marketDataProvider", "marketDataSourceUrl",
            "marketDataRetrievedAt", "marketDataDelayMinutes",
        ):
            position.pop(key, None)
        closed_count += 1
        changed = True

    # A disappearance is evidence of uncertainty, never proof of closure.
    closed_codes = {row.get("mnemo") for row in closed}
    for position in positions:
        if position.get("status") != "open" or position.get("mnemo") in closed_codes:
            continue
        if (position.get("id") or position.get("mnemo")) in seen_ids:
            continue
        cycles = int(position.get("missingOpenCycles", 0)) + 1
        status = "UNKNOWN" if cycles >= 3 else "PENDING_RECONCILIATION"
        if position.get("missingOpenCycles") != cycles or position.get("recommendationStatus") != status:
            position["missingOpenCycles"] = cycles
            position["recommendationStatus"] = status
            position.setdefault("mutationHistory", []).append({
                "changedAt": detected_at,
                "eventType": "missing_from_open_listing",
                "missingOpenCycles": cycles,
            })
            changed = True

    meta = document.setdefault("meta", {})
    expected_meta = {
            "generatedAt": detected_at,
            "source": "Zonebourse / recommandations produits dérivés",
            "lastPublicationCheckedAt": detected_at,
            "openRecommendationCount": len(current),
            "automaticSync": True,
    }
    if meta.get("automaticSync") is not True or meta.get("openRecommendationCount") != len(current):
        changed = True
    if changed:
        meta.update(expected_meta)
    quality_updated = refresh_data_quality(positions)
    changed = bool(changed or quality_updated)
    return {
        "added": added,
        "closed": closed_count,
        "openSeen": len(current),
        "changed": changed,
        "qualityUpdated": quality_updated,
    }


def main() -> int:
    now = datetime.now(PARIS)
    positions_path = Path("positions.json")
    try:
        current_by_url: dict[str, dict] = {}
        for open_html, open_source in fetch_pages(OPEN_URLS):
            for card in parse_html_articles(open_html, open_source, recommendations_only=True):
                current_by_url[card["url"]] = card
        current = list(current_by_url.values())
        if not current:
            raise RuntimeError("aucune recommandation ouverte extraite")
        enrichment_errors = enrich_current_cards(
            json.loads(positions_path.read_text(encoding="utf-8")), current
        )
        closed_by_url: dict[str, dict] = {}
        for closed_html, closed_source in fetch_pages(CLOSED_URLS, max_pages=5):
            for card in parse_closed_cards(closed_html, closed_source, now):
                closed_by_url[card["url"]] = card
        closed = list(closed_by_url.values())
    except Exception as exc:
        print(f"::warning title=Synchronisation Zonebourse ignorée::{exc}")
        return 2

    document = json.loads(positions_path.read_text(encoding="utf-8"))
    result = synchronize(document, current, closed, now)
    result["enrichmentErrors"] = enrichment_errors
    if result["changed"]:
        positions_path.write_text(
            json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    output = os.getenv("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as handle:
            handle.write(
                f"changed={'true' if result['changed'] else 'false'}\n"
                f"added={result['added']}\nclosed={result['closed']}\n"
            )
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"sync_zonebourse_positions error: {exc}", file=sys.stderr)
        raise
