#!/usr/bin/env python3
"""Synchronise les positions ouvertes et les sorties explicites de Zonebourse."""
from __future__ import annotations

import json
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
)
CLOSED_URLS = (
    "https://www.zonebourse.com/bourse/derives/recommandations/cloturees/",
    "https://ch.zonebourse.com/bourse/derives/recommandations/cloturees/",
)
CLOSED_CARD_RE = re.compile(
    r"^(?P<asset>.+?)\s+(?P<product>TURBO|WARRANT)\s+-\s+"
    r"(?P<code>[A-Z0-9]+)\s+-\s+(?P<day>\d{2})/(?P<month>\d{2})\s+"
    r"(?P<title>.+?)\s+Prix d.exercice\s*Entrée\s*Sortie\s*Performance\s+"
    r"(?:[-\d\s,.]+\s*€|-)\s+(?P<entry>\d+[,.]\d+)\s+"
    r"(?P<exit>\d+[,.]\d+)\s+(?P<performance>[+-]\d+[,.]\d+)\s*%\s+SORTIE$",
    re.IGNORECASE,
)
ISIN_RE = re.compile(r"\b(?:ISIN\s*:?[\s-]*)?([A-Z]{2}[A-Z0-9]{9}\d)\b", re.IGNORECASE)


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


def parse_article_details(raw: str) -> dict:
    """Extrait uniquement les valeurs explicitement publiées sur la fiche."""
    text = article_text(raw)
    isin_match = ISIN_RE.search(text)
    return {
        "isin": isin_match.group(1).upper() if isin_match else None,
        "entryPrice": first_number(text, (
            r"Cours d['’]entrée\s*:?[\s€]*([\d\s.,]+)",
            r"Prix d['’]entrée\s*:?[\s€]*([\d\s.,]+)",
            r"\bEntrée\s*:?[\s€]*([\d]+[,.]\d+)",
            r"(?:acheté|recommandé)\s+(?:à|au cours de)\s+([\d\s.,]+)\s*€",
        )),
        "target": first_number(text, (
            r"Objectif de cours\s*:?\s*([\d\s.,]+)",
            r"Objectif\s*:?\s*([\d\s.,]+)\s*(?:EUR|USD|€)",
        )),
        "stop": first_number(text, (
            r"seuil d['’]invalidation[^\d]{0,100}([\d\s.,]+)\s*(?:EUR|USD|€)",
            r"Invalidation\s*:?\s*([\d\s.,]+)\s*(?:EUR|USD|€)",
            r"Stop(?: loss)?\s*:?\s*([\d\s.,]+)\s*(?:EUR|USD|€)",
        )),
    }


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
        "target": card.get("target"),
        "stop": card.get("stop"),
        "note": f"{card['title']}. Prix d'entrée et ISIN à confirmer depuis la fiche article.",
        "sourceConfidence": "medium",
    }


def synchronize(document: dict, current: list[dict], closed: list[dict], now: datetime) -> dict:
    positions = document.setdefault("positions", [])
    open_by_code = {
        item.get("mnemo"): item for item in positions if item.get("status") == "open"
    }
    detected_at = now.isoformat(timespec="seconds")
    added = 0
    closed_count = 0
    changed = False

    for card in current:
        existing = open_by_code.get(card["productCode"])
        if existing:
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
            for field in ("isin", "entryPrice", "target", "stop"):
                value = card.get(field)
                if value is not None and existing.get(field) != value:
                    existing[field] = value
                    changed = True
            if any(card.get(field) is not None for field in ("isin", "entryPrice", "target", "stop")):
                existing["sourceConfidence"] = "high"
            continue
        position = new_position(card, detected_at)
        positions.append(position)
        open_by_code[position["mnemo"]] = position
        added += 1
        changed = True

    for row in closed:
        position = open_by_code.get(row["mnemo"])
        if not position:
            continue
        position.update(
            {
                "status": "closed",
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
        return 0

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
